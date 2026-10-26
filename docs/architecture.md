# Architecture

Button Box runs as separate systemd services with constrained users and
permissions. Runtime services use the `messagebox` account, while the web
onboarding service uses `messagebox-onboarding`. systemd limits filesystem,
device, and capability access. Wi-Fi management and reset operations retain the
root access they require.

## Runtime services

`messagebox.target` groups the enabled runtime services:

| Unit | Role |
| --- | --- |
| `messagebox-button.service` | Record, play, and send voice messages |
| `messagebox-poller.service` | Queue voice notes and ordinary video soundtracks from configured contacts |
| `messagebox-sync.service` | Keep one WhatsApp connection alive and the local store synchronized in wacli mode |
| `messagebox-nfc.service` | Read recipient cards and maintain NFC selection state |
| `messagebox-dash.service` | Serve the canonical household dashboard on the Wi-Fi interface |

One exact default recipient is stored with the private contact allow-list. A
recognized NFC selection has first priority for a standalone recording. With no
card and no claimed inbound message, the exact sender of the newest message
played within the last hour is preferred in both recording modes. An active
replay remains the newest played message even while the dashboard hides it in
the queue, in-flight, hold, or trash state. If that newest fresh route is invalid
or no longer allowed, recording fails closed; it never selects an older sender
or the default. The explicit default is used only when played history is absent
or expired. Unknown-card state and stale or missing NFC reader health also fail
closed before recent-sender routing. A held unknown card stays blocked after its
announcement is consumed; the transient block clears only after reader removal
grace or a recognized card presentation.

Continuous sync owns the writable WhatsApp store. Outbound voice notes,
played reactions, and recording presence use an immediate lock attempt so the
pinned client delegates them to the active sync connection immediately.
Commands that require exclusive store access, such as recipient refresh, pause
sync and retain their bounded lock wait.

In cloud mode, the same physical runtime uses an authenticated device client.
The service supplies scoped family membership, delivery permissions and bounded
commands. The device owns recording, playback, NFC, durable local queues and
command execution. Claiming requires both WhatsApp identity and a physical
button press. Provider integration, accounts and billing live in the separate
service. See [cloud runtime and acceptance](cloud-runtime.md).

In the default transport, the poller accepts wacli media types `audio` and `video`, converts the first
audio track to the same mono 48 kHz WAV queue format, and retains the exact
originating chat and sender in the existing private routing sidecar. Ordinary
videos are bounded by `MSGBOX_VIDEO_MAX_BYTES` and
`MSGBOX_VIDEO_MAX_DURATION_S`. A download failure is retried; missing audio,
invalid media, or a configured-limit rejection is recorded and skipped so later
messages can continue in order. Routine service output reports only generic
processing status and elapsed time; identifiers and routing details remain in
the restricted routing sidecars and structured event log.

The pinned wacli 0.17.1 client does not expose WhatsApp circular instant video
notes. Those notes use the separate WhatsApp `ptvMessage` field, while that
release only extracts `videoMessage`; no media metadata reaches `messages list`
or `media download`. Ordinary videos are supported. Circular-note playback
therefore remains unavailable until the pinned client gains that classification
and download support, and must not be claimed from poller tests alone.

A fresh valid NFC selection reserves the next button interaction for a new
outbound message before any unread incoming message is claimed. The incoming
queue stays intact until an ordinary press after the card message is queued.
The selection is consumed only when the approved recording enters the durable
outbox. Short presses, empty audio, cancelled or deleted recordings, recording
timeouts, and audio errors keep the choice with its original selection time.
The choice expires after `MSGBOX_NFC_SELECTION_TTL_S` (30 seconds by default),
even while the card remains on the reader. A new presentation replaces it.
A recording that starts within the window keeps its exact recipient.
Removing a card is silent. Re-presenting the same card suppresses its selection
announcement for 60 seconds after the last announcement, including reader gaps
while moving it. `MSGBOX_NFC_REANNOUNCE_COOLDOWN_S` overrides this positive interval.
A different card announces immediately; pairing feedback bypasses the cooldown.
Cloud reaction cards keep their separate per-message send deduplication.
The cooldown uses process-local monotonic time and does not renew on suppressed
reads. It does not change selection expiry, rearming, or quiet-time playback.
An NFC daemon restart resets this audio cooldown; held-card restart protection
still applies as described below. No new state is persisted.
In hold-to-record mode, a short release keeps the choice without playing the
queue or saving silent audio. A button-service crash before approval leaves
the choice available until expiry; there is no claim to recover. Invalid card
state blocks outbound routing rather than selecting a recent sender or default.

