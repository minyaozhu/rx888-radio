#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Web UI for radiod (spectrum, waterfall, tuning, audio): http://localhost:8081
# Start ka9q/run_radiod.sh first.
HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE/tools/ka9q-web" && exec ./ka9q-web -m rx888-status.local -p 8081 -n "RX-888 MkII HF, macOS" "$@"
