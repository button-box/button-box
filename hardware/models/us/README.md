# US prototype enclosure

- **Model id:** `us`
- **Human name:** US prototype Button Box
- **Region:** United States
- **Status:** Unvalidated
- **Default / canon:** yes — this is the model the main hardware list describes

Two-part printable enclosure sized with the original STLs that landed in
`hardware/enclosure/` before regional speaker variants existed.

- [Top](enclosure/top.stl)
- [Bottom](enclosure/bottom.stl)
- [BOM](bom.md)

The meshes share a 192 mm by 120 mm footprint when interpreted in millimeters.
The top is approximately 74.5 mm tall and the bottom is approximately 20.5 mm
tall.

## Speaker pocket (measured from the original STLs)

There is no parametric CAD. These numbers are from the original meshes, not
from a physical caliper session in this repository.

| Dimension | Pocket |
| --- | ---: |
| Width (X), tightest top inner walls | 183.22 mm |
| Width (X), main top inner walls | 183.86 mm |
| Width (X), front window | 183.10 mm |
| Width (X), bottom bay | 187.00 mm |
| Depth (Y) | 41.69 mm |
| Height (Z), floor to lintel | ~56.4 mm |
| Outer footprint | 192 x 120 mm |

The speaker sits grille-forward in the front bay. The example US USB soundbar
on the BOM is the LIELONGREN 8 W unit already listed on the main hardware
page (listed about 182 mm wide). That SKU is a buying hint for the pocket
these STLs were published with; it is not a newly invented part number.

Physical print and fit-check on a Pi are still required.
