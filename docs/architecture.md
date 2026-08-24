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

One exact default recipient is stored with the private contact allow-list. A
recognized NFC selection overrides that default; otherwise the default is used.
No default, an unknown card, or invalid routing state fails closed.

## Onboarding services

| Unit | Role |
| --- | --- |
| `comitup.service` | Use [Comitup](https://github.com/davesteele/comitup) to manage Wi-Fi and the setup hotspot through NetworkManager |
| `comitup-web.service` | Serve the onboarding portal on the setup hotspot |
| `messagebox-onboarding-home.service` | Serve the portal after Wi-Fi setup |
| `messagebox-whatsapp-pairing.service` | Isolate WhatsApp pairing operations |
| `messagebox-onboarding-nfc.service` | Read tags and own private tag-first pairing state |
| `messagebox-onboarding-complete.path` | Watch for the content-free completion request |
| `messagebox-onboarding-complete.service` | Validate setup and perform the fixed runtime handoff |
| `messagebox-onboarding-voice.path` | Watch for the private fixed voice-proof request |
| `messagebox-onboarding-voice-gate.service` | Validate the request and default before activating hardware |
| `messagebox-onboarding-voice.target` | Run sync, polling, and the guided onboarding button without the normal runtime target |

During the voice proof, Comitup continues to own connectivity and the
onboarding portal while the shared sync and poller services run. The normal
`messagebox.target`, button, dashboard, and NFC services remain conflicted and
inactive.

The NFC onboarding worker runs as `messagebox`, owns I2C and tone playback, and
offers only a group-restricted Unix socket to the isolated web portal. A read
tag UID is held privately for at most two minutes while the caregiver chooses
an opaque recipient token. Browser responses contain labels, kinds, default
state, counts, and progress only. Assignment reuses the contact store's atomic
one-tag/one-recipient transaction; tags are never written.

Skip or Done creates a fixed, content-free completion request. The root gate
validates the completed recipient state and default, enables button/sync/poller,
enables NFC only when mappings exist, keeps the technical dashboard disabled,
removes the onboarding gate, and starts `messagebox.target`. A failed handoff
restores onboarding instead of leaving both modes partially active.

`messagebox-wifi-reset.service` is a one-shot boot check for the physical Wi-Fi
reset gesture. The onboarding portal accesses Comitup through a restricted
D-Bus policy. NetworkManager and Avahi provide network management and local name
resolution.

WhatsApp pairing, recipient identities, message IDs, tag identifiers, and voice-proof
correlation remain behind a group-restricted Unix socket owned by the private
worker. The browser receives only opaque recipient tokens, labels, kinds, and
content-free progress booleans. Person labels are international phone numbers;
group labels use their WhatsApp names. Exact JIDs, message IDs, and tag UIDs never cross
the worker boundary.
Same-origin manual-number mutations accept a strict international number. The
private worker converts it to the exact direct-chat identity and persists it in
the same contact allow-list. A manual first choice starts the normal voice
proof, while later manual additions do not replace the default unless the user
explicitly chooses **Make default** in the completed recipient manager. The
authoritative contact-store change takes effect immediately for no-card routing;
a valid mapped NFC card still overrides it.
