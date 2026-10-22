"""FT8 on every HF band at once.

One USB channel per FT8 dial frequency (radiod demodulates them all from the same ADC stream).
Each channel's audio is cut into 15 s UTC slots, decoded with ft8_lib's decode_ft8, and the
messages go into the database, grouped later into conversations (who called whom).

Slot timing uses RTP timestamps, not packet arrival: each channel keeps the smallest
(arrival - timestamp/rate) seen recently as its clock offset, which is the arrival time of a
packet that wasn't delayed, so jitter doesn't smear the slot edges.
"""

import asyncio
import logging
import math
import re
import subprocess
import time
import wave
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

from .config import CONFIG, ROOT, RUNTIME

log = logging.getLogger(__name__)

DECODER = ROOT / "tools" / "ft8_lib" / "decode_ft8"
SLOT_SEC = 15.0
RATE = 12000                 # decode_ft8 expects 12 kHz
DECODE_DELAY = 1.0           # wait this long after a slot ends for its last packets
MIN_FILL = 0.85              # skip a slot with less audio than this (channel paused or just started)
BUFFER_SEC = 40.0
OFFSET_WINDOW = 30.0         # clock offset = min over this many seconds of packets
DECODE_JOBS = 4
FILTER_HZ = (100.0, 3200.0)  # FT8 sub-band: 200-3000 Hz audio, with some margin
# radiod's filtering delays the audio by about this much beyond packet arrival (measured: median
# decode dt was 0.80 s, against 0.5 s for an on-time FT8 transmission)
LATENCY_SEC = 0.3
TX_START_SEC = 0.5           # FT8 transmissions start 0.5 s into the slot; dt is reported relative to it

# FT8 dial frequencies (USB), lowest to highest
DIALS = [("160m", 1.840e6), ("80m", 3.573e6), ("60m", 5.357e6), ("40m", 7.074e6), ("30m", 10.136e6),
         ("20m", 14.074e6), ("17m", 18.100e6), ("15m", 21.074e6), ("12m", 24.915e6), ("10m", 28.074e6),
         ("6m", 50.313e6)]

# decode_ft8 prints e.g. "000000 +13.5 +0.08 1497 ~  N2SAB W5WCA DN70":
# slot, score (ft8_lib's rough SNR), dt s, audio Hz, '~', message
LINE = re.compile(r"^\s*(?P<nums>(?:[-+]?\d+(?:\.\d+)?\s+){2,6})[~:]?\s*(?P<msg>[A-Z0-9/<>+\-. ]+?)\s*$")
CALL = re.compile(r"^(?:[A-Z0-9]{1,3}/)?[A-Z0-9]{1,3}\d[A-Z0-9]{0,4}(?:/[A-Z0-9]{1,4})?$|^<[^>]+>$")
GRID = re.compile(r"^[A-R]{2}\d{2}$")


def parse(text: str) -> List[dict]:
    rows, seen = [], set()
    for line in text.splitlines():
        m = LINE.match(line)
        if not m or not re.search(r"[A-Z]", m["msg"]):
            continue
        msg = " ".join(m["msg"].split())
        if msg in seen:
            continue
        seen.add(msg)
        nums = [float(n) for n in m["nums"].split()]
        if len(nums) < 3:
            continue
        score, dt, hz = nums[-3:]
        rows.append({"score": score, "dt": dt, "hz": hz, "msg": msg, **parties(msg)})
    return rows


def parties(msg: str) -> dict:
    """Standard messages are '<to> <from> <extra>' or 'CQ [DX|POTA..] <from> <grid>'."""
    t = msg.split()
    if t and t[0] == "CQ":
        rest = t[1:]
        if len(rest) > 1 and not CALL.match(rest[0]):
            rest = rest[1:]                       # CQ DX / CQ POTA ...
        return {"to_call": "CQ", "from_call": rest[0] if rest else None, "extra": " ".join(rest[1:])}
    if len(t) >= 2 and CALL.match(t[0]) and CALL.match(t[1]):
        return {"to_call": t[0], "from_call": t[1], "extra": " ".join(t[2:])}
    return {"to_call": None, "from_call": None, "extra": msg}


