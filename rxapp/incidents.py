# SPDX-License-Identifier: GPL-3.0-or-later
"""What a public-safety transmission is about, where, and how it moves along: one transmission at a time.

  kind(text)     -> incident types from radio codes and words: [{'cat', 'label', 'code'}], most serious first
  status(text)   -> unit status said in it: enroute / onscene / tow / ambulance / clear / cancel
  units(text)    -> the unit ID a transmission starts with ("12-345, I'm 97" -> '12345')
  where(text)    -> location phrases: highway + cross street, cross street + highway, town names
  locate(...)    -> those phrases placed: a highway junction from rxapp/junctions.py, or a town centre

Only highways, street names and towns are read; house numbers, plates and names are never used for a location.
The codes are the CHP's 11- and 10-codes and California vehicle / penal code sections; agencies differ, so a
type is a guide. Everything is from what the transcriber heard, so expect the odd wrong reading.
"""

import re
from typing import Dict, List, Optional, Tuple

from .junctions import Junctions, km
from .places import CITIES, CITY_RE, FIXES

# ------------------------------------------------------------------ incident types
# (category, label, pattern on the normalized text); most specific / serious first. Codes are matched with their
# separators removed ("11-80", "11 80" and "1180" are all "1180").
CATEGORIES = {
    "help": "Officer needs help", "collision": "Collision", "fire": "Fire", "pursuit": "Pursuit",
    "crime": "Crime", "dui": "DUI / reckless driver", "medical": "Medical", "hazard": "Road hazard",
    "pedestrian": "Pedestrian", "disabled": "Disabled vehicle", "stop": "Traffic stop", "other": "Other",
}
TYPES: List[Tuple[str, str, str]] = [
    ("help", "Officer needs help", r"\b1199\b|\b999\b|officer needs (?:help|assistance)"),
    ("collision", "Fatal collision", r"\b1144\b|fatal(?:ity)?\b"),
    ("collision", "Collision, major injuries", r"\b1180\b|major injur"),
    ("collision", "Collision, ambulance responding", r"\b1179\b"),
    ("collision", "Collision, minor injuries", r"\b1181\b|minor injur|complaint of pain"),
    ("collision", "Hit and run", r"\b2000[12]\b|hit and run|refused to exchange"),
    ("collision", "Collision, property damage", r"\b1182\b|property damage"),
    ("collision", "Collision", r"\b1183\b|collision|\bcrash|roll ?over|overturned|\bt ?c(?:'d| d)?\b|rear ?end|"
                               r"spun out|into the (?:wall|divider|center divide|guard ?rail)|hit the (?:guard ?rail|wall|divider)|"
                               r"vehicles? (?:blocking|involved)|versus a"),
    ("fire", "Vehicle fire", r"vehicle fire|car fire|(?:car|truck|van|vehicle) (?:is )?on fire|all ?around fire|fully involved"),
    ("fire", "Grass / brush fire", r"(?:grass|brush|vegetation|hill(?:side)?s?) fire|smoke on the hills|fire on the (?:hill|shoulder)"),
    ("fire", "Fire", r"(?<!jose )(?<!county )(?<!city )(?<!1039 )(?<!give )(?<!notify )(?<!advise )(?<!send )(?<!22 )"
                     r"\bfire\b(?! department| dept| engine| units?\b| crew| with| copy| (?:is|are|will be) (?:en ?route|on scene|97))|"
                     r"\bsmoke\b|in flames|flames (?:showing|visible)|engulfed"),
    ("pursuit", "Pursuit", r"\bpursuit|fleeing|failed to yield|failure to yield|evading|\b2800\b"),
    ("crime", "Robbery", r"\b211\b|robbery"),
    ("crime", "Assault / road rage", r"\b245\b|\b242\b|assault|road rage|brandish|threw [a-z ]+ at|\bgun\b|shots? fired"),
    ("crime", "Stolen vehicle", r"\b10851\b|stolen vehicle|\bstolen\b"),
    ("crime", "Burglary / theft", r"\b459\b|\b484\b|\b488\b|burglary|theft|shoplift"),
    ("crime", "Disturbance", r"\b415\b|disturbance|fight\b"),
    ("crime", "Mental health", r"\b5150\b"),
    ("crime", "Suspicious", r"suspicious|panic alarm|\bprowler"),
    ("dui", "DUI", r"\b2315[23q]\b|\bdui\b|drunk|impaired|under the influence"),
    ("dui", "Reckless driver", r"\b23103\b|\b2310\b|reckless|swerving|weaving|unable to maintain|in and out of (?:the )?lanes"),
    ("dui", "Speed contest / sideshow", r"\b23109\b|sideshow|speed contest|street rac"),
    ("dui", "Speeding", r"speeding|over (?:100|a hundred)|high rate of speed"),
    ("medical", "Medical", r"\b1141\b|medical|ambulance|unresponsive|not breathing|\bcpr\b|overdose|\binjur"),
    ("pedestrian", "Pedestrian on the road", r"pedestrian|walking (?:in|on|along) (?:the )?(?:slow |fast |number \w+ )?lane|"
                                             r"person (?:in|on) the (?:road|freeway|shoulder)|lying on the shoulder"),
    ("hazard", "Road hazard", r"\b1125\b|debris|in the (?:slow |fast |middle |number \w+ )?lanes?\b|in the roadway|hazard|ladder|"
                              r"mattress|tire in|\bbox in|wooden plank|baseball bat|animal|dog on|deer|cones?\b|blocking the number"),
    ("disabled", "Disabled / abandoned vehicle", r"\b112[46]\b|disabled|stalled|flat tire|out of gas|broken down|abandoned"),
    ("stop", "Traffic stop", r"\b1195\b|traffic stop|stop made|on a stop\b"),
]
_TYPES = [(c, l, re.compile(p)) for c, l, p in TYPES]

