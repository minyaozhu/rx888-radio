#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Run ka9q-radio's radiod on macOS with the RX-888 (HF). Ctrl-C to stop.
# Listen:  tools/ka9q-radio/src/monitor am.local
# Tune:    tools/ka9q-radio/src/control rx888-status.local
# Record:  tools/ka9q-radio/src/pcmrecord -d recordings am.local
#
# The configs say @ROOT@ for this project's folder (radiod needs absolute driver and firmware
# paths); a copy with the real path goes in state/ and radiod runs from that.
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
CONF="${1:-radiod-rx888.conf}"
mkdir -p "$HERE/state"
sed "s#@ROOT@#$ROOT#g" "$HERE/$CONF" > "$HERE/state/$CONF" || exit 1
cd "$HERE" && exec ../tools/ka9q-radio/src/radiod "state/$CONF"
