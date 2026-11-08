# SPDX-License-Identifier: GPL-3.0-or-later
"""CW (Morse) skimmer on the HF bands: every signal in a band's CW segment decoded at once.

One wide USB channel per band (48 kHz output: the bottom ~24 kHz of the CW segment, where most CW is).
The audio goes through a short-time FFT (~47 Hz bins, a frame every ~5 ms), each frame normalised by its own
median (so radiod's AGC pumping and the noise floor cancel out). A bin that keeps going above the noise is a
keyed signal: it gets a Tone, which follows that bin's envelope, slices it into key-down / key-up runs with
an adaptive threshold, learns the sender's dot length from those runs (speed changes per station) and turns
the dots and dashes into text. Calls heard after CQ / DE / TEST become spots.

Pure decoding (Skimmer, Tone, unit_estimate, spots_in) needs no radio; CwMonitor wires it to radiod.
"""

import asyncio
import logging
import math
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

from .config import CONFIG, RUNTIME

log = logging.getLogger(__name__)

MORSE = {
    ".-": "A", "-...": "B", "-.-.": "C", "-..": "D", ".": "E", "..-.": "F", "--.": "G", "....": "H", "..": "I",
    ".---": "J", "-.-": "K", ".-..": "L", "--": "M", "-.": "N", "---": "O", ".--.": "P", "--.-": "Q", ".-.": "R",
    "...": "S", "-": "T", "..-": "U", "...-": "V", ".--": "W", "-..-": "X", "-.--": "Y", "--..": "Z",
    "-----": "0", ".----": "1", "..---": "2", "...--": "3", "....-": "4", ".....": "5", "-....": "6",
    "--...": "7", "---..": "8", "----.": "9",
    ".-.-.-": ".", "--..--": ",", "..--..": "?", "-..-.": "/", "-...-": "=", ".-.-.": "+", "-....-": "-",
    ".----.": "'", "-.--.": "(", "-.--.-": ")", "---...": ":", "...-.-": "<SK>", ".-...": "<AS>",
    "-.-.--": "!", "........": "<HH>",
}
TO_MORSE = {v: k for k, v in MORSE.items()}

BIN_HZ = 47.0               # FFT resolution target: narrow enough to split close signals, short enough for 40 WPM
ON_DB = 9.0                 # a bin this far above the frame's median counts as keyed (for finding signals)
ACTIVITY_SEC = 4.0          # time constant of the per-bin "keyed this often" average
ACTIVITY_MIN = 0.07         # a bin keyed this fraction of the time is a candidate signal
HIST_SEC = 4.0              # recent spectra kept, replayed into a new Tone so its first characters aren't lost
DROP_SEC = 15.0             # a Tone with no key-down this long has ended
CARRIER_SEC = 8.0           # ... and one with no key-up this long is a carrier, not CW
MAX_TONES = 48              # per band
WPM_MIN, WPM_MAX = 5.0, 60.0
WPM_NOISE = 50.0            # a "sender" this fast is noise the speed estimate chased to its limit
MIN_SNR = 6.0               # dB between key-down and key-up levels; below this a Tone is noise
DEBOUNCE = 2                # frames a level must hold before it counts (kills single-frame noise spikes)


def unit_estimate(durs) -> Optional[float]:
    """Dot length (same units as durs) from key-down and key-up run lengths.

    Dots and the gaps inside a character are one unit, dashes and gaps between characters three, word gaps
    seven: the shortest cluster is the unit. Two-means on log durations, the low centre wins."""
    d = np.asarray([x for x in durs if x > 0], float)
    if len(d) < 4:
        return None
    x = np.log(d)
    lo, hi = np.percentile(x, 15), np.percentile(x, 85)
    if hi - lo < math.log(1.8):           # one cluster (e.g. all dots and inner gaps): its centre
        return float(np.exp(np.median(x)))
    for _ in range(12):
        mid = (lo + hi) / 2
        a, b = x[x <= mid], x[x > mid]
        if not len(a) or not len(b):
            break
        lo, hi = a.mean(), b.mean()
    return float(np.exp(lo))