STATUS: List[Tuple[str, str]] = [
    ("cancel", r"\b1022\b|disregard|cancel(?:l?ed)?\b|unable to locate|\bgoa\b|gone on arrival|no (?:sign|vehicle) (?:of|located)"),
    ("clear", r"(?:^|\b(?:be|i'm|im|i am|is|now|show me|you're|youre)\s+)(?:10)?98\b|\b1098\b|code 4|cleared? (?:the|from) (?:scene|call)|"
              r"back in service|\b108\b(?! [a-z]+ (?:street|road))"),
    ("onscene", r"(?:^|\b(?:be|i'm|im|i am|is|now|show me|you're|youre|units? (?:are|is))\s+)(?:10)?97\b|\b1097\b|on scene|"
                r"i(?:'ve| have) got (?:it|visual)|got a visual|visual (?:on|of) the"),
    ("enroute", r"en ?route|in route|\benroute|responding|on my way|heading (?:that way|over)|\b1076\b|attempt to (?:catch up|locate)"),
    ("tow", r"\b1185\b|tow truck|\btow\b|rotation tow"),
    ("ambulance", r"\b1141\b|ambulance|\bems\b|medics?\b"),
    ("fire_dept", r"1039 fire|fire department|fire (?:is )?en ?route|notify fire"),
]
_STATUS = [(s, re.compile(p)) for s, p in STATUS]


def normalize(text: str) -> str:
    """Lower case; separators inside numbers removed ("11-80" -> "1180", "12-3-45" -> "12345")."""
    t = (text or "").lower().replace("’", "'")
    t = re.sub(r"(?<=\d)[- ](?=\d)", "", re.sub(r"(?<=\d)-(?=\d)", "", t))
    t = re.sub(r"(?<=[a-z])-(?=[a-z])", " ", t)
    return " ".join(t.split())


def kind(text: str) -> List[dict]:
    t = " ".join(re.sub(r"[,;:!?]|\.(?!\d)", " ", normalize(text)).split())
    out, seen = [], set()
    for cat, label, rx in _TYPES:
        m = rx.search(t)
        if m and cat not in seen:                    # the most specific label of each category
            seen.add(cat)
            code = m.group(0) if m.group(0).isdigit() else None
            out.append({"cat": cat, "label": label, "code": code})
    return out


def status(text: str) -> List[str]:
    t = normalize(text)
    return [s for s, rx in _STATUS if rx.search(t)]


_UNIT = re.compile(r"^\W*(?:golden gate,? |attention,? )?(\d{2,3}(?:-\d{1,3}){0,3}|\d{4,6})\b(?! units?\b| hours\b| block\b)")


