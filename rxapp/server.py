# SPDX-License-Identifier: GPL-3.0-or-later
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
from typing import Dict, List, Optional

import numpy as np
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import BAND_MODES, CONFIG, MODES, RADIOD_CONFS, RUNTIME, VHF_MAX_HZ, VHF_MIN_HZ
from .adsb import AdsbFeed
from .broadcast import Eibi, stations as broadcast_stations
from . import ssb
from .panels import PanelHub
from .vhf import WINDOW_HZ
from .ft8 import FT4, Ft8, conversations
from .places import HOME_CITY, Geocoder, locate
from .wspr import WsprMonitor, grid_ll, km_between
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
    ft4: Ft8
    wspr: WsprMonitor
    sstv: SstvMonitor
    adsb: AdsbFeed
    geo: Geocoder
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
    S.ft4 = Ft8(S.radio, S.rtp, S.store, FT4)
    S.wspr = WsprMonitor(S.radio, S.rtp, S.store)
    if RUNTIME.wspr_bands:
        try:
            await S.wspr.start(RUNTIME.wspr_bands)
        except Exception as e:
            log.warning("could not restart WSPR: %s", e)
    for mon in (S.ft8, S.ft4):           # a monitor that was on: carry on
        if mon.saved_bands:
            try:
                await mon.start(mon.saved_bands)
            except Exception as e:
                log.warning("could not restart %s: %s", mon.proto.name, e)
    S.geo = Geocoder(CONFIG.data_dir / "geocache.json")
    S.adsb = AdsbFeed()
    S.adsb.start()                      # fetches only while a page is asking for aircraft
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
    S.panels = PanelHub(S.radio, S.spectrum)
    S.panels.start()
    S.store.event("app_start")
    log.info("rxapp ready on http://%s:%d", CONFIG.host, CONFIG.port)
    yield
    for ssrc, rec in list(S.recorders.items()):
        rec.stop()
    await S.monitor.stop()
    await S.ft8.stop(save=False)
    await S.ft4.stop(save=False)
    await S.wspr.stop(save=False)
    await S.sstv.stop(save=False)
    await S.adsb.stop()
    await S.panels.stop()
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
    kind: str = "listen"                # listen (a page's own) | air | safety (voice_session.py's recorders)


class ChannelUpdate(BaseModel):
    freq_hz: Optional[float] = None
    mode: Optional[str] = None
    label: Optional[str] = None
    filter_hz: Optional[List[float]] = None     # [low, high] Hz around the carrier; [] = the mode's preset
    stereo: Optional[bool] = None               # WFM: FM stereo on / off


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
    for mon in (S.ft8, S.ft4):           # 6 m comes and goes with the coverage
        if mon.running:
            await mon.start(mon.saved_bands)
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


class Qth(BaseModel):
    grid: str


