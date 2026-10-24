# SPDX-License-Identifier: GPL-3.0-or-later
"""Receive radiod's RTP audio and hand it out by SSRC.

One socket per multicast group, joined on the loopback interface (radiod runs with
ttl = 0). Payloads are 16-bit big-endian PCM; subscribers get little-endian int16 bytes,
which is what the browser and WAV files want.

Channel count per packet: radiod's WFM decoder switches a stereo channel between 2 and 1 output channels
on its own, block by block, as it finds or loses the 19 kHz pilot, and gives each format its own
(dynamically numbered) RTP payload type. The hub learns each payload type's channel count from the
timestamp step (frames per packet) against the payload size, and `channels(ssrc)` says what the packet
being handed out carries.
"""

import asyncio
import logging
import socket
import struct
from collections import defaultdict
from typing import Callable, Dict, List, Optional

import numpy as np

log = logging.getLogger(__name__)

# Called with (pcm_le_int16_bytes, rtp_timestamp, sequence)
Subscriber = Callable[[bytes, int, int], None]


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, hub):
        self.hub = hub

    def datagram_received(self, data, addr):
        if len(data) < 12 or (data[0] >> 6) != 2:
            return
        cc = data[0] & 0x0F
        seq, ts, ssrc = struct.unpack("!HII", data[2:12])
        pt = data[1] & 0x7F
        subs = self.hub.subs.get(ssrc)
        self.hub.packets[ssrc] += 1
        if not subs:
            self.hub.last[ssrc] = None
            return
        hdr = 12 + 4 * cc
        if data[0] & 0x10:  # header extension
            if len(data) < hdr + 4:
                return
            hdr += 4 + 4 * struct.unpack("!H", data[hdr + 2:hdr + 4])[0]
        pcm = np.frombuffer(data[hdr:], ">i2").astype("<i2").tobytes()
        self.hub.learn(ssrc, pt, seq, ts, len(pcm) // 2)
        for cb in list(subs):
            try:
                cb(pcm, ts, seq)
            except Exception:
                log.exception("RTP subscriber failed")


class RtpHub:
    def __init__(self, iface_addr: str, port: int):
        self.iface_addr = iface_addr
        self.port = port
        self.subs: Dict[int, List[Subscriber]] = defaultdict(list)
        self.packets: Dict[int, int] = defaultdict(int)
        self._transports = {}
        self.pt_channels: Dict[int, int] = {}        # RTP payload type -> channels (learned)
        self.last: Dict[int, Optional[tuple]] = {}   # ssrc -> (seq, ts, pt) of the last packet
        self.current: Dict[int, int] = {}            # ssrc -> channels of the packet being handed out
        self.stereo_packets: Dict[int, int] = defaultdict(int)

    def learn(self, ssrc: int, pt: int, seq: int, ts: int, samples: int):
        prev = self.last.get(ssrc)
        if prev and pt == prev[2] and seq == (prev[0] + 1) & 0xFFFF:   # keep learning: radiod renumbers on restart
            frames = (ts - prev[1]) & 0xFFFFFFFF
            if 0 < frames < 100000 and samples in (frames, 2 * frames):
                self.pt_channels[pt] = samples // frames
        self.last[ssrc] = (seq, ts, pt)
        n = self.pt_channels.get(pt, 1)
        self.current[ssrc] = n
        if n == 2:
            self.stereo_packets[ssrc] += 1

    def channels(self, ssrc: int) -> int:
        """Channels (1 or 2) of the packet being handed out for ssrc."""
        return self.current.get(ssrc, 1)

    async def join(self, group: str):
        if group in self._transports:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        sock.bind((group, self.port))  # binding the group filters out other groups
        mreq = socket.inet_aton(group) + socket.inet_aton(self.iface_addr)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.setblocking(False)
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(lambda: _Protocol(self), sock=sock)
        self._transports[group] = transport
        log.info("joined RTP group %s:%d on %s", group, self.port, self.iface_addr)

    def subscribe(self, ssrc: int, cb: Subscriber):
        self.subs[ssrc].append(cb)

    def unsubscribe(self, ssrc: int, cb: Subscriber):
        if cb in self.subs.get(ssrc, []):
            self.subs[ssrc].remove(cb)

    def close(self):
        for t in self._transports.values():
            t.close()
        self._transports.clear()
