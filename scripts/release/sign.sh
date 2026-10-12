#!/bin/sh
# The key path is an explicit argument; minisign prompts for its password.
set -eu
exec python3 "$(dirname "$0")/release.py" sign "$@"
