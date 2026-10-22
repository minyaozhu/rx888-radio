"""Record and transcribe every airband channel in the VHF tuner's window at once.

Runs next to rxapp (which must be running). It switches rxapp to the VHF input, centred so the
tuner's 8.4 MHz window covers the wanted part of the airband, and then:

  * watches radiod's spectrum of the whole window (a private spectrum stream, so the page's view
    is untouched) for AM carriers keying up on the 25 kHz channel grid;
  * gives each channel that keys up its own rxapp AM channel, and records its audio;
  * cuts out each transmission by its carrier (on when it rises OPEN_DB above the floor), saves it
    as a WAV, and transcribes it on-device with Whisper (mlx-whisper, large-v3-turbo);
  * drops channels whose carrier never goes away (receiver spurs, not voice).

Everything goes to rxapp_data/airband/<stamp>/: one WAV per transmission and transmissions.jsonl.
At the end it removes its channels and puts rxapp back on the input it was on.

    .venv/bin/python airband_session.py --center 135.7e6                 # until stopped
    .venv/bin/python airband_session.py --center 135.7e6 --minutes 60 --preload 135.65,133.175
    .venv/bin/python airband_session.py --retranscribe rxapp_data/airband/<stamp>

Watch it live at http://127.0.0.1:8090/airband.html.
"""

import argparse
import json
import logging
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
from pathlib import Path

import numpy as np
from ka9q import RadiodControl, SpectrumStream

from rxapp.addr import maddr
from rxapp.config import CONFIG

log = logging.getLogger("airband")
API = f"http://{CONFIG.host}:{CONFIG.port}/api"

GRID_HZ = 25e3
AIR_LO, AIR_HI = 118.0e6, 136.975e6
BIN_HZ = 2050.0
BINS = 4096                      # 8.4 MHz: the tuner's usable window
WIDE_PEAK_DB = 10.0              # how far a spilling voice carrier stands above its neighbours
NEIGHBOURS = 3                   # grid channels either side a carrier can spill into
OPEN_DB = 12.0                   # carrier this far above the window's median floor = keyed
HANG_SEC = 0.7                   # carrier gone this long = transmission over
MIN_TX_SEC = 0.6
PRE_SEC, POST_SEC = 0.4, 0.4
SPUR_FRACTION = 0.9              # keyed this much of the last SPUR_WINDOW s = a spur, not voice
SPUR_WINDOW = 60.0
MAX_CHANNELS = 30
WHISPER = "mlx-community/whisper-large-v3-turbo"
PROMPT = ("Air traffic control radio, Northern California. NorCal Approach, Oakland Center, San Francisco "
          "tower. United, Southwest, Alaska, SkyWest, Delta, American, JetBlue. Descend and maintain, "
          "climb and maintain, turn left heading, reduce speed to 210, cleared visual approach runway "
          "two eight left, contact tower, traffic twelve o'clock, altimeter, roger, wilco.")
HALLUCINATIONS = {"", "thank you.", "thanks for watching!", "you", "bye.", ".", "thank you very much."}


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
        self.rate = {}
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


def prepare(a: np.ndarray, rate: int = 12000) -> np.ndarray:
    """AM audio -> what Whisper hears best: carrier DC removed, voice band only, normalised, 16 kHz.
    (Fed the raw WAV, Whisper called nearly every transmission 'no speech': the carrier's DC level
    and slow AGC swings swamp the voice.)"""
    from scipy.signal import butter, resample_poly, sosfilt
    x = a.astype(np.float32) / 32768
    x = sosfilt(butter(4, [250, 3200], btype="band", fs=rate, output="sos"), x - x.mean())
    x = x / (np.abs(x).max() + 1e-9) * 0.8
    return resample_poly(x, 4, 3).astype(np.float32)          # 12 kHz -> 16 kHz