class Tone:
    """One keyed signal (an FFT bin): envelope -> key-down/up runs -> dots and dashes -> text."""

    def __init__(self, bin_: int, frame_sec: float, born: float):
        self.bin, self.fs = bin_, frame_sec
        self.frac = 0.0                         # where the signal sits between bins (-0.5..0.5)
        self.born = self.last_on = self.last_edge = born
        self.state, self.run, self.pend = False, 0, 0
        self.gap = 0
        self.mark_db: Optional[float] = None    # level while keyed (dB above the median), tracked
        self.noise_db: Optional[float] = None   # level while not keyed
        self.durs = deque(maxlen=60)            # recent run lengths (frames), keyed and not, for the unit
        self.unit: Optional[float] = None       # dot length in frames
        self.elems = ""
        self.text = ""
        self.spaced = True
        self.chars = 0
        self.spotted: set = set()

    @property
    def wpm(self) -> Optional[float]:
        return 1.2 / (self.unit * self.fs) if self.unit else None

    @property
    def snr(self) -> Optional[float]:
        return None if self.mark_db is None or self.noise_db is None else self.mark_db - self.noise_db

    @property
    def plausible(self) -> bool:
        """Looks like Morse, not noise: clear of the noise, a human speed, and text that isn't mostly the
        one- and two-element letters noise decodes to (E T I A M N: ~45% of English, ~75% of noise)."""
        if (self.snr or 0) < MIN_SNR or not self.wpm or self.wpm > WPM_NOISE:
            return False
        s = self.text.replace(" ", "")[-60:]
        return len(s) >= 4 and sum(c in "ETIAMN" for c in s) / len(s) < 0.62

    def prime(self, env: np.ndarray):
        """Levels and speed from recent history (before decoding it), so decoding starts calibrated."""
        self.mark_db = float(np.percentile(env, 92))
        self.noise_db = float(np.percentile(env, 30))
        self.step(env, emit=False)
        self.state, self.run, self.pend, self.gap = False, 0, 0, 0
        self.elems, self.spaced = "", True

    def _unit_bounds(self):
        lo = 1.2 / WPM_MAX / self.fs
        hi = 1.2 / WPM_MIN / self.fs
        return lo, hi

    def _learn(self):
        u = unit_estimate(self.durs)
        if u:
            lo, hi = self._unit_bounds()
            self.unit = min(hi, max(lo, u))

    def step(self, env: np.ndarray, t_end: float = 0.0, emit: bool = True):
        """Feed envelope frames (dB above the frame median), the last one ending at wall time t_end."""
        n = len(env)
        for i, v in enumerate(env):
            v = float(v)
            thr = self.noise_db + max(6.0, self.mark_db - self.noise_db) * 0.5
            want = v > thr + 1.0 if not self.state else v > thr - 1.0
            now = t_end - (n - i - 1) * self.fs
            if want != self.state:
                self.pend += 1
                if self.pend >= DEBOUNCE:
                    self._edge(emit, now)
                    continue
            else:
                self.run += 1 + self.pend
                self.pend = 0
            real = self.run >= self._min_mark()
            if self.state and real:
                self.mark_db += 0.02 * (v - self.mark_db)
                if emit:
                    self.last_on = now
            elif not self.state:
                self.noise_db += 0.01 * (v - self.noise_db)
                if emit and self.unit:
                    if self.elems and self.run >= 2.0 * self.unit:
                        self._char()
                    if not self.spaced and self.run >= 5.0 * self.unit:
                        self.text += " "
                        self.spaced = True

    def _min_mark(self) -> float:
        return max(DEBOUNCE + 1, 0.5 * self.unit) if self.unit else DEBOUNCE + 1

    def _edge(self, emit: bool, now: float):
        """The level changed (after the debounce): close the run that ended."""
        run, pend = self.run, self.pend
        if self.state:                                        # a key-down ended
            if run < self._min_mark():                        # a noise spike: it was all one gap
                self.state, self.run, self.pend = False, self.gap + run + pend, 0
                return
            if self.gap and self.gap < 12 * (self.unit or 1e9):   # word gaps and pauses don't say much
                self.durs.append(self.gap)
            self.durs.append(run)
            self._learn()
            if emit and self.unit:
                self.elems += "." if run < 2.0 * self.unit else "-"
                if len(self.elems) > 8:                       # not Morse (a carrier with drop-outs, noise)
                    self.elems = ""
            if emit:
                self.last_edge = now
        else:                                                 # a gap ended; judged when the mark after it ends
            self.gap = run
            if emit:
                self.last_edge = now
        self.state, self.run, self.pend = not self.state, pend, 0

    def _char(self):
        c = MORSE.get(self.elems)
        self.elems = ""
        if c:
            self.text += c
            self.chars += 1
            self.spaced = False
            if len(self.text) > 600:
                self.text = self.text[-400:]


