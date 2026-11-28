# SPDX-License-Identifier: GPL-3.0-or-later
"""Places named on the radio turned into map positions, with OpenStreetMap.

Addresses, named places and streets are looked up with OSM's Nominatim; a pair of cross streets with the Overpass API
(the node the two streets share). Both are free services with usage rules: Nominatim allows at most 1 request a
second and asks for a User-Agent naming the app, and results must be cached, so every answer (misses too) is kept in
a JSON file and a place is asked about once. Lookups are limited to Santa Clara County, the radio system's
coverage, so a place named without its city (and there are many El Camino Reals) isn't found up the Peninsula.

What's sent is the place as the summary names it (street address, cross streets or a business) and the city,
nothing else from the transcripts.
"""
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

log = logging.getLogger("geo")

NOMINATIM = "https://nominatim.openstreetmap.org/search"
OVERPASS = "https://overpass-api.de/api/interpreter"
USER_AGENT = "rx888-radio P25 incident map (personal, non-commercial)"
VIEWBOX = (-122.21, 37.49, -121.20, 36.89)          # lon/lat corners: Santa Clara County (Palo Alto to Gilroy)
COUNTY = "Santa Clara County"
MIN_GAP_SEC = 1.1                                    # Nominatim: at most one request a second

_DIRS = r"(?:n|s|e|w|north|south|east|west)\.?"
_TYPES = (r"(?:st|street|ave|avenue|av|rd|road|blvd|boulevard|dr|drive|way|ln|lane|ct|court|pl|place|pkwy|parkway|"
          r"expy|expressway|hwy|highway|cir|circle|ter|terrace|row|loop|sq|square)\.?")


