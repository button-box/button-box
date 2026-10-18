#!/bin/sh
# Install Button Box from zero inside a booted Raspberry Pi OS arm64 userspace.
# CI runs each step on an arm64 Linux runner with Docker; see docs/ci-pi-image.md.
# Usage: run.sh build-image | boot | install MODE | assert MODE | check MODE | logs
# MODE is wacli (self-hosted, installed with provision.sh) or cloud (setup.sh on
# the Pi, then the Cloud updater bootstrap).
set -eu

HARNESS_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_DIR=$(dirname "$(dirname "$(dirname "$HARNESS_DIR")")")
WORK_DIR=${PI_OS_WORK_DIR:-${RUNNER_TEMP:-/tmp}/pi-os-ci}
CACHE_DIR=${PI_OS_CACHE_DIR:-$WORK_DIR/cache}
IMAGE_URL=${PI_OS_IMAGE_URL:-https://downloads.raspberrypi.com/raspios_lite_arm64/images/raspios_lite_arm64-2026-09-15/2026-09-15-raspios-trixie-arm64-lite.img.xz}
IMAGE_SHA256=${PI_OS_IMAGE_SHA256:-cdf4f3bfac35ae947b46e4e767f935453810549779ac3290e05a6754aee627e5}
ROOTFS_TAG=${PI_OS_ROOTFS_TAG:-button-box-ci/pi-os-rootfs:latest}
BOX_TAG=${PI_OS_BOX_TAG:-button-box-ci/pi-os:latest}
PI_CONTAINER=${PI_CONTAINER:-button-box-pi}
ADMIN=${PI_OS_ADMIN:-admin}
SSH_TARGET="$ADMIN@ci-pi"
# The Pi has no device tree here; comitup.sh and release_verify.py read this
# copy instead (see shim.sh).
FAKE_MODEL="Raspberry Pi 4 Model B Rev 1.5"
export PI_CONTAINER ADMIN

die() {
  echo "pi-os-ci: $*" >&2
  exit 1
}

# Run a shell command on the Pi as the installing administrator, whose shell
# has a strict umask. This is how the NFC venv bug reached a real box.
as_admin() {
  docker exec -u "$ADMIN" -w "/home/$ADMIN" "$PI_CONTAINER" /bin/bash -c "umask 077; $1"
}

build_image() {
  command -v docker >/dev/null || die "docker is required"
  [ "$(uname -m)" = aarch64 ] || die "run on an arm64 Linux host (QEMU is too slow for a full install)"
  mkdir -p "$CACHE_DIR" "$WORK_DIR"
  archive=$CACHE_DIR/$(basename "$IMAGE_URL")
  if [ ! -f "$archive" ]; then
    curl -fL --retry 3 --proto '=https' --tlsv1.2 "$IMAGE_URL" -o "$archive.part"
    mv "$archive.part" "$archive"
  fi
  printf '%s  %s\n' "$IMAGE_SHA256" "$archive" | sha256sum --check --strict

  image=$WORK_DIR/raspios.img
  mount_dir=$WORK_DIR/rootfs
  xz --decompress --stdout --threads=0 "$archive" >"$image"
  loop=$(sudo losetup --find --show --partscan --read-only "$image")
  mkdir -p "$mount_dir"
  sudo mount -o ro "${loop}p2" "$mount_dir"
  sudo mount -o ro "${loop}p1" "$mount_dir/boot/firmware"
  # The boot partition is included so raspi-config edits a real config.txt.
  sudo tar -C "$mount_dir" --numeric-owner --xattrs --xattrs-include='*' -cf - . |
    docker import - "$ROOTFS_TAG"
  sudo umount "$mount_dir/boot/firmware" "$mount_dir"
  sudo losetup --detach "$loop"
  rm -f "$image"

  docker build --build-arg "ROOTFS=$ROOTFS_TAG" --build-arg "ADMIN=$ADMIN" \
    -t "$BOX_TAG" -f "$HARNESS_DIR/Dockerfile" "$HARNESS_DIR"
}

boot() {
  docker rm -f "$PI_CONTAINER" >/dev/null 2>&1 || true
  # systemd is PID 1 so units, journald, D-Bus, NetworkManager and Avahi are
  # real. Host modules are visible so raspi-config can load i2c-dev.
  docker run -d --name "$PI_CONTAINER" --hostname ci-pi \
    --privileged --cgroupns=private \
    --tmpfs /run --tmpfs /run/lock \
    -v /lib/modules:/lib/modules:ro \
    -v "$REPO_DIR:/src:ro" \
    "$BOX_TAG" >/dev/null
  state=
  for _ in $(seq 90); do
    state=$(docker exec "$PI_CONTAINER" systemctl is-system-running 2>/dev/null || true)
    case "$state" in
      running|degraded) break ;;
    esac
    sleep 2
  done
  echo "Pi OS boot state: $state"
  case "$state" in
    running|degraded) ;;
    *)
      docker logs "$PI_CONTAINER" 2>&1 | tail -80
      docker exec "$PI_CONTAINER" systemctl list-jobs --no-pager || true
      die "boot did not finish"
      ;;
  esac
  docker exec "$PI_CONTAINER" systemctl --failed --no-pager || true
  docker exec -i "$PI_CONTAINER" /bin/sh -s <"$HARNESS_DIR/fake-sound.sh"
  docker exec "$PI_CONTAINER" sh -c \
    "mkdir -p /run/ci-device-tree && printf '%s\\000' '$FAKE_MODEL' >/run/ci-device-tree/model"
  docker exec "$PI_CONTAINER" sh -c '. /etc/os-release; echo "$PRETTY_NAME $(uname -m)"; cat /etc/rpi-issue | head -1'
}

