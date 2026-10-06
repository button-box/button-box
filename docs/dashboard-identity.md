# Dashboard support identity

Both setup and runtime `/api/state` implementations return `box_id`: an assigned
`BOX-` number or JSON `null`. The Wi-Fi setup screens omit the support footer,
identity Copy control and owner community link so the flow focuses on getting
Button Box online. Identity remains available to operator commands and the runtime
API. It is never derived from the hostname, WhatsApp account, family card, or
message identifiers.

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
non-ASCII, symlink or non-regular files produce JSON `null`. No raw file contents or OS errors are exposed.

Read `/api/state` again to see a changed assignment. Verify the reported assignment against the physical label during the
unit's acceptance test.

## Persistence and recovery

The identity lives outside mutable onboarding/settings state. The existing setup,
upgrade and Wi-Fi reset paths do not delete it. Reboot keeps the file, but a full
SD-card reimage does **not** automatically restore it. Reconcile the physical label
and inventory record and reinstall the same verified assignment after reimage.
Never clone an assigned identity onto a different physical unit.

An assigned ID or queued ringtone request does not prove that
the unit receives, plays, records or delivers WhatsApp messages. Those remain
separate physical acceptance checks.

## Box shell color

Manufacturing sets the shell color with `sudo messageboxctl set-color pink-red`.
It must match the physical shell. `/etc/messagebox-box-color` is a root:root,
mode-0644 regular file containing one plain ASCII value plus a newline. Accepted
values are `yellow`, `pink-red`, `green`, `blue`, `white`, `black`, and `beige`.
Missing, unreadable, malformed, unknown, non-ASCII, oversized (over 32 bytes),
symlink or non-regular files safely default to `yellow` without exposing contents
or OS errors.

Like the box ID, this top-level file is outside `/etc/messagebox` and onboarding
state. Setup, provisioning, updates, Wi-Fi reset and reboot keep it; installers
never copy a developer's color file. After a full SD-card reimage, run `set-color`
again to match the shell. The initializer prints the color on the insert, Cloud
registration sends it as `color`, and each setup or dashboard HTML request reads
it for the accent theme. Reload the page after changing it. Color is not sent in
heartbeats.
