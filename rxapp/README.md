# rxapp

A browser SDR and monitoring app for the RX-888, built on ka9q-radio's `radiod` (the macOS port in
[`../ka9q`](../ka9q/README.md)). `radiod` owns the radio and demodulates any number of channels at once;
rxapp is its only client.

```
RX-888 ─USB─▶ radiod ──multicast (loopback)──▶ rxapp (FastAPI) ──HTTP/WebSocket──▶ browser
                status/commands 5006, RTP audio 5004
```

## Run

```bash
./run_rxapp.sh            # starts radiod too if it isn't running; open http://127.0.0.1:8090
```

To run it at login and keep it running: see `service/com.rx888.rxapp.plist` (a LaunchAgent).
Only one program can use the RX-888, so stop rxapp before using SDR++, ka9q-web's radiod session
or the SoapySDDC scripts.

## What it does

| | |
|---|---|
| **Inputs** | **HF** (direct sampling) or **VHF** (the R828D tuner, 24–1700 MHz), switched from the page. HF **coverage** 0–32 MHz (radiod at 64.8 MS/s) or 0–64 MHz (129.6 MS/s, ~90% of one core); switching restarts radiod and restores the channels. |
| **Listen** | Spectrum + waterfall (2048 bins, ~10 fps), click to tune (snaps to the nearest signal), ←/→ or wheel to step (Shift ×10), drag to pan. The same Coverage and Span menus on both inputs (100 kHz up to 0–32 or 0–64 MHz); on VHF, spans up to 8.4 MHz are live and wider ones are *swept* (the tuner steps across the range, ~2.3 s per update for 64 MHz, VHF audio paused; clicking a signal returns to live). **Gain**: HF −41.5…+34 dB (below −10 dB the attenuator) or Auto; VHF tuner IF gain 0–15. HF modes AM, SAM, USB, LSB, CW, NFM; VHF modes WFM, NFM, AM. Audio in the browser over a WebSocket, S-meter and SNR. |
| **Channels** | Any number at once (radiod demodulates them all); listen to one, record any. |
| **Scan** | HF: AM broadcast, shortwave broadcast bands, ham bands (peak finding for SSB), CB, WWV propagation check. VHF: FM broadcast, airband, 2 m, NOAA weather; bands wider than the tuner window are scanned in steps (FM: 4 steps, ~25 s), which briefly moves the tuner. HF scans use their own spectrum channel, so the display doesn't move. |
| **Monitor** (server-side) | Scheduled scans (default: AM every 10 min, WWV every 30 min) and *watches*: a channel recorded automatically whenever its SNR rises above a threshold, optionally transcribed on-device. |
| **FT8** (server-side) | Every FT8 band at once (160 m–10 m, plus 6 m with 0–64 MHz coverage): one USB channel per dial frequency, cut into 15 s UTC slots and decoded with ft8_lib. Live message list, conversations grouped by station pair (✓ when they reach RR73/73), CQ callers with grids, search by callsign or grid. Keeps running across restarts; pauses while VHF is selected. |
| **Airband** (with `airband_session.py`) | Every airband channel in the tuner's 8.4 MHz window recorded and transcribed at once, alternating between the NorCal approach and Bay Area tower windows; a live map page with all Bay Area airports. See [Airband](#airband). |
| **SSTV** (server-side) | Watches the SSTV calling frequencies (3.845 and 7.171 MHz LSB; 14.230, 14.233, 21.340, 28.680 MHz USB) and decodes every picture into a gallery, with the audio kept. |
| **Dashboard** | Occupancy per channel, detections over time, receiver health (IF power, RF gain, spectrum rate, ADC overloads), event log. |
| **Recovery** | A supervisor restarts radiod when it stops answering (USB unplug, crash) and re-creates every channel with the same SSRC; open pages reconnect and re-create their channel. |

## How VHF works with radiod

radiod's RX-888 driver has no working VHF support ("VHF tuner does not work yet"), but it doesn't need
it: radiod keeps sampling the ADC as on HF, and rxapp flips the RX-888's antenna switch to the tuner and
programs the R828D over USB control transfers (`rxapp/vhf.py`, using ka9q-radio's
`rx888_vhf_demo/rx888_vhf.py`). The tuner mixes the wanted signal to a 4.57 MHz IF with its LO above the
signal, so the IF spectrum is inverted:

    radiod frequency = 4.57 MHz − (RF − tuner centre)       (USB/LSB swap)