Each card recording saves its selection ID with the durable outbox job. Button
startup and input reconcile committed jobs before routing another recording.
Successful-send cleanup and all Cloud job deletion paths reconcile before
removing that receipt. A crash after the queue commit therefore cannot leave
an old card choice armed; a later presentation has a different ID and survives
reconciliation of an older job. Reconciliation failure keeps input blocked and
the job available for retry. The unknown-card block and selection are read
together under the same lock used by the NFC writer; blocked state never means
that the default or recent sender may be used.

An NFC service restart preserves unused choices, consumed markers and their
original timestamps. A held card is resumed as the same presentation until the
reader confirms removal for the normal grace interval. An unrelated contacts
revision refreshes the same person's mapping without renewing the choice. A
changed mapping blocks that presentation until removal and a new scan.
This check also applies to a held card after expiry or a successful send has
consumed the choice. Routing and reader refresh hold the contacts lock through
selection processing; the lock order is contacts, then NFC selection.

In standalone mode, after successful playback, the private WAV and routing sidecar move into
`queue/.played`. The dashboard shows up to 20 metadata records from the last 14
days, newest first. Playable media is further bounded to the 10 newest files and
128 MiB; a retained record whose media was pruned remains visible as
unavailable. Requeueing gives the retained item a fresh queue-order filename so
it follows messages already waiting, while its sidecar keeps the original
history identity and reply route. The sidecar is published before the WAV. A
locked history record names the actual replay file, so repeated or concurrent
requests cannot create duplicate playable entries and an interrupted
pre-publication attempt can be retried immediately. The same lock covers replay
scans, player claim/release/recovery, and dashboard hold/trash moves. Archiving
keeps the replay marker until the WAV move commits, so restart recovery cannot
lose the marker and publish a duplicate. History reads, audio access, and
requeue all enforce expiry without a polling service. Active replays remain part
of routing history even though the dashboard suppresses their duplicate Recently
played row. This covers voice notes and ordinary video soundtracks only; it does
not add circular video-note support.

## Setup services

