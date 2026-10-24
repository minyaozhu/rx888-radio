# SPDX-License-Identifier: GPL-3.0-or-later
"""Places mentioned in public-safety transcripts, with coordinates, for the public-safety map.

Two kinds:
  * city / town names around the Bay, from the built-in list below (positions are town centres);
  * a highway plus a cross street or landmark ("southbound 680 north of Capitol Expressway"): the cross street
    is looked up with OpenStreetMap's Nominatim geocoder inside the Bay Area, near the city mentioned in the
    same call (or the channel's home city) when there is one. Approximate: a cross street can be miles long.

Nominatim's usage policy: at most 1 request per second, a real User-Agent, cache the results. Only road and
place names are sent, never house numbers. Lookups run in a background thread; a place not looked up yet comes
back without coordinates and appears on the map once it has them.
"""

import json
import logging
import re
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

NOMINATIM = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "rx888-toolkit public-safety map (personal, non-commercial)"
VIEWBOX = "-123.2,38.6,-121.2,36.8"          # lon/lat corners around the Bay Area and the South Bay

# Town centres (approximate)
CITIES = {
    "san francisco": (37.7749, -122.4194), "oakland": (37.8044, -122.2712), "san jose": (37.3382, -121.8863),
    "berkeley": (37.8715, -122.2730), "alameda": (37.7652, -122.2416), "richmond": (37.9358, -122.3477),
    "fremont": (37.5485, -121.9886), "hayward": (37.6688, -122.0808), "union city": (37.5934, -122.0439),
    "newark": (37.5297, -122.0402), "milpitas": (37.4323, -121.8996), "santa clara": (37.3541, -121.9552),
    "sunnyvale": (37.3688, -122.0363), "mountain view": (37.3861, -122.0839), "palo alto": (37.4419, -122.1430),
    "menlo park": (37.4530, -122.1817), "redwood city": (37.4852, -122.2364), "san carlos": (37.5072, -122.2605),
    "belmont": (37.5202, -122.2758), "san mateo": (37.5630, -122.3255), "burlingame": (37.5841, -122.3661),
    "millbrae": (37.5985, -122.3872), "san bruno": (37.6305, -122.4111), "south san francisco": (37.6547, -122.4077),
    "daly city": (37.6879, -122.4702), "pacifica": (37.6138, -122.4869), "half moon bay": (37.4636, -122.4286),
    "foster city": (37.5585, -122.2711), "east palo alto": (37.4688, -122.1411), "los altos": (37.3852, -122.1141),
    "cupertino": (37.3230, -122.0322), "saratoga": (37.2638, -122.0230), "los gatos": (37.2358, -121.9624),
    "campbell": (37.2872, -121.9500), "morgan hill": (37.1305, -121.6544), "gilroy": (37.0058, -121.5683),
    "san martin": (37.0855, -121.6105), "san leandro": (37.7249, -122.1561), "castro valley": (37.6941, -122.0864),
    "dublin": (37.7022, -121.9358), "pleasanton": (37.6624, -121.8747), "livermore": (37.6819, -121.7680),
    "san ramon": (37.7799, -121.9780), "danville": (37.8216, -121.9999), "walnut creek": (37.9101, -122.0652),
    "concord": (37.9780, -122.0311), "pleasant hill": (37.9480, -122.0608), "martinez": (38.0194, -122.1341),
    "lafayette": (37.8858, -122.1180), "orinda": (37.8771, -122.1797), "moraga": (37.8349, -122.1297),
    "antioch": (38.0049, -121.8058), "pittsburg": (38.0280, -121.8847), "brentwood": (37.9319, -121.6958),
    "el cerrito": (37.9161, -122.3108), "pinole": (38.0044, -122.2989), "hercules": (38.0172, -122.2886),
    "vallejo": (38.1041, -122.2566), "benicia": (38.0494, -122.1586), "fairfield": (38.2494, -122.0400),
    "napa": (38.2975, -122.2869), "sausalito": (37.8591, -122.4853), "mill valley": (37.9060, -122.5450),
    "san rafael": (37.9735, -122.5311), "novato": (38.1074, -122.5697), "petaluma": (38.2324, -122.6367),
    "santa rosa": (38.4404, -122.7141), "scotts valley": (37.0511, -122.0147), "santa cruz": (36.9741, -122.0308),
    "watsonville": (36.9102, -121.7569), "hollister": (36.8525, -121.4016), "alviso": (37.4263, -121.9738),
    "almaden": (37.2232, -121.8640), "evergreen": (37.3170, -121.7810), "coyote": (37.2183, -121.7377),
    "sunol": (37.5944, -121.8883), "woodside": (37.4299, -122.2539), "portola valley": (37.3841, -122.2352),
    "atherton": (37.4613, -122.1977), "brisbane": (37.6808, -122.3999), "emeryville": (37.8313, -122.2852),
    "albany": (37.8869, -122.2978), "piedmont": (37.8244, -122.2317), "san lorenzo": (37.6810, -122.1244),
}
# Channels whose dispatcher is in a known city: a cross street heard there is looked up near it
HOME_CITY = {42500000: "san jose", 42280000: "san jose"}
# Highways around the Bay, as spoken ("101", "two eighty", "highway 17", "interstate 880")
HIGHWAYS = ["101", "280", "680", "880", "580", "80", "238", "237", "17", "85", "87", "92", "84", "1", "9", "35",
            "4", "24", "13", "152", "156", "25", "129", "130", "262", "37", "29", "12"]
