#!/bin/sh
# Interactively test Button Box network and attached hardware without messaging.
# Installed helper: /opt/messagebox/dev/hardware-test.sh
set -u

CONFIG_FILE=/etc/messagebox/env
TEST_WORK_DIR=/var/lib/messagebox/state
RINGTONE=/opt/messagebox/ringtones/ring1.wav

config_value() {
  if [ ! -r "$CONFIG_FILE" ]; then
    return
  fi
  key=$1
  while IFS= read -r line; do
    case "$line" in
      "$key="*) printf '%s\n' "${line#*=}"; return ;;
    esac
  done <"$CONFIG_FILE"
}

MIC_DEV=${MSGBOX_MIC_DEV:-$(config_value MSGBOX_MIC_DEV)}
MIC_DEV=${MIC_DEV:-plughw:CARD=mic,DEV=0}
SPK_DEV=${MSGBOX_SPK_DEV:-$(config_value MSGBOX_SPK_DEV)}
SPK_DEV=${SPK_DEV:-plughw:CARD=Device,DEV=0}
BUTTON_PIN=${MSGBOX_BUTTON_PIN:-$(config_value MSGBOX_BUTTON_PIN)}
BUTTON_PIN=${BUTTON_PIN:-17}
LED_PIN=${MSGBOX_LED_PIN:-$(config_value MSGBOX_LED_PIN)}
LED_PIN=${LED_PIN:-26}
NFC_RESET_PIN=${MSGBOX_NFC_RESET_PIN:-$(config_value MSGBOX_NFC_RESET_PIN)}
NFC_RESET_PIN=${NFC_RESET_PIN:-D20}
NFC_REQUEST_PIN=${MSGBOX_NFC_REQUEST_PIN:-$(config_value MSGBOX_NFC_REQUEST_PIN)}
NFC_REQUEST_PIN=${NFC_REQUEST_PIN:-D16}
NFC_VENV=/opt/messagebox/venv-nfc
WACLI_STORE_DIR=/var/lib/messagebox/wacli
export WACLI_STORE_DIR
RECORDING=$(mktemp --suffix=.wav)
FAILURES=""

cleanup() {
  rm -f "$RECORDING"
}

interrupted() {
  trap - EXIT
  cleanup
  exit 130
}

trap cleanup EXIT
trap interrupted HUP INT TERM

if [ ! -t 0 ]; then
  printf 'Run this guided test from an interactive terminal.\n' >&2
  exit 2
fi
if [ ! -d "$TEST_WORK_DIR" ] || [ ! -w "$TEST_WORK_DIR" ]; then
  printf 'Hardware test working directory is not writable: %s\n' "$TEST_WORK_DIR" >&2
  exit 1
fi
cd "$TEST_WORK_DIR" || exit 1

pass() {
  printf 'PASS: %s\n\n' "$1"
}

fail() {
  printf 'FAIL: %s\n\n' "$1"
  FAILURES="$FAILURES\n- $1"
}

continue_prompt() {
  printf 'Press Enter to continue. '
  if ! read -r _answer; then
    printf '\nInput closed; stopping the test.\n' >&2
    exit 2
  fi
}

confirm() {
  question=$1
  while true; do
    printf '%s [Y/n]: ' "$question"
    if ! read -r answer; then
      printf '\nInput closed; stopping the test.\n' >&2
      exit 2
    fi
    case "$answer" in
      ""|y|Y|yes|YES) return 0 ;;
      n|N|no|NO) return 1 ;;
      *) printf 'Please answer y or n.\n' ;;
    esac
  done
}

printf '\nButton Box guided hardware test\n'
printf 'No WhatsApp messages will be sent and no NFC card IDs will be shown.\n\n'

printf '1. Network\n'
if curl -fsSI --max-time 10 https://github.com/ >/dev/null; then
  pass "Internet connection"
else
  fail "Internet connection"
fi
continue_prompt

printf '\n2. Speaker\n'
printf 'A quiet music-box ringtone will play through %s.\n' "$SPK_DEV"
continue_prompt
if ffmpeg -nostdin -hide_banner -loglevel error \
  -i "$RINGTONE" -t 4 -filter:a volume=0.08 \
  -f s16le -acodec pcm_s16le -ac 1 -ar 48000 - |
  aplay -q -D "$SPK_DEV" -t raw -f S16_LE -c 1 -r 48000; then
  if confirm "Did you hear the ringtone clearly?"; then
    pass "Speaker"
  else
    fail "Speaker ringtone was not heard clearly"
  fi
else
  fail "Speaker command failed for $SPK_DEV"
fi

