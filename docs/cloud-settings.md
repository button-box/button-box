# Immediate Cloud settings

Cloud settings compare the expected revision to the local settings under the existing file lock. A new desired revision may skip failed or expired generations, but must advance. The full document is validated before atomic replacement. Replays of the same document do not increment the revision, and stale local writes still fail.

Unknown top-level settings keys are skipped in both local and Cloud settings; every known key retains its existing validation. Nested objects such as quiet hours still require their supported schema. The heartbeat capability `settings_unknown_keys: "ignore"` advertises this behavior.

The poller checks for the button process’s applied marker promptly while a settings change is pending. It acknowledges success only when that marker matches the normalized document of known settings. Received and terminal settings acknowledgements include `ignored_settings` when keys were skipped, including across restart and replay. This field contains at most 32 sorted names matching `^[a-z][a-z0-9_]{0,63}$`; other unknown keys are still skipped, but their names cannot be included in the wire field. Values are never reported. The Cloud dashboard waits for this receipt before showing success, with a short deadline and an explicit unconfirmed outcome when confirmation is unavailable.

For each released build, run `python3 scripts/dev/settings_contract.py > <build>.json` and hand the result to the Cloud repository's `tests/fixtures/box-builds/`. To describe an older checkout, run the current exporter with `--repo <path>`. The exporter inspects source without importing runtime modules. Unsupported source shapes fail clearly on stderr instead of emitting a guessed contract. Capabilities describe the checkout's bundled sound assets with NFC unavailable; installed hardware and assets can change these runtime-dependent values. Generate release fixtures from clean committed checkouts so the build id identifies the actual source.


## Quiet hours

Cloud and wacli boxes suppress arrival sound and lamp during quiet hours;
deliberate playback still works. At the local end time in the settings timezone,
waiting messages trigger one normal arrival signal, using `arrival_signal`, the
chosen ringtone, master volume and lamp rhythm. An empty queue stays silent.
Recording, guided flows, playback and a held button defer the signal until idle,
for at most 30 minutes after the end. A press stops the signal as usual.
Disabled quiet hours and equal start/end times (all-day quiet) have no end signal.

The private `/var/lib/messagebox/state/morning-ring.json` marker is written
atomically before signaling, keyed by timezone, quiet-hours start and local end
date/time. Restarting within the grace period cannot repeat a consumed signal.
An empty queue or silent arrival setting also consumes that window. A crash
between the durable marker and playback can lose that signal; it is never retried
after ambiguous playback. Marker failures suppress the morning signal. Settings
keys and Cloud command payloads are unchanged.

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
