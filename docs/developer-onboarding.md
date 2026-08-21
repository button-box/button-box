# Developer onboarding

## Consumer and developer boundaries

The consumer web onboarding flow ends after it verifies Wi-Fi and WhatsApp
readiness. It does not select recipients, enroll NFC cards, or start the Message
Box runtime.

`messagebox-dev-onboard` is a separate standalone dev flow for an
installed prototype. It pairs WhatsApp directly, configures a contact and the
dashboard, runs hardware checks, and can enable and start runtime services. It
does not configure Wi-Fi and is not a continuation of consumer web onboarding.

Shipped boxes use hostnames such as `message-box-001`, where the zero-padded
number matches the physical label and device inventory. Consumer onboarding
shows that number as the device ID. Development devices may use another valid
lowercase ID when a shipping number has not been assigned.

## Internet access for a fresh card

A fresh Pi needs temporary Wi-Fi, routed Ethernet, or macOS Internet Sharing
while setup installs packages. For a direct cable from a Mac, open **System
Settings > General > Sharing > Internet Sharing**, share the Mac's Wi-Fi
connection to the USB Ethernet adapter, and turn Internet Sharing on. Reconnect
the Ethernet cable or reboot the Pi so it requests a DHCP lease.

Confirm that SSH works using mDNS, then exit that session:

```sh
ssh admin@message-box-001.local
exit
```

From the repository root on the Mac, provision the Pi:

```sh
./scripts/provision.sh admin@message-box-001.local
```

macOS assigns the Ethernet address dynamically, so use the Pi's `.local`
hostname rather than assuming a fixed address. After package installation, turn
Internet Sharing off before testing consumer Wi-Fi onboarding.

## Reprovision a test box

To clear consumer onboarding and WhatsApp test credentials and redeploy the
current tree, connect over routed Ethernet or macOS Internet Sharing and run:

```sh
./scripts/dev/reprovision.sh admin@message-box-001.local
```

Use the `.local` hostname after the Pi receives a DHCP lease from a router or
macOS Internet Sharing.

The helper displays its exact deletion scope and requires confirmation. It
preserves the operating system, packages, users, current network profile,
contacts, incoming queue, and hardware configuration. It clears pending
outbound recordings with the WhatsApp identity so they cannot be sent through
a newly paired account. It is not a substitute for testing installation on a
freshly imaged card.

## Consumer Wi-Fi initialization

On a newly installed Pi, generate and record its Wi-Fi hotspot password and
setup URL:

```sh
sudo messagebox-init-wifi-onboarding
```

This initializes consumer onboarding but does not start it. For a consumer-bound
box, the manufacturer records the displayed device ID, hotspot password, and
setup URL with the matching box number. Print the box number, hotspot name,
password, and URL as an insert packaged with the box, then arm onboarding:

```sh
sudo messageboxctl reset-wifi
```

`reset-wifi` erases configured network state, enables onboarding for future
boots, and starts the setup hotspot immediately. The manufacturer may verify
that the hotspot appears, then runs `sudo shutdown now` and hands off the powered
down box. The manufacturer does not complete the browser flow. The recipient
powers on the box and opens the printed setup URL. After entering home Wi-Fi,
the setup hotspot disappears; the recipient reconnects the phone to home Wi-Fi,
waits up to two minutes, and opens the same URL, such as
`http://message-box-001.local/`, to continue with WhatsApp pairing.

## Standalone developer flow

Run the installed developer flow as a sudo-capable administrator, not as root:

```sh
messagebox-dev-onboard
```

From the development computer, the equivalent remote shortcut is:

```sh
ssh -t admin@message-box-001.local messagebox-dev-onboard
```

The flow uses the `messagebox` service account for WhatsApp and contacts. It
shows synced chats and their exact JIDs with the current client command
equivalent to:

```sh
sudo -u messagebox -H env \
  WACLI_STORE_DIR=/var/lib/messagebox/wacli \
  WACLI_SYNC_MAX_DB_SIZE=2GB \
  /usr/local/bin/wacli --read-only chats list --limit 200
```

Copy the exact `CHAT_JID` shown by that output. Group JIDs look like
`123456789@g.us`; direct-chat JIDs look like
`15551234567@s.whatsapp.net`. Do not derive or guess a JID from a display name
or phone number.

## Contacts and routing

The installed `messagebox-contact` command supports `add`, `enroll`, `list`,
and `remove`. These examples use synthetic JIDs:

```sh
messagebox-contact add "Family" 123456789@g.us
messagebox-contact enroll 123456789@g.us
messagebox-contact list
messagebox-contact remove 123456789@g.us
```

`add` normally arms enrollment for the first NFC card for five minutes. Both
`messagebox-nfc.service` and `messagebox-button.service` must be active, and
`MSGBOX_NFC_DETECTION_BEEP` must be enabled, before card enrollment. Use
`--no-card` only when deliberately adding a contact without enrolling a card:

```sh
messagebox-contact add "Direct example" 15551234567@s.whatsapp.net --no-card
```

With exactly one contact, standalone recordings route to that contact
automatically and no active card selection is required. With multiple contacts,
present an enrolled NFC card before recording; the recent valid card selection
determines the recipient. If no valid selection exists, recording is blocked
rather than guessed or rerouted.

## Hardware and dashboard

The guided hardware test is an internal installed helper:

```sh
/opt/messagebox/dev/hardware-test.sh
```

It tests network access, speaker, microphone, LED, button, PN532/card detection,
and WhatsApp authentication without sending a message or displaying NFC IDs.
`messagebox-dev-onboard` invokes it as `messagebox`, which provides the same
permissions and private state used by runtime services. Run it directly only in
an interactive terminal as that service user:

```sh
sudo -u messagebox -H /opt/messagebox/dev/hardware-test.sh
```

The dashboard has no login. Prefer binding `MSGBOX_DASH_BIND` to the Pi's
Tailscale IPv4 address so it is reachable only through that private network.
Binding to `0.0.0.0` exposes it on every connected network, including Wi-Fi and
Ethernet; use that setting only on a trusted network with the exposure
understood. Leaving the bind address empty keeps the dashboard unavailable.
