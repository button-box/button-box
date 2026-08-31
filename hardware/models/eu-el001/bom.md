# EU EL-001 BOM

Shared parts (Pi, arcade button, NFC, screws) match the main
[hardware list](../../README.md). Use a 5 V Pi supply that is legal for your
mains region. Audio parts for this model:

| Part | Vendor-agnostic name | Example SKU | Notes |
| --- | --- | --- | --- |
| Speaker | USB computer soundbar, 187 x 55 x 40 mm | EL-001, Shenzhen Liguo Electronics Co., Ltd. | Size printed on the product box. USB 5V in, 3W x 2 out. This is the part the widened pocket is for. |
| Microphone | TBD | Not chosen | Do not use the Adafruit 3367 puck; quality was bad. Candidates: INMP441 I2S MEMS board (I2S wiring vs PN532 — the default PN532 reset pin is D20 / GPIO 20, so an I2S board that wants that pin conflicts until pins are remapped), or crack a TONOR G11 and mount the innards. Until one of those is validated, treat the mic as TBD. |
