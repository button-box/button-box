# Developer onboarding

## Consumer and developer boundaries

Consumer onboarding ends at verified Wi-Fi and WhatsApp readiness. It does not
select recipients, enroll NFC cards, or start the runtime.

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

With one contact, standalone recordings route there automatically. With multiple
contacts, present an enrolled card within the selection window (30 seconds by
default). Without a valid selection, a standalone recording is blocked rather
than guessed or rerouted.

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

Service names are `button`, `sync`, `poller`, `dashboard`, and `nfc`.

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
