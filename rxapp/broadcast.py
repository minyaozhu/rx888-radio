"""AM / FM / shortwave broadcast: Bay Area station names, and the EiBi shortwave schedule.

AM and FM stations from Wikipedia's "List of radio stations in the San Francisco Bay Area" (October 2026).
Shortwave: the EiBi schedule (eibispace.de, free; seasons A = summer, B = winter), as CSV in
rxapp_data/eibi/sked-<season>.csv; on_air() says who is scheduled on a frequency at a given UTC time.
"""

import csv
import datetime as dt
import logging
import re
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

AM = [(610, "KEAR", "San Francisco", "Family Radio"), (680, "KNBR", "San Francisco", "Sports"),
      (740, "KCBS", "San Francisco", "All-news"), (810, "KSFO", "San Francisco", "Conservative talk"),
      (860, "KTRB", "San Francisco", "Conservative talk"), (910, "KKSF", "Oakland", "Black Information Network"),
      (960, "KNEW", "Oakland", "Sports and conservative talk"), (1010, "KIQI", "San Francisco", "Spanish talk/brokered"),
      (1050, "KTCT", "San Mateo", "Sports"), (1100, "KFAX", "San Francisco", "Christian"),
      (1170, "KLOK", "San Jose", "Punjabi"), (1190, "KDYA", "Vallejo", "Gospel"),
      (1220, "KDOW", "Palo Alto", "Business news"), (1260, "KSFB", "San Francisco", "Relevant Radio"),
      (1310, "KMKY", "Oakland", "Hindi/Punjabi/South Asian"), (1370, "KZSF", "San Jose", "Regional Mexican"),
      (1400, "KVTO", "Berkeley", "Sing Tao Chinese Radio"), (1430, "KVVN", "Santa Clara", "Vietnamese"),
      (1450, "KEST", "San Francisco", "Multilingual / New Age talk"), (1500, "KSJX", "San Jose", "Vietnamese"),
      (1510, "KSFN", "Piedmont", "Regional Mexican"), (1550, "KZDG", "San Francisco", "Radio Zindagi"),
      (1590, "KLIV", "San Jose", "Vietnamese"), (1640, "KDIA", "Vallejo", "Christian")]
FM = [(87.7, "KBKF-LD", "San Jose", "Air1"), (88.1, "KECG", "El Cerrito", "Campus/variety"),
      (88.5, "KQED-FM", "San Francisco", "NPR/talk"), (89.1, "KCEA", "Atherton", "Campus/adult standards"),
      (89.3, "KPFB", "Berkeley", "Pacifica Radio"), (89.5, "KPOO", "San Francisco", "Community/variety"),
      (89.7, "KFJC", "Los Altos", "College/variety"), (89.9, "KCRH", "Hayward", "College/variety"),
      (90.1, "KZSU", "Stanford", "College/variety"), (90.3, "KDFC", "San Francisco", "Classical"),
      (90.5, "KSJS", "San Jose", "College/variety"), (90.7, "KALX", "Berkeley", "College/variety"),
      (91.1, "KCSM", "San Mateo", "Jazz"), (91.5, "KKUP", "Cupertino", "Community/variety"),
      (91.7, "KALW", "San Francisco", "NPR/talk/variety"), (92.1, "KKDV", "Walnut Creek", "Country"),
      (92.3, "KSJO", "San Jose", "Bollywood music"), (92.7, "KEXC", "San Francisco", "Variety"),
      (93.3, "KRZZ", "San Francisco", "Regional Mexican"), (93.7, "KXZM", "Felton", "Regional Mexican"),
      (94.1, "KPFA", "Berkeley", "Pacifica Radio"), (94.5, "KBAY", "Gilroy", "Country"),
      (94.9, "KYLD", "San Francisco", "Contemporary hits"), (95.3, "KJLV", "San Jose", "K-Love"),
      (95.7, "KGMZ-FM", "San Francisco", "Sports"), (96.1, "KSQQ", "Morgan Hill", "Sing Tao Chinese Radio"),
      (96.5, "KOIT", "San Francisco", "Adult contemporary"), (97.3, "KLLC", "San Francisco", "Hot AC"),
      (97.7, "KWAI", "Los Altos", "Air1"), (98.1, "KISQ", "San Francisco", "Soft AC"),
      (98.5, "KUFX", "San Jose", "Country"), (98.9, "KSOL", "San Francisco", "Regional Mexican"),
      (99.1, "KSQL", "Santa Cruz", "Regional Mexican"), (99.7, "KMVQ-FM", "San Francisco", "Contemporary hits"),
      (100.3, "KBRG", "San Jose", "Spanish AC"), (100.7, "KVVZ", "San Rafael", "Reggaeton"),
      (101.3, "KIOI", "San Francisco", "Hot AC"), (101.7, "KKIQ", "Livermore", "Adult contemporary"),
      (102.1, "KRBQ", "San Francisco", "Classic hip hop"), (102.5, "KSFP-LP", "San Francisco", "Public radio/talk"),
      (102.9, "KBLX-FM", "Berkeley", "Urban AC"), (103.3, "KSCU", "Santa Clara", "College/variety"),
      (103.7, "KOSF", "San Francisco", "Classic hits"), (104.5, "KNBR-FM", "San Francisco", "Sports"),
      (104.9, "KXSC", "Sunnyvale", "Classical"), (105.3, "KITS", "San Francisco", "Alternative rock"),
      (105.7, "KVVF", "Santa Clara", "Reggaeton"), (106.1, "KMEL", "San Francisco", "Urban contemporary"),
      (106.5, "KEZR", "San Jose", "Hot AC"), (106.9, "KFRC-FM", "San Francisco", "All-news"),
      (107.3, "KLVS", "Livermore", "K-Love"), (107.7, "KSAN", "San Mateo", "Classic rock")]


