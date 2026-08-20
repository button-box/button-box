# Privacy boundary

## Repository-safe data

- Source code and configuration schemas
- Synthetic tests and examples
- Generic documentation
- Checksums and dependency metadata

## Device-private data

- Wi-Fi credentials and generated setup passwords
- WhatsApp authentication databases, phone numbers, JIDs, and pairing codes
- Contact and listener profiles
- NFC card identifiers and routing assignments
- Incoming and outgoing audio, message metadata, and queue state
- Private dashboard addresses, webhook URLs, and webhook secrets

Device-private data belongs under restrictive runtime paths such as
`/etc/messagebox` and `/var/lib/messagebox`. Diagnostics must redact it. Do not
attach raw logs, screenshots, databases, or recordings to public issues.
