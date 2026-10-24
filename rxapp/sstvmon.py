# SPDX-License-Identifier: GPL-3.0-or-later
"""SSTV monitor: watch the SSTV calling frequencies and decode every picture sent.

One SSB channel per frequency (LSB below 10 MHz, USB above). Every few seconds the last
20 s of each channel's audio is checked for SSTV (the Malahit toolkit's detector: most energy
in the 1100-2350 Hz video band, and a 1200 Hz sync pulse repeating once per picture line).
When it fires, the channel captures from 40 s before the trigger until SSTV has been gone for
20 s (or 6 minutes), then the `sstv` decoder (Rust, 18 modes) looks for pictures in it. Pictures
are saved as PNG with the audio; captures without a picture are dropped.
"""

import asyncio
import logging
import time
import wave
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

from .config import CONFIG, RUNTIME

log = logging.getLogger(__name__)

RATE = 12000
CHANNELS = [("80m", 3.845e6, "lsb"), ("40m", 7.171e6, "lsb"), ("20m", 14.230e6, "usb"),
            ("20m-alt", 14.233e6, "usb"), ("15m", 21.340e6, "usb"), ("10m", 28.680e6, "usb")]
CHECK_EVERY = 5.0          # s between detector runs
WINDOW = 20.0              # s of audio the detector looks at
PRE_ROLL = 40.0            # s kept before a trigger (the VIS header comes before the picture)
QUIET_END = 20.0           # s without SSTV that ends a capture
MAX_CAPTURE = 360.0        # s; the longest common modes (PD290) take ~290 s
FILTER_HZ = (100.0, 3000.0)
LIVE_EVERY = 5.0           # s between decodes of a capture in progress (the "receiving now" window)
SETTLE = 30.0              # s ignored after a channel starts: its AGC settling looks like sync pulses

# Sync period per line (ms). PD modes send one sync per two lines.
SYNC_MODES = {"Robot 36": 150.0, "Martin M2": 226.8, "Scottie S2": 277.7, "Robot 72": 300.0,
              "Scottie S1": 428.2, "Martin M1": 446.4, "PD90": 703.0, "PD120": 508.5, "PD180": 754.2}


