#!/usr/bin/env python3
"""Scan the AM broadcast band (530-1700 kHz) on the RX-888 HF input.

Captures direct-sampled ADC data, measures the carrier on every 10 kHz US
channel against the band's noise floor, and lists stations strongest first.
"""

import argparse
import sys

import numpy as np

from rx888.sddc import RX888, PGA_EN, AD8340_VGA

FS = 32e6
N = 1 << 16  # ~490 Hz bins


def spectrum(r, seconds):
    x = r.read(int(FS * seconds)).astype(np.float32)
    segs = x[: (len(x) // N) * N].reshape(-1, N) * np.hanning(N)
    p = 10 * np.log10((np.abs(np.fft.rfft(segs, axis=1)) ** 2).mean(0) + 1e-12)
    return np.fft.rfftfreq(N, 1 / FS), p, x


def scan(f, p):
    band = (f > 520e3) & (f < 1710e3)
    floor = np.median(p[band])
    rows = []
    for khz in range(530, 1710, 10):
        m = np.abs(f - khz * 1e3) <= 600  # carrier sits within +-600 Hz
        rows.append((khz, p[m].max() - floor))
    return floor, rows


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=float, default=0.5)
    ap.add_argument("--min-snr", type=float, default=10, help="dB above floor to count as a station")
    ap.add_argument("--vga", type=int, default=None, help="HF VGA setting (0-127); default: try several")
    args = ap.parse_args()

    r = RX888()
    try:
        r.hf_mode()
        r.start(FS)
        r.read(1 << 21)  # settle
        best = None
        for vga in ([args.vga] if args.vga is not None else [0, 32, 64, 96, 127]):
            r.set_gpio(PGA_EN, True)
            r.set_param(AD8340_VGA, vga)
            r.read(1 << 21)
            f, p, x = spectrum(r, args.seconds)
            floor, rows = scan(f, p)
            peak = np.abs(x).max()
            n = sum(s >= args.min_snr for _, s in rows)
            print(f"VGA {vga:3d}: ADC peak {peak:5.0f}/32767, {n} stations >= {args.min_snr:.0f} dB")
            if peak < 30000 and (best is None or n > best[0]):
                best = (n, vga, rows)
    finally:
        r.close()

    if best is None:
        print("ADC clipped at every gain setting", file=sys.stderr)
        return 1
    n, vga, rows = best
    print(f"\nStations at VGA {vga} (strongest first):")
    for khz, snr in sorted(rows, key=lambda t: -t[1]):
        if snr >= args.min_snr:
            print(f"  {khz:5d} kHz  {snr:5.1f} dB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
