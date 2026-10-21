"""HF front-end gain: manual (AD8370 VGA + PE4312 attenuator) or automatic.

radiod's own RF AGC switches off for good once a manual gain or attenuation command
arrives (there's no command to turn it back on), so "Auto" after a manual setting is
rxapp's own loop doing the same job: hold the ADC between -26 and -15 dBFS.

The same amplifier is in the VHF tuner's IF path, so on VHF the gain is always
automatic; a manual HF setting is re-applied when the input goes back to HF.
"""

import asyncio
import logging
from typing import Optional

from .config import RUNTIME

log = logging.getLogger(__name__)

GAIN_MIN, GAIN_MAX = -10.0, 34.0     # AD8370 range rxapp uses (dB)
ATTEN_MAX = 31.5                     # PE4312 attenuator (dB, 0.5 dB steps)
SLIDER_MIN = GAIN_MIN - ATTEN_MAX    # one slider: below -10 dB the attenuator takes over
AGC_LOW, AGC_HIGH = -26.0, -15.0     # radiod's own thresholds (rx888.c)
AGC_EVERY_SEC = 1.0


def split(total_db: float):
    """One 'gain' number -> (VGA gain, attenuation)."""
    total_db = max(SLIDER_MIN, min(GAIN_MAX, total_db))
    if total_db >= GAIN_MIN:
        return total_db, 0.0
    return GAIN_MIN, min(ATTEN_MAX, round((GAIN_MIN - total_db) * 2) / 2)


class HfGain:
    def __init__(self, radio, probe, ssrc_fn):
        self.radio = radio
        self.probe = probe           # async () -> radiod status dict (with "frontend") or None
        self.ssrc_fn = ssrc_fn       # () -> any SSRC radiod knows (front-end commands ride on a channel)
        self.radiod_agc = True       # radiod's own AGC is on until we send a manual value
        self._task = None
        self.last = {}

    @property
    def manual_db(self) -> Optional[float]:
        return RUNTIME.hf_gain_db      # None = auto

    async def _send(self, gain, atten):
        ssrc = self.ssrc_fn()
        if ssrc is None:
            return
        await asyncio.to_thread(self.radio.ctl.set_rf_gain, ssrc, gain)
        await asyncio.to_thread(self.radio.ctl.set_rf_attenuation, ssrc, atten)
        self.radiod_agc = False
        self.last = {"gain_db": gain, "atten_db": atten}

    async def set(self, total_db: Optional[float]):
        """Manual total gain in dB, or None for automatic."""
        RUNTIME.hf_gain_db = None if total_db is None else float(total_db)
        RUNTIME.save()
        if total_db is not None and self.radio.band == "hf":
            await self._send(*split(total_db))
        log.info("HF gain: %s", "auto" if total_db is None else f"{total_db:.1f} dB")

    async def apply(self, restarted: bool = False):
        """Re-apply after radiod restarted (its own AGC is on again then) or the input changed."""
        if restarted:
            self.radiod_agc = True
        if self.manual_db is not None and self.radio.band == "hf":
            await self._send(*split(self.manual_db))

    def start(self):
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        if self._task:
            self._task.cancel()

    async def _loop(self):
        while True:
            await asyncio.sleep(AGC_EVERY_SEC)
            try:
                auto = self.manual_db is None or self.radio.band == "vhf"
                if not auto:
                    continue
                st = await self.probe()
                fe = (st or {}).get("frontend") or {}
                self.radiod_agc = bool(fe.get("rf_agc"))
                if self.radiod_agc:
                    continue             # radiod's own AGC is still on and doing the job
                level, gain, atten = fe.get("if_power"), fe.get("rf_gain"), fe.get("rf_atten") or 0.0
                if level is None or gain is None or AGC_LOW <= level <= AGC_HIGH:
                    continue
                target = (AGC_LOW + AGC_HIGH) / 2
                total = (gain - atten) - (level - target)
                await self._send(*split(total))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("HF AGC: %s", e)
