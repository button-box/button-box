# Installation candidate

This procedure is not release-approved. Use only a disposable Raspberry Pi 4
and test microSD card; never overwrite a working household device.

1. Install Raspberry Pi OS Lite 64-bit based on Debian 13.
2. Clone the candidate onto the Pi as a normal sudo-capable administrator.
3. Run `./scripts/setup.sh`. The script installs files and dependencies but
   does not copy credentials, pair WhatsApp, or start Message Box services.
4. Configure protected Wi-Fi onboarding with
   `sudo messagebox-configure-wifi`.
5. Record the generated setup credentials, then run
   `sudo messageboxctl reset-wifi`.
6. Complete Wi-Fi and WhatsApp pairing in the local portal.

Current gap: recipient selection and NFC enrollment are not yet a complete
self-service continuation of the web flow. Publication remains blocked until
the complete clean-card journey and recovery cases pass physically.
