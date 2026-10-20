#!/bin/sh
# Launch the locally built SDR++ (with the RX-888 / SDDC input) from tools/SDRPlusPlus.
SDRPP="$(cd "$(dirname "$0")/tools/SDRPlusPlus" && pwd)"
exec "$SDRPP/build/sdrpp" -r "$SDRPP/root_dev" "$@"
