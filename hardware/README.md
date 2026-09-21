# Hardware

## BOM

| Item | Approx. price | Sources |
| --- | ---: | --- |
| Raspberry Pi 4 Model B, 1 GB RAM | $40 | [PiShop.US](https://www.pishop.us/product/raspberry-pi-4-model-b-1gb/), [Vilros](https://vilros.com/products/raspberry-pi-4-model-b-1), [CanaKit](https://www.canakit.com/raspberry-pi-4.html) |
| SanDisk 32 GB Ultra A1 Class 10 microSDHC card | $24 | [Amazon](https://www.amazon.com/dp/B08L5HMJVW/) |
| TONOR G11 omnidirectional USB microphone | $30 | [Amazon](https://www.amazon.com/dp/B07GVGMW59) |
| LIELONGREN 8 W USB computer speaker | $16 | [Amazon](https://www.amazon.com/dp/B08QRYTPGH) |
| iUniker 5 V / 4 A USB-C Pi 4 power supply | $10 | [Amazon](https://www.amazon.com/dp/B097P2NLVH) |
| EG STARTS 100 mm illuminated arcade button, blue | $11 | [Amazon](https://www.amazon.com/dp/B072JLSH34) |
| VGBUY 750-piece M2.5 screw, nut, and washer kit | $10 | [Amazon](https://www.amazon.com/dp/B0FJ1XN2XP) |
| HiLetgo PN532 NFC/RFID V3 module kit | $9 | [Amazon](https://www.amazon.com/dp/B01I1J17LC) |
| **Approx. total** | **$150** | |

The **Approx. total** covers only the rows above. A complete build also needs the
NFC cards or tokens, hook-up wire and insulated connectors, a microSD card reader,
and an enclosure, all listed under "You will also need" in the
[README](../README.md). Nothing in the table above connects the button to the
GPIO header.

The card reader is easy to overlook: many current laptops have no SD slot at all,
and without one there is no way to write the card. A reader that takes microSD
directly, such as [this USB-C one](https://www.amazon.com/dp/B0DQ71G4G4), avoids
also needing the card's full-size adapter.

The table lists the HiLetgo PN532 module, but `messagebox/nfc.py` is written for a
Waveshare PN532 NFC HAT: it passes a `req` pin, the PN532's P32 "H_Request", and
the `config/env.example` defaults of `D20` and `D16` are that HAT's documented
jumper positions. A HiLetgo build needs soldering and its own pin mapping.

The parts above are the reference build. Other USB speakers and microphones, and other GPIO-connected buttons, may work electrically and with the software, but each substitution is unvalidated. The printable enclosure was designed for the parts in this list. If you change the speaker, microphone, or another part, the 3D-print designs may need a revision; do not assume the substitute will fit the same case.

## Community wiring diagram

This community-contributed diagram shows a Raspberry Pi 40-pin header, an
arcade-button switch, a button LED, and a PN532 module in I²C mode. It is a
visual reference, not a physically validated reference build.

> [!CAUTION]
> The image omits LED current limiting. Do not connect a bare LED directly to
> a GPIO: use a suitable series resistor, and an appropriate driver if the
> illuminated button requires more current or a higher voltage than GPIO can
> provide. The PN532 reset/request connections also differ from the software
> defaults; use the configuration below only for this wiring. Disconnect Pi
> power before connecting or changing any wires.

![Community wiring diagram for the Raspberry Pi button, LED, and PN532 I²C module](images/community-wiring.jpeg)

### Connections shown

BCM GPIO numbers are not physical header pin numbers.

| Connection | Raspberry Pi signal | Physical header pin | Wire in diagram |
| --- | --- | ---: | --- |
| Button switch | BCM GPIO 17 | 11 | Blue |
| Button switch ground | GND | 9 | Green |
| LED positive, through suitable current limiting/driver | BCM GPIO 26 | 37 | Beige |
| LED negative | GND | 39 | Black |
| PN532 VCC | 3.3 V | 1 | Brown |
| PN532 GND | GND | 25 | Grey |
| PN532 SDA | BCM GPIO 2 / SDA | 3 | Red |
| PN532 SCL | BCM GPIO 3 / SCL | 5 | Orange |
| PN532 RST | BCM GPIO 4 / D4 | 7 | Yellow |
| PN532 IRQ | BCM GPIO 27 / D27 | 13 | Purple |

Use the switch's COM and normally-open (NO) terminals; identify them from the
actual switch markings rather than their position in the picture. On the
pictured PN532 module, I²C mode is switch **1 ON, 2 OFF**. Check the markings
and documentation for your exact board, including its reset pin and supply
requirements; other PN532 modules and HATs may differ.

### Configuration for this diagram

For this wiring, set the following values in `/etc/messagebox/env`:

```sh
MSGBOX_BUTTON_PIN=17
MSGBOX_LED_PIN=26
MSGBOX_NFC_RESET_PIN=D4
MSGBOX_NFC_REQUEST_PIN=D27
```

The repository defaults in [`config/env.example`](../config/env.example) are
instead **D20** for reset (physical pin 38) and **D16** for request (physical
pin 36). If using those defaults, connect the yellow and purple wires to
those pins instead. Do not combine one wiring layout with the other settings.

Before applying power, independently check header orientation, every
connection, LED current limiting, and the module's I²C mode. Then follow the
[hardware test](../README.md#step-8--test-the-hardware). The image and the
configuration comparison have been reviewed against the repository; physical
GPIO, LED, and NFC operation with this exact layout has not been verified as
part of this contribution.

## Reference button terminals

The EG STARTS 100 mm illuminated button has **four** spade terminals on one
carrier, and they are not interchangeable:

| Terminals | Size | What they are |
| --- | --- | --- |
| `COM` and `NO` | 4.8 mm | Microswitch — the two narrower blades |
| Lamp + and - | 6.3 mm | LED lamp — the two wider tabs with round holes |

The seller states the two sizes. The moulded `COM` and `NO` markings sit between
terminals rather than beside them, so they do not reliably identify which tab is
which.

Connecting the switch wires to the lamp pair produces a silent failure. The switch
still clicks, the wiring is continuous, the GPIO is configured correctly, and
nothing registers.

The lamp is polarity-sensitive: reversed, it stays dark rather than failing.

### Lamp circuit, reference part only

The reference button's lamp module has been observed working **directly from BCM
GPIO 26** (physical pin 37), with its return on a ground pin, driven by
`messagebox-button.service`. It also lights brightly at 5 V. The module has
internal current limiting, which is why the general caution against driving a bare
LED from a GPIO does not apply to this particular part.

Current draw was not measured. Verify your own lamp before assuming the same; a
substitute may need a transistor and a separate supply.

## PN532 NFC HAT configuration

A Waveshare-style PN532 NFC HAT **does not ship in I²C mode**. Out of the box the
DIP switches for `SCL` and `SDA` are off and both mode jumpers sit on `L`, which
selects UART. In that state the chip does not appear on the I²C bus at all: a bus
scan returns no addresses, rather than an unresponsive device.

For the repository defaults of `MSGBOX_NFC_RESET_PIN=D20` and
`MSGBOX_NFC_REQUEST_PIN=D16`:

| Setting | Value |
| --- | --- |
| DIP switches 5 (`SCL`) and 6 (`SDA`) | **ON** |
| DIP switches 1-4 (SPI) and 7-8 (UART) | OFF |
| `I0` jumper | **H** |
| `I1` jumper | **L** |
| `RSTPDN` jumper | **D20** |
| `INT0` jumper | **D16** |

The board prints its own mode table: UART is `I1=L, I0=L`; I²C is `I1=L, I0=H`;
SPI is `I1=H, I0=L`.

Confirm the reader before enabling the NFC service. A HAT in I²C mode answers at
address `0x24`:

```sh
/opt/messagebox/venv-nfc/bin/python -c "
import board, busio, time
i2c = busio.I2C(board.SCL, board.SDA)
while not i2c.try_lock(): time.sleep(0.01)
print([hex(a) for a in i2c.scan()]); i2c.unlock()"
```

An empty result means the interface selection is wrong. `0x24` present but
unresponsive points at the reset and request jumpers instead.

These HATs commonly have a stacking header, so the button and lamp can use the
pass-through pins above the board.

## Enclosure

The prototype Button Box enclosure has two printable parts:

- [Top](enclosure/button-box-enclosure-top.stl)
- [Bottom](enclosure/button-box-enclosure-bottom.stl)

See the [enclosure notes](enclosure/README.md) for dimensions.