class Skimmer:
    """All CW signals in one channel's audio. feed() it int16 samples as they come."""

    def __init__(self, rate: int, lo_hz: float = 200.0, hi_hz: Optional[float] = None):
        self.rate = rate
        self.nfft = 1 << int(round(math.log2(rate / BIN_HZ)))
        self.hop = self.nfft // 4
        self.bin_hz = rate / self.nfft
        self.fs = self.hop / rate                              # seconds per frame
        self.win = np.hanning(self.nfft).astype(np.float32)
        hi_hz = hi_hz or rate / 2 - 200
        self.b0, self.b1 = int(lo_hz / self.bin_hz), int(hi_hz / self.bin_hz)
        nb = self.b1 - self.b0
        self.buf = np.zeros(0, np.float32)
        self.hist: deque = deque(maxlen=int(HIST_SEC / self.fs))
        self.activity = np.zeros(nb, np.float32)
        self.level = np.zeros(nb, np.float32)                 # average dB above the median, per bin
        self.tones: Dict[int, Tone] = {}
        self.ended: List[Tone] = []

    def reset(self):
        self.ended.extend(self.tones.values())
        self.tones.clear()
        self.buf = np.zeros(0, np.float32)
        self.hist.clear()
        self.activity[:] = 0
        self.level[:] = 0

    def hz(self, b: float) -> float:
        """Audio frequency of bin index b (relative to b0)."""
        return (b + self.b0) * self.bin_hz

    def spectra(self, x: np.ndarray) -> np.ndarray:
        """dB above each frame's median, frames x bins (b0..b1)."""
        self.buf = np.concatenate([self.buf, x.astype(np.float32)])
        n = (len(self.buf) - self.nfft) // self.hop + 1
        if n <= 0:
            return np.zeros((0, self.b1 - self.b0), np.float32)
        frames = np.lib.stride_tricks.sliding_window_view(self.buf, self.nfft)[::self.hop][:n]
        p = np.abs(np.fft.rfft(frames * self.win, axis=1))[:, self.b0:self.b1] ** 2
        self.buf = self.buf[n * self.hop:]
        med = np.median(p, axis=1, keepdims=True) + 1e-9
        return (10 * np.log10(p / med + 1e-9)).astype(np.float32)

    def env(self, s: np.ndarray, b: int) -> np.ndarray:
        return s[:, b]          # its own bin only: a neighbour 150 Hz away is ~3 bins off

    def feed(self, samples: np.ndarray, t_end: Optional[float] = None):
        t_end = time.time() if t_end is None else t_end
        s = self.spectra(samples)
        n = len(s)
        if not n:
            return
        for tone in list(self.tones.values()):
            tone.step(self.env(s, tone.bin), t_end)
        # find new signals: bins keyed a fair share of the time, peaks of their neighbourhood
        k = 1 - math.exp(-n * self.fs / ACTIVITY_SEC)
        self.activity += k * ((s > ON_DB).mean(axis=0) - self.activity)
        self.level += k * (s.mean(axis=0) - self.level)
        self.hist.extend(s)
        self._discover(t_end)
        self._retire(t_end)

    def _discover(self, t_end: float):
        a = self.activity
        cand = np.flatnonzero(a > ACTIVITY_MIN)
        if not len(cand) or len(self.tones) >= MAX_TONES:
            return
        hist = np.asarray(self.hist)
        lv = self.level
        for b in cand[np.argsort(-lv[cand])]:
            if lv[b] < lv[max(0, b - 1):b + 2].max():
                continue                                      # a neighbour is the signal's centre
            if any(abs(b - t) <= 1 for t in self.tones):
                continue
            e = self.env(hist, b)
            if self._echo(hist, b, e):
                continue
            tone = Tone(int(b), self.fs, t_end)
            if 0 < b < len(lv) - 1:                           # peak between bins: parabola through three
                l, c, r = (float(v) for v in lv[b - 1:b + 2])
                den = l - 2 * c + r
                tone.frac = max(-0.5, min(0.5, 0.5 * (l - r) / den)) if den < 0 else 0.0
            tone.prime(e)
            tone.step(e, t_end)                               # decode the history: the first characters
            self.tones[int(b)] = tone
            if len(self.tones) >= MAX_TONES:
                break

    def _echo(self, hist, b, e) -> bool:
        """True if bin b only keys along with a stronger signal nearby (its key clicks or FFT leakage)."""
        on = e > ON_DB
        for t in self.tones:
            if 1 < abs(b - t) <= 10:
                o = self.env(hist, t)
                if o.mean() > e.mean() and on.any():
                    both = (on & (o > ON_DB)).sum() / on.sum()
                    if both > 0.8:
                        return True
        return False

    def _retire(self, now: float):
        for b, tone in list(self.tones.items()):
            weak = now - tone.born > 8 and not tone.plausible             # noise that got lucky
            if weak or now - tone.last_on > DROP_SEC or (tone.state and now - tone.last_edge > CARRIER_SEC):
                if tone.elems and tone.unit:
                    tone._char()
                self.tones.pop(b)
                if not weak:
                    self.ended.append(tone)
                self.activity[max(0, b - 2):b + 3] = 0        # a carrier must earn its way back in

    def live(self) -> List[Tone]:
        return [t for t in self.tones.values() if t.plausible]


