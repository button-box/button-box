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
The setup portal on home Wi-Fi provides a ten-minute local WhatsApp claim
link and QR code. A physical button press confirms possession. The root
completion gate rechecks the claimed state with the Cloud API before starting
runtime services; it does not require legacy wacli pairing or recipients.
If the box clock is still catching up after joining Wi-Fi, the portal asks the
owner to try again in a moment. Links outside the normal expiry bound are not
saved or used for button confirmation. Retry after time synchronization.
While a connection is pending, Cancel connection invalidates that exact cloud
claim before clearing local claim mode. It also works after the button press
while WhatsApp confirmation is pending. If cancellation cannot be confirmed,
the portal hides the claim link and offers Retry cancellation; restart and
repeated button presses keep the pending cancellation for recovery. A completed
connection stays connected, and cancellation reports that outcome separately.
This control does not unlink an owned box or change its settings or account.

In runtime cloud mode, the local page opens the hosted dashboard at
`https://button.box/dashboard`. Family, WhatsApp and settings management belongs
there. The local page keeps a collapsed Wi-Fi recovery form and support link;
it does not show the standalone management dashboard. Existing local device
settings and message APIs retain their contracts for device controls.

Cloud setup state never queries the retained standalone WhatsApp, recipient or
NFC store, and those management routes reject requests in both setup and runtime.
After durable home internet proof, the local setup root opens the Cloud claiming
page. That page keeps an explicit Wi-Fi recovery action. Finishing the confirmed
claim opens the hosted dashboard. Standalone setup and management remain available
when the selected transport is wacli. A dashboard link does not prove entitlement,
message delivery or physical acceptance; those still require independent checks.

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
