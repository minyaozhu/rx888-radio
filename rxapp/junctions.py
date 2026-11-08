# SPDX-License-Identifier: GPL-3.0-or-later
"""Where Bay Area streets meet the freeways and state routes, for placing "southbound 880 north of Brokaw".

A dispatcher names a highway and a cross street; the cross street alone can be miles long, but where it meets
that highway is one point. rxapp/data/junctions.json holds, for each highway, every named street that touches it
and the point where it does. It is built once from OpenStreetMap (Overpass API) with
    python -m rxapp.junctions --build
and then used offline. Map data (c) OpenStreetMap contributors, ODbL 1.0 (https://www.openstreetmap.org/copyright).

Two ways to build it:
    python -m rxapp.junctions --pbf norcal-latest.osm.pbf   (recommended: a Geofabrik extract, read locally with
        the osmium package, pip install osmium; a few minutes, no server involved)
    python -m rxapp.junctions --build                        (Overpass API, tile by tile; slow when it is busy)

Lookup is forgiving about what transcription does to street names: "Doherty" finds Dougherty Road, "the Stevenson"
finds Stevenson Boulevard, "Auto Mall" finds Auto Mall Parkway, and a mangled highway number ("southbound 80 north
of Brokaw") is resolved by which highway actually meets that street.
"""

import difflib
import json
import math
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DATA = Path(__file__).with_name("data") / "junctions.json"
OVERPASS = "https://overpass-api.de/api/interpreter"
USER_AGENT = "rx888-radio public-safety map (personal, non-commercial)"
BBOX = (36.85, -122.75, 38.35, -121.40)          # south, west, north, east: the Bay Area to Gilroy and Vallejo

# Highways the CHP and county channels around the Bay talk about, with their OpenStreetMap refs
HIGHWAYS = {
    "101": "US 101", "280": "I 280", "680": "I 680", "880": "I 880", "580": "I 580", "80": "I 80", "780": "I 780",
    "380": "I 380", "238": "CA 238", "237": "CA 237", "17": "CA 17", "85": "CA 85", "87": "CA 87", "92": "CA 92",
    "84": "CA 84", "24": "CA 24", "4": "CA 4", "13": "CA 13", "1": "CA 1", "242": "CA 242", "35": "CA 35",
    "9": "CA 9", "152": "CA 152", "12": "CA 12", "29": "CA 29", "37": "CA 37", "160": "CA 160", "262": "CA 262",
    "82": "CA 82", "25": "CA 25", "156": "CA 156", "129": "CA 129", "130": "CA 130", "61": "CA 61", "123": "CA 123",
}
CROSS_CLASSES = ("motorway|trunk|primary|secondary|tertiary|unclassified|residential|"
                 "primary_link|secondary_link|tertiary_link")
NEAR_M = 300           # candidates: streets with a stored point this close (Overpass checks points, not lines)
# Highways also called by the street they become ("880 north of Mission" = the CA 262 interchange in Fremont)
HIGHWAY_NAMES = {"262": ["Mission Boulevard"], "238": ["Mission Boulevard"], "84": ["Thornton Avenue"],
                 "130": ["Alum Rock Avenue"], "237": [], "85": [], "87": ["Guadalupe Parkway"], "92": [], "17": []}
HW_TOUCH_M = 600        # a highway ending at another one joins it by unnamed ramps, so allow more
TOUCH_M = 60            # a street within this of the highway's centre line meets it
SPLIT_KM = 2.5          # the same name meeting the highway again this far away is another junction

SUFFIXES = {"road": "rd", "rd": "rd", "avenue": "ave", "ave": "ave", "av": "ave", "boulevard": "blvd", "blvd": "blvd",
            "street": "st", "st": "st", "parkway": "pkwy", "pkwy": "pkwy", "expressway": "expy", "expy": "expy",
            "drive": "dr", "dr": "dr", "lane": "ln", "ln": "ln", "way": "way", "court": "ct", "ct": "ct",
            "place": "pl", "pl": "pl", "highway": "hwy", "hwy": "hwy", "freeway": "fwy", "fwy": "fwy",
            "circle": "cir", "terrace": "ter", "trail": "trl", "square": "sq", "plaza": "plz", "alley": "aly"}
NUM_WORDS = {"first": "1st", "second": "2nd", "third": "3rd", "fourth": "4th", "fifth": "5th", "sixth": "6th",
             "seventh": "7th", "eighth": "8th", "ninth": "9th", "tenth": "10th"}


