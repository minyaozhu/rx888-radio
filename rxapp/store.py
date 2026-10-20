"""SQLite log: scans and their detections, recordings, health samples, events."""

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
  id INTEGER PRIMARY KEY, t REAL NOT NULL, band TEXT NOT NULL,
  start_hz REAL, stop_hz REAL, step_hz REAL, bin_hz REAL, frames INTEGER, floor_db REAL);
CREATE TABLE IF NOT EXISTS detections (
  scan_id INTEGER NOT NULL REFERENCES scans(id), freq_hz REAL NOT NULL, snr_db REAL NOT NULL);
CREATE INDEX IF NOT EXISTS det_scan ON detections(scan_id);
CREATE TABLE IF NOT EXISTS recordings (
  id INTEGER PRIMARY KEY, freq_hz REAL, mode TEXT, label TEXT, path TEXT,
  start REAL, stop REAL, seconds REAL, peak_dbfs REAL);
CREATE TABLE IF NOT EXISTS health (
  t REAL NOT NULL, if_power_db REAL, rf_gain_db REAL, ad_over INTEGER,
  input_samples INTEGER, spectrum_fps REAL, channels INTEGER);
CREATE TABLE IF NOT EXISTS events (t REAL NOT NULL, kind TEXT, detail TEXT);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(SCHEMA)

    def _exec(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    def query(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def event(self, kind, **detail):
        self._exec("INSERT INTO events VALUES (?,?,?)", (time.time(), kind, json.dumps(detail)))

    def add_scan(self, band, start, stop, step, bin_hz, frames, floor, hits):
        cur = self._exec("INSERT INTO scans (t,band,start_hz,stop_hz,step_hz,bin_hz,frames,floor_db) "
                         "VALUES (?,?,?,?,?,?,?,?)", (time.time(), band, start, stop, step, bin_hz, frames, floor))
        sid = cur.lastrowid
        with self.lock:
            self.db.executemany("INSERT INTO detections VALUES (?,?,?)", [(sid, f, s) for f, s in hits])
            self.db.commit()
        return sid

    def add_recording(self, **r):
        cols = ",".join(r)
        return self._exec(f"INSERT INTO recordings ({cols}) VALUES ({','.join('?' * len(r))})",
                          tuple(r.values())).lastrowid

    def finish_recording(self, rid, stop, seconds, peak):
        self._exec("UPDATE recordings SET stop=?, seconds=?, peak_dbfs=? WHERE id=?", (stop, seconds, peak, rid))

    def add_health(self, **h):
        self._exec("INSERT INTO health (t,if_power_db,rf_gain_db,ad_over,input_samples,spectrum_fps,channels) "
                   "VALUES (:t,:if_power_db,:rf_gain_db,:ad_over,:input_samples,:spectrum_fps,:channels)", h)
