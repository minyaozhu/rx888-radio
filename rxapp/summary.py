# SPDX-License-Identifier: GPL-3.0-or-later
"""On-demand situation summary of a P25 session's conversations, written by a local model.

The model runs in LM Studio on this computer (its OpenAI-style server on 127.0.0.1:1234), so the transcripts never
leave it. The server is started and the model loaded when a summary is asked for (lms server start / lms load,
with a TTL so LM Studio unloads it again after TTL_SEC unused). A span too long for one prompt is summarized in
parts first (notes per part), then the notes together.

The summary is kept next to the session's recordings (summary.json), never published, and leaves out what
identifies private people (the prompt asks for no names, phone numbers, plates or dates of birth).
"""
import json
import logging
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import List, Optional

log = logging.getLogger("summary")

LMS = Path.home() / ".lmstudio" / "bin" / "lms"
URL = "http://127.0.0.1:1234"
MODEL = "qwen/qwen3.8-27b"
IDENT = "rxapp-summary"            # the loaded model's name in LM Studio
CONTEXT = 32768
TTL_SEC = 900
CHUNK_CHARS = 40_000               # about 10k tokens of transcript per prompt (plus thinking and answer: < CONTEXT)

SYSTEM = """You write situation reports from radio dispatch transcripts of a public-safety trunked radio system in the \
San Francisco Bay Area (700 MHz P25: police, fire, EMS, transit and public works share it). Each line is one \
transmission: time, talkgroup (TG, a channel used by one agency or team), the radio ID that talked, and the words. \
The words come from automatic speech recognition and contain errors: misheard numbers, street names and codes. \
Be factual: only state what the transcripts support, say "unclear" when they don't, and never invent details. \
Leave out anything that identifies private people: no names of civilians, phone numbers, license plates, dates of \
birth or apartment numbers. Unit names (Engine 78, Medic 12), businesses, streets and cross streets are fine. Think briefly, then write."""

REPORT = """Write the situation report for these transmissions ({span}). Use this layout, in plain Markdown:

## Overview
Two to four sentences: how busy it was and what stood out.

## Incidents
One bullet per incident or event, most important first: **what** (type), where, which units, what happened in \
order (with times), and its status at the end (ongoing / resolved / unclear). Merge transmissions about the same \
event, even across talkgroups.

## Other activity
Short bullets for routine traffic worth knowing (status checks, transit operations, assignments).

## Talkgroups
One line per talkgroup that carried real traffic: what it seems to be used for.

Transmissions:
{lines}"""

NOTES = """These are transmissions {span} (part {i} of {n} of a longer period). Write compact notes for a later \
summary: one bullet per incident or event with time, type, place, units and outcome; then one line per talkgroup on \
what it carried. Facts only.

Transmissions:
{lines}"""

FROM_NOTES = """Here are notes on consecutive parts of {span}, in order. Combine them into the situation report \
(an incident that spans parts is one incident). Use this layout, in plain Markdown: ## Overview (two to four \
sentences), ## Incidents (one bullet each, most important first: what, where, units, what happened with times, \
status), ## Other activity, ## Talkgroups (one line each).

{notes}"""


def transcript_lines(threads: list, since: float = 0.0) -> List[str]:
    """The session's clear, transcribed calls after `since`, oldest first: "HH:MM:SS TG 1501 r13190001: words"."""
    calls = [c for th in threads for c in th["calls"]
             if c["t"] >= since and not c.get("enc") and (c.get("text") or "").strip() not in ("", "(no speech)")]
    calls.sort(key=lambda c: c["t"])
    return [f'{time.strftime("%H:%M:%S", time.localtime(c["t"]))} TG {c["tg"]} r{c["src"]}: {c["text"].strip()}'
            for c in calls]


def chunks(lines: List[str], size: Optional[int] = None) -> List[List[str]]:
    size = size or CHUNK_CHARS
    out, cur, n = [], [], 0
    for line in lines:
        if cur and n + len(line) > size:
            out.append(cur)
            cur, n = [], 0
        cur.append(line)
        n += len(line) + 1
    if cur:
        out.append(cur)
    return out


def _http(path: str, body: Optional[dict] = None, timeout: float = 5.0):
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _lms(*args, timeout: float = 300.0) -> str:
    r = subprocess.run([str(LMS), *args], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"lms {' '.join(args)}: {(r.stderr or r.stdout).strip()[-300:]}")
    return r.stdout