def grid_of(extra: str) -> Optional[str]:
    g = (extra or "").split()[:1]
    return g[0] if g and GRID.match(g[0]) and g[0] != "RR73" else None


@dataclass
class Feed:
    """One band: its channel and recent audio, keyed by unwrapped RTP timestamp."""
    band: str
    dial_hz: float
    ssrc: Optional[int] = None
    chunks: deque = field(default_factory=deque)       # (ts_start, int16 array)
    offsets: deque = field(default_factory=deque)      # (arrival, arrival - ts/RATE)
    last_ts: Optional[int] = None
    ts_hi: int = 0                                      # wraps of the 32-bit timestamp
    slots: int = 0
    decodes: int = 0
    last_count: Optional[int] = None
    last_slot: Optional[float] = None

    def reset(self):
        self.chunks.clear()
        self.offsets.clear()
        self.last_ts = None
        self.ts_hi = 0

    def add(self, pcm: bytes, ts: int):
        now = time.time()
        a = np.frombuffer(pcm, "<i2")
        if self.last_ts is not None:
            if ts < self.last_ts and self.last_ts - ts > 1 << 31:
                self.ts_hi += 1 << 32             # 32-bit timestamp wrapped
            elif abs(ts - self.last_ts) > RATE * 5:
                self.reset()                      # channel re-created (radiod restart): new timeline
        self.last_ts = ts
        t = self.ts_hi + ts
        self.chunks.append((t, a))
        # the packet's last sample arrived at 'now'
        self.offsets.append((now, now - (t + len(a)) / RATE))
        while self.offsets and now - self.offsets[0][0] > OFFSET_WINDOW:
            self.offsets.popleft()
        while self.chunks and (t - self.chunks[0][0]) / RATE > BUFFER_SEC:
            self.chunks.popleft()

    def offset(self) -> Optional[float]:
        return min(o for _, o in self.offsets) if self.offsets else None

    def cut(self, t0: float, seconds: float) -> Optional[np.ndarray]:
        """Audio for wall-clock [t0, t0 + seconds), gaps zero-filled; None if mostly missing."""
        off = self.offset()
        if off is None:
            return None
        off -= LATENCY_SEC
        s0 = int(round((t0 - off) * RATE))
        n = int(seconds * RATE)
        out = np.zeros(n, np.int16)
        got = 0
        for ts, a in list(self.chunks):
            i, j = max(ts, s0), min(ts + len(a), s0 + n)
            if j > i:
                out[i - s0:j - s0] = a[i - ts:j - ts]
                got += j - i
        return out if got >= MIN_FILL * n else None


