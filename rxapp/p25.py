# SPDX-License-Identifier: GPL-3.0-or-later
"""700 MHz P25 trunked radio: dsd-fme fed from radiod, every call decoded.

The region's 700 MHz system (heard here as WACN BEE00, SYSID 5E4, NAC 5E1) is a Motorola simulcast system:
a control channel on 772.30625 MHz, voice on Phase 2 TDMA channels around 772.6-774.7 MHz. Everything fits
in the RX-888's ~4 MHz tuner window centred on 773.45 MHz, so radiod can give each one its own channel.

    radiod "iq" channel on the control channel -> CqpskDemod --TCP--> dsd-fme -ft -mc (grants; doesn't follow them)
    a grant to a clear call on a voice channel with no decoder yet -> a decoder for it:
    radiod "iq" channel on that frequency      -> CqpskDemod --TCP--> dsd-fme -f2 -X <wacn sysid nac> -mc
        per-call WAVs (-P -7 calls/), audio to UDP VOICE_PORT + k (rxapp/p25live.py), console -> Trunk -> p25.json

Voice decoders stay up while their channel is used (closed after VOICE_IDLE_SEC without a grant). One dsd-fme
following the trunk (the earlier design) could decode one call at a time and missed every grant while on a call:
in two minutes of the same traffic it recorded 4 calls, three fixed voice decoders 15.

The demodulator is ours, not radiod's FM discriminator: the system is simulcast (several transmitters on one
frequency, so the received phase wanders between symbols), and the discriminator's instantaneous frequency
read that as symbol errors. A matched (root-raised-cosine) filter and the phase change over one symbol period
turn CQPSK into the four C4FM-like levels dsd-fme slices with -mc. On the same 10-minute capture (rxapp_data/p25/
20261126-180346): error-free voice frames 16% -> 94%, voice frames decoded +78%, control-channel messages
395 -> 4131 and call grants 5 -> 213 (most calls were never followed before).

Encrypted calls decode to noise until dsd-fme learns their ALG ID; they are labelled from the voice channel's
own messages (Trunk), not transcribed, and never played live.

Calls on encrypted talkgroups are granted on the control channel (so they show in the activity) but can't be
heard. Recordings stay in rxapp_data/p25 (pruned like the public-safety ones) and are never published.
"""
import json
import logging
import re
import socket
import struct
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

import numpy as np

log = logging.getLogger("p25")

ROOT = Path(__file__).resolve().parent.parent
DSD_FME = ROOT / "tools" / "dsd-fme" / "build" / "dsd-fme"
CC_HZ = 772.30625e6                # control channel
CENTRE_HZ = 773.45e6               # tuner centre: control channel and every voice channel seen inside
AUDIO_PORT, VOICE_PORT = 17355, 23471   # local only: dsd-fme inputs AUDIO_PORT (+1+k), voice outputs VOICE_PORT (+k)
CAPTURE_SEC = 600                  # raw IQ (230 MB) and dsd-fme input (58 MB) kept per session, for offline tests
FS = 48000                         # IQ and demodulated audio rate
CC_SYMBOL_RATE, VOICE_SYMBOL_RATE = 4800, 6000      # Phase 1 control channel, Phase 2 TDMA voice channels
TG_KEEP_SEC = 3600                 # talkgroups listed if heard within the hour
SLOT_FRESH_SEC = 4.0               # a slot's talkgroup counts as current this long after the channel named it
ANSI = re.compile(r"\x1b\[[0-9;]*m")


