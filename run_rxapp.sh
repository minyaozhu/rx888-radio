#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Start rxapp (it starts and supervises radiod itself). Open http://127.0.0.1:8090
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE" && exec .venv/bin/python -m rxapp "$@"
