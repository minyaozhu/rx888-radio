# SPDX-License-Identifier: GPL-3.0-or-later
"""APRS stations from decoded packets (multimon-ng -A prints TNC2 lines; aprslib parses them).

voice_session.py feeds every packet heard on 144.390 MHz to Stations.ingest() and writes snapshot() to
stations.json for the APRS page. A station is keyed by its call sign (or, for an object or item, the
object's name); positions build a track; weather reports and status texts are kept with the station.
"""

import time
from typing import Dict, Optional

TRACK_SEC = 3 * 3600
FORGET_SEC = 12 * 3600

# APRS symbol (primary table "/") -> what it is, for the page's legend and icons. Most common ones only.
KINDS = {">": "car", "k": "truck", "u": "truck", "v": "van", "j": "jeep", "<": "motorcycle", "b": "bicycle",
         "[": "person", "-": "house", "y": "house", "_": "weather", "#": "digipeater", "&": "gateway",
         "r": "repeater", "O": "balloon", "'": "aircraft", "^": "aircraft", "X": "helicopter", "s": "boat",
         "Y": "boat", "a": "ambulance", "f": "fire", "U": "bus", "=": "train", "R": "RV", "/": "dot"}


def kind(symbol_table: Optional[str], symbol: Optional[str]) -> str:
    if not symbol:
        return "other"
    if symbol_table and symbol_table != "/":
        if symbol == "#":
            return "digipeater"
        if symbol == "&":
            return "gateway"
        if symbol == "_":
            return "weather"
        return "other"
    return KINDS.get(symbol, "other")


class Stations:
    def __init__(self):
        self.s: Dict[str, dict] = {}
        self.packets = 0

    def ingest(self, p: dict, now: Optional[float] = None):
        """One aprslib.parse() result."""
        now = now or time.time()
        self.packets += 1
        name = (p.get("object_name") or p.get("item_name") or p.get("from") or "").strip()
        if not name:
            return
        st = self.s.setdefault(name, {"name": name, "first": now, "packets": 0, "track": []})
        st["packets"] += 1
        st["last"] = now
        st["via"] = p.get("from") if p.get("object_name") or p.get("item_name") else None
        st["path"] = p.get("path") or []
        st["format"] = p.get("format")
        st["raw"] = p.get("raw")
        for k in ("symbol", "symbol_table", "comment", "speed", "course", "altitude", "status", "phg", "mbits"):
            if p.get(k) not in (None, ""):
                st[k] = p[k]
        if p.get("weather"):
            st["weather"] = p["weather"]
        lat, lon = p.get("latitude"), p.get("longitude")
        if lat is not None and lon is not None and abs(lat) <= 90 and abs(lon) <= 180:
            st.update(lat=lat, lon=lon, pos_t=now)
            tr = st["track"]
            if not tr or abs(tr[-1][1] - lat) + abs(tr[-1][2] - lon) > 2e-5:
                tr.append([round(now, 1), round(lat, 5), round(lon, 5)])
            while tr and now - tr[0][0] > TRACK_SEC:
                tr.pop(0)
        st["kind"] = kind(st.get("symbol_table"), st.get("symbol"))

    def prune(self, now: Optional[float] = None):
        now = now or time.time()
        for k in [k for k, v in self.s.items() if now - v["last"] > FORGET_SEC]:
            self.s.pop(k)

    def snapshot(self):
        return {"t": time.time(), "packets": self.packets, "stations": sorted(self.s.values(), key=lambda v: -v["last"])}
