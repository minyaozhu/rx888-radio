# SPDX-License-Identifier: GPL-3.0-or-later
"""Record and transcribe every voice channel in the receiver's window, across a rotation of windows.

Runs next to rxapp (which must be running). Each *window* is a piece of spectrum the RX-888 can see at
once: an 8.4 MHz slice through the VHF tuner, or a slice of the HF input (direct sampling, so no tuner;
CHP's 39-46 MHz low band is there). The session visits the windows in turn and, in each one:

  * watches radiod's spectrum of the window (a private spectrum stream; the page's view is untouched)
    for carriers keying up on the window's channel grid;
  * gives each channel that keys up its own rxapp channel (AM for airband, FM for public safety), and
    records its audio;
  * cuts out each transmission by its carrier, saves it as a WAV, and transcribes it on-device with
    Whisper (mlx-whisper, large-v3-turbo). Public-safety transmissions that look digital (P25, DMR) are
    logged but not transcribed;
  * drops channels whose carrier never goes away (receiver spurs, not voice).

Output per service: rxapp_data/airband/<stamp>/ and rxapp_data/safety/<stamp>/ (one WAV per transmission,
transmissions.jsonl, status.json for the live pages). Public-safety recordings and transcripts are deleted
after SAFETY_KEEP_DAYS. At the end the session removes its channels and puts rxapp back on its input.

    .venv/bin/python voice_session.py --windows approach:vhf:135.7,towers:vhf:120.6,lowband:hf:42.6:safety,vhfhigh:vhf:155.0:safety
    .venv/bin/python voice_session.py --windows approach:135.7 --minutes 60        # one airband window
    .venv/bin/python voice_session.py --retranscribe rxapp_data/airband/<stamp>

Window syntax: name:band:centre_MHz[:service[:hold]], band = vhf | hf, service = air (default) | safety | marine | aprs | p25 | uhf;
"hold" = visit this window only while the receiver is held on its service (the page's button).
(The older name:centre_MHz means a VHF airband window.) Live pages: /airband.html, /safety.html.
"""

import argparse
import json
import logging
import math
import queue
import re
import signal
import socket
import struct
import threading
import time
import urllib.request
import wave
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

import numpy as np
from ka9q import RadiodControl

from rxapp.addr import maddr
from rxapp.config import CONFIG
from rxapp.spectrum import LeasedSpectrumStream
from rxapp.speech import (COMPRESSION_MAX, HALLUCINATIONS, VOICED_MIN, WHISPER, prepare,  # noqa: F401
                          transcribe, voiced_share)

log = logging.getLogger("voice")
API = f"http://{CONFIG.host}:{CONFIG.port}/api"

BIN_HZ = 2050.0
BINS = 4096                      # 8.4 MHz: the tuner's usable window (and the same slice of HF)
OPEN_DB = 12.0                   # carrier this far above the window's median floor = keyed
HANG_SEC = 0.7                   # carrier gone this long = transmission over
MIN_TX_SEC = 0.6
PRE_SEC, POST_SEC = 0.4, 0.4
SPUR_FRACTION = 0.9              # keyed this much of the last SPUR_WINDOW s = a spur, not voice
SPUR_WINDOW = 60.0
MAX_CHANNELS = 64
SAFETY_KEEP_DAYS = 7

ATC_PROMPT = ("Air traffic control radio, Northern California. NorCal Approach, Oakland Center, San Francisco "
              "tower. United, Southwest, Alaska, SkyWest, Delta, American, JetBlue. Descend and maintain, "
              "climb and maintain, turn left heading, reduce speed to 210, cleared visual approach runway "
              "two eight left, contact tower, traffic twelve o'clock, altimeter, roger, wilco.")
SAFETY_PROMPT = ("Police and CHP dispatch radio, San Francisco Bay Area. 10-4, 10-97, 11-80, 11-79, 11-83, "
                 "11-25, 10-15, 1125, Code 3, Code 4, copy, en route, on scene, northbound 101 at Tully, "
                 "southbound 280, 880, 680, 17, 85, 87, Highway 1, units, San Jose, Redwood City, Oakland, "
                 "Hayward, Gilroy, plate, DUI, collision, traffic break, BOLO, ambulance, fire.")


@dataclass(frozen=True)
class Profile:
    """How one kind of radio service is found, demodulated and transcribed."""
    service: str                 # air | safety: output folder, page, rxapp channel kind
    mode: str                    # rxapp mode
    grid: float                  # channel raster to look on (Hz)
    neighbours: int              # grid points either side a carrier can spill into (only the strongest counts)
    wide_min: int                # this many grid points lit with no clear peak = a wide non-voice signal
    wide_peak_db: float          # ...unless the strongest stands this far above the next one
    snap: float                  # a carrier within this of a known channel is that channel (FM wanders on a fine grid)
    ranges: Tuple[Tuple[float, float], ...]
    prompt: str
    label: str
    digital_check: bool          # classify digital transmissions and skip their transcription
    min_frames: int = 1          # spectrum frames (~0.1 s each) a carrier must last before it counts
    sideband_hz: float = 0.0     # a carrier this close to one sideband_db stronger is that one's sideband
    sideband_db: float = 10.0


AIR = Profile("air", "am", 25e3, 3, 3, 10.0, 0.0, ((118.0e6, 136.975e6),), ATC_PROMPT, "AIR", False, min_frames=2)
# Public-safety FM: 12.5 kHz channels on a 2.5 kHz raster (154.0025, 158.4375; 42.500, 42.280).
# A voice FM signal lights ~4-5 grid points, so "wide" needs much more; NOAA weather (162.4-162.55) excluded.
# The 2.5 kHz grid means ~3,300 points per window: single-frame noise peaks would open dozens of channels
# a minute (seen on the HF low band), so a carrier must hold for 3 frames.
# A strong FM signal (+30 dB) also shows sidebands 17.5-20 kHz either side (seen around 41.100 MHz).
SAFETY = Profile("safety", "nfm", 2.5e3, 5, 10, 10.0, 6e3,
                 ((30.0e6, 50.0e6), (150.8e6, 162.0e6), (450.0e6, 470.0e6)), SAFETY_PROMPT, "PS", True,
                 min_frames=3, sideband_hz=25e3)
MARINE_PROMPT = ("Marine VHF radio, San Francisco Bay. Coast Guard Sector San Francisco, San Francisco Traffic, "
                 "Vessel Traffic Service, channel one six, channel two two alpha, securite, pan pan, mayday, "
                 "tug, ferry, container ship, pilot boat, Golden Gate, Bay Bridge, Alcatraz, Oakland harbor, "
                 "Richmond, Carquinez, inbound, outbound, over, out, roger.")
# Marine voice on 25 kHz channels: 156.0-157.45 (ship and coast, minus channel 70, 156.525, which is digital
# DSC) and the coast side of the duplex channels; AIS (161.975 / 162.025) and NOAA weather are handled apart.
MARINE = Profile("marine", "nfm", 25e3, 1, 3, 10.0, 0.0,
                 ((156.0e6, 156.5e6), (156.55e6, 157.45e6), (160.6e6, 161.95e6)), MARINE_PROMPT, "MAR", False,
                 min_frames=2)
# APRS: data only (no voice channels); the window's work is AprsReceiver on 144.390 MHz
APRS = Profile("aprs", "nfm", 25e3, 1, 3, 10.0, 0.0, (), "", "APRS", False)
UHF_PROMPT = ("Business and industrial two-way radio, San Francisco Bay Area: security, maintenance, facilities, "
              "school bus, transit, construction, utilities, hospital, hotel, warehouse, parking, event staff. "
              "Copy, ten-four, on my way, go ahead, standby, base, dispatch, unit, gate, dock, loading, over.")
