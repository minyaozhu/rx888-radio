"""FastAPI server: REST for channels/scans/recordings, WebSockets for audio, spectrum, status.

Run:  .venv/bin/python -m rxapp          (then open http://127.0.0.1:8090)
"""

import asyncio
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Optional

import numpy as np
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import BAND_MODES, CONFIG, MODES, RADIOD_CONFS, RUNTIME, VHF_MAX_HZ, VHF_MIN_HZ
from .ft8 import Ft8, conversations
from .hfgain import GAIN_MAX, SLIDER_MIN, HfGain
from .jobs import BANDS, Recorder, Scanner
from .monitor import Monitor
from .radio import Radio
from .rtp import RtpHub
from .spectrum import Spectrum
from .sstvmon import SstvMonitor
from .store import Store
from .supervisor import Supervisor

log = logging.getLogger("rxapp")
WEB = Path(__file__).resolve().parent.parent / "web"


class State:
    store: Store
    radio: Radio
    rtp: RtpHub
    spectrum: Spectrum
    scanner: Scanner
    supervisor: Supervisor
    monitor: Monitor
    hfgain: HfGain
    ft8: Ft8
    sstv: SstvMonitor
    recorders: Dict[int, Recorder] = {}


S = State()


@asynccontextmanager
async def lifespan(app):
    S.store = Store(CONFIG.db_path)
    S.radio = Radio()
    S.radio.store_event = S.store.event
    S.spectrum = Spectrum(S.radio)
    S.supervisor = Supervisor(S.radio, S.spectrum, S.store)
    await S.supervisor.ensure_radiod()
    await S.radio.connect()
    S.rtp = RtpHub(CONFIG.iface_addr, CONFIG.rtp_port)
    await S.rtp.join(S.radio.pcm_group)
    await S.spectrum.start()
    S.scanner = Scanner(S.spectrum, S.store)
    S.supervisor.busy = S.scanner.lock.locked
    S.supervisor.start()
    S.monitor = Monitor(S.radio, S.rtp, S.scanner, S.store)
    await S.monitor.start()

    def any_ssrc():
        st = S.spectrum._stream
        if st is not None and st.ssrc:
            return st.ssrc
        return next((c.ssrc for c in S.radio.active()), None)

    S.hfgain = HfGain(S.radio, S.supervisor._probe, any_ssrc)
    S.supervisor.on_restart.append(lambda: S.hfgain.apply(restarted=True))
    await S.hfgain.apply(restarted=True)
    S.hfgain.start()
    S.ft8 = Ft8(S.radio, S.rtp, S.store)
    if RUNTIME.ft8_bands:                # the FT8 monitor was on: carry on
        try:
            await S.ft8.start(RUNTIME.ft8_bands)
        except Exception as e:
            log.warning("could not restart FT8: %s", e)
    S.sstv = SstvMonitor(S.radio, S.rtp, S.store)
    if RUNTIME.sstv_on:
        try:
            await S.sstv.start()
        except Exception as e:
            log.warning("could not restart the SSTV monitor: %s", e)
    if RUNTIME.band == "vhf":           # come back on the input that was selected last time
        try:
            await S.radio.switch_band("vhf", RUNTIME.vhf_center_hz)
            await S.spectrum.band_changed("hf")
        except Exception as e:
            log.warning("could not restore VHF: %s", e)
    S.store.event("app_start")
    log.info("rxapp ready on http://%s:%d", CONFIG.host, CONFIG.port)
    yield
    for ssrc, rec in list(S.recorders.items()):
        rec.stop()
    await S.monitor.stop()
    await S.ft8.stop(save=False)
    await S.sstv.stop(save=False)
    await S.hfgain.stop()
    await S.supervisor.stop()
    await S.spectrum.stop()
    await S.radio.close()
    S.rtp.close()
    S.store.event("app_stop")


app = FastAPI(title="rxapp", lifespan=lifespan)


@app.middleware("http")
async def no_stale_pages(request, call_next):
    """Browsers must re-check the page and its scripts, so a reload always gets the current UI."""
    resp = await call_next(request)
    if not request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


