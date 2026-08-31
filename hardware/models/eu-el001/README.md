# EU EL-001 enclosure

- **Model id:** `eu-el001`
- **Human name:** EU EL-001 Button Box
- **Region:** Europe
- **Status:** Unvalidated
- **Default / canon:** no — use this when printing for the EL-001 speaker

Two-part printable enclosure widened from the `us` STLs so the front speaker
bay accepts the EL-001 USB speaker (187 x 55 x 40 mm) bought in Europe.

- [Top](enclosure/top.stl)
- [Bottom](enclosure/bottom.stl)
- [BOM](bom.md)

The meshes share a 197 mm by 120 mm footprint when interpreted in millimeters.
The top is approximately 74.5 mm tall and the bottom is approximately 20.5 mm
tall.

## Speaker pocket (EU EL-001)

- Product: USB Speaker for Notebook & PC
- Model: EL-001
- Manufacturer: Shenzhen Liguo Electronics Co., Ltd.
- Box size: 187 x 55 x 40 mm
- Input: USB 5V, output: 3W x 2

The speaker sits grille-forward in the front bay: 187 mm along the long axis of
the box, 40 mm into the box, and 55 mm tall.

| Dimension | `us` pocket | `eu-el001` pocket | EL-001 + clearance |
| --- | ---: | ---: | ---: |
| Width (X), tightest top inner walls | 183.22 mm | 188.22 mm | 187 mm + 0.61 mm/side |
| Width (X), main top inner walls | 183.86 mm | 188.86 mm | 187 mm + 0.93 mm/side |
| Width (X), front window | 183.10 mm | 188.10 mm | 187 mm + 0.55 mm/side |
| Width (X), bottom bay | 187.00 mm | 192.00 mm | 187 mm + 2.50 mm/side |
| Depth (Y) | 41.69 mm | 41.69 mm | 40 mm + 0.84 mm total |
| Height (Z), floor to lintel | ~56.4 mm | ~56.4 mm | 55 mm + ~1.4 mm total |
| Outer footprint | 192 x 120 mm | 197 x 120 mm | — |

Width clearance is a slip fit for FDM: snug enough that the bar should not
rattle, loose enough that it should not need force that cracks a print. The
`us` top window is 3.9 mm narrower than the EL-001 and has essentially no
print clearance; that is the dimension that missed.

The extra 5 mm of outer width is a 2.5 mm end-cap spread at x = ±88 mm (the
front-corner centers). That keeps the ~8 mm corner radii and wall thickness
instead of carving through them. Button, Pi, microphone, and NFC pockets in
the center of the box are unchanged.

No parametric CAD was in the repository. The spread is implemented by
`scripts/dev/widen_speaker_pocket.py` (read `us`, write `eu-el001`; do not
re-run it on the already-widened files). Physical reprint and fit-check on a
Pi are still required.
