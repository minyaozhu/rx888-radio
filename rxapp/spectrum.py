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

from .config import CONFIG, VHF_MAX_HZ, VHF_MIN_HZ
from .vhf import WINDOW_HZ

LIVE_VHF_SPAN = 2 * WINDOW_HZ     # widest VHF view the tuner shows at once; wider views are swept
SWEEP_SETTLE_FRAMES = 2           # spectrum frames to discard after each tuner step

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
        self.sweeping = False             # VHF view wider than the tuner: built by stepping the tuner
        self._sweep_task: Optional[asyncio.Task] = None
        self.sweep_sec = None             # time one full sweep took
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
        self._emit(Frame(self.center_hz, self.bin_hz, db, time.time()))

    def _emit(self, f: Frame):
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

    def _clamp(self, follow_tuner: bool = True):
        if self.vhf:
            # Same span choices as HF (up to the coverage); wider than the tuner window -> sweep
            self.bin_hz = min(self.bin_hz, CONFIG.max_freq_hz / self.bins)
            half = self.span_hz / 2
            if self.span_hz <= LIVE_VHF_SPAN + 1:
                if not follow_tuner:          # leaving a sweep: the tuner will move to this view
                    self.center_hz = min(max(self.center_hz, VHF_MIN_HZ + half), VHF_MAX_HZ - half)
                    return
                c0 = self.radio.vhf.center_hz
                self.center_hz = min(max(self.center_hz, c0 - WINDOW_HZ + half), c0 + WINDOW_HZ - half)
            else:
                self.center_hz = min(max(self.center_hz, VHF_MIN_HZ + half), VHF_MAX_HZ - half)
        else:
            nyq = CONFIG.max_freq_hz
            self.bin_hz = min(self.bin_hz, nyq / self.bins)
            half = self.span_hz / 2
            self.center_hz = min(max(self.center_hz, half), nyq - half)

    @property
    def wants_sweep(self):
        return self.vhf and self.span_hz > LIVE_VHF_SPAN + 1

    async def configure(self, center_hz: Optional[float] = None, bin_hz: Optional[float] = None):
        """Move or zoom the view (RF frequencies)."""
        old_bin = self.bin_hz
        if bin_hz is not None:
            self.bin_hz = float(bin_hz)
        if center_hz is not None:
            self.center_hz = float(center_hz)
        self._clamp(follow_tuner=not self.sweeping)
        self.latest = None
        if self.wants_sweep:
            await self._start_sweep()
            return
        if self.sweeping:
            await self._stop_sweep()
            await self.radio.resume_vhf(self.center_hz)   # tuner back; triggers retarget -> live stream
            return
        if self.bin_hz != old_bin:
            await self._restart()     # bin width needs a new spectrum channel
        else:
            self._radiod_center = self._radiod_center_for(self.center_hz)
            await asyncio.to_thread(self._stream.set_frequency, self._radiod_center)

    async def ensure_live(self, center_hz: float):
        """Leave a VHF sweep (e.g. to tune and listen): widest live view around center_hz."""
        if self.sweeping:
            await self.configure(center_hz, LIVE_VHF_SPAN / self.bins)

    async def retarget(self):
        """The tuner moved (or radiod restarted): keep the view on the tuner's window."""
        if self.sweeping:
            await self._start_sweep()          # radiod restarted under a sweep: rebuild it
            return
        if self.vhf:
            self.center_hz = self.radio.vhf.center_hz
        self._clamp()
        await self._restart()

    # -- VHF sweep ----------------------------------------------------------
    async def _start_sweep(self):
        if self._sweep_task:
            self._sweep_task.cancel()
            self._sweep_task = None
        if not self.radio.vhf_paused:
            await self.radio.pause_vhf()
        if self._stream:
            await asyncio.to_thread(self._stream.stop)
        self.sweeping = True
        win_bins = max(16, min(self.bins, int(LIVE_VHF_SPAN * 0.95 / self.bin_hz)))
        q: asyncio.Queue = asyncio.Queue()
        loop = self._loop

        def on_frame(st):
            db = st.spectrum.bin_power_db
            if db is not None and len(db):
                loop.call_soon_threadsafe(q.put_nowait, np.asarray(db, np.float32))

        self._stream = SpectrumStream(control=self.radio.ctl, frequency_hz=self.radio.vhf.if_hz,
                                      bin_count=win_bins, resolution_bw=self.bin_hz, on_spectrum=on_frame)
        await asyncio.to_thread(self._stream.start)
        self._radiod_center = None           # live-path frames don't apply
        self._sweep_task = asyncio.create_task(self._sweep_loop(q, win_bins))
        log.info("spectrum: VHF sweep %.1f-%.1f MHz, %d windows of %d x %.0f Hz",
                 (self.center_hz - self.span_hz / 2) / 1e6, (self.center_hz + self.span_hz / 2) / 1e6,
                 int(np.ceil(self.span_hz / (win_bins * self.bin_hz))), win_bins, self.bin_hz)

    async def _sweep_loop(self, q, win_bins):
        vhf = self.radio.vhf
        while True:
            t0 = time.time()
            center, bin_hz, n = self.center_hz, self.bin_hz, self.bins
            lo = center - n * bin_hz / 2
            full = np.full(n, np.nan, np.float32)
            width = win_bins * bin_hz
            for wc in np.arange(lo + width / 2, lo + n * bin_hz + width / 2, width):
                await vhf.retune(float(wc))
                while not q.empty():
                    q.get_nowait()
                db = None
                try:
                    for _ in range(SWEEP_SETTLE_FRAMES + 1):
                        db = await asyncio.wait_for(q.get(), 2.0)
                except asyncio.TimeoutError:
                    continue
                f_if = vhf.if_hz + (np.arange(len(db)) - len(db) // 2) * bin_hz
                idx = np.round((vhf.to_rf(f_if) - lo) / bin_hz).astype(int)
                ok = (idx >= 0) & (idx < n)
                full[idx[ok]] = db[ok]
            if np.isnan(full).all():
                continue
            full[np.isnan(full)] = np.nanmin(full)
            self.sweep_sec = time.time() - t0
            if (center, bin_hz) == (self.center_hz, self.bin_hz):
                self._emit(Frame(center, bin_hz, full, time.time()))

    async def _stop_sweep(self):
        if self._sweep_task:
            self._sweep_task.cancel()
            self._sweep_task = None
        if self._stream:
            await asyncio.to_thread(self._stream.stop)
            self._stream = None
        self.sweeping = False

    async def band_changed(self, old_band: str):
        """Called after radio.switch_band: remember the old band's view, restore the new one's."""
        if old_band == "hf":
            self._hf_view = (self.center_hz, self.bin_hz)
        else:
            self._vhf_view = (self.center_hz, self.bin_hz)
        if self.sweeping:
            await self._stop_sweep()            # switch_band already released the tuner
        if self.vhf:
            self.center_hz, self.bin_hz = self._vhf_view or (self.radio.vhf.center_hz, LIVE_VHF_SPAN / self.bins)
        else:
            self.center_hz, self.bin_hz = self._hf_view
        self._clamp()
        if self.wants_sweep:
            await self._start_sweep()
        else:
            await self._restart()

    async def stop(self):
        if self._sweep_task:
            self._sweep_task.cancel()
        if self._stream:
            await asyncio.to_thread(self._stream.stop)
            self._stream = None

    def info(self):
        return {"center_hz": self.center_hz, "bin_hz": self.bin_hz, "bins": self.bins,
                "span_hz": self.span_hz, "frames": self.frames, "band": self.radio.band,
                "max_span_hz": CONFIG.max_freq_hz, "live_vhf_span_hz": LIVE_VHF_SPAN,
                "sweeping": self.sweeping, "sweep_sec": self.sweep_sec}