Channels keep their real frequency and band; rxapp maps them, flips and relabels the spectrum, and moves
the tuner when you tune outside its ±4.2 MHz window (channels outside the window pause until it returns).
The tuner's LNA and mixer run on their own AGC with the IF VGA at 6 (adjustable, "Tuner gain"); radiod's
RF AGC then holds the ADC at about −20 dBFS. Expect a burst of ADC overloads for a second or two right
after switching to VHF while radiod's AGC settles. Channels that stop producing audio for 5 s (seen once,
right after a switch) are re-created automatically.

Channels of the input not in use are *parked* (kept by rxapp, absent from radiod); HF watches and HF scan
schedules pause while VHF is selected, and the other way round.

## HF gain

Manual gain goes to radiod as RF gain (AD8370) plus attenuation (PE4312). radiod's own RF AGC turns
off for good after the first manual command, so **Auto** is rxapp's AGC (`rxapp/hfgain.py`), holding the
ADC between −26 and −15 dBFS like radiod's. The same amplifier sits in the VHF tuner's IF path, so on VHF
the gain is always automatic and a manual HF value comes back when you return to HF.

## FT8

Needs ft8_lib's decoder, built into `tools/ft8_lib` (not committed):

```bash
git clone --depth 1 https://github.com/kgoba/ft8_lib.git tools/ft8_lib
make -C tools/ft8_lib decode_ft8 CFLAGS="-O3 -DHAVE_STPCPY -I. -include stdio.h"   # stdio.h: current macOS SDKs
```

Slot timing comes from RTP timestamps, anchored to the wall clock by the least-delayed packet, and
corrected for radiod's ~0.3 s filter delay; measured decode `dt` is centred on 0 like WSJT-X. The host
clock must be right (macOS keeps it NTP-synced). `score` is ft8_lib's rough SNR estimate, not WSJT-X's.
Decodes are in the `ft8` table of `rxapp_data/rxapp.sqlite`; the audio isn't kept.

