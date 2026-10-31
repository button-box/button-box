# Owner-only wacli scratch receiver

`scripts/dev/simulate-received-message.py` is a two-step test-only adapter for
one newly received, explicitly authorized wacli voice note. It is narrower than
the normal FIFO receiver and the standard input simulator. It does not prove
FIFO behavior, replay or retention against an existing history, hardware
behavior, counterpart delivery, or a Cloud-mode path.

The receive authorization is private JSON with exactly these fields:

- `version: 1` and `transport: "wacli"`;
- the existing approved `recipient` and exact expected `sender_jid`;
- an owner-observed `not_before` and `max_age_seconds` no greater than 600;
- SHA-256 fingerprints of the unchanged production contacts, settings, and
  linked wacli account;
- the sorted subset of `wacli.db`, `wacli.db-shm`, and `wacli.db-wal` that the
  active normal sync process may change during the run.

Use a private, empty receiver directory, a new candidate-manifest pathname in a
separate private directory, and later a different private, empty outbound
scratch directory. Validate the input plan without execution first. The owner
must then stop and verify inactive `messagebox-button`, `messagebox-nfc`, and
`messagebox-poller` services while leaving `messagebox-sync` active with its
reviewed account. The child requires an explicit `MSGBOX_TRANSPORT=wacli`; an
unset or different mode fails closed. Do not pair, relink, reset, edit contacts/settings, or move any
existing queue, history, outbox, receipt, or media file for this test.

In `--receive` mode the child binds runtime paths before importing application
modules, copies the existing seen IDs and exact contacts/settings into scratch,
and calls the production `poll_once`, media download, authorization, transcode,
and `queue_message` path. It refuses unless the normal ten-message listing has
exactly one unseen, authorized playable item, and that item is a fresh audio
message from the authorized sender to the authorized recipient. The guarded
wacli boundary permits only the exact list and target-download commands.

After a complete scratch WAV and routing sidecar exist, the adapter writes the
candidate manifest and a `seen_commit_pending` receipt. It then rechecks the
inactive producers, production queue/outbox/state trees, contacts, settings,
and account before atomically adding only the target message ID to the real
seen ledger. This one deduplication entry prevents the restored normal poller
from later copying the deliberate target into the production queue. The
receipt becomes `received` only after the production queue/outbox are unchanged,
the state tree differs only by that exact seen-ID addition, and the wacli store
diff is confined to the authorization's database allowlist. Production root and
entry ownership/modes, the known lgpio notification FIFO, and existing NFC
markers are also preserved and rechecked.

The private receipt retains the exact observed server timestamp and its parsed
Unix value. The candidate and pending receipt files and their parent directory
entries are synced before the production seen ledger is changed. Execute-time,
claim-time, and send-time guards apply the original `not_before`, future-skew,
and maximum-age bounds to that receiver timestamp; newly created scratch-file
mtimes cannot refresh an old message.

Any failure or timeout requires reconciliation before retry. Preserve the
receiver directory, candidate manifest, outbound directory, and production
seen ledger. A `seen_commit_pending` receipt is intentionally not executable:
determine whether the target ID was committed and whether the ordinary poller
could have received it before deciding on a new run. A failed or uncertain send
remains in the separate outbound scratch for exact transport reconciliation;
never retry it blindly.

In `--execute` mode the adapter requires the completed receipt to bind the same
manifest, target, original authorization, account, storage paths, and seen-ID
commit. It then delegates unchanged to `simulate-selected-message.py`. That
driver supplies the real confirmed-press handler, production inbound
authorization, atomic selected claim, real speaker/microphone review flow, one
generated outbox job, exact-recipient wacli send, and its child-only claim
confirmation guard. Both phases compare the real production roots before and
after. Live production contacts, settings, account, transport, sync-service
state, and NFC markers are rechecked at the selected driver's claim and send
validation boundaries; only scratch artifacts, the exact seen-ID addition, and explicitly
allowed normal wacli database churn are accepted.
