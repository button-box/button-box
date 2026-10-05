# Immediate Cloud settings

Cloud settings compare the expected revision to the local settings under the existing file lock. A new desired revision may skip failed or expired generations, but must advance. The full document is validated before atomic replacement. Replays of the same document do not increment the revision, and stale local writes still fail.

The poller checks for the button process’s applied marker promptly while a settings change is pending. It acknowledges success only when that marker matches the entire requested document. The Cloud dashboard waits for this receipt before showing success, with a short deadline and an explicit unconfirmed outcome when confirmation is unavailable.
