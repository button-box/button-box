## Summary

Describe the user-visible or maintainer-visible change.

For a new printable enclosure or regional BOM, add a folder under
`hardware/models/` instead of overwriting another model's STLs. Use the
[hardware-model template](PULL_REQUEST_TEMPLATE/hardware-model.md) and see
[hardware/models/README.md](../hardware/models/README.md).

## Verification

- [ ] `make check` passes.
- [ ] Tests were added or updated where appropriate.
- [ ] Repository evidence and physical Pi evidence are labeled separately.
- [ ] The diff contains no secrets, personal data, recordings, device state, or private URLs.
- [ ] Third-party licensing and attribution were reviewed if dependencies or assets changed.

## Physical validation

State `not required`, `not performed`, or provide the exact test hardware and
result. Never treat CI as proof of physical behavior.