# ---------------------------------------------------------------- REST
class NewChannel(BaseModel):
    freq_hz: float
    mode: str = "am"
    label: str = ""
    band: Optional[str] = None
    kind: str = "listen"                # listen (a page's own) | air (airband_session.py's recorders)


class ChannelUpdate(BaseModel):
    freq_hz: Optional[float] = None
    mode: Optional[str] = None
    label: Optional[str] = None


class SpectrumView(BaseModel):
    center_hz: Optional[float] = None
    bin_hz: Optional[float] = None


def _channel(ssrc):
    ch = S.radio.channels.get(ssrc)
    if ch is None:
        raise HTTPException(404, f"no channel {ssrc}")
    return ch


@app.get("/api/info")
async def info():
    return {"modes": list(MODES), "band_modes": BAND_MODES, "bands": {k: vars(b) for k, b in BANDS.items()},
            "max_freq_hz": CONFIG.max_freq_hz, "vhf_range_hz": [VHF_MIN_HZ, VHF_MAX_HZ],
            "adc_rates_hz": sorted(RADIOD_CONFS), "adc_rate_hz": RUNTIME.adc_rate_hz,
            "band": S.radio.band, "vhf": S.radio.vhf.info(), "spectrum": S.spectrum.info(),
            "hf_gain": {"db": RUNTIME.hf_gain_db, "min": SLIDER_MIN, "max": GAIN_MAX}}


class BandSwitch(BaseModel):
    band: str
    center_hz: Optional[float] = None


class Coverage(BaseModel):
    adc_rate_hz: float


class VhfGain(BaseModel):
    vga: int


class HfGainSet(BaseModel):
    gain_db: Optional[float] = None     # None = automatic


@app.post("/api/band")
async def set_band(body: BandSwitch):
    """Select the HF input or the VHF/UHF tuner (optionally centred on center_hz)."""
    old = S.radio.band
    try:
        if body.band == "vhf" and old == "vhf" and body.center_hz:
            await S.radio.move_tuner(body.center_hz)
        else:
            await S.radio.switch_band(body.band, body.center_hz)
    except ValueError as e:
        raise HTTPException(400, str(e))
    for ssrc, rec in list(S.recorders.items()):       # recordings can't continue on a parked channel
        if S.radio.channels.get(ssrc) is None or S.radio.channels[ssrc].parked:
            S.recorders.pop(ssrc).stop()
    if body.band != old:
        await S.spectrum.band_changed(old)
        await S.hfgain.apply()          # manual HF gain back on HF; auto (rxapp's AGC) on VHF
    return await info()


@app.post("/api/coverage")
async def set_coverage(body: Coverage):
    """HF coverage: restart radiod at 64.8 MS/s (0-32 MHz) or 129.6 MS/s (0-64 MHz)."""
    try:
        await S.supervisor.set_coverage(body.adc_rate_hz)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if S.ft8.running:                    # 6 m comes and goes with the coverage
        await S.ft8.start(RUNTIME.ft8_bands)
    return await info()


@app.post("/api/hf/gain")
async def hf_gain(body: HfGainSet):
    """HF front-end gain in dB (-41.5..+34; below -10 dB the attenuator is used), or null for auto."""
    await S.hfgain.set(body.gain_db)
    return {"db": RUNTIME.hf_gain_db, **S.hfgain.last}


@app.post("/api/vhf/gain")
async def vhf_gain(body: VhfGain):
    RUNTIME.vga = max(0, min(15, body.vga))
    RUNTIME.save()
    await S.radio.vhf.set_vga(RUNTIME.vga)
    return S.radio.vhf.info()


class Ft8Set(BaseModel):
    on: bool
    bands: Optional[list] = None        # band names ("40m", ...); None = every band the coverage allows


@app.get("/api/ft8")
async def ft8_info():
    return S.ft8.info()


@app.post("/api/ft8")
async def ft8_set(body: Ft8Set):
    try:
        if body.on:
            await S.ft8.start(body.bands)
        else:
            await S.ft8.stop()
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e))
    return S.ft8.info()