def street_core(name: str) -> str:
    """'E Maple St' -> 'Maple': the part of a street name that's spoken the same every time."""
    s = re.sub(r"[^\w\s.'-]", " ", name or "").strip()
    s = re.sub(rf"^{_DIRS}\s+", "", s, flags=re.I)
    s = re.sub(rf"\s+{_TYPES}$", "", s, flags=re.I)
    s = re.sub(rf"\s+{_DIRS}$", "", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip()


def overpass_literal(text: str) -> str:
    """text as a regex that matches it literally, safe inside an Overpass QL string: metacharacters go in a
    bracket ([.]) rather than behind a backslash, which Overpass's strings don't accept (re.escape's '\\.')."""
    return "".join(f"[{ch}]" if ch in ".^$*+?()[]{}|" else ch for ch in text if ch not in '"\\')


class Geocoder:
    def __init__(self, cache_file: Path):
        self.cache_file = Path(cache_file)
        self.lock = threading.Lock()
        self.last = 0.0
        try:
            self.cache = json.loads(self.cache_file.read_text())
        except (OSError, ValueError):
            self.cache = {}

    # ---- the two services, cached
    def _get(self, url: str, data: Optional[bytes] = None, tries: int = 3):
        """GET (or POST data), one request at a time and a second apart; a busy server (429, 5xx) is asked again."""
        for i in range(tries):
            with self.lock:
                wait = self.last + MIN_GAP_SEC - time.time()
                if wait > 0:
                    time.sleep(wait)
                try:
                    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
                    with urllib.request.urlopen(req, timeout=40) as r:
                        return json.loads(r.read())
                except urllib.error.HTTPError as e:
                    if i == tries - 1 or not (e.code == 429 or e.code >= 500):
                        raise
                finally:
                    self.last = time.time()
            time.sleep(3 * (i + 1))

    def _cached(self, key: str, fetch):
        if key in self.cache:
            return self.cache[key]
        val = fetch()
        self.cache[key] = val
        try:
            tmp = self.cache_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.cache))
            tmp.replace(self.cache_file)
        except OSError as e:
            log.warning("geo cache: %s", e)
        return val

    def search(self, q: str) -> Optional[dict]:
        """Nominatim's best match in the Bay Area: {lat, lon, rank, name, bbox} or None."""
        def fetch():
            p = {"q": q, "format": "jsonv2", "limit": 5, "countrycodes": "us", "bounded": 1,
                 "viewbox": ",".join(str(v) for v in VIEWBOX)}
            r = self._get(NOMINATIM + "?" + urllib.parse.urlencode(p))
            if not r:
                return None
            x = next((x for x in r if COUNTY in x.get("display_name", "")), r[0])    # the box's corners reach past it
            return {"lat": float(x["lat"]), "lon": float(x["lon"]), "rank": int(x.get("place_rank") or 0),
                    "name": x.get("display_name", ""), "bbox": [float(v) for v in x.get("boundingbox") or []]}
        return self._cached("n:" + q.lower(), fetch)

    def crossing(self, a: str, b: str, bbox) -> Optional[dict]:
        """Where streets a and b meet inside bbox (south, west, north, east): the node they share, nearest the middle."""
        ca, cb = street_core(a), street_core(b)
        if not ca or not cb or ca.lower() == cb.lower():
            return None
        box = ",".join(f"{v:.4f}" for v in bbox)
        rx = lambda c: "(^| )" + overpass_literal(c) + "( |$)"

        def fetch():
            q = (f'[out:json][timeout:25];way["highway"]["name"~"{rx(ca)}",i]({box})->.a;'
                 f'way["highway"]["name"~"{rx(cb)}",i]({box})->.b;node(w.a)->.na;node(w.b)->.nb;node.na.nb;out;')
            r = self._get(OVERPASS, urllib.parse.urlencode({"data": q}).encode())
            pts = [(e["lat"], e["lon"]) for e in r.get("elements", []) if e.get("type") == "node"]
            if not pts:
                return None
            mid = ((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)
            lat, lon = min(pts, key=lambda p: (p[0] - mid[0]) ** 2 + (p[1] - mid[1]) ** 2)
            return {"lat": lat, "lon": lon, "n": len(pts)}
        return self._cached(f"x:{ca.lower()}|{cb.lower()}|{box}", fetch)

    def city_box(self, city: str):
        """The city's bounding box (south, west, north, east), a little padded; the whole area without a city."""
        r = self.search(f"{city}, California") if city else None
        if r and len(r["bbox"]) == 4 and r["rank"] <= 20:
            s, n, w, e = r["bbox"]
            return (s - 0.01, w - 0.01, n + 0.01, e + 0.01)
        return (VIEWBOX[3], VIEWBOX[0], VIEWBOX[1], VIEWBOX[2])

    # ---- one place from the summary
    def locate(self, p: dict) -> Optional[dict]:
        """{address, cross, place, city} -> {lat, lon, how, approx} or None. Tried in order: the street address,
        the named place, the cross streets, then the street alone (approximate)."""
        city = (p.get("city") or "").strip()
        at = lambda *parts: ", ".join(x for x in parts if x)
        addr, place = (p.get("address") or "").strip(), (p.get("place") or "").strip()
        cross = [c.strip() for c in (p.get("cross") or []) if isinstance(c, str) and c.strip()][:2]
        if addr and re.match(r"^\d+\s", addr):
            r = self.search(at(addr, city))
            if r and r["rank"] >= 28:
                return {"lat": r["lat"], "lon": r["lon"], "how": "address", "approx": False}
        if place:
            r = self.search(at(place, city))
            if r and r["rank"] >= 26:
                return {"lat": r["lat"], "lon": r["lon"], "how": "place", "approx": r["rank"] < 28}
        if len(cross) == 2:
            r = self.crossing(cross[0], cross[1], self.city_box(city))
            if r:
                return {"lat": r["lat"], "lon": r["lon"], "how": "cross streets", "approx": False}
        for street in ([re.sub(r"^\d+\s+", "", addr)] if addr else []) + cross[:1]:
            r = self.search(at(street, city))
            if r and 26 <= r["rank"] <= 27:
                return {"lat": r["lat"], "lon": r["lon"], "how": "street", "approx": True}
        return None
