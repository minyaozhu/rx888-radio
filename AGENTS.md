# AGENTS.md: working on rx888-radio

Instructions for coding agents (Codex, Claude Code, Antigravity, others). The READMEs are the full reference:
`README.md` (setup, SDR++), `ka9q/README.md` (radiod build), `rxapp/README.md` (the app, every feature, tests).

## What this is

An RX-888 MkII SDR on an Apple-silicon Mac. **rxapp** (`rxapp/`, Python FastAPI) drives ka9q-radio's `radiod`
over its multicast control protocol and serves the browser UI (`web/`) at http://127.0.0.1:8090.
**voice_session.py** runs beside it: it rotates the receiver through airband / public-safety / marine / APRS
windows, opens channels on what keys up and transcribes them on-device (mlx-whisper).

## Prerequisites (the human has to provide these)

- RX-888 MkII on a **USB 3** port; HF antenna on the HF input, VHF/UHF antenna on the R828D tuner input.
- Apple-silicon Mac, Homebrew, Python 3.13.
- `fw/SDDC_FX3.img` from the [ik1xpv/ExtIO_sddc](https://github.com/ik1xpv/ExtIO_sddc) releases (not committed).

## Set up, in this order (check each step before the next)

```bash
# 1. ka9q-radio, patched for macOS (details: ka9q/README.md)
brew install fftw libusb ncurses opus iniparser portaudio libsamplerate libogg gdbm glib gettext libcsv
git clone --depth 1 https://github.com/ka9q/ka9q-radio.git tools/ka9q-radio
git -C tools/ka9q-radio apply ../../ka9q/ka9q-radio-macos.patch
K=$PWD/tools/ka9q-radio
make -C $K/src -j10 ENABLE_RX888=1 pkgdatadir=$K/share pkglibdir=$K/src statedir=$PWD/ka9q/state \
     radiod rx888.so control monitor pcmrecord tune
mkdir -p ka9q/state/repeaters.d
# check: tools/ka9q-radio/src/radiod exists

# 2. Python
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q tests/          # check: all pass (no radio needed; needs step 1's clone)

# 3. Decoders in tools/ (each optional; its page says so if missing). Exact commands: rxapp/README.md
#    ft8_lib (FT8 / FT4), WSJT-X lib/wsprd with the osd stub (WSPR), multimon-ng (APRS), AIS-catcher (AIS)

# 4. Run
./run_rxapp.sh                                # starts and supervises radiod
curl -s 127.0.0.1:8090/api/health             # check: "ok": true
```

The voice session (start it after rxapp; full window list as used here):

```bash
.venv/bin/python voice_session.py --dwell 300 \
  --windows lowband:hf:42.6:safety,vhfhigh:vhf:155.0:safety,approach:vhf:135.7,towers:vhf:120.6,aprs:vhf:146.0:aprs,marine:vhf:159.9:marine:hold \
  --preload 135.65,133.175,135.1,134.55,133.7,133.95,134.5,135.275,133.775,120.5,121.8,118.3,121.7 \
  >> rxapp_data/voice_live.out 2>&1 &
```

Start long-running processes fully detached (`nohup ... &` with an absolute log path, stdin from /dev/null).
A process started inside an agent's sandboxed or task-tracked shell can die with the session, or lose macOS
permissions (Input Monitoring, LaunchServices).

## Code map

| Path | What |
|---|---|
| `rxapp/server.py` | FastAPI app: lifespan wiring, every `/api/*` route and `/ws/*` WebSocket |
| `rxapp/radio.py` | Channels in radiod (create / tune / park), HF vs VHF input, VHF tuner moves |
| `rxapp/vhf.py` | R828D tuner (8.4 MHz window, inverted IF: `RF = tuner + (IF − f_if)`) |
| `rxapp/rtp.py` | RTP audio by SSRC; learns channels per payload type (stereo FM) |
| `rxapp/spectrum.py`, `panels.py` | Display spectrum; the Radio tab's 2×2 panel feeds (`/ws/panels`) |
| `rxapp/jobs.py`, `monitor.py` | Band scanner, recorder, scheduled scans and watches |
| `rxapp/ft8.py`, `wspr.py`, `sstvmon.py` | Server-side digital-mode monitors |
| `rxapp/broadcast.py`, `ssb.py`, `places.py`, `adsb.py`, `ais.py`, `aprs.py` | Station lists, EiBi, geocoding, feeds |
| `rxapp/supervisor.py` | Restarts radiod, recreates channels |
| `voice_session.py` | Window rotation, carrier detection, transcription; heals after an rxapp restart |
| `web/index.html` + `controller.js` | Tab shell; keyboard / dial controller |
| `web/radio.html`, `app.js`, `panels.js` | Radio tab |
| `web/*.html` + `*.js` | One page per tab (broadcast, ssb, ft8 + ft8viz, wspr, airband, safety, marine, aprs, sstv) |
| `tests/test_rxapp.py` | Unit tests (pure functions, no radio) |
| `ka9q/` | macOS patches, radiod configs (`@ROOT@` filled into `ka9q/state/` at run time) |

## Conventions

- Every source file starts with `SPDX-License-Identifier: GPL-3.0-or-later` (Python/shell `#`, JS `//`, HTML `<!-- -->`).
- Pages load scripts as `name.js?v=N`: **bump N** in the HTML when you change the script. HTML and JS are
  served `Cache-Control: no-cache`, but long-open tabs still need a reload.
- Match the surrounding style: small functions, comments that say *why*, no new frameworks or build steps.
- Run `.venv/bin/python -m pytest -q tests/` and `node --check web/<file>.js` after changes; add a test for new
  pure logic.
- Commits: one topic each, imperative subject, body explaining why. Author email is the GitHub noreply address
  set in this repo's git config; keep it. Never commit with a personal address.
- Update `rxapp/README.md` (and `README.md` if user-visible) in the same commit as the feature.

## Never

- Commit anything from `rxapp_data/` (recordings, transcripts, databases, settings), `recordings/*.wav|iq|raw`,
  `fw/*.img` or `tools/` builds. Check `git status` before committing.
- Publish public-safety audio or transcripts (47 U.S.C. § 605); airband clips only with care.
- Exceed 1 request/s to adsb.fi or Nominatim, drop their caching or User-Agent, or use adsb.fi data commercially.
- Change the user's receiver state (input, tuner, voice-session focus) and leave it changed: note it, restore it.
- Force-push, rename the repo or change its visibility without the owner's explicit request.

## Pitfalls already learned

- **One input at a time.** HF (0–32 / 0–64 MHz) or VHF, and VHF shows one 8.4 MHz window. Moving the tuner moves
  every VHF channel; channels outside the window are *parked*.
- **Restarting rxapp forgets every channel.** Pages recreate theirs (with `keep_tuner`), voice_session.py heals
  within ~10 s. Shutdown can hang: `kill`, wait, then `kill -9`; never leave two rxapp processes.
- **Iterating `radio.channels` or the panel feed dict across an `await`** needs `list(...)`: requests add to them.
- **radiod WFM stereo** switches between stereo and mono packets as the pilot comes and goes, each with its
  own dynamic RTP payload type. Never assume a channel's format; use `rtp.channels(ssrc)`.
- **Changing a channel's mode resets its filter** (preset); WFM output is 48 kHz.
- **The Ulanzi Dial** sends synthetic keys without modifier flags; the controller tracks Shift / Ctrl / Alt / Cmd
  itself. macOS remapping (`hidutil`), IOHID readers and Karabiner all see zeroed reports from it; only Ulanzi
  Studio can change what it sends.
- **swiftc's default target can be newer than the Mac**; pass `-target arm64-apple-macos12.0`.

## Checking your work against the radio

Use the API, not guesses: `/api/health`, `/api/info`, `/api/channels`, `/api/voice/focus`, the session status
in `rxapp_data/<service>/<stamp>/status.json`, and the logs `rxapp_data/rxapp.log` and `rxapp_data/voice_live.out`.
For a live test make your own temporary channel (`POST /api/channels`, label it as a test), and delete it after.