def stations():
    return {"am": [{"freq_hz": k * 1e3, "call": c, "city": city, "format": f} for k, c, city, f in AM],
            "fm": [{"freq_hz": round(m * 1e6), "call": c, "city": city, "format": f} for m, c, city, f in FM],
            "source": "Wikipedia, List of radio stations in the San Francisco Bay Area"}


# ---- EiBi --------------------------------------------------------------------------------
EIBI_URL = "http://www.eibispace.de/dx/sked-{season}.csv"
LANG = {"E": "English", "S": "Spanish", "F": "French", "A": "Arabic", "M": "Mandarin", "C": "Chinese", "R": "Russian",
        "G": "German", "P": "Portuguese", "J": "Japanese", "K": "Korean", "I": "Italian", "T": "Thai", "VN": "Vietnamese",
        "HI": "Hindi", "IN": "Indonesian", "TU": "Turkish", "FA": "Farsi", "UR": "Urdu", "BE": "Bengali", "SWA": "Swahili",
        "HA": "Hausa", "AM": "Amharic", "CA": "Cantonese", "TB": "Tibetan", "UI": "Uighur", "KH": "Khmer", "BU": "Burmese",
        "TAG": "Tagalog", "PS": "Pashto", "DR": "Dari", "SO": "Somali", "UK": "Ukrainian", "PO": "Polish", "RO": "Romanian",
        "-TS": "time signal", "-CW": "Morse", "-MX": "music", "MUL": "multilingual"}
TARGET = {"NAm": "North America", "CNA": "Central North America", "WNA": "Western North America", "ENA": "Eastern North America",
          "LAm": "Latin America", "CAm": "Central America", "SAm": "South America", "Eu": "Europe", "ME": "Middle East",
          "Af": "Africa", "NAf": "North Africa", "WAf": "West Africa", "EAf": "East Africa", "SAf": "Southern Africa",
          "As": "Asia", "EAs": "East Asia", "SAs": "South Asia", "SEA": "Southeast Asia", "CAs": "Central Asia",
          "Oc": "Oceania", "Pac": "Pacific", "FE": "Far East", "Car": "Caribbean", "Ant": "Antarctica"}
DAYS = {"Mo": 1, "Tu": 2, "We": 3, "Th": 4, "Fr": 5, "Sa": 6, "Su": 7}


