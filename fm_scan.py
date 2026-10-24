#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Scan the FM broadcast band (88.1-107.9 MHz) on the RX-888 VHF input.

Goes through the official SoapySDDC driver (run with .venv-soapy, see README).
Sweeps the band in 8 MHz windows, measures the power in each 200 kHz US FM
channel against the band's noise floor, and lists stations strongest first.
--tune writes the strongest station into the SDR++ config (VHF antenna, WFM).
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

FS = 8e6
USE = 3.2e6               # use the middle +-3.2 MHz of each window
CENTERS = (91.0e6, 97.4e6, 103.8e6, 107.0e6)
N = 1 << 14               # ~490 Hz bins
CHANNELS = [round(88.1e6 + 0.2e6 * i) for i in range(100)]  # 88.1 ... 107.9
SDRPP_ROOT = HERE / "tools/SDRPlusPlus/root_dev"
LABEL = "SDDC :: RX888mk2          sn:0009030300D9241A"


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


def psd(x, center):
    segs = x[: (len(x) // N) * N].reshape(-1, N) * np.hanning(N)
    p = (np.abs(np.fft.fftshift(np.fft.fft(segs, axis=1), axes=1)) ** 2).mean(0)
    f = center + np.fft.fftshift(np.fft.fftfreq(N, 1 / FS))
    return f, p


def tune_sdrpp(hz, rf, if_):
    """Point SDR++ (SoapySDR source, VHF antenna, WFM) at hz."""
    cfg = SDRPP_ROOT / "config.json"
    c = json.loads(cfg.read_text())
    c.update(frequency=float(hz), centerTuning=True, vfoOffsets={"Radio": 0.0}, source="SoapySDR")
    cfg.write_text(json.dumps(c, indent=4))
    soapy = SDRPP_ROOT / "soapy_source_config.json"
    s = json.loads(soapy.read_text())
    s["device"] = LABEL
    s.setdefault("devices", {})[LABEL] = {"antenna": 1, "bandwidth": 0, "sampleRate": 2000000.0,
                                          "gains": {"RF": float(rf), "IF": float(if_)}}
    soapy.write_text(json.dumps(s, indent=4))
    radio = SDRPP_ROOT / "radio_config.json"
    r = json.loads(radio.read_text())
    r["Radio"]["selectedDemodId"] = 1  # WFM
    radio.write_text(json.dumps(r, indent=4))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rf", type=float, default=20, help="VHF RF (LNA) gain, 0-49.6 dB")
    ap.add_argument("--if-gain", type=float, default=10, help="IF gain, -4.7-40.8 dB")
    ap.add_argument("--seconds", type=float, default=0.3)
    ap.add_argument("--min-snr", type=float, default=10)
    ap.add_argument("--tune", action="store_true", help="set SDR++ to the strongest station")
    args = ap.parse_args()

    sdr = SoapySDR.Device("driver=SDDC")
    sdr.setAntenna(SOAPY_SDR_RX, 0, "VHF")
    sdr.setSampleRate(SOAPY_SDR_RX, 0, FS)
    sdr.setGain(SOAPY_SDR_RX, 0, "RF", args.rf)
    sdr.setGain(SOAPY_SDR_RX, 0, "IF", args.if_gain)
    stream = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32)
    sdr.activateStream(stream)
    power = {}
    peak = 0.0
    try:
        for center in CENTERS:
            sdr.setFrequency(SOAPY_SDR_RX, 0, center)
            capture(sdr, stream, 0.2)  # let the tuner settle
            x = capture(sdr, stream, args.seconds)
            peak = max(peak, float(np.abs(x).max()))
            f, p = psd(x, center)
            for ch in CHANNELS:
                if abs(ch - center) > USE or ch in power:
                    continue
                power[ch] = p[np.abs(f - ch) <= 75e3].mean()
    finally:
        sdr.deactivateStream(stream)
        sdr.closeStream(stream)

    db = {ch: 10 * np.log10(v) for ch, v in power.items()}
    floor = float(np.percentile(list(db.values()), 25))
    stations = sorted(((ch, d - floor) for ch, d in db.items() if d - floor >= args.min_snr), key=lambda t: -t[1])
    print(f"RF {args.rf:.0f} dB, IF {args.if_gain:.0f} dB: peak {peak:.2f} of full scale"
          + ("  (CLIPPING - lower --rf)" if peak > 0.9 else ""))
    print(f"{len(stations)} stations >= {args.min_snr:.0f} dB above floor (strongest first):")
    for ch, snr in stations:
        print(f"  {ch / 1e6:6.1f} MHz  {snr:5.1f} dB")
    if args.tune and stations:
        tune_sdrpp(stations[0][0], args.rf, args.if_gain)
        print(f"\nSDR++ set to {stations[0][0] / 1e6:.1f} MHz WFM, VHF antenna")
    return 0


if __name__ == "__main__":
    sys.exit(main())
