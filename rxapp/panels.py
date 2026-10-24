# SPDX-License-Identifier: GPL-3.0-or-later
"""Spectrum feeds for the Radio page's panel grid: several ranges at once, live where the receiver can see them.

The RX-888 has one input at a time. On HF every panel range below the coverage gets its own radiod spectrum
channel, so all HF panels are live together. On VHF the tuner shows one 8.4 MHz window: one feed covers it, and
each VHF panel takes the part that overlaps. Feeds remember their last frame (VHF: one per tuner position), so a
panel can show what its range looked like when the receiver was last there.

Wire format (binary, little-endian): u32 feed id, f64 frequency of the first bin, f32 bin width, u32 bins,
f64 time of the frame, then that many f32 dB values, lowest frequency first.
"""

import asyncio
import logging
import time
from typing import Dict, List, Optional, Set, Tuple

import numpy as np
from ka9q import SpectrumStream

from .config import CONFIG
from .spectrum import LIVE_VHF_SPAN

log = logging.getLogger(__name__)

BINS = 2048
SEND_EVERY = 0.2          # seconds between frames per feed (5 fps)
HISTORY = 60              # VHF tuner positions remembered
VHF_ID = 0
SAVE_EVERY = 60.0         # seconds: the last pictures survive an rxapp restart
HISTORY_FILE = CONFIG.data_dir / "panels_history.npz"


def frame_bytes(fid: int, first_hz: float, bin_hz: float, db: np.ndarray, t: float) -> bytes:
    return (np.array([fid], "<u4").tobytes() + np.array([first_hz], "<f8").tobytes()
            + np.array([bin_hz], "<f4").tobytes() + np.array([len(db)], "<u4").tobytes()
            + np.array([t], "<f8").tobytes() + db.astype("<f4").tobytes())


class Feed:
    def __init__(self, fid: int, kind: str, lo: float, hi: float):
        self.id, self.kind, self.lo, self.hi = fid, kind, lo, hi
        self.stream: Optional[SpectrumStream] = None
        self.radiod_center: Optional[float] = None
        self.center = (lo + hi) / 2
        self.bin = (hi - lo) / BINS
        self.latest: Optional[Tuple[float, float, np.ndarray, float]] = None   # first_hz, bin, db, t
        self.got = 0.0            # when the last frame arrived (stall watchdog)


