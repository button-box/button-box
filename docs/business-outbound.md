# Transitional Business adapter

This adapter is retained for existing prototype installations and rollback.
New cloud setup uses [the cloud device path](cloud-runtime.md).

The optional `MSGBOX_TRANSPORT=business` path sends an approved tap-review
outbox job to the existing WhatsApp Business bench API. It keeps the recorded
recipient bound to the audio and rechecks the current allow-list before send.
Only exact person contacts are supported. Group contacts fail closed. The
hold-release mode stages each completed recording into the same durable keyed
outbox; a crash during staging reuses its stable ID and bound recipient. The
default transport remains `wacli`.

The client posts mono Ogg Opus audio to `/api/send-voice` with `audio`, `box`,
and `to`, then accepts only a complete 200 response with the matching box and a
message ID. Configuration is documented in `config/env.example`. The bearer
token is read from a protected file rather than placed in the environment.
Device HTTP clients send a consistent `ButtonBox/0.1.0` user agent and reject
redirects to avoid forwarding credentials to another destination.
Tests inject a synthetic HTTP opener and never contact the service.

A compatible service must accept the durable outbox job ID as an
`Idempotency-Key` and prevent duplicate provider sends. A timeout, server error,
or invalid success response still leaves the durable job `uncertain` for operator review;
the sender never retries it automatically. An explicit 400/401/403/413 rejection
leaves the job `failed`. No success cue plays for either state. A crash while
`sending` is already quarantined by the existing startup recovery path.

Business mode now polls `/api/device-inbox`, downloads authenticated audio,
queues a WAV with its exact reply route, and acknowledges only after the WAV
and local deduplication ledger are durable. A lost acknowledgement replays the
same item without creating a second WAV. Removed recipients are skipped and
acknowledged; an unavailable contact store, media failure or queue failure
keeps the server item pending. The poller uses the existing systemd service;
the wacli sync service skips startup in Business mode. Recording presence
signals are also suppressed so the old account is not used by the business
path. Played reactions are not yet sent through the Cloud API.

Review the compatible service, device binding, protected token and contact
mapping before enabling delivery. Review installation and
rollback, then perform an attended send/reply/play acceptance test before
household use. New cloud installations use the separate procedure in
`cloud-runtime.md`.