def _digital_mode_routes(name: str):
    """/api/ft8/... and /api/ft4/...: the same endpoints over the monitor and table of that protocol."""
    mon = lambda: getattr(S, name)

    @app.get(f"/api/{name}", name=f"{name}_info")
    async def info_():
        return mon().info()

    @app.post(f"/api/{name}", name=f"{name}_set")
    async def set_(body: Ft8Set):
        try:
            if body.on:
                await mon().start(body.bands)
            else:
                await mon().stop()
        except (ValueError, RuntimeError) as e:
            raise HTTPException(400, str(e))
        return mon().info()

    @app.post(f"/api/{name}/qth", name=f"{name}_qth")
    async def qth_(body: Qth):
        """The receiver's Maidenhead locator, for distances and the home marker on the map."""
        g = body.grid.strip()
        if not re.fullmatch(r"[A-Ra-r]{2}\d{2}([A-Xa-x]{2})?", g):
            raise HTTPException(400, "give a Maidenhead locator like CM87 or CM87tj")
        RUNTIME.qth = g[:2].upper() + g[2:4] + g[4:].lower()
        RUNTIME.save()
        return mon().info()

    @app.get(f"/api/{name}/decodes", name=f"{name}_decodes")
    async def decodes_(since: float = 0, after_id: int = 0, band: Optional[str] = None, limit: int = 500):
        """Newest first. Poll with after_id = the largest id seen (slots on different bands finish apart)."""
        sql, args = f"SELECT * FROM {name} WHERE t > ? AND id > ?", [since, after_id]
        if band:
            sql, args = sql + " AND band = ?", args + [band]
        return S.store.query(sql + " ORDER BY t DESC, band, hz LIMIT ?", args + [min(limit, 50000)])

    @app.get(f"/api/{name}/conversations", name=f"{name}_conversations")
    async def conversations_(hours: float = 1.0, band: Optional[str] = None):
        sql, args = f"SELECT * FROM {name} WHERE t > ?", [time.time() - hours * 3600]
        if band:
            sql, args = sql + " AND band = ?", args + [band]
        return conversations(S.store.query(sql + " ORDER BY t, id", args))

    @app.get(f"/api/{name}/stats", name=f"{name}_stats")
    async def stats_(hours: float = 24.0):
        """Decodes and distinct stations per band, and per band per 15 min for a heat map."""
        t0 = time.time() - hours * 3600
        per_band = S.store.query("SELECT band, COUNT(*) AS decodes, COUNT(DISTINCT from_call) AS stations "
                                 f"FROM {name} WHERE t > ? GROUP BY band", (t0,))
        buckets = S.store.query("SELECT band, CAST(t / 900 AS INTEGER) * 900 AS t, COUNT(*) AS n "
                                f"FROM {name} WHERE t > ? GROUP BY band, CAST(t / 900 AS INTEGER)", (t0,))
        return {"per_band": per_band, "buckets": buckets}


_digital_mode_routes("ft8")
_digital_mode_routes("ft4")


@app.get("/api/wspr")
async def wspr_info():
    return S.wspr.info()


@app.post("/api/wspr")
async def wspr_set(body: Ft8Set):
    try:
        if body.on:
            await S.wspr.start(body.bands)
        else:
            await S.wspr.stop()
    except (ValueError, RuntimeError) as e:
        raise HTTPException(400, str(e))
    return S.wspr.info()


@app.get("/api/wspr/spots")
async def wspr_spots(since: float = 0, after_id: int = 0, limit: int = 5000):
    """WSPR spots, newest first (poll with after_id)."""
    return S.store.query("SELECT * FROM wspr WHERE t > ? AND id > ? ORDER BY t DESC, band LIMIT ?",
                         (since, after_id, min(limit, 50000)))


@app.get("/api/propagation")
async def propagation(minutes: float = 30):
    """Which bands are open now: per band, WSPR spots and FT8/FT4 decodes in the last `minutes`, distinct
    stations, and the farthest station heard (from grids)."""
    t0 = time.time() - minutes * 60
    me = grid_ll(RUNTIME.qth)
    bands = {}
    def row(b):
        return bands.setdefault(b, {"band": b, "wspr": 0, "ft8": 0, "ft4": 0, "stations": set(), "far_km": 0, "far_call": None})
    for r in S.store.query("SELECT band, call, grid, km FROM wspr WHERE t > ?", (t0,)):
        x = row(r["band"]); x["wspr"] += 1; x["stations"].add(r["call"])
        if r["km"] and r["km"] > x["far_km"]:
            x["far_km"], x["far_call"] = r["km"], r["call"]
    for table in ("ft8", "ft4"):
        for r in S.store.query(f"SELECT band, from_call, extra FROM {table} WHERE t > ?", (t0,)):
            x = row(r["band"]); x[table] += 1
            if r["from_call"]:
                x["stations"].add(r["from_call"])
            g = (r["extra"] or "").split(" ")[0]
            if g and g != "RR73":
                d = km_between(me, grid_ll(g))
                if d and d > x["far_km"]:
                    x["far_km"], x["far_call"] = round(d, 1), r["from_call"]
    out = []
    for x in bands.values():
        x["stations"] = len(x["stations"])
        out.append(x)
    return {"minutes": minutes, "qth": RUNTIME.qth, "bands": out}


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


@app.get("/api/sstv/live/{band}.png")
async def sstv_live(band: str):
    """The picture being received on a band right now, as decoded so far."""
    path = S.sstv.dir / f"live_{Path(band).name}.png"
    if not path.exists():
        raise HTTPException(404, "nothing being received on that band")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})


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


