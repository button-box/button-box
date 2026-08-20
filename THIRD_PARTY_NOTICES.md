# Third-party notices

This is a preliminary source-candidate inventory, not the final distribution
notice. No third-party binaries or operating-system images are committed here.

| Component | How it is used | Distribution treatment |
| --- | --- | --- |
| `wacli` | WhatsApp Web client installed from a pinned upstream release | Preserve its upstream license and attribution; verify release checksum and license before distribution. |
| Comitup | Wi-Fi hotspot and captive-portal support installed from a pinned package | Preserve upstream and Debian package notices; verify the exact package license before distribution. |
| Adafruit CircuitPython PN532 | PN532 NFC access installed from PyPI | Preserve upstream license and dependency notices. |
| Adafruit Blinka | Raspberry Pi CircuitPython compatibility installed from PyPI | Preserve upstream license and dependency notices. |
| `lgpio` | GPIO access installed from PyPI and Debian packages | Preserve upstream license and dependency notices. |
| Raspberry Pi OS and Debian packages | Operating system and runtime dependencies | Not vendored here; any image distribution requires a complete license inventory and corresponding-source review. |

Release blocker: replace this preliminary table with verified versions,
licenses, copyright notices, source links, and any required license text. A
prebuilt image additionally requires an SBOM and provenance record.
