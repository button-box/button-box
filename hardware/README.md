# Hardware

## BOM

| Item | Approx. price | Sources |
| --- | ---: | --- |
| Raspberry Pi 4 Model B, 1 GB RAM | $40 | [PiShop.US](https://www.pishop.us/product/raspberry-pi-4-model-b-1gb/), [Vilros](https://vilros.com/products/raspberry-pi-4-model-b-1), [CanaKit](https://www.canakit.com/raspberry-pi-4.html) |
| Or Raspberry Pi 400 (same BCM2711 SoC, integrated keyboard) | $70 | [raspberrypi.com](https://www.raspberrypi.com/products/raspberry-pi-400-unit/) |
| SanDisk 32 GB Ultra A1 Class 10 microSDHC card | $24 | [Amazon](https://www.amazon.com/dp/B08L5HMJVW/) |
| TONOR G11 omnidirectional USB microphone | $30 | [Amazon](https://www.amazon.com/dp/B07GVGMW59) |
| LIELONGREN 8 W USB computer speaker | $16 | [Amazon](https://www.amazon.com/dp/B08QRYTPGH) |
| iUniker 5 V / 4 A USB-C Pi 4 / 400 power supply | $10 | [Amazon](https://www.amazon.com/dp/B097P2NLVH) |
| EG STARTS 100 mm illuminated arcade button, blue | $11 | [Amazon](https://www.amazon.com/dp/B072JLSH34) |
| VGBUY 750-piece M2.5 screw, nut, and washer kit | $10 | [Amazon](https://www.amazon.com/dp/B0FJ1XN2XP) |
| HiLetgo PN532 NFC/RFID V3 module kit (optional; omit for a no-NFC build) | $9 | [Amazon](https://www.amazon.com/dp/B01I1J17LC) |
| **Approx. total** | **$150** | |

The Raspberry Pi 400 uses the same USB-C power supply as the Pi 4B and the same
shared parts. The 400 is a single-board computer in a keyboard enclosure, so
mounting differs from the bare-board reference build; confirm GPIO button and
I²C NFC wiring before powering on.

The parts above are the reference build. Other USB speakers and microphones, and other GPIO-connected buttons, may work electrically and with the software, but each substitution is unvalidated. The printable enclosure was designed for the parts in this list. If you change the speaker, microphone, or another part, the 3D-print designs may need a revision; do not assume the substitute will fit the same case.

## Enclosure

The prototype Button Box enclosure has two printable parts:

- [Top](enclosure/button-box-enclosure-top.stl)
- [Bottom](enclosure/button-box-enclosure-bottom.stl)

See the [enclosure notes](enclosure/README.md) for dimensions.