def season(today: Optional[dt.date] = None) -> str:
    """A (summer): last Sunday of March to the last Sunday of October; B (winter) otherwise. E.g. a26, b26."""
    d = today or dt.datetime.utcnow().date()
    def last_sunday(y, m):
        x = dt.date(y, m, 31)
        return x - dt.timedelta(days=(x.weekday() + 1) % 7)
    if last_sunday(d.year, 3) <= d < last_sunday(d.year, 10):
        return f"a{d.year % 100:02d}"
    yr = d.year if d >= last_sunday(d.year, 10) else d.year - 1
    return f"b{yr % 100:02d}"


def _days(s: str):
    """EiBi day field -> set of ISO weekdays, or None for every day. '1245', 'Mo-Fr', 'Sa,Su', 'irr'."""
    s = (s or "").strip()
    if not s:
        return None
    if re.fullmatch(r"[1-7]+", s):
        return {int(c) for c in s}
    out = set()
    for part in s.split(","):
        m = re.fullmatch(r"(Mo|Tu|We|Th|Fr|Sa|Su)(?:-(Mo|Tu|We|Th|Fr|Sa|Su))?", part.strip())
        if not m:
            return None                       # irregular etc.: treat as possible any day
        a, b = DAYS[m[1]], DAYS[m[2] or m[1]]
        out |= set(range(a, b + 1)) if a <= b else set(range(a, 8)) | set(range(1, b + 1))
    return out


class Eibi:
    def __init__(self, folder: Path):
        self.folder = folder
        self.entries: List[dict] = []
        self.file: Optional[str] = None
        self.load()

    def fetch(self, s: str) -> bool:
        self.folder.mkdir(parents=True, exist_ok=True)
        try:
            req = urllib.request.Request(EIBI_URL.format(season=s), headers={"User-Agent": "rx888-toolkit"})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
            if b"kHz" not in data[:200]:
                return False
            (self.folder / f"sked-{s}.csv").write_bytes(data)
            return True
        except Exception as e:
            log.info("EiBi %s: %s", s, e)
            return False

    def load(self):
        want = season()
        path = self.folder / f"sked-{want}.csv"
        if not path.exists():
            self.fetch(want)
        files = sorted(self.folder.glob("sked-*.csv"), key=lambda p: p.stat().st_mtime)
        if path.exists():
            files.append(path)
        if not files:
            return
        path = files[-1]
        rows = []
        with open(path, encoding="latin-1", newline="") as f:
            for r in csv.reader(f, delimiter=";"):
                if len(r) < 7 or not re.fullmatch(r"\d+(\.\d+)?", r[0].strip()):
                    continue
                tm = re.fullmatch(r"(\d\d)(\d\d)-(\d\d)(\d\d)", r[1].strip())
                if not tm:
                    continue
                start = int(tm[1]) * 60 + int(tm[2])
                end = int(tm[3]) * 60 + int(tm[4])
                rows.append({"khz": float(r[0]), "start": start, "end": end, "days": _days(r[2]), "itu": r[3].strip(),
                             "station": r[4].strip(), "lang": r[5].strip(), "language": LANG.get(r[5].strip(), r[5].strip()),
                             "target": r[6].strip(), "target_name": TARGET.get(r[6].strip(), r[6].strip()),
                             "remarks": r[7].strip() if len(r) > 7 else "", "time": r[1].strip(), "day_text": r[2].strip()})
        self.entries, self.file = rows, path.name
        log.info("EiBi: %d entries from %s", len(rows), path.name)

    def on_air(self, khz_lo=2300, khz_hi=26100, when: Optional[float] = None) -> List[dict]:
        t = dt.datetime.utcfromtimestamp(when or time.time())
        m, wd = t.hour * 60 + t.minute, t.isoweekday()
        out = []
        for e in self.entries:
            if not khz_lo <= e["khz"] <= khz_hi:
                continue
            s, en = e["start"], e["end"]
            on = (s <= m < en) if s < en else (m >= s or m < en)          # e.g. 2300-0100 runs past midnight
            if en == 2400 and s == 0:
                on = True
            if on and (e["days"] is None or wd in e["days"]):
                out.append({k: v for k, v in e.items() if k != "days"})
        return out