def ensure_model(say=lambda s: None):
    """LM Studio's server up (on localhost only) and the model loaded under IDENT."""
    if not LMS.exists():
        raise RuntimeError("LM Studio isn't installed (no ~/.lmstudio/bin/lms)")
    try:
        _http("/v1/models")
    except (urllib.error.URLError, OSError):
        say("starting LM Studio's local server…")
        _lms("server", "start", "--port", "1234", "--bind", "127.0.0.1", timeout=60)
        for _ in range(30):
            try:
                _http("/v1/models")
                break
            except (urllib.error.URLError, OSError):
                time.sleep(1)
    if IDENT not in _lms("ps", timeout=30):
        say("loading the model (first time: about a minute)…")
        _lms("load", MODEL, "-y", "--identifier", IDENT, "--ttl", str(TTL_SEC), "-c", str(CONTEXT), timeout=600)


THINK_TOKENS = 6000                # the model reasons before it answers (it can't be turned off); room for both


def chat(prompt: str, max_tokens: int = 2000) -> str:
    r = _http("/v1/chat/completions", {"model": IDENT, "temperature": 0.2, "max_tokens": max_tokens + THINK_TOKENS,
                                       "reasoning_effort": "low",
                                       "messages": [{"role": "system", "content": SYSTEM},
                                                    {"role": "user", "content": prompt}]}, timeout=1800)
    ch = r["choices"][0]
    text = re.sub(r"(?s)<think>.*?</think>", "", ch["message"].get("content") or "").strip()
    if not text:
        raise RuntimeError("the model ran out of room before writing (it spent its output thinking)" if ch.get("finish_reason") == "length"
                           else "the model wrote nothing")
    return text


def span_text(minutes: int, lines: List[str]) -> str:
    if not lines:
        return "no transmissions"
    a, b = lines[0][:5], lines[-1][:5]
    what = f"the last {minutes} minutes" if minutes and minutes < 120 else f"the last {minutes // 60} hours" if minutes else "the whole session"
    return f"{what}, {a}–{b}, {len(lines)} transmissions"


def summarize(lines: List[str], minutes: int, say=lambda s: None) -> str:
    span = span_text(minutes, lines)
    parts = chunks(lines)
    if len(parts) == 1:
        say(f"reading {len(lines)} transmissions…")
        return chat(REPORT.format(span=span, lines="\n".join(lines)))
    notes = []
    for i, part in enumerate(parts, 1):
        say(f"reading part {i} of {len(parts)} ({len(part)} transmissions)…")
        notes.append(f"### Part {i} ({part[0][:5]}–{part[-1][:5]})\n" + chat(NOTES.format(span=span, i=i, n=len(parts), lines="\n".join(part)), 1500))
    say("putting the parts together…")
    return chat(FROM_NOTES.format(span=span, notes="\n\n".join(notes)), 2500)


PLACES = """Here is a situation report. For each bullet under "## Incidents", in the same order, give where it happened. \
Reply with only a JSON array, one object per incident bullet:
{{"n": 1, "what": "2 to 5 words", "kind": "fire|medical|police|transit|traffic|other", \
"status": "ongoing|resolved|unclear", "time": "HH:MM of its first transmission", \
"address": "house number and street, or empty", "cross": ["street", "street"] or [], \
"place": "a named business, station, school or park, or empty", "city": "city, or empty"}}
Only use what the report says; leave a field empty when it doesn't say. Leave out apartment, unit and space numbers.

{report}"""

KINDS = ("fire", "medical", "police", "transit", "traffic", "other")


def extract_places(report: str) -> List[dict]:
    """The incidents' places as the model reads them from the report (one dict per incident bullet)."""
    text = chat(PLACES.format(report=report), 2500)
    m = re.search(r"(?s)\[.*\]", text)
    if not m:
        raise RuntimeError("the model didn't list the places")
    out = []
    for i, x in enumerate(json.loads(m.group(0)), 1):
        if not isinstance(x, dict):
            continue
        st = lambda k: str(x.get(k) or "").strip()
        m = re.search(r"\d+", str(x.get("n") or ""))
        out.append({"n": int(m.group(0)) if m else i, "what": st("what")[:60],
                    "kind": st("kind").lower() if st("kind").lower() in KINDS else "other",
                    "status": st("status").lower() if st("status").lower() in ("ongoing", "resolved") else "unclear",
                    "time": st("time")[:5], "address": st("address"), "place": st("place"), "city": st("city"),
                    "cross": [str(c).strip() for c in (x.get("cross") or []) if str(c).strip()][:2]})
    return out


def where_text(p: dict) -> str:
    w = p["address"] or (" & ".join(p["cross"]) if len(p["cross"]) == 2 else "") or p["place"] or (p["cross"] or [""])[0]
    if p["place"] and w != p["place"]:
        w = f'{p["place"]}, {w}'
    return ", ".join(x for x in (w, p["city"]) if x)


