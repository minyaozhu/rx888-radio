# SPDX-License-Identifier: GPL-3.0-or-later
"""Live P25 voice for the P25 page: dsd-fme's audio output, followed like the airband's "Follow activity".

dsd-fme (run by voice_session.py, rxapp/p25.py) already follows the trunk: it retunes to each clear call. Its
decoded voice goes to UDP 127.0.0.1:VOICE_PORT as 8 kHz s16 stereo, one TDMA slot per side (slot 1 left,
slot 2 right), encrypted slots muted. When only one slot is talking dsd-fme copies it to both sides.

A listener either follows activity (stays on the slot it's playing while that slot talks, moves to the other
one after SILENT_SEC of quiet) or holds one talkgroup (plays only the slot whose current talkgroup it is, from
the slots in p25.json). Audio goes out in /ws/audio's framing: 4-byte little-endian rate, then s16 mono.
"""
import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from .p25 import VOICE_PORT

log = logging.getLogger("p25live")

RATE = 8000
SILENT_SEC = 0.8
VOICE_RMS = 60.0                     # a side with less than this is silent (dsd-fme writes zeros between frames)


class Follower:
    """One listener's choice of slot. pick(stereo) -> mono int16 to send, or None for nothing."""

    def __init__(self, tg: Optional[int], slots: Callable[[], dict]):
        self.tg, self.slots = tg, slots
        self.cur = 0                     # 0 = left (slot 1), 1 = right (slot 2)
        self.last_voice = 0.0

    def pick(self, a: np.ndarray, now: float) -> Optional[np.ndarray]:
        left, right = a[:, 0], a[:, 1]
        rms = [float(np.sqrt(np.mean(x.astype(np.float64) ** 2))) if len(x) else 0.0 for x in (left, right)]
        on = [r > VOICE_RMS for r in rms]
        if self.tg is not None:
            sl = self.slots() or {}
            mine = [int(k) for k, v in sl.items() if v.get("tg") == self.tg]
            if not mine:
                return None
            side = mine[0] if mine[0] in (0, 1) else 0
            if np.array_equal(left, right):          # one voice, copied to both sides: the slot named last talks
                talking = min(sl.items(), key=lambda kv: kv[1].get("age", 99))[1].get("tg")
                return left.copy() if talking == self.tg else None
            return (left if side == 0 else right).copy()
        if np.array_equal(left, right):
            if on[0]:
                self.last_voice = now
            return left.copy()
        if on[self.cur]:
            self.last_voice = now
        elif on[1 - self.cur] and now - self.last_voice >= SILENT_SEC:
            self.cur, self.last_voice = 1 - self.cur, now
        return (left if self.cur == 0 else right).copy()


class P25Live:
    def __init__(self, p25_dir: Path):
        self.dir = p25_dir
        self.subs = set()
        self.transport = None
        self.last_packet = 0.0
        self._slots, self._slots_t, self._slots_mtime = {}, 0.0, None

    async def start(self):
        loop = asyncio.get_running_loop()
        live = self

        class Proto(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                live.packet(data)
        try:
            self.transport, _ = await loop.create_datagram_endpoint(Proto, local_addr=("127.0.0.1", VOICE_PORT))
        except OSError as e:
            log.warning("P25 live audio: can't listen on UDP %d (%s)", VOICE_PORT, e)

    def stop(self):
        if self.transport:
            self.transport.close()

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

    def packet(self, data: bytes):
        n = len(data) // 4 * 4
        if not n:
            return
        self.last_packet = time.time()
        a = np.frombuffer(data[:n], "<i2").reshape(-1, 2)
        for q in list(self.subs):
            if q.qsize() < 200:              # a stalled listener drops audio rather than piling it up
                q.put_nowait(a)

    def status(self) -> dict:
        return {"audio_age": round(time.time() - self.last_packet, 1) if self.last_packet else None,
                "listeners": len(self.subs), "slots": self.slots()}

    async def serve(self, ws, tg: Optional[int]):
        """Stream to one WebSocket until it closes."""
        q: asyncio.Queue = asyncio.Queue()
        f = Follower(tg, self.slots)
        self.subs.add(q)
        head = RATE.to_bytes(4, "little")
        try:
            while True:
                a = await q.get()
                out = f.pick(a, time.time())
                if out is not None:
                    await ws.send_bytes(head + out.astype("<i2").tobytes())
        finally:
            self.subs.discard(q)