# ---- dsd-fme console parsing ----------------------------------------------------------------
class Trunk:
    """What dsd-fme prints, turned into state: the system, the channel plan (identifier updates), and the
    grants, i.e. which talkgroup is talking on which voice channel, encrypted or not. Pure: fed lines."""

    SYNC = re.compile(r"Sync: [+-]?P25p([12]).*?WACN: (\w+); SYS: (\w+); NAC/CC: (\w+); RFSS: (\w+); Site: (\w+);")
    IDEN = re.compile(r"Channel Identifier \[(\d+)\]")
    BASE = re.compile(r"Channel Spacing \[(\w+)\] Base Frequency \[\w+\] \[(\d+)\]")
    GRANT = re.compile(r"SVC \[(\w+)\] CHAN \[(\w+)\] Group \[(\d+)\] Source \[(\d+)\]")
    UPDATE = re.compile(r"Channel \d \[(\w+)\] Group \d \[(\d+)\]")
    VCH = re.compile(r"VCH (\d) - TG (\d+) SRC (\d+)")
    VCH_SVC = re.compile(r"VCH (\d) - TG: (\d+); SRC: \d+;\s+(Encrypted )?Group Voice")    # MAC_ACTIVE / MAC_PTT
    VCH_ALG = re.compile(r"VCH (\d) - ALG ID: 0x([0-9A-F]{2})")
    ALG = re.compile(r"ALG ID: 0x([0-9A-F]{2})")

    def __init__(self):
        self.system = {}
        self.phase = None
        self.last_sync = 0.0
        self.syncs = deque()                     # sync times over the last minute (decode health)
        self.iden = {}                           # identifier -> {"base", "spacing", "tdma"}
        self._iden_pending = None                # (identifier, tdma) waiting for its base frequency line
        self._enc_next = False
        self.tgs = {}                            # talkgroup -> {"tg", "first", "last", "grants", "enc", "src", "hz"}
        self.call_times = {}                     # talkgroup -> call start times within TG_KEEP_SEC
        self.grants = deque(maxlen=200)          # recent grants, newest last
        self.enc_calls = deque(maxlen=2000)      # (t, talkgroup) seen encrypted on a voice channel
        self._slot_tg = {}                       # TDMA slot -> (talkgroup its last MAC message named, when)
        self.clear_tgs = set()                   # talkgroups seen with a clear (not encrypted) voice call
        self._pending_slot = None                # a MAC_PTT whose ALG ID line comes next
        self.voice_sync = {}                     # voice decoder -> when it last synced (it carries a call)
        self.on_grant = None                     # fn(tg, hz, enc, now), called for every grant and update

    def freq(self, chan: int) -> Optional[float]:
        """Channel number (4-bit identifier, 12-bit channel) to Hz, once that identifier's update was heard."""
        i = self.iden.get(chan >> 12)
        if not i:
            return None
        n = chan & 0xFFF
        if i["tdma"]:
            n //= 2                              # two TDMA slots per carrier
        return i["base"] + n * i["spacing"]

    def feed(self, line: str, now: float, chan=None):
        """One console line. chan: the voice decoder it came from (None: the control channel). TDMA slots are
        per voice channel, so they are kept as "<chan>:<slot>"."""
        line = ANSI.sub("", line).rstrip()
        # a MAC_PTT's ALG ID is the line right after its "VCH n - TG" line; any later ALG line (e.g. the MI(128)
        # continuation of the other slot's ESSB) is not about it
        pending, self._pending_slot = self._pending_slot, None
        m = self.SYNC.search(line)
        if m and chan is not None:
            self.voice_sync[chan] = now
            return
        if m:
            self.phase = int(m.group(1))
            self.system = dict(zip(("wacn", "sysid", "nac", "rfss", "site"), m.groups()[1:]))
            self.last_sync = now
            self.syncs.append(now)
            while self.syncs and now - self.syncs[0] > 60:
                self.syncs.popleft()
            return
        if "Identifier Update" in line:
            self._iden_pending = ("TDMA" in line, None)
            return
        m = self.IDEN.search(line)
        if m and self._iden_pending is not None:
            self._iden_pending = (self._iden_pending[0], int(m.group(1)))
            return
        m = self.BASE.search(line)
        if m and self._iden_pending and self._iden_pending[1] is not None:
            tdma, ident = self._iden_pending
            self.iden[ident] = {"base": float(m.group(2)), "spacing": int(m.group(1), 16) * 125.0, "tdma": tdma}
            self._iden_pending = None
            return
        # the voice channel's own word on encryption: "VCH 1 - TG: 1451; SRC: ...; Encrypted Group Voice"
        key = (lambda n: n if chan is None else f"{chan}:{n}")
        m = self.VCH_SVC.search(line)
        if m:
            slot, tg = key(int(m.group(1))), int(m.group(2))
            if tg:
                self._slot_tg[slot] = (tg, now)
                if m.group(3):
                    self._alg(tg, "84", now)
                else:
                    self.clear_tgs.add(tg)
            return
        m = self.VCH.search(line)
        if m:
            slot = key(int(m.group(1)))
            if int(m.group(2)):
                self._slot_tg[slot] = (int(m.group(2)), now)
            self._pending_slot = slot
            return
        m = self.VCH_ALG.search(line)                 # "VCH 1 - ALG ID: 0x84 ..." (from the call's ESS)
        if m:
            tg, t = self._slot_tg.get(key(int(m.group(1))), (None, 0))
            if now - t < 3:                           # only a slot named just now: an older name may be stale
                self._alg(tg, m.group(2), now)
            return
        m = self.ALG.search(line)                     # the line after a MAC_PTT: "ALG ID: 0x84 ... MPTT"
        if m and pending is not None:
            self._alg(self._slot_tg.get(pending, (None, 0))[0], m.group(1), now)
            return
        if "Encrypted" in line and "Grant" in line:
            self._enc_next = True
            return
        m = self.GRANT.search(line)
        if m:
            svc, chan, tg, src = int(m.group(1), 16), int(m.group(2), 16), int(m.group(3)), int(m.group(4))
            self._grant(tg, chan, now, src=src, enc=bool(svc & 0x40) or self._enc_next)
            self._enc_next = False
            return
        m = self.UPDATE.search(line)
        if m:
            self._grant(int(m.group(2)), int(m.group(1), 16), now, enc=self._enc_next, update=True)
            self._enc_next = False

    def _alg(self, tg, alg, now):
        if tg is None or alg in ("80", "00"):          # 0x80 = clear
            return
        self.enc_calls.append((now, tg))
        if tg in self.tgs:
            self.tgs[tg]["enc"] = True

    def _grant(self, tg, chan, now, src=None, enc=False, update=False):
        if tg == 0:
            return
        t = self.tgs.get(tg)
        hz = self.freq(chan)
        new = t is None
        if new:
            t = self.tgs[tg] = {"tg": tg, "first": now, "last": now, "grants": 0, "enc": enc, "src": None, "hz": hz}
        # an update repeats a grant while the call goes on: count it once per call (a gap of 5 s or more)
        if new or not update or now - t["last"] > 5:
            t["grants"] += 1
            times = self.call_times.setdefault(tg, [])        # the hour's call starts, for counts since a reset
            times.append(round(now, 1))
            while times and now - times[0] > TG_KEEP_SEC:
                times.pop(0)
            self.grants.append({"t": round(now, 1), "tg": tg, "src": src, "hz": hz, "enc": enc})
        t["last"], t["enc"] = now, t["enc"] or enc
        t["hz"] = hz or t["hz"]
        if src:
            t["src"] = src
        if self.on_grant:
            self.on_grant(tg, hz, enc, now)

    def snapshot(self, now: float) -> dict:
        enc = self.enc_tgs()
        tgs = sorted((dict(t, active=now - t["last"] < 3, times=self.call_times.get(t["tg"], []),
                           enc=t["tg"] in enc or (t["enc"] and t["tg"] not in self.clear_tgs))
                      for t in self.tgs.values() if now - t["last"] < TG_KEEP_SEC),
                     key=lambda t: -t["last"])
        return {"system": self.system, "phase": self.phase, "locked": now - self.last_sync < 3,
                "sync_age": round(now - self.last_sync, 1) if self.last_sync else None,
                "syncs_per_min": len(self.syncs), "channel_plan": len(self.iden),
                "talkgroups": [{k: (round(v, 1) if isinstance(v, float) else v) for k, v in t.items()} for t in tgs],
                "grants": list(self.grants)[-50:][::-1],
                "enc_calls": [[round(t, 1), tg] for t, tg in self.enc_calls if now - t < TG_KEEP_SEC],
                "enc_tgs": sorted(self.enc_tgs()),
                # each TDMA slot's talkgroup, while the voice channel keeps naming it (for live listening)
                "slots": {str(k): {"tg": tg, "age": round(now - t, 1), "enc": tg in self.enc_tgs()}
                          for k, (tg, t) in self._slot_tg.items() if now - t < SLOT_FRESH_SEC}}

    def enc_tgs(self) -> set:
        """Talkgroups heard encrypted and never in the clear: all their calls are labelled encrypted."""
        return ({tg for tg, t in self.tgs.items() if t["enc"]} | {tg for _, tg in self.enc_calls}) - self.clear_tgs