def norm(name: str) -> Tuple[str, str]:
    """(core, suffix): 'North First Street' -> ('north 1st', 'st'); 'Auto Mall Pkwy' -> ('auto mall', 'pkwy')."""
    w = re.sub(r"[^a-z0-9 ]", " ", name.lower().replace("'", "")).split()
    w = [NUM_WORDS.get(x, x) for x in w if x not in ("the",)]
    if len(w) > 2 and w[0] in ("north", "south", "east", "west", "n", "s", "e", "w"):
        w = w[1:]                                   # "East Brokaw Road": dispatch says "Brokaw"
    if w and w[0] in ("saint", "st") and len(w) > 1 and w[-1] != w[0]:
        w[0] = "st"
    suf = ""
    if len(w) > 1 and w[-1] in SUFFIXES:
        suf = SUFFIXES[w.pop()]
    return " ".join(w), suf


def km(a, b) -> float:
    r = math.pi / 180
    h = math.sin((b[0] - a[0]) * r / 2) ** 2 + math.cos(a[0] * r) * math.cos(b[0] * r) * math.sin((b[1] - a[1]) * r / 2) ** 2
    return 12742 * math.asin(math.sqrt(min(1.0, h)))


# ------------------------------------------------------------------ build (once, from OpenStreetMap)

def _overpass(q: str, tries=4) -> dict:
    for i in range(tries):
        try:
            req = urllib.request.Request(OVERPASS, data=urllib.parse.urlencode({"data": q}).encode(),
                                         headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=1000) as r:
                d = json.loads(r.read())
            if d.get("remark") and not d.get("elements"):
                raise RuntimeError(d["remark"][:200])
            return d
        except Exception as e:                      # busy server: wait and retry
            print(f"  overpass: {e}; retry {i + 1}", file=sys.stderr)
            time.sleep(30 * (i + 1))
    raise RuntimeError("Overpass unavailable")


def _seg_dist_m(p, a, b) -> float:
    """Metres from p to segment a-b (equirectangular; fine at these distances)."""
    kx = 111320 * math.cos(math.radians(p[0])); ky = 110540
    ax, ay = (a[1] - p[1]) * kx, (a[0] - p[0]) * ky
    bx, by = (b[1] - p[1]) * kx, (b[0] - p[0]) * ky
    dx, dy = bx - ax, by - ay
    t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / (dx * dx + dy * dy)))
    return math.hypot(ax + t * dx, ay + t * dy)


TILE = 0.2                                        # degrees: small queries the public server answers quickly


def _tile(s, w, n, e) -> Tuple[list, list]:
    """The tile's highways and the named streets near them, with geometry. Overpass's 'around' filter misses
    streets that cross a highway between two far-apart points (a bridge), so it only picks candidates within
    NEAR_M and the crossing itself is worked out here."""
    refs = "|".join(sorted(set(HIGHWAYS.values())))
    q = (f'[out:json][timeout:600][maxsize:1073741824][bbox:{s},{w},{n},{e}];'
         f'way["highway"~"^(motorway|trunk|primary|secondary)$"]["ref"~"(^|;) *({refs})($| *;)"]->.h;'
         f'way(around.h:{NEAR_M})["name"]["highway"~"^({CROSS_CLASSES})$"]->.c;'
         f'(.h; .c;); out tags geom;')
    d = _overpass(q)
    hw, cross = [], []
    wanted = set(HIGHWAYS.values())
    for el in d.get("elements", []):
        g = [(p["lat"], p["lon"]) for p in el.get("geometry", []) if p]
        if len(g) < 2:
            continue
        tags = el.get("tags", {})
        refs_ = {r.strip() for r in tags.get("ref", "").split(";")} & wanted
        if refs_ and tags.get("highway") in ("motorway", "trunk", "primary", "secondary"):
            hw.append((refs_, g))
        if tags.get("name"):
            cross.append((tags["name"], refs_, g))
    return hw, cross


def _xy(p, lat0):
    return (p[1] * 111320 * math.cos(math.radians(lat0)), p[0] * 110540)


def _seg_seg(a, b, c, d, lat0) -> Tuple[float, tuple]:
    """Metres between segments a-b and c-d, and the point on c-d nearest a-b (lat, lon)."""
    A, B, C, D = (_xy(p, lat0) for p in (a, b, c, d))

    def cross(o, p, q):
        return (p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0])
    d1, d2, d3, d4 = cross(A, B, C), cross(A, B, D), cross(C, D, A), cross(C, D, B)
    if (d1 > 0) != (d2 > 0) and (d3 > 0) != (d4 > 0) and d1 != d2:
        t = d1 / (d1 - d2)                           # they cross
        return 0.0, (c[0] + t * (d[0] - c[0]), c[1] + t * (d[1] - c[1]))
    return min((_seg_dist_m(c, a, b), c), (_seg_dist_m(d, a, b), d),
               (_seg_dist_m(a, c, d), c if _seg_dist_m(a, c, c) < _seg_dist_m(a, d, d) else d),
               (_seg_dist_m(b, c, d), c if _seg_dist_m(b, c, c) < _seg_dist_m(b, d, d) else d))


