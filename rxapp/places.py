# SPDX-License-Identifier: GPL-3.0-or-later
"""Bay Area towns (approximate centres) and the transcriber's usual misspellings of local road names, for placing
public-safety calls (rxapp/incidents.py). Highway junctions come from rxapp/junctions.py."""

import re

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
# Whisper's usual misspellings of local road names
FIXES = {"capital expressway": "capitol expressway", "capital avenue": "capitol avenue", "tulley": "tully",
         "almond expressway": "almaden expressway", "almaden express way": "almaden expressway",
         "lawrence express way": "lawrence expressway", "san tomas express way": "san tomas expressway",
         "montague express way": "montague expressway", "central express way": "central expressway",
         "mckey": "mckee", "tully road": "tully road"}
CITY_RE = re.compile(r"\b(" + "|".join(sorted((re.escape(c) for c in CITIES), key=len, reverse=True)) + r")\b", re.I)
