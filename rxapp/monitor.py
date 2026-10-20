"""Unattended monitoring, run by the server (no browser needed).

- Scan schedules: scan a band every N minutes; results land in SQLite for the dashboard.
- Watches: keep a channel open and record whenever its SNR rises above a threshold
  (a squelch-triggered recorder), optionally transcribing each recording on-device with
  the Malahit toolkit's Transcribe.app (macOS Speech framework).
"""

import asyncio
import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

from .config import ROOT
from .jobs import BANDS, Recorder

log = logging.getLogger(__name__)

TRANSCRIBE_APP = ROOT.parent / "Malahit" / "tools" / "Transcribe.app"
OPEN_HOLD_SEC = 1.0       # SNR must stay up this long to start recording
HANG_SEC = 4.0            # keep recording this long after the signal drops
MIN_RECORDING_SEC = 2.0   # shorter than this is a blip; discarded

SCHEMA = """
CREATE TABLE IF NOT EXISTS schedules (
  id INTEGER PRIMARY KEY, band TEXT NOT NULL, every_min REAL NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1, last_run REAL);
CREATE TABLE IF NOT EXISTS watches (
  id INTEGER PRIMARY KEY, freq_hz REAL NOT NULL, mode TEXT NOT NULL, label TEXT,
  snr_db REAL NOT NULL DEFAULT 10, transcribe INTEGER NOT NULL DEFAULT 0,
  enabled INTEGER NOT NULL DEFAULT 1);
"""


@dataclass
class WatchState:
    row: dict
    ssrc: Optional[int] = None
    above_since: Optional[float] = None
    below_since: Optional[float] = None
    recorder: Optional[Recorder] = None
    snr: Optional[float] = None
    recordings: int = 0


