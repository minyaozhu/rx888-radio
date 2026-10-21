"""Live spectrum from radiod's spectrum channel, fanned out to asyncio subscribers.

ka9q-python's SpectrumStream receives the FFT bins (inside status packets) on its own
thread; frames are handed to the event loop and kept as `latest`.

The view is always in real (RF) frequencies. On VHF radiod's spectrum is of the tuner's
IF, which is inverted, so the bins are flipped and relabelled. The span is kept within
what the input can deliver: Nyquist on HF (radiod returns half-empty frames beyond it),
the tuner's IF window on VHF.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

import numpy as np
from ka9q import SpectrumStream

from .config import CONFIG
from .vhf import WINDOW_HZ

log = logging.getLogger(__name__)


@dataclass
class Frame:
    center_hz: float
    bin_hz: float
    db: np.ndarray          # power per bin, dB, lowest frequency first
    t: float

    @property
    def freqs(self) -> np.ndarray:
        n = len(self.db)
        return self.center_hz + (np.arange(n) - n // 2) * self.bin_hz

    def to_bytes(self) -> bytes:
        """Wire format for the browser: center f64, bin_hz f32, n u32, then n x f32 dB."""
        hdr = np.array([self.center_hz], "<f8").tobytes() + np.array([self.bin_hz], "<f4").tobytes() \
            + np.array([len(self.db)], "<u4").tobytes()
        return hdr + self.db.astype("<f4").tobytes()


class Spectrum:
    def __init__(self, radio):
        self.radio = radio
        self.center_hz = CONFIG.spectrum_center_hz
        self.bin_hz = CONFIG.spectrum_bin_hz
        self.bins = CONFIG.spectrum_bins
        self.latest: Optional[Frame] = None
        self.subs: List[Callable[[Frame], None]] = []
        self._stream: Optional[SpectrumStream] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._radiod_center = None
        self.frames = 0
        self._hf_view = (self.center_hz, self.bin_hz)      # remembered per band
        self._vhf_view = None
        radio.on_tuner_move.append(self.retarget)

    @property
    def span_hz(self):
        return self.bins * self.bin_hz

    @property
    def vhf(self):
        return self.radio.band == "vhf"

    def _radiod_center_for(self, center_hz):
        return self.radio.vhf.to_if(center_hz) if self.vhf else center_hz

    def _on_frame(self, st):
        db = st.spectrum.bin_power_db
        if db is None or len(db) == 0:
            return
        db = np.asarray(db, np.float32)
        self._loop.call_soon_threadsafe(self._publish, st.frequency, db)

    def _publish(self, radiod_center, db):
        # drop frames from before the last retune (radiod reports the centre it used)
        if self._radiod_center is None or abs(radiod_center - self._radiod_center) > self.bin_hz:
            return
        if self.vhf:
            db = db[::-1]
        f = Frame(self.center_hz, self.bin_hz, db, time.time())
        self.latest = f
        self.frames += 1
        for cb in list(self.subs):
            try:
                cb(f)
            except Exception:
                log.exception("spectrum subscriber failed")

    async def start(self):
        self._loop = asyncio.get_running_loop()
        await self._restart()

    async def _restart(self):
        if self._stream:
            await asyncio.to_thread(self._stream.stop)
        self._radiod_center = self._radiod_center_for(self.center_hz)
        self._stream = SpectrumStream(
            control=self.radio.ctl, frequency_hz=self._radiod_center, bin_count=self.bins,
            resolution_bw=self.bin_hz, on_spectrum=self._on_frame)
        await asyncio.to_thread(self._stream.start)
        log.info("spectrum: %.3f MHz center, %d x %.0f Hz (%s)", self.center_hz / 1e6, self.bins,
                 self.bin_hz, self.radio.band)

    def _clamp(self):
        if self.vhf:
            max_span = 2 * WINDOW_HZ
            self.bin_hz = min(self.bin_hz, max_span / self.bins)
            half = self.span_hz / 2
            c0 = self.radio.vhf.center_hz
            self.center_hz = min(max(self.center_hz, c0 - WINDOW_HZ + half), c0 + WINDOW_HZ - half)
        else:
            nyq = CONFIG.max_freq_hz
            self.bin_hz = min(self.bin_hz, nyq / self.bins)
            half = self.span_hz / 2
            self.center_hz = min(max(self.center_hz, half), nyq - half)

    async def configure(self, center_hz: Optional[float] = None, bin_hz: Optional[float] = None):
        """Move or zoom the view (RF frequencies)."""
        old_bin = self.bin_hz
        if bin_hz is not None:
            self.bin_hz = float(bin_hz)
        if center_hz is not None:
            self.center_hz = float(center_hz)
        self._clamp()
        if self.bin_hz != old_bin:
            await self._restart()     # bin width needs a new spectrum channel
        else:
            self._radiod_center = self._radiod_center_for(self.center_hz)
            await asyncio.to_thread(self._stream.set_frequency, self._radiod_center)
        self.latest = None

    async def retarget(self):
        """The tuner moved: keep the view on the tuner's window."""
        if self.vhf:
            self.center_hz = self.radio.vhf.center_hz
        self._clamp()
        await self._restart()

    async def band_changed(self, old_band: str):
        """Called after radio.switch_band: remember the old band's view, restore the new one's."""
        if old_band == "hf":
            self._hf_view = (self.center_hz, self.bin_hz)
        else:
            self._vhf_view = (self.center_hz, self.bin_hz)
        if self.vhf:
            self.center_hz, self.bin_hz = self._vhf_view or (self.radio.vhf.center_hz, 2 * WINDOW_HZ / self.bins)
        else:
            self.center_hz, self.bin_hz = self._hf_view
        self._clamp()
        await self._restart()

    async def stop(self):
        if self._stream:
            await asyncio.to_thread(self._stream.stop)
            self._stream = None

    def info(self):
        return {"center_hz": self.center_hz, "bin_hz": self.bin_hz, "bins": self.bins,
                "span_hz": self.span_hz, "frames": self.frames, "band": self.radio.band,
                "max_span_hz": 2 * WINDOW_HZ if self.vhf else CONFIG.max_freq_hz}
