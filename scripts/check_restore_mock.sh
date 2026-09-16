#!/bin/sh
# Runs only isolated mock projects; requires the already-built release image.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec python3 "$ROOT/scripts/check_restore_mock.py" "$@"
