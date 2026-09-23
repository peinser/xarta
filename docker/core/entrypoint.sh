#!/bin/sh

set -eu

app="$1"
shift
exec python -m "xarta.bin.${app}" --host=0.0.0.0 --workers=1 "$@"
