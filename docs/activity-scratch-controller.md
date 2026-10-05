# Activity scratch controller

`scripts/dev/simulate-activity-dashboard.py` is a test-only adapter for one
completed `simulate-received-message.py` run. It serves the installed dashboard
HTML, JavaScript, CSS, `/api/state`, `/api/data`, and the five Activity move
routes from the original `messagebox.dashboard.app.Handler`. It does not copy or
reimplement queue, hold, trash, played-history, or replay behavior.

The child binds queue, history, outbox, events, contacts, settings, receipt, and
runtime marker paths to the private receiver scratch root before importing the
dashboard. It audits every mutable path used by the allowed routes. The server
binds only `127.0.0.1`, requires the exact loopback client, `Host`, and same
origin, rejects forwarded headers, and blocks every route outside this Activity
surface. Audio reads, pairing, contact and settings management, Wi-Fi changes,
ring requests, WhatsApp sync/send commands, audio subprocesses, and service
mutations are unavailable.

Validation is the default and reads no device state:

```sh
python3 scripts/dev/simulate-activity-dashboard.py /private/path/candidate.json
```

The sole device owner may schedule `--serve` during the existing reservation.
They must run this sequence in their existing bounded owner harness, with the
reserved device alias and private artifact paths supplied from that harness:

1. Record the production tree/settings/account fingerprints and current service
   states. Verify the normal dashboard is active, the input producers are
   inactive, and normal wacli sync is active.
2. Run `sudo systemctl stop messagebox-dash.service`, then require
   `systemctl is-active messagebox-dash.service` to return `inactive`. Register
   restoration in the harness's unconditional `finally` path before proceeding.
3. Start the controller command below on the device. It must emit one `ready`
   record before the owner opens a browser.
4. From the owner's workstation, run
   `ssh -N -L 18080:127.0.0.1:18080 <reserved-device-alias>` and open exactly
   `http://127.0.0.1:18080/#activity` in the intended desktop or mobile browser.
5. Stop/wait for the controller and forward. In `finally`, run
   `sudo systemctl start messagebox-dash.service`, require its original active
   state and endpoint, and compare all original fingerprints. Retain scratch on
   any mismatch or uncertain request.

The adapter verifies that the production dashboard is inactive; it never
pauses, starts, or restores a service and is not a standalone executor. A
bounded controller command is:

```sh
python3 scripts/dev/simulate-activity-dashboard.py /private/path/candidate.json \
  --serve --receiver-root /private/path/receiver --port 18080 --timeout 300
```

The owner must retain the private start/stop JSON output and browser evidence.
The stop record contains method, path, and status only; query tokens and response
bodies are excluded. Evidence for BB-ACT-01/02 requires the real desktop and
mobile Activity page, before/after API state, and the intended item moving
through hold, reinstate, trash, and history/replay controls. The exact receiver
item and all scratch bytes remain private.

Before every allowed request and after every response, the child rechecks the
real wacli account, transport, contacts, settings outside the state directory,
production queue/history/outbox/state trees, NFC markers, service state, and the
explicit wacli database/WAL/SHM allowance from the receiver receipt. Scratch
contacts and settings must retain their authorized hashes. Any unknown change
permanently fails the child. If a check fails after the original Handler has
already committed a success response, the adapter preserves that valid response
and the exact scratch move, refuses later controls, stops, and exits nonzero;
that HTTP success is not acceptance and the owner must reconcile scratch. The
normal dashboard `GET /api/data` may prune or update only the
scratch played-history tree; it never imports the production queue path.

The child is supervised for less than 600 seconds. The requested timeout is the
whole child bound; three seconds are reserved for server close, final checks,
and process-group termination. It leaves all scratch artifacts for
reconciliation. A timeout or failed preservation check is a failed run.
The owner must not retry an uncertain replay action until the scratch tree shows
whether it already completed.

This can provide live UI evidence for BB-ACT-01 and BB-ACT-02 and bounded visual
evidence for the Activity portions of the desktop/mobile layout cases. It does
not prove ordinary production FIFO behavior, production-history retention,
audio playback, physical controls, provider delivery, or the full detailed
replay acceptance case. Those remain separate acceptance gaps.
