# SPDX-License-Identifier: GPL-3.0-or-later
"""Live P25 voice for the P25 page: the voice decoders' audio, followed like the airband's "Follow activity".

rxapp/p25.py (run by voice_session.py) decodes every voice channel at once: voice decoder k sends its audio to UDP
127.0.0.1:VOICE_PORT + k as 8 kHz s16 stereo, one TDMA slot per side (slot 1 left, slot 2 right); when only one
slot talks it is copied to both sides. p25.json's slots name each "<k>:<slot>"'s talkgroup (and whether it's
encrypted: encrypted slots are never played, they decode to noise).

A listener either follows activity (stays with the talkgroup it's playing, a conversation, while it talks and
for HOLD_SEC of quiet after, for a reply on any channel or slot; then moves to whatever else is talking) or holds
one talkgroup (plays the slot whose current talkgroup it is). Audio goes out in /ws/audio's framing: 4-byte
little-endian rate, then s16 mono.
"""
import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from .p25 import MAX_VOICE, VOICE_PORT

log = logging.getLogger("p25live")

RATE = 8000
SILENT_SEC = 0.8                     # nothing being followed yet: switch sources after this much quiet
HOLD_SEC = 5.0                       # following a talkgroup: wait this long for its reply before moving on
VOICE_RMS = 60.0                     # a side with less than this is silent (dsd-fme writes zeros between frames)


def _rms(x):
    return float(np.sqrt(np.mean(x.astype(np.float64) ** 2))) if len(x) else 0.0


def _same(a, b):
    """Two sources are one if they're the same slot, or one of them is that channel's copied-to-both voice."""
    return a == b or (a is not None and b is not None and a[0] == b[0] and "m" in (a[1], b[1]))


class Follower:
    """One listener's choice of source among the voice decoders' slots. pick(k, stereo, now) -> mono int16 to
    send, or None for nothing."""

    def __init__(self, tg: Optional[int], slots: Callable[[], dict]):
        self.tg, self.slots = tg, slots
        self.cur = None                  # (decoder, slot 0 / 1, or "m": its one voice on both sides)
        self.last_voice = 0.0
        self.following = None            # follow activity: the talkgroup (conversation) being played

    def _sources(self, k, a):
        sl = self.slots() or {}
        left, right = a[:, 0], a[:, 1]
        if np.array_equal(left, right):  # one voice: the slot named last on this channel
            mine = [v for key, v in sl.items() if str(key).split(":")[0] == str(k)]
            info = min(mine, key=lambda v: v.get("age", 99)) if mine else {}
            return [((k, "m"), left, info)]
        return [((k, 0), left, sl.get(f"{k}:0") or {}), ((k, 1), right, sl.get(f"{k}:1") or {})]

    def pick(self, k: int, a: np.ndarray, now: float) -> Optional[np.ndarray]:
        cands = []
        for src, x, info in self._sources(k, a):
            tg, enc = info.get("tg"), bool(info.get("enc"))
            cands.append((src, x, tg, enc, _rms(x) > VOICE_RMS and not enc))
        if self.tg is not None:          # one talkgroup: its slot, wherever it is
            for src, x, tg, enc, on in cands:
                if tg == self.tg and not enc:
                    return x.copy()
            return None
        cur = next((c for c in cands if _same(c[0], self.cur)), None)
        if cur is not None and cur[3]:   # turned out encrypted: let anything else take over at once
            self.cur, self.following, cur = None, None, None
        # the conversation being followed, wherever it talks now (its slot or channel can change between overs)
        reply = next((c for c in cands if c[4] and self.following is not None and c[2] == self.following), None)
        if reply is None and cur is not None and cur[4] and (cur[2] is None or self.following is None or cur[2] == self.following):
            reply = cur
        if reply is not None:
            self.cur, self.last_voice = reply[0], now
            self.following = reply[2] if reply[2] is not None else self.following
            return reply[1].copy()
        # once the conversation has been quiet long enough: anything else that talks
        wait = 0.0 if self.cur is None else HOLD_SEC if self.following is not None else SILENT_SEC
        other = next((c for c in cands if c[4]), None) if now - self.last_voice >= wait else None
        if other is not None:
            self.cur, self.last_voice, self.following = other[0], now, other[2]
            return other[1].copy()
        if cur is None:
            return None
        # the slot being played, quiet between words (the stream stays continuous), or now someone else's: silence
        return np.zeros_like(cur[1]) if cur[4] else cur[1].copy()


