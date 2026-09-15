# Runtime card-pairing feedback

Pairing feedback appears inside the selected recipient row, next to its controls.
A new attempt clears the previous result. The browser distinguishes waiting,
reader unavailable, reconnecting, confirmed success and unconfirmed completion.
Cancellation clears pending polling; late responses cannot replace a newer attempt.

The NFC worker writes a private atomic `nfc-enrollment.result.json` receipt only
after committing enrollment and its existing audio handoff, before removing the
active request. It contains an opaque attempt ID and completion time, no card UID
or recipient identity. The dashboard confirms only a matching receipt within five
minutes. An absent/expired/cancelled request alone never establishes success.
The single receipt is overwritten by the next success; old tabs may therefore
show an unconfirmed result instead of claiming success without evidence.

Verify on a physical box: successful scan/reassignment, cancellation, two-minute
expiry, disconnected reader, and retry. Browser tests cannot establish PN532 or
phone-on-device acceptance. This change is independent of the portrait-layout fix.
