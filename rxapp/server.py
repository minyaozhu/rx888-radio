"""FastAPI server: REST for channels/scans/recordings, WebSockets for audio, spectrum, status.

Run:  .venv/bin/python -m rxapp          (then open http://127.0.0.1:8090)
"""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Optional

import numpy as np
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import CONFIG, MODES
from .jobs import BANDS, Recorder, Scanner
from .monitor import Monitor
from .radio import Radio
from .rtp import RtpHub
from .spectrum import Spectrum
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
    recorders: Dict[int, Recorder] = {}


S = State()


@asynccontextmanager
async def lifespan(app):
    S.store = Store(CONFIG.db_path)
    S.radio = Radio()
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
    S.store.event("app_start")
    log.info("rxapp ready on http://%s:%d", CONFIG.host, CONFIG.port)
    yield
    for ssrc, rec in list(S.recorders.items()):
        rec.stop()
    await S.monitor.stop()
    await S.supervisor.stop()
    await S.spectrum.stop()
    await S.radio.close()
    S.rtp.close()
    S.store.event("app_stop")


app = FastAPI(title="rxapp", lifespan=lifespan)


# ---------------------------------------------------------------- REST
class NewChannel(BaseModel):
    freq_hz: float
    mode: str = "am"
    label: str = ""


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
    return {"modes": list(MODES), "bands": {k: vars(b) for k, b in BANDS.items()},
            "max_freq_hz": CONFIG.max_freq_hz, "spectrum": S.spectrum.info()}


@app.get("/api/health")
async def health():
    return S.supervisor.health


@app.get("/api/channels")
async def channels():
    return [c.to_dict() | {"recording": c.ssrc in S.recorders} for c in S.radio.channels.values()]


@app.post("/api/channels")
async def create_channel(body: NewChannel):
    try:
        ch = await S.radio.create(body.freq_hz, body.mode, body.label)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return ch.to_dict()


@app.patch("/api/channels/{ssrc}")
async def update_channel(ssrc: int, body: ChannelUpdate):
    ch = _channel(ssrc)
    if ssrc in S.recorders and (body.mode not in (None, ch.mode)):
        raise HTTPException(409, "stop the recording before changing mode")
    try:
        ch = await S.radio.tune(ssrc, body.freq_hz, body.mode, body.label)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return ch.to_dict()


@app.delete("/api/channels/{ssrc}")
async def delete_channel(ssrc: int):
    if _channel(ssrc).kind == "watch":
        raise HTTPException(409, "this channel belongs to a watch; delete the watch instead")
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
    return await S.scanner.scan(band, frames, min_snr)


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
            for ssrc in list(S.radio.channels):
                await S.radio.refresh(ssrc)
            await ws.send_json({
                "t": time.time(),
                "channels": [c.to_dict() | {"recording": c.ssrc in S.recorders} for c in S.radio.channels.values()],
                "health": S.supervisor.health, "spectrum": S.spectrum.info(),
                "scanning": S.scanner.lock.locked()})
            await asyncio.sleep(1)
    except (WebSocketDisconnect, RuntimeError):
        pass


app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
