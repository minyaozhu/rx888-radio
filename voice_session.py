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

Window syntax: name:band:centre_MHz[:service], band = vhf | hf, service = air (default) | safety.
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
from ka9q import RadiodControl, SpectrumStream

from rxapp.addr import maddr
from rxapp.config import CONFIG

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
WHISPER = "mlx-community/whisper-large-v3-turbo"
HALLUCINATIONS = {"", "thank you.", "thanks for watching!", "you", "bye.", ".", "thank you very much."}

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
PROFILES = {"air": AIR, "safety": SAFETY}


@dataclass(frozen=True)
class Window:
    name: str
    band: str                    # vhf | hf
    centre: float                # Hz
    profile: Profile


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
        out.append(Window(name, band, centre, prof))
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
def prepare(a: np.ndarray, rate: int = 12000) -> np.ndarray:
    """Demodulated audio -> what Whisper hears best: DC removed (an AM carrier's level), voice band only,
    normalised, 16 kHz. (Fed raw AM, Whisper called nearly every transmission 'no speech'.)"""
    from scipy.signal import butter, resample_poly, sosfilt
    x = a.astype(np.float32) / 32768
    x = sosfilt(butter(4, [250, 3200], btype="band", fs=rate, output="sos"), x - x.mean())
    x = x / (np.abs(x).max() + 1e-9) * 0.8
    g = math.gcd(16000, int(rate))
    return resample_poly(x, 16000 // g, int(rate) // g).astype(np.float32)


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


def transcribe(a: np.ndarray, rate: int = 12000, prompt: str = ATC_PROMPT) -> dict:
    """Two readings: plain, and with the service's vocabulary prompt (it helps with call signs and
    phrases, but can also pull a garbled transmission towards stock phrases)."""
    import mlx_whisper
    x = prepare(a, rate)
    out = {}
    for key, pr in (("text", None), ("text_atc", prompt)):
        r = mlx_whisper.transcribe(x, path_or_hf_repo=WHISPER, language="en", initial_prompt=pr,
                                   condition_on_previous_text=False, temperature=0.0)
        segs = r.get("segments") or []
        text = " ".join(s["text"].strip() for s in segs if s.get("no_speech_prob", 0) < 0.8).strip()
        caption = re.fullmatch(r"[\[(]?[A-Z][A-Z ,.'!-]*[\])]?", text) is not None   # "BELL RINGS": a sound caption
        out[key] = "" if text.lower() in HALLUCINATIONS or caption else text
    return out


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


# ---- the session -------------------------------------------------------------------------
class Session:
    def __init__(self, windows: List[Window], minutes, stamp, dwell_sec=300.0, preload=()):
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
        self.focus = None             # air | safety | hf | pause | None (all windows), from rxapp
        self.focus_checked = 0.0

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
        self.set_window(0)

    def set_window(self, w):
        """Move to window w: switch rxapp's input (and tuner), re-aim the spectrum. Transmissions in
        progress end here; rxapp parks the channels the new window can't hear and restores the others."""
        now = time.time()
        for f in list(self.keyed_since):
            self.finish(f, self.keyed_since.pop(f), self.last_keyed.get(f, now))
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
        if spec_f != self.spec_freq:
            self._open_spectrum(spec_f)
        self.settle_until = time.time() + 1.0       # spectrum frames from before the move, PLL settling
        log.info("window %s (%s, %s): watching %.4f-%.4f MHz, %d channels on the %.1f kHz grid",
                 win.name, win.band, prof.service, self.lo / 1e6, self.hi / 1e6, len(self.grid), prof.grid / 1e3)
        for f in self.preload:                      # channels known to be busy: ready before they key up
            g = round(f / prof.grid) * prof.grid
            if any(a <= g <= b for a, b in rng):
                self.ensure_channel(g)

    # -- focus: the pages' "switch the receiver here" buttons ---------------------------
    def allowed(self, w: Window) -> bool:
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
        self.stream = SpectrumStream(control=self.ctl, frequency_hz=freq, bin_count=BINS,
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
                  "windows": [{"name": w.name, "band": w.band, "service": w.profile.service,
                               "lo": max(w.profile.ranges[0][0], w.centre - half), "hi": min(w.profile.ranges[-1][1], w.centre + half)}
                              for w in self.windows],
                  "channels": {f"{f:.0f}": c["ssrc"] for f, c in mine.items() if here and self.lo <= f <= self.hi},
                  "known": sorted(f"{f:.0f}" for f in mine),
                  "keyed": {f"{f:.0f}": round(self.peak.get(f, 0), 1) for f in self.keyed_since if f in mine},
                  "spurs": [f"{f:.0f}" for f in sorted(self.spurs)], "end": self.end}
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
            if "safety" in self.outs and time.time() - self.last_prune > 3600:
                n = prune(CONFIG.data_dir / "safety", SAFETY_KEEP_DAYS)
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
            self.maybe_switch(t)
            if self.idle or t < self.settle_until or not len(self.grid):
                continue
            self.step(t, db)

    def step(self, t, db):
        prof = self.win.profile
        floor = float(np.median(db))
        bins = np.array([self.bin_of[f] for f in self.grid], dtype=int)
        levels = np.array([float(db[max(0, b - 1):b + 2].max()) for b in bins]) - floor
        # Each grid point takes the max over +-1 bin, so neighbours tie on one peak: break ties by the
        # grid point's own bin, so exactly one grid point wins.
        score = levels + 1e-3 * (db[bins] - floor)
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
    prof = SAFETY if session_dir.parent.name == "safety" else AIR
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
            else:
                rec.update(kind="voice", **transcribe(a, rate, prof.prompt))
            f.write(json.dumps(rec) + "\n")
            f.flush()
            print(f"{i + 1}/{len(recs)} {rec['freq_hz'] / 1e6:.4f} {rec['text']}", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--center", type=float, default=135.7e6, help="one VHF airband window at this centre (Hz)")
    ap.add_argument("--windows", default="", help="windows to rotate through, name:band:centre_MHz[:service],...")
    ap.add_argument("--dwell", type=float, default=300, help="seconds on each window before switching")
    ap.add_argument("--minutes", type=float, default=0, help="0 (default): until stopped (Ctrl-C / SIGTERM)")
    ap.add_argument("--preload", default="", help="comma-separated MHz to open channels on as soon as they're in view")
    ap.add_argument("--retranscribe", type=Path, help="transcribe this session directory's WAVs again")
    args = ap.parse_args()
    if args.retranscribe:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        return retranscribe(args.retranscribe)
    windows = parse_windows(args.windows) or [Window("window", "vhf", args.center, AIR)]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    s = Session(windows, args.minutes, stamp, args.dwell, [float(x) * 1e6 for x in args.preload.split(",") if x.strip()])
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
