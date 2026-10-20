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
| `run_sdrpp.sh` | Launches the locally built SDR++ with its RX-888 (SDDC) input |

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`pyusb` needs `libusb` (`brew install libusb`, already installed on this Mac).

Firmware: put `SDDC_FX3.img` from the [ik1xpv/ExtIO_sddc](https://github.com/ik1xpv/ExtIO_sddc) releases in `fw/` (it isn't committed).

## SDR++ with the RX-888 input

The macOS release of SDR++ leaves out its SDDC (RX-888) input, and the input in its source code is an unfinished stub: it doesn't compile on macOS and points to a firmware file on its author's PC. `tools/sdrpp_sddc_macos.patch` fixes that. To rebuild:

```bash
brew install cmake fftw glfw volk rtaudio
git clone --depth 1 https://github.com/AlexandreRouma/SDRPlusPlus.git tools/SDRPlusPlus
git -C tools/SDRPlusPlus apply ../sdrpp_sddc_macos.patch
cmake -S tools/SDRPlusPlus -B tools/SDRPlusPlus/build -DCMAKE_BUILD_TYPE=Release -DOPT_BUILD_SDDC_SOURCE=ON -DBUILD_SDDC_UTILS=OFF -DOPT_BUILD_AIRSPY_SOURCE=OFF -DOPT_BUILD_AIRSPYHF_SOURCE=OFF -DOPT_BUILD_HACKRF_SOURCE=OFF -DOPT_BUILD_PLUTOSDR_SOURCE=OFF -DOPT_BUILD_RTL_SDR_SOURCE=OFF -DOPT_BUILD_AUDIO_SOURCE=OFF -DOPT_BUILD_DISCORD_PRESENCE=OFF
make -C tools/SDRPlusPlus/build -j10
```

The patch hard-codes the firmware path as `fw/SDDC_FX3.img` in this project; the input uploads it automatically when the device is still in bootloader mode. The run root (`tools/SDRPlusPlus/root_dev`) is a copy of `root/` plus a `modules/` folder of links to the built `.dylib` files and a `config.json` with the SDDC input enabled.

The input is basic: HF direct sampling only (no VHF/UHF tuner, gain or attenuator controls yet), with sample rates of 4–64 MHz.

Note: the FX3 enumerates at USB 2 speed while it's in bootloader mode; it comes back at USB 3 once the firmware is running.

## Roadmap

1. Firmware upload to the FX3 bootloader from Python.
2. Streaming raw ADC samples (HF direct sampling) and checking the achieved rate.
3. Digital downconversion to narrow IQ channels, then reuse the Malahit scanners and decoders (FT8, SSB, airband) against them.
4. Wideband HF survey: the whole 0–30 MHz spectrum at once, instead of the Malahit's 192 kHz window.