class Qth(BaseModel):
    grid: str


@app.post("/api/ft8/qth")
async def ft8_qth(body: Qth):
    """The receiver's Maidenhead locator, for distances and the home marker on the map."""
    g = body.grid.strip()
    if not re.fullmatch(r"[A-Ra-r]{2}\d{2}([A-Xa-x]{2})?", g):
        raise HTTPException(400, "give a Maidenhead locator like CM87 or CM87tj")
    RUNTIME.qth = g[:2].upper() + g[2:4] + g[4:].lower()
    RUNTIME.save()
    return S.ft8.info()


@app.get("/api/ft8/decodes")
async def ft8_decodes(since: float = 0, after_id: int = 0, band: Optional[str] = None, limit: int = 500):
    """Newest first. Poll with after_id = the largest id seen (slots on different bands finish apart)."""
    sql, args = "SELECT * FROM ft8 WHERE t > ? AND id > ?", [since, after_id]
    if band:
        sql, args = sql + " AND band = ?", args + [band]
    return S.store.query(sql + " ORDER BY t DESC, band, hz LIMIT ?", args + [min(limit, 50000)])


@app.get("/api/ft8/conversations")
async def ft8_conversations(hours: float = 1.0, band: Optional[str] = None):
    sql, args = "SELECT * FROM ft8 WHERE t > ?", [time.time() - hours * 3600]
    if band:
        sql, args = sql + " AND band = ?", args + [band]
    return conversations(S.store.query(sql + " ORDER BY t, id", args))


@app.get("/api/ft8/stats")
async def ft8_stats(hours: float = 24.0):
    """Decodes and distinct stations per band, and per band per 15 min for a heat map."""
    t0 = time.time() - hours * 3600
    per_band = S.store.query("SELECT band, COUNT(*) AS decodes, COUNT(DISTINCT from_call) AS stations "
                             "FROM ft8 WHERE t > ? GROUP BY band", (t0,))
    buckets = S.store.query("SELECT band, CAST(t / 900 AS INTEGER) * 900 AS t, COUNT(*) AS n "
                            "FROM ft8 WHERE t > ? GROUP BY band, CAST(t / 900 AS INTEGER)", (t0,))
    return {"per_band": per_band, "buckets": buckets}


class SstvSet(BaseModel):
    on: bool


@app.get("/api/sstv")
async def sstv_info():
    return S.sstv.info()


@app.post("/api/sstv")
async def sstv_set(body: SstvSet):
    if body.on:
        await S.sstv.start()
    else:
        await S.sstv.stop()
    return S.sstv.info()


@app.get("/api/sstv/pictures")
async def sstv_pictures(limit: int = 200):
    return S.store.query("SELECT id, t, band, freq_hz, mode, complete, width, height FROM sstv "
                         "ORDER BY t DESC, id LIMIT ?", (min(limit, 2000),))


def _sstv_row(pid):
    r = S.store.query("SELECT * FROM sstv WHERE id = ?", (pid,))
    if not r:
        raise HTTPException(404, f"no picture {pid}")
    return r[0]


@app.get("/api/sstv/pictures/{pid}.png")
async def sstv_png(pid: int):
    return FileResponse(_sstv_row(pid)["png"], media_type="image/png")


@app.get("/api/sstv/pictures/{pid}.wav")
async def sstv_wav(pid: int):
    r = _sstv_row(pid)
    return FileResponse(r["wav"], media_type="audio/wav", filename=Path(r["wav"]).name)


@app.delete("/api/sstv/pictures/{pid}")
async def sstv_delete(pid: int):
    r = _sstv_row(pid)
    S.store._exec("DELETE FROM sstv WHERE id = ?", (pid,))
    Path(r["png"]).unlink(missing_ok=True)
    if not S.store.query("SELECT 1 FROM sstv WHERE wav = ?", (r["wav"],)):   # last picture from that audio
        Path(r["wav"]).unlink(missing_ok=True)
    return {"ok": True}


# ---------------------------------------------------------------- airband sessions
AIRBAND_DIR = CONFIG.data_dir / "airband"


