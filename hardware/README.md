# Hardware

Printable architectures live under [models/](models/README.md). Each model has
its own enclosure STLs and audio BOM. **Default / canon model:** [`us`](models/us/).

## Shared parts

These parts are common across current models. Speaker and microphone are
per-model — see the table below.

| Item | Approx. price | Sources |
| --- | ---: | --- |
| Raspberry Pi 4 Model B, 1 GB RAM | $40 | [PiShop.US](https://www.pishop.us/product/raspberry-pi-4-model-b-1gb/), [Vilros](https://vilros.com/products/raspberry-pi-4-model-b-1), [CanaKit](https://www.canakit.com/raspberry-pi-4.html) |
| SanDisk 32 GB Ultra A1 Class 10 microSDHC card | $24 | [Amazon](https://www.amazon.com/dp/B08L5HMJVW/) |
| iUniker 5 V / 4 A USB-C Pi 4 power supply | $10 | [Amazon](https://www.amazon.com/dp/B097P2NLVH) |
| EG STARTS 100 mm illuminated arcade button, blue | $11 | [Amazon](https://www.amazon.com/dp/B072JLSH34) |
| VGBUY 750-piece M2.5 screw, nut, and washer kit | $10 | [Amazon](https://www.amazon.com/dp/B0FJ1XN2XP) |
| HiLetgo PN532 NFC/RFID V3 module kit | $9 | [Amazon](https://www.amazon.com/dp/B01I1J17LC) |

The default `us` audio pairing (TONOR G11 microphone and the US USB soundbar)
brings the published reference build to about **$150** before the enclosure,
Zero 2 W adapters, NFC tokens, shipping, and taxes.

## Models

| Model | Region | Speaker | Microphone | STLs | Status |
| --- | --- | --- | --- | --- | --- |
| [`us`](models/us/) (default) | US | USB soundbar, pocket 183.2 mm (example LIELONGREN 8 W) | TONOR G11 USB omni | [top](models/us/enclosure/top.stl), [bottom](models/us/enclosure/bottom.stl) | Unvalidated |
| [`eu-el001`](models/eu-el001/) | EU | EL-001 187 x 55 x 40 mm | TBD | [top](models/eu-el001/enclosure/top.stl), [bottom](models/eu-el001/enclosure/bottom.stl) | Unvalidated |

Full rows and notes are in each model's `bom.md`. To add a third model, open a
pull request that adds a folder — see [how to add a model](models/README.md).

## Enclosure paths

`hardware/enclosure/` is a pointer only. Use the model STLs above. Old
`button-box-enclosure-*.stl` filenames are retired so a path always includes a
model id. See the [enclosure pointer](enclosure/README.md).
