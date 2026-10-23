# rx888-toolkit

Tools for driving an **RX-888 MkII** SDR from a Mac (Apple Silicon): **rxapp**, a browser SDR and monitoring app built on ka9q-radio's `radiod`; macOS ports of ka9q-radio and ka9q-web; SoapySDDC/SDR++ setup; and Python scripts that talk to the hardware directly. A companion to the [Malahit DSP2 toolkit](https://github.com/minyaozhu/malahit-dsp2-toolkit).

## The hardware

- **HF direct sampling:** LTC2208 16-bit ADC, up to ~130 MSPS, covering roughly 0–64 MHz in one stream.
- **VHF/UHF:** R828D tuner, about 25 MHz–1.8 GHz.
- **Interface:** Cypress FX3 over USB 3. The firmware (`SDDC_FX3.img`) must be loaded into the FX3's RAM on every plug-in; until then it shows up as `04b4:00f3` (bootloader), and afterwards as `04b4:00f1`.
- It needs a real USB 3 (SuperSpeed) link for full sample rates.

## What's here

| Script | What it does |
|---|---|
| **`rxapp/`** | **Your own SDR app: HF (0–32 or 0–64 MHz) and VHF/UHF inputs, browser waterfall, click-to-tune, audio, scans, FT8 on every band at once with a world map, an SSTV picture monitor, a live Bay Area airband map, server-side monitoring, dashboard, auto-recovery. `./run_rxapp.sh`, then http://127.0.0.1:8090. See [rxapp/README.md](rxapp/README.md)** |
| **`airband_session.py`** | **Records and transcribes every airband channel in the VHF tuner's window at once (alternating approach and tower windows), for rxapp's live map at `/airband.html`. See [rxapp/README.md](rxapp/README.md#airband)** |
| `probe.py` | Finds the RX-888, reports firmware state and USB link speed |
| `rx888/usb.py` | USB discovery (VID/PID for bootloader and SDDC states) |
| `am_scan.py` | Scans the AM broadcast band through SoapySDDC, tries several IF gains, lists stations by SNR; `--tune` sets SDR++ to the strongest |
| `rx888/sddc.py` | Direct control: HF/VHF port, gain, VHF tuner, ADC rate, sample stream |
| `fm_scan.py` | Scans the FM band on the VHF input; `--tune` sets SDR++ to the strongest station (WFM) |
| `ka9q/` | ka9q-radio (radiod, multichannel) ported to macOS; see [ka9q/README.md](ka9q/README.md) |
| `run_sdrpp.sh` | Launches the locally built SDR++ with the official SoapySDDC driver |

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`pyusb` needs `libusb` (`brew install libusb`).

Firmware: put `SDDC_FX3.img` from the [ik1xpv/ExtIO_sddc](https://github.com/ik1xpv/ExtIO_sddc) releases in `fw/` (it isn't committed).

## SDR++ with the RX-888 (via the official SoapySDDC driver)

The RX-888 runs in SDR++ through its **SoapySDR** input and **SoapySDDC**, the SoapySDR driver from the official [ik1xpv/ExtIO_sddc](https://github.com/ik1xpv/ExtIO_sddc) project. It's the same core that Windows HDSDR users run: HF and VHF ports, RF attenuation (−31.5–0 dB) and IF gain (−24.6–+33.1 dB), 2–64 MSPS, 10 kHz–1800 MHz, and a NEON-optimized downconverter on Apple Silicon. It also uploads the firmware itself (embedded in the driver).

```bash
brew install cmake fftw glfw volk rtaudio soapysdr

# Driver
git clone --depth 1 https://github.com/ik1xpv/ExtIO_sddc.git tools/ExtIO_sddc
cmake -S tools/ExtIO_sddc -B tools/ExtIO_sddc/build -DCMAKE_BUILD_TYPE=Release -DCMAKE_OSX_ARCHITECTURES=arm64
cmake --build tools/ExtIO_sddc/build -j10

# SDR++ with the SoapySDR input
git clone --depth 1 https://github.com/AlexandreRouma/SDRPlusPlus.git tools/SDRPlusPlus
cmake -S tools/SDRPlusPlus -B tools/SDRPlusPlus/build -DCMAKE_BUILD_TYPE=Release -DOPT_BUILD_SOAPY_SOURCE=ON -DOPT_BUILD_AIRSPY_SOURCE=OFF -DOPT_BUILD_AIRSPYHF_SOURCE=OFF -DOPT_BUILD_HACKRF_SOURCE=OFF -DOPT_BUILD_PLUTOSDR_SOURCE=OFF -DOPT_BUILD_RTL_SDR_SOURCE=OFF -DOPT_BUILD_AUDIO_SOURCE=OFF -DOPT_BUILD_DISCORD_PRESENCE=OFF
make -C tools/SDRPlusPlus/build -j10
```

`tools/soapysddc_macos.patch` fixes an off-by-one in the driver's USB string handling (the device label picked up random bytes after the serial, so SDR++ never matched its saved settings). Apply it after cloning: `git -C tools/ExtIO_sddc apply ../soapysddc_macos.patch`.

Python scripts that use the driver (`am_scan.py`) run in `.venv-soapy`, which sees Homebrew's SoapySDR bindings:

```bash
/opt/homebrew/bin/python3.15 -m venv --system-site-packages .venv-soapy
.venv-soapy/bin/pip install numpy pyusb
.venv-soapy/bin/python am_scan.py --tune
```

Close SDR++ before running a script: only one program can hold the RX-888. At HF the driver tunes about 850 Hz high; `am_scan.py` allows for it.

`run_sdrpp.sh` points `SOAPY_SDR_PLUGIN_PATH` at the driver build, so nothing is installed system-wide. Check the driver alone with `SOAPY_SDR_PLUGIN_PATH=tools/ExtIO_sddc/build/SoapySDDC SoapySDRUtil --probe="driver=SDDC"`. The run root (`tools/SDRPlusPlus/root_dev`) is a copy of `root/` plus a `modules/` folder of links to the built `.dylib` files.

In the SoapySDR source menu: antenna **HF** for 0–64 MHz (AM broadcast, shortwave, ham bands), **VHF** for FM broadcast, airband and above.

### Superseded: SDR++'s own SDDC input

SDR++ also has a built-in `sddc_source`, but it's an unfinished stub (hard-coded serial and firmware path, VHF port selected while doing HF direct sampling, USB reads that overflow on macOS and wedge the device). `tools/sdrpp_sddc_macos.patch` makes it work for HF, but it has no gain or port controls. Use the SoapySDR input instead. (The patch's firmware path reads `@ROOT@/fw/SDDC_FX3.img`; replace `@ROOT@` with this folder before applying it.)

## Roadmap

- **ADS-B on the map.** The 1090 MHz demodulator (`rxapp/modes.py`) is ready and tested, but the current
  antenna hears nothing at 1090 MHz (see [rxapp/README.md](rxapp/README.md#ads-b-not-yet)); needs a 1090 MHz
  antenna, or a second dongle.
- Infer aircraft positions from what controllers say (altitudes on known approach paths, "N miles south of",
  named fixes), with uncertainty circles.
- Wider live VHF window (~10 MHz) by moving the tuner IF up and opening its widest filter.
- FT4 alongside FT8; stereo and RDS for WFM.

## License

GPL-3.0 (see [LICENSE](LICENSE)), matching ka9q-radio, whose patched sources and configs this repo ships.
