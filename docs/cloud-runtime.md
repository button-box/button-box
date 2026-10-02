# Cloud runtime boundary

`MSGBOX_TRANSPORT=cloud` selects the authenticated Cloud API v1 device path. The
default `wacli` path remains available. The transitional `business` adapter is
retained for existing prototype installations and rollback.
Queued outbound recordings remain bound to the transport that approved them.
Changing transport preserves other-mode work without sending it; return to its
original transport to resume eligible pending jobs. For compatibility with older
standalone releases, missing transport metadata is treated as `wacli`. Older
business/cloud builds also created untagged hold-release WAVs: inspect and
preserve these before changing modes, and do not resume them until their
original transport is established. Never infer it from the current mode. Cloud
audio remains subject to cloud authorization and cannot be played through
another connection mode.
Cloud recordings also retain the opaque `account_scope` from the last verified
heartbeat at the start of their recording interaction. The approval and any
hold-release sidecar keep that same scope if ownership changes during recording
or review. Offline recording can use this last known binding; sending requires
a fresh heartbeat with an identical scope. The sender checks it before audio
conversion and includes it in the upload for server-side validation. A transfer
that keeps a relative and device credential does not authorize old recordings
under the new account.

Pending Cloud jobs without a scope, including jobs created by older releases,
remain preserved and are never assigned the current account automatically.
Mismatched and unbound work does not block eligible current-account work. The
read-only outcome lookup can still resolve a previously attempted upload; it
never makes an unbound or mismatched recording eligible for reupload.

This contract needs a coordinated Cloud and Pi rollout. Apply the compatible
Cloud API first: until the Pi update, uploads missing a scope are rejected and
remain local. After the Pi update, a successful new heartbeat is required before
recording or sending when the cached snapshot lacks the scope. An older Cloud
heartbeat without the field is rejected. Neither release migrates old pending
recordings to the new binding; preserve them for private recovery.

The setup portal on home Wi-Fi provides a ten-minute local WhatsApp claim
link and QR code. A physical button press confirms possession. The root
completion gate rechecks the claimed state with the Cloud API before starting
runtime services; it does not require legacy wacli pairing or recipients.

The poller accepts family audio only from a fresh authenticated heartbeat and
the exact inbox message. It verifies the media hash before publishing a WAV,
then acknowledges the durable queue entry. The button checks the same fresh
authorization before playback or send. A heartbeat is valid for at most 90
monotonic seconds on the same Linux boot, with a checked wall/server clock.
The service authorizes inbound delivery and outbound sending separately.
The device enforces those permissions and their expiry; accounts, billing,
provider credentials and service policy remain server-side. Message expiry, deletion tombstones, active membership,
and queue hold remain playback gates.

Cloud WAV/history copies and successfully uploaded outbound sources are
deleted only after a fresh authenticated server timestamp reaches their
authoritative expiry, or after a scoped cloud deletion. A lost upload response
leaves the local recording in `uncertain`; the poller uses a read-only status
lookup by its original idempotency key to recover the message ID and expiry.
It never reuploads an uncertain job. If the server has no record or cannot be
reached, the source remains private for operator recovery. Command effects and
ACKs retain per-operation receipts so retries cannot reselect a different NFC
card or replay a ringtone after a crash.

Pending NFC operations retain their original request IDs across restart. The
poller reports pairing as applied only with a matching committed success receipt.
If the original enrollment has ended or been replaced without that receipt, it
records `rejected` with `nfc_enrollment_ended` and clears the pending entry;
matching pending or claimed requests keep waiting. A durable terminal receipt is
replayed after a crash before changing any leftover pending entry. Cancellation
and expiry do not change saved card routes or queued recordings.

Before an attended installation, verify the bounded release manifest and
rollback, preserve current household files, and run `make check`. On an
existing box, the bounded updater does not rerun full setup: first create
`/var/lib/messagebox-cloud` as `messagebox:messagebox-settings` with mode
`2770` using the reviewed tmpfiles directive, after the baseline backup.
After applying the bounded release on an already configured business box,
run `sudo messageboxctl enter-cloud-claim`. It validates the configured
marker, private transport config and cloud directory, then changes only
`MSGBOX_TRANSPORT=business` to `cloud`, enters setup mode and starts the
local claim portal. It preserves Wi-Fi profiles, household recordings/state,
device identity, credentials and the release marker. An activation failure
restores the prior transport and runtime mode; check its reported error and
service state before retrying. Repeating the command in cloud setup mode
resumes the portal without resetting Wi-Fi or household data.
Verify that the setup portal, physical button process, runtime poller and
completion gate can access the same private claim and device identity files.
Acceptance requires a real claim QR/link and button confirmation, authorized
family send → download → playback, blocked removed member, owner settings
revision and NFC flows, deletion/expiry retention, restart/ACK recovery, and
the existing wacli path remaining intact. Automated tests and service health
alone do not establish physical or household acceptance.

## Combined-release acceptance

Run `make check` on the exact candidate revision and retain its installed-file
manifest. The [standard acceptance matrix](box-acceptance.md) still applies to
standalone mode. Validate cloud mode separately on an explicitly identified
device and service revision; a successful standalone run does not prove cloud
behaviour, and synthetic cloud tests do not prove physical operation.

| Check | Required observation |
| --- | --- |
| Existing standalone installation | Update preserves its connection mode, recipients, NFC mappings, recordings and queues; send, receive and playback still work. |
| Local cloud setup | Wi-Fi portal remains responsive; claim expiry/retry works; WhatsApp identity and a physical button press are both required. |
| Cloud messaging | Both recording modes send only to the intended person; replies queue once and play on the physical device. |
| Family and NFC changes | Removed members cannot send or receive through a stale local route; NFC enroll, cancel and unpair have the intended effect. |
| Settings | A permitted settings change reaches the local store and hardware; stale revisions are rejected. |
| Expiry and deletion | Expired or deleted audio cannot play or reappear after reconnection; authorized cleanup removes retained copies. |
| Failure and restart | Lost responses, duplicate inbox work and acknowledgment retries do not duplicate sends or playback; uncertain uploads remain recoverable. |
| Connection-mode isolation | Pending recordings stay bound to the connection mode that approved them. Changing mode does not send them through another account or make retained cloud audio playable without cloud authorization; foreign-mode work remains preserved. |
| Update and rollback | Setup and runtime updates preserve private state and recover the recorded service state; the previous compatible release can be restored. |
| Cold reboot | The same unit restores its selected mode, identity, permissions and intended message route. |

Record each result as Passed, Failed, Not run or Inconclusive with its evidence
layer. Keep device identifiers, credentials, contact details and recordings in
private acceptance records. Do not publish a final release from automated
checks alone. Recheck the merged main revision before creating the release tag.
