#!/bin/sh
# Launch the locally built SDR++. Use its "SoapySDR" source for the RX-888: that goes
# through the official SoapySDDC driver built in tools/ExtIO_sddc (HF + VHF, gains).
HERE="$(cd "$(dirname "$0")" && pwd)"
SDRPP="$HERE/tools/SDRPlusPlus"
export SOAPY_SDR_PLUGIN_PATH="$HERE/tools/ExtIO_sddc/build/SoapySDDC"
exec "$SDRPP/build/sdrpp" -r "$SDRPP/root_dev" "$@"
