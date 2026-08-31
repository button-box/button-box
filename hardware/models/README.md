# Printable Button Box models

Each folder under `hardware/models/` is one printable architecture: enclosure
STLs plus the BOM that goes with them. The software stays the same; the plastic
and the speaker or microphone do not.

**Default / canon model:** [`us`](us/) — the original 192 mm enclosure and the
parts listed on the main [hardware list](../README.md).

| Model | Region | Speaker | Microphone | Footprint | Status |
| --- | --- | --- | --- | ---: | --- |
| [`us`](us/) | US | USB soundbar the original pocket was sized for (example LIELONGREN 8 W) | TONOR G11 USB omni | 192 x 120 mm | Unvalidated |
| [`eu-el001`](eu-el001/) | EU | EL-001 187 x 55 x 40 mm | TBD (see that model's BOM) | 197 x 120 mm | Unvalidated |

`Unvalidated` means the STLs have not been print-tested and fit-checked against
the listed parts on a Raspberry Pi in this repository.

## How to add a model

Do not overwrite another model's STLs. Open a pull request that adds a new
folder.

1. Pick a lowercase model id: letters, digits, and hyphens only
   (`eu-el001`, `us`, `jp-soundbar-v2`). The id is the folder name.
2. Copy the required files below into `hardware/models/<model-id>/`.
3. Fill in `README.md` and `bom.md`. Speaker and microphone rows are required.
4. Add printable `enclosure/top.stl` and `enclosure/bottom.stl`.
5. Open a pull request. Use the
   [hardware-model PR template](../../.github/PULL_REQUEST_TEMPLATE/hardware-model.md)
   if your GitHub UI offers a template chooser, or copy that checklist into the
   PR body.

### Required files

```
hardware/models/<model-id>/
  README.md           # human name, region, status
  bom.md              # parts table
  enclosure/
    top.stl
    bottom.stl
```

Optional, in the same folder: architecture notes, photos, or a parametric
source file. Keep one-off scripts out of this tree; developer tooling belongs
in `scripts/dev/`.

### README.md columns

State at least:

- Human name
- Model id (must match the folder)
- Region or market the parts were bought in
- Status: `validated` or `unvalidated`
- Outer footprint in millimeters
- Links to `bom.md` and the two STLs

Use `validated` only when a named person has printed the parts and assembled
them with the listed speaker and microphone. CI passing is not validation.

### BOM columns

`bom.md` must include a table with these columns:

| Part | Vendor-agnostic name | Example SKU | Notes |
| --- | --- | --- | --- |

Speaker and microphone are required rows. Other parts (Pi, button, NFC, power)
may be listed here or pointed at the shared list on [hardware/README.md](../README.md).

Do not recommend the Adafruit 3367 puck microphone; it was tried and the
quality was bad. Prefer a USB mic that is known to work (TONOR G11 on `us`),
an INMP441 I2S board with the GPIO 20 / PN532 conflict called out, or a
disassembled TONOR G11, and mark unknowns as TBD.

Example SKUs are buying hints, not exclusive requirements. If the enclosure is
sized from a measured pocket rather than a published SKU, say that in Notes.

### PR checklist

- [ ] New folder `hardware/models/<model-id>/` — nothing overwritten
- [ ] `README.md` has name, region, and `validated` / `unvalidated`
- [ ] `bom.md` has speaker and microphone rows with the columns above
- [ ] `enclosure/top.stl` and `enclosure/bottom.stl` are included
- [ ] This model's row is added to the table in `hardware/models/README.md`
- [ ] `make check` passes
- [ ] Physical print/fit status is labeled (usually `not performed`)

## Old enclosure paths

`hardware/enclosure/` no longer stores a single unnamed STL pair. That README
points here so old links do not look like a third architecture.
