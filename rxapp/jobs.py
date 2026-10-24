# SPDX-License-Identifier: GPL-3.0-or-later
"""Band scans (from the spectrum) and channel recordings (from RTP)."""

import asyncio
import logging
import math
import time
import wave
from dataclasses import dataclass
from typing import Dict, Optional

import numpy as np
from ka9q import SpectrumStream

from .config import CONFIG
from .vhf import WINDOW_HZ

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Band:
    name: str
    start_hz: float
    stop_hz: float
    step_hz: float          # channel grid; 0 = find peaks anywhere
    mode: str
    first_hz: Optional[float] = None   # first grid channel, if not start_hz
    band: str = "hf"                   # which input: hf | vhf


BANDS: Dict[str, Band] = {b.name: b for b in [
    Band("am", 530e3, 1700e3, 10e3, "am"),
    Band("120m", 2.3e6, 2.495e6, 5e3, "am"),
    Band("90m", 3.2e6, 3.4e6, 5e3, "am"),
    Band("75m", 3.9e6, 4.0e6, 5e3, "am"),
    Band("60m", 4.75e6, 5.06e6, 5e3, "am"),
    Band("49m", 5.9e6, 6.2e6, 5e3, "am"),
    Band("41m", 7.2e6, 7.45e6, 5e3, "am"),
    Band("31m", 9.4e6, 9.9e6, 5e3, "am"),
    Band("25m", 11.6e6, 12.1e6, 5e3, "am"),
    Band("22m", 13.57e6, 13.87e6, 5e3, "am"),
    Band("19m", 15.1e6, 15.8e6, 5e3, "am"),
    Band("16m", 17.48e6, 17.9e6, 5e3, "am"),
    Band("13m", 21.45e6, 21.85e6, 5e3, "am"),
    Band("wwv", 2.5e6, 25e6, 0, "am"),       # handled specially: the WWV frequencies
    Band("160m", 1.8e6, 2.0e6, 0, "lsb"),
    Band("80m", 3.6e6, 4.0e6, 0, "lsb"),
    Band("40m", 7.125e6, 7.3e6, 0, "lsb"),
    Band("20m", 14.15e6, 14.35e6, 0, "usb"),
    Band("17m", 18.11e6, 18.168e6, 0, "usb"),
    Band("15m", 21.2e6, 21.45e6, 0, "usb"),
    Band("12m", 24.93e6, 24.99e6, 0, "usb"),
    Band("10m", 28.3e6, 28.85e6, 0, "usb"),
    Band("cb", 26.965e6, 27.405e6, 10e3, "am"),
    Band("fm", 88.1e6, 107.9e6, 200e3, "wfm", band="vhf"),
    Band("air", 118.0e6, 136.975e6, 25e3, "am", band="vhf"),
    Band("2m", 144.0e6, 148.0e6, 0, "nfm", band="vhf"),
    Band("wx", 162.4e6, 162.55e6, 25e3, "nfm", band="vhf"),
]}

WWV_HZ = [2.5e6, 5e6, 10e6, 15e6, 20e6, 25e6]
NICE_BINS = [10, 25, 50, 100, 250, 500, 1000, 2000, 2500, 5000, 10000, 15000]