def transcribe(a: np.ndarray) -> dict:
    """Two readings: plain, and with an ATC vocabulary prompt (it helps with call signs and phrases,
    but can also pull a garbled transmission towards stock phrases)."""
    import mlx_whisper
    x = prepare(a)
    out = {}
    for key, prompt in (("text", None), ("text_atc", PROMPT)):
        r = mlx_whisper.transcribe(x, path_or_hf_repo=WHISPER, language="en", initial_prompt=prompt,
                                   condition_on_previous_text=False, temperature=0.0)
        segs = r.get("segments") or []
        text = " ".join(s["text"].strip() for s in segs if s.get("no_speech_prob", 0) < 0.8).strip()
        caption = re.fullmatch(r"[\[(]?[A-Z][A-Z ,.'!-]*[\])]?", text) is not None   # "BELL RINGS": a sound caption
        out[key] = "" if text.lower() in HALLUCINATIONS or caption else text
    return out


class Transcriber(threading.Thread):
    def __init__(self, out_path):
        super().__init__(daemon=True)
        self.q = queue.Queue()
        self.out = out_path
        self.done = 0

    def run(self):
        while True:
            rec = self.q.get()
            if rec is None:
                return
            try:
                with wave.open(rec["wav"]) as w:
                    rec.update(transcribe(np.frombuffer(w.readframes(w.getnframes()), "<i2")))
            except Exception as e:
                rec["text"], rec["error"] = "", str(e)
            with open(self.out, "a") as f:
                f.write(json.dumps(rec) + "\n")
            self.done += 1
            log.info("[%s] %.3f %4.1fs %+.0fdB  %s", time.strftime("%H:%M:%S", time.localtime(rec["t0"])),
                     rec["freq_hz"] / 1e6, rec["t1"] - rec["t0"], rec["peak_db"], rec["text"] or "(no speech)")


