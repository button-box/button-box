#!/bin/sh
# Install the pinned wacli release for this machine; setup.sh calls this helper.
# Direct usage on the Pi: ./scripts/install/wacli.sh
set -eu

VERSION="0.17.1"
INSTALL_PATH="/usr/local/bin/wacli"
ARCHIVE="wacli_${VERSION}_linux_arm64.tar.gz"
SHA256="8e5d21f8d5f097e5d3a883cdb42848a9e50a7383e4de049c807cc44e6e7c81b6"

case "$(uname -s):$(uname -m)" in
  Linux:aarch64) ;;
  *)
    echo "wacli installation requires Linux arm64." >&2
    exit 1
    ;;
esac

URL="https://github.com/openclaw/wacli/releases/download/v${VERSION}/${ARCHIVE}"

validate_binary() {
  binary=$1
  version_output=$("$binary" --version)
  if [ "$version_output" != "wacli $VERSION" ]; then
    echo "Unexpected wacli version: $version_output" >&2
    exit 1
  fi
  sync_help=$("$binary" sync --help 2>&1)
  for required_flag in --webhook --webhook-secret --webhook-events --webhook-allow-private; do
    if ! printf '%s\n' "$sync_help" | grep -q -- "$required_flag"; then
      echo "wacli $VERSION lacks required sync flag: $required_flag" >&2
      exit 1
    fi
  done
}

if [ -x "$INSTALL_PATH" ] && [ "$("$INSTALL_PATH" --version 2>/dev/null)" = "wacli $VERSION" ]; then
  validate_binary "$INSTALL_PATH"
  "$INSTALL_PATH" --version
  exit 0
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

curl -fL "$URL" -o "$TMP_DIR/$ARCHIVE"
printf '%s  %s\n' "$SHA256" "$TMP_DIR/$ARCHIVE" | sha256sum -c -
tar -xzf "$TMP_DIR/$ARCHIVE" -C "$TMP_DIR" ./wacli
validate_binary "$TMP_DIR/wacli"
sudo install -m 0755 "$TMP_DIR/wacli" "$INSTALL_PATH"
validate_binary "$INSTALL_PATH"
"$INSTALL_PATH" --version
