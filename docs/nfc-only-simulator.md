# WACLI NFC-only simulator

`scripts/dev/simulate-nfc-only.py` is a private, owner-operated adapter for four
fixed NFC component profiles. Validation is the default. `--execute` starts one
fresh spawned child and owns the complete bounded phase: it verifies production,
copies the current authorized contacts and settings into a new empty namespace,
generates the normal feedback beeps, and delegates the tag timeline to the
unchanged `simulate-inputs.execute(..., send=False)` path.

This is deliberately independent of WhatsApp receiving. Its authorization and
receipts contain no message target, media, sender, seen-ledger, or source-message
timestamp. It never uses, changes, claims, plays, moves, or prunes the production
queue or history.

The adapter supports only the installed WACLI mode. It creates no button edge,
capture, outbox job, or transport send. Audio still occurs: the real standard
driver generates feedback beeps, applies the already configured volume of 30,
and plays the exact current known-contact or unknown-token cue reached by the
plan. The child permits only the exact standard `ffmpeg`, `amixer`, and `aplay`
commands plus fixed read-only account and service probes. Microphone commands,
WACLI sends, service changes, and other subprocesses fail closed.

The owner supplies a private plan, authorization, empty namespace directory,
and separate empty outbound scratch directory. Input files and directories must
be regular/private, owned by the runtime user, and separate from production.
The authorization binds:

- exact raw-file and parsed-document hashes plus mode, UID, and GID for current
  production contacts and settings;
- the current WACLI account hash, the fixed inactive button/NFC/poller/dashboard
  and active sync service baseline, and the permitted WACLI DB/WAL/SHM churn;
- an existing approved recipient subset and exact current UID mappings;
- every reachable current contact or unknown-token cue by private path, hash,
  WAV duration, channels, sample rate, width, frame count, measured peak, and
  clipped-sample count.

An empty or absent cue must be explicitly authorized as missing. Normal
production beep/failure behavior remains active. The helper does not replace a
cue, alter contacts, suppress playback, or claim that an unclipped cue was heard.
Mixer application and acoustic output remain separate owner observations even
when the process exits successfully.

The supported profiles are fixed:

| Profile | Real elapsed events | Required actual trace |
| --- | --- | --- |
| `known_repeat` | present 1s, repeat 2s, remove 3s, present 5s, remove 6s, finish 8s | both presentations return selected/recognized, the real one-contact recognized or multi-contact refreshed result occurs, and both absence-grace removals occur |
| `unknown_hold` | unknown 1s, repeat 2s, remove 5s, finish 7s | unknown result creates the block, the marker remains after announcement consumption while held, and clears only after removal grace |
| `stale_selection` | present 1s, remove 2s, finish 36s | removal occurs and the stored selection is observed outside the normal 30-second TTL with no artificial clock change |
| `assigned_alternation` | assigned A 1s, assigned B 2s, assigned A 3s, remove 4s, finish 6s | exact A-to-B-to-A card and recipient hashes plus the final removal |

Alternation refuses to run without two distinct currently mapped UIDs assigned
to two distinct approved contacts. A one-contact store can only produce the
production router's `recognized` result, not selected-card routing.
`stale_selection` also requires a store with at least two contacts because the
production router intentionally creates no durable selection in one-contact
mode. This is checked during preparation before beep generation or playback.

Before the scratch copy, the child checks actual WACLI mode, service and NFC
state, absent claim state, raw contacts/settings hashes, and immutable production
queue, history, outbox, state, Cloud storage, settings metadata, and WACLI store
metadata. Application modules are imported only after scratch path binding.
After binding, the actual account and parsed documents are checked. A latching
monitor repeats those checks during the run and the full guard runs immediately
before every permitted audio command and at completion. Only authorized WACLI
database byte/existence churn is allowed; existing ownership and modes remain
fixed.

The standard receipt store creates its normal empty `sent`, `pending`,
`inflight`, and `seen` directories. Those four empty regular directories are
the only accepted receipt output. Any file, link, special entry, extra
directory, recording, or outbox entry fails the run.

The child writes a private namespace receipt before audio preparation and a
content-free run receipt on success or failure. The outer supervisor owns the
direct execution process group, enforces a maximum 120-second deadline, and
terminates processes that remain in that group. Before owner use, a separately
reviewed test-owned cgroup guardian must be the sole phase owner and verify the
exact mode and service baseline, hard deadline, descendant cleanup, and final
restoration. This helper does not claim that its process-group cleanup proves
those external lifecycle guarantees. Preserve a failed namespace and receipts
for reconciliation; do not retry it or manually clear artifacts. A later
cleanup is an explicit owner action outside this helper.

This adapter adds `live_app_simulated_tag_events` evidence only after an actual
owner run. It does not prove RF reading, physical possession, onboarding UI,
enrollment/cancel/unpair, default or recent-recipient routing, a stale route
rejection, held-unknown button rejection, capture, sending, delivery, mixer
application, or audible/unclipped playback. Cloud mode and Cloud cases remain
unsupported. BB-NFC-01, routing-decision parts of BB-NFC-02, and BB-NFC-04 remain
separate acceptance work.

The private authorization schema is intentionally not illustrated with fixture
UIDs, recipients, account identifiers, or clip paths. Use the exact owner-reviewed
manifest.
