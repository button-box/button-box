# Cloud runtime boundary

`MSGBOX_TRANSPORT=cloud` selects the authenticated Cloud API v1 device path. The
default `wacli` path remains available. These are the only two connection modes.
The earlier `business` adapter was removed: `MSGBOX_TRANSPORT=business` now stops
the button and poller services with `unsupported message transport`, and the
wacli sync service does not start. Recordings tagged `business` stay on the box
and are never sent through either remaining mode.
Queued outbound recordings remain bound to the transport that approved them.
Changing transport preserves other-mode work without sending it; return to its
original transport to resume eligible pending jobs. For compatibility with older
standalone releases, missing transport metadata is treated as `wacli`. Older
cloud builds also created untagged hold-release WAVs: inspect and
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

Device registration includes the capability fields, `natural_registration_text: true`,
and `color` read from `/etc/messagebox-box-color`. The exact color values and safe
yellow default are documented in [dashboard identity](dashboard-identity.md#box-shell-color).
Manufacturing sets it before registration; heartbeats do not include color.

After home Wi-Fi setup, the handoff page asks the family to wait for the online
beep and scan the QR code on their setup card. The printed URL opens the Cloud's
WhatsApp connection flow without requiring phone support for `.local` names.
The small `.local` fallback opens the home setup portal, which can still create
a ten-minute WhatsApp connection link and QR code. A physical button press
confirms possession. The root
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
The HOME setup portal starts a setup watcher in its Gunicorn worker without
waiting for a browser request. It reconciles pending Wi-Fi connectivity proofs
in the background. In `WHATSAPP_PENDING` or `WHATSAPP_READY`, it POSTs
`/cloud-api/v1/device/setup-checkin` every five seconds using the existing
Bearer credential and stable `device_id`. Failed checks back off to 10, 20,
then at most 30 seconds; success resets the interval to five seconds. Other
phases make no check-in request, and removing the setup marker stops the watcher.
Check-ins update Cloud online freshness without creating a connection request.
An unexpired `pending_claim` is atomically written to the same private
`/var/lib/messagebox-cloud/claim.json` used by local registration, with
`physical_confirmed: false` and an empty `whatsapp_url`: no token is returned
to the Pi. Existing physical confirmation, cancellation intent, newer local
requests and local changes during the HTTP request are retained. The local
fallback can explicitly create its own connection link; its existing button
confirmation and cancellation paths still apply.
Once owned, the watcher requests the same guarded root completion as the
operator-only `POST /onboarding/complete`.
Normal claim polling then stops. If the setup marker remains after 60 seconds,
it retries the guarded request at most twice, 60 seconds apart, then stops until
portal restart. Status and filesystem failures keep setup pending without
logging claim contents. The root gate still independently verifies setup mode
and the live Cloud claim before starting runtime. Leaving the page for WhatsApp
does not delay activation. The claimed page says **Your box is ready. Go back to
WhatsApp.** and stays there; it does not POST completion or redirect away.

The registration press plays the runtime press cue before confirming possession.
Missing, expired, invalid or failed confirmations also play the failure cue;
already confirmed/claimed presses and pending cancellation do not. Setup plays
bundled `cue-press.wav` and `cue-oops.wav` through the detected speaker; audio
failures remain content-free and never prevent claim confirmation.

The first successful setup check-in queues `cue-connected.wav`. The shared,
group-restricted `/var/lib/messagebox-cloud/setup-online.json` keeps one receipt
per setup marker. The idle setup button listener marks it played before bounded
playback, so polling and restarts cannot replay it in the same session. After
the connected cue finishes, `voice-online.wav` ("I'm here!") plays only once in
the box's lifetime, with a durable receipt in the private runtime sound state.
The runtime also plays connected after a fresh heartbeat on boot or recovery.
Quiet hours defer these unsolicited welcomes.

The existing snapshot's boot ID and monotonic verification time provide the
reachability signal. After three minutes without verification, the idle button
owner plays `cue-offline.wav` once for that outage. A receipt prevents repeat on
service restart; a fresh snapshot resets the outage. There are no added network
probes. Unclaimed boxes, snapshots from another boot and wacli mode do not imply
offline. Quiet hours, recording, prompts and button presses suppress it.

After the guarded setup completion succeeds, it queues a group-readable
`setup-complete-sound.json` request. The runtime plays `cue-all_set.wav` then
`voice-all-set.wav` once per box. Program updates do not themselves create this
request. All bundled assets are in the release manifest and bounded updater;
private sound receipts and requests are excluded.

When the physical-button owner applies changed Cloud settings successfully,
the poller queues a `settings_saved` audio request before acknowledging that
revision as applied. The idle button owner plays `cue-card_saved.wav` after
the new master volume takes effect. Consecutive saves within three seconds
share one confirmation, played after that debounce window. Ringtone changes
use this confirmation; the dashboard's explicit ringtone preview stays separate.
Recording, guided sessions, message playback and held presses defer the cue
for at most 30 seconds from application, using a current-boot monotonic deadline.
Playback runs synchronously on the same main-loop audio owner, so it cannot
overlap a message. A press interrupts the cue and leaves it pending within its
original deadline. Quiet hours discard confirmations, including saves applied
during quiet hours; they do not sound later. First settings adoption after button
service startup, unchanged values and box-originated revisions echoed by Cloud
stay silent. Private apply markers and command intents retain the boot, origin
and changed-values evidence; request receipts prevent replay after restart.
No Cloud API change, new setting ID or new sound asset is required.

B20 needs a real-box check for the captive-page handoff, online beep audibility
and one-shot behavior, QR-to-WhatsApp-to-button completion, connection errors,
Wi-Fi retries, NTP catch-up, reboot, and the `.local` fallback. Offline tests do
not establish speaker, GPIO, phone, Cloud compatibility or WhatsApp acceptance.

In runtime cloud mode, the local page opens the hosted dashboard at
`https://button.box/dashboard`. Family, WhatsApp and settings management belongs
there. The local page keeps a collapsed Wi-Fi recovery form and support link;
it does not show the standalone management dashboard. Existing local device
settings and message APIs retain their contracts for device controls.

Cloud setup state never queries the retained standalone WhatsApp, recipient or
NFC store, and those management routes reject requests in both setup and runtime.
After durable home internet proof, the local setup root opens the Cloud claiming
page. That page keeps an explicit Wi-Fi recovery action. Claim completion activates
runtime without a browser; the runtime local page opens the hosted dashboard.
Standalone setup and management remain available when the selected transport is
wacli. A dashboard link does not prove entitlement,
message delivery or physical acceptance; those still require independent checks.

The Cloud poller also opens an outbound authenticated WSS connection at the
configured API origin's `/cloud-api/v1/device/events` path. It sends the existing
device credential in the Authorization header, never in the URL; redirects are
rejected. The socket carries only `{"type":"work"}` hints. Commands, audio,
account scope and authorization still come from the authenticated HTTPS API.
Only the existing poller thread executes work and writes runtime state. Repeated
hints coalesce, and a new connection immediately checks the inbox to recover
notifications missed while offline. A hint received during an HTTP request is
preserved for the next check. The existing receipts prevent duplicate effects.

An idle connected socket uses a 30-second inbox backup poll. The same runtime
thread checks local ACKs, preview/NFC/settings completion and outbound recovery every
2–3 seconds, with fresh authorization and bounded failure backoff. An
unavailable socket retains the existing 2–3-second successful polling cadence;
HTTP failures retain bounded backoff. Heartbeats stay on a separate 30-second
monotonic schedule. The listener sends text `ping` every 20 seconds and expects
text `pong` within ten seconds, so silent loss returns to polling. Socket retry
backoff grows from 2 to 60 seconds with jitter; unsupported endpoints, rejected
credentials and missing dependencies wait five minutes before retrying. The
Cloud service may close a connection to require fresh authentication. Shutdown
interrupts the listener and closes the socket; no other service owns this stream.

Set `MSGBOX_CLOUD_EVENTS=0` in `/etc/messagebox/env` to use polling only, then
restart the poller during an authorized maintenance window. Fresh setup installs
the distro `python3-websocket` package. Before a bounded update on an existing
box, install it with `sudo apt-get install python3-websocket`; bounded updates do
not install OS dependencies. If absent, HTTP polling continues. Repository checks
pin `websocket-client==1.9.2` for reproducible tests. Socket availability and a
source test pass do not establish live message delivery or hardware acceptance.

The poller accepts family audio only from a fresh authenticated heartbeat and
the exact inbox message. It verifies the media hash before publishing a WAV,
then acknowledges the durable queue entry. The button checks the same fresh
authorization before playback or send. A heartbeat is valid for at most 90
monotonic seconds on the same Linux boot, with a checked wall/server clock.
The service authorizes inbound delivery and outbound sending separately.
The device enforces those permissions and their expiry; accounts, billing,
provider credentials and service policy remain server-side. Message expiry, deletion tombstones, active membership,
and queue hold remain playback gates.
An inbox audio operation encountered during a temporary queue hold remains
pending without a rejection or media download. Later hold/resume commands still
apply in sequence; a subsequent poll fetches the audio with current authorization.
Hold effects and their sequence are saved together, so an interrupted command can
resume safely and an older retry cannot override a newer hold/resume command.

Cloud WAV/history copies and successfully uploaded outbound sources are
deleted only after a fresh authenticated server timestamp reaches their
authoritative expiry, or after a scoped cloud deletion. A lost upload response
leaves the local recording in `uncertain`; the poller uses a status lookup by
its original idempotency key to recover the message ID and expiry. Before the
first upload, the job durably retains `audio.ogg` and a `cloud_upload` descriptor
with its hash, key, original account, recipient identity and duration. A keyed
not-found response can return an interrupted job to `pending` only when that
exact payload remains valid and fresh authorization still matches. The sender
checks status again and reuses those bytes and routing fields, so a delayed
original request and retry share the server's unique upload key. A shared job
lock serializes local sending and recovery. Automatic retries end at the
retention deadline fixed from the first upload's verified clock, before server
key tombstones can be collected; this does not authorize local deletion.
Older uncertain jobs without the original encoding, changed payloads, expired
retry deadlines, and unavailable status remain private for operator recovery.
The status lookup accepts `uncertain` and `held_for_review`, retaining the local
source until authoritative expiry or deletion. Command effects and ACKs retain per-operation receipts so retries cannot reselect a different NFC
card or replay a ringtone after a crash.

Cloud send confirmations and ringtone previews use short-lived durable audio
requests consumed by the existing button service. The poller never starts preview
playback. The button owner waits until recording, guided interaction and playback
are idle, checks fresh account scope, and yields to a physical press while stopping
the player before starting recording. Claims are committed before playback; terminal
receipts suppress repeated commands and requests across process restarts. A preview
is `received` while waiting and `applied` only after successful playback. Expired,
failed, interrupted or crash-uncertain previews are never replayed. Its start window
is at most 30 seconds and playback retains the selected speaker and WAV-duration
plus five-second timeout.

Cloud sends in `queued`, `waiting_for_reply`, `accepted`, `delivered` or `read`
can produce one success cue: the Cloud has accepted the message for sending,
even if it is waiting for the recipient's chat window to open. Review-held,
uncertain, `failed`, `rejected`, `delivery_uncertain`, `canceled` and `expired`
statuses stay silent. The status API has no acceptance timestamp, so recovered
acceptance requires a send or status observation no more than 30 seconds old.
Acceptance discovered after a longer gap stays silent. Each cue expires 30
seconds after its first verified acceptance observation; later polls and waiting
for a busy button owner never extend that deadline. Deleted or expired messages
and messages outside the current account scope cannot produce a cue.
Standalone wacli send confirmation behavior is unchanged.

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
To reopen the claim portal on a configured box that already has
`MSGBOX_TRANSPORT=cloud`, run `sudo messageboxctl enter-cloud-claim`. It validates
the configured marker, private transport config and cloud directory, then enters
setup mode and starts the local claim portal. It does not change the connection
mode and refuses any other `MSGBOX_TRANSPORT` value. It preserves Wi-Fi profiles,
household recordings/state, device identity, credentials and the release marker.
An activation failure restores the runtime mode; check its reported error and
service state before retrying. Repeating the command in cloud setup mode
resumes the portal without resetting Wi-Fi or household data.
Verify that the setup portal, physical button process, runtime poller and
completion gate can access the same private claim and device identity files.
Acceptance requires a real claim QR/link and button confirmation, authorized
family send → download → playback, blocked removed member, owner settings
revision and NFC flows, deletion/expiry retention, restart/ACK recovery, and
the existing wacli path remaining intact. Automated tests and service health
alone do not establish physical or household acceptance.

## Cloud listening announcements (B26)

Registration and heartbeat advertise `listened_announcements: true`. A `listened`
command carries the outbound message ID, listener identity and first name, with
optional `text_hash`, authenticated `media_url`, `sha256` and `content_type`.
Cloud can issue the notice for WhatsApp `played`, or `read` when playback status
is unavailable; a read status does not prove the person heard the recording.

The poller verifies SHA-256 and PCM WAV format (16-bit mono, 48 kHz), then caches
the voice under `/var/lib/messagebox-cloud/listened-clips`. Cache keys bind
the household, listener and text hash. Changed text invalidates the older voice;
verified roster removal (including opt-out), household change and account deletion
remove ineligible cached voices. The cache holds at most 32 clips of 1 MiB each, with
0700 directory and 0600 files. Interrupted downloads are removed on housekeeping.
No name audio is generated on the Pi.

Missing clips, failed downloads, invalid hashes or invalid WAVs enqueue an empty
clip and use the bundled `voice-listened.wav` fallback. Evicted clips also fall
back at playback. The notice uses the existing private `listened-receipts` store,
separate from incoming family audio. Queue and operation deduplication are durable
before `received`; the poller sends `applied` only after the button owner records
successful announcement playback. Playback failures retain the receipt for retry.
No `played` ACK is used for a listening notice.

Only the main button loop plays `cue-listened.wav`, followed by the name voice
or fallback. Recording, guided interaction, a held button and synchronous family
playback defer unsolicited notices. Quiet hours keep receipts pending under the
existing rules. Before starting a Cloud notice, fresh local authorization must
match its household and listener, service and queue hold. Expired or deleted
messages and removed listeners cannot start an announcement; stale authorization
waits for the existing heartbeat. Result ACKs survive restart.

No new bundled assets, runtime modules, environment settings or updater targets
are required: the existing manifest includes the changed Python files and the
existing listened cue and fallback. Generated private clips are state, outside
the program release manifest.

Real-box acceptance must verify a Cloud read/play event produces the correct
name once, cue/voice order and volume, missing-clip fallback, quiet-hours deferral,
recording/guided/family-playback deferral, restart/ACK recovery and expiry or
membership removal while a receipt waits.

## Connected Boxes

The device advertises `box_link: true`. Heartbeats may include up to 50 ready
`connected_boxes` entries (`id`, `box_name`). Invalid entries are dropped;
an absent or malformed list means no connected boxes. Existing `people` entries
retain their WhatsApp routing. Connected box contacts use `kind: box` and
`box:{link_id}` keys. Removal deletes that contact and its family card mappings,
and clears the default when no authorized default remains. Setup recipient lists
show "{box name} (connected box)" without an additional setup step.

A connected box family card enrolls through the existing `nfc_enroll` command.
Inventory uses the link ID and opaque card reference. Both recording modes send
the link ID as `recipient_id`; durable retries retain the exact original upload
bytes, account scope and route. A removed link cannot authorize a retry or a
queued inbound message. Presenting its old family card uses the existing
unrecognized card feedback.

Inbound `sender_kind: box` audio uses the same playback path and ordinary cues,
with no spoken box name. Its local chat route is `box:{sender_id}`, so both invited
replies and recent-message replies target the sending box. The current heartbeat
list authorizes playback and reply sending.

A box `listened` notice contains `listener_link_id` and `listener_name`, with
optional voice clip fields. Its expiry comes from Cloud (30 minutes after
playback). Quiet hours or `arrival_signal: silent` expire the notice immediately;
`lamp_only` pulses the lamp without either cue or voice. These gates are rechecked
before playback. Box notices never defer through quiet hours; identity notices
keep the existing quiet-hours deferral. A missing or invalid clip, or a box name
longer than 24 characters, uses the bundled generic listened voice. Removed links
lose cached clips and cannot authorize pending announcements.

Real-box acceptance still requires recording, delivery, playback, reply routing,
heard feedback, dashboard states and unlink/card rejection on both boxes.

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

The regular heartbeat reports saved NFC card counts per authorized recipient, scoped to the last verified account and contact-store revision. It never sends card UIDs or local contact labels. Existing cards and subsequent unpairing appear in the authenticated Cloud dashboard after the next heartbeat.

## Card prompt clip delivery

A capable Pi reports `card_name_prompt: true` and accepts `card_prompt` commands
through the existing inbox and authenticated listened-media download path. The
payload uses `listener_identity_id` for a person or `listener_link_id` for a
connected box, plus `voice_pack`, `text_hash`, `media_url`, `sha256` and
`content_type: "audio/wav"`. Cloud also supplies `listener_first_name` or
`listener_name`, as for listened clips. There is no message ID or listened
receipt: a validated preload is ACKed `applied` without playing it.

The existing clip downloader enforces its byte limit, SHA-256 and full 48 kHz,
16-bit mono PCM validation. Card clips live in the private `card-prompts/`
directory next to the runtime state, separate from `listened-clips/`, keyed by
account scope, identity/link, pack and text hash. A private atomic sidecar binds
the clip to the current name and audio hash. Changed text replaces that pack's
clip; changed names, stale authorization, corrupt/missing files, revoked people
or links, and account changes cannot select an old name clip. Cache pruning uses
the existing bounded clip policy, independently for each clip kind.

A card tap only reads local storage. Missing clips and offline boxes use the
current pack's `voice-card-prompt.wav`; Robot and DJ always use that generic
asset. wacli uses only bundled generic invitations and never accesses Cloud.
The boolean `card_name_prompt` defaults to true, including older saved settings;
false restores cue-only tap feedback. Unknown top-level settings remain ignored
and reported, while unsupported command kinds remain rejected.
