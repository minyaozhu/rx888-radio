# SPDX-License-Identifier: GPL-3.0-or-later
"""Transcribe what the SSB Voice page is listening to.

SSB has no carrier and no squelch: the receiver hears hiss and static all the time, and Whisper turns that
into invented words. So speech is found first, by voicing (rxapp/speech.py: a pitched voice in 30 ms frames),
and only utterances go to Whisper:

    voicing  per 40 ms frame, from the envelope's pitch pulses (speech.envelope_voiced_frames), which a
             mistuned SSB receiver doesn't disturb
    start    when 6 of the last 20 frames (0.8 s) are voiced; the 0.8 s before is kept (the first syllable)
    end      when at most 2 of the last 38 frames (1.5 s) are voiced; 0.3 s after the last voiced frame kept
    split    at 30 s; drop anything under 0.8 s or under 15% voiced overall (hiss: 1.8%)

The page sends a heartbeat (POST /api/ssb/transcribe/beat) while it plays with "Transcribe" or "Auto-tune" on;
without one for BEAT_SEC the transcriber lets go of the channel, so a closed tab doesn't keep it busy. With
auto-tune, each utterance's voice also measures the tuning error (rxapp/ssbtune.py) and the channel is retuned. Utterances and their
text go to rxapp_data/ssb/<YYYYMMDD>/ (transcripts.jsonl and WAVs), kept KEEP_DAYS.
"""
import asyncio
import json
import logging
import shutil
import time
import wave
from collections import deque
from pathlib import Path
from typing import List, Optional

import numpy as np

from .speech import ENV_FRAME_SEC as FRAME_SEC, envelope_voiced_frames
from .ssbtune import AutoTune, comb_scores

log = logging.getLogger("ssbvoice")

SSB_PROMPT = ("Amateur radio and HF voice. CQ CQ, QRZ, this is W6ABC, K6XYZ, N0CALL, VE7, portable, mobile, "
              "five nine, signal report, QSL, QTH, QSB, seventy-three, net control, check-ins, go ahead, over, "
              "roger, copy, Alpha Bravo Charlie Delta Echo Foxtrot. VOLMET, Gander, New York radio, Coast Guard.")
BEAT_SEC = 25.0                    # let go of the channel this long after the page's last heartbeat
KEEP_DAYS = 7


class Segmenter:
    """Continuous audio in, utterances out (see the module docstring for the rules)."""
    START_WIN, START_MIN = 20, 6
    END_WIN, END_MAX = 38, 2
    TAIL = 8                        # frames kept after the last voiced one
    MAX_SEC, MIN_SEC = 30.0, 0.8
    MIN_VOICED = 0.15               # an utterance's overall voiced share
    BLOCK = 8                       # frames judged together (0.32 s)...
    CONTEXT = 4                     # ...with this many frames before them, so the filters have no edge there

    def __init__(self, rate: int):
        self.rate = rate
        self.L = int(rate * FRAME_SEC)
        self.pending = np.zeros(0, np.int16)
        self.pending_t = None           # wall time of pending[0]
        self.ctx = np.zeros(0, np.int16)   # the audio just before pending, for the filters
        self.pre = deque(maxlen=self.START_WIN)    # (frame audio, voiced, t) before a start
        self.flags = deque(maxlen=self.END_WIN)
        self.seg = None                 # list of (frame, voiced, t) while talking

    def feed(self, a: np.ndarray, t_end: float) -> List[dict]:
        """a: int16 samples ending at wall time t_end. Returns the utterances that finished."""
        if self.pending_t is None or not len(self.pending):
            self.pending_t = t_end - len(a) / self.rate
        self.pending = np.concatenate([self.pending, np.asarray(a, np.int16)])
        out = []
        n = self.BLOCK * self.L
        while len(self.pending) >= n:
            block, self.pending = self.pending[:n], self.pending[n:]
            v = envelope_voiced_frames(np.concatenate([self.ctx, block]), self.rate)[len(self.ctx) // self.L:]
            self.ctx = block[-self.CONTEXT * self.L:]
            for k in range(self.BLOCK):
                fr = block[k * self.L:(k + 1) * self.L]
                out += self._frame(fr, bool(v[k]) if k < len(v) else False, self.pending_t)
                self.pending_t += FRAME_SEC
        return out

    def _frame(self, fr, voiced, t) -> List[dict]:
        self.flags.append(voiced)
        if self.seg is None:
            self.pre.append((fr, voiced, t))
            if sum(x[1] for x in self.pre) >= self.START_MIN:
                self.seg = list(self.pre)
                self.pre.clear()
                self.flags.clear()
            return []
        self.seg.append((fr, voiced, t))
        out = []
        ended = len(self.flags) == self.END_WIN and sum(self.flags) <= self.END_MAX
        if ended or len(self.seg) * FRAME_SEC >= self.MAX_SEC:
            out = self._emit(trim=ended)
            self.flags.clear()
        return out

    def _emit(self, trim: bool) -> List[dict]:
        seg, self.seg = self.seg, None
        if trim:
            last = max((i for i, x in enumerate(seg) if x[1]), default=len(seg) - 1)
            seg = seg[: last + 1 + self.TAIL]
        if len(seg) * FRAME_SEC < self.MIN_SEC:
            return []
        share = sum(x[1] for x in seg) / len(seg)
        if share < self.MIN_VOICED:
            return []
        return [{"audio": np.concatenate([x[0] for x in seg]), "t0": seg[0][2],
                 "t1": seg[-1][2] + FRAME_SEC, "voiced": round(share, 3)}]

    def flush(self) -> List[dict]:
        return self._emit(trim=True) if self.seg else []


def _wav(path: Path, a: np.ndarray, rate: int):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.asarray(a, "<i2").tobytes())


