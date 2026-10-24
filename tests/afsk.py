"""Make APRS test audio: an AX.25 UI frame as Bell 202 AFSK (1200 baud, 1200/2200 Hz), the way a radio sends it."""
import numpy as np


def _addr(call: str, last: bool) -> bytes:
    c, _, ssid = call.partition("-")
    b = bytes((ord(ch) << 1) for ch in c.ljust(6)[:6])
    return b + bytes([0x60 | ((int(ssid or 0) & 15) << 1) | (1 if last else 0)])


def _fcs(data: bytes) -> bytes:
    crc = 0xFFFF
    for byte in data:
        for i in range(8):
            bit = (byte >> i) & 1
            crc = (crc >> 1) ^ 0x8408 if (crc ^ bit) & 1 else crc >> 1
    crc ^= 0xFFFF
    return bytes([crc & 0xFF, crc >> 8])


def frame(src: str, dst: str, path: list, info: str) -> bytes:
    calls = [dst, src] + list(path)
    a = b"".join(_addr(c, i == len(calls) - 1) for i, c in enumerate(calls))
    body = a + b"\x03\xf0" + info.encode("latin-1")
    return body + _fcs(body)


def afsk(packet: bytes, rate: int = 22050, flags: int = 30) -> np.ndarray:
    bits = []
    for _ in range(flags):                                # preamble flags (no stuffing)
        bits += [(0x7E >> i) & 1 for i in range(8)]
    ones = 0
    for byte in packet:                                   # LSB first, bit-stuffed
        for i in range(8):
            b = (byte >> i) & 1
            bits.append(b)
            ones = ones + 1 if b else 0
            if ones == 5:
                bits.append(0)
                ones = 0
    for _ in range(3):
        bits += [(0x7E >> i) & 1 for i in range(8)]
    tone, out, ph = 1200.0, [], 0.0                       # NRZI: a 0 bit toggles the tone
    spb = rate / 1200.0
    acc = 0.0
    for b in bits:
        if b == 0:
            tone = 2200.0 if tone == 1200.0 else 1200.0
        acc += spb
        n = int(round(acc)); acc -= n
        t = np.arange(n)
        out.append(np.sin(ph + 2 * np.pi * tone * t / rate))
        ph = (ph + 2 * np.pi * tone * n / rate) % (2 * np.pi)
    return np.concatenate(out)
