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
| `messagebox-sync.service` | Keep one WhatsApp connection alive and the local store synchronized |
| `messagebox-nfc.service` | Read recipient cards and maintain NFC selection state |
| `messagebox-dash.service` | Serve the canonical household dashboard on the Wi-Fi interface |

One exact default recipient is stored with the private contact allow-list. A
recognized NFC selection overrides that default; otherwise the default is used.
No default, an unknown card, or invalid routing state fails closed.

The poller accepts wacli media types `audio` and `video`, converts the first
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
queue stays intact for the following ordinary press. The selection is one-shot
and expires when that recording interaction completes or is abandoned. In
hold-to-record mode, releasing before the hold threshold cancels the selected
recording intent without playing the queue or saving a silent recording.

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