# ---------------------------------------------------------------- voice sessions (airband, public safety)
AIRBAND_DIR = CONFIG.data_dir / "airband"
SAFETY_DIR = CONFIG.data_dir / "safety"
MARINE_DIR = CONFIG.data_dir / "marine"
APRS_DIR = CONFIG.data_dir / "aprs"


def _session_dir(base: Path, name: Optional[str]) -> Path:
    if name:
        d = base / Path(name).name
        if not d.is_dir():
            raise HTTPException(404, f"no session {name}")
        return d
    dirs = sorted(p for p in base.glob("*") if p.is_dir()) if base.exists() else []
    if not dirs:
        raise HTTPException(404, "no session yet (run voice_session.py)")
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


def _session_info(base: Path, session: Optional[str]):
    d = _session_dir(base, session)
    status = {}
    try:
        status = json.loads((d / "status.json").read_text())
    except (OSError, ValueError):
        pass
    live = bool(status) and time.time() - status.get("t", 0) < 10
    return {"session": d.name, "live": live, "status": status,
            "sessions": sorted((p.name for p in base.glob("*") if p.is_dir()), reverse=True)}


def _session_transmissions(base: Path, session: Optional[str], after: int):
    """Transmissions in order (index = position); later transcripts (transcripts.jsonl) win."""
    d = _session_dir(base, session)
    recs = _jsonl(d / "transmissions.jsonl")
    better = {r["wav"]: r for r in _jsonl(d / "transcripts.jsonl")}
    recs = sorted((better.get(r["wav"], r) for r in recs), key=lambda r: r["t0"])
    return [{"i": i, **{k: r.get(k) for k in ("t0", "t1", "freq_hz", "peak_db", "text", "text_atc", "kind", "band")},
             "clip": Path(r["wav"]).name} for i, r in enumerate(recs) if i > after]


def _session_clip(base: Path, session: str, name: str):
    path = _session_dir(base, session) / Path(name).name
    if path.suffix != ".wav" or not path.exists():
        raise HTTPException(404, "no such clip")
    return FileResponse(path, media_type="audio/wav")


@app.get("/api/airband")
async def airband_info(session: Optional[str] = None):
    """The latest (or a named) airband session: its live status and the other sessions."""
    return _session_info(AIRBAND_DIR, session)


@app.get("/api/airband/transmissions")
async def airband_transmissions(session: Optional[str] = None, after: int = -1):
    return _session_transmissions(AIRBAND_DIR, session, after)


@app.get("/api/airband/clip/{session}/{name}")
async def airband_clip(session: str, name: str):
    return _session_clip(AIRBAND_DIR, session, name)


@app.get("/api/safety")
async def safety_info(session: Optional[str] = None):
    """The latest (or a named) public-safety session (recordings are kept 7 days)."""
    return _session_info(SAFETY_DIR, session)


@app.get("/api/safety/transmissions")
async def safety_transmissions(session: Optional[str] = None, after: int = -1):
    return _session_transmissions(SAFETY_DIR, session, after)


@app.get("/api/safety/clip/{session}/{name}")
async def safety_clip(session: str, name: str):
    return _session_clip(SAFETY_DIR, session, name)


@app.get("/api/marine")
async def marine_info(session: Optional[str] = None):
    """The latest (or a named) marine session: marine voice, NOAA weather channels, AIS state."""
    return _session_info(MARINE_DIR, session)


@app.get("/api/marine/transmissions")
async def marine_transmissions(session: Optional[str] = None, after: int = -1):
    return _session_transmissions(MARINE_DIR, session, after)


@app.get("/api/marine/clip/{session}/{name}")
async def marine_clip(session: str, name: str):
    return _session_clip(MARINE_DIR, session, name)


@app.get("/api/marine/vessels")
async def marine_vessels(session: Optional[str] = None):
    """Vessels decoded from AIS in this session (AIS-catcher via voice_session.py), with tracks."""
    d = _session_dir(MARINE_DIR, session)
    try:
        return json.loads((d / "vessels.json").read_text())
    except (OSError, ValueError):
        return {"t": None, "messages": 0, "vessels": [], "running": False, "error": None}


