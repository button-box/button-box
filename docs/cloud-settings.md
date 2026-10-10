# Immediate Cloud settings

Cloud settings compare the expected revision to the local settings under the existing file lock. A new desired revision may skip failed or expired generations, but must advance. The full document is validated before atomic replacement. Replays of the same document do not increment the revision, and stale local writes still fail.

Unknown top-level settings keys are skipped in both local and Cloud settings; every known key retains its existing validation. Nested objects such as quiet hours still require their supported schema. The heartbeat capability `settings_unknown_keys: "ignore"` advertises this behavior.

The poller checks for the button process’s applied marker promptly while a settings change is pending. It acknowledges success only when that marker matches the normalized document of known settings. Received and terminal settings acknowledgements include `ignored_settings` when keys were skipped, including across restart and replay. This field contains at most 32 sorted names matching `^[a-z][a-z0-9_]{0,63}$`; other unknown keys are still skipped, but their names cannot be included in the wire field. Values are never reported. The Cloud dashboard waits for this receipt before showing success, with a short deadline and an explicit unconfirmed outcome when confirmation is unavailable.

For each released build, run `python3 scripts/dev/settings_contract.py > <build>.json` and hand the result to the Cloud repository's `tests/fixtures/box-builds/`. To describe an older checkout, run the current exporter with `--repo <path>`. The exporter inspects source without importing runtime modules. Unsupported source shapes fail clearly on stderr instead of emitting a guessed contract. Capabilities describe the checkout's bundled sound assets with NFC unavailable; installed hardware and assets can change these runtime-dependent values. Generate release fixtures from clean committed checkouts so the build id identifies the actual source.


## Quiet schedules and silence controls

Cloud settings accept `quiet_schedules` (null for legacy behavior, or an object
with `school_days` and `weekend` arrays). Each array has up to three `{start, end}`
periods in local `HH:MM`, without names. Empty arrays disable that schedule;
overlapping periods remain quiet throughout their union. Equal start/end is
all-day quiet and never generates a morning signal. `quiet_hours` remains
accepted and required for existing Cloud documents and local settings clients.
When `quiet_schedules` is null or absent, its enabled period is the first and
only period of both schedules. Explicit schedules take precedence in Cloud mode.
The dashboard initializes Weekend by copying School days and sends both arrays.

`weekend_days` is null for the timezone default or a list of unique weekday
integers, Monday=0 through Sunday=6 (an empty list means no weekend days).
Asia/Jerusalem defaults to Friday/Saturday; all other zones to Saturday/Sunday.
Daytime periods use that calendar day's schedule. Periods crossing midnight use
their ending day's schedule: Portugal's weekend nights are Friday/Saturday;
Israel's are Thursday/Friday. All decisions use the box's timezone, including DST.

`holiday_until` is null for off, or an inclusive local date `YYYY-MM-DD`.
Through that date, weekend replaces the school schedule; later mornings and
daytime automatically return to their configured schedules. Nights still use
the schedule of the morning on which they end.

`morning_ring` is boolean, default true. Only the latest-ending period whose end
is between 04:00 and 11:00 inclusive can signal in Cloud mode. Naps and the end of
Quiet now cannot signal. The normal arrival signal uses the configured ringtone,
volume and lamp rhythm when messages are waiting. Idle deferral is bounded to
30 minutes. A disabled morning ring, mute, Quiet now, another overlapping quiet
period, an empty queue or silent arrival setting consumes the morning window
without signaling. No deferred signal follows their end. The existing durable
`/var/lib/messagebox/state/morning-ring.json` marker prevents replay after restart;
marker failures suppress signaling.

The `quiet_now` command has `quiet_until`: a Unix timestamp in seconds, `"manual"`,
or null to end it. The `mute` command has `muted`: true or false. Missing or invalid
known values are rejected; extra optional payload fields are ignored. The Cloud
resolves durations and "until morning" into an absolute timestamp. Silence controls
persist atomically in `/var/lib/messagebox-cloud/silence.json`, independently of
settings revisions. Each control tracks its command sequence, so replay and
older retries cannot undo a newer command. Timed Quiet now expires by comparing
the deadline with the current time; no expiry process or late alert is needed.
Heartbeat `quiet_state` reports `{muted, quiet_until}`, using null after expiry.

