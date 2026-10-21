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
the tuner when you tune outside its ±3.6 MHz window (channels outside the window pause until it returns).
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

## Tests

```bash
.venv/bin/python -m pytest tests/test_rxapp.py
```