@app.get("/api/aprs")
async def aprs_info(session: Optional[str] = None):
    """The latest (or a named) APRS session's live status."""
    return _session_info(APRS_DIR, session)


@app.get("/api/aprs/stations")
async def aprs_stations(session: Optional[str] = None):
    """APRS stations heard on 144.390 MHz (multimon-ng + aprslib via voice_session.py), with tracks."""
    d = _session_dir(APRS_DIR, session)
    try:
        return json.loads((d / "stations.json").read_text())
    except (OSError, ValueError):
        return {"t": None, "packets": 0, "stations": [], "running": False, "error": None}


@app.get("/api/aprs/packets")
async def aprs_packets(session: Optional[str] = None, limit: int = 300):
    """The latest raw packets, newest first."""
    d = _session_dir(APRS_DIR, session)
    return list(reversed(_jsonl(d / "packets.jsonl")[-min(limit, 5000):]))


@app.get("/api/safety/places")
async def safety_places(session: Optional[str] = None, after: int = -1):
    """Places mentioned in public-safety transmissions (towns; highway + cross street via OpenStreetMap),
    with coordinates once looked up. Transmissions without a place are left out."""
    out = []
    for r in _session_transmissions(SAFETY_DIR, session, after):
        places = locate(r, S.geo, HOME_CITY.get(round((r["freq_hz"] or 0) / 1000) * 1000))
        if places:
            out.append({"i": r["i"], "t0": r["t0"], "freq_hz": r["freq_hz"], "text": r["text"], "places": places})
    return out


# ---------------------------------------------------------------- receiver focus (voice_session.py reads this)
FOCUS_FILE = CONFIG.data_dir / "voice_control.json"
FOCUS_VALUES = {"air": "airband windows", "safety": "public-safety windows", "marine": "the marine window (AIS, marine radio, weather)",
                "aprs": "the APRS window (144.390 MHz)",
                "hf": "the HF input (FT8, SSTV, low band)",
                "pause": "paused for manual tuning"}


class Focus(BaseModel):
    focus: Optional[str] = None         # air | safety | marine | hf | pause | null (rotate through everything)


@app.get("/api/voice/focus")
async def get_focus():
    try:
        d = json.loads(FOCUS_FILE.read_text())
    except (OSError, ValueError):
        d = {"focus": None}
    return {**d, "label": FOCUS_VALUES.get(d.get("focus"), "rotating through all windows")}


@app.post("/api/voice/focus")
async def set_focus(body: Focus):
    """Hold the voice session on one kind of window (the pages' "switch the receiver here" buttons)."""
    if body.focus is not None and body.focus not in FOCUS_VALUES:
        raise HTTPException(400, f"focus is one of {', '.join(FOCUS_VALUES)} or null")
    FOCUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    FOCUS_FILE.write_text(json.dumps({"focus": body.focus, "since": time.time()}))
    S.store.event("voice_focus", focus=body.focus)
    return await get_focus()


# ---------------------------------------------------------------- controller key bindings (web/controller.js)
CONTROLLER_FILE = CONFIG.data_dir / "controller.json"


@app.get("/api/controller")
async def get_controller():
    """Key / dial bindings for the tab shell: {"bindings": {"Shift+BracketRight": "tune_up_fast", ...}} or {} = defaults."""
    try:
        return json.loads(CONTROLLER_FILE.read_text())
    except (OSError, ValueError):
        return {}


@app.put("/api/controller")
async def put_controller(body: dict):
    b = body.get("bindings")
    if b is not None and not (isinstance(b, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in b.items())):
        raise HTTPException(400, "bindings is an object of combo -> action")
    CONTROLLER_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONTROLLER_FILE.write_text(json.dumps({"bindings": b} if b is not None else {}, indent=1))
    return await get_controller()


# ---------------------------------------------------------------- SSB voice (HF)
@app.get("/api/ssb/channels")
async def ssb_channels():
    return ssb.channels(CONFIG.max_freq_hz)


class SsbCheck(BaseModel):
    group: Optional[str] = None          # one group (ham | air | mil | marine), or every listed channel


