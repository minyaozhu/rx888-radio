# SPDX-License-Identifier: GPL-3.0-or-later
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
from .config import BAND_MODES, CONFIG, MODES, RUNTIME, VHF_MAX_HZ, VHF_MIN_HZ
from .vhf import WINDOW_HZ, Vhf

SLIDE_MARGIN_HZ = 0.4e6     # tuning past the window's edge: keep the channel this far inside it

log = logging.getLogger(__name__)

LIFETIME_FRAMES = 1500        # radiod frames are 20 ms: 30 s without a refresh
KEEPALIVE_SEC = 10
STALL_SEC = 5                 # no new output packets for this long: re-create the channel


@dataclass
class Channel:
    ssrc: int
    freq_hz: float
    mode: str
    label: str = ""
    kind: str = "listen"            # listen | watch | ft8
    band: str = "hf"                # hf | vhf
    sample_rate: int = 12000
    parked: bool = False            # True: not in radiod (other band selected)
    filter_hz: Optional[tuple] = None  # (low, high) audio passband instead of the preset's
    stereo: bool = False            # WFM: decode FM stereo (two interleaved channels at 48 kHz)
    packets: Optional[int] = None   # radiod's output packet count, for stall detection
    packets_t: Optional[float] = None
    reopens: int = 0
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
        self.band = "hf"
        self.vhf = Vhf()
        self.on_tuner_move = []          # async callbacks (the spectrum follows the tuner)
        self.vhf_paused = False          # a VHF sweep owns the tuner; VHF channels are parked
        self.store_event = lambda kind, **kw: None

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
        if self.vhf.active:
            await self.vhf.disable()     # leave the radio on HF for the next user
        if self.ctl:
            await asyncio.to_thread(self.ctl.close)

    # -- channels --------------------------------------------------------
    # A channel keeps its real (RF) frequency and its band. On VHF radiod actually
    # demodulates the tuner's inverted IF, so _radiod_params() maps it. Channels of the
    # band not in use are "parked": kept here, absent from radiod.
    def _check(self, freq_hz: float, mode: str, band: str):
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}; choose from {', '.join(MODES)}")
        if mode not in BAND_MODES[band]:
            raise ValueError(f"{mode.upper()} isn't offered on {band.upper()}; use {', '.join(BAND_MODES[band])}")
        if band == "hf" and not 0 < freq_hz < CONFIG.max_freq_hz:
            raise ValueError(f"{freq_hz / 1e6:.4f} MHz is outside 0-{CONFIG.max_freq_hz / 1e6:.1f} MHz (HF input)")
        if band == "vhf" and not VHF_MIN_HZ <= freq_hz <= VHF_MAX_HZ:
            raise ValueError(f"{freq_hz / 1e6:.3f} MHz is outside the VHF tuner's "
                             f"{VHF_MIN_HZ / 1e6:.0f}-{VHF_MAX_HZ / 1e6:.0f} MHz")

    def _radiod_params(self, ch):
        preset, rate = MODES[ch.mode]
        if ch.band == "vhf":
            return self.vhf.to_if(ch.freq_hz), MODES[self.vhf.radiod_mode(ch.mode)][0], rate
        return ch.freq_hz, preset, rate

    async def _open(self, ch):
        freq, preset, rate = self._radiod_params(ch)
        await asyncio.to_thread(
            self.ctl.create_channel, frequency_hz=freq, preset=preset, sample_rate=rate,
            agc_enable=1, destination=self.pcm_group, ssrc=ch.ssrc, lifetime=LIFETIME_FRAMES)
        # create_channel's agc flag doesn't take on this radiod; without AGC the audio sits near -55 dBFS
        await asyncio.to_thread(self.ctl.set_agc, ch.ssrc, True)
        if ch.filter_hz:
            await asyncio.to_thread(self.ctl.set_filter, ch.ssrc, *ch.filter_hz)
        if ch.mode == "wfm" and ch.stereo:
            await asyncio.to_thread(self.ctl.set_output_channels, ch.ssrc, 2)
        ch.parked = False

    async def _close(self, ch):
        ch.parked = True
        try:
            await asyncio.to_thread(self.ctl.remove_channel, ch.ssrc)
        except Exception as e:  # radiod may already have dropped it
            log.warning("remove %d: %s", ch.ssrc, e)

    def active(self):
        return [c for c in self.channels.values() if not c.parked]

    async def create(self, freq_hz: float, mode: str = "am", label: str = "", kind: str = "listen",
                     band: Optional[str] = None, filter_hz: Optional[tuple] = None) -> Channel:
        band = band or self.band
        self._check(freq_hz, mode, band)
        if band == "vhf" and band == self.band and not self.vhf_paused and not self.vhf.in_window(freq_hz):
            await self.move_tuner(freq_hz)
        ssrc = secrets.randbelow(0x7FFFFFFF - 1000) + 1000  # stay clear of radiod's static channels
        ch = Channel(ssrc=ssrc, freq_hz=freq_hz, mode=mode, label=label, kind=kind, band=band,
                     sample_rate=MODES[mode][1] or 48000, parked=True, filter_hz=filter_hz)
        self.channels[ssrc] = ch
        if band == self.band and not (band == "vhf" and self.vhf_paused):
            await self._open(ch)
        log.info("created %s %s channel %d at %.3f kHz (%s)%s", band, kind, ssrc, freq_hz / 1e3, mode,
                 "" if band == self.band else " (parked)")
        return ch

    async def tune(self, ssrc: int, freq_hz: Optional[float] = None, mode: Optional[str] = None,
                   label: Optional[str] = None, filter_hz=..., stereo: Optional[bool] = None) -> Channel:
        """filter_hz: (low, high) passband in Hz around the carrier, None for the mode's preset, ... to keep."""
        ch = self.channels[ssrc]
        freq_hz = ch.freq_hz if freq_hz is None else freq_hz
        mode = ch.mode if mode is None else mode
        self._check(freq_hz, mode, ch.band)
        old_mode = ch.mode
        ch.freq_hz, ch.mode = freq_hz, mode
        if mode != old_mode:
            ch.filter_hz = None                  # a new mode starts from its preset's passband
        if filter_hz is not ...:
            ch.filter_hz = tuple(filter_hz) if filter_hz else None
        stereo_changed = stereo is not None and stereo != ch.stereo
        if stereo is not None:
            ch.stereo = stereo
        if label is not None:
            ch.label = label
        if ch.parked:
            return ch
        if ch.band == "vhf" and self.vhf_paused:
            return ch
        if ch.band == "vhf" and not self.vhf.in_window(freq_hz):
            # Tuning on past the edge slides the window along (the new range comes in a step at a time);
            # a jump further away centres it on the new frequency.
            c0 = self.vhf.center_hz or freq_hz
            if abs(freq_hz - c0) < 2 * WINDOW_HZ:
                side = 1 if freq_hz > c0 else -1
                await self.move_tuner(freq_hz - side * (WINDOW_HZ - SLIDE_MARGIN_HZ))
            else:
                await self.move_tuner(freq_hz)   # re-maps every VHF channel, this one included
        else:
            freq, preset, rate = self._radiod_params(ch)
            await asyncio.to_thread(self.ctl.set_frequency, ssrc, freq)
        if mode != old_mode or (filter_hz is not ... and not ch.filter_hz):
            freq, preset, rate = self._radiod_params(ch)        # the preset also restores its own filter
            await asyncio.to_thread(self.ctl.set_preset, ssrc, preset)
            if rate:
                await asyncio.to_thread(self.ctl.set_sample_rate, ssrc, rate)
            await asyncio.to_thread(self.ctl.set_agc, ssrc, True)
            ch.sample_rate = rate or 48000          # WFM: radiod's output is always 48 kHz
        if ch.filter_hz and (filter_hz is not ... or mode != old_mode):
            await asyncio.to_thread(self.ctl.set_filter, ssrc, *ch.filter_hz)
        if ch.mode == "wfm" and (stereo_changed or mode != old_mode):
            await asyncio.to_thread(self.ctl.set_output_channels, ssrc, 2 if ch.stereo else 1)
        return ch

    async def pause_vhf(self):
        """Hand the tuner to a sweep: park every VHF channel."""
        self.vhf_paused = True
        for ch in self.active():
            if ch.band == "vhf":
                await self._close(ch)

    async def resume_vhf(self, center_hz: float):
        """Sweep finished: tuner to center_hz, VHF channels in the window come back."""
        self.vhf_paused = False
        await self.move_tuner(center_hz)

    async def move_tuner(self, center_hz: float):
        """Retune the VHF tuner so center_hz sits at the IF, and re-map the VHF channels."""
        await self.vhf.retune(center_hz)
        for ch in list(self.channels.values()):
            if ch.band != "vhf" or self.band != "vhf" or self.vhf_paused:
                continue
            if self.vhf.in_window(ch.freq_hz):
                if ch.parked:
                    await self._open(ch)          # back inside the tuner's window
                else:
                    await asyncio.to_thread(self.ctl.set_frequency, ch.ssrc, self.vhf.to_if(ch.freq_hz))
            elif not ch.parked:
                await self._close(ch)             # outside the window: pause until the tuner returns
        for cb in self.on_tuner_move:
            await cb()

    async def switch_band(self, band: str, center_hz: Optional[float] = None):
        if band not in ("hf", "vhf"):
            raise ValueError("band is hf or vhf")
        if band == self.band and (band == "hf" or center_hz is None):
            return
        for ch in self.active():
            await self._close(ch)
        self.vhf_paused = False
        if band == "vhf":
            center = center_hz or self.vhf.center_hz or 97.4e6
            await self.vhf.enable(center)
        else:
            await self.vhf.disable()
        self.band = band
        for ch in list(self.channels.values()):   # pages can add channels while this awaits
            if ch.band == band:
                if band == "vhf" and not self.vhf.in_window(ch.freq_hz):
                    continue                      # outside the tuner window; stays parked
                await self._open(ch)
        log.info("band -> %s", band)
        RUNTIME.band, RUNTIME.vhf_center_hz = band, self.vhf.center_hz
        RUNTIME.save()
        self.store_event("band", band=band, center_hz=self.vhf.center_hz if band == "vhf" else None)

    async def restore(self):
        """After radiod restarted: re-select VHF if we were on it, re-create our channels."""
        if self.band == "vhf":
            await self.vhf.enable(self.vhf.center_hz)   # radiod's start-up resets the antenna switch
        n = 0
        for ch in list(self.channels.values()):   # pages can add channels while this awaits
            if not ch.parked:
                await self._open(ch)
                n += 1
        log.info("restored %d channels", n)

    async def remove(self, ssrc: int):
        ch = self.channels.pop(ssrc, None)
        if ch is not None and not ch.parked:
            await self._close(ch)

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
        if ch is None or ch.parked:
            return ch
        d = await self.status(ssrc)
        if d is None:
            return ch
        if d.get("output_samprate"):
            ch.sample_rate = int(d["output_samprate"])
        await self._check_stall(ch, d.get("output_data_packets"))
        ch.power_db, ch.noise_db = d.get("baseband_power"), d.get("noise_density")
        ch.gain_db, ch.snr_db, ch.updated = d.get("gain"), estimate_snr(d), time.time()
        return ch

    async def _check_stall(self, ch, packets):
        """A channel whose output stops (seen right after an input switch) is re-created."""
        now = time.time()
        if packets is None:
            return
        if ch.packets is None or packets != ch.packets:
            ch.packets, ch.packets_t = packets, now
            return
        if now - ch.packets_t >= STALL_SEC:
            log.warning("channel %d (%.3f kHz) produced no audio for %.0f s; re-creating it",
                        ch.ssrc, ch.freq_hz / 1e3, now - ch.packets_t)
            await self._close(ch)
            await self._open(ch)
            ch.reopens += 1
            ch.packets, ch.packets_t = None, now
            self.store_event("channel_reopen", ssrc=ch.ssrc, freq_hz=ch.freq_hz)

    async def _keepalive_loop(self):
        while True:
            await asyncio.sleep(KEEPALIVE_SEC)
            for ssrc in [c.ssrc for c in self.active()]:
                try:
                    await asyncio.to_thread(self.ctl.set_channel_lifetime, ssrc, LIFETIME_FRAMES)
                except Exception as e:
                    log.warning("keepalive %d: %s", ssrc, e)