CALL_RE = re.compile(r"(\d{8}_\d{6})_\d+_P25_\w*?_GROUP_TGT_(\d+)_SRC_(\d+)\.wav$")


def list_calls(calls_dir: Path, now: Optional[float] = None, limit: int = 300, enc_calls=(), enc_tgs=()):
    """Finished per-call recordings (dsd-fme -P), newest first. A file still being written (changed in the last
    2 s) isn't finished; dsd-fme's catch-all file for calls it couldn't attribute (TGT_0) is left out."""
    now = now or time.time()
    out = []
    for p in calls_dir.glob("*.wav") if calls_dir.exists() else []:
        m = CALL_RE.search(p.name)
        st = p.stat()
        if not m or int(m.group(2)) == 0 or now - st.st_mtime < 2:
            continue
        t0 = time.mktime(time.strptime(m.group(1), "%Y%m%d_%H%M%S"))
        secs = round(max(0, st.st_size - 44) / 16000, 1)
        tg = int(m.group(2))
        # encrypted: the voice channel named this talkgroup with a non-clear ALG ID during the call. dsd-fme
        # often learns that a few frames in, so the recording is noise; such calls are listed, not played.
        enc = tg in enc_tgs or any(etg == tg and t0 - 3 <= et <= t0 + secs + 3 for et, etg in enc_calls)
        out.append({"t": t0, "tg": tg, "src": int(m.group(3)), "secs": secs, "clip": p.name, "enc": enc})
    return sorted(out, key=lambda c: -c["t"])[:limit]


