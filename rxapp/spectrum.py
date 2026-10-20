"""Live spectrum from radiod's spectrum channel, fanned out to asyncio subscribers.

ka9q-python's SpectrumStream receives the FFT bins (inside status packets) on its own
thread; frames are handed to the event loop and kept as `latest` for the scanner.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

import numpy as np
from ka9q import SpectrumStream

from .config import CONFIG

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
        self.frames = 0

    @property
    def span_hz(self):
        return self.bins * self.bin_hz

    def _on_frame(self, st):
        db = st.spectrum.bin_power_db
        if db is None or len(db) == 0:
            return
        f = Frame(self.center_hz, self.bin_hz, np.asarray(db, np.float32), time.time())
        self._loop.call_soon_threadsafe(self._publish, f)

    def _publish(self, f: Frame):
        if f.center_hz != self.center_hz or f.bin_hz != self.bin_hz:
            return  # left over from before a retune
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
        self._stream = SpectrumStream(
            control=self.radio.ctl, frequency_hz=self.center_hz, bin_count=self.bins,
            resolution_bw=self.bin_hz, on_spectrum=self._on_frame)
        await asyncio.to_thread(self._stream.start)
        log.info("spectrum: %.3f MHz center, %d x %.0f Hz", self.center_hz / 1e6, self.bins, self.bin_hz)

    async def configure(self, center_hz: Optional[float] = None, bin_hz: Optional[float] = None):
        """Move or zoom the view. The span is kept inside 0..Nyquist."""
        if bin_hz is not None and bin_hz != self.bin_hz:
            self.bin_hz = float(bin_hz)
        if center_hz is not None:
            self.center_hz = float(center_hz)
        half = self.span_hz / 2
        if 2 * half >= CONFIG.max_freq_hz:
            self.center_hz = CONFIG.max_freq_hz / 2   # view wider than the band: show it all
        else:
            self.center_hz = min(max(self.center_hz, half), CONFIG.max_freq_hz - half)
        if bin_hz is not None:
            await self._restart()     # bin width needs a new spectrum channel
        else:
            await asyncio.to_thread(self._stream.set_frequency, self.center_hz)
        self.latest = None

    async def stop(self):
        if self._stream:
            await asyncio.to_thread(self._stream.stop)
            self._stream = None

    def info(self):
        return {"center_hz": self.center_hz, "bin_hz": self.bin_hz, "bins": self.bins,
                "span_hz": self.span_hz, "frames": self.frames}
