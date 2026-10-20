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
| **Listen** | Spectrum + waterfall (2048 bins, ~10 fps), click to tune (snaps to the nearest signal), wheel to step, drag to pan, zoom 100 kHz–full HF. Modes AM, SAM, USB, LSB, CW, NFM. Audio in the browser over a WebSocket, S-meter and SNR. |
| **Channels** | Any number at once (radiod demodulates them all); listen to one, record any. |
| **Scan** | AM broadcast, shortwave broadcast bands, ham bands (peak finding for SSB), CB, and a WWV propagation check (2.5–25 MHz). Each scan uses its own spectrum channel, so the display doesn't move. |
| **Monitor** (server-side) | Scheduled scans (default: AM every 10 min, WWV every 30 min) and *watches*: a channel recorded automatically whenever its SNR rises above a threshold, optionally transcribed on-device. |
| **Dashboard** | Occupancy per channel, detections over time, receiver health (IF power, RF gain, spectrum rate, ADC overloads), event log. |
| **Recovery** | A supervisor restarts radiod when it stops answering (USB unplug, crash) and re-creates every channel with the same SSRC; open pages reconnect and re-create their channel. |

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

- **HF only (0–32.4 MHz at 64.8 MS/s).** radiod's RX-888 driver doesn't drive the VHF tuner; FM/airband
  still go through SoapySDDC (`fm_scan.py`) or SDR++.
- **Transcription needs a one-time macOS permission.** Watches with *transcribe* call the Malahit toolkit's
  `Transcribe.app`; run it once from Terminal and allow Speech Recognition:
  `open -W -n ../Malahit/tools/Transcribe.app --args <some.wav> /tmp/out.txt`.
- **Power is relative.** The S-meter shows radiod's baseband power (dB), not calibrated dBm.
- Several browser tabs share the server's channels; each page uses the first listen channel.

## Tests

```bash
.venv/bin/python -m pytest tests/test_rxapp.py
```
