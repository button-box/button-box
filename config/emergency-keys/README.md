# Emergency public pins

`button-box-emergency-2026.pub` is the emergency signer's minisign PUBLIC key
(key ID 73BD56BB8F9940FC). It was reviewed independently of the release key.
No secret key or test key is included in this directory.
Bootstrap copies these public pins to `/etc/button-box-update/emergency-keys/`.
An empty directory disables over-the-air trust replacement. Keep the emergency
secret offline and separate from release signing keys. See
[release procedures](../../docs/releases.md#emergency-trust-updates).
