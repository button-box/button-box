#!/bin/sh
# CI only, run as root inside the Pi OS container. The runner kernel has no
# sound subsystem, so /sys/class/sound does not exist. Replace /sys/class with
# a tmpfs that bind-mounts every real class back, then add one USB speaker
# (card 0, "Device") and one microphone (card 1, "mic") laid out like sysfs.
# audio_config.py and messagebox-audio-detect.service then run unchanged.
set -eu

mkdir -p /run/ci-sys-class
mount --bind /sys/class /run/ci-sys-class
mount -t tmpfs -o mode=0755 ci-sys-class /sys/class
for entry in /run/ci-sys-class/*; do
  name=$(basename "$entry")
  mkdir "/sys/class/$name"
  mount --bind "$entry" "/sys/class/$name"
done

sound=/sys/class/sound
mkdir -p "$sound/usb/1-1.2/1-1.2:1.0" "$sound/card0" "$sound/card1" \
  "$sound/pcmC0D0p" "$sound/pcmC1D0c"
printf '0d8c\n' >"$sound/usb/1-1.2/idVendor"
printf '0014\n' >"$sound/usb/1-1.2/idProduct"
printf 'Device\n' >"$sound/card0/id"
printf 'mic\n' >"$sound/card1/id"
ln -s ../usb/1-1.2/1-1.2:1.0 "$sound/card0/device"
mount -o remount,ro /sys/class
