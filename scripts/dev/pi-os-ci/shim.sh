#!/bin/sh
# CI only: point the two Raspberry Pi model probes at /run/ci-device-tree/model.
# A container cannot create /proc/device-tree, and these are the only readers.
# The count check makes this fail loudly when the install scripts change.
# Usage: shim.sh CHECKOUT_DIR
set -eu

checkout=${1:?usage: shim.sh CHECKOUT_DIR}

redirect() {
  file=$checkout/$1
  expected=$2
  found=$(grep -c 'proc/device-tree/model' "$file" || true)
  if [ "$found" != "$expected" ]; then
    echo "shim: expected $expected device-tree reads in $1, found $found" >&2
    exit 1
  fi
  sed -i 's#proc/device-tree/model#run/ci-device-tree/model#g' "$file"
}

redirect scripts/install/comitup.sh 2
redirect scripts/install/release_verify.py 1
