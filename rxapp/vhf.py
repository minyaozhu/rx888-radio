"""The RX-888's VHF/UHF path (R828D tuner) driven alongside radiod.

radiod keeps sampling the ADC as on HF; this module flips the antenna switch to the
tuner and programs it over USB control transfers (via ka9q-radio's rx888_vhf_demo
driver). The tuner mixes the wanted signal down to an IF around 4.57 MHz with the
local oscillator above the signal (LO = RF + IF), so the spectrum arrives inverted:

    f_if = IF - (f_rf - center)        center = RF frequency at the IF centre

Channels are radiod channels at f_if; sidebands swap (USB <-> LSB).
"""

import asyncio
import logging
import sys
import threading
from typing import Optional

from .config import CONFIG, RUNTIME, VHF_MAX_HZ, VHF_MIN_HZ

sys.path.insert(0, str(CONFIG.radiod_script.parent.parent / "tools" / "ka9q-radio" / "rx888_vhf_demo"))
import rx888_vhf  # noqa: E402

log = logging.getLogger(__name__)

HF_GPIO = 0            # radiod's GPIO word on HF with dither and randomizer off
WINDOW_HZ = 3.6e6      # usable half-width around the IF with the 8 MHz channel filter
SWAP = {"usb": "lsb", "lsb": "usb", "cwu": "cwl", "cwl": "cwu"}


class Vhf:
    def __init__(self):
        self.active = False
        self.center_hz: Optional[float] = None
        self.if_hz = rx888_vhf.IF_CARRIER
        self.locked = None
        self._rx = None
        self._lock = threading.Lock()

    # -- frequency mapping ---------------------------------------------------
    def to_if(self, rf_hz: float) -> float:
        return self.if_hz - (rf_hz - self.center_hz)

    def to_rf(self, if_hz: float) -> float:
        return self.center_hz + (self.if_hz - if_hz)

    @staticmethod
    def radiod_mode(mode: str) -> str:
        return SWAP.get(mode, mode)

    def in_window(self, rf_hz: float) -> bool:
        return self.center_hz is not None and abs(rf_hz - self.center_hz) <= WINDOW_HZ

    # -- hardware (blocking; run in a thread) ------------------------------------
    def _enable(self, center_hz):
        with self._lock:
            if self._rx is None:
                self._rx = rx888_vhf.RX888()
            rx = self._rx
            rx.check_alive()
            rx.enter_vhf(HF_GPIO, False)
            rx.clkb_on(rx888_vhf.R828D_REF_HZ)
            rx.r828d_init()
            rx.set_lna_agc(True)
            rx.set_mixer_agc(True)
            rx.set_vga_gain(RUNTIME.vga)
            self.if_hz = rx.if_hz
            self.locked = rx.r828d_set_freq(int(center_hz))
            self.center_hz = float(center_hz)
            self.active = True
        log.info("VHF on: tuner at %.3f MHz, IF %.3f MHz, PLL %s", center_hz / 1e6, self.if_hz / 1e6,
                 "locked" if self.locked else "NOT locked")

    def _retune(self, center_hz):
        with self._lock:
            self.locked = self._rx.r828d_set_freq(int(center_hz))
            self.center_hz = float(center_hz)
        log.info("VHF tuner -> %.3f MHz (PLL %s)", center_hz / 1e6, "locked" if self.locked else "NOT locked")

    def _disable(self):
        with self._lock:
            if self._rx is not None:
                self._rx.standby(HF_GPIO)
            self.active = False
        log.info("VHF off, back to HF")

    def _set_vga(self, vga):
        with self._lock:
            if self._rx is not None and self.active:
                self._rx.set_vga_gain(vga)

    async def enable(self, center_hz):
        if not VHF_MIN_HZ <= center_hz <= VHF_MAX_HZ:
            raise ValueError(f"VHF tuner range is {VHF_MIN_HZ / 1e6:.0f}-{VHF_MAX_HZ / 1e6:.0f} MHz")
        await asyncio.to_thread(self._enable, center_hz)

    async def retune(self, center_hz):
        if not VHF_MIN_HZ <= center_hz <= VHF_MAX_HZ:
            raise ValueError(f"VHF tuner range is {VHF_MIN_HZ / 1e6:.0f}-{VHF_MAX_HZ / 1e6:.0f} MHz")
        await asyncio.to_thread(self._retune, center_hz)

    async def disable(self):
        await asyncio.to_thread(self._disable)

    async def set_vga(self, vga):
        await asyncio.to_thread(self._set_vga, vga)

    def info(self):
        return {"active": self.active, "center_hz": self.center_hz, "if_hz": self.if_hz,
                "locked": self.locked, "window_hz": WINDOW_HZ, "vga": RUNTIME.vga}
