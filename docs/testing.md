# Testing

## Repository tests

Run `make check` for Python compilation, shell syntax checks, and the synthetic
unit suite. Run `make scan` separately when Gitleaks is installed.

Repository tests cover routing, onboarding state, credential redaction,
WhatsApp-pairing contracts, NFC behavior, and recovery logic. They do not prove
behavior on a real Pi, phone, network, microphone, speaker, button, or PN532.

## Physical acceptance

A release requires a clean Raspberry Pi 4 and test microSD card. Validate Wi-Fi
success and failure recovery, WhatsApp pairing and interruption, recipient
selection, NFC enrollment/removal/unknown cards, audio input/output, first
send/reply, reboot, power loss, and rollback. Confirm the test did not inherit
any state from a development or household device.