def pin(report: str, geo, say=lambda s: None) -> List[dict]:
    """The report's incidents with map positions where OpenStreetMap could place them (lat None where it couldn't)."""
    say("finding the places in the report…")
    places = extract_places(report)
    out = []
    for i, p in enumerate(places, 1):
        say(f"placing incident {i} of {len(places)} on the map…")
        try:
            loc = geo.locate(p) if (p["address"] or p["place"] or p["cross"]) else None
        except Exception as e:                       # one lookup failing doesn't lose the others
            log.warning("geo %s: %s", where_text(p), e)
            loc = None
        out.append({**{k: p[k] for k in ("n", "what", "kind", "status", "time")}, "where": where_text(p),
                    "lat": loc and loc["lat"], "lon": loc and loc["lon"], "how": loc and loc["how"],
                    "approx": bool(loc and loc["approx"])})
    return out


class Summarizer:
    """One summary at a time, in a thread; status() for the page to poll. The result is kept per session.
    After the report, its incidents are pinned on a map (geo: a geo.Geocoder; None leaves the map out)."""

    def __init__(self, geo=None):
        self.lock = threading.Lock()
        self.job = None
        self.geo = geo

    def status(self, session_dir: Path) -> dict:
        with self.lock:
            job = dict(self.job) if self.job else None
        last = None
        try:
            last = json.loads((session_dir / "summary.json").read_text())
        except (OSError, ValueError):
            pass
        return {"job": job if job and job["session"] == session_dir.name else None,
                "busy": bool(job and job["state"] == "running"), "last": last}

    def start(self, session_dir: Path, threads: list, minutes: int) -> bool:
        with self.lock:
            if self.job and self.job["state"] == "running":
                return False
            since = time.time() - minutes * 60 if minutes else 0.0
            lines = transcript_lines(threads, since)
            self.job = {"session": session_dir.name, "state": "running", "minutes": minutes, "started": time.time(),
                        "step": "starting…", "calls": len(lines)}
        threading.Thread(target=self._run, args=(session_dir, lines, minutes), daemon=True).start()
        return True

    def _pins(self, out: dict):
        if self.geo is None:
            return
        try:
            out["pins"] = pin(out["text"], self.geo, self._say)
        except Exception as e:                       # the report still stands without its map
            log.warning("summary pins: %s", e)
            out["pins_error"] = str(e)[:300]

    @staticmethod
    def _save(session_dir: Path, out: dict):
        tmp = session_dir / "summary.json.tmp"
        tmp.write_text(json.dumps(out))
        tmp.replace(session_dir / "summary.json")

    def start_pins(self, session_dir: Path) -> bool:
        """Pin the incidents of the session's existing summary (one written before the map, or whose pins failed)."""
        try:
            out = json.loads((session_dir / "summary.json").read_text())
            assert isinstance(out, dict) and out.get("text")
        except (OSError, ValueError, AssertionError):
            raise FileNotFoundError("the last summary can't be read; write a new one")
        with self.lock:
            if self.job and self.job["state"] == "running":
                return False
            self.job = {"session": session_dir.name, "state": "running", "minutes": out.get("minutes"),
                        "started": time.time(), "step": "starting…", "calls": out.get("calls", 0)}

        def run():
            try:
                ensure_model(self._say)
                out.pop("pins_error", None)
                self._pins(out)
                self._save(session_dir, out)
                with self.lock:
                    self.job.update(state="error" if "pins_error" in out else "done",
                                    step=out.get("pins_error", "done"))
            except Exception as e:
                log.warning("summary pins: %s", e)
                with self.lock:
                    self.job.update(state="error", step=str(e)[:300])
        threading.Thread(target=run, daemon=True).start()
        return True

    def _say(self, s):
        with self.lock:
            self.job["step"] = s

    def _run(self, session_dir: Path, lines: List[str], minutes: int):
        t0 = time.time()
        try:
            if not lines:
                raise RuntimeError("no transcribed calls in that span")
            ensure_model(self._say)
            text = summarize(lines, minutes, self._say)
            out = {"made": time.time(), "minutes": minutes, "span": span_text(minutes, lines), "calls": len(lines),
                   "model": MODEL, "took_sec": round(time.time() - t0), "text": text}
            self._pins(out)
            out["took_sec"] = round(time.time() - t0)
            self._save(session_dir, out)
            with self.lock:
                self.job.update(state="done", step="done", took_sec=out["took_sec"])
        except Exception as e:
            log.warning("summary: %s", e)
            with self.lock:
                self.job.update(state="error", step=str(e)[:300])