# Calls worth spotting: after CQ (and the words that follow it: contest and activity names), DE or TEST.
# "CQ CQ DE K6ABC K6ABC K", "CQ SS N9RV", "TEST W1AW"
SPOT = re.compile(r"\b(?:CQ|DE|TEST|QRZ)\s+(?:(?:CQ|DE|TEST|DX|POTA|SOTA|NA|FD|SS|WW|QRP|CWT|SST|MST|NS|BUG)\s+)*"
                  r"([A-Z0-9]{1,3}/)?([A-Z0-9]*\d[A-Z0-9]*[A-Z])(/[A-Z0-9]{1,4})?\b")
CALL = re.compile(r"^[A-Z0-9]{1,3}\d{1,2}[A-Z]{1,4}$|^\d[A-Z]\d[A-Z]{1,3}$")


def spots_in(text: str) -> List[str]:
    """Call signs a station announces in decoded text ("CQ ... DE <call>", "CQ SS <call>", "TEST <call>").

    A call must be decoded at least twice (anywhere in the text) before it counts: one hit is too often a
    call with one letter wrong (AL4A heard as AL4E), the way the Reverse Beacon Network checks its spots."""
    out = []
    words = text.split()
    for m in SPOT.finditer(text):
        call = m[2]
        if not CALL.match(call) or not re.search(r"[A-Z]", call[:3]) or len(call) < 3:
            continue
        full = (m[1] or "") + call + (m[3] or "")
        if full not in out and sum(w == full for w in words) >= 2:
            out.append(full)
    return out


# ---- the monitor: radiod channels -> skimmers -> database ------------------------------------------------
RATE = 48000
FILTER_HZ = (200.0, 23800.0)
# USB dial per band: the channel hears dial + 0.2 .. dial + 23.8 kHz (the busy bottom of each CW segment)
DIALS = [("160m", 1.8170e6), ("80m", 3.5150e6), ("40m", 7.0100e6), ("30m", 10.1030e6), ("20m", 14.0100e6),
         ("17m", 18.0700e6), ("15m", 21.0100e6), ("12m", 24.8900e6), ("10m", 28.0100e6), ("6m", 50.0800e6)]
TICK_SEC = 1.0
SPOT_REPEAT_SEC = 600       # the same call on the same band is spotted again after this long
MIN_TEXT_CHARS = 12         # a finished transmission shorter than this isn't kept

SCHEMA = """
CREATE TABLE IF NOT EXISTS cw_spots (
  id INTEGER PRIMARY KEY, t REAL NOT NULL, band TEXT NOT NULL, freq_hz REAL, call TEXT, wpm REAL, snr REAL);
CREATE INDEX IF NOT EXISTS cw_spots_t ON cw_spots(t);
CREATE TABLE IF NOT EXISTS cw_text (
  id INTEGER PRIMARY KEY, t REAL NOT NULL, t_end REAL, band TEXT NOT NULL, freq_hz REAL, wpm REAL, snr REAL, text TEXT);
CREATE INDEX IF NOT EXISTS cw_text_t ON cw_text(t);
"""


@dataclass
class CwFeed:
    band: str
    dial_hz: float
    ssrc: Optional[int] = None
    pending: list = field(default_factory=list)
    last_ts: Optional[int] = None
    skim: Skimmer = field(default_factory=lambda: Skimmer(RATE, *FILTER_HZ))
    restart: bool = False

    def add(self, pcm: bytes, ts: int, seq: int):
        a = np.frombuffer(pcm, "<i2")
        if self.last_ts is not None:
            d = (ts - self.last_ts) & 0xFFFFFFFF
            if RATE < d < (1 << 32) - RATE:                    # a second or more missing: a new timeline
                self.pending.clear()
                self.restart = True
        self.last_ts = (ts + len(a)) & 0xFFFFFFFF
        self.pending.append(a)

    def freq(self, tone: Tone) -> float:
        return self.dial_hz + self.skim.hz(tone.bin + tone.frac)


