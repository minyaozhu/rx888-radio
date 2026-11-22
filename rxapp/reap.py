# SPDX-License-Identifier: GPL-3.0-or-later
"""One-off cleanup: delete the spectrum channels radiod still holds from before streams had a lifetime.

Spectrum streams used to be stopped with remove_channel alone, which only tunes the channel to 0 Hz;
radiod deletes a channel only when its lifetime runs out, so each one stayed forever. This gives every
such orphan (spectrum demod, 0 Hz, not owned by rxapp) a short lifetime, and radiod drops it within a
second. Channels in use are never touched: anything at a nonzero frequency, anything rxapp lists, and
anything that was not at 0 Hz in both of two discovery passes. It never polls an SSRC (polling an
unknown SSRC makes radiod create a default channel).

    .venv/bin/python -m rxapp.reap            # list what would be removed
    .venv/bin/python -m rxapp.reap --apply    # remove it
"""

import argparse
import json
import time
import urllib.request
from typing import Iterable, Set

from ka9q import RadiodControl
from ka9q.discovery import discover_channels_native

from .addr import maddr
from .config import CONFIG
from .spectrum import END_FRAMES

SPECTRUM_DEMODS = (3, 4)          # SPECT_DEMOD, SPECT2_DEMOD


def orphans(channels: Iterable, owned: Set[int]) -> Set[int]:
    """SSRCs of spectrum channels parked at 0 Hz that nobody owns."""
    return {c.ssrc for c in channels
            if c.demod_type in SPECTRUM_DEMODS and not c.frequency and c.ssrc not in owned}


def _discover():
    return discover_channels_native(maddr(CONFIG.status_name), listen_duration=5.0,
                                    interface=CONFIG.iface_addr).values()


def _rxapp_ssrcs() -> Set[int]:
    with urllib.request.urlopen(f"http://127.0.0.1:{CONFIG.port}/api/channels", timeout=5) as r:
        return {int(c["ssrc"]) for c in json.load(r)}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="remove them (default: only list)")
    a = ap.parse_args()
    owned = _rxapp_ssrcs()         # fails loudly if rxapp is down: then we can't tell what it owns
    first = _discover()
    time.sleep(2)
    second = list(_discover())
    owned |= {c.ssrc for c in second if c.frequency}
    reap = orphans(first, owned) & orphans(second, owned)
    print(f"{len(second)} channels, {len(reap)} orphaned spectrum channels at 0 Hz")
    if not a.apply:
        return
    ctl = RadiodControl(maddr(CONFIG.status_name), client_id="reap", interface=CONFIG.iface_addr)
    for ssrc in sorted(reap):
        ctl.set_channel_lifetime(ssrc, END_FRAMES, encoding=0)
        time.sleep(0.005)          # don't flood radiod's command socket
    time.sleep(3)
    left = orphans(_discover(), owned) & reap
    print(f"gave {len(reap)} a {END_FRAMES}-frame lifetime; {len(left)} still present")


if __name__ == "__main__":
    main()