install_tailscale() {
  # Optional remote support, run the way provision-tailscale.sh runs it.
  docker exec -i -u "$ADMIN" "$PI_CONTAINER" /bin/bash -c 'umask 077; sudo -n /bin/sh -s' \
    <"$REPO_DIR/scripts/install/tailscale.sh"
}

install_wacli() {
  # Self-hosted owner: provision from another computer over SSH. ssh and rsync
  # are stubs that reach the container instead of a network host.
  source_copy=$WORK_DIR/provision-source
  rm -rf "$source_copy"
  cp -a "$REPO_DIR/." "$source_copy"
  sh "$HARNESS_DIR/shim.sh" "$source_copy"
  PATH="$HARNESS_DIR/host-bin:$PATH" "$source_copy/scripts/provision.sh" "$SSH_TARGET"
  install_tailscale
}

install_cloud() {
  # Clone onto the Pi and run setup.sh there, then make it a Cloud box and
  # install the fixed updater with the attended bootstrap.
  as_admin 'sudo apt-get update -q && sudo apt-get install -y -q --no-install-recommends git'
  as_admin "git config --global --add safe.directory '*' && git clone -q /src button-box && sh button-box/scripts/dev/pi-os-ci/shim.sh button-box"
  as_admin 'cd button-box && ./scripts/setup.sh'
  as_admin "sudo sed -i 's/^MSGBOX_TRANSPORT=.*/MSGBOX_TRANSPORT=cloud/' /etc/messagebox/env && sudo grep -qx 'MSGBOX_TRANSPORT=cloud' /etc/messagebox/env"
  as_admin 'sudo apt-get install -y -q --no-install-recommends minisign espeak-ng'
  # A box installed by setup.sh has no release.json; the signed updater writes
  # it on real boxes. Record this checkout's identity so bootstrap can convert.
  as_admin "cd button-box && printf '{\"version\": \"%s\", \"commit\": \"%s\"}\\n' \"\$(cat VERSION)\" \"\$(git rev-parse HEAD)\" | sudo tee /opt/messagebox/release.json >/dev/null && sudo chmod 0644 /opt/messagebox/release.json"
  as_admin 'cd button-box && sudo ./scripts/install/update-bootstrap.sh auto stable'
  install_tailscale
}

pristine_checkout() {
  as_admin "[ -d button-box-check ] || { git config --global --add safe.directory '*' && git clone -q /src button-box-check; }"
}

case "${1:-}" in
  build-image) build_image ;;
  boot) boot ;;
  install)
    case "${2:-}" in
      wacli) install_wacli ;;
      cloud) install_cloud ;;
      *) die "usage: $0 install wacli|cloud" ;;
    esac
    ;;
  assert)
    pristine_checkout
    docker exec "$PI_CONTAINER" python3 "/home/$ADMIN/button-box-check/scripts/dev/pi-os-ci/assert_install.py" \
      --mode "${2:?mode}" --source "/home/$ADMIN/button-box-check"
    ;;
  check)
    pristine_checkout
    as_admin 'sudo apt-get install -y -q --no-install-recommends make minisign'
    # The runner's pinned uv and Bun builds are arm64 Linux binaries too.
    for tool in uv uvx bun; do
      docker cp -L "$(command -v "$tool")" "$PI_CONTAINER:/usr/local/bin/$tool"
    done
    docker exec "$PI_CONTAINER" ln -sf bun /usr/local/bin/bunx
    as_admin "cd button-box-check && MSGBOX_TEST_TRANSPORT='${2:?mode}' make check"
    ;;
  logs)
    docker exec "$PI_CONTAINER" journalctl --no-pager -n 300 || true
    ;;
  *) die "usage: $0 build-image|boot|install MODE|assert MODE|check MODE|logs" ;;
esac
