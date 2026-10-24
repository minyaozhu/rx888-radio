# SPDX-License-Identifier: GPL-3.0-or-later
"""SSB voice on HF: known channels (amateur nets and calling frequencies, oceanic aviation, VOLMET, military,
Coast Guard) and a passband check that says which of them have a signal right now.

Frequencies are the dial (suppressed-carrier) frequency, as published; the voice sits 300-2700 Hz above it
(USB) or below it (LSB). By convention amateurs use LSB below 10 MHz (except the 60 m channels), USB above;
everything else on HF voice is USB.
"""

import asyncio
from typing import Dict, List

import numpy as np

# group, kHz, mode, name, note
CHANNELS = [
    ("ham", 3985.0, "lsb", "80 m QRP SSB calling", "low-power stations' calling frequency"),
    ("ham", 5330.5, "usb", "60 m channel 1", "US amateur 60 m channel (USB, 100 W ERP)"),
    ("ham", 5346.5, "usb", "60 m channel 2", "US amateur 60 m channel"),
    ("ham", 5357.0, "usb", "60 m channel 3", "US amateur 60 m channel"),
    ("ham", 5371.5, "usb", "60 m channel 4", "US amateur 60 m channel"),
    ("ham", 5403.5, "usb", "60 m channel 5", "US amateur 60 m channel"),
    ("ham", 7285.0, "lsb", "40 m QRP SSB calling", "low-power stations' calling frequency"),
    ("ham", 14285.0, "usb", "20 m QRP SSB calling", "low-power stations' calling frequency"),
    ("ham", 14300.0, "usb", "Maritime Mobile Service Net", "daily nets for boats at sea; also emergency traffic"),
    ("ham", 14325.0, "usb", "Hurricane Watch Net", "active when a hurricane threatens"),
    ("ham", 21385.0, "usb", "15 m QRP SSB calling", "low-power stations' calling frequency"),
    ("ham", 28385.0, "usb", "10 m QRP SSB calling", "low-power stations' calling frequency"),
    ("ham", 28400.0, "usb", "10 m SSB calling", "busy when 10 m is open"),
    ("ham", 50125.0, "usb", "6 m SSB calling", "needs the 0-64 MHz coverage"),
    # oceanic aviation: San Francisco Radio (ARINC) works Pacific flights on these families
    ("air", 2869.0, "usb", "Pacific CEP", "Central East Pacific: San Francisco Radio, night"),
    ("air", 5547.0, "usb", "Pacific CEP", "Central East Pacific: San Francisco Radio"),
    ("air", 5574.0, "usb", "Pacific CEP", "Central East Pacific: San Francisco Radio"),
    ("air", 8843.0, "usb", "Pacific CEP", "Central East Pacific: San Francisco Radio"),
    ("air", 11282.0, "usb", "Pacific CEP", "Central East Pacific: San Francisco Radio, day"),
    ("air", 13288.0, "usb", "Pacific CEP", "Central East Pacific: San Francisco Radio, day"),
    ("air", 2932.0, "usb", "North Pacific NP", "North Pacific: San Francisco / Anchorage, night"),
    ("air", 5628.0, "usb", "North Pacific NP", "North Pacific: San Francisco / Anchorage"),
    ("air", 6655.0, "usb", "North Pacific NP", "North Pacific: San Francisco / Anchorage"),
    ("air", 10048.0, "usb", "North Pacific NP", "North Pacific: San Francisco / Anchorage"),
    ("air", 11330.0, "usb", "North Pacific NP", "North Pacific: San Francisco / Anchorage, day"),
    ("air", 13339.0, "usb", "North Pacific NP", "North Pacific: San Francisco / Anchorage, day"),
    ("air", 2863.0, "usb", "Honolulu VOLMET", "airport weather for the Pacific, from H+00 and H+30"),
    ("air", 6679.0, "usb", "Honolulu VOLMET", "airport weather for the Pacific, from H+00 and H+30"),
    ("air", 8828.0, "usb", "Honolulu VOLMET", "airport weather for the Pacific, from H+00 and H+30"),
    ("air", 13282.0, "usb", "Honolulu VOLMET", "airport weather for the Pacific, from H+00 and H+30"),
    # USAF High Frequency Global Communications System: Emergency Action Messages, phone patches
    ("mil", 4724.0, "usb", "USAF HFGCS", "night"),
    ("mil", 6739.0, "usb", "USAF HFGCS", ""),
    ("mil", 8992.0, "usb", "USAF HFGCS", "the busiest: Emergency Action Messages"),
    ("mil", 11175.0, "usb", "USAF HFGCS", "the busiest: Emergency Action Messages"),
    ("mil", 13200.0, "usb", "USAF HFGCS", "day"),
    ("mil", 15016.0, "usb", "USAF HFGCS", "day"),
    # US Coast Guard
    ("marine", 4125.0, "usb", "USCG distress and safety", "ship-shore distress / calling"),
    ("marine", 6215.0, "usb", "USCG distress and safety", "ship-shore distress / calling"),
    ("marine", 8291.0, "usb", "USCG distress and safety", "ship-shore distress / calling"),
    ("marine", 12290.0, "usb", "USCG distress and safety", "ship-shore distress / calling"),
    ("marine", 16420.0, "usb", "USCG distress and safety", "ship-shore distress / calling"),
    ("marine", 4426.0, "usb", "USCG NMC weather", "Point Reyes: high-seas forecasts at scheduled times"),
    ("marine", 6501.0, "usb", "USCG NMC weather", "Point Reyes: high-seas forecasts at scheduled times"),
    ("marine", 8764.0, "usb", "USCG NMC weather", "Point Reyes: high-seas forecasts at scheduled times"),
    ("marine", 13089.0, "usb", "USCG NMC weather", "Point Reyes: high-seas forecasts at scheduled times"),
    ("marine", 17314.0, "usb", "USCG NMC weather", "Point Reyes: high-seas forecasts at scheduled times"),
]
GROUPS = {"ham": "Amateur", "air": "Aviation", "mil": "Military", "marine": "Marine"}
# amateur phone segments the scanner can search (scanner band name, label, sideband)
HAM_BANDS = [("160m", "160 m", "lsb"), ("80m", "80 / 75 m", "lsb"), ("40m", "40 m", "lsb"), ("20m", "20 m", "usb"),
             ("17m", "17 m", "usb"), ("15m", "15 m", "usb"), ("12m", "12 m", "usb"), ("10m", "10 m", "usb")]

