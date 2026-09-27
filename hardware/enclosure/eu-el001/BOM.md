# EU BOM — BB-PI4-EU-001

Reference parts list for the **European EU S8 enclosure** (EL001 speaker).
Print files: [base-print.stl](exports/base-print.stl) · [lid-print.stl](exports/lid-print.stl).

> [!NOTE]
> Frozen design snapshot (7 Sep 2026), reviewed but **not physically built**.
> Several picks were never finalized — flagged as *unresolved* below.
> Canonical version lives in the project's Notion BOM Versions database.

| # | Part | Qty | Notes |
| --- | --- | --- | --- |
| 1 | Raspberry Pi 4 Model B (1 GB) | 1 | Reference board at reviewed coordinates |
| 2 | 32 GB microSD card | 1 | Exact SKU *unresolved* |
| 3 | HOTUT USB conference mic | 1 | Bare PCB 39 × 26 mm, 30 mm mounting pitch |
| 4 | EL-001 USB soundbar | 1 | Measured envelope 186 × 56 × 38 mm; USB lead included |
| 5 | EU USB mains supply | 1 | Model *unresolved*; official Pi 15 W supply suggested |
| 6 | EG STARTS 100 mm illuminated arcade button, blue | 1 | |
| 7 | Waveshare PN532 NFC HAT | 1 | Seated directly on the Pi |
| 8 | Full-size NFC cards | — | Supplier/chip *unresolved*; 3+ intended age |
| 9 | Enclosure lid (EU R4, 3D-printed) | 1 | [lid-print.stl](exports/lid-print.stl) |
| 10 | Enclosure base (EU R4, 3D-printed) | 1 | [base-print.stl](exports/base-print.stl) |
| 11 | Mounting fasteners | — | Schedule *unresolved* |
| 12 | Internal wiring / connectors | — | Schedule *unresolved* |
| 13 | PLA feedstock | — | Grade/colour *unresolved* |

Slice the two `*-print` STLs in millimeters; the base floor and lid roof lie on Z=0.
See [eu-el001/README.md](README.md) for CAD source, rebuild instructions and review notes.
