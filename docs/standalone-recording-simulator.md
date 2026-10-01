# Standalone recording scratch adapter

`scripts/dev/simulate-standalone-recording.py` is an owner-only developer
adapter for one bounded WACLI standalone recording. It does not require or
create an incoming WhatsApp message, receiver receipt, source timestamp, queue
item, or recent-reply route. It runs the unchanged `simulate-inputs.py`
scheduler and the normal `button_send.handle_confirmed_press` application
path.

Validation is the default and reads only the private plan. Execution is a
separate explicit operation requiring `--execute`, `--send-generated`, a
private authorization document, an empty private namespace, an empty private
outbound scratch directory, and a hard timeout. The plan must exactly match
one built-in profile. Arbitrary timelines are refused.

The initial owner profile is `tap_review_short`: a normal standalone press,
real microphone capture, a stop press, review playback, an approval press,
durable WACLI outbox approval, conversion, and one transport invocation.
`tap_review_long` changes only the fixed capture timing. The optional
`hold_release_short` profile applies an authorization-bound recording-mode
override to the copied scratch settings through `SettingsStore.update`; it
does not edit production settings or a Cloud revision.

The private authorization binds all of the following:

- WACLI mode, fixed profile, and the current default recipient;
- exact raw and parsed hashes plus ownership and mode for the current contacts
  and settings files;
- the effective scratch settings hash, including any explicit hold-release
  override and its source revision;
- the current linked-account fingerprint;
- every normal guided prompt path and file hash;
- the normal `messagebox` runtime UID and GID, derived on the device rather
  than copied from a release or another installation;
- the exact service baseline and the narrow WACLI database/WAL/SHM allowance.

The authorization intentionally has no target message, sender, message ID,
freshness timestamp, media hash, receiver completion, or inbound receipt. The
adapter does not synthesize those fields and cannot be used as an inbound or
reply test.

Before import of the application modules, the adapter binds queue, contacts,
settings, runtime, and NFC constants into the new namespace. It copies the
currently authorized contacts and settings bytes and requires an empty queue.
Before the guard baseline it performs the normal played-history route scan and
requires the fallback result; this creates only the normal empty
`.played.lock`, which is then protected exactly for the whole run.
The production queue, played history, outbox, state and seen ledger, Cloud
state, contacts, settings, account binding, and NFC markers are snapshotted and
rechecked throughout the run. The button, NFC and poller services and the
dashboard must be exactly inactive; WACLI sync must be active. Any transient
guard failure latches the run failed.

Only the standard application subprocess shapes are admitted: exact beep
generation, 30 percent mixer application, authorized prompt or generated
recording playback, one normal capture, presence updates to the authorized
counterpart, one exact conversion, read-only account and service probes, and
one WACLI voice send to the authorized counterpart. Prompt and generated audio
must be bounded regular files without symlinks. A second capture, second send,
different recipient, different subprocess, foreign transport, or changed
default route is refused. A failed or uncertain send is retained in the
scratch outbox and must be reconciled; the adapter never retries it.

The completed receipt contains only plan and authorization hashes, the
profile, capture/send/receipt counts, and `counterpart_delivery: unverified`.
It contains no JID or message content. A completed source run proves the
application call graph and local durable accounting. It does not prove real
microphone quality, speaker audibility, button mechanics, provider delivery,
ordinary FIFO behavior, replay/history behavior, Cloud behavior, or another
contact.

A failed receipt records capture and transport invocation counts from the
guarded boundaries even when failure occurs after the transport call. It
records the separately observed durable sent-receipt count, or `null` if that
directory cannot be verified. These fields support reconciliation and never
claim counterpart delivery.

Future owner execution must be the sole scheduled device operation. A
separately reviewed, active external cgroup guardian must own the service
pause, the child process group, timeout cleanup, and exact service restoration.
The helper never changes services. The owner must create a fresh authorization
from the current device state immediately before the run, verify the existing
approved human counterpart and account, use brand-new empty scratch
directories, and preserve every artifact on failure. No owner run is approved
by this source file or its offline tests.

The focused tests use actual application scheduling, routing, `OutboxStore`,
and `ReceiptStore` code. Their microphone, player, encoder, mixer, service and
WACLI boundaries are mocked, so they are source-only tests and cannot be used
as hardware, acoustic, account, or delivery acceptance.
