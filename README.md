# rx888-toolkit

Python tools for driving an **RX-888 MkII** SDR from a Mac (Apple Silicon, macOS 27). A companion to the [Malahit DSP2 toolkit](../Malahit), using the same approach: talk to the hardware directly, no vendor software.

## The hardware

- **HF direct sampling:** LTC2208 16-bit ADC, up to ~130 MSPS, covering roughly 0–64 MHz in one stream.
- **VHF/UHF:** R828D tuner, about 25 MHz–1.8 GHz.
- **Interface:** Cypress FX3 over USB 3. The firmware (`SDDC_FX3.img`) must be loaded into the FX3's RAM on every plug-in; until then it shows up as `04b4:00f3` (bootloader), and afterwards as `04b4:00f1`.
- It needs a real USB 3 (SuperSpeed) link for full sample rates.

## What's here

| Script | What it does |
|---|---|
| `probe.py` | Finds the RX-888, reports firmware state and USB link speed |
| `rx888/usb.py` | USB discovery (VID/PID for bootloader and SDDC states) |

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`pyusb` needs `libusb` (`brew install libusb`, already installed on this Mac).

Firmware: put `SDDC_FX3.img` from the [ik1xpv/ExtIO_sddc](https://github.com/ik1xpv/ExtIO_sddc) releases in `fw/` (it isn't committed).

## Roadmap

1. Firmware upload to the FX3 bootloader from Python.
2. Streaming raw ADC samples (HF direct sampling) and checking the achieved rate.
3. Digital downconversion to narrow IQ channels, then reuse the Malahit scanners and decoders (FT8, SSB, airband) against them.
4. Wideband HF survey: the whole 0–30 MHz spectrum at once, instead of the Malahit's 192 kHz window.