@app.post("/api/ssb/check")
async def ssb_check(body: SsbCheck):
    """Signal in each listed channel's voice band right now (one spectrum capture per ~430 kHz of channels)."""
    items = [c for c in ssb.channels(CONFIG.max_freq_hz)["channels"]
             if c["reachable"] and (body.group is None or c["group"] == body.group)]
    try:
        return {"t": time.time(), "results": await ssb.check(S.scanner, items)}
    except ValueError as e:
        raise HTTPException(409, str(e))


# ---------------------------------------------------------------- broadcast (AM / FM / shortwave)
_eibi: Optional[Eibi] = None


async def _schedule() -> Eibi:
    global _eibi
    if _eibi is None:                    # loads (and if needed downloads) the season's schedule on first use
        _eibi = await asyncio.to_thread(Eibi, CONFIG.data_dir / "eibi")
    return _eibi


# FM stereo: which stations send the 19 kHz pilot. Probed by opening a stereo WFM channel on every station in
# a tuner window at once and counting the stereo packets radiod sends (it sends stereo only with the pilot).
FM_STEREO_FILE = CONFIG.data_dir / "fm_stereo.json"
FM_PACKETS_PER_S = 150          # radiod's WFM output: 48 kHz in 320-frame packets


def _fm_stereo() -> Dict:
    try:
        return json.loads(FM_STEREO_FILE.read_text())
    except (OSError, ValueError):
        return {}


class StereoProbe(BaseModel):
    freqs_hz: List[float]
    seconds: float = 2.5


@app.get("/api/broadcast/fm/stereo")
async def fm_stereo_known():
    """{"<kHz>": {"stereo": true | false | null (no audio: squelched), "t": unix time}} from earlier probes."""
    return _fm_stereo()


@app.post("/api/broadcast/fm/stereo")
async def fm_stereo_probe(body: StereoProbe):
    if S.radio.band != "vhf":
        raise HTTPException(409, "FM is on the VHF tuner; switch the input to VHF first")
    freqs = sorted({round(f / 1e5) * 1e5 for f in body.freqs_hz if 87.5e6 <= f <= 108.1e6})[:60]
    use = 2 * WINDOW_HZ * 0.85                     # stay clear of the tuner window's edges
    groups, i = [], 0
    while i < len(freqs):
        g = [f for f in freqs[i:] if f - freqs[i] <= use]
        groups.append(g)
        i += len(g)
    home = S.radio.vhf.center_hz
    known, out = _fm_stereo(), {}
    async with S.scanner.lock:                     # not under a scan
        try:
            for g in groups:
                if not all(S.radio.vhf.in_window(f) and abs(f - S.radio.vhf.center_hz) < use / 2 for f in g):
                    await S.radio.move_tuner((g[0] + g[-1]) / 2)
                chans, base = [], {}
                for f in g:
                    ch = await S.radio.create(f, "wfm", label="stereo probe", kind="probe", band="vhf")
                    await S.radio.tune(ch.ssrc, stereo=True)
                    S.rtp.subscribe(ch.ssrc, _noop_pcm)          # the hub learns channel counts only for subscribers
                    chans.append(ch)
                await asyncio.sleep(0.5)                          # let the stereo decoders settle
                for ch in chans:
                    base[ch.ssrc] = (S.rtp.packets[ch.ssrc], S.rtp.stereo_packets[ch.ssrc])
                wait = max(1.0, min(body.seconds, 6.0))
                await asyncio.sleep(wait)
                for ch in chans:
                    total = S.rtp.packets[ch.ssrc] - base[ch.ssrc][0]
                    st = S.rtp.stereo_packets[ch.ssrc] - base[ch.ssrc][1]
                    # audio flows at ~150 packets/s; much less means the squelch keeps closing: a weak signal,
                    # too weak for the stereo decoder whatever the station sends
                    weak = total < 0.8 * FM_PACKETS_PER_S * wait
                    verdict = None if total < 20 else True if st >= 0.3 * total else None if weak else False
                    out[f"{ch.freq_hz / 1e3:.0f}"] = {"stereo": verdict, "weak": weak and total >= 20, "t": time.time(),
                                                      "packets": total, "heard": total > 0}
                    S.rtp.unsubscribe(ch.ssrc, _noop_pcm)
                    await S.radio.remove(ch.ssrc)
        finally:
            for ch in [c for c in list(S.radio.channels.values()) if c.kind == "probe"]:
                S.rtp.unsubscribe(ch.ssrc, _noop_pcm)
                await S.radio.remove(ch.ssrc)
            if home and abs((S.radio.vhf.center_hz or 0) - home) > 1:
                await S.radio.move_tuner(home)
    for k, v in out.items():                       # the latest says how it is now
        known[k] = v
    FM_STEREO_FILE.write_text(json.dumps(known))
    return {"results": out, "known": known}


