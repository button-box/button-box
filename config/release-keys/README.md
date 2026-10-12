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

For rotation, distribute an old-key-signed **bridge** through an attended
maintenance operation that adds the new public key to device trust. Later
releases use the new key. Keep the old key trusted until every box has the
bridge. Remove the old key only through a later reviewed maintenance step. Never
replace the pin and sign only with the new key: existing devices could not
authenticate it. A lost or compromised key needs physical recovery unless a
separate emergency key was pinned during bootstrap.