def _airband_session(name: Optional[str]) -> Path:
    if name:
        d = AIRBAND_DIR / Path(name).name
        if not d.is_dir():
            raise HTTPException(404, f"no airband session {name}")
        return d
    dirs = sorted(p for p in AIRBAND_DIR.glob("*") if p.is_dir()) if AIRBAND_DIR.exists() else []
    if not dirs:
        raise HTTPException(404, "no airband session yet (run airband_session.py)")
    return dirs[-1]


def _jsonl(path: Path):
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass                    # a line being written right now
    return out


@app.get("/api/airband")
async def airband_info(session: Optional[str] = None):
    """The latest (or a named) airband_session.py run: its live status and the other sessions."""
    d = _airband_session(session)
    status = {}
    try:
        status = json.loads((d / "status.json").read_text())
    except (OSError, ValueError):
        pass
    live = bool(status) and time.time() - status.get("t", 0) < 10
    return {"session": d.name, "live": live, "status": status,
            "sessions": sorted((p.name for p in AIRBAND_DIR.glob("*") if p.is_dir()), reverse=True)}


@app.get("/api/airband/transmissions")
async def airband_transmissions(session: Optional[str] = None, after: int = -1):
    """Transmissions in order (index = position); later transcripts (transcripts.jsonl) win."""
    d = _airband_session(session)
    recs = _jsonl(d / "transmissions.jsonl")
    better = {r["wav"]: r for r in _jsonl(d / "transcripts.jsonl")}
    recs = sorted((better.get(r["wav"], r) for r in recs), key=lambda r: r["t0"])
    out = []
    for i, r in enumerate(recs):
        if i > after:
            out.append({"i": i, **{k: r.get(k) for k in ("t0", "t1", "freq_hz", "peak_db", "text", "text_atc")},
                        "clip": Path(r["wav"]).name})
    return out


@app.get("/api/airband/clip/{session}/{name}")
async def airband_clip(session: str, name: str):
    path = _airband_session(session) / Path(name).name
    if path.suffix != ".wav" or not path.exists():
        raise HTTPException(404, "no such clip")
    return FileResponse(path, media_type="audio/wav")


@app.get("/api/health")
async def health():
    return S.supervisor.health


@app.get("/api/channels")
async def channels():
    return [c.to_dict() | {"recording": c.ssrc in S.recorders} for c in S.radio.channels.values()]


@app.post("/api/channels")
async def create_channel(body: NewChannel):
    if (body.band or S.radio.band) == "vhf" and S.radio.band == "vhf":
        await S.spectrum.ensure_live(body.freq_hz)
    try:
        if body.kind not in ("listen", "air"):
            raise ValueError("kind is listen or air")
        ch = await S.radio.create(body.freq_hz, body.mode, body.label, kind=body.kind, band=body.band)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return ch.to_dict()


@app.patch("/api/channels/{ssrc}")
async def update_channel(ssrc: int, body: ChannelUpdate):
    ch = _channel(ssrc)
    if ch.kind != "listen":       # a monitor's channel: retuning it would silently break the monitor
        raise HTTPException(409, f"channel {ssrc} belongs to the {ch.kind} monitor and can't be retuned")
    if ssrc in S.recorders and (body.mode not in (None, ch.mode)):
        raise HTTPException(409, "stop the recording before changing mode")
    if ch.band == "vhf" and S.spectrum.sweeping and body.freq_hz is not None:
        await S.spectrum.ensure_live(body.freq_hz)     # tuning means listening: tuner back to live
    try:
        ch = await S.radio.tune(ssrc, body.freq_hz, body.mode, body.label)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return ch.to_dict()


@app.delete("/api/channels/{ssrc}")
async def delete_channel(ssrc: int):
    if _channel(ssrc).kind == "watch":
        raise HTTPException(409, "this channel belongs to a watch; delete the watch instead")
    if _channel(ssrc).kind == "ft8":
        raise HTTPException(409, "this channel belongs to the FT8 monitor; stop it on the FT8 tab")
    if _channel(ssrc).kind == "sstv":
        raise HTTPException(409, "this channel belongs to the SSTV monitor; stop it on the SSTV tab")
    if ssrc in S.recorders:
        S.recorders.pop(ssrc).stop()
    await S.radio.remove(ssrc)
    return {"ok": True}


