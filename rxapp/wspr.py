"""WSPR on every HF band at once.

WSPR beacons send their call sign, grid and power once in a 110.6 s transmission that starts 1 s after an
even UTC minute, as 4-tone FSK 6 Hz wide somewhere in 1400-1600 Hz of USB audio. One USB channel per WSPR
dial frequency; each channel's audio is cut into 2-minute slots (the FT8 monitor's RTP timing), and each
slot goes to WSJT-X's own decoder, wsprd. Spots (who was heard, how strongly, from where) go to the wspr
table, with the distance from this station's locator.

wsprd is built from WSJT-X's lib/wsprd (see README); its optional Fortran ordered-statistics decoder (-o) is
replaced by a stub, so it runs without a Fortran compiler.
"""

import asyncio
import logging
import math
import re
import subprocess
import time
import wave
from typing import Dict, List, Optional

import numpy as np

from .config import CONFIG, ROOT, RUNTIME
from .ft8 import RATE, Feed

log = logging.getLogger(__name__)

WSPRD = ROOT / "tools" / "wsjtx" / "lib" / "wsprd" / "wsprd"
SLOT_SEC = 120.0
DECODE_DELAY = 1.5
DECODE_JOBS = 3
FILTER_HZ = (1250.0, 1750.0)          # WSPR lives in 1400-1600 Hz of the USB audio
DIALS = [("160m", 1.8366e6), ("80m", 3.5686e6), ("60m", 5.2872e6), ("40m", 7.0386e6), ("30m", 10.1387e6),
         ("20m", 14.0956e6), ("17m", 18.1046e6), ("15m", 21.0946e6), ("12m", 24.9246e6), ("10m", 28.1246e6),
         ("6m", 50.293e6)]