# ---- detector (from the Malahit toolkit's sstv_scan.py) ----------------------------
def _tone_envelope(audio: np.ndarray, f0: float, bw: float) -> np.ndarray:
    """Envelope of a narrow band around f0, at 200 Hz resolution."""
    spec = np.fft.rfft(audio)
    f = np.fft.rfftfreq(len(audio), 1 / RATE)
    spec[np.abs(f - f0) > bw / 2] = 0
    env = np.abs(np.fft.irfft(spec, len(audio)))
    hop = RATE // 200
    return env[: len(env) // hop * hop].reshape(-1, hop).mean(1)


def analyse(audio: np.ndarray) -> dict:
    spec = np.abs(np.fft.rfft(audio * np.hanning(len(audio)))) ** 2
    f = np.fft.rfftfreq(len(audio), 1 / RATE)
    band = spec[(f >= 300) & (f <= 3000)].sum()
    video = spec[(f >= 1100) & (f <= 2350)].sum() / band if band else 0.0
    env = _tone_envelope(audio, 1200, 100)
    env = env - env.mean()
    ac = np.correlate(env, env, mode="full")[len(env) - 1:]
    ac = ac / ac[0] if ac[0] > 0 else ac
    lags_ms = np.arange(len(ac)) * 5.0                 # 200 Hz -> 5 ms per lag
    win = (lags_ms >= 120) & (lags_ms <= 800)
    i = np.flatnonzero(win)[np.argmax(ac[win])]
    period, sync = lags_ms[i], float(ac[i])
    mode = min(SYNC_MODES, key=lambda m: abs(SYNC_MODES[m] - period))
    if abs(SYNC_MODES[mode] - period) > 12:
        mode = "?"
    return {"video": float(video), "sync": sync, "period": float(period), "mode": mode}


def looks_like_sstv(r: dict) -> bool:
    """Generous on purpose: a false trigger only costs a decode attempt."""
    return r["sync"] >= 0.25 and r["video"] >= 0.45


def decode_pictures(audio: np.ndarray) -> list:
    import sstv                                       # the decoder package, imported on first use
    return sstv.decode(audio.astype(np.int16), RATE)   # int16 as is; float input must be scaled to +-1


# ---- one frequency ------------------------------------------------------------------
@dataclass
class Watch:
    band: str
    freq_hz: float
    mode: str
    ssrc: Optional[int] = None
    buf: deque = field(default_factory=deque)          # int16 chunks
    buf_n: int = 0
    total_n: int = 0                                    # samples received since the channel started
    capture: Optional[List[np.ndarray]] = None         # audio since PRE_ROLL before the trigger
    capture_t: Optional[float] = None
    capture_n: int = 0
    last_sstv: float = 0.0
    last: dict = field(default_factory=dict)            # latest detector result
    pictures: int = 0
    triggers: int = 0
    live: Optional[dict] = None                         # the picture so far, while capturing
    live_t: float = 0.0

    def add(self, pcm: bytes):
        a = np.frombuffer(pcm, "<i2")
        self.buf.append(a)
        self.buf_n += len(a)
        self.total_n += len(a)
        keep = int(max(WINDOW, PRE_ROLL) * RATE)
        while self.buf and self.buf_n - len(self.buf[0]) >= keep:
            self.buf_n -= len(self.buf.popleft())
        if self.capture is not None:
            self.capture.append(a)
            self.capture_n += len(a)

    def recent(self, seconds: float) -> Optional[np.ndarray]:
        n = int(seconds * RATE)
        if self.buf_n < n:
            return None
        return np.concatenate(list(self.buf))[-n:]

    def start_capture(self, now):
        pre = np.concatenate(list(self.buf)) if self.buf else np.zeros(0, np.int16)
        self.capture, self.capture_n, self.capture_t = [pre], len(pre), now - len(pre) / RATE
        self.triggers += 1

    def take_capture(self) -> np.ndarray:
        a = np.concatenate(self.capture) if self.capture else np.zeros(0, np.int16)
        self.capture, self.capture_n = None, 0
        return a

    def state(self):
        if self.capture is not None:
            return f"capturing {self.capture_n / RATE:.0f} s"
        return "listening"


class SstvMonitor:
    def __init__(self, radio, rtp, store):
        self.radio, self.rtp, self.store = radio, rtp, store
        self.watches: Dict[str, Watch] = {}
        self._task: Optional[asyncio.Task] = None
        self.dir = CONFIG.data_dir / "sstv"
        store.ensure_sstv()

    @property
    def running(self):
        return self._task is not None

    async def start(self):
        await self.stop(save=False)
        self.dir.mkdir(parents=True, exist_ok=True)
        for band, f, mode in CHANNELS:
            if f >= CONFIG.max_freq_hz:
                continue
            w = Watch(band, f, mode)
            ch = await self.radio.create(f, mode, label=f"SSTV {band}", kind="sstv", band="hf", filter_hz=FILTER_HZ)
            w.ssrc = ch.ssrc
            self.rtp.subscribe(ch.ssrc, self._sub(w))
            self.watches[band] = w
        self._task = asyncio.create_task(self._loop())
        RUNTIME.sstv_on = True
        RUNTIME.save()
        self.store.event("sstv_start")
        log.info("SSTV monitor on %s", ", ".join(self.watches))

    async def stop(self, save: bool = True):
        if self._task:
            self._task.cancel()
            self._task = None
        for w in self.watches.values():
            self.rtp.subs.pop(w.ssrc, None)
            await self.radio.remove(w.ssrc)
        self.watches.clear()
        if save:
            RUNTIME.sstv_on = False
            RUNTIME.save()
            self.store.event("sstv_stop")

    @staticmethod
    def _sub(w):
        return lambda pcm, ts, seq: w.add(pcm)

    async def _loop(self):
        while True:
            await asyncio.sleep(CHECK_EVERY)
            for w in list(self.watches.values()):
                try:
                    await self.check(w, time.time())
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("SSTV %s", w.band)

    async def check(self, w: Watch, now: float):
        a = w.recent(WINDOW)
        if a is None or w.total_n < SETTLE * RATE:
            return
        r = await asyncio.to_thread(analyse, a.astype(np.float64) / 32768)
        w.last = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in r.items()}
        hit = looks_like_sstv(r)
        if hit:
            w.last_sstv = now
        if w.capture is None:
            if hit:
                w.start_capture(now)
                log.info("SSTV %s: signal (%s, sync %.2f, video %.2f), capturing", w.band, r["mode"], r["sync"], r["video"])
            return
        if now - w.last_sstv >= QUIET_END or w.capture_n / RATE >= MAX_CAPTURE:
            t0 = w.capture_t
            audio = w.take_capture()
            w.live = None
            (self.dir / f"live_{w.band}.png").unlink(missing_ok=True)
            await self._decode(w, t0, audio)
        elif now - w.live_t >= LIVE_EVERY:
            w.live_t = now
            await self._live(w, now)

    async def _live(self, w: Watch, now: float):
        """Decode the capture so far: the picture builds up line by line in the page's live window."""
        audio = np.concatenate(w.capture) if w.capture else np.zeros(0, np.int16)
        pics = await asyncio.to_thread(decode_pictures, audio)
        if not pics:
            return                                          # no VIS header (yet): nothing to show
        im = pics[-1]
        rows = int((np.asarray(im.convert("L")) > 0).any(axis=1).sum())
        path = self.dir / f"live_{w.band}.png"
        tmp = path.with_suffix(".tmp.png")
        im.save(tmp)
        tmp.replace(path)
        w.live = {"band": w.band, "freq_hz": w.freq_hz, "mode": str(im.info.get("sstv_mode", "")).replace("Mode.", ""),
                  "rows": rows, "height": im.height, "width": im.width, "updated": now,
                  "started": w.capture_t, "seconds": round(len(audio) / RATE)}

    async def _decode(self, w: Watch, t0: float, audio: np.ndarray):
        pics = await asyncio.to_thread(decode_pictures, audio)
        if not pics:
            log.info("SSTV %s: %.0f s captured, no picture decoded", w.band, len(audio) / RATE)
            return
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(t0))
        wav = self.dir / f"{stamp}_{w.band}.wav"
        with wave.open(str(wav), "wb") as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(RATE)
            f.writeframes(audio.astype("<i2").tobytes())
        for k, im in enumerate(pics):
            png = self.dir / f"{stamp}_{w.band}_{k}.png"
            im.save(png)
            mode = str(im.info.get("sstv_mode", "")).replace("Mode.", "")
            complete = bool(im.info.get("sstv_complete"))
            self.store.add_sstv(t=t0, band=w.band, freq_hz=w.freq_hz, mode=mode, complete=int(complete),
                                width=im.width, height=im.height, png=str(png), wav=str(wav))
            w.pictures += 1
            log.info("SSTV %s: picture %s (%s, %s)", w.band, png.name, mode, "complete" if complete else "partial")
        self.store.event("sstv_picture", band=w.band, pictures=len(pics))

    def info(self):
        return {"running": self.running,
                "channels": [{"band": w.band, "freq_hz": w.freq_hz, "mode": w.mode, "ssrc": w.ssrc,
                              "parked": getattr(self.radio.channels.get(w.ssrc), "parked", True),
                              "state": w.state(), "last": w.last, "triggers": w.triggers,
                              "pictures": w.pictures, "live": w.live} for w in self.watches.values()]}
