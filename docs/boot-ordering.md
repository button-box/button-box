# Boot recovery ordering

The Cloud bootstrap adds `Requires=` and `After=messagebox-update-recovery.service`
to application units, including path and timer activators. Recovery is a normal
service: its implicit ordering is after `basic.target`. A default path unit is
before `paths.target`, and a default timer is before `timers.target`; both targets
are before `basic.target`. Combining those dependencies forms a boot cycle.
Systemd may delete recovery's job to break it, leaving dependent application and
controller starts unsuccessful. A successful boot on another attempt does not
disprove the cycle.

## Repair

These application activators explicitly disable default dependencies and order
themselves after `basic.target`:

- `messagebox-mode-reconcile.path`
- `messagebox-wifi-change.path`
- `messagebox-wifi-watchdog.timer`
- `messagebox-onboarding-complete.path`
- `messagebox-onboarding-voice.path`

They retain `Requires=sysinit.target` and explicitly restore
`Conflicts=shutdown.target` / `Before=shutdown.target`. Their path triggers,
timer intervals, target membership, mode conditions and install inhibit are
unchanged. In wacli mode they have no dependency on the Cloud updater. Their
services already run after basic initialization; the watchers now do too.
Application targets have no implicit ordering before basic initialization and
need no change. The independent updater timers have no recovery ordering gate
and retain their original default dependencies.

The existing Cloud bootstrap drop-ins remain unchanged, so recovery still
finishes before any gated application unit, controller, selector or supervisor
starts. Recovery does not move to early boot: `/usr/bin/python3`, local mounts
(including separate `/var` and `/opt` mounts), `/run/lock`, update journals and
snapshots, and its existing sandbox continue to use the normal service boot
environment. There is no new pre-sysinit filesystem or sandbox assumption.

## Signed delivery and its limits

The manifest mapper already installs these five unit files under
`/etc/systemd/system/`; they are already in the release payload allowlist.
Both frozen and current workers accept their paths. The application transaction
snapshots the old external files, installs the new units while inhibited and
quiesced, switches the application and reloads systemd. A committed release
therefore has the corrected graph on its next boot, even with the original
`software-update.conf` files still present. No bootstrap rerun or updater binary
rewrite is needed. Application rollback or interrupted-install recovery restores
the old unit files, so the repair is retained only after successful commit.
Until all five units are replaced, or after rollback to the old units, the
original cycle exposure remains. Host interrupted-write tests exercise rollback
once recovery runs; they cannot prove systemd will schedule that recovery on a
partially repaired physical box.

The seven original updater unit files are frozen: changing the recovery service
file would cause `maintenance_release_required`. Legacy workers accept a
`*.service.d/messagebox.conf`, but the installed frozen selector rejects new or
changed controller/recovery drop-ins. Changing a recovery drop-in would therefore
miss selector-equipped boxes. The five application activators are outside those
protections, and permanent controller routes are preserved by this repair.

Reachability depends on the installed controller and current runtime:

- A running, bootstrapped Cloud box with a compatible selected controller can
  receive this repair in its normal signed application release. Existing policy,
  approval, time window, trust, compatibility and sequence checks still apply.
- A box still running the original v0.3.9–v0.3.12 worker accepts a compatible
  application carrying just this repair. The complete current application also
  includes `sounds/voices/buttonbox`, which that worker does not accept. A
  compatible signed controller bridge and promotion must precede the full current
  application, or the hotfix must be packaged against the legacy application.
  A signed descriptor alone does not promote the controller. Check the selected
  controller, not just the application's version string.
- If boot deleted the recovery job, a later discovery timer activation pulls in
  controller and recovery again. Once the original boot jobs have settled, that
  transaction need not contain the offending path jobs and can succeed. This is
  conditional, not a guaranteed field recovery: failed or rate-limited services,
  a failed timer, or an incomplete recovery can still prevent discovery.
- Even when discovery resumes, an entirely inactive runtime cannot normally
  install the release. The frozen broker's `installed()` gate needs an active
  button PID and current-boot confirmation proof; Auto refuses without it and
  Ask needs the button. Those boxes need runtime restoration first. On a
  settled boot, an operator can use `sudo systemctl start messagebox.target`,
  then verify recovery, runtime and heartbeat before the normal signed update.
  A reboot may happen to start the old release, but is not the repair.
- Boxes without an operational bootstrap, usable trust/network/storage, or a
  supported update platform cannot acquire this release through this OTA path.
  Setup mode still defers normal application updates. wacli has no Cloud
  application discovery; its normal installation receives the same unit fix.

No new runtime self-heal monitor is included. The existing Wi-Fi watchdog belongs
to `messagebox.target` and cannot rescue an inactive parent. An independent
monitor would need the update lock throughout probation, a terminal journal,
completed recovery and the mode transition lock: checking only `install-inhibit`
is insufficient because probation removes it. A new monitor also cannot reach
boxes already stranded behind the old broker's readiness gate. Keep that work
separate from this five-unit repair rather than add an updater competitor.

## Validation and physical acceptance

`tests/test_boot_ordering.py` parses every shipped unit/drop-in and the actual
bootstrap-generated gates, models the relevant systemd default dependencies,
reproduces the frozen cycle and checks Cloud, wacli and selector graphs. It
tests the repair through every recorded frozen worker's verification, extraction
and transaction path with and without selector admission; it also tests the full
current payload with the current worker, legacy payload rejection, partial-write
boot rollback and
the dead-button readiness gate. Host systemd, outer signature results and health
are substitutes in the field harness. The native dependency-graph test runs
`systemd-analyze verify` when available, with OS dependency stubs and service
executables replaced; macOS uses the static checks. These are not Pi boot tests.

On a physical Cloud Pi, install the signed candidate through the actual selected
controller, verify the five effective units and unchanged recovery gates, and
run `systemd-analyze verify` against the installed units. Repeatedly power-cycle
and inspect each boot for ordering-cycle/job-deletion messages, recovery success,
active `messagebox.target` and runtime services, and a fresh Cloud heartbeat.
Test a power cut during installation and confirm rollback/recovery ordering;
test setup mode and updater probation to confirm nothing starts ahead of recovery.
Before merging a release, the physical wacli send/play/reply, family card,
quiet-hours and listened-notice checks remain required.

This is a Pi boot/updater fix. Cloud assistant, Cloud service/dashboard and setup
screens need no change. A companion Cloud feature could alert on a claimed box's
missing heartbeat, with startup/update/setup grace periods and deduplication.
Such an alert detects outages; it does not repair the Pi. It is not implemented
here.
