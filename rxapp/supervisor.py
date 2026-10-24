# SPDX-License-Identifier: GPL-3.0-or-later
"""Keeps radiod running and rxapp attached to it.

radiod quits when the RX-888 stops sending (unplugged, power-cycled, USB reset). The
watchdog notices missing status replies, restarts radiod (which reloads the firmware),
then re-creates rxapp's channels and the spectrum view.
"""

import asyncio
import logging
import subprocess
import time

from .config import CONFIG, RADIOD_CONFS, RUNTIME

log = logging.getLogger(__name__)

RADIOD_MATCH = "ka9q-radio/src/radiod"
CHECK_SEC = 5
FAILS_BEFORE_RESTART = 3
HEALTH_EVERY_SEC = 30


def radiod_pids():
    r = subprocess.run(["pgrep", "-f", RADIOD_MATCH], capture_output=True, text=True)
    return [int(p) for p in r.stdout.split()]


class Supervisor:
    def __init__(self, radio, spectrum, store):
        self.radio, self.spectrum, self.store = radio, spectrum, store
        self._restarting = False
        self.on_restart = []          # async callbacks after radiod came back (re-apply manual gain, ...)
        self.busy = lambda: False   # set by the server: True while a scan is reconfiguring the spectrum
        self.proc = None
        self.restarts = 0
        self.fails = 0
        self.health = {}
        self._task = None
        self._fps_mark = (time.time(), 0)
        self._last_over = None

    async def ensure_radiod(self):
        if radiod_pids():
            return
        if not CONFIG.manage_radiod:
            raise RuntimeError("radiod is not running (start ka9q/run_radiod.sh, or set RXAPP_MANAGE_RADIOD=1)")
        await self._start_radiod()

    async def _start_radiod(self):
        CONFIG.data_dir.mkdir(parents=True, exist_ok=True)
        logpath = CONFIG.data_dir / "radiod.log"
        self._log_start = logpath.stat().st_size if logpath.exists() else 0
        logf = open(logpath, "ab")
        conf = RADIOD_CONFS[RUNTIME.adc_rate_hz]
        self.proc = subprocess.Popen([str(CONFIG.radiod_script), conf], stdout=logf, stderr=subprocess.STDOUT,
                                     start_new_session=True)
        log.info("started radiod (pid %d)", self.proc.pid)
        self.store.event("radiod_start", pid=self.proc.pid)
        for _ in range(40):            # firmware load + clock setup take a few seconds
            await asyncio.sleep(0.5)
            if self.proc.poll() is not None:
                raise RuntimeError(f"radiod exited ({self.proc.returncode}); see {CONFIG.data_dir / 'radiod.log'}")
            if self._radiod_log_says_running():
                return
        log.warning("radiod did not report 'running' within 20 s")

    def _radiod_log_says_running(self):
        try:
            with open(CONFIG.data_dir / "radiod.log", "rb") as f:
                f.seek(self._log_start)     # only this run's output
                return b"rx888 running" in f.read()
        except OSError:
            return False

    def start(self):
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        if self._task:
            self._task.cancel()

    async def _probe(self):
        """radiod's heartbeat and front-end report: status of any channel we own.

        The spectrum channel is tried first, but scans replace it, so our audio channels
        are tried too; radiod only counts as silent if none of them answers.
        """
        ssrcs = ([self.spectrum._stream.ssrc] if self.spectrum._stream and self.spectrum._stream.ssrc else []) \
            + list(self.radio.channels)
        for ssrc in ssrcs[:4]:
            st = await self.radio.status(ssrc, timeout=1.5)
            if st is not None:
                return st
        return None

    async def _loop(self):
        last_health = 0
        while True:
            await asyncio.sleep(CHECK_SEC)
            try:
                st = await self._probe()
                if st is not None:
                    self.fails = 0
                    fe = st.get("frontend") or {}
                    now = time.time()
                    t0, n0 = self._fps_mark
                    fps = (self.spectrum.frames - n0) / max(now - t0, 1e-3)
                    self._fps_mark = (now, self.spectrum.frames)
                    rate = fe.get("input_samprate")
                    if rate and rate in RADIOD_CONFS and rate != RUNTIME.adc_rate_hz:
                        RUNTIME.adc_rate_hz = float(rate)   # radiod was started elsewhere: adopt its rate
                        RUNTIME.save()
                    over = fe.get("ad_over") or 0
                    recent = 0 if self._last_over is None or over < self._last_over else over - self._last_over
                    self._last_over = over
                    self.health = {
                        "t": now, "ok": True, "restarts": self.restarts, "overloads_recent": recent,
                        "band": self.radio.band, "vhf": self.radio.vhf.info(), "adc_rate_hz": RUNTIME.adc_rate_hz,
                        "input_samprate": fe.get("input_samprate"), "if_power_db": fe.get("if_power"),
                        "rf_gain_db": fe.get("rf_gain"), "rf_atten_db": fe.get("rf_atten"),
                        "rf_agc": fe.get("rf_agc"), "ad_over": fe.get("ad_over"),
                        "input_samples": fe.get("input_samples"),
                        "fe_low_hz": fe.get("fe_low_edge"), "fe_high_hz": fe.get("fe_high_edge"),
                        "spectrum_fps": round(fps, 1), "channels": len(self.radio.channels)}
                    if now - last_health >= HEALTH_EVERY_SEC:
                        self.store.add_health(t=now, if_power_db=self.health["if_power_db"],
                                              rf_gain_db=self.health["rf_gain_db"], ad_over=recent,
                                              input_samples=self.health["input_samples"],
                                              spectrum_fps=self.health["spectrum_fps"],
                                              channels=self.health["channels"])
                        last_health = now
                    continue
                if self._restarting or (self.busy() and radiod_pids()):
                    continue                 # a scan is swapping spectrum channels; not a radiod failure
                self.fails += 1
                self.health = {**self.health, "ok": False, "fails": self.fails}
                log.warning("no status from radiod (%d)", self.fails)
                if self.fails >= FAILS_BEFORE_RESTART or not radiod_pids():
                    await self._recover()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("watchdog")

    async def set_coverage(self, rate_hz: float):
        """Restart radiod at another ADC rate (64.8 MS/s: 0-32 MHz, 129.6 MS/s: 0-64 MHz)."""
        if rate_hz not in RADIOD_CONFS:
            raise ValueError(f"rate must be one of {sorted(RADIOD_CONFS)}")
        if rate_hz == RUNTIME.adc_rate_hz and radiod_pids():
            return
        RUNTIME.adc_rate_hz = float(rate_hz)
        RUNTIME.save()
        self.store.event("coverage", adc_rate_hz=rate_hz)
        await self._restart_radiod()

    async def _restart_radiod(self):
        self._restarting = True
        try:
            pids = radiod_pids()
            for pid in pids:          # stop it so the USB device is free
                subprocess.run(["kill", str(pid)])
            for _ in range(20):
                if not radiod_pids():
                    break
                await asyncio.sleep(0.5)
            await self._start_radiod()
            self.restarts += 1
            self._last_over = None
            await self.radio.restore()
            await self.spectrum.retarget()
            for cb in self.on_restart:
                await cb()
            self.fails = 0
        finally:
            self._restarting = False

    async def _recover(self):
        if not CONFIG.manage_radiod:
            return
        self.store.event("radiod_recover", running=bool(radiod_pids()), fails=self.fails)
        await self._restart_radiod()
        log.info("recovered radiod (restart #%d)", self.restarts)