class PanelHub:
    def __init__(self, radio, spectrum):
        self.radio, self.spectrum = radio, spectrum
        self.hf: Dict[Tuple[float, float], Feed] = {}      # by (lo, hi)
        self.vhf = Feed(VHF_ID, "vhf", 0, 0)
        self.vhf_history: Dict[int, Tuple[float, float, np.ndarray, float]] = {}   # tuner kHz -> frame
        self.clients: Dict[object, Set[Tuple[float, float]]] = {}
        self._next = 1
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._task: Optional[asyncio.Task] = None
        self._state = None        # (band, tuner centre, coverage) the streams were built for
        self._lock = asyncio.Lock()
        self._saved = 0.0
        self.load()

    # -- last pictures on disk ----------------------------------------------------------------------
    def snapshot(self):
        return [("v", k, fr) for k, fr in list(self.vhf_history.items())] + \
               [("h", f"{lo:.0f}:{hi:.0f}", f.latest) for (lo, hi), f in list(self.hf.items()) if f.latest]

    def save(self, frames=None):
        arrays, meta = {}, []
        frames = self.snapshot() if frames is None else frames
        for i, (kind, key, (first, bin_hz, db, t)) in enumerate(frames):
            arrays[f"db{i}"] = np.asarray(db, np.float32)
            meta.append((kind, str(key), first, bin_hz, t))
        if not meta:
            return
        tmp = HISTORY_FILE.with_suffix(".tmp.npz")
        np.savez_compressed(tmp, meta=np.array(meta, dtype=object), **arrays)
        tmp.replace(HISTORY_FILE)

    def load(self):
        try:
            with np.load(HISTORY_FILE, allow_pickle=True) as z:
                for i, (kind, key, first, bin_hz, t) in enumerate(z["meta"]):
                    fr = (float(first), float(bin_hz), z[f"db{i}"], float(t))
                    if kind == "v":
                        self.vhf_history[int(key)] = fr
                    else:
                        lo, hi = (float(x) for x in key.split(":"))
                        self.feed_for(lo, hi).latest = fr
            log.info("panels: %d VHF windows and %d HF pictures from %s", len(self.vhf_history),
                     sum(1 for f in self.hf.values() if f.latest), HISTORY_FILE.name)
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("panels: could not read %s: %s", HISTORY_FILE, e)

    # -- clients -------------------------------------------------------------------------------------
    def wanted(self) -> Set[Tuple[float, float]]:
        out = set()
        for ranges in self.clients.values():
            out |= ranges
        return out

    def feed_for(self, lo: float, hi: float) -> Feed:
        key = (lo, hi)
        if key not in self.hf:
            self.hf[key] = Feed(self._next, "hf", lo, hi)
            self._next += 1
        return self.hf[key]

    def describe(self, ranges) -> Dict:
        return {"band": self.radio.band, "coverage_hz": CONFIG.max_freq_hz,
                "vhf": {"id": VHF_ID, "center_hz": self.radio.vhf.center_hz, "span_hz": LIVE_VHF_SPAN},
                "feeds": [{"id": self.feed_for(lo, hi).id, "lo": lo, "hi": hi} for lo, hi in ranges]}

    def history(self, ranges) -> List[bytes]:
        """The last frame of every feed a client asked for, and every remembered VHF window."""
        out = []
        for lo, hi in ranges:
            f = self.hf.get((lo, hi))
            if f and f.latest:
                out.append(frame_bytes(f.id, *f.latest))
        for fr in self.vhf_history.values():
            out.append(frame_bytes(VHF_ID, *fr))
        return out

    # -- streams -------------------------------------------------------------------------------------
    def _on_frame(self, feed: Feed):
        def cb(st):
            db = st.spectrum.bin_power_db
            if db is None or len(db) == 0:
                return
            self._loop.call_soon_threadsafe(self._publish, feed, st.frequency, np.asarray(db, np.float32))
        return cb

    def _publish(self, feed: Feed, radiod_center: float, db: np.ndarray):
        if feed.radiod_center is None or abs(radiod_center - feed.radiod_center) > feed.bin:
            return                                  # from before a retune
        now = time.time()
        feed.got = now
        if feed.kind == "vhf":
            if self.radio.vhf_paused or self.spectrum.sweeping or self.radio.band != "vhf":
                return                              # the tuner is being swept: not this window
            db = db[::-1]                           # the tuner's IF is inverted
            c = self.radio.vhf.center_hz
            first = c - len(db) // 2 * feed.bin
            feed.latest = (first, feed.bin, db, now)
            self.vhf_history[int(round(c / 1e3))] = feed.latest
            if len(self.vhf_history) > HISTORY:
                del self.vhf_history[min(self.vhf_history, key=lambda k: self.vhf_history[k][3])]
        else:
            first = feed.center - len(db) // 2 * feed.bin
            feed.latest = (first, feed.bin, db, now)

    async def _open(self, feed: Feed, radiod_center: float, bins: int, bin_hz: float):
        feed.radiod_center, feed.bin, feed.got = radiod_center, bin_hz, time.time()
        feed.stream = SpectrumStream(control=self.radio.ctl, frequency_hz=radiod_center, bin_count=bins,
                                     resolution_bw=bin_hz, on_spectrum=self._on_frame(feed))
        await asyncio.to_thread(feed.stream.start)

    async def _close(self, feed: Feed):
        if feed.stream:
            s, feed.stream, feed.radiod_center = feed.stream, None, None
            try:
                await asyncio.to_thread(s.stop)
            except Exception:
                pass

    async def _sync(self, force: bool = False):
        """Streams for what is wanted on the input that is selected."""
        async with self._lock:
            band = self.radio.band
            state = (band, round(self.radio.vhf.center_hz or 0), CONFIG.max_freq_hz)
            wanted = {k for k in self.wanted() if k[1] <= CONFIG.max_freq_hz} if band == "hf" else set()
            if force or state != self._state:
                for f in list(self.hf.values()):
                    await self._close(f)
                await self._close(self.vhf)
                self._state = state
            for key, f in list(self.hf.items()):
                if f.stream and key not in wanted:
                    await self._close(f)
            # zooming makes a feed per zoom level: forget the oldest idle ones
            idle = sorted((k for k, f in list(self.hf.items()) if not f.stream and k not in self.wanted()),
                          key=lambda k: self.hf[k].latest[3] if self.hf[k].latest else 0)
            for k in idle[:max(0, len(self.hf) - 24)]:
                del self.hf[k]
            for key in wanted:
                f = self.feed_for(*key)
                if not f.stream:
                    lo, hi = key
                    await self._open(f, (lo + hi) / 2, BINS, (hi - lo) / BINS)
            if band == "vhf" and self.clients and not self.vhf.stream and self.radio.vhf.center_hz:
                bin_hz = LIVE_VHF_SPAN / BINS
                await self._open(self.vhf, self.radio.vhf.to_if(self.radio.vhf.center_hz), BINS, bin_hz)
            if band == "hf" or not self.clients:
                await self._close(self.vhf)

    async def _watch(self):
        """Follow the input, the tuner and the coverage; restart a feed that has gone quiet (radiod restart)."""
        while True:
            try:
                await self._sync()
                now = time.time()
                stalled = [f for f in [*list(self.hf.values()), self.vhf] if f.stream and now - f.got > 6
                           and not (f.kind == "vhf" and (self.radio.vhf_paused or self.spectrum.sweeping))]
                if stalled:
                    log.info("panels: %d feed(s) stalled, reopening", len(stalled))
                    await self._sync(force=True)
                if now - self._saved > SAVE_EVERY:
                    self._saved = now
                    await asyncio.to_thread(self.save, self.snapshot())
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("panels")
            await asyncio.sleep(0.5)

    def start(self):
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._watch())

    async def stop(self):
        if self._task:
            self._task.cancel()
        try:
            self.save()
        except Exception as e:
            log.warning("panels: could not save: %s", e)
        for f in [*list(self.hf.values()), self.vhf]:
            await self._close(f)

    # -- one browser ---------------------------------------------------------------------------------
    async def serve(self, ws):
        self.clients[ws] = set()
        ranges: List[Tuple[float, float]] = []
        sent: Dict[int, float] = {}

        async def reader():
            nonlocal ranges
            while True:
                msg = await ws.receive_json()
                if "ranges" in msg:
                    ranges = [(float(lo), float(hi)) for lo, hi in msg["ranges"] if 0 <= float(lo) < float(hi)]
                    self.clients[ws] = {r for r in ranges if r[1] <= CONFIG.max_freq_hz}
                    await self._sync()
                    await ws.send_json(self.describe(ranges))
                    for b in self.history(ranges):
                        await ws.send_bytes(b)

        task = asyncio.create_task(reader())
        info_every, last_info = 2.0, 0.0
        try:
            while not task.done():
                now = time.time()
                if now - last_info > info_every:          # the input / tuner can move under us
                    last_info = now
                    if ranges:
                        await ws.send_json(self.describe(ranges))
                for f in [self.hf.get(r) for r in ranges] + [self.vhf]:
                    if f and f.stream and f.latest and f.latest[3] > sent.get(f.id, 0):
                        sent[f.id] = f.latest[3]
                        await ws.send_bytes(frame_bytes(f.id, *f.latest))
                await asyncio.sleep(SEND_EVERY)
            task.result()
        finally:
            task.cancel()
            self.clients.pop(ws, None)
