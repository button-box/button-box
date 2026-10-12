# Emergency public pins

The coordinator adds the independently reviewed real minisign `.pub` here.
No production emergency key or test key is included in this checkout.
Bootstrap copies these public pins to `/etc/button-box-update/emergency-keys/`.
An empty directory disables over-the-air trust replacement. Keep the emergency
secret offline and separate from release signing keys. See
[release procedures](../../docs/releases.md#emergency-trust-updates).