class Scanner:
    """Band scans on a private spectrum channel, so the display's view never moves."""

    def __init__(self, spectrum, store):
        self.spectrum = spectrum      # the display's Spectrum (for its radio and bin count)
        self.store = store
        self.lock = asyncio.Lock()    # one scan at a time
        self.bins = spectrum.bins

    async def _capture(self, center_hz: float, bin_hz: float, frames: int, timeout: float = 15.0,
                       clamp: bool = True):
        """Average `frames` spectrum frames (linear power) from a temporary spectrum channel."""
        half = self.bins * bin_hz / 2
        if clamp:
            center_hz = min(max(center_hz, half), CONFIG.max_freq_hz - half) if 2 * half < CONFIG.max_freq_hz \
                else CONFIG.max_freq_hz / 2
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()

        def on_frame(st):
            db = st.spectrum.bin_power_db
            if db is not None and len(db):
                loop.call_soon_threadsafe(q.put_nowait, np.asarray(db, np.float64))

        stream = SpectrumStream(control=self.spectrum.radio.ctl, frequency_hz=center_hz, bin_count=self.bins,
                                resolution_bw=bin_hz, on_spectrum=on_frame)
        await asyncio.to_thread(stream.start)
        acc, n, t0 = None, 0, time.time()
        try:
            for _ in range(2):        # the first frames can predate the full configuration
                await asyncio.wait_for(q.get(), timeout)
            while n < frames and time.time() - t0 < timeout:
                p = 10 ** (await asyncio.wait_for(q.get(), timeout) / 10)
                acc = p if acc is None else acc + p
                n += 1
        finally:
            await asyncio.to_thread(stream.stop)
        if n == 0:
            raise TimeoutError("no spectrum frames")
        freqs = center_hz + (np.arange(len(acc)) - len(acc) // 2) * bin_hz
        return freqs, 10 * np.log10(acc / n), n

    async def scan(self, name: str, frames: int = 20, min_snr: float = 8.0):
        band = BANDS[name]
        radio = self.spectrum.radio
        if band.band != radio.band:
            raise ValueError(f"{name} is a {band.band.upper()} band; switch the input to {band.band.upper()} first")
        async with self.lock:
            if name == "wwv":
                return await self._scan_wwv(frames)
            width = band.stop_hz - band.start_hz
            if band.band == "vhf":
                if self.spectrum.sweeping:          # the scan needs the tuner
                    await self.spectrum.ensure_live(radio.vhf.center_hz)
                bin_hz = 2500.0 if band.step_hz >= 100e3 else 500.0
                freqs, db, n = await self._capture_vhf(band.start_hz, band.stop_hz, bin_hz, frames)
            else:
                bin_hz = next(b for b in NICE_BINS if b * self.bins >= width * 1.1)
                if band.step_hz:
                    bin_hz = min(bin_hz, max(NICE_BINS[0], band.step_hz / 4))
                    while bin_hz * self.bins < width * 1.1:
                        bin_hz *= 2
                freqs, db, n = await self._capture((band.start_hz + band.stop_hz) / 2, bin_hz, frames)
            inband = (freqs >= band.start_hz) & (freqs <= band.stop_hz)
            floor = float(np.percentile(db[inband], 30))
            hits = self._grid_hits(band, freqs, db, floor, min_snr) if band.step_hz \
                else self._peak_hits(freqs, db, inband, floor, min_snr, bin_hz)
            sid = self.store.add_scan(name, band.start_hz, band.stop_hz, band.step_hz, bin_hz, n, floor, hits)
            self.store.event("scan", band=name, stations=len(hits))
            return {"scan_id": sid, "band": name, "mode": band.mode, "floor_db": floor, "bin_hz": bin_hz,
                    "frames": n, "hits": [{"freq_hz": f, "snr_db": round(s, 1)} for f, s in hits]}

    async def _capture_vhf(self, start_hz, stop_hz, bin_hz, frames):
        """Step the tuner across [start, stop] and stitch the (un-inverted) IF spectra.

        This moves the tuner for every VHF channel, so listening is interrupted for the
        few seconds a scan takes; the tuner goes back where it was afterwards.
        """
        radio = self.spectrum.radio
        home = radio.vhf.center_hz
        span = min(self.bins * bin_hz, 2 * WINDOW_HZ)
        use = span * 0.9                               # skip the filter skirts at the edges
        centers = list(np.arange(start_hz + use / 2, stop_hz + use / 2, use))
        all_f, all_db, n_min = [], [], frames
        try:
            for c in centers:
                await radio.move_tuner(c)
                await asyncio.sleep(0.2)               # PLL settle
                f_if, db, n = await self._capture(radio.vhf.if_hz, bin_hz, frames, clamp=False)
                rf = radio.vhf.to_rf(f_if)
                keep = np.abs(rf - c) <= use / 2
                order = np.argsort(rf[keep])
                all_f.append(rf[keep][order])
                all_db.append(db[keep][order])
                n_min = min(n_min, n)
        finally:
            await radio.move_tuner(home)
        return np.concatenate(all_f), np.concatenate(all_db), n_min

    @staticmethod
    def _grid_hits(band, freqs, db, floor, min_snr):
        hits = []
        first = band.first_hz or band.start_hz
        n = int(round((band.stop_hz - first) / band.step_hz))
        tol = max(band.step_hz * 0.15, (freqs[1] - freqs[0]) * 1.5)
        for k in range(n + 1):
            f0 = first + k * band.step_hz
            m = np.abs(freqs - f0) <= tol
            if m.any():
                snr = float(db[m].max() - floor)
                if snr >= min_snr:
                    hits.append((f0, snr))
        return sorted(hits, key=lambda h: -h[1])

    @staticmethod
    def _peak_hits(freqs, db, inband, floor, min_snr, bin_hz):
        """Local maxima at least 3 kHz apart (SSB signals off any grid)."""
        k = max(1, int(3000 / bin_hz))
        idx = np.where(inband)[0]
        hits = []
        for i in idx:
            lo, hi = max(0, i - k), min(len(db), i + k + 1)
            if db[i] == db[lo:hi].max() and db[i] - floor >= min_snr:
                hits.append((float(round(freqs[i] / 100) * 100), float(db[i] - floor)))
        return sorted(hits, key=lambda h: -h[1])[:60]

    async def _scan_wwv(self, frames):
        hits = []
        floors = []
        for f0 in WWV_HZ:
            if f0 >= CONFIG.max_freq_hz:
                continue
            freqs, db, n = await self._capture(f0, 50, frames)
            floor = float(np.percentile(db, 30))
            snr = float(db[np.abs(freqs - f0) <= 150].max() - floor)
            floors.append(floor)
            hits.append((f0, snr))
        sid = self.store.add_scan("wwv", WWV_HZ[0], WWV_HZ[-1], 0, 50, frames, float(np.mean(floors)), hits)
        return {"scan_id": sid, "band": "wwv", "mode": "am", "bin_hz": 50, "frames": frames,
                "hits": [{"freq_hz": f, "snr_db": round(s, 1)} for f, s in hits]}


class Recorder:
    """Writes a channel's audio to a WAV file while subscribed to its RTP stream."""

    def __init__(self, rtp, store, ch):
        self.rtp, self.store, self.ch = rtp, store, ch
        CONFIG.recordings_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.path = CONFIG.recordings_dir / f"{stamp}_{ch.freq_hz / 1e3:.0f}kHz_{ch.mode}.wav"
        self.w = wave.open(str(self.path), "wb")
        self.w.setnchannels(2 if ch.mode == "wfm" and getattr(ch, "stereo", False) else 1)
        self.w.setsampwidth(2)
        self.w.setframerate(ch.sample_rate)
        self.samples = 0
        self.peak = 0
        self.start = time.time()
        self.rid = store.add_recording(freq_hz=ch.freq_hz, mode=ch.mode, label=ch.label,
                                       path=str(self.path), start=self.start)
        rtp.subscribe(ch.ssrc, self._on_pcm)

    def _on_pcm(self, pcm: bytes, ts, seq):
        self.w.writeframes(pcm)
        a = np.frombuffer(pcm, "<i2")
        self.samples += len(a)
        if len(a):
            self.peak = max(self.peak, int(np.abs(a.astype(np.int32)).max()))

    def stop(self):
        self.rtp.unsubscribe(self.ch.ssrc, self._on_pcm)
        self.w.close()
        secs = self.samples / self.ch.sample_rate
        peak = 20 * math.log10(max(self.peak, 1) / 32768)
        self.store.finish_recording(self.rid, time.time(), secs, peak)
        return {"id": self.rid, "path": str(self.path), "seconds": round(secs, 1), "peak_dbfs": round(peak, 1)}
