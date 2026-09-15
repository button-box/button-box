# Dashboard support identity

The shared onboarding and household dashboard show the physical inventory **Box ID**
in the support footer, not the header. Both `/api/state` implementations return
`box_id`: an assigned `BOX-` number or JSON `null`. This is public household support
information, not a credential. It is never derived from the hostname, WhatsApp
account, NFC card, or message identifiers.

## Assign an existing inventory ID

The operator must first reconcile the physical unit with its existing inventory
record. Do not allocate a new ID just because the footer is unassigned. Prepare a
plain ASCII file named `box-id.txt` containing the verified ID and a newline, then
on that **authorized target** install it as:

```sh
sudo install -o root -g root -m 0644 box-id.txt /etc/messagebox-box-id
```

The fixed top-level path is intentional: onboarding cannot traverse the private
`/etc/messagebox` runtime directory. No service-group or directory permissions are
relaxed. Root owns the file and service users can only read it. The dashboard has
no identity-write endpoint. Provisioning installs the reader module but never
copies a developer's identity file or assigns a sample ID.

Accepted IDs are `BOX-` followed by 1–9 digits, with no leading zero. The entire
file must be at most 32 bytes, including whitespace. Missing, unreadable, malformed,
non-ASCII, symlink or non-regular files produce `null`, **Not assigned**, and a
disabled Copy button. No raw file contents or OS errors are exposed.

Reload any dashboard route to read a changed assignment. Verify the footer on both
the setup and runtime interfaces during the unit's acceptance test. Copy uses the
existing secure clipboard API or plain-HTTP fallback, with manual selection guidance
if copying is denied.

## Persistence and recovery

The identity lives outside mutable onboarding/settings state. The existing setup,
upgrade and Wi-Fi reset paths do not delete it. Reboot keeps the file, but a full
SD-card reimage does **not** automatically restore it. Reconcile the physical label
and inventory record and reinstall the same verified assignment after reimage.
Never clone an assigned identity onto a different physical unit.

An assigned ID, working Copy button or queued ringtone request does not prove that
the unit receives, plays, records or delivers WhatsApp messages. Those remain
separate physical acceptance checks.