class CwMonitor:
    def __init__(self, radio, rtp, store):
        self.radio, self.rtp, self.store = radio, rtp, store
        self.feeds: Dict[str, CwFeed] = {}
        self._task: Optional[asyncio.Task] = None
        self._recent: Dict[tuple, float] = {}          # (band, call) -> last spotted
        with store.lock:
            store.db.executescript(SCHEMA)

    @property
    def running(self):
        return self._task is not None

    def available(self):
        return [(b, f) for b, f in DIALS if f + RATE / 2 < CONFIG.max_freq_hz]

    async def start(self, bands: Optional[List[str]] = None):
        wanted = bands or [b for b, _ in DIALS]
        avail = dict(self.available())
        use = [b for b in wanted if b in avail]
        if not use:
            raise ValueError("no CW band fits the current coverage")
        await self.stop(save=False)
        for b in use:
            feed = CwFeed(b, avail[b])
            ch = await self.radio.create(feed.dial_hz, "usb", label=f"CW {b}", kind="cw", band="hf",
                                         filter_hz=FILTER_HZ, rate=RATE)
            feed.ssrc = ch.ssrc
            self.rtp.subscribe(ch.ssrc, feed.add)
            self.feeds[b] = feed
        self._task = asyncio.create_task(self._loop())
        RUNTIME.cw_bands = wanted
        RUNTIME.save()
        self.store.event("cw_start", bands=use)
        log.info("CW skimmer on %s", ", ".join(use))

    async def stop(self, save: bool = True):
        if self._task:
            self._task.cancel()
            self._task = None
        for feed in self.feeds.values():
            self.rtp.unsubscribe(feed.ssrc, feed.add)
            await self.radio.remove(feed.ssrc)
            feed.skim.reset()
            self._finish(feed)
        self.feeds.clear()
        if save:
            RUNTIME.cw_bands = None
            RUNTIME.save()
            self.store.event("cw_stop")

    async def _loop(self):
        while True:
            await asyncio.sleep(TICK_SEC)
            try:
                for feed in list(self.feeds.values()):
                    if feed.restart:
                        feed.restart = False
                        feed.skim.reset()
                    if not feed.pending:
                        continue
                    x = np.concatenate(feed.pending)
                    feed.pending = []
                    await asyncio.to_thread(feed.skim.feed, x, time.time())
                    self._spot(feed)
                    self._finish(feed)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("CW skimmer")

    def _spot(self, feed: CwFeed):
        now, rows = time.time(), []
        for tone in feed.skim.live():
            for call in spots_in(tone.text):
                if call in tone.spotted:
                    continue
                tone.spotted.add(call)
                key = (feed.band, call)
                if now - self._recent.get(key, 0) < SPOT_REPEAT_SEC:
                    continue
                self._recent[key] = now
                rows.append((now, feed.band, round(feed.freq(tone), 1), call,
                             round(tone.wpm or 0, 1), round(tone.snr or 0, 1)))
        if rows:
            with self.store.lock:
                self.store.db.executemany("INSERT INTO cw_spots (t,band,freq_hz,call,wpm,snr) VALUES (?,?,?,?,?,?)", rows)
                self.store.db.commit()
        if len(self._recent) > 5000:
            self._recent = {k: v for k, v in self._recent.items() if now - v < SPOT_REPEAT_SEC}

    def _finish(self, feed: CwFeed):
        """Keep the text of signals that have ended."""
        done, feed.skim.ended = feed.skim.ended, []
        rows = [(t.born, t.last_on, feed.band, round(feed.freq(t), 1), round(t.wpm or 0, 1), round(t.snr or 0, 1),
                 t.text.strip()) for t in done if t.chars >= MIN_TEXT_CHARS and t.plausible]
        if rows:
            with self.store.lock:
                self.store.db.executemany("INSERT INTO cw_text (t,t_end,band,freq_hz,wpm,snr,text) VALUES (?,?,?,?,?,?,?)", rows)
                self.store.db.commit()

    def live(self, band: Optional[str] = None):
        out = []
        for feed in list(self.feeds.values()):
            if band and feed.band != band:
                continue
            for t in feed.skim.live():
                out.append({"band": feed.band, "freq_hz": round(feed.freq(t), 1), "wpm": round(t.wpm or 0, 1),
                            "snr": round(t.snr or 0, 1), "text": t.text[-240:], "since": t.born,
                            "last": t.last_on, "keyed": t.state})
        return sorted(out, key=lambda r: r["freq_hz"])

    def info(self):
        return {"running": self.running, "available": [b for b, _ in self.available()],
                "bands": [{"band": f.band, "dial_hz": f.dial_hz, "lo_hz": f.dial_hz + FILTER_HZ[0],
                           "hi_hz": f.dial_hz + FILTER_HZ[1], "ssrc": f.ssrc,
                           "parked": getattr(self.radio.channels.get(f.ssrc), "parked", True),
                           "tones": len(f.skim.live())} for f in self.feeds.values()]}