class Session:
    def __init__(self, center_hz, minutes, out_dir):
        self.center_hz, self.minutes = center_hz, minutes
        self.out = out_dir
        self.out.mkdir(parents=True, exist_ok=True)
        self.lo = max(AIR_LO, center_hz - BINS * BIN_HZ / 2 * 0.98)
        self.hi = min(AIR_HI, center_hz + BINS * BIN_HZ / 2 * 0.98)
        g0 = np.ceil(self.lo / GRID_HZ) * GRID_HZ
        self.grid = np.arange(g0, self.hi + 1, GRID_HZ)
        self.channels = {}            # freq -> ssrc
        self.spurs = set()
        self.keyed_since = {}         # freq -> start time of the current transmission
        self.last_keyed = {}
        self.peak = {}
        self.history = defaultdict(deque)   # freq -> (t, keyed) for spur detection
        self.frames = queue.Queue()
        self.n_tx = 0
        self.end = None

    # -- setup / teardown ------------------------------------------------------------
    def start(self):
        info = call("GET", "/info")
        self.prev_band = info["band"]
        call("POST", "/band", {"band": "vhf", "center_hz": self.center_hz})
        time.sleep(2)
        info = call("GET", "/info")
        self.tuner_hz, self.if_hz = info["vhf"]["center_hz"], info["vhf"]["if_hz"]
        log.info("VHF tuner at %.3f MHz; watching %.3f-%.3f MHz (%d channels on the 25 kHz grid)",
                 self.tuner_hz / 1e6, self.lo / 1e6, self.hi / 1e6, len(self.grid))
        self.ctl = RadiodControl(maddr(CONFIG.status_name), client_id="airband", interface=CONFIG.iface_addr)
        self.audio = Audio(maddr(CONFIG.pcm_name))
        self.tx = Transcriber(self.out / "transmissions.jsonl")
        self.tx.start()
        self._open_spectrum()

    def _open_spectrum(self):
        def on(st):
            db = st.spectrum.bin_power_db
            if db is not None and len(db):
                self.frames.put((time.time(), np.asarray(db, float)))
        self.stream = SpectrumStream(control=self.ctl, frequency_hz=self.if_hz, bin_count=BINS,
                                     resolution_bw=BIN_HZ, on_spectrum=on)
        self.stream.start()
        # radiod's IF is inverted: RF = tuner + (IF_centre - f_if); bin i sits at f_if = IF + (i - N/2) * bin
        rf_of_bin = self.tuner_hz - (np.arange(BINS) - BINS // 2) * BIN_HZ
        self.bin_of = {f: int(np.argmin(np.abs(rf_of_bin - f))) for f in self.grid}

    def stop(self):
        (self.out / "status.json").unlink(missing_ok=True)
        try:
            self.stream.stop()
        except Exception:
            pass
        for f, ssrc in list(self.channels.items()):
            try:
                call("DELETE", f"/channels/{ssrc}")
            except Exception as e:
                log.warning("remove %.3f: %s", f / 1e6, e)
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
    def ensure_channel(self, f):
        if f in self.channels or f in self.spurs or len(self.channels) >= MAX_CHANNELS:
            return
        try:
            ch = call("POST", "/channels", {"freq_hz": float(f), "mode": "am", "label": f"AIR {f / 1e6:.3f}",
                                            "band": "vhf", "kind": "air"})
        except Exception as e:
            log.warning("channel %.3f: %s", f / 1e6, e)
            return
        self.channels[f] = ch["ssrc"]
        self.audio.wanted.add(ch["ssrc"])
        log.info("new channel %.3f MHz (%d)", f / 1e6, len(self.channels))

    def drop_spur(self, f):
        self.spurs.add(f)
        ssrc = self.channels.pop(f, None)
        if ssrc:
            call("DELETE", f"/channels/{ssrc}")
        log.info("%.3f MHz is on all the time: a spur, ignoring it", f / 1e6)

    def write_status(self, now):
        """status.json: what rxapp's live airband page shows (channels, who is on the air now)."""
        st = {"t": now, "tuner_hz": self.tuner_hz, "lo": self.lo, "hi": self.hi, "transmissions": self.n_tx,
              "channels": {f"{f:.0f}": ssrc for f, ssrc in self.channels.items()},
              "keyed": {f"{f:.0f}": round(self.peak.get(f, 0), 1) for f in self.keyed_since},
              "spurs": [f"{f:.0f}" for f in sorted(self.spurs)], "end": self.end}
        tmp = self.out / "status.json.tmp"
        tmp.write_text(json.dumps(st))
        tmp.replace(self.out / "status.json")

    # -- main loop --------------------------------------------------------------------
    def run(self, preload=()):
        for f in preload:                       # channels already known to be busy: ready before they key up
            g = round(f / GRID_HZ) * GRID_HZ
            if self.lo <= g <= self.hi:
                self.ensure_channel(g)
        self.end = time.time() + self.minutes * 60 if self.minutes > 0 else None
        last_frame = last_status = time.time()
        while self.end is None or time.time() < self.end:
            if time.time() - last_status >= 0.5:
                self.write_status(time.time())
                last_status = time.time()
            try:
                t, db = self.frames.get(timeout=5)
            except queue.Empty:
                if time.time() - last_frame > 10:
                    log.warning("no spectrum for 10 s (radiod restarted?); reopening")
                    try:
                        self.stream.stop()
                    except Exception:
                        pass
                    self._open_spectrum()
                    last_frame = time.time()
                continue
            last_frame = t
            floor = float(np.median(db))
            levels = np.array([float(db[max(0, self.bin_of[f] - 1):self.bin_of[f] + 2].max()) - floor for f in self.grid])
            for k, f in enumerate(self.grid):
                level = levels[k]
                # A strong carrier spills into the neighbouring 25 kHz channels; only the strongest
                # channel within +-NEIGHBOURS counts, or one transmission turns into five.
                lo, hi = max(0, k - NEIGHBOURS), k + NEIGHBOURS + 1
                near = np.sort(levels[lo:hi])
                keyed = level >= OPEN_DB and f not in self.spurs and level >= near[-1]
                # Lit across 3+ channels at once with no clear peak: a wide non-voice signal (data,
                # interference), not AM voice, which fits in one channel. Voice strong enough to spill
                # stands well above its neighbours.
                if keyed and (near >= OPEN_DB).sum() >= 3 and level - near[-2] < WIDE_PEAK_DB:
                    keyed = False
                h = self.history[f]
                h.append((t, keyed))
                while h and t - h[0][0] > SPUR_WINDOW:
                    h.popleft()
                if keyed:
                    self.last_keyed[f] = t
                    self.peak[f] = max(self.peak.get(f, -99), level)
                    if f not in self.keyed_since:
                        self.keyed_since[f] = t
                        self.ensure_channel(f)
                    if t - h[0][0] > SPUR_WINDOW * 0.9 and sum(k for _, k in h) / len(h) > SPUR_FRACTION:
                        self.keyed_since.pop(f, None)
                        self.drop_spur(f)
                elif f in self.keyed_since and t - self.last_keyed[f] >= HANG_SEC:
                    self.finish(f, self.keyed_since.pop(f), self.last_keyed[f])

    def finish(self, f, t0, t1):
        peak = self.peak.pop(f, 0.0)
        if t1 - t0 < MIN_TX_SEC or f not in self.channels:
            return
        ssrc = self.channels[f]
        a = self.audio.cut(ssrc, t0 - PRE_SEC, t1 + POST_SEC, 12000)
        if len(a) < 0.3 * 12000:
            return
        self.n_tx += 1
        path = self.out / f"{time.strftime('%H%M%S', time.localtime(t0))}_{f / 1e3:.0f}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(12000)
            w.writeframes(a.tobytes())
        self.tx.q.put({"t0": t0, "t1": t1, "freq_hz": float(f), "peak_db": round(peak, 1), "wav": str(path)})


def retranscribe(session_dir: Path):
    """Transcribe a session's WAVs again (e.g. after improving prepare()) into transcripts.jsonl."""
    recs = [json.loads(l) for l in open(session_dir / "transmissions.jsonl")]
    out = session_dir / "transcripts.jsonl"
    done = {json.loads(l)["wav"] for l in open(out)} if out.exists() else set()
    with open(out, "a") as f:
        for i, rec in enumerate(sorted(recs, key=lambda r: r["t0"])):
            if rec["wav"] in done:
                continue
            with wave.open(rec["wav"]) as w:
                rec.update(transcribe(np.frombuffer(w.readframes(w.getnframes()), "<i2")))
            f.write(json.dumps(rec) + "\n")
            f.flush()
            print(f"{i + 1}/{len(recs)} {rec['freq_hz'] / 1e6:.3f} {rec['text']}", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--center", type=float, default=135.7e6, help="tuner centre, Hz (window is +-4.2 MHz)")
    ap.add_argument("--minutes", type=float, default=0, help="0 (default): until stopped (Ctrl-C / SIGTERM)")
    ap.add_argument("--preload", default="", help="comma-separated MHz to open channels on right away")
    ap.add_argument("--retranscribe", type=Path, help="transcribe this session directory's WAVs again")
    args = ap.parse_args()
    if args.retranscribe:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        return retranscribe(args.retranscribe)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = CONFIG.data_dir / "airband" / stamp
    out.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S",
                        handlers=[logging.StreamHandler(), logging.FileHandler(out / "session.log")])
    for noisy in ("ka9q", "httpx", "huggingface_hub"):     # ka9q logs every spectrum poll at INFO
        logging.getLogger(noisy).setLevel(logging.WARNING)
    s = Session(args.center, args.minutes, out)
    signal.signal(signal.SIGTERM, lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
    s.start()
    try:
        s.run([float(x) * 1e6 for x in args.preload.split(",") if x.strip()])
    except KeyboardInterrupt:
        pass
    finally:
        s.stop()
    log.info("done: %d transmissions in %s", s.n_tx, out)


if __name__ == "__main__":
    main()
