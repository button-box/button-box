# Immediate Cloud settings

Cloud settings compare the expected revision to the local settings under the existing file lock. A new desired revision may skip failed or expired generations, but must advance. The full document is validated before atomic replacement. Replays of the same document do not increment the revision, and stale local writes still fail.

The poller checks for the button process’s applied marker promptly while a settings change is pending. It acknowledges success only when that marker matches the entire requested document. The Cloud dashboard waits for this receipt before showing success, with a short deadline and an explicit unconfirmed outcome when confirmation is unavailable.


## Voice packs

`voice_pack` defaults to `jessica`; absent or unknown values normalize to Jessica
without losing the settings revision. Supported ids are `jessica`, `pirate`,
`alien`, `dj`, `robot`, `french` and `charlie`. Jessica lives in `sounds/voice/`;
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
