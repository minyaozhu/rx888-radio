#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Scan the AM broadcast band (530-1700 kHz) on the RX-888 HF input.

Goes through the official SoapySDDC driver (run with .venv-soapy, see README).
Captures IQ centred on the band, measures the carrier on every 10 kHz US channel
against the band's noise floor, tries several IF gains, and lists stations
strongest first. --tune writes the strongest station into the SDR++ config.
"""

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
os.environ.setdefault("SOAPY_SDR_PLUGIN_PATH", str(HERE / "tools/ExtIO_sddc/build/SoapySDDC"))
import SoapySDR  # noqa: E402
from SoapySDR import SOAPY_SDR_CF32, SOAPY_SDR_RX  # noqa: E402

FS = 2e6
CENTER = 1.115e6  # 115 kHz - 2.115 MHz
N = 1 << 13       # ~244 Hz bins
SDRPP_ROOT = HERE / "tools/SDRPlusPlus/root_dev"


def capture(sdr, stream, seconds):
    n = int(FS * seconds)
    out = np.empty(n, np.complex64)
    buf = np.empty(1 << 16, np.complex64)
    got = 0
    while got < n:
        sr = sdr.readStream(stream, [buf], len(buf), timeoutUs=1_000_000)
        if sr.ret <= 0:
            continue
        k = min(sr.ret, n - got)
        out[got:got + k] = buf[:k]
        got += k
    return out


def channels(x):
    segs = x[: (len(x) // N) * N].reshape(-1, N) * np.hanning(N)
    p = 10 * np.log10((np.abs(np.fft.fftshift(np.fft.fft(segs, axis=1), axes=1)) ** 2).mean(0) + 1e-20)
    f = CENTER + np.fft.fftshift(np.fft.fftfreq(N, 1 / FS))
    floor = np.median(p[(f > 520e3) & (f < 1710e3)])
    rows = []
    for khz in range(530, 1710, 10):
        m = np.abs(f - khz * 1e3) <= 1500  # SoapySDDC tunes ~850 Hz off at HF
        rows.append((khz, float(p[m].max() - floor)))
    return rows


def tune_sdrpp(khz):
    """Point SDR++ (SoapySDR source, HF antenna, AM) at khz."""
    cfg = SDRPP_ROOT / "config.json"
    c = json.loads(cfg.read_text())
    c.update(frequency=khz * 1e3, centerTuning=True, vfoOffsets={"Radio": 0.0}, source="SoapySDR")
    cfg.write_text(json.dumps(c, indent=4))
    radio = SDRPP_ROOT / "radio_config.json"
    r = json.loads(radio.read_text())
    r["Radio"]["selectedDemodId"] = 2  # AM
    radio.write_text(json.dumps(r, indent=4))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=float, default=1.0)
    ap.add_argument("--min-snr", type=float, default=10, help="dB above floor to count as a station")
    ap.add_argument("--gains", default="0,10,20,30", help="IF gains (dB) to try")
    ap.add_argument("--tune", action="store_true", help="set SDR++ to the strongest station")
    args = ap.parse_args()

    sdr = SoapySDR.Device("driver=SDDC")
    sdr.setAntenna(SOAPY_SDR_RX, 0, "HF")
    sdr.setSampleRate(SOAPY_SDR_RX, 0, FS)
    sdr.setFrequency(SOAPY_SDR_RX, 0, CENTER)
    sdr.setGain(SOAPY_SDR_RX, 0, "RF", 0.0)
    stream = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32)
    sdr.activateStream(stream)
    best = None
    try:
        capture(sdr, stream, 0.5)  # settle
        for g in (float(v) for v in args.gains.split(",")):
            sdr.setGain(SOAPY_SDR_RX, 0, "IF", g)
            capture(sdr, stream, 0.3)
            x = capture(sdr, stream, args.seconds)
            rows = channels(x)
            n = sum(s >= args.min_snr for _, s in rows)
            peak = float(np.abs(x).max())
            print(f"IF gain {g:4.0f} dB: peak {peak:.3f} of full scale, {n} stations >= {args.min_snr:.0f} dB")
            if peak < 0.9 and (best is None or n > best[0]):
                best = (n, g, rows)
    finally:
        sdr.deactivateStream(stream)
        sdr.closeStream(stream)

    if best is None:
        print("Clipped at every gain", file=sys.stderr)
        return 1
    n, g, rows = best
    stations = sorted((r for r in rows if r[1] >= args.min_snr), key=lambda r: -r[1])
    print(f"\nStations at IF gain {g:.0f} dB (strongest first):")
    for khz, snr in stations:
        print(f"  {khz:5d} kHz  {snr:5.1f} dB")
    if args.tune and stations:
        tune_sdrpp(stations[0][0])
        print(f"\nSDR++ set to {stations[0][0]} kHz AM")
    return 0


if __name__ == "__main__":
    sys.exit(main())
