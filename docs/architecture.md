# Architecture

Message Box runs as separate systemd services with constrained users and
permissions.

1. Comitup manages Wi-Fi through NetworkManager. The onboarding portal uses its
   restricted D-Bus API to scan and connect.
2. After Wi-Fi setup, systemd runs the home portal and a separate WhatsApp
   pairing worker.
3. Contact and NFC tools maintain approved routing state under
   `/var/lib/messagebox`.
4. The poller queues voice messages from configured contacts.
5. The button service records or plays audio and sends only to the selected
   approved contact.
6. systemd manages service lifecycle and restart behavior.

Private runtime state must remain outside Git in permission-restricted device
paths. See [Privacy boundary](privacy.md).