THREAD_GAP_SEC = 30.0               # calls on one talkgroup this close together: one conversation


def threads(calls, now: Optional[float] = None, gap: float = THREAD_GAP_SEC):
    """Calls (list_calls, newest first) grouped into conversations: one talkgroup, each call starting less than
    `gap` seconds after the previous one ended. Newest first; a thread's calls oldest first. live: its last call
    ended less than `gap` ago (a reply may still come)."""
    now = now or time.time()
    open_by_tg, out = {}, []
    for c in sorted(calls, key=lambda c: c["t"]):
        th = open_by_tg.get(c["tg"])
        if th is None or c["t"] - th["end"] > gap:
            th = {"tg": c["tg"], "start": c["t"], "end": c["t"], "calls": []}
            open_by_tg[c["tg"]] = th
            out.append(th)
        th["calls"].append(c)
        th["end"] = max(th["end"], c["t"] + c["secs"])
    for th in out:
        cs = th["calls"]
        th["id"] = f'{th["tg"]}-{cs[0]["clip"]}'
        th["n"] = len(cs)
        th["secs"] = round(sum(c["secs"] for c in cs), 1)
        th["srcs"] = sorted({c["src"] for c in cs})
        th["enc"] = all(c["enc"] for c in cs)
        th["live"] = now - th["end"] < gap
    return sorted(out, key=lambda th: -th["end"])


# ---- demodulator ----------------------------------------------------------------------------
def rrc_taps(sps: int, alpha: float = 0.5, span: int = 6) -> np.ndarray:
    """Root-raised-cosine, unit energy. alpha 0.5 measured best here (0.2: 91% error-free voice frames, 0.5: 94%)."""
    t = np.arange(-span * sps, span * sps + 1) / sps
    h = np.empty_like(t)
    for i, x in enumerate(t):
        if x == 0:
            h[i] = 1 - alpha + 4 * alpha / np.pi
        elif abs(abs(4 * alpha * x) - 1) < 1e-9:
            h[i] = alpha / np.sqrt(2) * ((1 + 2 / np.pi) * np.sin(np.pi / (4 * alpha)) + (1 - 2 / np.pi) * np.cos(np.pi / (4 * alpha)))
        else:
            h[i] = (np.sin(np.pi * x * (1 - alpha)) + 4 * alpha * x * np.cos(np.pi * x * (1 + alpha))) / (np.pi * x * (1 - (4 * alpha * x) ** 2))
    return h / np.sqrt((h ** 2).sum())