printf '\n3. Microphone\n'
printf 'After pressing Enter, say "test test test" into the microphone for 4 seconds.\n'
continue_prompt
if arecord -q -D "$MIC_DEV" -f S16_LE -r 48000 -c 1 -d 4 "$RECORDING"; then
  printf 'Playing the recording through the speaker.\n'
  if ffmpeg -nostdin -hide_banner -loglevel error \
    -i "$RECORDING" -filter:a volume=0.10 \
    -f s16le -acodec pcm_s16le -ac 1 -ar 48000 - |
    aplay -q -D "$SPK_DEV" -t raw -f S16_LE -c 1 -r 48000 &&
    confirm "Did you hear 'test test test' clearly?"; then
    pass "Microphone and recording playback"
  else
    fail "Microphone recording was not heard clearly"
  fi
else
  fail "Microphone command failed for $MIC_DEV"
fi

printf '\n4. Status LED\n'
printf 'The status LED on GPIO%s will light for 2 seconds.\n' "$LED_PIN"
continue_prompt
if GPIOZERO_PIN_FACTORY=lgpio LED_PIN="$LED_PIN" python3 - <<'PY'
import os
import time
from gpiozero import Device, LED

led = LED(int(os.environ["LED_PIN"]))
factory = Device.pin_factory
try:
    led.on()
    time.sleep(2)
finally:
    led.off()
    led.close()
    factory.close()
    time.sleep(0.1)
PY
then
  if confirm "Did the status LED light?"; then
    pass "Status LED"
  else
    fail "Status LED did not light"
  fi
else
  fail "Could not control status LED on GPIO$LED_PIN"
fi

printf '\n5. Record button\n'
printf 'After pressing Enter, press and release the physical button within 15 seconds.\n'
continue_prompt
if BUTTON_PIN="$BUTTON_PIN" python3 - <<'PY'
import os
import time
import _lgpio

pin = int(os.environ["BUTTON_PIN"])
chip = _lgpio._gpiochip_open(0)
if chip < 0 or _lgpio._gpio_claim_input(chip, 32, pin) != 0:  # pull-up
    raise SystemExit(1)
try:
    deadline = time.monotonic() + 15
    while _lgpio._gpio_read(chip, pin) != 0:
        if time.monotonic() >= deadline:
            raise SystemExit(1)
        time.sleep(0.02)
    deadline = time.monotonic() + 15
    while _lgpio._gpio_read(chip, pin) == 0:
        if time.monotonic() >= deadline:
            raise SystemExit(1)
        time.sleep(0.02)
finally:
    _lgpio._gpiochip_close(chip)
PY
then
  pass "Record button"
else
  fail "No complete button press detected on GPIO$BUTTON_PIN"
fi

printf '\n6. NFC reader and card\n'
printf 'After pressing Enter, hold one NFC card on the reader for up to 20 seconds.\n'
continue_prompt
if [ ! -x "$NFC_VENV/bin/python" ]; then
  fail "NFC Python environment is missing at $NFC_VENV"
elif NFC_RESET_PIN="$NFC_RESET_PIN" NFC_REQUEST_PIN="$NFC_REQUEST_PIN" \
  "$NFC_VENV/bin/python" - <<'PY'
import os

import board
import busio
from adafruit_pn532.i2c import PN532_I2C
from digitalio import DigitalInOut

reset = DigitalInOut(getattr(board, os.environ["NFC_RESET_PIN"]))
request = DigitalInOut(getattr(board, os.environ["NFC_REQUEST_PIN"]))
pn532 = PN532_I2C(busio.I2C(board.SCL, board.SDA), reset=reset, req=request)
pn532.SAM_configuration()
uid = pn532.read_passive_target(timeout=20)
raise SystemExit(0 if uid is not None else 1)
PY
then
  pass "PN532 reader and NFC card"
else
  fail "PN532 initialized but no card was read"
fi

printf '\n7. WhatsApp client\n'
if command -v wacli >/dev/null 2>&1; then
  wacli --version
  AUTH_STATUS=$(wacli --read-only --json auth status 2>/dev/null || true)
  if printf '%s\n' "$AUTH_STATUS" |
     grep -Eq '"authenticated"[[:space:]]*:[[:space:]]*true'; then
    pass "wacli is installed and authenticated"
  else
    fail "wacli is installed but not authenticated for this service user"
  fi
else
  fail "wacli is not installed"
fi

printf 'Hardware test complete.\n'
if [ -n "$FAILURES" ]; then
  printf 'Failed checks:%b\n' "$FAILURES"
  exit 1
fi
printf 'All checks passed.\n'