@app.get("/api/channels/{ssrc}/status")
async def channel_status(ssrc: int):
    _channel(ssrc)
    ch = await S.radio.refresh(ssrc)
    return ch.to_dict()


@app.post("/api/channels/{ssrc}/record")
async def start_recording(ssrc: int):
    ch = _channel(ssrc)
    if ssrc in S.recorders:
        raise HTTPException(409, "already recording")
    if ch.parked:
        raise HTTPException(409, f"this channel is on {ch.band.upper()}; switch the input to record it")
    S.recorders[ssrc] = Recorder(S.rtp, S.store, ch)
    return {"recording": True, "path": str(S.recorders[ssrc].path)}


@app.delete("/api/channels/{ssrc}/record")
async def stop_recording(ssrc: int):
    rec = S.recorders.pop(ssrc, None)
    if rec is None:
        raise HTTPException(404, "not recording")
    return rec.stop()


@app.get("/api/spectrum")
async def spectrum_info():
    return S.spectrum.info()


@app.post("/api/spectrum")
async def spectrum_view(body: SpectrumView):
    await S.spectrum.configure(body.center_hz, body.bin_hz)
    return S.spectrum.info()


@app.post("/api/scan/{band}")
async def scan(band: str, frames: int = 20, min_snr: float = 8.0):
    if band not in BANDS:
        raise HTTPException(404, f"unknown band {band}")
    try:
        return await S.scanner.scan(band, frames, min_snr)
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.get("/api/scans")
async def scans(band: Optional[str] = None, limit: int = 50):
    q = "SELECT s.*, COUNT(d.freq_hz) AS stations FROM scans s LEFT JOIN detections d ON d.scan_id = s.id"
    args = []
    if band:
        q += " WHERE s.band = ?"
        args.append(band)
    q += " GROUP BY s.id ORDER BY s.t DESC LIMIT ?"
    return S.store.query(q, args + [limit])


@app.get("/api/scans/{sid}")
async def scan_detail(sid: int):
    s = S.store.query("SELECT * FROM scans WHERE id = ?", (sid,))
    if not s:
        raise HTTPException(404)
    hits = S.store.query("SELECT freq_hz, snr_db FROM detections WHERE scan_id = ? ORDER BY snr_db DESC", (sid,))
    return s[0] | {"hits": hits}


@app.get("/api/occupancy/{band}")
async def occupancy(band: str, hours: float = 24):
    """For the dashboard: per channel, how often it was heard and its median SNR."""
    since = time.time() - hours * 3600
    nscans = S.store.query("SELECT COUNT(*) AS n FROM scans WHERE band = ? AND t >= ?", (band, since))[0]["n"]
    rows = S.store.query(
        "SELECT d.freq_hz, COUNT(*) AS heard, AVG(d.snr_db) AS mean_snr, MAX(d.snr_db) AS max_snr "
        "FROM detections d JOIN scans s ON s.id = d.scan_id WHERE s.band = ? AND s.t >= ? "
        "GROUP BY d.freq_hz ORDER BY d.freq_hz", (band, since))
    return {"band": band, "scans": nscans, "channels": rows}


@app.get("/api/timeline/{band}")
async def timeline(band: str, hours: float = 24):
    since = time.time() - hours * 3600
    return S.store.query(
        "SELECT s.t, d.freq_hz, d.snr_db FROM detections d JOIN scans s ON s.id = d.scan_id "
        "WHERE s.band = ? AND s.t >= ? ORDER BY s.t", (band, since))


@app.get("/api/recordings")
async def recordings(limit: int = 100):
    return S.store.query("SELECT * FROM recordings ORDER BY start DESC LIMIT ?", (limit,))