def _junctions(hw, cross) -> List[list]:
    """[highway number, street name, lat, lon] for each named street that meets a highway in the tile."""
    by_ref = {v: k for k, v in HIGHWAYS.items()}
    G = 0.002                                        # grid cell (degrees, ~200 m)
    grid: Dict[tuple, list] = {}
    for refs_, g in hw:
        for i in range(len(g) - 1):
            a, b = g[i], g[i + 1]
            for cy in range(int(min(a[0], b[0]) // G) - 4, int(max(a[0], b[0]) // G) + 5):
                for cx in range(int(min(a[1], b[1]) // G) - 4, int(max(a[1], b[1]) // G) + 5):
                    grid.setdefault((cy, cx), []).append((refs_, a, b))
    out = []
    unnamed = [(None, refs_, g) for refs_, g in hw]   # highway meets highway: listed as "Highway 101" too
    for name, own, g in cross + unnamed:
        best: Dict[str, tuple] = {}
        limit = TOUCH_M if name else HW_TOUCH_M
        for i in range(len(g) - 1):
            c, d = g[i], g[i + 1]
            seen = set()
            for cy in range(int(min(c[0], d[0]) // G), int(max(c[0], d[0]) // G) + 1):
                for cx in range(int(min(c[1], d[1]) // G), int(max(c[1], d[1]) // G) + 1):
                    for refs_, a, b in grid.get((cy, cx), ()):
                        if (a, b) in seen:
                            continue
                        seen.add((a, b))
                        dist, p = _seg_seg(a, b, c, d, c[0])
                        if dist <= limit:
                            for r in refs_ - own:    # not the highway itself ("Nimitz Freeway")
                                if r not in best or dist < best[r][0]:
                                    best[r] = (dist, p)
        for r, (dist, p) in best.items():
            names = [name] if name else []
            if not name:
                for r2 in own:
                    names += [f"Highway {by_ref[r2]}"] + HIGHWAY_NAMES.get(by_ref[r2], [])
            for nm in names:
                out.append([by_ref[r], nm, round(p[0], 5), round(p[1], 5)])
    return out


def build():
    """Every named street meeting each highway, tile by tile (resumable: finished tiles are kept)."""
    s0, w0, n0, e0 = BBOX
    work = DATA.with_suffix(".tiles.json")
    try:
        done = json.loads(work.read_text())
    except (OSError, ValueError):
        done = {}
    keys = []
    lat = s0
    while lat < n0 - 1e-9:
        lon = w0
        while lon < e0 - 1e-9:
            keys.append((round(lat, 2), round(lon, 2)))
            lon += TILE
        lat += TILE
    import threading
    from concurrent.futures import ThreadPoolExecutor
    lock = threading.Lock()

    def one(k):
        la, lo = k
        key = f"{la:.2f},{lo:.2f}"
        if key in done:
            return
        t = time.time()
        hw, cross = _tile(la, lo, min(la + TILE, n0), min(lo + TILE, e0))
        found = _junctions(hw, cross)
        with lock:
            done[key] = found
            work.parent.mkdir(exist_ok=True)
            work.write_text(json.dumps(done))
            print(f"tile {key}: {len(hw)} highway ways, {len(cross)} streets, {len(found)} junctions, "
                  f"{time.time() - t:.0f}s ({len(done)}/{len(keys)})", file=sys.stderr, flush=True)

    with ThreadPoolExecutor(2) as ex:                # Overpass allows two queries at a time per address
        list(ex.map(one, keys))
    _write([j for found in done.values() for j in found])


def build_pbf(path: str):
    """Build from an OpenStreetMap extract (.osm.pbf) covering BBOX, e.g. Geofabrik's norcal-latest.osm.pbf."""
    import osmium                                    # pip install osmium (only needed to build)
    s0, w0, n0, e0 = BBOX
    wanted = set(HIGHWAYS.values())
    hw_classes = {"motorway", "trunk", "primary", "secondary"}
    cross_classes = set(CROSS_CLASSES.split("|"))
    hw, cross = [], []
    t = time.time()
    fp = osmium.FileProcessor(path).with_locations().with_filter(osmium.filter.KeyFilter("highway"))
    for o in fp:
        if not o.is_way():
            continue
        cls = o.tags.get("highway")
        name = o.tags.get("name")
        refs_ = {r.strip() for r in (o.tags.get("ref") or "").split(";")} & wanted
        is_hw = bool(refs_) and cls in hw_classes
        if not is_hw and not (name and cls in cross_classes):
            continue
        try:
            g = [(n.lat, n.lon) for n in o.nodes]
        except osmium.InvalidLocationError:
            g = [(n.lat, n.lon) for n in o.nodes if n.location.valid()]
        if len(g) < 2 or not any(s0 <= la <= n0 and w0 <= lo <= e0 for la, lo in g):
            continue
        if is_hw:
            hw.append((refs_, g))
        if name:
            cross.append((name, refs_, g))
    print(f"read {len(hw)} highway ways, {len(cross)} named streets in {time.time() - t:.0f}s", file=sys.stderr)
    found = _junctions(hw, cross)
    _write(found)


def _write(found):
    out: Dict[str, Dict[str, list]] = {}
    for num, name, la, lo in found:
        pts = out.setdefault(num, {}).setdefault(name, [])
        if not any(km(p, (la, lo)) < SPLIT_KM for p in pts):
            pts.append([la, lo])
    DATA.parent.mkdir(exist_ok=True)
    DATA.write_text(json.dumps({"source": "OpenStreetMap contributors, ODbL 1.0", "built": time.strftime("%Y-%m-%d"),
                                "highways": {num: [{"name": nm, "pts": pts} for nm, pts in sorted(st.items())]
                                             for num, st in sorted(out.items())}}, separators=(",", ":")))
    print(f"{sum(len(v) for v in out.values())} highway/street pairs -> {DATA}", file=sys.stderr)


# ------------------------------------------------------------------ lookup

class Junctions:
    """Highway + cross street -> the point where they meet."""

    def __init__(self, path: Path = DATA):
        self.hw: Dict[str, List[Tuple[str, str, str, list]]] = {}
        try:
            data = json.loads(path.read_text()).get("highways", {})
        except (OSError, ValueError):
            data = {}
        for num, streets in data.items():
            self.hw[num] = [(s["name"], *norm(s["name"]), s["pts"]) for s in streets]

    def __bool__(self):
        return bool(self.hw)

    @staticmethod
    def candidates(heard: str) -> List[str]:
        """Highways a heard number may be: itself, then what transcription often makes of it
        ('80' or '88' for 880, '68' for 680, '28' for 280, '192' for 92)."""
        out = [heard]
        if len(heard) == 3 and heard not in HIGHWAYS:
            out.append(heard[1:])
        for h in HIGHWAYS:
            if h != heard and len(h) == 3 and (h.startswith(heard) or h.replace("0", "") == heard
                                               or (len(heard) == 2 and h[1:] == heard)):
                out.append(h)
        return [h for h in out if h in HIGHWAYS]

    def _match(self, num: str, cross: str) -> List[Tuple[float, str, list]]:
        """Streets meeting highway num that the heard name may be; leading extra words are allowed
        ("near auto mall parkway") at a small cost."""
        core, suf = norm(cross)
        words = core.split()
        out = []
        for i in range(min(len(words), 3)):
            out += [(sc - 0.1 * i, name, pts) for sc, name, pts in self._match1(num, " ".join(words[i:]), suf)]
        return out

    def _match1(self, num: str, core: str, suf: str) -> List[Tuple[float, str, list]]:
        if len(core) < 3:
            return []
        out = []
        for name, c, s, pts in self.hw.get(num, ()):
            if c == core:
                sc = 1.0 if (not suf or not s or suf == s) else 0.9
            elif c.startswith(core + " ") or core.startswith(c + " "):
                sc = 0.8                             # "Auto Mall" ~ "Auto Mall Parkway"
            elif c.replace(" ", "") == core.replace(" ", ""):
                sc = 0.95                            # "McKee" / "Mc Kee"
            else:
                r = difflib.SequenceMatcher(None, c, core).ratio()
                if r < 0.8 or abs(len(c) - len(core)) > 3 or min(len(c), len(core)) < 5:
                    continue
                sc = 0.5 + (r - 0.8) * 2            # "Doherty" ~ "Dougherty": 0.5 .. 0.9
            out.append((sc, name, pts))
        return out

    def find(self, heard_hwy: str, cross: str, near: Optional[Tuple[float, float]] = None) -> Optional[dict]:
        """{'hwy', 'street', 'lat', 'lon', 'score', 'corrected'} or None."""
        best = None
        for i, num in enumerate(self.candidates(heard_hwy)):
            for sc, name, pts in self._match(num, cross):
                sc -= 0.15 * (i > 0)                  # prefer the highway as heard
                for p in pts:
                    s2 = sc - (min(km(near, p), 80) / 400 if near else 0)   # tie-break: nearer the channel's area
                    if best is None or s2 > best[0]:
                        best = (s2, num, name, p, sc)
        if not best:
            return None
        s2, num, name, p, sc = best
        return {"hwy": num, "street": name, "lat": p[0], "lon": p[1], "score": round(sc, 2),
                "corrected": num != heard_hwy}


if __name__ == "__main__":
    if "--pbf" in sys.argv:
        build_pbf(sys.argv[sys.argv.index("--pbf") + 1])
    elif "--build" in sys.argv:
        build()
    else:
        print(__doc__)
