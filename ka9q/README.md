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

## Web UI: ka9q-web

[ka9q-web](https://github.com/wa2n-code/ka9q-web) (WA2N/WA2ZKD, after G0ORX) gives radiod a browser UI:
spectrum, waterfall, click-to-tune, S-meter/SNR and audio, at http://localhost:8081.

```bash
brew install libevent epoll-shim gnutls libgcrypt
git clone --depth 1 https://github.com/davidmoreno/onion.git tools/onion
git -C tools/onion apply ../../ka9q/onion-macos.patch
cmake -S tools/onion -B tools/onion/build -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX=$PWD/tools/onion/install -DCMAKE_PREFIX_PATH=/opt/homebrew \
  -DCMAKE_C_FLAGS="-D_DARWIN_C_SOURCE -I/opt/homebrew/include/libepoll-shim -I/opt/homebrew/include" \
  -DCMAKE_SHARED_LINKER_FLAGS="-L/opt/homebrew/lib -lepoll-shim-interpose -lepoll-shim" \
  -DONION_POLLER=epoll -DONION_USE_SSL=ON -DONION_USE_PAM=OFF -DONION_USE_PNG=OFF -DONION_USE_JPEG=OFF \
  -DONION_USE_XML2=OFF -DONION_USE_SYSTEMD=OFF -DONION_USE_SQLITE3=OFF -DONION_USE_REDIS=OFF -DONION_USE_GC=OFF \
  -DONION_USE_TESTS=OFF -DONION_EXAMPLES=OFF -DONION_USE_BINDINGS_CPP=OFF -DONION_USE_PTHREADS=ON
cmake --build tools/onion/build -j10 --target onion && cmake --install tools/onion/build

git clone --depth 1 https://github.com/wa2n-code/ka9q-web.git tools/ka9q-web
git -C tools/ka9q-web apply ../../ka9q/ka9q-web-macos.patch
make -C tools/ka9q-web ONION=$PWD/tools/onion/install RESOURCES_BASE_DIR=$PWD/tools/ka9q-web

ka9q/run_radiod.sh &      # radiod first
ka9q/run_ka9q_web.sh      # then open http://localhost:8081
```

(Onion's `make install` stops at its template tools, which don't link on macOS and aren't needed; if
`tools/onion/install/include/onion` is missing, copy `src/onion/*.h` and `build/src/onion/*.h` there.)

What it took on macOS:

| Component | Problem | Fix |
|---|---|---|
| Onion | Built with `-D_POSIX_C_SOURCE`, which hides `strcasecmp`, `strndup`, `DT_DIR` on macOS | Compile with `-D_DARWIN_C_SOURCE` |
| Onion | Default poller is Linux `epoll` | Use [epoll-shim](https://github.com/jiixyj/epoll-shim) (epoll over kqueue); define the missing `struct itimerspec` |
| Onion | Its `libev`/`libevent` pollers run one loop from many threads, so a WebSocket handler that blocks freezes the whole server (libevent's also relies on unnamed POSIX semaphores, which macOS lacks) | Don't use them; the epoll poller lets each thread wait independently. The libevent poller's signature and semaphore are fixed in the patch anyway |
| Onion | WebSocket handshake needs SHA-1, which Onion only gets from GnuTLS; without it the server **aborts** on the first browser connection | Build with `ONION_USE_SSL=ON` |
| ka9q-web | Same multicast issues as radiod (`sin_len`, `.local` lookup) | Same fixes as `ka9q-radio-macos.patch` |
| ka9q-web | `<bsd/string.h>`, `-lbsd` | libc on macOS |
| ka9q-web | Two-argument `pthread_setname_np` | No-op on macOS (names are cosmetic) |
| ka9q-web | Three global mutex/condvar left zero-initialised: valid on Linux, not on macOS, so `audio_thread` spun at 100% CPU | `PTHREAD_*_INITIALIZER` (correct everywhere) |

Tested: the page loads, the WebSocket streams spectrum and audio (about 16 messages/s), the frequency box
retunes radiod (860 kHz: −55 dBm, S9+18, SNR 36 dB), and the server idles at 0% CPU. Browsers only
start audio after a click on the page.

Upstream welcomes portability fixes, so this patch is a candidate for a pull request.
