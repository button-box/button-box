# EU BOM — BB-PI4-EU-001

Reference parts list for the **European EU S8 enclosure** (EL001 speaker).
Print files: [base-print.stl](exports/base-print.stl) · [lid-print.stl](exports/lid-print.stl).

Prices checked 27 Sep 2026; retailers change stock and pricing. We don't endorse any retailer.

| # | Part | Qty | Buy | Notes |
| --- | --- | --- | --- | --- |
| 1 | Raspberry Pi 4 Model B (1 GB) | 1 | [Welectron €39.90](https://www.welectron.com/Raspberry-Pi-4-B-1-GB) | Reference board at reviewed coordinates |
| 2 | 32 GB microSD card | 1 | [Conrad €14.99](https://www.conrad.de/de/p/sandisk-ultra-microsdhc-microsdhc-karte-32-gb-class-10-uhs-i-inkl-sd-adapter-2336835.html) | SanDisk Ultra A1, with adapter |
| 3 | HOTUT USB conference mic | 1 | [eBay.de €22.95](https://www.ebay.de/itm/147504383698) | Bare PCB 39 × 26 mm, 30 mm mounting pitch |
| 4 | EL-001 USB soundbar | 1 | — | Measured envelope 186 × 56 × 38 mm; USB lead included. The exact EL-001 is not currently stocked by EU retailers; the closest found ([ZETIY soundbar €35.29](https://www.ebay.de/itm/336533952004)) is 220 × 63 × 36 mm and does not fit this enclosure as designed |
| 5 | EU USB mains supply | 1 | [Welectron €7.50](https://www.welectron.com/Offizielles-Raspberry-Pi-4-Steckernetzteil-USB-C-51V-3A) | Official 15 W USB-C, EU plug (also [BerryBase €7.90](https://www.berrybase.de/offizielles-raspberry-pi-usb-c-netzteil-5-1v-3-0a-eu-schwarz)) |
| 6 | EG STARTS 100 mm illuminated arcade button, blue | 1 | [amazon.de €11.00](https://www.amazon.de/dp/B072JLSH34) | |
| 7 | Waveshare PN532 NFC HAT | 1 | — | Seated directly on the Pi. Out of stock at every EU retailer checked on 27 Sep 2026 |
| 8 | Full-size NFC cards | — | [nfc-tag-shop.de €1.69/pc from 10](https://www.nfc-tag-shop.de/NFC-Karte-PVC-85-6-x-54-mm-NTAG213-180-Byte-orange-matt/17068) | NTAG213, credit-card format; 3+ intended age |
| 9 | Enclosure lid (EU R4, 3D-printed) | 1 | Print it | [lid-print.stl](exports/lid-print.stl) |
| 10 | Enclosure base (EU R4, 3D-printed) | 1 | Print it | [base-print.stl](exports/base-print.stl) |
| 11 | Mounting fasteners | — | [Conrad €16.95](https://www.conrad.de/de/p/thicon-models-20139-schraubensortiment-300-st-2898917.html) | M3 screw/nut/washer assortment, 300 pc |
| 12 | Internal wiring / connectors | — | — | Match terminal sizes on your button before ordering |
| 13 | PLA feedstock | — | [3DJake $18.05/kg](https://www.3djake.com/esun/pla-black-7?sai=11751) | eSUN PLA+, 1.75 mm |

Slice the two `*-print` STLs in millimeters; the base floor and lid roof lie on Z=0.
See [eu-el001/README.md](README.md) for CAD source, rebuild instructions and review notes.
