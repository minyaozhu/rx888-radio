# rx888-toolkit

Tools for driving an **RX-888 MkII** SDR from a Mac (Apple Silicon): **rxapp**, a browser SDR and monitoring app built on ka9q-radio's `radiod`; macOS ports of ka9q-radio and ka9q-web; SoapySDDC/SDR++ setup; and Python scripts that talk to the hardware directly. A companion to the [Malahit DSP2 toolkit](https://github.com/minyaozhu/malahit-dsp2-toolkit).

![Bay Airband, Live: a map of the San Francisco Bay Area with its airports; SFO, Palo Alto, Moffett and San José arrivals labelled by call sign; the Listen panel following whichever tower or approach channel is on the air](docs/images/airband-map.webp)

*Bay Airband, Live (`/airband.html`): every airband channel in the RX-888's VHF window recorded and
transcribed on the Mac at once. Airports light up when their tower is on the air; aircraft are real positions
and tracks from the [adsb.fi](https://adsb.fi) ADS-B feed, and call signs picked out of the transcripts are
matched to them. Listen follows whichever channel is talking.*

![The timeline: one lane per flight (United 1387, American 2331, Delta 2038, United 1905, Alaska 1369…) with each transmission as a dot coloured by phase: check-in, speed/altitude, vector, traffic, cleared, handoff](docs/images/airband-timeline.webp)

*The same session as a timeline: one lane per flight, each call coloured by phase, from check-in to the
handoff to the tower. Scrub or Play to replay; click a dot to read the transcript and hear the clip.*

**Hear one:** [▶ American 2105 on NorCal Approach, 135.650 MHz (5 s, AAC)](docs/audio/american2105-cleared-ils28r.m4a)
([WAV](docs/audio/american2105-cleared-ils28r.wav)), 23 Oct 2026, 11:49 PDT. Whisper's transcript:
*"210 knots and cleared ILS to a right, American 2105"*, a read-back of a speed and an ILS approach
clearance to SFO's runway 28R. Recorded by `voice_session.py` and cleaned the same way the transcriber
sees it (carrier removed, 250–3200 Hz, normalised).

## The hardware

- **HF direct sampling:** LTC2208 16-bit ADC, up to ~130 MSPS, covering roughly 0–64 MHz in one stream.
- **VHF/UHF:** R828D tuner, about 25 MHz–1.8 GHz.
- **Interface:** Cypress FX3 over USB 3. The firmware (`SDDC_FX3.img`) must be loaded into the FX3's RAM on every plug-in; until then it shows up as `04b4:00f3` (bootloader), and afterwards as `04b4:00f1`.
- It needs a real USB 3 (SuperSpeed) link for full sample rates.

## What's here

| Script | What it does |
|---|---|
| **`rxapp/`** | **Your own SDR app: HF (0–32 or 0–64 MHz) and VHF/UHF inputs, browser waterfall, click-to-tune, audio, scans, FT8, FT4 and WSPR on every band at once with world maps and band conditions, an SSTV picture monitor, a live Bay Area airband map, a public-safety monitor (CHP low band, VHF-high), maritime (AIS ships, marine radio, NOAA weather), APRS, server-side monitoring, dashboard, auto-recovery. `./run_rxapp.sh`, then http://127.0.0.1:8090: tabs for Radio, AM / FM / SW, FT8 map, FT4 map, WSPR, Airband, Public safety, Maritime, APRS and SSTV. See [rxapp/README.md](rxapp/README.md)** |
| **`voice_session.py`** | **Records and transcribes every voice channel in view at once, rotating through windows: airband approach and towers, CHP's low band (HF input), VHF-high public safety. Feeds the Airband and Public safety tabs. See [rxapp/README.md](rxapp/README.md#voice-sessions-airband-and-public-safety)** (`airband_session.py` is the old name and still works) |
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

- **ADS-B from the RX-888 itself.** The map shows aircraft from the adsb.fi feed today; the 1090 MHz
  demodulator (`rxapp/modes.py`) is ready and tested, but the current antenna hears nothing at 1090 MHz (see
  [rxapp/README.md](rxapp/README.md#ads-b-not-yet)); needs a 1090 MHz antenna, or a second dongle.
- Infer aircraft positions from what controllers say (altitudes on known approach paths, "N miles south of",
  named fixes), with uncertainty circles.
- Wider live VHF window (~10 MHz) by moving the tuner IF up and opening its widest filter.
- Stereo and RDS for WFM.

## License

GPL-3.0-or-later (see [LICENSE](LICENSE)), matching ka9q-radio, whose patched sources and configs this repo ships.
Every source file carries an `SPDX-License-Identifier` line.

## Credits and third-party licenses

This repo contains only its own code, configuration and the patches below. The programs and libraries it uses
are fetched or installed separately (`tools/` is git-ignored) and stay under their own licenses. If you
redistribute a bundle that includes any of them, include each one's license file, and for the GPL ones the
corresponding source.

**Patched here** (each patch is under the license of the project it modifies)

| Project | License | Patch |
|---|---|---|
| [ka9q-radio](https://github.com/ka9q/ka9q-radio) (Phil Karn, KA9Q) | GPL-3.0 | `ka9q/ka9q-radio-macos.patch` |
| [ka9q-web](https://github.com/wa2n-code/ka9q-web) | GPL-3.0 | `ka9q/ka9q-web-macos.patch` |
| [onion](https://github.com/davidmoreno/onion) | GPL-2.0-or-later or Apache-2.0 | `ka9q/onion-macos.patch` |
| [SDR++](https://github.com/AlexandreRouma/SDRPlusPlus) | GPL-3.0 | `tools/sdrpp_sddc_macos.patch` |
| [ExtIO_sddc / SoapySDDC](https://github.com/ik1xpv/ExtIO_sddc) | MIT | `tools/soapysddc_macos.patch` |

**Run as separate programs** (built into `tools/`, see the READMEs)

| Program | License | Used for |
|---|---|---|
| [ft8_lib](https://github.com/kgoba/ft8_lib) | MIT | FT8 / FT4 decoding |
| [WSJT-X](https://wsjt.sourceforge.io/) `lib/wsprd` | GPL-3.0 | WSPR decoding (its Fortran OSD routine replaced by a C stub) |
| [AIS-catcher](https://github.com/jvde-github/AIS-catcher) | GPL-3.0 | AIS ship positions |
| [multimon-ng](https://github.com/EliasOenal/multimon-ng) | GPL-2.0 | APRS (AFSK1200) |

**Python packages** (`requirements.txt`)

| Package | License |
|---|---|
| numpy, scipy, pyusb, uvicorn | BSD |
| FastAPI, ka9q-python, mlx-whisper (OpenAI Whisper weights: MIT), sstv | MIT |
| pyModeS | GPL-3.0 |
| aprslib | GPL-2.0-or-later |

**In the browser** (loaded from CDNs): [Leaflet](https://leafletjs.com) (BSD-2-Clause),
[D3](https://d3js.org) and [topojson-client](https://github.com/topojson/topojson-client) (ISC),
[world-atlas](https://github.com/topojson/world-atlas) (ISC, from public-domain Natural Earth data).

**Data and services** (fetched at run time; not stored in this repo)

| Source | Terms | How rxapp complies |
|---|---|---|
| [OpenStreetMap](https://www.openstreetmap.org/copyright) map tiles | ODbL data; [tile usage policy](https://operations.osmfoundation.org/policies/tiles/) | Attribution on every map; light, interactive use only |
| [Nominatim](https://nominatim.org) geocoding | [Usage policy](https://operations.osmfoundation.org/policies/nominatim/) | ≤ 1 request/s, identifying User-Agent, results cached, no house numbers |
| [adsb.fi](https://adsb.fi) open data | Personal, non-commercial use; attribution | Credited on the airband map; ≤ 1 request/s |
| [EiBi](http://www.eibispace.de) shortwave schedule | Free to use | Downloaded per season into `rxapp_data/eibi/` |
| Wikipedia, *List of radio stations in the San Francisco Bay Area* | CC BY-SA 4.0 | Only call signs, frequencies, cities and formats (facts) in `rxapp/broadcast.py`, with the source named |
| [AirNav](https://www.airnav.com) / FAA airport frequencies | Public facts | Frequencies only |

**Radio content.** The audio clip and screenshots in `docs/` are the author's own receptions. Recordings and
transcripts (`rxapp_data/`, `recordings/*.wav`) are git-ignored and stay local; in the US, 47 U.S.C. § 605
limits divulging the contents of radio communications other than broadcast, amateur, CB and distress traffic, so
check the rules before publishing recordings or transcripts, especially of public-safety channels.
