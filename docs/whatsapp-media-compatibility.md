# WhatsApp media compatibility

Button Box pins wacli 0.17.1. The receive path supports the media types that
this exact version stores in its message database and returns from
`messages list --json --full`.

## Video compatibility

| WhatsApp message | wacli 0.17.1 result | Button Box result |
| --- | --- | --- |
| Ordinary video with audio | Stored as `MediaType: "video"` with downloadable video metadata | Audio is extracted to the normal mono 48 kHz WAV queue |
| Ordinary video without audio | Stored and downloadable as video | Skipped as `no_audio_track`; later messages continue |
| Circular instant video note | Not stored as downloadable media | Not available to Button Box with the pinned wacli version |

The circular-note limit is upstream of the Button Box poller. WhatsApp exposes
that message as a distinct `ptvMessage`; wacli 0.17.1 commit
`97e14efdf91a7c9de1b68845321eb6355943b5f5` inspects `videoMessage` but does not
inspect `ptvMessage`. The resulting row therefore has no media classification
or download metadata, so `media download` cannot retrieve it. Do not describe
circular-note receive or playback as supported until the pinned wacli version
exposes that message type and the Button Box compatibility tests are updated.

This boundary was checked with synthetic protobuf and encrypted-media fixtures
against the tagged wacli source. The check confirmed ordinary-video
classification and download with video encryption, and confirmed that the
same synthetic metadata under `ptvMessage` is not classified. No customer or
family media is required for this compatibility check.

## Local limits and failure behavior

`MSGBOX_MEDIA_MAX_BYTES` defaults to 100 MiB, matching wacli's download cap.
`MSGBOX_MEDIA_MAX_SECONDS` defaults to 1,800 seconds. Both settings must be
positive. Downloads that fail are retried on a later poll. Successfully
downloaded media that is over a limit, invalid, or missing an audio stream is
skipped once so subsequent messages keep moving.