Both commands stop active button-service audio. Mute blocks all later audio,
including cues, voice lines and previews, and arrival lamp blinking. A deliberate
button press while muted flashes the lamp once without playing or claiming a
message. Saved speaker volume remains intact. Quiet now suppresses unsolicited
sounds and arrival lamp blinking but permits deliberate playback after the
command's immediate stop. Interrupted incoming audio remains queued and receives
no played receipt. Audio is monitored at 20 ms intervals; actual acoustic latency
requires verification on the Pi. The guard targets its own child processes only.

### Receiver-first release

Before any device registration or heartbeat from this build, deploy and verify
Cloud acceptance of all new settings keys, capabilities and optional `quiet_state`.
Unknown optional fields must never reject registration or heartbeat. Capabilities
are `quiet_periods: 3`, `quiet_schedules`, `weekend_days`, `holiday_mode`,
`morning_ring`, `quiet_now` and `mute` (the latter six are true). Existing
capabilities and command kinds remain present. Older boxes receive only the
first school period through `quiet_hours`; multiple schedules and controls need
this build. No Pi installation precedes the receiver rollout and contract test.

Regenerate the fleet fixture from the final committed checkout with
`python3 scripts/dev/settings_contract.py`, then place it in Cloud's
`tests/fixtures/box-builds/`. A working-tree fixture is for integration development
only: its `build` still names the base commit and must be replaced after commit.

### wacli impact

Shared changes are in `settings.py`, `button_send.py`, `scripts/setup.sh` and
`scripts/provision.sh`. Settings retain new optional values for schema tolerance;
wacli keeps its legacy `quiet_hours`, B45 end signal and existing audio subprocess
behavior. It does not consult Cloud silence state or apply Cloud schedules,
holiday mode, morning-ring switch or mute/Quiet now commands. The new Cloud module
is included in both installers without starting an additional service.

`test_cloud_quiet.py` proves wacli ignores the new schedules and silence controls;
`test_button_settings_behavior.py` covers both transports' durable B45 signal,
legacy quiet boundaries, arrivals and button behavior. Existing routing, guided
reply, NFC and listened-notice tests run in both transport checks. Before a merge
to main, the physical wacli send/play/reply/card/quiet/listened matrix remains
required, along with Cloud box audio interruption, queue preservation and muted
lamp/press checks.

## Voice packs

`voice_pack` defaults to `jessica`; absent or unknown values normalize to Jessica
without losing the settings revision. Supported ids are `jessica`, `pirate`,
`alien`, `dj`, `robot`, `french`, `charlie` and `tata`. Jessica lives in `sounds/voice/`;
other packs live in `sounds/voices/<id>/`. Every spoken prompt resolves the current
pack at use, with a per-file Jessica fallback and one warning per missing line.

The `voice_packs` capability lists installed packs only after full WAV/manifest
validation, always including Jessica. The Cloud must offer only those ids and
must not send a non-Jessica setting to a box without that capability.

A `voice_preview` command carries `voice_pack` and queues that pack's
`voice-msg-start.wav` for the idle button audio owner, with the lamp off. Previews
retain the existing bounded lifetime and crash receipts. Applied voice changes
use the same settings-saved debounce and play the current pack's sample after
volume is applied. If voice and volume/ringtone change together, the voice sample
wins; changes to volume/ringtone alone keep their existing preview behavior.

A Cloud listened receipt retains its payload `voice_pack` through restart. The
button owner uses a cached name clip only when that value exactly matches the
current pack at playback; absent or mismatched values use the current pack's
`voice-listened.wav`. This also handles a voice change while a notice is queued.
Cloud generation and dashboard picker implementation belong to the Cloud repo.

## Recording modes

`talk_mode` is `tap` or `hold`; `review_before_send` is boolean. Fresh settings
use tap with review on. Legacy-only saved settings map `tap_review` to tap with
review, and `hold_release` to hold without review; loading preserves revision
and does not rewrite the saved file. The new pair is authoritative when both
interfaces are present. Partial new pairs and invalid known values are rejected.
The legacy `recording_mode` returned to old consumers is `tap_review` for tap
and `hold_release` for hold, the closest available old mode.

Capabilities advertise `talk_modes: ["tap", "hold"]` and
`review_before_send: true`. Cloud must use legacy `recording_mode` documents for
boxes without these capabilities and show that the other combinations require
an update. The fleet fixture must be regenerated after the coordinator commits
the completed release; an uncommitted export still carries the base build id.