class CqpskDemod:
    """Streaming CQPSK demodulator: IQ in, dsd-fme's "audio" out. Matched filter, then the phase change over one
    symbol: +-pi/4 and +-3pi/4 come out as +-1 and +-3 (times SCALE), the levels of a C4FM signal, at FS."""
    SCALE = 5000

    def __init__(self, fs: int = FS):
        self.fs = fs
        self.sps = None
        self.taps = {}

    def process(self, iq: np.ndarray, symbol_rate: int) -> np.ndarray:
        from scipy.signal import lfilter
        sps = self.fs // symbol_rate
        if sps != self.sps:                               # retuned between control and voice: start afresh
            self.sps = sps
            if sps not in self.taps:
                self.taps[sps] = rrc_taps(sps)
            self.zi = np.zeros(len(self.taps[sps]) - 1, complex)
            self.tail = np.zeros(sps, complex)
        iq = np.nan_to_num(np.asarray(iq, np.complex64))
        y, self.zi = lfilter(self.taps[sps], 1, iq, zi=self.zi)
        full = np.concatenate([self.tail, y])
        self.tail = full[-sps:]
        d = full[sps:] * np.conj(full[:-sps])
        s = np.angle(d) * (4 / np.pi) * self.SCALE
        return np.clip(s, -32000, 32000).astype(np.int16)


# ---- the receiver ---------------------------------------------------------------------------
MAX_VOICE = 8                      # voice decoders at most (six voice frequencies seen on this system)
VOICE_IDLE_SEC = 900.0             # a voice decoder whose frequency had no grant for this long is stopped
VOICE_SYNC_SEC = 2.0               # a voice decoder synced this recently is carrying a call


class _Decoder:
    """One radiod IQ channel, demodulated here (CqpskDemod) and streamed over TCP to its own dsd-fme."""

    def __init__(self, key, rf: float, symbol_rate: int, port: int, ssrc: int):
        import queue
        self.key, self.rf, self.symbol_rate, self.port, self.ssrc = key, rf, symbol_rate, port, ssrc
        self.demod = CqpskDemod()
        self.q = queue.SimpleQueue()
        self.clients, self.srv, self.proc = [], None, None
        self.started = self.last_grant = time.time()
        self.last_seq, self.lost = None, 0
        self.open = True


