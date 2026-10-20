# ka9q-radio on macOS with the RX-888

[ka9q-radio](https://github.com/ka9q/ka9q-radio) runs one wideband RX-888 stream through a fast-convolution
channelizer and sends any number of demodulated channels out as RTP multicast. Upstream builds its
interactive tools on macOS but treats the server, `radiod`, as Linux-only. With `ka9q-radio-macos.patch`
the server, the RX-888 driver and the clients run on Apple Silicon.

## What the patch changes

| File | Problem on macOS | Fix |
|---|---|---|
| `multicast.c` (`join_group`) | `MCAST_JOIN_GROUP` fails with EINVAL: BSD kernels require `sin_len` in the group address | Set `sin_len` / `sin6_len` |
| `multicast.c` (`resolve_mcast`) | Bonjour won't resolve the multicast host records radiod publishes, so clients retry forever | For `*.local` names, fall back to `make_maddr()`, the same name hash radiod uses to pick the address |
| `avahi.c` | No `avahi-publish`. When `exec` failed, the forked child *returned into radiod* and ran as a second radiod, fighting over the USB device (affects Linux without avahi too) | Publish with macOS `dns-sd -P`; children `_exit()` if `exec` fails |
| `monitor.c` | Repeater database path hard-coded to `/var/lib` | Use `STATEDIR` |

## Build

```bash
brew install fftw libusb ncurses opus iniparser portaudio libsamplerate libogg gdbm glib gettext libcsv
git clone --depth 1 https://github.com/ka9q/ka9q-radio.git tools/ka9q-radio
git -C tools/ka9q-radio apply ../../ka9q/ka9q-radio-macos.patch
K=$PWD/tools/ka9q-radio
make -C $K/src -j10 ENABLE_RX888=1 pkgdatadir=$K/share pkglibdir=$K/src statedir=$PWD/ka9q/state \
     radiod rx888.so control monitor pcmrecord tune
mkdir -p ka9q/state/repeaters.d
```

The data, driver and state paths point into this project, so nothing is installed system-wide.

## Run

```bash
ka9q/run_radiod.sh                                   # 6 AM broadcast channels from radiod-rx888.conf
tools/ka9q-radio/src/monitor am.local                # listen (all channels, pick with arrow keys)
tools/ka9q-radio/src/control rx888-status.local      # interactive tuning
tools/ka9q-radio/src/tune -r rx888-status.local -s 1050 -f 1050k -m am   # add a channel
tools/ka9q-radio/src/pcmrecord -d recordings am.local                    # record every channel
```

The first lookup of a `.local` name takes about 5 seconds while the mDNS query times out; after that
the derived address is used. radiod holds the RX-888, so close SDR++ first (and stop radiod before
using SDR++ or the Python scripts).

## Tested (2026-10-19, M-series MacBook Pro, macOS 27)

- RX-888 MkII found at USB 3, firmware 2.2, Si5351 set to 64.8 MHz, AGC on.
- Six AM channels (680–910 kHz) demodulated at once; `pcmrecord` wrote all six simultaneously,
  each with real programme audio (24–35% modulation, energy in the voice band).
- `tune` reads and sets channel parameters over the status multicast; `monitor` receives the streams.
- Not yet: 129.6 MHz full rate (0–64 MHz), sustained runs, FFTW wisdom (radiod warns it has none; generate with `fftwf-wisdom` for faster startup).
- radiod can't raise its own thread priority without root (`setpriority` warning); it runs fine without.

Upstream welcomes portability fixes, so this patch is a candidate for a pull request.
