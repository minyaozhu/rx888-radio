# SPDX-License-Identifier: GPL-3.0-or-later
import fcntl
import logging
import os
import sys
import time

import uvicorn

from .config import CONFIG

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("ka9q").setLevel(logging.WARNING)
log = logging.getLogger("rxapp")

LOCK_WAIT_SEC = 30      # a previous rxapp still shutting down gets this long to finish
GRACEFUL_SEC = 5        # on shutdown: how long open connections (pages' live websockets) may hold it up


def single_instance(path, wait: float = LOCK_WAIT_SEC):
    """Only one rxapp at a time. A second one would run every background job (scheduled scans, FT8/WSPR/CW
    channels, band and tuner moves) against the same receiver without serving any page: copies left behind by
    restarts piled up to nine, re-tuning under each other and flooding radiod with commands. The lock is held
    for the life of the process (the kernel releases it however the process ends)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+")
    t0 = time.time()
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            f.seek(0)
            f.truncate()
            f.write(f"{os.getpid()}\n")
            f.flush()
            return f
        except BlockingIOError:
            if time.time() - t0 > wait:
                f.seek(0)
                log.error("another rxapp (pid %s) is running; not starting a second one", f.read().strip() or "?")
                sys.exit(1)
            time.sleep(0.5)


if __name__ == "__main__":
    _lock = single_instance(CONFIG.data_dir / "rxapp.lock")
    # Without a limit uvicorn waits for every open connection before shutting down, and pages keep websockets
    # open: a stopped rxapp lived on for hours with its jobs running, its pages still attached to it.
    uvicorn.run("rxapp.server:app", host=CONFIG.host, port=CONFIG.port, log_level="info",
                timeout_graceful_shutdown=GRACEFUL_SEC)
