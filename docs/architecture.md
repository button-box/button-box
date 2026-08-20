# Architecture

Message Box runs as a set of least-privilege Linux services on a Raspberry Pi.

1. Comitup and the local onboarding portal establish Wi-Fi.
2. The home-mode portal starts an isolated WhatsApp pairing worker.
3. Contact and NFC tools store approved routing state under
   `/var/lib/messagebox`, outside the repository.
4. The poller downloads eligible inbound voice messages to a durable queue.
5. The button player records or plays audio and sends only to the selected,
   approved destination.
6. systemd owns startup, shutdown, service isolation, and recovery.

The repository contains code and safe examples only. Authentication databases,
contacts, NFC identifiers, audio messages, Wi-Fi credentials, and live device
state are runtime data and must never be copied into Git.