_HWY = r"(?:highway |hwy |route |state route |sr[- ]?|interstate |i[- ]?)?(" + "|".join(sorted(HIGHWAYS, key=len, reverse=True)) + r")\b"
_DIR = r"(?:(north|south|east|west)bound |\b(nb|sb|eb|wb) )"
_REL = r"(at|near|north of|south of|east of|west of|before|after|approaching|just north of|just south of|just east of|just west of|on ?ramp (?:from|to)|off ?ramp (?:to|at))"
_CROSS = r"((?:[a-z]+ ){0,3}?(?:expressway|road|rd|avenue|ave|boulevard|blvd|street|st|parkway|pkwy|way|drive|dr|lane|ln|highway \d+|interchange|overcrossing|bridge|summit|grade|curve|exit))"
# Whisper's usual misspellings of local road names
FIXES = {"capital expressway": "capitol expressway", "capital avenue": "capitol avenue", "tulley": "tully",
         "almond expressway": "almaden expressway", "almaden express way": "almaden expressway",
         "lawrence express way": "lawrence expressway", "san tomas express way": "san tomas expressway",
         "montague express way": "montague expressway", "central express way": "central expressway",
         "sand hill": "sand hill", "page mill": "page mill"}
ROAD_RE = re.compile(_DIR + "?" + _HWY + r"[ ,]+" + _REL + r" (?:the )?" + _CROSS, re.I)
CITY_RE = re.compile(r"\b(" + "|".join(sorted((re.escape(c) for c in CITIES), key=len, reverse=True)) + r")\b", re.I)


def extract(text: str) -> List[dict]:
    """Place mentions in one transcript: cities, and highway + cross street."""
    t = " ".join((text or "").lower().replace("-", " ").split())
    for wrong, right in FIXES.items():
        t = t.replace(wrong, right)
    out = []
    for m in CITY_RE.finditer(t):
        name = m.group(1)
        if not any(p["kind"] == "city" and p["name"] == name for p in out):
            out.append({"kind": "city", "name": name, "text": m.group(0)})
    for m in ROAD_RE.finditer(t):
        d = (m.group(1) or {"nb": "north", "sb": "south", "eb": "east", "wb": "west"}.get(m.group(2) or "", "") or "")
        cross = m.group(5).strip()
        if re.search(r"\d{3,}", cross):            # never a house number
            continue
        out.append({"kind": "road", "hwy": m.group(3), "dir": d, "rel": m.group(4), "cross": cross,
                    "text": m.group(0)})
    return out


class Geocoder:
    """Nominatim lookups for cross streets, cached on disk, one request per second, in the background."""

    def __init__(self, cache_path: Path):
        self.path = cache_path
        self.cache: Dict[str, Optional[list]] = {}
        try:
            self.cache = json.loads(cache_path.read_text())
        except (OSError, ValueError):
            pass
        self.queue: deque = deque()
        self.queued = set()
        self.lock = threading.Lock()
        self.last_error: Optional[str] = None
        threading.Thread(target=self._worker, daemon=True).start()

    @staticmethod
    def key(cross: str, city: Optional[str]) -> str:
        return f"{cross}|{city or ''}"

    def lookup(self, cross: str, city: Optional[str]):
        """Coordinates if known ([lat, lon] or None = not found); else queue it and return 'pending'."""
        k = self.key(cross, city)
        with self.lock:
            if k in self.cache:
                return self.cache[k]
            if k not in self.queued:
                self.queued.add(k)
                self.queue.append((k, cross, city))
        return "pending"

    def _query(self, q: str):
        url = NOMINATIM + "?" + urllib.parse.urlencode({"q": q, "format": "jsonv2", "limit": 1, "viewbox": VIEWBOX,
                                                         "bounded": 1, "countrycodes": "us"})
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as r:
            res = json.loads(r.read())
        return [round(float(res[0]["lat"]), 5), round(float(res[0]["lon"]), 5)] if res else None

    def _worker(self):
        while True:
            try:
                k, cross, city = self.queue.popleft()
            except IndexError:
                time.sleep(0.5)
                continue
            q = f"{cross}, {city.title()}, California" if city else f"{cross}, California"
            try:
                pos = self._query(q)
                self.last_error = None
            except Exception as e:                  # network trouble: try again later
                self.last_error = str(e)
                log.warning("geocode %r: %s", q, e)
                time.sleep(30)
                with self.lock:
                    self.queued.discard(k)
                continue
            with self.lock:
                self.cache[k] = pos
                self.queued.discard(k)
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    self.path.write_text(json.dumps(self.cache))
                except OSError:
                    pass
            time.sleep(1.1)                          # Nominatim: at most 1 request per second


def locate(rec: dict, geo: Geocoder, home_city: Optional[str] = None) -> List[dict]:
    """Places in one transmission (both readings), with coordinates where known."""
    found = extract(rec.get("text") or "") + extract(rec.get("text_atc") or "")
    seen, out = set(), []
    cities = [p["name"] for p in found if p["kind"] == "city"]
    for p in found:
        sig = (p["kind"], p.get("name") or p.get("cross"), p.get("hwy"))
        if sig in seen:
            continue
        seen.add(sig)
        if p["kind"] == "city":
            lat, lon = CITIES[p["name"]]
            out.append({**p, "lat": lat, "lon": lon, "approx_km": 3})
        else:
            pos = geo.lookup(p["cross"], cities[0] if cities else home_city)
            out.append({**p, "lat": pos[0] if isinstance(pos, list) else None,
                        "lon": pos[1] if isinstance(pos, list) else None,
                        "pending": pos == "pending", "approx_km": 2})
    return out