def _noop_pcm(pcm, ts, seq):
    pass


@app.get("/api/broadcast/stations")
async def broadcast_station_list():
    """Bay Area AM and FM stations (call sign, city, format)."""
    return broadcast_stations()


@app.get("/api/broadcast/sw/now")
async def broadcast_sw_now(band: Optional[str] = None):
    """Shortwave broadcasts scheduled right now (EiBi), within one broadcast band or all of them."""
    e = await _schedule()
    sw = [b for b in BANDS.values() if b.band == "hf" and b.mode == "am" and b.step_hz == 5e3]
    if band:
        sw = [b for b in sw if b.name == band]
        if not sw:
            raise HTTPException(404, f"no shortwave band {band}")
    out = []
    for b in sw:
        for x in e.on_air(b.start_hz / 1e3, b.stop_hz / 1e3):
            out.append({**x, "band": b.name})
    return {"file": e.file, "bands": [{"name": b.name, "start_hz": b.start_hz, "stop_hz": b.stop_hz} for b in sw],
            "now": sorted(out, key=lambda x: x["khz"])}


@app.get("/api/adsb")
async def adsb(since: float = 0.0):
    """Aircraft around the Bay from the adsb.fi open-data feed, with track points newer than `since`."""
    return S.adsb.snapshot(since)


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
        if body.kind not in ("listen", "air", "safety", "marine"):
            raise ValueError("kind is listen, air, safety or marine")
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
        flt = ... if body.filter_hz is None else body.filter_hz
        if flt is not ... and flt and not (len(flt) == 2 and -150e3 <= flt[0] < flt[1] <= 150e3):
            raise ValueError("filter_hz is [low, high] in Hz, low < high, within ±150 kHz")
        ch = await S.radio.tune(ssrc, body.freq_hz, body.mode, body.label, flt, body.stereo)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return ch.to_dict()


@app.delete("/api/channels/{ssrc}")
async def delete_channel(ssrc: int):
    if _channel(ssrc).kind == "watch":
        raise HTTPException(409, "this channel belongs to a watch; delete the watch instead")
    if _channel(ssrc).kind in ("ft8", "ft4", "wspr"):
        raise HTTPException(409, f"this channel belongs to the {_channel(ssrc).kind.upper()} monitor; stop it on its tab")
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
    """Binary frames: u32 header (LE) + int16 LE PCM. Header: sample rate, with the top bit set when the
    PCM is stereo (L, R interleaved)."""
    await ws.accept()
    if ssrc not in S.radio.channels:
        await ws.close(code=4404)
        return
    q: asyncio.Queue = asyncio.Queue(maxsize=50)

    def on_pcm(pcm, ts, seq):
        ch = S.radio.channels.get(ssrc)
        rate = ch.sample_rate if ch else 12000
        if S.rtp.channels(ssrc) == 2:             # radiod sends stereo only while it hears the pilot
            rate |= 0x80000000
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


@app.websocket("/ws/panels")
async def ws_panels(ws: WebSocket):
    """The Radio page's panel grid: send {"ranges": [[lo_hz, hi_hz], ...]}; frames come back (see panels.py)."""
    await ws.accept()
    if S.spectrum.sweeping:                # an old wide VHF view would keep the tuner moving
        await S.spectrum.ensure_live(S.radio.vhf.center_hz)
    try:
        await S.panels.serve(ws)
    except (WebSocketDisconnect, RuntimeError, ValueError):
        pass


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


@app.middleware("http")
async def no_stale_pages(request, call_next):
    """Pages are small and change with rxapp: have browsers re-check them (scripts are versioned with ?v=)."""
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith((".html", ".js", ".css")):
        response.headers["Cache-Control"] = "no-cache"
    return response


app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
