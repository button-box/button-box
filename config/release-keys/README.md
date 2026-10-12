# Pinned public release keys

This directory is deliberately empty of keys. No key was generated for this
change. Before the first signed release, the human signer supplies the dedicated
minisign PUBLIC key as a reviewed `*.pub` file here. Verify its fingerprint through
an independent trusted channel. Never place a secret key, password, or encrypted
secret-key copy here; encrypted private keys are still private keys.

The release verifier requires an explicitly selected key from this directory.
Installing these pins on devices and maintaining trust outside application
rollback belong to the updater/bootstrap work. Merely adding a public key here
does not update device trust.

For rotation, distribute an old-key-signed **bridge release** that adds the new
public key to persistent device trust. Subsequent releases use the new key. Keep
the old key trusted until fleet acknowledgements confirm the bridge is installed;
missed devices can still install the bridge. Remove the old key only through a
later reviewed release. Never replace the pin and immediately sign only with the
new key: existing devices could not authenticate it. Lost or compromised keys
require physical recovery unless a separate emergency rotation key was already
pinned during bootstrap.
