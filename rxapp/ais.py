# SPDX-License-Identifier: GPL-3.0-or-later
"""Vessels from AIS messages (as AIS-catcher prints them with -o 5), with recent tracks.

voice_session.py runs AIS-catcher on radiod's IQ around 162.000 MHz while the marine window is on, feeds
each decoded message to Vessels.ingest(), and writes Vessels.snapshot() to vessels.json for the
Maritime page. Position reports (types 1-3, 18, 19, 27) move a vessel; static reports (5, 24) name it.
"""

import time
from typing import Dict, Optional

TRACK_SEC = 2 * 3600          # track kept per vessel
FORGET_SEC = 6 * 3600         # vessels not heard for this long are dropped
POSITION_TYPES = {1, 2, 3, 18, 19, 27}


def category(shiptype: Optional[int]) -> str:
    """AIS ship type code -> a few groups the map colours by."""
    t = shiptype or 0
    if 70 <= t <= 79:
        return "cargo"
    if 80 <= t <= 89:
        return "tanker"
    if 60 <= t <= 69:
        return "passenger"
    if t in (31, 32, 52):
        return "tug"
    if t == 30:
        return "fishing"
    if t in (36, 37):
        return "pleasure"
    if t in (35, 50, 51, 53, 54, 55, 58):
        return "special"         # military, pilot, SAR, port tender, law enforcement, medical...
    if 40 <= t <= 49:
        return "high speed"
    return "other"


class Vessels:
    def __init__(self):
        self.v: Dict[int, dict] = {}
        self.messages = 0

    def ingest(self, m: dict, now: Optional[float] = None):
        now = now or time.time()
        mmsi = m.get("mmsi")
        if not mmsi:
            return
        self.messages += 1
        v = self.v.setdefault(mmsi, {"mmsi": mmsi, "track": [], "first": now, "messages": 0})
        v["messages"] += 1
        v["last"] = now
        for k in ("shipname", "callsign", "shiptype", "shiptype_text", "destination", "imo", "to_bow", "to_stern",
                  "to_port", "to_starboard", "status_text", "channel"):
            if m.get(k) not in (None, ""):
                v[k] = m[k].strip() if isinstance(m[k], str) else m[k]
        if m.get("type") in POSITION_TYPES or ("lat" in m and "lon" in m):
            lat, lon = m.get("lat"), m.get("lon")
            if lat is None or lon is None or abs(lat) > 90 or abs(lon) > 180:      # 91/181 = "not available"
                return
            v.update(lat=lat, lon=lon, pos_t=now)
            for k in ("speed", "course", "heading"):
                if m.get(k) is not None:
                    v[k] = m[k]
            tr = v["track"]
            if not tr or now - tr[-1][0] >= 5 or abs(tr[-1][1] - lat) + abs(tr[-1][2] - lon) > 1e-4:
                tr.append([round(now, 1), round(lat, 5), round(lon, 5)])
            while tr and now - tr[0][0] > TRACK_SEC:
                tr.pop(0)
        v["category"] = category(v.get("shiptype"))

    def prune(self, now: Optional[float] = None):
        now = now or time.time()
        for mmsi in [k for k, v in self.v.items() if now - v["last"] > FORGET_SEC]:
            self.v.pop(mmsi)

    def snapshot(self):
        return {"t": time.time(), "messages": self.messages,
                "vessels": sorted(self.v.values(), key=lambda v: -v["last"])}
