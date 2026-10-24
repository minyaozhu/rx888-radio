# SPDX-License-Identifier: GPL-3.0-or-later
"""Aircraft positions from the adsb.fi open-data ADS-B feed, with recent tracks.

The RX-888's antenna can't hear 1090 MHz (see README, "ADS-B (not yet)"), so the airband page shows
positions from adsb.fi, a volunteer network of receivers. Terms: personal, non-commercial use, at most
1 request per second, and the page must credit adsb.fi with a link. This polls every POLL_SEC, and only
while a page has asked for aircraft in the last IDLE_SEC, so nothing is fetched when nobody is looking.
"""

import asyncio
import json
import logging
import time
import urllib.request
from collections import deque
from typing import Dict, Optional

log = logging.getLogger(__name__)

URL = "https://opendata.adsb.fi/api/v2/lat/{lat}/lon/{lon}/dist/{nm}"
POLL_SEC = 5.0
IDLE_SEC = 120.0
TRACK_SEC = 20 * 60          # history kept per aircraft
FORGET_SEC = 5 * 60          # aircraft not reported for this long are dropped
USER_AGENT = "rx888-toolkit airband map (personal, non-commercial)"


def _num(v):
    return v if isinstance(v, (int, float)) else None


class AdsbFeed:
    def __init__(self, lat=37.6, lon=-122.2, nm=45):
        self.lat, self.lon, self.nm = lat, lon, nm
        self.aircraft: Dict[str, dict] = {}        # hex -> latest state
        self.tracks: Dict[str, deque] = {}         # hex -> (t, lat, lon, alt_ft or None)
        self.last_fetch: Optional[float] = None
        self.last_error: Optional[str] = None
        self.wanted_until = 0.0
        self._task: Optional[asyncio.Task] = None

    def start(self):
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        if self._task:
            self._task.cancel()

    def _fetch(self):
        url = URL.format(lat=self.lat, lon=self.lon, nm=self.nm)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def ingest(self, data, now=None):
        """Merge one feed response: latest state per aircraft, plus a point on its track."""
        now = now or time.time()
        for a in data.get("ac") or data.get("aircraft") or []:
            hx = a.get("hex")
            lat, lon = _num(a.get("lat")), _num(a.get("lon"))
            if not hx or lat is None or lon is None:
                continue
            alt = a.get("alt_baro")
            alt_ft = 0 if alt == "ground" else _num(alt)
            t = now - (_num(a.get("seen_pos")) or 0.0)          # when that position was received
            self.aircraft[hx] = {
                "hex": hx, "flight": (a.get("flight") or "").strip(), "type": a.get("t") or "",
                "reg": a.get("r") or "", "lat": lat, "lon": lon, "alt": alt_ft, "ground": alt == "ground",
                "gs": _num(a.get("gs")), "track": _num(a.get("track")) if a.get("track") is not None else _num(a.get("true_heading")),
                "rate": _num(a.get("baro_rate") if a.get("baro_rate") is not None else a.get("geom_rate")),
                "squawk": a.get("squawk") or "", "t": t,
            }
            tr = self.tracks.setdefault(hx, deque())
            if not tr or t - tr[-1][0] >= 1.0:
                tr.append((round(t, 1), round(lat, 5), round(lon, 5), alt_ft))
        for hx in list(self.aircraft):
            if now - self.aircraft[hx]["t"] > FORGET_SEC:
                self.aircraft.pop(hx)
                self.tracks.pop(hx, None)
        for tr in self.tracks.values():
            while tr and now - tr[0][0] > TRACK_SEC:
                tr.popleft()

    async def _loop(self):
        while True:
            if time.time() < self.wanted_until:
                try:
                    data = await asyncio.to_thread(self._fetch)
                    self.ingest(data)
                    self.last_fetch, self.last_error = time.time(), None
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self.last_error = str(e)
                    log.warning("ADS-B feed: %s", e)
            await asyncio.sleep(POLL_SEC)

    def snapshot(self, since: float = 0.0):
        """Aircraft now, with the track points newer than `since` (the page keeps the rest)."""
        self.wanted_until = time.time() + IDLE_SEC
        out = []
        for hx, a in self.aircraft.items():
            out.append({**a, "trail": [p for p in self.tracks.get(hx, ()) if p[0] > since]})
        return {"now": time.time(), "fetched": self.last_fetch, "error": self.last_error,
                "source": "adsb.fi", "source_url": "https://adsb.fi", "aircraft": out}
