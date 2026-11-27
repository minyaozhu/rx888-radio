# SPDX-License-Identifier: GPL-3.0-or-later
"""700 MHz P25 trunked radio: dsd-fme fed from radiod, following the trunk.

The region's 700 MHz system (heard here as WACN BEE00, SYSID 5E4, NAC 5E1) is a Motorola simulcast system:
a control channel on 772.30625 MHz, voice on Phase 2 TDMA channels around 772.6-774.7 MHz. Everything fits
in the RX-888's ~4 MHz tuner window centred on 773.45 MHz, so following a call is only a radiod retune.

    radiod "nfm" channel (flat FM discriminator, 48 kHz)  --TCP-->  dsd-fme -i tcp  (-mq: simulcast CQPSK)
    dsd-fme rigctl client ("F <Hz>")                       --TCP-->  P25Receiver: retunes that channel
    dsd-fme per-call WAVs (-P -7 calls/), event log (-J), console (stderr)  -> parsed here -> p25.json

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
AUDIO_PORT, RIG_PORT, VOICE_PORT = 17355, 14532, 23471   # local only; off SDR++'s defaults (7355, 4532)
CAPTURE_SEC = 600                  # raw input kept per session (57.6 MB), for offline decoder comparisons
TG_KEEP_SEC = 3600                 # talkgroups listed if heard within the hour
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
        self._slot_tg = {}                       # TDMA slot -> the talkgroup its last MAC_PTT named
        self._pending_slot = None                # a MAC_PTT whose ALG ID line comes next

    def freq(self, chan: int) -> Optional[float]:
        """Channel number (4-bit identifier, 12-bit channel) to Hz, once that identifier's update was heard."""
        i = self.iden.get(chan >> 12)
        if not i:
            return None
        n = chan & 0xFFF
        if i["tdma"]:
            n //= 2                              # two TDMA slots per carrier
        return i["base"] + n * i["spacing"]

    def feed(self, line: str, now: float):
        line = ANSI.sub("", line).rstrip()
        m = self.SYNC.search(line)
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
        m = self.VCH.search(line)
        if m:
            slot = int(m.group(1))
            self._slot_tg[slot] = int(m.group(2))
            self._pending_slot = slot
            return
        m = self.VCH_ALG.search(line)                 # "VCH 1 - ALG ID: 0x84 ..." (from the call's ESS)
        if m:
            self._alg(self._slot_tg.get(int(m.group(1))), m.group(2), now)
            return
        m = self.ALG.search(line)                     # the line after a MAC_PTT: "ALG ID: 0x84 ... MPTT"
        if m and self._pending_slot is not None:
            self._alg(self._slot_tg.get(self._pending_slot), m.group(1), now)
            self._pending_slot = None
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

    def snapshot(self, now: float) -> dict:
        tgs = sorted((dict(t, active=now - t["last"] < 3, times=self.call_times.get(t["tg"], []))
                      for t in self.tgs.values() if now - t["last"] < TG_KEEP_SEC),
                     key=lambda t: -t["last"])
        return {"system": self.system, "phase": self.phase, "locked": now - self.last_sync < 3,
                "sync_age": round(now - self.last_sync, 1) if self.last_sync else None,
                "syncs_per_min": len(self.syncs), "channel_plan": len(self.iden),
                "talkgroups": [{k: (round(v, 1) if isinstance(v, float) else v) for k, v in t.items()} for t in tgs],
                "grants": list(self.grants)[-50:][::-1],
                "enc_calls": [[round(t, 1), tg] for t, tg in self.enc_calls if now - t < TG_KEEP_SEC]}


CALL_RE = re.compile(r"(\d{8}_\d{6})_\d+_P25_\w*?_GROUP_TGT_(\d+)_SRC_(\d+)\.wav$")


def list_calls(calls_dir: Path, now: Optional[float] = None, limit: int = 300, enc_calls=()):
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
        enc = any(etg == tg and t0 - 3 <= et <= t0 + secs + 3 for et, etg in enc_calls)
        out.append({"t": t0, "tg": tg, "src": int(m.group(3)), "secs": secs, "clip": p.name, "enc": enc})
    return sorted(out, key=lambda c: -c["t"])[:limit]


