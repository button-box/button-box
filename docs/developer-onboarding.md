# Developer onboarding

## Consumer and developer boundaries

Consumer onboarding verifies Wi-Fi and WhatsApp, selects an initial default
recipient, proves one received-and-replied voice exchange, and then exposes a
recipient manager where the default can be changed, optionally pairs NFC tags,
and then activates the normal runtime. Zero, partial, or full tag coverage is
valid.
During the proof it starts only the scoped onboarding sync, poller, and guided
button target; the normal `messagebox.target` remains gated. Consumer completion
enables button, sync, and poller, enables NFC only when mappings exist, and
leaves the technical dashboard off.

`messagebox-dev-onboard` is an independent prototype workflow. It pairs
WhatsApp, configures contacts and the dashboard, tests hardware, and can start
runtime services; it does not configure Wi-Fi. Development devices may use a
valid lowercase ID instead of the numbered shipping hostname defined in
[Installation](installation.md).

## Internet access for a fresh card

A fresh Pi needs temporary Wi-Fi, routed Ethernet, or macOS Internet Sharing
during setup. For a direct cable from a Mac, open **System Settings > General >
Sharing > Internet Sharing**, share Wi-Fi to the USB Ethernet adapter, and turn
sharing on. Reconnect the cable or reboot the Pi to request a DHCP lease.

Provision from the repository root on the development computer:

```sh
./scripts/provision.sh admin@message-box-001.local
```

Provisioning refuses to connect to the Pi if any required prompt is absent.
This keeps a clean card from reaching recipient onboarding with an unusable
button service. Pass `--guided-prompts DIR` to test an alternate licensed set.

Use the Pi's `.local` hostname instead of assuming a fixed address. Turn
Internet Sharing off before testing consumer Wi-Fi onboarding.

## Reprovision a test box

On a disposable test box reachable over SSH, run:

```sh
./scripts/dev/reprovision.sh admin@message-box-001.local
```

The helper requires confirmation. It preserves the operating system, packages,
users, NetworkManager profiles, contacts, incoming queue, and hardware
configuration. It deletes generated onboarding credentials and state, WhatsApp
authentication, and pending outbound recordings so they cannot be sent through
a newly paired account. It is not a clean-card installation test.

## Reset consumer Wi-Fi

Follow [Installation](installation.md) for first-time initialization and
manufacturer handoff. To return an initialized test Pi to onboarding mode, run:

```sh
sudo messageboxctl reset-wifi
```

`reset-wifi` deletes every saved infrastructure Wi-Fi profile, stops runtime
services, resets onboarding state, enables onboarding for future boots, and
starts the setup hotspot. It does not delete contacts or queued incoming
messages.

## Standalone developer flow

Run locally as a non-root sudo-capable administrator:

```sh
messagebox-dev-onboard
```

Or run it remotely:

```sh
ssh -t admin@message-box-001.local messagebox-dev-onboard
```

The flow stops Message Box services before configuration and testing. If it
exits early, they may remain stopped. It shows synced chats and their exact JIDs.
To list them again later, run:

```sh
sudo -u messagebox -H env \
  WACLI_STORE_DIR=/var/lib/messagebox/wacli \
  WACLI_SYNC_MAX_DB_SIZE=2GB \
  /usr/local/bin/wacli --read-only chats list --limit 200
```

Use the exact `CHAT_JID` shown. Group JIDs look like `123456789@g.us`;
direct-chat JIDs look like `15551234567@s.whatsapp.net`. Never infer a JID from
a display name or phone number.

## Contacts and routing

These examples use synthetic JIDs:

```sh
messagebox-contact add "Family" 123456789@g.us
messagebox-contact enroll 123456789@g.us
messagebox-contact list
messagebox-contact remove 123456789@g.us
```

`add` arms enrollment for the first NFC card for five minutes. Both
`messagebox-nfc.service` and `messagebox-button.service` must be active, with
`MSGBOX_NFC_DETECTION_BEEP` enabled. Use `--no-card` only when deliberately
adding a contact without a card:

```sh
messagebox-contact add "Direct example" 15551234567@s.whatsapp.net --no-card
```

The contact store has an explicit default recipient. With no valid card
selection, standalone recordings route to that default. A recognized card can
override it within the selection window (30 seconds by default). A missing
default or an unknown or invalid card state blocks rather than guessing or
rerouting. Replies to incoming messages always use the exact originating chat.

## Service control

`messageboxctl` wraps common systemd and journal operations. Run
`messageboxctl --help` for the full command list. Common commands are:

```sh
messageboxctl services
messageboxctl status
messageboxctl logs poller
messageboxctl logs onboarding
sudo messageboxctl restart
sudo messageboxctl enable button sync poller nfc
sudo messageboxctl disable dashboard
```

Service names are `button`, `sync`, `poller`, `dashboard`, and `nfc`. Consumer
voice proof uses `messagebox-onboarding-voice.target` and
`messagebox-onboarding-button.service`; they are not operator-selectable runtime
services. Tag pairing uses `messagebox-onboarding-nfc.service`, not the normal
runtime NFC daemon.

## Hardware and dashboard

The hardware test checks network access, speaker, microphone, LED, button,
PN532/card detection, and WhatsApp authentication. It does not send a message or
display NFC IDs. Run it directly only in an interactive terminal as the
`messagebox` service user:

```sh
sudo -u messagebox -H /opt/messagebox/dev/hardware-test.sh
```

The dashboard has no login. Prefer binding `MSGBOX_DASH_BIND` to the Pi's
Tailscale IPv4 address. Binding to `0.0.0.0` exposes it on every connected
network; use that setting only on a trusted network. An empty bind keeps the
dashboard unavailable.