class Monitor:
    def __init__(self, radio, rtp, scanner, store):
        self.radio, self.rtp, self.scanner, self.store = radio, rtp, scanner, store
        with store.lock:
            store.db.executescript(SCHEMA)
            cols = {r[1] for r in store.db.execute("PRAGMA table_info(recordings)")}
            if "transcript" not in cols:
                store.db.execute("ALTER TABLE recordings ADD COLUMN transcript TEXT")
            if "watch_id" not in cols:
                store.db.execute("ALTER TABLE recordings ADD COLUMN watch_id INTEGER")
            store.db.commit()
        self.watches: Dict[int, WatchState] = {}
        self._tasks = []

    # -- lifecycle --------------------------------------------------------
    async def start(self):
        for row in self.store.query("SELECT * FROM watches WHERE enabled = 1"):
            await self._open_watch(row)
        self._tasks = [asyncio.create_task(self._schedule_loop()), asyncio.create_task(self._watch_loop())]

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        for w in self.watches.values():
            if w.recorder:
                self._finish(w)

    # -- scan schedules -----------------------------------------------------
    def schedules(self):
        return self.store.query("SELECT * FROM schedules ORDER BY id")

    def add_schedule(self, band, every_min):
        if band not in BANDS:
            raise ValueError(f"unknown band {band}")
        return self.store._exec("INSERT INTO schedules (band, every_min) VALUES (?,?)", (band, every_min)).lastrowid

    def delete_schedule(self, sid):
        self.store._exec("DELETE FROM schedules WHERE id = ?", (sid,))

    async def _schedule_loop(self):
        while True:
            await asyncio.sleep(5)
            now = time.time()
            for s in self.schedules():
                if not s["enabled"] or (s["last_run"] and now - s["last_run"] < s["every_min"] * 60):
                    continue
                self.store._exec("UPDATE schedules SET last_run = ? WHERE id = ?", (now, s["id"]))
                try:
                    r = await self.scanner.scan(s["band"])
                    log.info("scheduled scan %s: %d signals", s["band"], len(r["hits"]))
                except Exception as e:
                    log.warning("scheduled scan %s failed: %s", s["band"], e)
                    self.store.event("scan_failed", band=s["band"], error=str(e))

    # -- watches ------------------------------------------------------------
    def watch_list(self):
        out = []
        for row in self.store.query("SELECT * FROM watches ORDER BY id"):
            w = self.watches.get(row["id"])
            out.append(row | {"ssrc": w.ssrc if w else None, "snr_now": w.snr if w else None,
                              "recording": bool(w and w.recorder), "recordings_this_run": w.recordings if w else 0})
        return out

    async def add_watch(self, freq_hz, mode, label="", snr_db=10.0, transcribe=False):
        wid = self.store._exec("INSERT INTO watches (freq_hz, mode, label, snr_db, transcribe) VALUES (?,?,?,?,?)",
                               (freq_hz, mode, label, snr_db, int(transcribe))).lastrowid
        row = self.store.query("SELECT * FROM watches WHERE id = ?", (wid,))[0]
        await self._open_watch(row)
        return wid

    async def delete_watch(self, wid):
        w = self.watches.pop(wid, None)
        if w:
            if w.recorder:
                self._finish(w)
            if w.ssrc:
                await self.radio.remove(w.ssrc)
        self.store._exec("DELETE FROM watches WHERE id = ?", (wid,))

    async def _open_watch(self, row):
        ch = await self.radio.create(row["freq_hz"], row["mode"], row["label"] or f"watch {row['id']}", kind="watch")
        self.watches[row["id"]] = WatchState(row=row, ssrc=ch.ssrc)
        log.info("watching %.3f kHz %s (SNR >= %.0f dB)", row["freq_hz"] / 1e3, row["mode"], row["snr_db"])

    async def _watch_loop(self):
        while True:
            await asyncio.sleep(0.5)
            now = time.time()
            for w in list(self.watches.values()):
                ch = await self.radio.refresh(w.ssrc)
                if ch is None:          # radiod restarted and the supervisor hasn't restored yet
                    continue
                w.snr = ch.snr_db
                up = ch.snr_db is not None and ch.snr_db >= w.row["snr_db"]
                if up:
                    w.below_since = None
                    w.above_since = w.above_since or now
                    if not w.recorder and now - w.above_since >= OPEN_HOLD_SEC:
                        w.recorder = Recorder(self.rtp, self.store, ch)
                        log.info("watch %d: signal (SNR %.1f dB), recording", w.row["id"], ch.snr_db)
                else:
                    w.above_since = None
                    if w.recorder:
                        w.below_since = w.below_since or now
                        if now - w.below_since >= HANG_SEC:
                            self._finish(w)

    def _finish(self, w: WatchState):
        rec, w.recorder, w.below_since = w.recorder, None, None
        info = rec.stop()
        self.store._exec("UPDATE recordings SET watch_id = ? WHERE id = ?", (w.row["id"], info["id"]))
        if info["seconds"] < MIN_RECORDING_SEC:
            Path(info["path"]).unlink(missing_ok=True)
            self.store._exec("DELETE FROM recordings WHERE id = ?", (info["id"],))
            return
        w.recordings += 1
        self.store.event("watch_recording", watch=w.row["id"], seconds=info["seconds"], path=info["path"])
        if w.row["transcribe"]:
            asyncio.create_task(self._transcribe(info["id"], info["path"]))

    async def _transcribe(self, rid, wav):
        if not TRANSCRIBE_APP.exists():
            log.warning("transcription skipped: %s not built", TRANSCRIBE_APP)
            return
        out = Path(wav).with_suffix(".txt")
        proc = await asyncio.create_subprocess_exec(
            "open", "-W", "-n", str(TRANSCRIBE_APP), "--args", wav, str(out), "en-US",
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            await asyncio.wait_for(proc.wait(), 180)
        except asyncio.TimeoutError:
            proc.kill()
            return
        if out.exists():
            text = out.read_text().strip()
            self.store._exec("UPDATE recordings SET transcript = ? WHERE id = ?", (text, rid))
            log.info("transcribed recording %d: %d chars", rid, len(text))