# UHF business and public safety: 12.5 kHz FM channels on the 6.25 kHz raster, 450-470 MHz. FRS/GMRS
# (462.5500-462.7250, 467.5500-467.7250) are personal radios, so left out. A 12.5 kHz FM signal lights ~2 grid
# points; a 25 kHz-wide one (4+) is data.
UHF = Profile("uhf", "nfm", 6.25e3, 2, 5, 10.0, 3e3,
              ((450.0e6, 462.53e6), (462.74e6, 467.53e6), (467.74e6, 470.0e6)), UHF_PROMPT, "UHF", True,
              min_frames=3, sideband_hz=25e3)
# P25: the 700 MHz trunked system, decoded by dsd-fme (rxapp/p25.py); no spectrum-found voice channels either
P25 = Profile("p25", "nfm", 25e3, 1, 3, 10.0, 0.0, (), "", "P25", False)
PROFILES = {"air": AIR, "safety": SAFETY, "marine": MARINE, "aprs": APRS, "p25": P25, "uhf": UHF}
APRS_HZ = 144.390e6
MULTIMON = Path(__file__).resolve().parent / "tools" / "multimon-ng" / "build" / "multimon-ng"
NOAA_HZ = [162.400e6, 162.425e6, 162.450e6, 162.475e6, 162.500e6, 162.525e6, 162.550e6]
# ADS-B: short hops of the VHF tuner to 1090 MHz in the middle of any VHF window (see AdsbHopper)
ADSB_HZ = 1090e6
ADSB_TUNE_OFFSET = 1.0e6      # tune 1 MHz above, so 1090 MHz sits away from the tuner's DC spike
ADSB_REF = (37.6, -122.2)     # reference for decoding positions from one message (anywhere within ~180 NM)
AIS_CENTRE = 162.000e6        # AIS channel A 161.975, B 162.025: both in a 96 kHz IQ channel here
AIS_RATE = 96000
AIS_CATCHER = Path(__file__).resolve().parent / "tools" / "AIS-catcher" / "build" / "AIS-catcher"


@dataclass(frozen=True)
class Window:
    name: str
    band: str                    # vhf | hf
    centre: float                # Hz
    profile: Profile
    on_demand: bool = False      # visited only while the receiver is held on this window's service


def parse_windows(spec: str) -> List[Window]:
    out = []
    for w in (x.strip() for x in spec.split(",")):
        if not w:
            continue
        p = w.split(":")
        if len(p) == 2:                                   # old form: name:centre (VHF airband)
            p = [p[0], "vhf", p[1]]
        name, band, centre = p[0], p[1].lower(), float(p[2]) * 1e6
        prof = PROFILES[p[3].lower()] if len(p) > 3 else AIR
        if band not in ("vhf", "hf"):
            raise ValueError(f"window {w!r}: band is vhf or hf")
        out.append(Window(name, band, centre, prof, on_demand=len(p) > 4 and p[4].lower() == "hold"))
    return out


def call(method, path, body=None, timeout=60):
    req = urllib.request.Request(API + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"} if body is not None else {})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read() or b"null")


class Audio:
    """rxapp's RTP audio for our channels: per SSRC, recent (arrival time, samples)."""

    def __init__(self, group, keep_sec=60.0):
        self.keep = keep_sec
        self.buf = defaultdict(deque)
        self.lock = threading.Lock()
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
        s.bind((group, CONFIG.rtp_port))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(group) + socket.inet_aton(CONFIG.iface_addr))
        s.settimeout(1.0)
        self.sock = s
        self.wanted = set()
        self.running = True
        threading.Thread(target=self._rx, daemon=True).start()

    def _rx(self):
        while self.running:
            try:
                d, _ = self.sock.recvfrom(65536)
            except socket.timeout:
                continue
            if len(d) < 12:
                continue
            ssrc = struct.unpack("!I", d[8:12])[0]
            if ssrc not in self.wanted:
                continue
            h = 12 + 4 * (d[0] & 15)
            if d[0] & 0x10:
                h += 4 + 4 * struct.unpack("!H", d[h + 2:h + 4])[0]
            a = np.frombuffer(d[h:], ">i2").astype("<i2")
            now = time.time()
            with self.lock:
                q = self.buf[ssrc]
                q.append((now, a))
                while q and now - q[0][0] > self.keep:
                    q.popleft()

    def cut(self, ssrc, t0, t1, rate):
        """Samples whose packets arrived in [t0, t1] (packet arrival ~ end of its samples)."""
        with self.lock:
            parts = [a for t, a in self.buf.get(ssrc, ()) if t0 <= t <= t1 + len(a) / rate]
        return np.concatenate(parts) if parts else np.zeros(0, np.int16)


# ---- audio analysis ---------------------------------------------------------------------

