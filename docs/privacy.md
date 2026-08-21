# Privacy boundary

## Repository-safe data

- Source code and configuration schemas
- Synthetic tests and examples
- Documentation containing only synthetic examples
- Checksums and dependency metadata

## Device-private data

- Wi-Fi credentials and generated setup passwords
- WhatsApp authentication databases, real phone numbers, JIDs, and pairing codes
- Contact and listener profiles
- NFC card identifiers and routing assignments
- Incoming and outgoing audio, message metadata, and queue state
- Private dashboard addresses, webhook URLs, and webhook secrets

Keep device-private data outside the repository in permission-restricted runtime
paths such as `/etc/messagebox`, `/var/lib/messagebox`,
`/etc/messagebox-onboarding`, and `/var/lib/messagebox-onboarding`. Treat raw
logs, event files, and screenshots as device-private. Redact all device-private
data from diagnostics before sharing. Never attach authentication databases or
recordings to public issues.
