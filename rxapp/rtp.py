# SPDX-License-Identifier: GPL-3.0-or-later
"""Receive radiod's RTP audio and hand it out by SSRC.

One socket per multicast group, joined on the loopback interface (radiod runs with
ttl = 0). Payloads are 16-bit big-endian PCM; subscribers get little-endian int16 bytes,
which is what the browser and WAV files want.
"""

import asyncio
import logging
import socket
import struct
from collections import defaultdict
from typing import Callable, Dict, List

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
        subs = self.hub.subs.get(ssrc)
        self.hub.packets[ssrc] += 1
        if not subs:
            return
        hdr = 12 + 4 * cc
        if data[0] & 0x10:  # header extension
            if len(data) < hdr + 4:
                return
            hdr += 4 + 4 * struct.unpack("!H", data[hdr + 2:hdr + 4])[0]
        pcm = np.frombuffer(data[hdr:], ">i2").astype("<i2").tobytes()
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
