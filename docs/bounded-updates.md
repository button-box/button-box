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
sudo python3 scripts/install/bounded_update.py apply \
  --source-root "$PWD" \
  --manifest "$PWD/release-manifest.json" \
  --backup-dir /var/backups/button-box/RELEASE-UTC_TIMESTAMP
```

The transaction records the prior bytes, permissions, ownership, boot-mode
marker and legacy mode links, plus the enabled and active state of managed
units. It does not copy `/etc/messagebox`, `/var/lib/messagebox`, or
`/var/lib/messagebox-onboarding`; those configuration and private-state trees
remain in place. Candidate program files and the boot selector are the bounded
rollback surface.

The boot-mode generator is installed as executable. The checked migration
removes the legacy mode links and enables the reconciliation path without
changing the setup marker. Only units that were active before the update are
started again, apart from the reconciliation path intentionally activated by
the migration. A failure after the first runtime mutation automatically uses
the new backup to restore the prior files, selector links, enablement, and
active units.

On success, `/opt/messagebox/release.json` records the manifest's version,
exact commit, and manifest SHA-256. Compare that file with the retained release
manifest when auditing the installed revision. A matching record and file
hashes are software evidence; they do not replace physical button, NFC, audio,
messaging, network, or cold-reboot acceptance.

## Roll back

Keep the backup directory unchanged. To restore it deliberately:

```sh
sudo python3 scripts/install/bounded_update.py rollback \
  --backup-dir /var/backups/button-box/RELEASE-UTC_TIMESTAMP
```

Rollback validates the backup records and stored file hashes before stopping
managed units. It restores files that existed, removes candidate files that
were previously absent, restores the marker and mode links, reloads systemd,
then restores recorded enablement and active units. It rejects paths and unit
names outside its fixed rollback allowlist.
