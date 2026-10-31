#!/bin/sh
# Attended draft creation only. This command never publishes a release.
set -eu
exec python3 "$(dirname "$0")/release.py" publish "$@"