def looks_digital(a: np.ndarray, rate: int) -> bool:
    """FM-demodulated P25/DMR/NXDN: a continuous, noise-like signal with a steady level, unlike speech,
    whose level rises and falls with syllables. Heuristic: steady 20 ms power (low variation) AND a flat
    spectrum in the 300-2400 Hz band where the symbols sit."""
    x = a.astype(np.float64)
    x = x - x.mean()
    n = int(0.02 * rate)
    if len(x) < 25 * n:
        return False
    frames = x[: len(x) // n * n].reshape(-1, n)
    p = np.sqrt((frames ** 2).mean(1))
    p = p[len(p) // 10: -len(p) // 10 or None]           # skip key-up and key-down
    cv = p.std() / (p.mean() + 1e-9)
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
    f = np.fft.rfftfreq(len(x), 1 / rate)
    band = spec[(f >= 300) & (f <= 2400)] + 1e-12
    flat = np.exp(np.log(band).mean()) / band.mean()       # 1 = white, ~0 = tonal / formants
    return cv < 0.3 and flat > 0.3




class Transcriber(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.q = queue.Queue()
        self.done = 0

    def run(self):
        while True:
            job = self.q.get()
            if job is None:
                return
            rec, out, prof, rate = job
            try:
                with wave.open(rec["wav"]) as w:
                    a = np.frombuffer(w.readframes(w.getnframes()), "<i2")
                if prof.digital_check and looks_digital(a, rate):
                    rec.update(kind="digital", text="", text_atc="")
                elif prof.digital_check and (v := voiced_share(a, rate)) < VOICED_MIN:
                    rec.update(kind="voice", text="", text_atc="", voiced=round(v, 3))   # no speech: Whisper would invent some
                else:
                    rec.update(kind="voice", **transcribe(a, rate, prof.prompt))
            except Exception as e:
                rec["text"], rec["error"] = "", str(e)
            with open(out, "a") as f:
                f.write(json.dumps(rec) + "\n")
            self.done += 1
            log.info("[%s] %s %.4f %4.1fs %+.0fdB  %s", time.strftime("%H:%M:%S", time.localtime(rec["t0"])),
                     prof.label, rec["freq_hz"] / 1e6, rec["t1"] - rec["t0"], rec["peak_db"],
                     "(digital)" if rec.get("kind") == "digital" else rec["text"] or "(no speech)")


# ---- retention ---------------------------------------------------------------------------
def prune_p25(days: float, now: float = None):
    """P25 call recordings (rxapp_data/p25/<session>/calls/) older than `days`: the same retention as the
    public-safety recordings."""
    cut = (now or time.time()) - days * 86400
    gone = 0
    for wav in [*(CONFIG.data_dir / "p25").glob("*/calls/*.wav"), *(CONFIG.data_dir / "p25").glob("*/input.raw"),
                *(CONFIG.data_dir / "p25").glob("*/iq.raw")]:
        if wav.stat().st_mtime < cut:
            wav.unlink(missing_ok=True)
            gone += 1
    return gone


def prune(base: Path, days: float, now: float = None):
    """Delete recordings and transcript lines older than `days` under base/<session>/."""
    now = now or time.time()
    cut = now - days * 86400
    if not base.exists():
        return 0
    gone = 0
    for d in base.iterdir():
        if not d.is_dir():
            continue
        for wav in d.glob("*.wav"):
            if wav.stat().st_mtime < cut:
                wav.unlink(missing_ok=True)
                gone += 1
        for jl in ("transmissions.jsonl", "transcripts.jsonl"):
            p = d / jl
            if not p.exists():
                continue
            keep = []
            for line in p.read_text().splitlines():
                try:
                    if json.loads(line).get("t0", now) >= cut:
                        keep.append(line)
                except ValueError:
                    pass
            p.write_text("".join(l + "\n" for l in keep))
        # an emptied session folder goes too, but only an old one (a session that just started is empty)
        if d.stat().st_mtime < cut and not any(d.glob("*.wav")) and not (d / "status.json").exists() and \
                all(not (d / jl).exists() or not (d / jl).read_text().strip() for jl in ("transmissions.jsonl", "transcripts.jsonl")):
            for f in d.iterdir():
                f.unlink(missing_ok=True)
            d.rmdir()
    return gone


# ---- AIS ----------------------------------------------------------------------------------
class AisReceiver:
    """While the marine window is on: a 96 kHz float IQ channel around 162.000 MHz from radiod, piped into
    AIS-catcher (-r CF32 . -o 5); its JSON messages update the vessel list."""

    def __init__(self, out_dir: Path):
        from rxapp.ais import Vessels
        self.out = out_dir
        self.vessels = Vessels()
        self.proc = None
        self.ssrc = None
        self.running = False
        self.error = None
        self.last_write = 0.0
        self.lock = threading.Lock()

    def start(self, ctl, f_if: float):
        if self.running:
            # Back in the window (after a pause, or the tuner moved): the old channel may sit at a stale IF
            # or have expired in radiod, so start over with a fresh one.
            self.stop()
        if not AIS_CATCHER.exists():
            self.error = f"AIS-catcher not built ({AIS_CATCHER}); see README"
            log.warning(self.error)
            return
        import subprocess
        self.ctl = ctl
        group = maddr("voice-ais.local")
        self.ssrc = int(time.time()) % 1_000_000_000 + 7_000_000
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16 << 20)
        s.bind((group, CONFIG.rtp_port))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(group) + socket.inet_aton(CONFIG.iface_addr))
        s.settimeout(1.0)
        self.sock = s
        # float output must be chosen when the channel is created (switching it later mixes formats)
        ctl.create_channel(frequency_hz=f_if, preset="iq", sample_rate=AIS_RATE, destination=group, ssrc=self.ssrc,
                           lifetime=1500, encoding=4, low_edge=-46000, high_edge=46000)
        self.proc = subprocess.Popen([str(AIS_CATCHER), "-r", "CF32", ".", "-s", str(AIS_RATE), "-o", "5"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.running = True
        self.error = None
        threading.Thread(target=self._pump, daemon=True).start()
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()
        log.info("AIS: receiving around %.3f MHz", AIS_CENTRE / 1e6)

    def _pump(self):
        rms, last_keep = None, time.time()
        proc = self.proc
        while self.running:
            if time.time() - last_keep > 10:         # radiod drops the channel when its lifetime runs out
                try:
                    self.ctl.set_channel_lifetime(self.ssrc, 1500)
                except Exception:
                    pass
                last_keep = time.time()
            try:
                d, _ = self.sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(d) < 12 or struct.unpack("!I", d[8:12])[0] != self.ssrc:
                continue
            h = 12 + 4 * (d[0] & 15)
            if d[0] & 0x10:
                h += 4 + 4 * struct.unpack("!H", d[h + 2:h + 4])[0]
            a = np.frombuffer(d[h:], "<f4").reshape(-1, 2).copy()
            a[:, 1] = -a[:, 1]                       # the tuner inverts the spectrum: conjugate it back
            r = float(np.sqrt((a ** 2).mean())) + 1e-12
            rms = r if rms is None else 0.999 * rms + 0.001 * r     # slow level control: AIS-catcher likes ~0.1
            try:
                proc.stdin.write((a * (0.1 / rms)).astype("<f4").tobytes())
            except (BrokenPipeError, ValueError, OSError):
                break

    def _read(self, proc):
        out = self.out / "ais.jsonl"
        for line in proc.stdout:
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get("class") != "AIS" or not m.get("mmsi"):
                continue
            now = time.time()
            with self.lock:
                self.vessels.ingest(m, now)
            keep = {k: m[k] for k in ("type", "mmsi", "lat", "lon", "speed", "course", "heading", "shipname", "shiptype",
                                      "destination", "status_text", "channel", "signalpower") if k in m}
            with open(out, "a") as f:
                f.write(json.dumps({"t": round(now, 2), **keep}) + "\n")

    def write(self, now):
        if now - self.last_write < 2:
            return
        self.last_write = now
        with self.lock:
            self.vessels.prune(now)
            snap = self.vessels.snapshot()
        snap.update(running=self.running, error=self.error)
        tmp = self.out / "vessels.json.tmp"
        tmp.write_text(json.dumps(snap))
        tmp.replace(self.out / "vessels.json")

    def stop(self):
        if not self.running:
            return
        self.running = False
        try:
            self.ctl.remove_channel(self.ssrc)
        except Exception:
            pass
        try:
            self.proc.stdin.close()
            self.proc.terminate()
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass
        log.info("AIS: stopped")
        self.write(time.time() + 3)


# ---- APRS ---------------------------------------------------------------------------------
class AprsReceiver:
    """While the APRS window is on: radiod FM audio on 144.390 MHz, resampled to 22,050 Hz and piped into
    multimon-ng (AFSK1200, -A); its TNC2 lines are parsed with aprslib into stations."""

    def __init__(self, out_dir: Path):
        from rxapp.aprs import Stations
        self.out = out_dir
        self.stations = Stations()
        self.running = False
        self.error = None
        self.last_write = 0.0
        self.lock = threading.Lock()

    def start(self, ctl, f_if: float):
        if self.running:
            # Back in the window (after a pause, or the tuner moved): the old channel may sit at a stale IF
            # or have expired in radiod, so start over with a fresh one.
            self.stop()
        if not MULTIMON.exists():
            self.error = f"multimon-ng not built ({MULTIMON}); see README"
            log.warning(self.error)
            return
        import subprocess
        self.ctl = ctl
        group = maddr("voice-aprs.local")
        self.ssrc = int(time.time()) % 1_000_000_000 + 9_000_000
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
        s.bind((group, CONFIG.rtp_port))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(group) + socket.inet_aton(CONFIG.iface_addr))
        s.settimeout(1.0)
        self.sock = s
        ctl.create_channel(frequency_hz=f_if, preset="nfm", sample_rate=24000, destination=group, ssrc=self.ssrc, lifetime=1500)
        self.proc = subprocess.Popen([str(MULTIMON), "-t", "raw", "-a", "AFSK1200", "-A", "-"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.running, self.error = True, None
        threading.Thread(target=self._pump, daemon=True).start()
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()
        log.info("APRS: receiving on %.3f MHz", APRS_HZ / 1e6)

    def _pump(self):
        from scipy.signal import resample_poly
        proc, last_keep, carry = self.proc, time.time(), np.zeros(0, np.float32)
        while self.running:
            # The FM squelch sends nothing between packets, so this can't wait for audio: radiod drops a
            # channel whose lifetime isn't renewed (30 s), and APRS would go deaf after the first quiet spell.
            if time.time() - last_keep > 10:
                try:
                    self.ctl.set_channel_lifetime(self.ssrc, 1500)
                except Exception:
                    pass
                last_keep = time.time()
            try:
                d, _ = self.sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(d) < 12 or struct.unpack("!I", d[8:12])[0] != self.ssrc:
                continue
            h = 12 + 4 * (d[0] & 15)
            if d[0] & 0x10:
                h += 4 + 4 * struct.unpack("!H", d[h + 2:h + 4])[0]
            carry = np.concatenate([carry, np.frombuffer(d[h:], ">i2").astype(np.float32)])
            if len(carry) < 2400:                 # resample in 0.1 s blocks (24 kHz -> 22.05 kHz = 147/160)
                continue
            n = len(carry) // 160 * 160
            y = resample_poly(carry[:n], 147, 160)
            carry = carry[n:]
            try:
                proc.stdin.write(np.clip(y, -32767, 32767).astype("<i2").tobytes())
                proc.stdin.flush()
            except (BrokenPipeError, ValueError, OSError):
                break

    def _read(self, proc):
        import aprslib
        out = self.out / "packets.jsonl"
        for raw in proc.stdout:
            line = raw.decode("latin-1", "replace").strip()
            if not line.startswith("APRS: "):
                continue
            tnc2 = line[6:]
            now = time.time()
            try:
                p = aprslib.parse(tnc2)
            except Exception as e:                # unparseable or unsupported: keep the raw packet
                p = {"raw": tnc2, "from": tnc2.split(">", 1)[0], "error": str(e)[:80]}
            with self.lock:
                self.stations.ingest(p, now)
            keep = {k: p[k] for k in ("from", "to", "format", "latitude", "longitude", "symbol", "symbol_table", "comment",
                                      "object_name", "speed", "course", "altitude", "weather", "status", "error") if k in p}
            with open(out, "a") as f:
                f.write(json.dumps({"t": round(now, 2), "raw": tnc2, **keep}) + "\n")
            log.info("APRS %s", tnc2[:120])

    def write(self, now):
        if now - self.last_write < 2:
            return
        self.last_write = now
        with self.lock:
            self.stations.prune(now)
            snap = self.stations.snapshot()
        snap.update(running=self.running, error=self.error)
        tmp = self.out / "stations.json.tmp"
        tmp.write_text(json.dumps(snap))
        tmp.replace(self.out / "stations.json")

    def stop(self):
        if not self.running:
            return
        self.running = False
        for f in (lambda: self.ctl.remove_channel(self.ssrc), lambda: self.proc.stdin.close(),
                  lambda: self.proc.terminate(), lambda: self.sock.close()):
            try:
                f()
            except Exception:
                pass
        log.info("APRS: stopped")
        self.write(time.time() + 3)


class AdsbHopper:
    """ADS-B from the RX-888, in hops: during a VHF window the tuner goes to 1090 MHz for a few seconds, a
    2 MS/s IQ channel records it, and the tuner comes back. Decoding (rxapp.modes + pyModeS) runs on a thread;
    aircraft go to adsb.json in the airband session, which rxapp merges into /api/adsb.

    The hops adapt to how busy voice is (plan()): the quieter the window has been over the last two minutes,
    the longer and more often they are. `points` is [(busy share, hop seconds, interval seconds)], interpolated
    in between and held beyond the ends (DEFAULT_PLAN).

    Positions come from single messages with a reference point (the aircraft's last position, or ADSB_REF),
    which suits short hops: the even/odd message pair a global decode needs may not arrive in one hop."""
    FS = 2_000_000
    KEEP_SEC = 15 * 60
    TRAIL_SEC = 20 * 60

    DEFAULT_PLAN = [(0.0, 5.0, 10.0), (0.15, 3.0, 20.0), (0.30, 2.0, 30.0)]   # silent: 5 s every 10 s; busy: 2 s every 30 s

    def __init__(self, out_dir: Path, points=None):
        self.out = out_dir
        self.points = sorted(points or self.DEFAULT_PLAN)
        self.secs, self.every = self.points[-1][1], self.points[-1][2]      # the current plan: start as if busy
        self.busy = None
        self.last = time.time()                  # first hop one interval in
        self.hops = self.messages = 0
        self.planes = {}                         # icao (lower case) -> state + trail
        self.lock = threading.Lock()
        self.work = queue.Queue(maxsize=2)
        self.sock = None
        self.ssrc = int(time.time()) % 1_000_000 + 8_000_000

    def plan(self, busy: float):
        """Hop length and interval for a window whose voice channels were on the air `busy` of the time."""
        p = self.points
        self.busy = round(busy, 3)
        x = min(max(busy, p[0][0]), p[-1][0])
        k = next((i for i in range(1, len(p)) if x <= p[i][0]), len(p) - 1)
        (b0, s0, e0), (b1, s1, e1) = p[max(k - 1, 0)], p[k]
        u = 0.0 if b1 == b0 else (x - b0) / (b1 - b0)
        self.secs = round(s0 + (s1 - s0) * u, 1)
        self.every = round(e0 + (e1 - e0) * u)
        return self.secs, self.every

    def due(self, now) -> bool:
        return now - self.last >= self.every

    def _socket(self):
        group = maddr("voice-adsb.local")
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 64 << 20)
        s.bind((group, CONFIG.rtp_port))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(group) + socket.inet_aton(CONFIG.iface_addr))
        s.settimeout(0.3)
        self.sock, self.group = s, group
        threading.Thread(target=self._decode_loop, daemon=True).start()

    def hop(self, ctl, home_hz: float):
        """Tuner to 1090 MHz, record, tuner back to home_hz. Returns when the tuner is home."""
        self.last = time.time()
        if self.sock is None:
            self._socket()
        self.ssrc += 1                           # a fresh channel each hop: radiod reaps the old one
        t_start, chunks = None, []
        try:
            info = call("POST", "/band", {"band": "vhf", "center_hz": ADSB_HZ + ADSB_TUNE_OFFSET})
            f_if = info["vhf"]["if_hz"] - (ADSB_HZ - info["vhf"]["center_hz"])
            ctl.create_channel(frequency_hz=f_if, preset="iq", sample_rate=self.FS, destination=self.group, ssrc=self.ssrc,
                               lifetime=int((self.secs + 2) * 50), encoding=4, low_edge=-1_000_000, high_edge=1_000_000)
            t0 = time.time()
            while time.time() - t0 < self.secs + 1.0:
                try:
                    d, _ = self.sock.recvfrom(65536)
                except socket.timeout:
                    continue
                if len(d) < 12 or struct.unpack("!I", d[8:12])[0] != self.ssrc:
                    continue
                h = 12 + 4 * (d[0] & 15)
                if d[0] & 0x10:
                    h += 4 + 4 * struct.unpack("!H", d[h + 2:h + 4])[0]
                if t_start is None:
                    t_start = time.time()
                chunks.append(d[h:])
                if time.time() - t_start >= self.secs:
                    break
        except Exception as e:
            log.warning("ADS-B hop: %s", e)
        finally:
            try:
                ctl.set_channel_lifetime(self.ssrc, 1)
                ctl.remove_channel(self.ssrc)
            except Exception:
                pass
            try:
                call("POST", "/band", {"band": "vhf", "center_hz": home_hz})
            except Exception as e:                   # rxapp restarting: heal() puts the tuner back
                log.warning("ADS-B hop: tuner not returned (%s)", e)
        self.hops += 1
        if chunks and t_start:
            try:
                self.work.put_nowait((t_start, b"".join(chunks)))
            except queue.Full:
                log.warning("ADS-B: decoder behind; dropping a hop")

    def _decode_loop(self):
        import pyModeS as pms
        from rxapp import modes
        while True:
            t_start, raw = self.work.get()
            a = np.frombuffer(raw, "<f4")
            iq = np.conj((a[0::2] + 1j * a[1::2]).astype(np.complex64))      # the tuner inverts the spectrum
            n = 0
            for idx, hx in modes.demodulate(iq):
                icao = hx[2:8].lower()
                with self.lock:
                    p = self.planes.get(icao)
                ref = (p["lat"], p["lon"]) if p and p.get("lat") is not None and t_start - p["t"] < 600 else ADSB_REF
                try:
                    d = pms.decode(hx, reference=ref)
                except Exception:
                    continue
                if not d.get("crc_valid") or d.get("df") not in (17, 18):
                    continue
                n += 1
                self._ingest(d, t_start + idx / self.FS)
            with self.lock:
                self.messages += n
            self.write(time.time())

    def _ingest(self, d, t):
        icao = d["icao"].lower()
        with self.lock:
            p = self.planes.setdefault(icao, {"hex": icao, "flight": "", "lat": None, "lon": None, "alt": None, "gs": None,
                                              "track": None, "rate": None, "t": 0.0, "heard": t, "messages": 0, "trail": []})
            p["heard"] = max(p["heard"], t)
            p["messages"] += 1
            if d.get("callsign"):
                p["flight"] = d["callsign"].strip("_ ").strip()
            if d.get("altitude") is not None:
                p["alt"] = d["altitude"]
            if d.get("groundspeed") is not None:
                p["gs"] = round(d["groundspeed"])
            if d.get("track") is not None:
                p["track"] = round(d["track"])
            if d.get("vertical_rate") is not None:
                p["rate"] = d["vertical_rate"]
            lat, lon = d.get("latitude"), d.get("longitude")
            if lat is not None and lon is not None:
                # a reference too far away decodes into the wrong zone: keep only plausible positions
                if abs(lat - ADSB_REF[0]) < 4 and abs(lon - ADSB_REF[1]) < 5:
                    p["lat"], p["lon"], p["t"] = round(lat, 5), round(lon, 5), t
                    tr = p["trail"]
                    if not tr or t - tr[-1][0] >= 1:
                        tr.append([round(t, 1), p["lat"], p["lon"], p["alt"]])

    def write(self, now):
        with self.lock:
            for k in [k for k, p in self.planes.items() if now - p["heard"] > self.KEEP_SEC]:
                del self.planes[k]
            for p in self.planes.values():
                p["trail"] = [x for x in p["trail"] if now - x[0] <= self.TRAIL_SEC]
            snap = {"t": now, "hops": self.hops, "every": self.every, "secs": self.secs, "busy": self.busy,
                    "plan": self.points, "messages": self.messages,
                    "aircraft": [dict(p) for p in self.planes.values()]}
        tmp = self.out / "adsb.json.tmp"
        tmp.write_text(json.dumps(snap))
        tmp.replace(self.out / "adsb.json")


# ---- the session -------------------------------------------------------------------------
class Session:
    def __init__(self, windows: List[Window], minutes, stamp, dwell_sec=300.0, preload=(), adsb_plan=()):
        self.windows = windows
        self.minutes, self.dwell = minutes, dwell_sec
        self.preload = list(preload)
        self.outs = {}
        for prof in {w.profile for w in windows}:
            d = CONFIG.data_dir / ("airband" if prof.service == "air" else prof.service) / stamp
            d.mkdir(parents=True, exist_ok=True)
            self.outs[prof.service] = d
        self.w = 0
        self.window_since = time.time()
        self.settle_until = 0.0
        self.channels = {}            # freq -> {"ssrc", "rate", "profile", "band"}
        self.spurs = set()
        self.keyed_since = {}         # freq -> start time of the current transmission
        self.last_keyed = {}
        self.peak = {}
        self.history = defaultdict(deque)   # freq -> (t, keyed) for spur detection
        self.pending = {}             # freq -> (first frame time, consecutive frames) before it counts
        self.frames = queue.Queue()
        self.n_tx = 0
        self.end = None
        self.stream = None
        self.spec_freq = None
        self.last_prune = 0.0
        self.focus = None             # air | safety | marine | hf | pause | None (all windows), from rxapp
        self.focus_checked = 0.0
        self.ais = AisReceiver(self.outs["marine"]) if "marine" in self.outs else None
        self.aprs = AprsReceiver(self.outs["aprs"]) if "aprs" in self.outs else None
        from rxapp.p25 import P25Receiver
        self.p25 = P25Receiver(self.outs["p25"]) if "p25" in self.outs else None
        self.weather = {}             # NOAA weather radio frequency -> rxapp channel (listen only)
        air = self.outs.get("air") or CONFIG.data_dir / "airband" / stamp
        air.mkdir(parents=True, exist_ok=True)
        self.adsb = AdsbHopper(air, adsb_plan or None) if adsb_plan is not None else None
        self.activity = deque()       # (frame time, any voice channel on the air) over the last ACTIVITY_SEC
        self.wx_bin = {}              # NOAA frequency -> spectrum bin, while the window covers it
        self.wx_db = {}               # NOAA frequency -> carrier above the floor (dB, smoothed)
        self.healed = 0.0

    @property
    def win(self) -> Window:
        return self.windows[self.w]

    # -- setup / teardown ------------------------------------------------------------
    def start(self):
        info = call("GET", "/info")
        self.prev_band = info["band"]
        self.ctl = RadiodControl(maddr(CONFIG.status_name), client_id="voice", interface=CONFIG.iface_addr)
        self.audio = Audio(maddr(CONFIG.pcm_name))
        self.tx = Transcriber()
        self.tx.start()
        first = next((i for i, w in enumerate(self.windows) if self.allowed(w)), 0)
        self.set_window(first)

    def set_window(self, w):
        """Move to window w: switch rxapp's input (and tuner), re-aim the spectrum. Transmissions in
        progress end here; rxapp parks the channels the new window can't hear and restores the others."""
        now = time.time()
        for f in list(self.keyed_since):
            self.finish(f, self.keyed_since.pop(f), self.last_keyed.get(f, now))
        self.activity.clear()                             # busyness is per window
        win = self.windows[w]
        if win.band == "vhf":
            call("POST", "/band", {"band": "vhf", "center_hz": win.centre})
            info = call("GET", "/info")
            self.tuner_hz, spec_f = info["vhf"]["center_hz"], info["vhf"]["if_hz"]
            # radiod sees the tuner's IF, inverted: RF = tuner + (IF_centre - f_if)
            rf_of_bin = self.tuner_hz - (np.arange(BINS) - BINS // 2) * BIN_HZ
        else:
            call("POST", "/band", {"band": "hf"})
            self.tuner_hz, spec_f = win.centre, win.centre
            rf_of_bin = win.centre + (np.arange(BINS) - BINS // 2) * BIN_HZ
        self.w, self.window_since = w, time.time()
        half = BINS * BIN_HZ / 2 * 0.98
        lo, hi = self.tuner_hz - half, self.tuner_hz + half
        prof = win.profile
        rng = [(max(lo, a), min(hi, b)) for a, b in prof.ranges if a < hi and b > lo]
        self.lo, self.hi = (rng[0][0], rng[-1][1]) if rng else (lo, lo)
        grid = [np.arange(math.ceil(a / prof.grid) * prof.grid, b + 1, prof.grid) for a, b in rng]
        self.grid = np.round(np.concatenate(grid)) if grid else np.zeros(0)
        self.bin_of = {f: int(np.argmin(np.abs(rf_of_bin - f))) for f in self.grid}
        self.wx_bin = ({f: int(np.argmin(np.abs(rf_of_bin - f))) for f in NOAA_HZ if lo <= f <= hi}
                       if prof.service == "marine" else {})
        self.wx_db = {}
        if spec_f != self.spec_freq:
            self._open_spectrum(spec_f)
        if self.ais is not None:
            if prof.service == "marine" and win.band == "vhf" and lo <= AIS_CENTRE <= hi:
                self.ais.start(self.ctl, info["vhf"]["if_hz"] - (AIS_CENTRE - self.tuner_hz))
                for f in NOAA_HZ:                         # weather radio: listen only, never transcribed
                    if lo <= f <= hi and f not in self.weather:
                        try:
                            ch = call("POST", "/channels", {"freq_hz": f, "mode": "nfm", "band": "vhf",
                                                            "label": f"NOAA {f / 1e6:.3f}", "kind": "marine"})
                            self.weather[f] = ch["ssrc"]
                        except Exception as e:
                            log.warning("weather channel %.3f: %s", f / 1e6, e)
            else:
                self.ais.stop()
        if self.aprs is not None:
            if prof.service == "aprs" and win.band == "vhf" and lo <= APRS_HZ <= hi:
                self.aprs.start(self.ctl, info["vhf"]["if_hz"] - (APRS_HZ - self.tuner_hz))
            else:
                self.aprs.stop()
        if self.p25 is not None:
            from rxapp.p25 import CC_HZ
            if prof.service == "p25" and win.band == "vhf" and lo <= CC_HZ <= hi:
                self.p25.start(self.ctl, self.tuner_hz, info["vhf"]["if_hz"], maddr, CONFIG.rtp_port, CONFIG.iface_addr)
            else:
                self.p25.stop()
        self.settle_until = time.time() + 1.0       # spectrum frames from before the move, PLL settling
        log.info("window %s (%s, %s): watching %.4f-%.4f MHz, %d channels on the %.1f kHz grid",
                 win.name, win.band, prof.service, self.lo / 1e6, self.hi / 1e6, len(self.grid), prof.grid / 1e3)
        for f in self.preload:                      # channels known to be busy: ready before they key up
            g = round(f / prof.grid) * prof.grid
            if any(a <= g <= b for a, b in rng):
                self.ensure_channel(g)

    # -- focus: the pages' "switch the receiver here" buttons ---------------------------
    def allowed(self, w: Window) -> bool:
        if self.focus and self.focus.startswith("window:"):
            return w.name == self.focus[7:]               # held on one window (e.g. the safety page's low band)
        if w.on_demand and self.focus != w.profile.service:
            return False                                  # e.g. marine: only while held there
        if self.focus in (None, ""):
            return True
        if self.focus == "hf":
            return w.band == "hf"
        return w.profile.service == self.focus

    def read_focus(self, now):
        """rxapp writes the focus to voice_control.json; check it once a second."""
        if now - self.focus_checked < 1.0:
            return
        self.focus_checked = now
        try:
            f = json.loads((CONFIG.data_dir / "voice_control.json").read_text()).get("focus")
        except (OSError, ValueError):
            f = None
        if f == self.focus:
            return
        was, self.focus = self.focus, f
        log.info("focus: %s -> %s", was or "all", f or "all")
        if f == "pause":
            for g in list(self.keyed_since):
                self.finish(g, self.keyed_since.pop(g), self.last_keyed.get(g, now))
            if getattr(self, "p25", None) is not None:
                self.p25.stop()                           # manual tuning moves the tuner off the trunk
            return
        if f == "hf" and not any(w.band == "hf" for w in self.windows):
            call("POST", "/band", {"band": "hf"})          # no HF window: just put the radio on HF and wait
            return
        if was == "pause" or not self.allowed(self.win):
            nxt = next((i for i in range(len(self.windows)) if self.allowed(self.windows[(self.w + i) % len(self.windows)])), 0)
            self.set_window((self.w + nxt) % len(self.windows))   # now, not at the next quiet moment

    @property
    def idle(self) -> bool:
        return self.focus == "pause" or (self.focus == "hf" and not any(w.band == "hf" for w in self.windows))

    def maybe_switch(self, now):
        """After the dwell time, move to the next (allowed) window in a quiet moment (or after another
        minute regardless), so a conversation isn't cut in half."""
        if self.idle or now - self.window_since < self.dwell:
            return
        order = [(self.w + i) % len(self.windows) for i in range(1, len(self.windows) + 1)]
        nxt = next((i for i in order if self.allowed(self.windows[i])), self.w)
        if nxt == self.w:
            return
        quiet = not self.keyed_since and all(now - t >= 3.0 for t in self.last_keyed.values() if t)
        if quiet or now - self.window_since >= self.dwell + 60:
            self.set_window(nxt)

    ACTIVITY_SEC = 120.0
    MIN_HISTORY_SEC = 30.0
    GAP_SEC = 3.0                 # a reply usually comes within this of a transmission's end: don't hop into it

    def busy(self, now) -> float:
        """Share of the last ACTIVITY_SEC (in this window) that any voice channel was on the air. Under
        MIN_HISTORY_SEC of history (a window just started, or re-aimed after an rxapp restart) it counts as busy:
        long hops wait until the window is known to be quiet."""
        a = self.activity
        while a and now - a[0][0] > self.ACTIVITY_SEC:
            a.popleft()
        if not a or a[-1][0] - a[0][0] < self.MIN_HISTORY_SEC:
            return 1.0
        return sum(k for _, k in a) / len(a)

    def maybe_adsb(self, now):
        """An ADS-B hop (AdsbHopper) during a VHF window: longer and more often while voice is quiet. It waits
        for a gap (nothing on the air for GAP_SEC), or goes regardless once half an interval overdue."""
        a = self.adsb
        if a is None or self.idle or self.win.band != "vhf" or self.win.profile.service == "p25":
            return                                    # P25: a hop would drop the control channel mid-call
        a.plan(self.busy(now))
        if not a.due(now):
            return
        last_voice = max((t for t in self.last_keyed.values() if t), default=0.0)
        quiet = not self.keyed_since and now - last_voice >= self.GAP_SEC
        if not quiet and now - a.last < a.every * 1.5:
            return
        for f in list(self.keyed_since):                  # cut cleanly: the rest arrives as a new transmission
            self.finish(f, self.keyed_since.pop(f), self.last_keyed.get(f, now))
        a.hop(self.ctl, self.tuner_hz)
        self.settle_until = time.time() + 0.5             # spectrum frames from the hop and the retune: skip

    def _open_spectrum(self, freq):
        if self.stream is not None:
            try:
                self.stream.stop()
            except Exception:
                pass

        def on(st):
            db = st.spectrum.bin_power_db
            if db is not None and len(db):
                self.frames.put((time.time(), np.asarray(db, float)))
        self.stream = LeasedSpectrumStream(control=self.ctl, frequency_hz=freq, bin_count=BINS,
                                     resolution_bw=BIN_HZ, on_spectrum=on)
        self.stream.start()
        self.spec_freq = freq

    def stop(self):
        for d in self.outs.values():
            (d / "status.json").unlink(missing_ok=True)
        try:
            self.stream.stop()
        except Exception:
            pass
        if self.ais is not None:
            self.ais.stop()
        if self.aprs is not None:
            self.aprs.stop()
        if self.p25 is not None:
            self.p25.stop()
        for f, ssrc in list(self.weather.items()):
            try:
                call("DELETE", f"/channels/{ssrc}")
            except Exception:
                pass
        for f, ch in list(self.channels.items()):
            try:
                call("DELETE", f"/channels/{ch['ssrc']}")
            except Exception as e:
                log.warning("remove %.4f: %s", f / 1e6, e)
        try:
            call("POST", "/band", {"band": self.prev_band})
            log.info("input back to %s", self.prev_band.upper())
        except Exception as e:
            log.warning("could not switch back: %s", e)
        self.audio.running = False
        self.tx.q.put(None)
        log.info("waiting for %d transcriptions", self.tx.q.qsize())
        self.tx.join()

    # -- channels ----------------------------------------------------------------------
    def canonical(self, f):
        """FM on a fine raster: a carrier near a channel we already have (or were told about) is that one."""
        snap = self.win.profile.snap
        if snap <= 0:
            return f
        cands = list(self.channels) + self.preload
        near = min(cands, key=lambda c: abs(c - f), default=None)
        return near if near is not None and abs(near - f) <= snap else f

    def ensure_channel(self, f):
        if f in self.channels or f in self.spurs or len(self.channels) >= MAX_CHANNELS:
            return
        if not self.lo <= f <= self.hi:     # outside the window: on VHF rxapp would move the tuner for it
            return
        prof, win = self.win.profile, self.win
        try:
            ch = call("POST", "/channels", {"freq_hz": float(f), "mode": prof.mode, "band": win.band,
                                            "label": f"{prof.label} {f / 1e6:.4f}", "kind": prof.service})
        except Exception as e:
            log.warning("channel %.4f: %s", f / 1e6, e)
            return
        self.channels[f] = {"ssrc": ch["ssrc"], "rate": ch.get("sample_rate") or 12000, "profile": prof, "band": win.band}
        self.audio.wanted.add(ch["ssrc"])
        log.info("new %s channel %.4f MHz (%d)", prof.service, f / 1e6, len(self.channels))

    def heal(self, now):
        """rxapp restarted (it forgets every channel) or moved the receiver: put back what this session needs.
        Checked every 10 s; quietly skipped while rxapp itself is down."""
        if now - self.healed < 10:
            return
        self.healed = now
        try:
            have = {c["ssrc"] for c in call("GET", "/channels")}
            info = call("GET", "/info")
        except Exception:
            return                                         # rxapp restarting: try again in 10 s
        lost = [f for f, c in self.channels.items() if c["ssrc"] not in have]
        for f in lost:
            ch = self.channels.pop(f)
            self.audio.wanted.discard(ch["ssrc"])
            self.keyed_since.pop(f, None)
        for f in [f for f, ssrc in self.weather.items() if ssrc not in have]:
            del self.weather[f]                            # reopened by the next marine window
        win = self.win
        moved = info["band"] != win.band or (win.band == "vhf" and abs((info["vhf"]["center_hz"] or 0) - self.tuner_hz) > 1e3)
        moved, self.was_moved = moved and getattr(self, "was_moved", False), moved   # twice running: not a scan passing
        if lost or (moved and not self.idle):
            log.info("rxapp lost %d channel(s)%s: restoring", len(lost), " and moved the receiver" if moved else "")
            if not self.idle:
                self.set_window(self.w)                    # re-aims the input/tuner and preloads
                for f in lost:
                    self.ensure_channel(f)                 # the ones heard here before

    def drop_spur(self, f):
        self.spurs.add(f)
        ch = self.channels.pop(f, None)
        if ch:
            call("DELETE", f"/channels/{ch['ssrc']}")
        log.info("%.4f MHz is on all the time: a spur, ignoring it", f / 1e6)

    def write_status(self, now):
        """status.json per service: what the live pages show (channels, who is on the air, windows)."""
        nxt = self.window_since + self.dwell if sum(self.allowed(w) for w in self.windows) > 1 else None
        half = BINS * BIN_HZ / 2 * 0.98
        for service, d in self.outs.items():
            here = self.win.profile.service == service
            mine = {f: c for f, c in self.channels.items() if c["profile"].service == service}
            st = {"t": now, "tuner_hz": self.tuner_hz, "lo": self.lo if here else None, "hi": self.hi if here else None,
                  "transmissions": self.n_tx, "window": self.win.name, "window_service": self.win.profile.service,
                  "window_since": self.window_since, "next_switch": None if self.idle else nxt,
                  "listening": here and not self.idle, "focus": self.focus,
                  "windows": [{"name": w.name, "band": w.band, "service": w.profile.service, "on_demand": w.on_demand,
                               "lo": max(w.profile.ranges[0][0], w.centre - half) if w.profile.ranges else w.centre - half,
                               "hi": min(w.profile.ranges[-1][1], w.centre + half) if w.profile.ranges else w.centre + half}
                              for w in self.windows],
                  "channels": {f"{f:.0f}": c["ssrc"] for f, c in mine.items() if here and self.lo <= f <= self.hi},
                  "known": sorted(f"{f:.0f}" for f in mine),
                  "keyed": {f"{f:.0f}": round(self.peak.get(f, 0), 1) for f in self.keyed_since if f in mine},
                  "spurs": [f"{f:.0f}" for f in sorted(self.spurs)], "end": self.end}
            if service == "aprs":
                st["aprs"] = {"running": bool(self.aprs.running), "error": self.aprs.error,
                              "packets": self.aprs.stations.packets, "stations": len(self.aprs.stations.s)}
            if service == "p25":
                st["p25"] = {"running": bool(self.p25 and self.p25.running), "error": self.p25 and self.p25.error}
            if service == "marine":
                st["weather"] = {f"{f:.0f}": ssrc for f, ssrc in self.weather.items()} if here else {}
                st["weather_db"] = {f"{f:.0f}": round(x, 1) for f, x in self.wx_db.items()} if here else {}
                st["ais"] = {"running": bool(self.ais and self.ais.running), "error": self.ais and self.ais.error,
                             "messages": self.ais.vessels.messages if self.ais else 0,
                             "vessels": len(self.ais.vessels.v) if self.ais else 0}
            d.mkdir(parents=True, exist_ok=True)
            tmp = d / "status.json.tmp"
            tmp.write_text(json.dumps(st))
            tmp.replace(d / "status.json")

    # -- main loop --------------------------------------------------------------------
    def run(self):
        self.end = time.time() + self.minutes * 60 if self.minutes > 0 else None
        last_frame = last_status = time.time()
        while self.end is None or time.time() < self.end:
            if time.time() - last_status >= 0.25:     # the pages follow who's on the air from this
                self.write_status(time.time())
                last_status = time.time()
                if self.ais is not None and self.ais.running:
                    self.ais.write(time.time())
                if self.aprs is not None and self.aprs.running:
                    self.aprs.write(time.time())
                if self.p25 is not None and self.p25.running:
                    self.p25.write(time.time())
            if ({"safety", "p25", "uhf"} & set(self.outs)) and time.time() - self.last_prune > 3600:
                n = (prune(CONFIG.data_dir / "safety", SAFETY_KEEP_DAYS) + prune(CONFIG.data_dir / "uhf", SAFETY_KEEP_DAYS)
                     + prune_p25(SAFETY_KEEP_DAYS))
                if n:
                    log.info("retention: deleted %d public-safety recordings older than %d days", n, SAFETY_KEEP_DAYS)
                self.last_prune = time.time()
            try:
                t, db = self.frames.get(timeout=5)
            except queue.Empty:
                if time.time() - last_frame > 10:
                    log.warning("no spectrum for 10 s (radiod restarted?); reopening")
                    self._open_spectrum(self.spec_freq)
                    last_frame = time.time()
                continue
            last_frame = t
            self.read_focus(t)
            self.heal(t)
            self.maybe_switch(t)
            self.maybe_adsb(time.time())
            if self.idle or t < self.settle_until or not len(self.grid):
                continue
            self.step(t, db)

    def step(self, t, db):
        if hasattr(self, "activity"):
            self.activity.append((t, bool(self.keyed_since)))
        prof = self.win.profile
        floor = float(np.median(db))
        bins = np.array([self.bin_of[f] for f in self.grid], dtype=int)
        levels = np.array([float(db[max(0, b - 1):b + 2].max()) for b in bins]) - floor
        # Each grid point takes the max over +-1 bin, so neighbours tie on one peak: break ties by the
        # grid point's own bin, so exactly one grid point wins.
        score = levels + 1e-3 * (db[bins] - floor)
        wx_db = getattr(self, "wx_db", {})
        for f, b in getattr(self, "wx_bin", {}).items():   # weather radio: a steady carrier; how strong is each?
            x = float(db[max(0, b - 1):b + 2].max()) - floor
            wx_db[f] = x if f not in wx_db else 0.8 * wx_db[f] + 0.2 * x
        nb = prof.neighbours
        # pass 1: which channels have a carrier this frame (grid points snapped to channels)
        hits = {}
        for k, g in enumerate(self.grid):
            level = levels[k]
            lo = max(0, k - nb)
            # A strong carrier spills into neighbouring grid points; only the strongest within +-nb counts,
            # or one transmission turns into several.
            if level < OPEN_DB or lo + int(np.argmax(score[lo:k + nb + 1])) != k:
                continue
            near = np.sort(levels[lo:k + nb + 1])
            # Lit across many grid points at once with no clear peak: a wide non-voice signal (data,
            # interference). Voice strong enough to spill stands well above its neighbours.
            if (near >= OPEN_DB).sum() >= prof.wide_min and level - near[-2] < prof.wide_peak_db:
                continue
            if prof.sideband_hz:                          # a much stronger signal close by: its sideband
                sb = int(prof.sideband_hz / prof.grid)
                if levels[max(0, k - sb):k + sb + 1].max() - level >= prof.sideband_db:
                    continue
            f = self.canonical(g)
            if f not in self.spurs:
                hits[f] = max(hits.get(f, -99.0), level)
        # pass 2: per channel, once per frame
        for f in list(self.pending):
            if f not in hits:
                self.pending.pop(f)                       # must be consecutive frames
        # every channel in view gets a sample every frame (quiet ones too), or a busy channel would look
        # "on all the time" to the spur check
        in_view = {f for f in self.channels if self.lo <= f <= self.hi}
        for f in set(hits) | set(self.keyed_since) | in_view:
            keyed = f in hits
            h = self.history[f]
            h.append((t, keyed))
            while h and t - h[0][0] > SPUR_WINDOW:
                h.popleft()
            if not keyed:
                continue
            if f not in self.keyed_since:                 # must hold for min_frames before it counts
                first, n = self.pending.get(f, (t, 0))
                self.pending[f] = (first, n + 1)
                if n + 1 < prof.min_frames:
                    continue
                self.keyed_since[f] = self.pending.pop(f)[0]
                self.ensure_channel(f)
            self.last_keyed[f] = t
            self.peak[f] = max(self.peak.get(f, -99), hits[f])
            if t - h[0][0] > SPUR_WINDOW * 0.9 and sum(x for _, x in h) / len(h) > SPUR_FRACTION:
                self.keyed_since.pop(f, None)
                self.drop_spur(f)
        for f in list(self.keyed_since):
            if t - self.last_keyed.get(f, t) >= HANG_SEC:
                self.finish(f, self.keyed_since.pop(f), self.last_keyed[f])

    def finish(self, f, t0, t1):
        peak = self.peak.pop(f, 0.0)
        ch = self.channels.get(f)
        if t1 - t0 < MIN_TX_SEC or ch is None:
            return
        rate = int(ch["rate"])
        a = self.audio.cut(ch["ssrc"], t0 - PRE_SEC, t1 + POST_SEC, rate)
        if len(a) < 0.3 * rate:
            return
        self.n_tx += 1
        prof = ch["profile"]
        out = self.outs[prof.service]
        path = out / f"{time.strftime('%H%M%S', time.localtime(t0))}_{f / 1e3:.1f}.wav".replace(".0.wav", ".wav")
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(a.tobytes())
        rec = {"t0": t0, "t1": t1, "freq_hz": float(f), "peak_db": round(peak, 1), "wav": str(path),
               "service": prof.service, "band": ch["band"], "rate": rate}
        self.tx.q.put((rec, out / "transmissions.jsonl", prof, rate))


def retranscribe(session_dir: Path):
    """Transcribe a session's WAVs again (e.g. after improving prepare()) into transcripts.jsonl."""
    prof = PROFILES.get(session_dir.parent.name, AIR)
    recs = [json.loads(l) for l in open(session_dir / "transmissions.jsonl")]
    out = session_dir / "transcripts.jsonl"
    done = {json.loads(l)["wav"] for l in open(out)} if out.exists() else set()
    with open(out, "a") as f:
        for i, rec in enumerate(sorted(recs, key=lambda r: r["t0"])):
            if rec["wav"] in done or not Path(rec["wav"]).exists():
                continue
            with wave.open(rec["wav"]) as w:
                rate = w.getframerate()
                a = np.frombuffer(w.readframes(w.getnframes()), "<i2")
            if prof.digital_check and looks_digital(a, rate):
                rec.update(kind="digital", text="", text_atc="")
            elif prof.digital_check and (v := voiced_share(a, rate)) < VOICED_MIN:
                rec.update(kind="voice", text="", text_atc="", voiced=round(v, 3))
            else:
                rec.update(kind="voice", **transcribe(a, rate, prof.prompt))
            f.write(json.dumps(rec) + "\n")
            f.flush()
            print(f"{i + 1}/{len(recs)} {rec['freq_hz'] / 1e6:.4f} {rec['text']}", flush=True)


def clean_noise(session_dir: Path) -> int:
    """Clear the text of a session's transmissions that aren't speech (voiced share below VOICED_MIN) or are a
    stock hallucination, without running Whisper again: the corrected records go to transcripts.jsonl, which
    the pages read over transmissions.jsonl. Returns how many were cleared."""
    recs = [json.loads(l) for l in open(session_dir / "transmissions.jsonl")]
    out = session_dir / "transcripts.jsonl"
    latest = {}
    if out.exists():
        for l in open(out):
            r = json.loads(l)
            latest[r["wav"]] = r
    n = 0
    with open(out, "a") as f:
        for rec in recs:
            rec = latest.get(rec["wav"], rec)
            if rec.get("kind") != "voice" or not (rec.get("text") or rec.get("text_atc")) or not Path(rec["wav"]).exists():
                continue
            with wave.open(rec["wav"]) as w:
                rate = w.getframerate()
                a = np.frombuffer(w.readframes(w.getnframes()), "<i2")
            v = voiced_share(a, rate)
            stock = (rec.get("text") or "").strip().lower() in HALLUCINATIONS
            if v < VOICED_MIN or stock:
                rec.update(text="", text_atc="", voiced=round(v, 3), cleaned="no speech" if v < VOICED_MIN else "stock phrase")
                f.write(json.dumps(rec) + "\n")
                n += 1
    return n


def plan_points(text: str):
    """"0:5:10,15:3:20,30:2:30" -> [(0.0, 5.0, 10.0), (0.15, 3.0, 20.0), (0.3, 2.0, 30.0)]; "off" -> None."""
    if str(text).strip().lower() in ("off", "0", "none", ""):
        return None
    pts = []
    for part in str(text).split(","):
        busy, secs, every = (float(x) for x in part.split(":"))
        if secs <= 0 or every < secs:
            raise ValueError(f"--adsb-plan {part}: the hop must be positive and fit in its interval")
        pts.append((busy / 100.0, secs, every))
    return sorted(pts)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--center", type=float, default=135.7e6, help="one VHF airband window at this centre (Hz)")
    ap.add_argument("--windows", default="", help="windows to rotate through, name:band:centre_MHz[:service],...")
    ap.add_argument("--dwell", type=float, default=300, help="seconds on each window before switching")
    ap.add_argument("--minutes", type=float, default=0, help="0 (default): until stopped (Ctrl-C / SIGTERM)")
    ap.add_argument("--preload", default="", help="comma-separated MHz to open channels on as soon as they're in view")
    ap.add_argument("--retranscribe", type=Path, help="transcribe this session directory's WAVs again")
    ap.add_argument("--clean-noise", type=Path, help="clear the text of this session directory's non-speech transmissions")
    ap.add_argument("--adsb-plan", default="0:5:10,15:3:20,30:2:30",
                    help="ADS-B hops to 1090 MHz during VHF windows, by how busy voice is: BUSY%%:HOP_S:EVERY_S,... "
                         "(interpolated; default: silent 5 s every 10 s, 15%% 3 s every 20 s, 30%%+ 2 s every 30 s; off: none)")
    args = ap.parse_args()
    if args.clean_noise:
        print(f"cleared {clean_noise(args.clean_noise)} transmissions")
        return
    if args.retranscribe:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        return retranscribe(args.retranscribe)
    windows = parse_windows(args.windows) or [Window("window", "vhf", args.center, AIR)]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    s = Session(windows, args.minutes, stamp, args.dwell, [float(x) * 1e6 for x in args.preload.split(",") if x.strip()],
                adsb_plan=plan_points(args.adsb_plan))
    first = next(iter(s.outs.values()))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S",
                        handlers=[logging.StreamHandler(), logging.FileHandler(first / "session.log")])
    for noisy in ("ka9q", "httpx", "huggingface_hub"):     # ka9q logs every spectrum poll at INFO
        logging.getLogger(noisy).setLevel(logging.WARNING)
    signal.signal(signal.SIGTERM, lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
    s.start()
    try:
        s.run()
    except KeyboardInterrupt:
        pass
    finally:
        s.stop()
    log.info("done: %d transmissions in %s", s.n_tx, ", ".join(str(d) for d in s.outs.values()))


if __name__ == "__main__":
    main()