@app.get("/api/recordings/{rid}/audio")
async def recording_audio(rid: int):
    r = S.store.query("SELECT path FROM recordings WHERE id = ?", (rid,))
    if not r or not Path(r[0]["path"]).exists():
        raise HTTPException(404)
    return FileResponse(r[0]["path"], media_type="audio/wav", filename=Path(r[0]["path"]).name)


@app.get("/api/events")
async def events(limit: int = 100):
    return S.store.query("SELECT * FROM events ORDER BY t DESC LIMIT ?", (limit,))


@app.get("/api/health/history")
async def health_history(hours: float = 24):
    return S.store.query("SELECT * FROM health WHERE t >= ? ORDER BY t", (time.time() - hours * 3600,))


class NewSchedule(BaseModel):
    band: str
    every_min: float = 10


class NewWatch(BaseModel):
    freq_hz: float
    mode: str = "am"
    label: str = ""
    snr_db: float = 10
    transcribe: bool = False


@app.get("/api/schedules")
async def schedules():
    return S.monitor.schedules()


@app.post("/api/schedules")
async def add_schedule(body: NewSchedule):
    try:
        return {"id": S.monitor.add_schedule(body.band, max(1.0, body.every_min))}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/schedules/{sid}")
async def delete_schedule(sid: int):
    S.monitor.delete_schedule(sid)
    return {"ok": True}


@app.get("/api/watches")
async def watches():
    return S.monitor.watch_list()


@app.post("/api/watches")
async def add_watch(body: NewWatch):
    try:
        return {"id": await S.monitor.add_watch(body.freq_hz, body.mode, body.label, body.snr_db, body.transcribe)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/watches/{wid}")
async def delete_watch(wid: int):
    await S.monitor.delete_watch(wid)
    return {"ok": True}


# ---------------------------------------------------------------- WebSockets
@app.websocket("/ws/audio/{ssrc}")
async def ws_audio(ws: WebSocket, ssrc: int):
    """Binary frames: u32 sample rate (LE) + int16 LE PCM."""
    await ws.accept()
    if ssrc not in S.radio.channels:
        await ws.close(code=4404)
        return
    q: asyncio.Queue = asyncio.Queue(maxsize=50)

    def on_pcm(pcm, ts, seq):
        ch = S.radio.channels.get(ssrc)
        rate = ch.sample_rate if ch else 12000
        try:
            q.put_nowait(np.array([rate], "<u4").tobytes() + pcm)
        except asyncio.QueueFull:
            pass  # slow client: drop rather than build latency

    S.rtp.subscribe(ssrc, on_pcm)
    try:
        while True:
            await ws.send_bytes(await q.get())
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        S.rtp.unsubscribe(ssrc, on_pcm)


@app.websocket("/ws/spectrum")
async def ws_spectrum(ws: WebSocket):
    await ws.accept()
    q: asyncio.Queue = asyncio.Queue(maxsize=4)

    def on_frame(f):
        if q.full():
            q.get_nowait()
        q.put_nowait(f)

    S.spectrum.subs.append(on_frame)
    try:
        while True:
            f = await q.get()
            await ws.send_bytes(f.to_bytes())
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        if on_frame in S.spectrum.subs:
            S.spectrum.subs.remove(on_frame)


@app.websocket("/ws/status")
async def ws_status(ws: WebSocket):
    """Once a second: channel levels/SNR, radiod health, spectrum view."""
    await ws.accept()
    try:
        while True:
            for ch in S.radio.active():
                await S.radio.refresh(ch.ssrc)
            await ws.send_json({
                "band": S.radio.band, "vhf": S.radio.vhf.info(), "adc_rate_hz": RUNTIME.adc_rate_hz,
                "hf_gain_db": RUNTIME.hf_gain_db,
                "max_freq_hz": CONFIG.max_freq_hz,
                "t": time.time(),
                "channels": [c.to_dict() | {"recording": c.ssrc in S.recorders} for c in S.radio.channels.values()],
                "health": S.supervisor.health, "spectrum": S.spectrum.info(),
                "scanning": S.scanner.lock.locked()})
            await asyncio.sleep(1)
    except (WebSocketDisconnect, RuntimeError):
        pass


app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
