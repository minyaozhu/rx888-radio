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
| `am_scan.py` | Scans the AM broadcast band on the HF input, tries several gains, lists stations by SNR |
| `rx888/sddc.py` | Direct control: HF/VHF port, gain, VHF tuner, ADC rate, sample stream |
| `run_sdrpp.sh` | Launches the locally built SDR++ with the official SoapySDDC driver |

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`pyusb` needs `libusb` (`brew install libusb`, already installed on this Mac).

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

`run_sdrpp.sh` points `SOAPY_SDR_PLUGIN_PATH` at the driver build, so nothing is installed system-wide. Check the driver alone with `SOAPY_SDR_PLUGIN_PATH=tools/ExtIO_sddc/build/SoapySDDC SoapySDRUtil --probe="driver=SDDC"`. The run root (`tools/SDRPlusPlus/root_dev`) is a copy of `root/` plus a `modules/` folder of links to the built `.dylib` files.

In the SoapySDR source menu: antenna **HF** for 0–64 MHz (AM broadcast, shortwave, ham bands), **VHF** for FM broadcast, airband and above.

### Superseded: SDR++'s own SDDC input

SDR++ also has a built-in `sddc_source`, but it's an unfinished stub (hard-coded serial and firmware path, VHF port selected while doing HF direct sampling, USB reads that overflow on macOS and wedge the device). `tools/sdrpp_sddc_macos.patch` makes it work for HF, but it has no gain or port controls. Use the SoapySDR input instead.

## Roadmap

1. Firmware upload to the FX3 bootloader from Python.
2. Streaming raw ADC samples (HF direct sampling) and checking the achieved rate.
3. Digital downconversion to narrow IQ channels, then reuse the Malahit scanners and decoders (FT8, SSB, airband) against them.
4. Wideband HF survey: the whole 0–30 MHz spectrum at once, instead of the Malahit's 192 kHz window.