PASS = (300.0, 2700.0)      # the voice band, Hz from the dial frequency


def channels(max_hz: float) -> Dict:
    return {"groups": GROUPS,
            "channels": [{"group": g, "freq_hz": k * 1e3, "mode": m, "name": n, "note": note,
                          "reachable": k * 1e3 + 3e3 < max_hz} for g, k, m, n, note in CHANNELS],
            "ham_bands": [{"band": b, "label": lab, "mode": m} for b, lab, m in HAM_BANDS]}


def passband_snr(freqs: np.ndarray, db: np.ndarray, f: float, mode: str) -> float:
    """Signal in a channel's voice band above the local floor (dB)."""
    lo, hi = (f + PASS[0], f + PASS[1]) if mode == "usb" else (f - PASS[1], f - PASS[0])
    inpass = (freqs >= lo) & (freqs <= hi)
    near = (np.abs(freqs - f) <= 40e3) & ~inpass
    if not inpass.any() or not near.any():
        return float("nan")
    floor = float(np.percentile(db[near], 30))
    top = np.sort(db[inpass])[-3:]          # the strongest few bins: speech is peaky
    return float(np.mean(top) - floor)


async def check(scanner, items: List[dict], frames: int = 12, bin_hz: float = 250.0) -> List[dict]:
    """Measure each channel's voice band, capturing as few spectrum windows as cover them all."""
    radio = scanner.spectrum.radio
    if radio.band != "hf":
        raise ValueError("SSB channels are on HF; switch the input to HF first")
    span = scanner.bins * bin_hz * 0.85
    todo = sorted(items, key=lambda x: x["freq_hz"])
    out = []
    async with scanner.lock:
        i = 0
        while i < len(todo):
            start = todo[i]["freq_hz"] - 45e3
            group = [x for x in todo[i:] if x["freq_hz"] + 45e3 <= start + span]
            i += len(group)
            freqs, db, _ = await scanner._capture(start + span / 2, bin_hz, frames)
            for x in group:
                s = passband_snr(freqs, db, x["freq_hz"], x["mode"])
                out.append({"freq_hz": x["freq_hz"], "snr_db": None if np.isnan(s) else round(s, 1)})
            await asyncio.sleep(0)
    return out
