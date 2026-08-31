## Summary

Adding or changing a printable model under `hardware/models/<model-id>/`.
Do not overwrite another model's STLs.

- **Model id:**
- **Region:**
- **Status:** unvalidated / validated
- **Speaker:**
- **Microphone:**
- **Outer footprint (mm):**

## Files

- [ ] `hardware/models/<model-id>/README.md`
- [ ] `hardware/models/<model-id>/bom.md` (speaker and microphone rows required)
- [ ] `hardware/models/<model-id>/enclosure/top.stl`
- [ ] `hardware/models/<model-id>/enclosure/bottom.stl`
- [ ] Row added to the table in `hardware/models/README.md`

## Verification

- [ ] `make check` passes.
- [ ] Tests were added or updated where appropriate.
- [ ] Repository evidence and physical Pi evidence are labeled separately.
- [ ] The diff contains no secrets, personal data, recordings, device state, or private URLs.
- [ ] Third-party licensing and attribution were reviewed if dependencies or assets changed.

## Physical validation

State `not performed` or the exact printed parts, speaker, microphone, and
result. Never treat CI as proof of physical behavior.