def units(text: str) -> List[str]:
    """The unit a transmission starts with ('12-3-45, ...' -> ['12345']); dispatch addresses a unit the same way.
    Spoken in groups ('12-345-6' = unit 12345, then something else): groups are joined until there are 4 digits."""
    m = _UNIT.match((text or "").lower())
    if not m:
        return []
    u = ""
    for part in m.group(1).split("-"):
        if len(u) >= 4:
            break
        u += part
    if len(u) < 3 or len(u) > 5 or u in ("1097", "1098", "1022", "1185", "1125", "1183", "1039", "1040"):
        return []
    return [u]


# ------------------------------------------------------------------ where

_DIRW = {"north": "N", "south": "S", "east": "E", "west": "W", "nb": "N", "sb": "S", "eb": "E", "wb": "W"}
DIR = r"(?:(north|south|east|west)(?: ?bound)?|\b(nb|sb|eb|wb))"
HWY = r"(?:(?:highway|hwy|route|interstate|state route|us|i|sr)\s+)?(\d{1,3})(?!\d|[-:]\d| hours| units?\b| block)"
REL = (r"(?:just |about |approximately |a (?:quarter|half) mile |a mile )?"
       r"(north of|south of|east of|west of|at|near|approaching|before|after|past|on the|off the|to the|exit(?:ing)?(?: at| to)?|"
       r"on ?ramp (?:from|to|at)|off ?ramp (?:to|at|from))")
STOP = {"for", "it's", "its", "in", "on", "with", "and", "number", "lane", "lanes", "shoulder", "blocking", "a", "an",
        "there", "we", "we're", "i", "i'm", "you", "he", "she", "they", "that", "this", "who", "which", "was", "is",
        "are", "going", "there's", "unit", "units", "vehicle", "regarding", "reference", "area", "direction", "so",
        "right", "left", "then", "break", "copy", "just", "north", "south", "east", "west", "northbound", "southbound",
        "eastbound", "westbound", "to", "of", "at", "by", "from", "near", "be", "will", "has", "have", "had", "per",
        "rp", "party", "parties", "car", "truck", "where", "when", "but", "or", "if", "into", "onto", "over", "under",
        "1097", "1098", "97", "98", "off", "ramp", "fire", "call", "traffic", "log", "info",
        "information", "from", "that's", "pending", "until", "about", "around", "still", "onramp", "offramp", "exit", "interchange", "overcrossing", "et"}
NAME = r"((?:[a-z][a-z'.]*|\d{1,2}(?:st|nd|rd|th))(?: (?:[a-z][a-z'.]*|\d{1,2}(?:st|nd|rd|th))){0,3})"
# (a) "southbound 880 just north of Brokaw"   (b) "Auto Mall Parkway on-ramp to northbound 880"
# (c) "First Street just south of 580"        (d) "880 at Coleman", "101 and McKee"
PAT_A = re.compile(rf"(?:{DIR}\s+)?{HWY}[ ,]+{REL}\s+(?:the )?{NAME}")
PAT_B = re.compile(rf"{NAME}\s+(?:on|off) ?ramp (?:to|onto|from) (?:the )?(?:{DIR}\s+)?{HWY}")
PAT_H = re.compile(rf"(?:{DIR}\s+)?{HWY}[ ,]+{REL}\s+(?:the )?(?:highway |hwy |route |interstate )?(\d{{1,3}})\b(?! hours| units?\b|[-:]\d)")
PAT_C = re.compile(rf"{NAME}\s+(?:just )?(north|south|east|west) of (?:the )?(?:highway |hwy |route |interstate )?(\d{{2,3}})\b(?! hours)")


def _clean_name(raw: str) -> Optional[str]:
    words = []
    for w in raw.split():
        if w in STOP:
            break
        words.append(w)
    while words and words[-1] in ("the",):
        words.pop()
    while words and words[0] in ("the", "of", "to"):
        words.pop(0)
    name = " ".join(words).strip(" .'")
    if not name or len(name) < 3 or name.isdigit():
        return None
    return name