class Concealer:
    """Short holes in the decoded voice (dsd-fme writes exact zeros for a voice frame it couldn't decode: about 3
    per 10 s of speech here, mostly 20-40 ms) are filled by repeating the last good 20 ms frame, fading, for up to
    MAX_FRAMES; a longer silence is left alone (a real pause or the end of the call)."""
    FRAME = 160                          # 20 ms at 8 kHz: one voice frame
    MAX_FRAMES = 3
    FADE = (0.7, 0.45, 0.25)

    def __init__(self):
        self.last = None                 # the last frame with sound
        self.run = 0                     # silent frames in a row since it
        self.tail = np.zeros(0, np.int16)

    def process(self, a: np.ndarray) -> np.ndarray:
        x = np.concatenate([self.tail, a]) if len(self.tail) else a
        n = len(x) // self.FRAME * self.FRAME
        self.tail = x[n:].copy()
        out = x[:n].copy()
        for i in range(0, n, self.FRAME):
            f = out[i:i + self.FRAME]
            if f.any():
                self.last, self.run = f.copy(), 0
                continue
            self.run += 1
            if self.last is not None and self.run <= self.MAX_FRAMES:
                out[i:i + self.FRAME] = (self.last * self.FADE[self.run - 1]).astype(np.int16)
            elif self.run > self.MAX_FRAMES:
                self.last = None
        return out


class P25Live:
    def __init__(self, p25_dir: Path):
        self.dir = p25_dir
        self.subs = set()
        self.transports = []
        self.last_packet = 0.0
        self._slots, self._slots_t, self._slots_mtime = {}, 0.0, None

    async def start(self):
        loop = asyncio.get_running_loop()
        live = self
        self.transports = []

        class Proto(asyncio.DatagramProtocol):
            def __init__(self, k):
                self.k = k

            def datagram_received(self, data, addr):
                live.packet(self.k, data)
        for k in range(MAX_VOICE):       # voice decoder k's audio
            try:
                t, _ = await loop.create_datagram_endpoint(lambda k=k: Proto(k), local_addr=("127.0.0.1", VOICE_PORT + k))
                self.transports.append(t)
            except OSError as e:
                log.warning("P25 live audio: can't listen on UDP %d (%s)", VOICE_PORT + k, e)

    def stop(self):
        for t in getattr(self, "transports", []):
            t.close()

    def slots(self) -> dict:
        """The live session's slots (p25.json), re-read at most twice a second."""
        now = time.time()
        if now - self._slots_t < 0.5:
            return self._slots
        self._slots_t = now
        try:
            sessions = sorted((p for p in self.dir.iterdir() if p.is_dir()), reverse=True)
            f = sessions[0] / "p25.json"
            st = f.stat()
            if now - st.st_mtime > 10:
                self._slots = {}
            elif st.st_mtime != self._slots_mtime:
                self._slots, self._slots_mtime = json.loads(f.read_text()).get("slots", {}), st.st_mtime
        except (OSError, ValueError, IndexError):
            self._slots = {}
        return self._slots

    def packet(self, k: int, data: bytes):
        n = len(data) // 4 * 4
        if not n:
            return
        self.last_packet = time.time()
        a = np.frombuffer(data[:n], "<i2").reshape(-1, 2)
        for q in list(self.subs):
            if q.qsize() < 200 * MAX_VOICE:  # a stalled listener drops audio rather than piling it up
                q.put_nowait((k, a))

    def status(self) -> dict:
        return {"audio_age": round(time.time() - self.last_packet, 1) if self.last_packet else None,
                "listeners": len(self.subs), "slots": self.slots()}

    async def serve(self, ws, tg: Optional[int]):
        """Stream to one WebSocket until it closes."""
        q: asyncio.Queue = asyncio.Queue()
        f, pc = Follower(tg, self.slots), Concealer()
        self.subs.add(q)
        head = RATE.to_bytes(4, "little")
        try:
            while True:
                k, a = await q.get()
                out = f.pick(k, a, time.time())
                if out is not None:
                    out = pc.process(out.astype(np.int16))
                    if len(out):
                        await ws.send_bytes(head + out.astype("<i2").tobytes())
        finally:
            self.subs.discard(q)