class SsbTranscriber:
    def __init__(self, radio, rtp, base: Path, enabled: bool = True):
        self.radio, self.rtp, self.base = radio, rtp, base
        self.enabled = enabled           # transcription allowed at all (the page's switch, kept in RUNTIME)
        self.transcribe = False          # ...and wanted by the page now
        self.autotune = False
        self.tuner = AutoTune()
        self.ssrc: Optional[int] = None
        self.last_beat = 0.0
        self.seg: Optional[Segmenter] = None
        self.q: asyncio.Queue = asyncio.Queue()
        self.busy = 0                    # utterances queued or being transcribed
        self.heard = 0                   # utterances found since the channel was taken
        self._cb = None
        self._tasks = []

    # -- lifecycle ---------------------------------------------------------------------------------
    def start(self):
        self._tasks = [asyncio.create_task(self._worker()), asyncio.create_task(self._watch())]
        self.prune()

    async def stop(self):
        self._release()
        for t in self._tasks:
            t.cancel()

    def set_enabled(self, on: bool):
        self.enabled = on
        if not on:
            self.transcribe = False
            if not self.autotune:
                self._release()

    def beat(self, ssrc: int, transcribe: bool = True, autotune: bool = False):
        """The page is playing this channel with Transcribe and / or Auto-tune on."""
        self.transcribe, self.autotune = self.enabled and transcribe, autotune
        if not (self.transcribe or self.autotune):
            self._release()
            return
        self.last_beat = time.time()
        if ssrc != self.ssrc:
            self._release()
            ch = self.radio.channels.get(ssrc)
            if ch is None:
                return
            self.ssrc = ssrc
            self.seg = Segmenter(int(ch.sample_rate or 12000))
            self.heard = 0
            self.tuner.new_station(ch.freq_hz)
            self._cb = lambda pcm, ts, seq: self._audio(pcm)
            self.rtp.subscribe(ssrc, self._cb)
            log.info("SSB transcription on channel %d (%.1f kHz %s)", ssrc, ch.freq_hz / 1e3, ch.mode)

    def _release(self):
        if self.ssrc is not None and self._cb is not None:
            self.rtp.unsubscribe(self.ssrc, self._cb)
            for u in (self.seg.flush() if self.seg else []):
                self._queue(u, tune=False)
            log.info("SSB transcription off channel %d", self.ssrc)
        self.ssrc, self._cb, self.seg = None, None, None

    async def _watch(self):
        while True:
            await asyncio.sleep(2)
            if self.ssrc is not None and time.time() - self.last_beat > BEAT_SEC:
                self._release()

    # -- audio -> utterances -> text ---------------------------------------------------------------
    def _audio(self, pcm: bytes):
        if self.seg is None:
            return
        for u in self.seg.feed(np.frombuffer(pcm, "<i2"), time.time()):
            self._queue(u)

    def _queue(self, u, tune: bool = True):
        ch = self.radio.channels.get(self.ssrc) if self.ssrc is not None else None
        u.update(freq_hz=ch.freq_hz if ch else None, mode=ch.mode if ch else None,
                 rate=self.seg.rate if self.seg else 12000)
        self.heard += 1
        if tune and self.autotune and ch is not None and ch.mode in ("usb", "lsb"):
            try:
                asyncio.get_running_loop().create_task(self._tune(u, self.ssrc))
            except RuntimeError:
                pass                     # no event loop (tests feeding audio directly)
        if self.transcribe:
            self.busy += 1
            self.q.put_nowait(u)

    async def _tune(self, u, ssrc):
        score, frames = await asyncio.to_thread(comb_scores, u["audio"], u["rate"])
        ch = self.radio.channels.get(ssrc)
        if ssrc != self.ssrc or not self.autotune or ch is None or ch.mode != u["mode"] \
                or abs(ch.freq_hz - u["freq_hz"]) >= 1:
            return                       # another channel or tuning since this was heard
        target = self.tuner.add(score, frames, ch.freq_hz, ch.mode, time.time())
        last = self.tuner.last
        if last and last["t"] >= time.time() - 1:
            log.info("SSB auto-tune %.2f kHz %s: voice %+.0f Hz (%d frames) -> %.2f kHz%s%s", ch.freq_hz / 1e3,
                     ch.mode, last["offset_hz"], last["frames"], last["to_hz"] / 1e3,
                     " (snapped)" if last["snapped"] else "", "" if last["moved"] else " (not moved)")
        if target is not None:
            try:
                await self.radio.tune(ssrc, target)
            except Exception:
                log.exception("SSB auto-tune")

    async def _worker(self):
        from .speech import transcribe
        while True:
            u = await self.q.get()
            try:
                r = await asyncio.to_thread(transcribe, u["audio"], u["rate"], SSB_PROMPT)
                self._save(u, r)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("SSB transcription")
            finally:
                self.busy -= 1

    def _save(self, u, r):
        day = self.base / time.strftime("%Y%m%d", time.localtime(u["t0"]))
        day.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%H%M%S", time.localtime(u["t0"])) + f"_{int(u['freq_hz'] or 0) // 100}"
        _wav(day / f"{stamp}.wav", u["audio"], u["rate"])
        rec = {"t0": round(u["t0"], 2), "t1": round(u["t1"], 2), "freq_hz": u["freq_hz"], "mode": u["mode"],
               "voiced": u["voiced"], "text": r.get("text", ""), "text_hint": r.get("text_atc", ""),
               "day": day.name, "clip": f"{stamp}.wav"}
        with open(day / "transcripts.jsonl", "a") as f:
            f.write(json.dumps(rec) + "\n")
        log.info("SSB %.1f kHz %4.1fs voiced %.2f: %s", (u["freq_hz"] or 0) / 1e3, u["t1"] - u["t0"], u["voiced"],
                 rec["text"] or "(no words)")

    # -- reading -----------------------------------------------------------------------------------
    def transcripts(self, after: float = 0.0, limit: int = 300, include_empty: bool = False) -> list:
        out = []
        for day in sorted((p for p in self.base.glob("[0-9]" * 8) if p.is_dir()), reverse=True)[:KEEP_DAYS]:
            f = day / "transcripts.jsonl"
            if not f.exists():
                continue
            for line in reversed(f.read_text().splitlines()):
                r = json.loads(line)
                if r["t0"] <= after:
                    break
                if include_empty or r.get("text"):
                    out.append(r)
                if len(out) >= limit:
                    return out
        return out

    def status(self) -> dict:
        ch = self.radio.channels.get(self.ssrc) if self.ssrc is not None else None
        return {"enabled": self.enabled, "listening": ch is not None, "ssrc": self.ssrc,
                "freq_hz": ch.freq_hz if ch else None, "mode": ch.mode if ch else None,
                "heard": self.heard, "pending": self.busy, "transcribing": self.transcribe and ch is not None,
                "autotune": {"on": self.autotune, **self.tuner.status()}}

    def prune(self, now: Optional[float] = None):
        cut = time.strftime("%Y%m%d", time.localtime((now or time.time()) - KEEP_DAYS * 86400))
        for day in self.base.glob("[0-9]" * 8):
            if day.is_dir() and day.name < cut:
                shutil.rmtree(day, ignore_errors=True)