# What transcription makes of highway numbers right after a direction ("North 911", "Northbound Navy")
HEARD_AS = [(r"\b((?:north|south|east|west)(?: ?bound)?) (?:911|9 1 1)\b", r"\1 101"),
            (r"\b((?:north|south|east|west)(?: ?bound)?) (?:navy|nathy|eighty eighty)\b", r"\1 880"),
            (r"\b((?:north|south|east|west)(?: ?bound)?) (?:one oh one|one o one)\b", r"\1 101")]


def _clean_tail(raw: str) -> Optional[str]:
    """The street name a phrase ends with ('a pedestrian near auto mall parkway' -> 'auto mall parkway')."""
    words, raw_w = [], raw.split()
    while raw_w and raw_w[-1] in ("just", "about", "approximately", "right", "the"):
        raw_w.pop()
    for w in reversed(raw_w):
        if w in STOP or w in (",", ".", "the"):
            break
        words.insert(0, w)
    return _clean_name(" ".join(words[-4:]))


def where(text: str) -> List[dict]:
    """Location phrases in one transmission."""
    t = normalize(text)
    for wrong, right in FIXES.items():
        t = t.replace(wrong, right)
    for a, b in HEARD_AS:
        t = re.sub(a, b, t)
    t = re.sub(r"[,;:!?]", " , ", t).replace(".", " . ")
    t = " ".join(t.split())
    out = []

    def add(p):
        if not any(q.get("hwy") == p.get("hwy") and q.get("cross") == p.get("cross") and q["kind"] == p["kind"] for q in out):
            out.append(p)

    for m in PAT_A.finditer(t):
        d = _DIRW.get(m.group(1) or m.group(2) or "", "")
        rel = m.group(4)
        if rel in ("on the", "to the") and not re.search(r"(?:off|on) ?ramp|exit|interchange|overcrossing",
                                                                     t[m.end():m.end() + 30]):
            continue
        name = _clean_name(m.group(5))
        if name:
            add({"kind": "junction", "hwy": m.group(3), "dir": d, "rel": rel, "cross": name, "text": m.group(0)})
    for m in PAT_H.finditer(t):                       # "880 just south of the 101"
        if m.group(5) != m.group(3) and m.group(4) not in ("on the", "to the"):
            add({"kind": "junction", "hwy": m.group(3), "dir": _DIRW.get(m.group(1) or m.group(2) or "", ""),
                 "rel": m.group(4), "cross": f"highway {m.group(5)}", "text": m.group(0)})
    for m in PAT_B.finditer(t):
        name = _clean_tail(m.group(1))
        if name:
            add({"kind": "junction", "hwy": m.group(4), "dir": _DIRW.get(m.group(2) or m.group(3) or "", ""),
                 "rel": "ramp", "cross": name, "text": m.group(0)})
    for m in PAT_C.finditer(t):
        name = _clean_tail(m.group(1))
        if name and not re.fullmatch(r"(?:north|south|east|west)(?:bound)?", name):
            add({"kind": "junction", "hwy": m.group(3), "dir": "", "rel": m.group(2) + " of", "cross": name,
                 "text": m.group(0)})
    for m in CITY_RE.finditer(t):
        name = m.group(1).lower()
        if not any(p["kind"] == "city" and p["name"] == name for p in out):
            out.append({"kind": "city", "name": name, "text": m.group(0)})
    return out


# The area each channel covers, for choosing between same-named streets (approximate centres)
CHANNEL_AREA = {42500: (37.33, -121.89), 42502: (37.33, -121.89), 42280: (37.33, -121.89),   # CHP San Jose
                44942: (37.62, -121.98), 42620: (37.65, -122.05),                            # CHP Hayward / Dublin
                42080: (37.55, -122.30), 42082: (37.55, -122.30)}                            # CHP Redwood City
BAY = (37.55, -122.05)


def area(freq_hz: float) -> Tuple[float, float]:
    return CHANNEL_AREA.get(int(round((freq_hz or 0) / 1000)), BAY)