# wsprd prints e.g. "1031 -15  0.3  14.097105  0  K6ABC CM87 33": (date/time or file name), snr, dt, MHz, drift, message
LINE = re.compile(r"^\S+\s+(?P<snr>-?\d+)\s+(?P<dt>-?\d+\.\d+)\s+(?P<mhz>\d+\.\d+)\s+(?P<drift>-?\d+)\s+(?P<msg>.+?)\s*$")
GRID = re.compile(r"^[A-R]{2}\d{2}([a-x]{2})?$", re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS wspr (
  id INTEGER PRIMARY KEY, t REAL NOT NULL, band TEXT NOT NULL, freq_hz REAL, snr REAL, dt REAL, drift INTEGER,
  call TEXT, grid TEXT, dbm INTEGER, km REAL, msg TEXT);
CREATE INDEX IF NOT EXISTS wspr_t ON wspr(t);
"""


def grid_ll(g: str):
    """Maidenhead locator (4 or 6 characters) -> (lat, lon) of its centre."""
    if not g or not GRID.match(g):
        return None
    g = g.upper()
    lon = (ord(g[0]) - 65) * 20 - 180 + int(g[2]) * 2
    lat = (ord(g[1]) - 65) * 10 - 90 + int(g[3])
    if len(g) >= 6:
        lon += (ord(g[4]) - 65) * (2 / 24) + 1 / 24
        lat += (ord(g[5]) - 65) / 24 + 1 / 48
    else:
        lon, lat = lon + 1, lat + 0.5
    return lat, lon


def km_between(a, b) -> Optional[float]:
    if not a or not b:
        return None
    r = math.pi / 180
    h = math.sin((b[0] - a[0]) * r / 2) ** 2 + math.cos(a[0] * r) * math.cos(b[0] * r) * math.sin((b[1] - a[1]) * r / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def parse(text: str, home: Optional[str] = None) -> List[dict]:
    """wsprd output lines -> spots. Messages: "CALL GRID DBM" (type 1), "CALL DBM" (2), "<CALL> GRID6 DBM" (3)."""
    out, seen = [], set()
    me = grid_ll(home) if home else None
    for line in text.splitlines():
        m = LINE.match(line.strip())
        if not m:
            continue
        parts = m["msg"].split()
        call, grid, dbm = parts[0].strip("<>"), None, None
        if len(parts) == 3 and GRID.match(parts[1]):
            grid, dbm = parts[1], parts[2]
        elif len(parts) == 2:
            dbm = parts[1]
        try:
            dbm = int(dbm) if dbm is not None else None
        except ValueError:
            dbm = None
        key = (call, round(float(m["mhz"]), 5))
        if key in seen:
            continue
        seen.add(key)
        out.append({"snr": int(m["snr"]), "dt": float(m["dt"]), "freq_hz": float(m["mhz"]) * 1e6,
                    "drift": int(m["drift"]), "call": call, "grid": grid, "dbm": dbm, "msg": m["msg"],
                    "km": round(km_between(me, grid_ll(grid)), 1) if me and grid else None})
    return out


class WsprMonitor:
    def __init__(self, radio, rtp, store):
        self.radio, self.rtp, self.store = radio, rtp, store
        self.feeds: Dict[str, Feed] = {}
        self._task: Optional[asyncio.Task] = None
        self._sem = asyncio.Semaphore(DECODE_JOBS)
        self.tmp = CONFIG.data_dir / "wspr_tmp"
        with store.lock:
            store.db.executescript(SCHEMA)

    @property
    def running(self):
        return self._task is not None

    def available(self):
        return [(b, f) for b, f in DIALS if f + 5e3 < CONFIG.max_freq_hz]

    async def start(self, bands: Optional[List[str]] = None):
        if not WSPRD.exists():
            raise RuntimeError(f"wsprd not found at {WSPRD}; see README (WSPR)")
        wanted = bands or [b for b, _ in DIALS]
        avail = dict(self.available())
        use = [b for b in wanted if b in avail]
        if not use:
            raise ValueError("no WSPR band fits the current coverage")
        await self.stop(save=False)
        self.tmp.mkdir(parents=True, exist_ok=True)
        for b in use:
            feed = Feed(b, avail[b], buffer_sec=SLOT_SEC + 30)       # the FT8 default (40 s) is too short
            ch = await self.radio.create(feed.dial_hz, "usb", label=f"WSPR {b}", kind="wspr", band="hf", filter_hz=FILTER_HZ)
            feed.ssrc = ch.ssrc
            self.rtp.subscribe(ch.ssrc, (lambda fd: lambda pcm, ts, seq: fd.add(pcm, ts))(feed))
            self.feeds[b] = feed
        self._task = asyncio.create_task(self._loop())
        RUNTIME.wspr_bands = wanted
        RUNTIME.save()
        self.store.event("wspr_start", bands=use)
        log.info("WSPR on %s", ", ".join(use))

    async def stop(self, save: bool = True):
        if self._task:
            self._task.cancel()
            self._task = None
        for feed in self.feeds.values():
            self.rtp.subs.pop(feed.ssrc, None)
            await self.radio.remove(feed.ssrc)
        self.feeds.clear()
        if save:
            RUNTIME.wspr_bands = None
            RUNTIME.save()
            self.store.event("wspr_stop")

    async def _loop(self):
        while True:
            now = time.time()
            t0 = math.floor(now / SLOT_SEC) * SLOT_SEC        # the 2-minute slot in progress (even minutes)
            await asyncio.sleep(t0 + SLOT_SEC + DECODE_DELAY - now)
            try:
                await asyncio.gather(*(self._decode(f, t0) for f in list(self.feeds.values())))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("WSPR slot %s", time.strftime("%H:%M", time.gmtime(t0)))

    async def _decode(self, feed: Feed, t0: float):
        audio = feed.cut(t0, SLOT_SEC)
        if audio is None:                      # the HF input wasn't selected for (most of) this slot
            feed.last_count = None
            return
        work = self.tmp / f"{feed.band}"
        work.mkdir(parents=True, exist_ok=True)
        path = work / f"{time.strftime('%y%m%d_%H%M', time.gmtime(t0))}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(audio.tobytes())
        try:
            async with self._sem:              # wsprd writes ALL_WSPR.TXT etc. in its working directory
                out = await asyncio.to_thread(subprocess.run, [str(WSPRD), "-f", f"{feed.dial_hz / 1e6:.6f}", path.name],
                                              cwd=work, capture_output=True, text=True, timeout=110)
            spots = parse(out.stdout, RUNTIME.qth)
        finally:
            path.unlink(missing_ok=True)
        feed.slots += 1
        feed.decodes += len(spots)
        feed.last_count, feed.last_slot = len(spots), t0
        if spots:
            with self.store.lock:
                self.store.db.executemany(
                    "INSERT INTO wspr (t,band,freq_hz,snr,dt,drift,call,grid,dbm,km,msg) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [(t0, feed.band, s["freq_hz"], s["snr"], s["dt"], s["drift"], s["call"], s["grid"], s["dbm"], s["km"], s["msg"])
                     for s in spots])
                self.store.db.commit()

    def info(self):
        return {"running": self.running, "decoder": WSPRD.exists(), "qth": RUNTIME.qth, "slot_sec": SLOT_SEC,
                "available": [b for b, _ in self.available()],
                "bands": [{"band": f.band, "dial_hz": f.dial_hz, "ssrc": f.ssrc,
                           "parked": getattr(self.radio.channels.get(f.ssrc), "parked", True),
                           "slots": f.slots, "decodes": f.decodes, "last_count": f.last_count, "last_slot": f.last_slot}
                          for f in self.feeds.values()]}
