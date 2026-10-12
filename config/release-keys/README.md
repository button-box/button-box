# Pinned public release keys

`button-box-release-2026.pub` is the release signer's minisign PUBLIC key
(key ID 221CAE6604AC0F93), verified against a test signature. Never place a
secret key, password, or encrypted secret-key copy here; encrypted private keys
are still private keys.

Bootstrap copies the public keys in this directory to
`/etc/button-box-update/release-keys`, outside application rollback. Routine
application updates cannot change the fixed controller, recovery units, or
trusted keys. Tests generate throwaway keys in temporary directories; test keys
are never trusted by a box.

These are **release** keys: they authenticate application releases only.
The separately reviewed public key in `config/emergency-keys/` authenticates
only `trust-update.json`; it must never sign an application release or share a
key ID with a release key. Bootstrap pins it separately in
`/etc/button-box-update/emergency-keys/`, root-owned with 0644 public files.
No emergency pin means trust replacement is disabled; normal releases continue.

For a lost or stolen release key, use the offline emergency key to sign a trust
update that drops the old key, permanently revokes its ID and adds a new release
public key. Publish those two assets, then publish a normal release signed with
the new release key. The updater atomically replaces the release set, without a
physical button press. Preserve the cumulative revoked-ID list and increase the
independent trust sequence. Application rollback never restores old trust.
See [the signing and recovery procedure](../../docs/releases.md#emergency-trust-updates).

If both signing keys are lost, boxes keep working with existing Cloud features,
but their box software freezes. Recovery requires an SD-card swap with newly
reviewed pins. Losing a release key on a box without an emergency pin also
requires that recovery. Never add a new pin through an untrusted release.