def locate(text: str, jx: Junctions, freq_hz: float = 0, near: Optional[Tuple[float, float]] = None) -> List[dict]:
    """Location phrases placed: [{kind, label, lat, lon, radius_km, hwy?, dir?, street?, corrected?}].
    near: where the call is probably about (the unit's current incident), for "80" = 280 or 880?"""
    home = area(freq_hz)
    near = near or home
    out = []
    phrases = where(text)
    for p in phrases:
        if p["kind"] == "junction":
            hit = jx.find(p["hwy"], p["cross"], near) if jx else None
            if not hit or hit["score"] < 0.5 or km(home, (hit["lat"], hit["lon"])) > 70:
                continue
            r = 0.4 if p["rel"] in ("at", "ramp", "on the", "off the", "to the") else 1.0
            if hit["score"] < 0.8:
                r += 0.8
            d = {"N": "northbound", "S": "southbound", "E": "eastbound", "W": "westbound"}.get(p["dir"], "")
            label = f"{d + ' ' if d else ''}{hit['hwy']} {p['rel'] if p['rel'] != 'ramp' else 'ramp at'} {hit['street']}"
            out.append({"kind": "junction", "label": label, "lat": hit["lat"], "lon": hit["lon"], "radius_km": r,
                        "hwy": hit["hwy"], "dir": p["dir"], "street": hit["street"], "heard": p["text"],
                        "corrected": hit["corrected"], "score": hit["score"]})
        else:
            lat, lon = CITIES[p["name"]]
            if km(home, (lat, lon)) > 45:
                continue
            out.append({"kind": "city", "label": p["name"].title(), "lat": lat, "lon": lon, "radius_km": 3.0,
                        "heard": p["text"]})
    return out


def analyze(rec: dict, jx: Junctions, near: Optional[Tuple[float, float]] = None) -> dict:
    """One transmission: types, statuses, unit, places (both transcript readings)."""
    # Types and statuses from the plain reading only: the vocabulary-hinted one invents calls ("Oh." -> "BOLO,
    # ambulance, fire."; "10-4" -> "11-4-4", a fatality). It may still place a call the plain one could not.
    text = rec.get("text") or ""
    kinds = kind(text)
    places = locate(rec.get("text") or "", jx, rec.get("freq_hz") or 0, near)
    if not places and rec.get("text_atc"):
        places = locate(rec["text_atc"], jx, rec.get("freq_hz") or 0, near)
    return {"kinds": kinds, "status": status(text), "units": units(rec.get("text") or ""), "places": places}


# ------------------------------------------------------------------ incidents: transmissions grouped, with a timeline

# Channels that share a dispatcher (a call and its follow-ups can be on either)
CHANNEL_GROUP = {42500: "chp-sj", 42502: "chp-sj", 42280: "chp-sj",
                 44942: "chp-east", 42620: "chp-east",
                 42080: "chp-rwc", 42082: "chp-rwc"}
SERIOUS = ["help", "collision", "fire", "pursuit", "crime", "dui", "medical", "pedestrian", "hazard", "disabled",
           "stop", "other"]
SAME_KM = 2.0            # a place this close to an open incident's is the same incident
QUIET_S = 90 * 60        # an incident nobody has mentioned for this long is over
UNIT_S = 60 * 60         # a unit's transmissions belong to its latest incident for this long


def group_of(freq_hz) -> str:
    k = int(round((freq_hz or 0) / 1000))
    return CHANNEL_GROUP.get(k, str(k))


# What is never shown in the incident view: plates, phone numbers, house addresses, names
_PHON = ("adam|boy|charles|david|edward|frank|george|henry|ida|john|king|lincoln|mary|nora|ocean|paul|queen|robert|"
         "sam|tom|union|victor|william|x-?ray|young|zebra|alpha|bravo|charlie|delta|echo|foxtrot|golf|hotel|india|"
         "juliet|kilo|lima|mike|november|oscar|papa|quebec|romeo|sierra|tango|uniform|whiskey|yankee|zulu|yellow")
