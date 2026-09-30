# Manifest-bounded updates

Use the bounded updater for a reviewed application release on an existing
Button Box. It installs only paths named by the release manifest, preserves the
current boot mode, records the installed release identity, and retains a local
root-only rollback. It does not run setup or provisioning.

This procedure does not establish the identity of a physical box, transfer a
package, or authorize a deployment. Resolve the target and the release through
the applicable operating process before running it.

## Prepare the release source

From the exact reviewed commit, run the repository checks and write its
installed-file manifest into the package:

```sh
make check
python3 scripts/dev/release-manifest.py >release-manifest.json
```

Package the source tree without credentials, device data, logs, caches, or
generated private media. Keep the manifest next to that extracted tree on the
device. Its `commit`, `version`, file paths, and SHA-256 hashes are the inputs
to the update. Verify any outer archive checksum before extraction.

Extract into a new root-owned staging directory. The source root, manifest,
every source file, and every directory between them must be owned by root and
must not be writable by group or other users. Preserve the generator's `0755`
mode. For an archive whose contents are rooted directly at the source tree:

```sh
sudo install -d -o root -g root -m 0700 /var/lib/button-box-update/RELEASE
sudo tar --extract --gzip --no-same-owner \
  --file button-box-release.tar.gz \
  --directory /var/lib/button-box-update/RELEASE
```

The updater refuses user-owned or writable staging. This matters because the
checked migration loads the staged generator as root.

The updater validates every source path, destination, hash, duplicate, existing
destination type, and the staged boot-mode migration before it stops a unit or
writes an installed file. The allowlist is deliberately narrower than the
filesystem prefixes used by a general installer. Adding an installed release
file requires updating both the canonical release manifest mapping and the
bounded updater mapping.

## Apply

Choose a new backup directory under the device's root-only backup area, then
run from the extracted source tree:

```sh
sudo python3 /var/lib/button-box-update/RELEASE/scripts/install/bounded_update.py apply \
  --source-root /var/lib/button-box-update/RELEASE \
  --manifest /var/lib/button-box-update/RELEASE/release-manifest.json \
  --backup-dir /var/backups/button-box/RELEASE-UTC_TIMESTAMP
```

The transaction records the prior bytes, permissions, ownership, boot-mode
marker and legacy mode links, plus the enabled and active state of managed
units. Before stopping them, it also records active external units in the
reverse `PartOf` closure of active managed units. Their unit files and
enablement remain outside the updater's rollback surface. An inactive external
unit is not started. Version 2 backups include these active unit names; older
version 1 backups remain readable but contain no such record. It does not copy
`/etc/messagebox`, `/var/lib/messagebox`, or
`/var/lib/messagebox-onboarding`; those configuration and private-state trees
remain in place. Candidate program files and the boot selector are the bounded
rollback surface.

The boot-mode generator is installed as executable. The checked migration
removes the legacy mode links and enables the reconciliation path without
changing the setup marker. Only units that were active before the update are
started again, apart from the reconciliation path intentionally activated by
the migration. Managed units start individually in a fixed dependency order;
recorded external units start afterward in their discovered order,
with systemd dependency expansion suppressed, so restoring an active target
cannot briefly start an inactive component. When ComItUp was active, it alone
selects and starts its home-network or hotspot portal; the updater must not race
its network-state transitions with a separate portal start. Restoration still
verifies the exact recorded portal states. Restoration allows up to 30 seconds
for setup services to settle, then requires the exact intended active states to
remain stable for two seconds. A newly failed unit or a persistent mismatch
fails verification; the initial pre-update snapshot still rejects transitions.
A failure after the first runtime mutation automatically
uses the new backup to restore the prior files, selector links, enablement, and
active units.

Apply and rollback share one nonblocking, root-owned update lock. A concurrent
operator command fails before inspecting or changing release state, and an
automatic rollback keeps the original apply operation's lock for the entire
recovery.

On success, `/opt/messagebox/release.json` records the manifest's version,
exact commit, and manifest SHA-256. Compare that file with the retained release
manifest when auditing the installed revision. A matching record and file
hashes are software evidence; they do not replace physical button, NFC, audio,
messaging, network, or cold-reboot acceptance.

## Roll back

Keep the backup directory unchanged. To restore it deliberately:

```sh
sudo python3 /var/lib/button-box-update/RELEASE/scripts/install/bounded_update.py rollback \
  --backup-dir /var/backups/button-box/RELEASE-UTC_TIMESTAMP
```

Rollback validates the backup records and stored file hashes before stopping
managed units. It restores files that existed, removes candidate files that
were previously absent, restores the marker and mode links, reloads systemd,
then restores recorded enablement and active units, including any recorded
external units. It rejects paths outside its fixed rollback allowlist and
invalid or duplicate external unit names.

A lost power supply or forced process termination can prevent automatic
rollback from running. Keep the original staging tree and completed backup;
after access is restored, run the explicit rollback command before retrying an
interrupted update. Verify installed hashes, boot selection and service state
again. An interrupted process is not a successful installation.
