"""Keeps radiod running and rxapp attached to it.

radiod quits when the RX-888 stops sending (unplugged, power-cycled, USB reset). The
watchdog notices missing status replies, restarts radiod (which reloads the firmware),
then re-creates rxapp's channels and the spectrum view.
"""

import asyncio
import logging
import subprocess
import time

from .config import CONFIG

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
        self.busy = lambda: False   # set by the server: True while a scan is reconfiguring the spectrum
        self.proc = None
        self.restarts = 0
        self.fails = 0
        self.health = {}
        self._task = None
        self._fps_mark = (time.time(), 0)

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
        self.proc = subprocess.Popen([str(CONFIG.radiod_script)], stdout=logf, stderr=subprocess.STDOUT,
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
                    self.health = {
                        "t": now, "ok": True, "restarts": self.restarts,
                        "input_samprate": fe.get("input_samprate"), "if_power_db": fe.get("if_power"),
                        "rf_gain_db": fe.get("rf_gain"), "rf_atten_db": fe.get("rf_atten"),
                        "rf_agc": fe.get("rf_agc"), "ad_over": fe.get("ad_over"),
                        "input_samples": fe.get("input_samples"),
                        "fe_low_hz": fe.get("fe_low_edge"), "fe_high_hz": fe.get("fe_high_edge"),
                        "spectrum_fps": round(fps, 1), "channels": len(self.radio.channels)}
                    if now - last_health >= HEALTH_EVERY_SEC:
                        self.store.add_health(t=now, if_power_db=self.health["if_power_db"],
                                              rf_gain_db=self.health["rf_gain_db"], ad_over=self.health["ad_over"],
                                              input_samples=self.health["input_samples"],
                                              spectrum_fps=self.health["spectrum_fps"],
                                              channels=self.health["channels"])
                        last_health = now
                    continue
                if self.busy() and radiod_pids():
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

    async def _recover(self):
        if not CONFIG.manage_radiod:
            return
        pids = radiod_pids()
        self.store.event("radiod_recover", running=bool(pids), fails=self.fails)
        for pid in pids:          # hung radiod: stop it so the USB device is free
            subprocess.run(["kill", str(pid)])
        if pids:
            await asyncio.sleep(2)
        await self._start_radiod()
        self.restarts += 1
        await self.radio.restore()
        await self.spectrum._restart()
        self.fails = 0
        log.info("recovered radiod (restart #%d)", self.restarts)