_TOK = rf"(?:\d{{1,4}}|(?:{_PHON})\b)"
REDACT = [
    (re.compile(rf"\b{_TOK}(?:[ ,-]+{_TOK}){{3,}}\b", re.I), "[plate]", lambda m: re.search(_PHON, m.group(0), re.I)),
    (re.compile(r"\b[\dA-Za-z]+(?:-[\dA-Za-z]+){3,}\b"), "[plate]",
     lambda m: re.search(r"(?:^|-)[A-Za-z]{3,}(?:-|$)", m.group(0)) and re.search(r"\d", m.group(0))),
    (re.compile(r"\b\d[A-Z]{3}\d{3}\b|\b\d{1,2}[A-Z]{2,3}\d{2,4}\b"), "[plate]", None),
    (re.compile(r"\b\d{3}[- .]\d{4}\b"), "[number]", None),
    (re.compile(r"\b(?:plate )?(?:ending|beginning|starting) (?:with|of|in|is) (?:an? )?[\dA-Za-z-]*\d[\dA-Za-z-]*", re.I),
     "[partial plate]", None),
    (re.compile(r"\b\d[\d-]* (?:block of )?(?:[A-Z][a-z]+ ){1,3}(?:Street|St|Avenue|Ave|Court|Ct|Drive|Dr|Road|Rd|Way|"
                r"Lane|Ln|Place|Pl|Boulevard|Blvd|Circle|Terrace|Parkway)\b"), "[address]", None),
    (re.compile(r"\b(?:[A-Z][a-z]+ )?[A-Z][a-z]+(?= (?:is |was )?booked\b)"), "[name]", None),
    (re.compile(r"\b(named|subject|driver is|owner is|party is|identified as|last name|first name|RO is|RO|Officer|"
                r"Sergeant|Sgt\.|Mr\.|Ms\.|Mrs\.) [A-Z][a-z]+(?: [A-Z][a-z]+)?"), r"\1 [name]", None),
]


# A records check's reply is nearly all personal detail (name, date of birth, licence, plate, probation): its
# text is not shown at all
RECORDS = re.compile(r"\b(?:10|1 0)[- ]?2[789]\b|\b2[789] (?:returns|is clear|on|turn)|returns? (?:clear|to|valid|expired)|"
                     r"registration (?:valid|expired|current)|clear and current|license (?:status|is|valid|suspended)|"
                     r"\bprobation\b|\bparole\b|warrants?\b|\bftas?\b|\bdob\b|date of birth|last (?:name )?of\b|"
                     r"first (?:name )?of\b|\bsoundex\b|\bcriminal (?:history|offense)|\bpriors\b|\bbooked\b|blood alcohol|"
                     r"out of [A-Z][a-z]+ \d", re.I)
_MIXED = re.compile(r"\b(?=[\dA-Z-]*\d)(?=[\dA-Z-]*[A-Z]{2})[\dA-Z]+(?:-[\dA-Z]+)*\b")    # e.g. 123456ABC, 12345A-XYZ


def redact(text: str) -> str:
    t = text or ""
    if RECORDS.search(t):
        return "(records check: not shown)"
    t = _MIXED.sub(lambda m: "[plate]" if len(m.group(0)) >= 5 else m.group(0), t)
    for rx, sub, check in REDACT:
        t = rx.sub(lambda m: m.expand(sub) if (check is None or check(m)) else m.group(0), t)
    return t


def _new(rec, group, n):
    return {"id": f"{group}-{int(rec['t0'])}-{n}", "group": group, "cat": None, "label": None, "kinds": [],
            "code": None, "place": None, "track": [], "t0": rec["t0"], "t1": rec["t0"], "units": [], "freqs": [],
            "state": "open", "closed_by": None, "status": [], "entries": []}


def _better_place(a, b):
    """The more precise of two places."""
    if not a:
        return b
    rank = lambda p: (p["kind"] == "junction", p.get("score", 0), -p["radius_km"])
    return b if rank(b) > rank(a) else a


