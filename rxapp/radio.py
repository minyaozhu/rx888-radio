"""Channel control on top of ka9q-python's RadiodControl.

rxapp owns the channels it creates. Each one is created with a finite lifetime and
refreshed by a keepalive, so if rxapp dies radiod drops them on its own instead of
demodulating forever.
"""

import asyncio
import logging
import math
import secrets
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, Optional

from ka9q import RadiodControl

from .addr import maddr
from .config import CONFIG, MODES

log = logging.getLogger(__name__)

LIFETIME_FRAMES = 1500        # radiod frames are 20 ms: 30 s without a refresh
KEEPALIVE_SEC = 10


@dataclass
class Channel:
    ssrc: int
    freq_hz: float
    mode: str
    label: str = ""
    kind: str = "listen"            # listen | record | scan
    sample_rate: int = 12000
    created: float = field(default_factory=time.time)
    # Latest status from radiod
    power_db: Optional[float] = None
    noise_db: Optional[float] = None
    snr_db: Optional[float] = None
    gain_db: Optional[float] = None
    updated: Optional[float] = None

    def to_dict(self):
        return asdict(self)


def estimate_snr(status: dict) -> Optional[float]:
    """Channel SNR from radiod status: baseband power vs noise density over the filter width."""
    p, n0 = status.get("baseband_power"), status.get("noise_density")
    lo, hi = status.get("low_edge"), status.get("high_edge")
    if None in (p, n0, lo, hi) or hi <= lo:
        return None
    noise = n0 + 10 * math.log10(hi - lo)
    return 10 * math.log10(max(10 ** ((p - noise) / 10) - 1, 1e-3))  # signal over noise, not (S+N)/N


class Radio:
    def __init__(self):
        self.status_addr = maddr(CONFIG.status_name)
        self.pcm_group = maddr(CONFIG.pcm_name)
        self.ctl: Optional[RadiodControl] = None
        self.channels: Dict[int, Channel] = {}
        self._keepalive: Optional[asyncio.Task] = None
        self.last_status_ok: Optional[float] = None

    # -- lifecycle -------------------------------------------------------
    async def connect(self):
        self.ctl = await asyncio.to_thread(
            RadiodControl, self.status_addr, client_id="rxapp", interface=CONFIG.iface_addr)
        self._keepalive = asyncio.create_task(self._keepalive_loop())
        log.info("radiod control at %s, audio group %s", self.status_addr, self.pcm_group)

    async def close(self):
        if self._keepalive:
            self._keepalive.cancel()
        for ssrc in list(self.channels):
            await self.remove(ssrc)
        if self.ctl:
            await asyncio.to_thread(self.ctl.close)

    # -- channels --------------------------------------------------------
    def _check(self, freq_hz: float, mode: str):
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}; choose from {', '.join(MODES)}")
        if not 0 < freq_hz < CONFIG.max_freq_hz:
            raise ValueError(f"{freq_hz / 1e6:.4f} MHz is outside 0-{CONFIG.max_freq_hz / 1e6:.1f} MHz (HF input)")

    async def create(self, freq_hz: float, mode: str = "am", label: str = "", kind: str = "listen") -> Channel:
        self._check(freq_hz, mode)
        preset, rate = MODES[mode]
        ssrc = secrets.randbelow(0x7FFFFFFF - 1000) + 1000  # stay clear of radiod's static channels
        await asyncio.to_thread(
            self.ctl.create_channel, frequency_hz=freq_hz, preset=preset, sample_rate=rate,
            agc_enable=1, destination=self.pcm_group, ssrc=ssrc, lifetime=LIFETIME_FRAMES)
        # create_channel's agc flag doesn't take on this radiod; without AGC the audio sits near -55 dBFS
        await asyncio.to_thread(self.ctl.set_agc, ssrc, True)
        ch = Channel(ssrc=ssrc, freq_hz=freq_hz, mode=mode, label=label, kind=kind, sample_rate=rate)
        self.channels[ssrc] = ch
        log.info("created %s channel %d at %.3f kHz (%s)", kind, ssrc, freq_hz / 1e3, mode)
        return ch

    async def tune(self, ssrc: int, freq_hz: Optional[float] = None, mode: Optional[str] = None,
                   label: Optional[str] = None) -> Channel:
        ch = self.channels[ssrc]
        freq_hz = ch.freq_hz if freq_hz is None else freq_hz
        mode = ch.mode if mode is None else mode
        self._check(freq_hz, mode)
        if freq_hz != ch.freq_hz:
            await asyncio.to_thread(self.ctl.set_frequency, ssrc, freq_hz)
        if mode != ch.mode:
            preset, rate = MODES[mode]
            await asyncio.to_thread(self.ctl.set_preset, ssrc, preset)
            if rate != ch.sample_rate:
                await asyncio.to_thread(self.ctl.set_sample_rate, ssrc, rate)
            await asyncio.to_thread(self.ctl.set_agc, ssrc, True)
            ch.sample_rate = rate
        ch.freq_hz, ch.mode = freq_hz, mode
        if label is not None:
            ch.label = label
        return ch

    async def restore(self):
        """Re-create our channels (same SSRCs) after radiod restarted and forgot them."""
        for ch in list(self.channels.values()):
            preset, rate = MODES[ch.mode]
            await asyncio.to_thread(
                self.ctl.create_channel, frequency_hz=ch.freq_hz, preset=preset, sample_rate=rate,
                agc_enable=1, destination=self.pcm_group, ssrc=ch.ssrc, lifetime=LIFETIME_FRAMES)
            await asyncio.to_thread(self.ctl.set_agc, ch.ssrc, True)
        log.info("restored %d channels", len(self.channels))

    async def remove(self, ssrc: int):
        self.channels.pop(ssrc, None)
        try:
            await asyncio.to_thread(self.ctl.remove_channel, ssrc)
        except Exception as e:  # radiod may already have dropped it
            log.warning("remove %d: %s", ssrc, e)

    # -- status ----------------------------------------------------------
    async def status(self, ssrc: int, timeout: float = 1.0) -> Optional[dict]:
        try:
            st = await asyncio.to_thread(self.ctl.poll_status, ssrc, timeout)
        except Exception:
            return None
        self.last_status_ok = time.time()
        return st.to_dict()

    async def refresh(self, ssrc: int) -> Optional[Channel]:
        ch = self.channels.get(ssrc)
        d = await self.status(ssrc)
        if ch is None or d is None:
            return ch
        ch.power_db, ch.noise_db = d.get("baseband_power"), d.get("noise_density")
        ch.gain_db, ch.snr_db, ch.updated = d.get("gain"), estimate_snr(d), time.time()
        return ch

    async def _keepalive_loop(self):
        while True:
            await asyncio.sleep(KEEPALIVE_SEC)
            for ssrc in list(self.channels):
                try:
                    await asyncio.to_thread(self.ctl.set_channel_lifetime, ssrc, LIFETIME_FRAMES)
                except Exception as e:
                    log.warning("keepalive %d: %s", ssrc, e)