**FT8 Skywatch** (`/ft8.html`, linked from the FT8 tab) shows the decodes like the
[airband page](https://minyaozhu.github.io/malahit-airband-monitor/): a world map (stations at their grid
centre, coloured by band; hollow when placed only by callsign prefix; lines from each station to the one
it's calling; rings for CQ) and a timeline with one lane per station, dots coloured by message phase
(CQ → answer + grid → report → roger + report → RR73/73). Scrub or Play to replay a window of 15 min to
24 h; at the end of the slider it follows live. Click a dot or station to read the message in plain
English, with distance from your locator ("Home", default CM87; `POST /api/ft8/qth`). The map libraries
(d3, topojson, world-atlas) load from jsDelivr, so the page needs internet.

API: `GET/POST /api/ft8` (status / `{on, bands}`), `GET /api/ft8/decodes?after_id=`,
`GET /api/ft8/conversations?hours=`, `GET /api/ft8/stats?hours=`.

## SSTV

Each SSTV channel's last 20 s of audio is checked every 5 s with the Malahit toolkit's detector
(energy in the 1100–2350 Hz video band, plus a 1200 Hz line-sync pulse repeating at the mode's line
period). A hit starts a capture that includes the 40 s before it (so the VIS header that names the mode
is in it) and ends 20 s after SSTV stops (or at 6 min). The capture goes to the
[`sstv`](https://pypi.org/project/sstv/) decoder (a Rust crate: Martin, Scottie, Robot, PD, Pasokon,
Wrasse modes); pictures go to `rxapp_data/sstv/` and the `sstv` table, and captures with no picture are
dropped. Measured on band noise, the detector's sync score sits at 0.04–0.12 against a 0.25 trigger; each
channel's first 30 s are ignored because its AGC settling looks like sync. The decoder needs the VIS
header, so a picture already under way when the monitor starts is missed.

API: `GET/POST /api/sstv` (status / `{on}`), `GET /api/sstv/pictures`, `GET /api/sstv/pictures/{id}.png|.wav`,
`DELETE /api/sstv/pictures/{id}`.

## Airband

`airband_session.py` runs next to rxapp. It switches rxapp to the VHF input and watches radiod's spectrum of
the whole tuner window (a private spectrum stream; the page's view is untouched):

```bash
.venv/bin/python airband_session.py --windows approach:135.7,towers:120.6 --dwell 300 \
    --preload 135.65,133.95,134.5,135.1,120.5,118.3,124.0,118.6,119.0,119.55,120.2,119.8,118.1,119.7
```

- **Windows.** The tuner shows 8.4 MHz at a time, so the session alternates: *approach* (131.6–137 MHz,
  NorCal Approach and some ATIS) and *towers* (118–124.7 MHz: the towers of SFO, OAK, SJC, PAO, SQL, NUQ, HWD,
  RHV, LVK, CCR and APC, most of their grounds, NorCal departure). It switches after `--dwell` seconds, in a
  quiet moment (or a minute later regardless). `--center` alone watches one window.
- **Capture.** A transmission is an AM carrier on the 25 kHz grid rising 12 dB over the window's median
  floor; it ends 0.7 s after the carrier goes. Only the strongest channel in a cluster counts (a strong carrier
  spills into its neighbours), a cluster lit across 3+ channels with no clear peak is a wide non-voice signal
  and is ignored, and a carrier that never goes away is a spur. Each channel that keys up gets an rxapp channel
  of kind `air` (the page can listen to it but not retune it); its audio is cut by the carrier timing.
- **Transcription.** On-device Whisper (mlx-whisper, large-v3-turbo) on DC-removed, 250–3200 Hz band-passed,
  normalised audio (fed the raw AM, Whisper called almost everything "no speech"). Two readings per
  transmission: plain, and with an ATC vocabulary prompt. Sound captions ("BELL RINGS") are dropped.
  `--retranscribe <session dir>` redoes a session.
- **Files.** `rxapp_data/airband/<stamp>/`: one WAV per transmission, `transmissions.jsonl`
  (`transcripts.jsonl` after a re-transcription), `status.json` (live: channels, who is on the air, window).

![Bay Airband, Live: the map](../docs/images/airband-map.webp)

![Bay Airband, Live: the per-flight timeline](../docs/images/airband-timeline.webp)

**The page** (`/airband.html`, after the Malahit toolkit's
[Quiet Bridge Arrivals](https://minyaozhu.github.io/malahit-airband-monitor/)):

- A Bay Area map (Leaflet + OpenStreetMap) with 12 airports and their tower/ground/ATIS frequencies
  (`web/bayair.js`, from AirNav / the FAA Chart Supplement). An airport lights up with the latest words while one
  of its frequencies is on the air; NorCal sectors show as chips under the map. ★ saves the current view as
  the default for that browser, ⌂ returns to it.
- Flights are picked out of the transcripts (airline + number, joining split numbers like "United 15, 75";
  or the number ending a read-back) and phases from key words. A flight talking to a tower sits at that
  airport; one with NorCal Approach sits along the SFO Quiet Bridge arrival path at the furthest stage it has
  been told. **These are inferred, not radar positions.**
- **Listen**: *Follow activity* (default) stays on a channel while it is active and jumps to whoever is on
  the air once it has been quiet for 0.8 s; or *Loudest*, or one frequency.
- Timeline with one lane per flight, scrub/Play, and the transcript log; click a call to read it (both
  readings) and play its clip. Earlier sessions are in the *Session* menu.

API: `GET /api/airband?session=` (status, sessions), `GET /api/airband/transmissions?session=&after=`,
`GET /api/airband/clip/{session}/{wav}`.

## ADS-B (not yet)

`rxapp/modes.py` demodulates Mode S at 2 MS/s (dump1090's preamble test and PPM slicing; pyModeS checks
the CRC and decodes); it recovers synthetic frames in noise (tests). On the air it found nothing: with the
tuner locked at 1090 MHz and a radiod IQ channel at 2 MS/s, 21 s of capture were pure noise. Measured through
the VHF input with the current antenna, FM broadcast is 29 dB over the noise, airband 19 dB, cell towers at
751–880 MHz only 3–8 dB, 1090 MHz nothing: the antenna doesn't work at UHF. The R828D's PLL does lock at
1090 MHz (the demo driver read the lock bit after 2 ms and reported false "not locked"; it now waits 10 ms like
librtlsdr). With a 1090 MHz antenna, ADS-B would be a third window in the airband rotation.

## Design notes

- **No mDNS.** radiod derives each multicast address from its name (`make_maddr`, an FNV-1 hash);
  `rxapp/addr.py` does the same, so nothing depends on Bonjour resolving radiod's groups.
- **Loopback multicast.** radiod runs with `ttl = 0`, so every socket joins on 127.0.0.1.
- **Commands via [ka9q-python](https://github.com/HamSCI/ka9q-python)**, audio via rxapp's own RTP receiver
  (`rxapp/rtp.py`): ka9q-python's stream helpers assume the default interface and its
  `ensure_channel` verification times out on macOS, but its control and status calls work.
- **Channels expire on their own.** Each is created with a 30 s lifetime and refreshed every 10 s, so a
  crashed rxapp never leaves radiod demodulating forever. AGC is enabled with an explicit command after
  creation (the flag in the create command doesn't take on this radiod; without it audio sits near −55 dBFS).
- **SNR** is estimated as baseband power over (noise density × filter width).
- **Storage**: `rxapp_data/rxapp.sqlite` (scans, detections, recordings, health, events) and
  `rxapp_data/recordings/*.wav`.

## API (selection)

| Method | Path | |
|---|---|---|
| GET | `/api/health` | radiod/front-end health |
| GET/POST | `/api/channels` | list / create `{freq_hz, mode, label}` |
| PATCH/DELETE | `/api/channels/{ssrc}` | retune `{freq_hz, mode}` / remove |
| POST/DELETE | `/api/channels/{ssrc}/record` | start / stop recording |
| POST | `/api/spectrum` | move/zoom the view `{center_hz, bin_hz}` |
| POST | `/api/scan/{band}` | scan now |
| GET/POST/DELETE | `/api/schedules`, `/api/watches` | server-side monitoring |
| GET | `/api/occupancy/{band}`, `/api/timeline/{band}`, `/api/health/history` | dashboard data |
| WS | `/ws/audio/{ssrc}` | u32 sample rate + int16 LE PCM per frame |
| WS | `/ws/spectrum` | f64 center, f32 bin width, u32 n, n × f32 dB |
| WS | `/ws/status` | channels, health, view, once a second |

## Known limits

- **VHF tuner gain** is one IF VGA setting plus the chip's own LNA/mixer AGC; there's no manual LNA control
  in the UI yet. Very strong local signals may need a lower "Tuner gain".
- **WFM is mono** (radiod's `wfm` preset with `mono = yes`); no RDS.
- **Transcription needs a one-time macOS permission.** Watches with *transcribe* call the Malahit toolkit's
  `Transcribe.app`; run it once from Terminal and allow Speech Recognition:
  `open -W -n ../Malahit/tools/Transcribe.app --args <some.wav> /tmp/out.txt`.
- **Power is relative.** The S-meter shows radiod's baseband power (dB), not calibrated dBm.
- Several browser tabs share the server's channels; each page uses the first listen channel.
- **USB drops.** This RX-888 has dropped off USB several times (it re-enumerates, sometimes on another bus).
  radiod's supervisor restarts it; the VHF tuner code then reopens its USB handle and re-programs the tuner
  (it used to call `sys.exit` and take rxapp down). A cable or port change is still worth trying.
- **One tuner.** Airband, FM and ADS-B all share the VHF tuner's 8.4 MHz window, and while the input is on VHF
  the FT8/SSTV monitors (HF) pause.

## Tests

```bash
.venv/bin/python -m pytest tests/test_rxapp.py
```

They need the ka9q-radio clone (`tools/ka9q-radio`, see [ka9q/README.md](../ka9q/README.md)): the VHF code
imports its `rx888_vhf_demo` driver. No radio is needed.