# ---- the receiver ---------------------------------------------------------------------------
class P25Receiver:
    """While the P25 window is on: a radiod flat-FM channel at the control channel, streamed to dsd-fme over
    TCP; dsd-fme's rigctl commands retune it to voice channels and back."""

    def __init__(self, out_dir: Path, ctl_factory=None):
        self.out = out_dir
        self.calls = out_dir / "calls"
        self.calls.mkdir(parents=True, exist_ok=True)
        self.trunk = Trunk()
        self.lock = threading.Lock()
        self.running = False
        self.error = None
        self.last_write = 0.0
        self.tuned_hz = None
        self.tunes = 0
        self.proc = None
        self.socks = []
        self.started = 0.0
        self.restarts = 0
        self.gen = 0
        self.capture, self.capture_left = None, 0
        self.iq_ssrc, self.iq_file, self.iq_left = None, None, 0
        self.lvl_n = self.lvl_clip = 0
        self.lvl_sq = 0.0
        self.lost = 0
        self.backlog = 0

    def start(self, ctl, tuner_hz: float, if_hz: float, maddr_fn, rtp_port: int, iface: str):
        if self.running:
            self.stop()                           # back in the window: a fresh channel at the current IF
        if not DSD_FME.exists():
            self.error = f"dsd-fme not built ({DSD_FME}); see rxapp/README.md"
            log.warning(self.error)
            return
        self.ctl, self.tuner_hz, self.if_hz = ctl, tuner_hz, if_hz
        self._net = (maddr_fn, rtp_port, iface)
        self.started = time.time()
        group = maddr_fn("voice-p25.local")
        self.ssrc = int(time.time()) % 1_000_000_000 + 9_100_000
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
        s.bind((group, rtp_port))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(group) + socket.inet_aton(iface))
        s.settimeout(1.0)
        self.rtp = s
        audio_srv = self._listen(AUDIO_PORT)
        rig_srv = self._listen(RIG_PORT)
        self.socks = [s, audio_srv, rig_srv]
        self.tuned_hz = CC_HZ
        ctl.create_channel(frequency_hz=self._if(CC_HZ), preset="nfm", sample_rate=48000, destination=group,
                           ssrc=self.ssrc, lifetime=1500)
        # no squelch: radiod's FM squelch cut the audio 9-13% of the time here, and dsd-fme lost the frames in
        # those gaps. radiod clamps its FM SNR estimate at 0, and simulcast (several transmitters, so the
        # envelope varies) often reads 0, which is below any finite dB threshold; -1000 dB underflows to a
        # threshold of exactly 0, which a 0 estimate never falls below.
        ctl.set_squelch(self.ssrc, enable=False, open_snr_db=-1000.0, close_snr_db=-1000.0)
        # the same channel as raw IQ (24 kHz complex float, retuned in step), captured for offline tests of
        # filters and demodulators; only while the capture runs
        self.iq_ssrc = self.ssrc + 1
        if not (self.out / "iq.raw").exists():
            ctl.create_channel(frequency_hz=self._if(CC_HZ), preset="iq", sample_rate=24000, destination=group,
                               ssrc=self.iq_ssrc, lifetime=1500, encoding=4, low_edge=-12000, high_edge=12000)
            self.iq_file = open(self.out / "iq.raw", "wb")
            self.iq_left = int(CAPTURE_SEC * 24000)
        self.clients = []
        # the first CAPTURE_SEC of each session's input (48 kHz s16le, retunes included), for A/B tests of
        # decoder settings on the same signal: dsd-fme -i input.wav ... (header written at the end)
        if not (self.out / "input.raw").exists():
            self.capture = open(self.out / "input.raw", "wb")
            self.capture_left = int(CAPTURE_SEC * 48000)
        self.running, self.error = True, None
        self.gen += 1                                   # threads of an earlier start see this and leave
        g = self.gen
        threading.Thread(target=self._accept_audio, args=(audio_srv, g), daemon=True).start()
        threading.Thread(target=self._accept_rig, args=(rig_srv, g), daemon=True).start()
        threading.Thread(target=self._pump, args=(g,), daemon=True).start()
        self.proc = subprocess.Popen(
            [str(DSD_FME), "-i", f"tcp:127.0.0.1:{AUDIO_PORT}", "-U", str(RIG_PORT), "-T", "-ft", "-mq",
             "-o", f"udp:127.0.0.1:{VOICE_PORT}", "-P", "-7", str(self.calls), "-J", str(self.out / "events.log")],
            cwd=self.out, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        threading.Thread(target=self._read, args=(self.proc, g), daemon=True).start()
        log.info("P25: control channel %.5f MHz, tuner %.3f MHz", CC_HZ / 1e6, tuner_hz / 1e6)

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

    def _live(self, g):
        return self.running and g == self.gen

    def _accept_audio(self, srv, g):
        while self._live(g):
            try:
                c, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.clients.append(c)

    def _accept_rig(self, srv, g):
        while self._live(g):
            try:
                c, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._rig, args=(c, g), daemon=True).start()

    def _rig(self, c, g):
        """The bit of the hamlib rigctl protocol dsd-fme uses: f (get), F <Hz> (set), M (mode), l (level)."""
        f = c.makefile("rb", buffering=0)
        while self._live(g):
            try:
                line = f.readline()
            except OSError:
                break
            if not line:
                break
            cmd = line.decode("ascii", "replace").strip()
            try:
                if cmd == "f":
                    c.sendall(f"{int(self.tuned_hz or 0)}\n".encode())
                elif cmd.startswith("F "):
                    rf = float(cmd.split()[1])
                    if abs(rf - self.tuner_hz) < 1.95e6:
                        self.ctl.set_frequency(self.ssrc, self._if(rf))
                        if self.iq_left > 0:
                            self.ctl.set_frequency(self.iq_ssrc, self._if(rf))
                        self.tuned_hz, self.tunes = rf, self.tunes + 1
                        c.sendall(b"RPRT 0\n")
                    else:
                        log.info("P25: %.5f MHz is outside the tuner window", rf / 1e6)
                        c.sendall(b"RPRT 1\n")
                elif cmd == "l":
                    c.sendall(b"-40.0\n")
                else:
                    c.sendall(b"RPRT 0\n")
            except OSError:
                break

    def _pump(self, g):
        """Receive only: radiod's packets into a queue. Writing the captures and feeding dsd-fme (which can
        block when dsd-fme reads slowly) happen in _feed, so the socket is always drained (a blocked receive
        loop let the UDP buffer overflow: 5-7% of the input lost, frames and calls broken)."""
        import queue
        self.q = q = queue.SimpleQueue()
        threading.Thread(target=self._feed, args=(q, g), daemon=True).start()
        last_keep, last_seq = time.time(), None
        while self._live(g):
            if time.time() - last_keep > 10:
                try:
                    self.ctl.set_channel_lifetime(self.ssrc, 1500)
                    if self.iq_left > 0:
                        self.ctl.set_channel_lifetime(self.iq_ssrc, 1500)
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
            ssrc = struct.unpack("!I", d[8:12])[0]
            if ssrc == self.ssrc:
                seq = struct.unpack("!H", d[2:4])[0]
                if last_seq is not None:
                    self.lost += (seq - last_seq - 1) & 0xFFFF
                last_seq = seq
            elif ssrc != self.iq_ssrc:
                continue
            q.put((ssrc, d))
        q.put(None)

    def _feed(self, q, g):
        """Everything waiting in the queue at once: one conversion, one capture write and one send per batch
        (per packet, Python fell ~5% behind real time, so dsd-fme's input lagged further and further)."""
        import queue as _q
        while True:
            items = [q.get()]
            try:
                while len(items) < 2000:
                    items.append(q.get_nowait())
            except _q.Empty:
                pass
            if items[-1] is None or not self._live(g):
                break
            self.backlog = max(self.backlog, len(items))
            pcm_parts, iq_parts = [], []
            for ssrc, d in items:
                h = 12 + 4 * (d[0] & 15)
                if d[0] & 0x10:
                    h += 4 + 4 * struct.unpack("!H", d[h + 2:h + 4])[0]
                (iq_parts if ssrc == self.iq_ssrc else pcm_parts).append(d[h:])
            if iq_parts and self.iq_left > 0:
                try:
                    blob = b"".join(iq_parts)
                    self.iq_file.write(blob)               # complex float32 (I, Q), little-endian
                    self.iq_left -= len(blob) // 8
                    if self.iq_left <= 0:
                        self.iq_file.close()
                        self.ctl.set_channel_lifetime(self.iq_ssrc, 1)
                        self.ctl.remove_channel(self.iq_ssrc)
                except (OSError, ValueError):
                    self.iq_left = 0
            if not pcm_parts:
                continue
            a = np.frombuffer(b"".join(pcm_parts), ">i2")
            self.lvl_n += len(a)
            self.lvl_sq += float(np.dot(a.astype(np.float64), a.astype(np.float64)))
            self.lvl_clip += int(np.count_nonzero(np.abs(a.astype(np.int32)) >= 32700))
            pcm = a.astype("<i2").tobytes()
            if self.capture_left > 0:                  # the input as dsd-fme gets it, to replay offline
                try:
                    self.capture.write(pcm)
                    self.capture_left -= len(a)
                    if self.capture_left <= 0:
                        self.capture.close()
                except (OSError, ValueError):
                    self.capture_left = 0
            for c in list(self.clients):
                try:
                    c.sendall(pcm)
                except OSError:
                    self.clients.remove(c)

    CONSOLE_MAX = 20 << 20             # dsd-fme's console, kept for diagnosis (console.log, then .1)

    def _read(self, proc, g):
        path = self.out / "console.log"
        logf = open(path, "a")
        for raw in proc.stderr:
            line = raw.decode("latin-1", "replace")
            with self.lock:
                self.trunk.feed(line, time.time())
            try:
                logf.write(time.strftime("%H:%M:%S ") + ANSI.sub("", line))
                if logf.tell() > self.CONSOLE_MAX:
                    logf.close()
                    path.replace(path.with_suffix(".log.1"))
                    logf = open(path, "a")
            except OSError:
                pass
        logf.close()
        if self._live(g):
            self.error = f"dsd-fme exited ({proc.poll()})"
            log.warning("P25: %s", self.error)

    VOICE_LOST_SEC = 5.0               # on a voice channel without sync this long: back to the control channel
    RESTART_SEC = 30.0                 # no sync at all this long: restart dsd-fme

    def watchdog(self, now):
        """dsd-fme can be left waiting on a voice channel (seen after the tuner was moved away for a few
        seconds): it never asks for the control channel again. Take it back there ourselves, and restart
        dsd-fme if even that brings no sync."""
        if not self.running:
            return
        last = self.trunk.last_sync or self.started
        if self.tuned_hz and self.tuned_hz != CC_HZ and now - last > self.VOICE_LOST_SEC:
            try:
                self.ctl.set_frequency(self.ssrc, self._if(CC_HZ))
                if self.iq_left > 0:
                    self.ctl.set_frequency(self.iq_ssrc, self._if(CC_HZ))
                self.tuned_hz = CC_HZ
                log.info("P25: no sync on the voice channel for %.0f s: back to the control channel", now - last)
            except Exception as e:
                log.warning("P25: retune to the control channel: %s", e)
        elif now - last > self.RESTART_SEC and now - self.started > self.RESTART_SEC:
            log.warning("P25: no sync for %.0f s: restarting dsd-fme", now - last)
            self.restarts += 1
            self.start(self.ctl, self.tuner_hz, self.if_hz, *self._net)

    def write(self, now):
        if now - self.last_write < 1:
            return
        self.watchdog(now)
        self.last_write = now
        with self.lock:
            snap = self.trunk.snapshot(now)
        if self.lvl_n:
            snap["input"] = {"rms": round((self.lvl_sq / self.lvl_n) ** 0.5), "clip": round(self.lvl_clip / self.lvl_n, 5),
                             "lost_packets": self.lost, "max_batch": self.backlog}
            self.backlog = 0
            self.lvl_n = self.lvl_clip = 0
            self.lvl_sq = 0.0
        snap.update(t=round(now, 1), running=self.running, error=self.error, cc_hz=CC_HZ,
                    tuned_hz=self.tuned_hz, tunes=self.tunes, restarts=self.restarts, on_voice=bool(self.tuned_hz and self.tuned_hz != CC_HZ))
        tmp = self.out / "p25.json.tmp"
        tmp.write_text(json.dumps(snap))
        tmp.replace(self.out / "p25.json")

    def stop(self):
        if not self.running:
            return
        self.running = False
        for f in (lambda: self.ctl.set_channel_lifetime(self.ssrc, 1), lambda: self.ctl.remove_channel(self.ssrc),
                  lambda: self.ctl.set_channel_lifetime(self.iq_ssrc, 1), lambda: self.ctl.remove_channel(self.iq_ssrc),
                  lambda: self.capture.close(), lambda: self.iq_file.close(),
                  lambda: self.proc.terminate(), lambda: self.proc.wait(3)):
            try:
                f()
            except Exception:
                pass
        for s in self.socks + getattr(self, "clients", []):
            try:
                s.close()
            except Exception:
                pass
        self.tuned_hz = None
        log.info("P25: stopped")
        self.write(time.time() + 3)