| Unit | Role |
| --- | --- |
| `comitup.service` | Use [Comitup](https://github.com/davesteele/comitup) to manage Wi-Fi and the setup hotspot through NetworkManager |
| `comitup-web.service` | Serve the setup portal on the setup hotspot |
| `messagebox-onboarding-home.service` | Serve the portal after Wi-Fi setup |
| `messagebox-whatsapp-pairing.service` | Isolate WhatsApp pairing operations |
| `messagebox-onboarding-nfc.service` | Read tags and own private tag-first pairing state |
| `messagebox-onboarding-complete.path` | Watch for the content-free completion request |
| `messagebox-onboarding-complete.service` | Validate setup and perform the fixed runtime handoff |
| `messagebox-onboarding-voice.path` | Watch for the private fixed voice-proof request |
| `messagebox-onboarding-voice-gate.service` | Validate the request and default before activating hardware |
| `messagebox-onboarding-voice.target` | Run sync, polling, and the guided setup button without the normal runtime target |
| `messagebox-mode-reconcile.path` | Reconcile current process state after the onboarding marker changes or an interrupted transition |

Both setup portals use Gunicorn 23's `gthread` worker: one process, four request
threads and at most 64 accepted connections. Its selector waits for request
bytes before assigning a thread, so browsers' speculative empty connections
cannot occupy the application's request slots. The previous direct-to-browser
`sync` worker could wait on one empty socket and block unrelated pages and
state requests until that socket closed. Adding more sync workers only moved
that limit; it did not remove the blocking behavior.

The four request threads also let pages and state requests proceed while a
bounded Wi-Fi or pairing operation is waiting. Comitup proxy calls are
serialized inside the adapter; state and settings retain their file locks and
atomic writes. Ringtone preview uses the single process's nonblocking speaker
lock. Connectivity proof runs on state requests, not application construction,
so external network checks cannot prevent the HTML or static assets loading.
This is a private LAN/hotspot portal, not a public Internet endpoint.

## Boot-mode selection

The root-owned `/etc/messagebox-onboarding/enabled` marker is the only
persistent mode authority. At every manager start and daemon reload,
`messagebox-mode-generator` validates that marker without following links and
generates one ephemeral `multi-user.target` dependency: Comitup when the marker
contains the trusted setup value, or `messagebox.target` when the marker is
absent. An unsafe marker fails closed and selects neither mode. Neither
entrypoint is persistently enabled under `multi-user.target`.

Completion and intentional Wi-Fi reset share one transition lock. Each actor
records a transient reconciliation request before its first side effect, then
commits its mode with one atomic marker unlink or replacement. The reconciler
waits for the actor lock, reads the marker, stops the opposite entrypoint and
starts the selected entrypoint. It never deletes Wi-Fi profiles, rewrites
recipients, or repeats another transition side effect. The path watcher observes
only atomic marker creation and unlink, so unrelated onboarding configuration
writes cannot change process mode. Its `/run` request is consumed once
before convergence, so a command failure is bounded and a later actor or marker
event can request a fresh attempt.

During the voice proof, Comitup continues to own connectivity and the
setup portal while the shared sync and poller services run. The normal
`messagebox.target`, button, dashboard, and NFC services remain conflicted and
inactive.

Choosing the same default recipient is retry-safe. The private voice-proof
request is persisted before recipient setup publishes the `testing` state, so
an interrupted selection can be retried without choosing or routing to a
different recipient.

The NFC setup worker runs as `messagebox`, owns I2C and tone playback, and
offers only a group-restricted Unix socket to the isolated web portal. A read
tag UID is held privately for at most two minutes while the caregiver chooses
an opaque recipient token. Browser responses contain labels, kinds, default
state, counts, and progress only. Assignment reuses the contact store's atomic
one-tag/one-recipient transaction; tags are never written.

Skip or Done creates a fixed, content-free completion request. The root gate
validates the completed recipient state and default, enables the button, sync,
poller, canonical dashboard, and NFC reader under `messagebox.target`, removes
the setup gate, reloads the boot selector, stops
Comitup, restarts Avahi, and starts `messagebox.target`. Comitup publishes mDNS
records during setup; stopping it can remove the hostname's IPv4 record after
a collision with Avahi's own registration. Restarting Avahi after Comitup exits
republishes the local hostname for runtime. The restart must succeed before
runtime starts. A failed handoff restores the setup gate and requests Comitup
restart, preserving the completion request for retry.

`messagebox-wifi-reset.service` is a one-shot boot check for the physical Wi-Fi
reset gesture. The setup portal accesses Comitup through a restricted
D-Bus policy. NetworkManager and Avahi provide network management and local name
resolution.

WhatsApp pairing, recipient identities, message IDs, tag identifiers, and voice-proof
correlation remain behind a group-restricted Unix socket owned by the private
worker. The browser receives only opaque recipient tokens, labels, kinds, and
content-free progress booleans. Person labels are international phone numbers;
group labels use their WhatsApp names. Exact JIDs, message IDs, and tag UIDs never cross
the worker boundary.
Same-origin manual-number mutations accept a strict international number. The
private worker converts it to the exact direct-chat identity and persists it in
the same contact allow-list. A manual first choice starts the normal voice
proof, while later manual additions do not replace the default unless the user
explicitly chooses **Make default** in the completed recipient manager. The
authoritative contact-store change takes effect immediately for no-card routing;
a valid mapped NFC card still overrides it.

## Canonical dashboard and settings

Setup and runtime serve the same mobile-first HTML, CSS, and JavaScript bundle.
The bundle uses Home, Setup, Settings, Activity, and Advanced routes. Setup is a
resumable task list; successful task boundaries remain in the existing atomic
setup, recipient, voice-proof, and NFC stores. Runtime enables the same
dashboard at port 80, bound specifically to `wlan0`'s current IPv4 address.
When optional remote support is provisioned, runtime also opens the same port
on loopback for private Tailscale Serve HTTPS proxying.

Caregiver behavior is stored in `/var/lib/messagebox-settings/settings.json`.
The document is versioned, validated, revision checked, locked, and replaced
atomically. A last-good snapshot is retained. The shared
`messagebox-settings` group and setgid data directory allow the restricted
setup and runtime accounts to use the same document without widening access to
the private WhatsApp or recipient stores. The hardware loop snapshots settings
at a confirmed interaction boundary, so a browser save never interrupts active
playback, recording, or sending.

The dashboard deliberately has no login or physical confirmation. Anyone on
the household Wi-Fi, on the protected setup hotspot while setup is active, or
authorized to reach an optionally configured private tailnet origin can change
caregiver settings and play queued audio. The runtime listener binds to Wi-Fi
and, only when configured, loopback rather than every interface. The tailnet
path accepts one device-local MagicDNS hostname through the loopback proxy and
requires HTTPS same-origin mutations; Tailscale Funnel remains disabled. This
is an accepted household-device risk; do not publish port 80 through a router
or public tunnel. Browser queue operations use process-local opaque handles, so
queue filenames, raw NFC identifiers, and internal message identifiers do not
cross the browser boundary.

Wi-Fi changes cross a narrow root boundary through a private mode-0600 request
watched by `messagebox-wifi-change.path`. The root worker keeps the old
NetworkManager profile while it connects to the candidate and repeats the
association, address, route, DNS, and HTTPS proof. Only a proven candidate
replaces the old profile. A failed candidate restores and rechecks the old
profile; if that also fails, the worker reopens the protected setup hotspot.
The password is supplied to `nmcli --ask` over stdin and is never placed in a
process argument, response, status document, or log.


## Cloud reaction cards

Cloud boxes report `reaction_cards: 1`. Heartbeat can carry up to six reaction
card IDs and emojis. Cloud contacts use `box:reaction_*`; they are NFC actions,
not voice-message recipients. A tap sends the card ID for the last message
played within ten minutes. Repeated taps use a stable operation ID. A removed
card is rejected by Cloud and its contact is removed on the next heartbeat.
The setup screens require no change. Add and remove cards in dashboard People.

Boxes also report `reaction_sent_voice: 1`. The box plays the card cue as soon
as it reads the card, while the Cloud request runs; the cue confirms the read,
not the send. When Cloud accepts the reaction (`state: "sent"`), the box then
says what was sent in the current voice, for example "You just sent a heart!".
The line follows "Say the name when a card is tapped" (`card_name_prompt`); with
it off, the box plays only the card cue. Any other result plays `cue-oops.wav`
after the cue and never claims a send.

The line plays once per accepted send. A card that is lifted and placed again,
or that the reader loses for a moment, reads as a new tap. When that tap has the
same operation ID as a send Cloud already accepted (same card, same message,
within the ten-minute window), the box does not call Cloud again. Within
`MSGBOX_REACTION_SETTLE_S` (default 5 s) of the first send it stays silent;
later it plays only the card cue. A tap after a failed send asks Cloud again. The lines are bundled, so a tap never
downloads or synthesizes audio: `voice-sent-<key>.wav` in `sounds/voice` and in
every `sounds/voices/<pack>`, listed under `reaction_lines` in each manifest.
Keys are `heart`, `big-laugh`, `applause`, `heart-eyes`, `thumbs-up`, `fire`,
`celebration`, `kiss`, `surprise`, `tear`, `hooray` and `smile` (the dashboard's
common emojis, using Cloud's emoji words). Any other emoji says "You just sent a
reaction!". A missing or invalid file falls back to Jessica, then to the generic
line, then to the card cue. These optional lines never block a voice pack.
Signed releases that add them need `min_updater_version: "1.1.0"`.

When Cloud omits the new section, the Pi keeps the old heart-only contact and
request body. Malformed optional card reports do not invalidate heartbeat.
The self-hosted wacli path stays unchanged and never calls the Cloud reaction API.
Real NFC, speaker, and WhatsApp checks are required before physical acceptance.

## Message age phrase

Cloud boxes report `message_age_phrase: true`. When a message plays long after
it was sent, the box says when it was sent: arrival cue, then the optional
"A message for you!" intro, then the age phrase, then the message. It is always
on; there is no setting. `messagebox/message_age.py` holds the rule as one pure
function. The phrase plays only when the message is at least three hours old
AND was sent in an earlier day part or on an earlier day, in the box's
configured timezone. Day parts are morning 05:00-12:00, afternoon 12:00-17:00,
evening 17:00-21:00 and night 21:00-05:00. A night belongs to the day it
started on: Tuesday 23:00 and Wednesday 02:00 are both "Tuesday night", and on
Wednesday morning both are "from last night". Day counts compare those day
anchors as calendar dates, so DST changes do not shift them; the three hours
are real elapsed time. Keys: `this-morning`, `this-afternoon`, `this-evening`,
`last-night`, `yesterday`, `2-days-ago` to `6-days-ago`, `last-week` (7-13
days) and `a-while-ago` (14 days or more).

The send time is the audio payload's `message_created_at`, stored in the queue
sidecar as `cloud_created_at`. Cloud sets it when the message is sent, and a
quiet-hours hold, a held queue or an offline box does not change it. The box
says nothing when the send time is missing, invalid or in the future, when the
clock is not verified by a recent heartbeat (`cloud_runtime.verified_now()`),
and during Mute, Quiet now and quiet schedules. Lines are bundled
(`voice-age-<key>.wav`, `age_lines` in each manifest) and fall back to Jessica,
then to silence. wacli boxes never play the phrase.