class Ft8:
    def __init__(self, radio, rtp, store):
        self.radio, self.rtp, self.store = radio, rtp, store
        self.feeds: Dict[str, Feed] = {}
        self._task: Optional[asyncio.Task] = None
        self._sem = asyncio.Semaphore(DECODE_JOBS)
        self.tmp = CONFIG.data_dir / "ft8_tmp"
        store.ensure_ft8()

    @property
    def running(self):
        return self._task is not None

    @staticmethod
    def available():
        return [(b, f) for b, f in DIALS if f + 5e3 < CONFIG.max_freq_hz]

    async def start(self, bands: Optional[List[str]] = None):
        if not DECODER.exists():
            raise RuntimeError(f"decode_ft8 not found at {DECODER}; see README (ft8_lib)")
        wanted = bands or [b for b, _ in DIALS]
        avail = dict(self.available())
        bands = [b for b in wanted if b in avail]          # 6 m needs the 0-64 MHz coverage
        if not bands:
            raise ValueError("no FT8 band fits the current coverage")
        await self.stop(save=False)
        self.tmp.mkdir(parents=True, exist_ok=True)
        for b in bands:
            feed = Feed(b, avail[b])
            ch = await self.radio.create(feed.dial_hz, "usb", label=f"FT8 {b}", kind="ft8", band="hf",
                                         filter_hz=FILTER_HZ)
            feed.ssrc = ch.ssrc
            self.rtp.subscribe(ch.ssrc, self._sub(feed))
            self.feeds[b] = feed
        self._task = asyncio.create_task(self._loop())
        RUNTIME.ft8_bands = wanted                         # kept for when the coverage allows more
        RUNTIME.save()
        self.store.event("ft8_start", bands=bands)
        log.info("FT8 on %s", ", ".join(bands))

    async def stop(self, save: bool = True):
        if self._task:
            self._task.cancel()
            self._task = None
        for feed in self.feeds.values():
            self.rtp.subs.pop(feed.ssrc, None)
            await self.radio.remove(feed.ssrc)
        self.feeds.clear()
        if save:
            RUNTIME.ft8_bands = None
            RUNTIME.save()
            self.store.event("ft8_stop")

    @staticmethod
    def _sub(feed):
        return lambda pcm, ts, seq: feed.add(pcm, ts)

    async def _loop(self):
        while True:
            now = time.time()
            t0 = math.floor(now / SLOT_SEC) * SLOT_SEC        # the slot in progress
            await asyncio.sleep(t0 + SLOT_SEC + DECODE_DELAY - now)
            try:
                await asyncio.gather(*(self._decode(feed, t0) for feed in list(self.feeds.values())))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("FT8 slot %s", time.strftime("%H:%M:%S", time.gmtime(t0)))

    async def _decode(self, feed: Feed, t0: float):
        audio = feed.cut(t0, SLOT_SEC)
        if audio is None:
            feed.last_count = None
            return
        path = self.tmp / f"{feed.band}_{int(t0)}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(audio.tobytes())
        try:
            async with self._sem:
                out = await asyncio.to_thread(
                    subprocess.run, [str(DECODER), str(path)], capture_output=True, text=True, timeout=30)
            rows = parse(out.stdout)
            for r in rows:
                r["dt"] = round(r["dt"] - TX_START_SEC, 2)       # like WSJT-X: 0 = on time
        finally:
            path.unlink(missing_ok=True)
        feed.slots += 1
        feed.decodes += len(rows)
        feed.last_count, feed.last_slot = len(rows), t0
        if rows:
            self.store.add_ft8(t0, feed.band, feed.dial_hz, rows)

    def info(self):
        return {"running": self.running, "decoder": DECODER.exists(),
                "available": [b for b, _ in self.available()], "slot_sec": SLOT_SEC,
                "bands": [{"band": f.band, "dial_hz": f.dial_hz, "ssrc": f.ssrc,
                           "parked": getattr(self.radio.channels.get(f.ssrc), "parked", True),
                           "slots": f.slots, "decodes": f.decodes, "last_count": f.last_count,
                           "last_slot": f.last_slot} for f in self.feeds.values()]}


def conversations(rows: List[dict]) -> dict:
    """Group decodes (oldest first) into QSOs between station pairs, plus CQ callers."""
    convs: Dict[tuple, dict] = {}
    cqs: Dict[str, dict] = {}
    for r in rows:
        to, frm = r["to_call"], r["from_call"]
        if to == "CQ" and frm:
            c = cqs.setdefault(frm, {"call": frm, "band": r["band"], "grid": None, "first": r["t"], "count": 0})
            c["count"] += 1
            c["last"] = r["t"]
            c["grid"] = grid_of(r["extra"]) or c["grid"]
        elif to and frm:
            key = (r["band"],) + tuple(sorted((to, frm)))
            c = convs.setdefault(key, {"band": r["band"], "a": key[1], "b": key[2], "messages": []})
            c["messages"].append({k: r[k] for k in ("t", "score", "hz", "msg", "from_call", "to_call", "extra")})
    out = []
    for c in convs.values():
        extras = [m["extra"] for m in c["messages"]]
        c["complete"] = any(e in ("RR73", "73", "RRR") for e in extras)
        c["first"], c["last"] = c["messages"][0]["t"], c["messages"][-1]["t"]
        out.append(c)
    out.sort(key=lambda c: c["last"], reverse=True)
    return {"conversations": out, "cq": sorted(cqs.values(), key=lambda c: c["last"], reverse=True)}