def build(recs: List[dict], jx: Junctions, now: Optional[float] = None) -> List[dict]:
    """Group a session's transmissions (in time order) into incidents. Each incident: its type (the most serious
    seen), best place and track, units, state (open / closed: cleared, cancelled or quiet) and a timeline of the
    transmissions that belong to it (text with plates, numbers, addresses and names taken out)."""
    out: List[dict] = []
    unit_inc: Dict[Tuple[str, str], dict] = {}
    for rec in sorted(recs, key=lambda r: r["t0"]):
        if rec.get("kind") == "digital" or not (rec.get("text") or "").strip():
            continue
        t, g = rec["t0"], group_of(rec.get("freq_hz"))
        for inc in out:                                  # quiet ones are over
            if inc["state"] == "open" and t - inc["t1"] > QUIET_S:
                inc["state"], inc["closed_by"] = "closed", "quiet"
        u = units(rec.get("text") or "")
        linked = None
        if u:
            cand = unit_inc.get((g, u[0]))
            if cand and cand["state"] == "open" and t - cand["t1"] < UNIT_S:
                linked = cand
        near = (linked["place"]["lat"], linked["place"]["lon"]) if linked and linked["place"] else None
        a = analyze(rec, jx, near)
        kinds, st, places = a["kinds"], a["status"], a["places"]
        junction = [p for p in places if p["kind"] == "junction"]
        where = (junction or places or [None])[0]
        inc = None
        if where:
            best = None
            for c in out:
                if c["group"] != g or c["state"] != "open" or not c["place"]:
                    continue
                d = km((c["place"]["lat"], c["place"]["lon"]), (where["lat"], where["lon"]))
                if d <= max(SAME_KM, c["place"]["radius_km"] + where["radius_km"]) and (best is None or d < best[0]):
                    best = (d, c)
            if best:
                inc = best[1]
            elif linked and not linked["place"] and (not kinds or not linked["cat"]
                                                     or kinds[0]["cat"] in (linked["cat"], "other")):
                inc = linked                             # the unit's call gets its location
            elif kinds or junction or st:
                inc = None                               # somewhere new: a new incident
            else:
                continue                                 # just a town named in passing
        elif kinds:
            if linked:
                inc = linked
            else:
                for c in reversed(out):                  # the same kind of call just broadcast on this system
                    if c["group"] == g and c["state"] == "open" and c["cat"] == kinds[0]["cat"] and t - c["t1"] < 20 * 60:
                        inc = c
                        break
        elif linked and (st or u):
            inc = linked
        else:
            continue
        if inc is None:
            inc = _new(rec, g, len(out))
            out.append(inc)
        # fold this transmission in
        inc["t1"] = max(inc["t1"], rec.get("t1") or t)
        for k in kinds:
            if k["label"] not in inc["kinds"]:
                inc["kinds"].append(k["label"])
            if inc["cat"] is None or SERIOUS.index(k["cat"]) < SERIOUS.index(inc["cat"]):
                inc["cat"], inc["label"], inc["code"] = k["cat"], k["label"], k["code"]
        if where:
            if not inc["track"] or km((inc["track"][-1]["lat"], inc["track"][-1]["lon"]), (where["lat"], where["lon"])) > 0.5:
                inc["track"].append({"t": t, "lat": where["lat"], "lon": where["lon"], "label": where["label"]})
            inc["place"] = _better_place(inc["place"], where)
        for x in u:
            if x not in inc["units"]:
                inc["units"].append(x)
            unit_inc[(g, x)] = inc
        f = int(round(rec.get("freq_hz") or 0))
        if f not in inc["freqs"]:
            inc["freqs"].append(f)
        for s_ in st:
            if s_ not in inc["status"]:
                inc["status"].append(s_)
        inc["entries"].append({"i": rec.get("i"), "t0": t, "t1": rec.get("t1"), "freq_hz": rec.get("freq_hz"),
                               "clip": rec.get("clip"), "unit": u[0] if u else None, "status": st,
                               "kinds": [k["label"] for k in kinds], "place": where and where["label"],
                               "text": redact(rec.get("text") or "")})
        if "cancel" in st and inc["state"] == "open":
            inc["state"], inc["closed_by"] = "closed", "cancelled"
        elif "clear" in st and inc["state"] == "open" and len(inc["entries"]) > 1:
            inc["state"], inc["closed_by"] = "closed", "cleared"
    now = now if now is not None else (max((r["t0"] for r in recs), default=0))
    for inc in out:
        if inc["state"] == "open" and now - inc["t1"] > QUIET_S:
            inc["state"], inc["closed_by"] = "closed", "quiet"
        inc["cat"] = inc["cat"] or "other"
        inc["label"] = inc["label"] or "Call"
        inc["n"] = len(inc["entries"])
        if inc["place"]:
            inc["place"] = {k: inc["place"][k] for k in ("kind", "label", "lat", "lon", "radius_km") if k in inc["place"]}
    # a lone mention with no type and only a town is not an incident
    return [i for i in out if i["kinds"] or (i["place"] and i["place"]["kind"] == "junction")]