class P25Receiver:
    """While the P25 window is on: a decoder that stays on the control channel (dsd-fme reading grants, not
    following them), and one decoder per voice channel in use (dsd-fme on that frequency, Phase 2 with the
    system's WACN / SYSID / NAC given), started when the control channel first grants a clear call there.
    So every call on the system is decoded, however many happen at once, and no grant is missed while a call
    is being decoded (one dsd-fme following the trunk could do one call at a time)."""

    def __init__(self, out_dir: Path, ctl_factory=None):
        import queue
        self.out = out_dir
        self.calls = out_dir / "calls"
        self.calls.mkdir(parents=True, exist_ok=True)
        self.trunk = Trunk()
        self.trunk.on_grant = self._on_grant
        self.lock = threading.Lock()
        self.log_lock = threading.Lock()
        self.running = False
        self.error = None
        self.last_write = 0.0
        self.decs = {}                     # key ("cc", or voice decoder number 0..MAX_VOICE-1) -> _Decoder
        self.by_ssrc = {}
        self.want = queue.Queue()          # voice frequencies granted that have no decoder yet
        self.voice_started = 0
        self.started = 0.0
        self.restarts = 0
        self.gen = 0
        self.capture, self.capture_left = None, 0
        self.iq_file, self.iq_left = None, 0
        self.lvl_n = self.lvl_clip = 0
        self.lvl_sq = 0.0
        self.backlog = 0
        self.rtp = None
        self.dec_lock = threading.RLock()  # opening / closing decoders (the manager thread and the watchdog)

    # ---- start / stop -------------------------------------------------------------------------------
    def start(self, ctl, tuner_hz: float, if_hz: float, maddr_fn, rtp_port: int, iface: str):
        if self.running:
            self.stop()                           # back in the window: fresh channels at the current IF
        if not DSD_FME.exists():
            self.error = f"dsd-fme not built ({DSD_FME}); see rxapp/README.md"
            log.warning(self.error)
            return
        self.ctl, self.tuner_hz, self.if_hz = ctl, tuner_hz, if_hz
        self._net = (maddr_fn, rtp_port, iface)
        self.started = time.time()
        self.group = maddr_fn("voice-p25.local")
        self.ssrc_base = int(time.time()) % 100_000_000 * 16 + 9_100_000
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16 << 20)
        s.bind((self.group, rtp_port))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(self.group) + socket.inet_aton(iface))
        s.settimeout(1.0)
        self.rtp = s
        # the first CAPTURE_SEC of the control channel's IQ (48 kHz complex64) and of dsd-fme's input (48 kHz
        # s16le), for offline tests of the demodulator and of decoder settings
        if not (self.out / "iq.raw").exists():
            self.iq_file = open(self.out / "iq.raw", "wb")
            self.iq_left = int(CAPTURE_SEC * FS)
        if not (self.out / "input.raw").exists():
            self.capture = open(self.out / "input.raw", "wb")
            self.capture_left = int(CAPTURE_SEC * 48000)
        self.running, self.error = True, None
        self.gen += 1                                   # threads of an earlier start see this and leave
        g = self.gen
        self.decs, self.by_ssrc = {}, {}
        self._open("cc", CC_HZ, CC_SYMBOL_RATE, AUDIO_PORT, g,
                   ["-ft", "-mc", "-o", "null", "-J", str(self.out / "events.log")])
        threading.Thread(target=self._pump, args=(g,), daemon=True).start()
        threading.Thread(target=self._manage, args=(g,), daemon=True).start()
        log.info("P25: control channel %.5f MHz, tuner %.3f MHz", CC_HZ / 1e6, tuner_hz / 1e6)

    def _open(self, key, rf, symbol_rate, port, g, args):
        import os
        ssrc = self.ssrc_base + (0 if key == "cc" else 1 + key)
        dec = _Decoder(key, rf, symbol_rate, port, ssrc)
        dec.srv = self._listen(port)
        # raw IQ (complex float32), no squelch (an IQ channel has none; the FM squelch used to cut 9-13% here)
        self.ctl.create_channel(frequency_hz=self._if(rf), preset="iq", sample_rate=FS, destination=self.group,
                                ssrc=ssrc, lifetime=1500, encoding=4, low_edge=-12000, high_edge=12000)
        self.decs[key], self.by_ssrc[ssrc] = dec, dec
        threading.Thread(target=self._accept, args=(dec, g), daemon=True).start()
        threading.Thread(target=self._feed, args=(dec, g), daemon=True).start()
        dec.proc = subprocess.Popen([str(DSD_FME), "-i", f"tcp:127.0.0.1:{port}", *args],
                                    cwd=self.out, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                    env={**os.environ, "DSD_FME_SKIP_ENC": "1"})
        threading.Thread(target=self._read, args=(dec, g), daemon=True).start()
        return dec

    def _open_voice(self, rf, g):
        """A decoder for voice channel rf: dsd-fme on Phase 2 only, with the system's TDMA parameters (it can't
        learn them on a voice channel alone), recording each call (-P) and sending its audio to VOICE_PORT + k."""
        with self.dec_lock:
            if not self._live(g) or any(d.key != "cc" and abs(d.rf - rf) < 1 for d in list(self.decs.values())):
                return None
            return self._open_voice_locked(rf, g)

    def _open_voice_locked(self, rf, g):
        sysinfo = self.trunk.system
        if not all(sysinfo.get(k) for k in ("wacn", "sysid", "nac")):
            return None
        k = next((i for i in range(MAX_VOICE) if i not in self.decs), None)
        if k is None:
            return None
        p2 = f"{sysinfo['wacn']}{sysinfo['sysid']}{sysinfo['nac']}"
        dec = self._open(k, rf, VOICE_SYMBOL_RATE, AUDIO_PORT + 1 + k, g,
                         ["-f2", "-X", p2, "-mc", "-o", f"udp:127.0.0.1:{VOICE_PORT + k}", "-P", "-7", str(self.calls)])
        self.voice_started += 1
        log.info("P25: voice decoder %d on %.5f MHz", k, rf / 1e6)
        return dec

    def _close(self, dec):
        with self.dec_lock:
            self._close_locked(dec)

    def _close_locked(self, dec):
        dec.open = False
        self.decs.pop(dec.key, None)
        self.by_ssrc.pop(dec.ssrc, None)
        for f in (lambda: self.ctl.set_channel_lifetime(dec.ssrc, 1), lambda: self.ctl.remove_channel(dec.ssrc),
                  lambda: dec.proc.terminate(), lambda: dec.proc.wait(3), lambda: dec.srv.close()):
            try:
                f()
            except Exception:
                pass
        for c in dec.clients:
            try:
                c.close()
            except OSError:
                pass
        dec.q.put(None)

    def stop(self):
        if not self.running:
            return
        self.running = False
        for dec in list(self.decs.values()):
            self._close(dec)
        for f in (lambda: self.capture.close(), lambda: self.iq_file.close(), lambda: self.rtp.close()):
            try:
                f()
            except Exception:
                pass
        log.info("P25: stopped")
        self.write(time.time() + 3)

    def _if(self, rf):
        return self.if_hz - (rf - self.tuner_hz)            # the tuner inverts: IF = IF_centre - (RF - tuner)

    @staticmethod
    def _listen(port):
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(1)
        srv.settimeout(1.0)
        return srv

    def _live(self, g, dec=None):
        return self.running and g == self.gen and (dec is None or dec.open)

    def _accept(self, dec, g):
        while self._live(g, dec):
            try:
                c, _ = dec.srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            dec.clients.append(c)

    # ---- the voice decoders: started on a grant, stopped when idle ----------------------------------
    def _on_grant(self, tg, hz, enc, now):
        """Trunk, for every grant (under self.lock): note it on that frequency's decoder, or ask for one."""
        if not hz or not self.running:
            return
        for dec in self.decs.values():
            if dec.key != "cc" and abs(dec.rf - hz) < 1:
                dec.last_grant = now
                return
        if not enc and abs(hz - self.tuner_hz) < 1.95e6:
            self.want.put(hz)

    def _manage(self, g):
        import queue
        while self._live(g):
            try:
                hz = self.want.get(timeout=1.0)
            except queue.Empty:
                continue
            if not self._live(g) or any(d.key != "cc" and abs(d.rf - hz) < 1 for d in list(self.decs.values())):
                continue
            try:
                if self._open_voice(hz, g) is None and len(self.decs) > MAX_VOICE:
                    log.info("P25: %.5f MHz granted but all %d voice decoders are busy", hz / 1e6, MAX_VOICE)
            except Exception as e:
                log.warning("P25: voice decoder on %.5f MHz: %s", hz / 1e6, e)

    # ---- data path ----------------------------------------------------------------------------------
    def _pump(self, g):
        """Receive only: every decoder's packets (one multicast group, told apart by SSRC) into its queue.
        Demodulating and feeding dsd-fme (which can block when it reads slowly) happen in _feed, so the socket is
        always drained (a blocked receive loop let the UDP buffer overflow: frames and calls lost)."""
        last_keep = time.time()
        while self._live(g):
            if time.time() - last_keep > 10:
                for dec in list(self.decs.values()):
                    try:
                        self.ctl.set_channel_lifetime(dec.ssrc, 1500)
                    except Exception:
                        pass
                last_keep = time.time()
            try:
                d, _ = self.rtp.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(d) < 12:
                continue
            dec = self.by_ssrc.get(struct.unpack("!I", d[8:12])[0])
            if dec is None:
                continue
            seq = struct.unpack("!H", d[2:4])[0]
            if dec.last_seq is not None:
                dec.lost += (seq - dec.last_seq - 1) & 0xFFFF
            dec.last_seq = seq
            dec.q.put(d)
        for dec in list(self.decs.values()):
            dec.q.put(None)

    def _feed(self, dec, g):
        """Everything waiting in the queue at once: one conversion and one send per batch (per packet, Python
        fell ~5% behind real time, so dsd-fme's input lagged further and further)."""
        import queue as _q
        q = dec.q
        while True:
            items = [q.get()]
            try:
                while len(items) < 2000:
                    items.append(q.get_nowait())
            except _q.Empty:
                pass
            if items[-1] is None or not self._live(g, dec):
                break
            self.backlog = max(self.backlog, len(items))
            parts = []
            for d in items:
                h = 12 + 4 * (d[0] & 15)
                if d[0] & 0x10:
                    h += 4 + 4 * struct.unpack("!H", d[h + 2:h + 4])[0]
                parts.append(d[h:])
            blob = b"".join(parts)
            blob = blob[:len(blob) // 8 * 8]
            if not blob:
                continue
            cc = dec.key == "cc"
            if cc and self.iq_left > 0:
                try:
                    self.iq_file.write(blob)               # complex float32 (I, Q), little-endian
                    self.iq_left -= len(blob) // 8
                    if self.iq_left <= 0:
                        self.iq_file.close()
                except (OSError, ValueError):
                    self.iq_left = 0
            a = dec.demod.process(np.frombuffer(blob, "<c8"), dec.symbol_rate)
            if cc:
                self.lvl_n += len(a)
                self.lvl_sq += float(np.dot(a.astype(np.float64), a.astype(np.float64)))
                self.lvl_clip += int(np.count_nonzero(np.abs(a.astype(np.int32)) >= 32000))
            pcm = a.astype("<i2").tobytes()
            if cc and self.capture_left > 0:          # the input as dsd-fme gets it, to replay offline
                try:
                    self.capture.write(pcm)
                    self.capture_left -= len(a)
                    if self.capture_left <= 0:
                        self.capture.close()
                except (OSError, ValueError):
                    self.capture_left = 0
            for c in list(dec.clients):
                try:
                    c.sendall(pcm)
                except OSError:
                    dec.clients.remove(c)

    CONSOLE_MAX = 20 << 20             # the decoders' console, kept for diagnosis (console.log, then .1)

    def _read(self, dec, g):
        path = self.out / "console.log"
        tag = "cc" if dec.key == "cc" else f"v{dec.key} {dec.rf / 1e6:.5f}"
        chan = None if dec.key == "cc" else dec.key
        for raw in dec.proc.stderr:
            line = raw.decode("latin-1", "replace")
            with self.lock:
                self.trunk.feed(line, time.time(), chan=chan)
            with self.log_lock:
                try:
                    with open(path, "a") as logf:
                        logf.write(f"{time.strftime('%H:%M:%S')} [{tag}] " + ANSI.sub("", line))
                    if path.stat().st_size > self.CONSOLE_MAX:
                        path.replace(path.with_suffix(".log.1"))
                except OSError:
                    pass
        if self._live(g, dec):
            if dec.key == "cc":
                self.error = f"dsd-fme exited ({dec.proc.poll()})"
            log.warning("P25: dsd-fme [%s] exited (%s)", tag, dec.proc.poll())

    RESTART_SEC = 30.0                 # no control-channel sync this long: restart everything

    def watchdog(self, now):
        """The control channel unheard for RESTART_SEC: start over. A voice decoder whose dsd-fme died is
        reopened; one whose frequency had no grant for VOICE_IDLE_SEC is closed."""
        if not self.running:
            return
        last = self.trunk.last_sync or self.started
        if now - last > self.RESTART_SEC and now - self.started > self.RESTART_SEC:
            log.warning("P25: no control-channel sync for %.0f s: restarting", now - last)
            self.restarts += 1
            self.start(self.ctl, self.tuner_hz, self.if_hz, *self._net)
            return
        for dec in list(self.decs.values()):
            if dec.key == "cc":
                continue
            if now - dec.last_grant > VOICE_IDLE_SEC:
                log.info("P25: voice decoder %d (%.5f MHz) idle: closed", dec.key, dec.rf / 1e6)
                self._close(dec)
            elif dec.proc.poll() is not None:
                rf = dec.rf
                self._close(dec)
                self._open_voice(rf, self.gen)

    def write(self, now):
        if now - self.last_write < 1:
            return
        self.watchdog(now)
        self.last_write = now
        with self.lock:
            snap = self.trunk.snapshot(now)
            vsync = dict(self.trunk.voice_sync)
        if self.lvl_n:
            snap["input"] = {"rms": round((self.lvl_sq / self.lvl_n) ** 0.5), "clip": round(self.lvl_clip / self.lvl_n, 5),
                             "lost_packets": sum(d.lost for d in self.decs.values()), "max_batch": self.backlog}
            self.backlog = 0
            self.lvl_n = self.lvl_clip = 0
            self.lvl_sq = 0.0
        voice = [{"k": d.key, "hz": d.rf, "active": now - vsync.get(d.key, 0) < VOICE_SYNC_SEC,
                  "up": round(now - d.started)} for d in sorted(self.decs.values(), key=lambda d: d.rf) if d.key != "cc"]
        snap.update(t=round(now, 1), running=self.running, error=self.error, cc_hz=CC_HZ, tuned_hz=CC_HZ if self.running else None,
                    tunes=self.voice_started, restarts=self.restarts, voice=voice, on_voice=any(v["active"] for v in voice))
        tmp = self.out / "p25.json.tmp"
        tmp.write_text(json.dumps(snap))
        tmp.replace(self.out / "p25.json")
