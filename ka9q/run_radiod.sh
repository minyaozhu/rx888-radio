#!/bin/sh
# Run ka9q-radio's radiod on macOS with the RX-888 (HF). Ctrl-C to stop.
# Listen:  tools/ka9q-radio/src/monitor am.local
# Tune:    tools/ka9q-radio/src/control rx888-status.local
# Record:  tools/ka9q-radio/src/pcmrecord -d recordings am.local
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE" && exec ../tools/ka9q-radio/src/radiod "${@:-radiod-rx888.conf}"
