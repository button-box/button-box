# Architecture

Message Box runs as separate systemd services with constrained users and
permissions. Runtime services use the `messagebox` account, while the web
onboarding service uses `messagebox-onboarding`. systemd limits filesystem,
device, and capability access. Wi-Fi management and reset operations retain the
root access they require.

## Runtime services

`messagebox.target` groups the enabled runtime services:

| Unit | Role |
| --- | --- |
| `messagebox-button.service` | Record, play, and send voice messages |
| `messagebox-poller.service` | Queue voice messages from configured contacts |
| `messagebox-sync.service` | Keep the local WhatsApp store synchronized |
| `messagebox-nfc.service` | Read recipient cards and maintain NFC selection state |
| `messagebox-dash.service` | Serve the optional status and queue dashboard |

## Onboarding services

| Unit | Role |
| --- | --- |
| `comitup.service` | Use [Comitup](https://github.com/davesteele/comitup) to manage Wi-Fi and the setup hotspot through NetworkManager |
| `comitup-web.service` | Serve the onboarding portal on the setup hotspot |
| `messagebox-onboarding-home.service` | Serve the portal after Wi-Fi setup |
| `messagebox-whatsapp-pairing.service` | Isolate WhatsApp pairing operations |

`messagebox-wifi-reset.service` is a one-shot boot check for the physical Wi-Fi
reset gesture. The onboarding portal accesses Comitup through a restricted
D-Bus policy. NetworkManager and Avahi provide network management and local name
resolution.
