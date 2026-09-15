# Implemented household dashboard

Implemented 16 September 2026 (Europe/Lisbon), based on `0845e57` on main.
The archived [prototype](prototype.html) remains a design reference. The actual
dashboard uses the existing shared HTML, CSS and JavaScript, not the prototype's
mock state machine.

## Delivered

- Warm-paper button.box palette, outlined cards and buttons, local font stacks.
- Five existing routes: sticky desktop rail and fixed mobile bottom navigation
  with safe-area spacing. Nested setup/management views retain their parent tab.
- Home connection/setup status, explicit attention notice, and **Play the ringtone**
  with request-only feedback. A ringtone does not verify messaging.
- Support footer with Box ID, Copy/manual-copy fallback, and the existing owner
  community link. No identifier in the prominent header.
- Optional, read-only inventory identity for both servers. No sample ID is installed.
  Missing/invalid identity displays **Not assigned**. See
  [assignment and recovery](../../dashboard-identity.md).
- Existing pairing, NFC feedback, recipient management, settings revisions,
  queued audio and recovery actions remain in their original handlers.

## Verification

- `make check`: 320 Python tests and 16 JavaScript tests, plus Python/shell syntax,
  Ruff, ShellCheck and frontend accessibility lint.
- Added identity file validation, both API state contracts, missing/invalid ID UI,
  copy success/failure, readiness, request-only ringtone semantics, and
  focus/scroll regression tests. Existing secure/plain-HTTP clipboard and NFC
  generation/cancellation tests pass.
- Browser: all five main routes at 320px had document width exactly 320px, one
  visible view and the matching current nav item.
- Browser: settings loaded and a synthetic save retained the selected signal;
  route changes returned to the heading; support Copy pasted `BOX-42` into a local
  unsaved test field; incomplete setup showed attention and disabled missing-ID Copy.
- Browser: onboarding Wi-Fi screen rendered from the shared assets with a synthetic
  network and no live connection attempt. Desktop and mobile screenshots reviewed.
- No console warnings/errors on the final runtime preview.

### Reproduce the local preview

```sh
python3 scripts/dev/dashboard-preview.py
python3 scripts/dev/dashboard-preview.py --port 8791 --state attention
python3 scripts/dev/dashboard-preview.py --port 8768 --state setup
```

Open the printed loopback URL. The helper uses **synthetic sample data only**:
`BOX-42`, `Example home`, sample counts/timestamps, and an in-memory settings mock.
It never connects to a Pi, account or inventory system. Mutations are not backend
integration evidence. Stop the process to discard mock settings. Ports may be
changed if occupied.

### Actual-app screenshots (synthetic data)

| Screen | Screenshot |
| --- | --- |
| Home, 390 × 844 | [Mobile Home](screenshots/implemented-mobile-home.png) |
| Support footer, 390 × 844 | [Footer](screenshots/implemented-mobile-footer.png) |
| Settings, 390 × 844 | [Settings](screenshots/implemented-mobile-settings.png) |
| Setup, 390 × 844 | [Setup](screenshots/implemented-mobile-setup.png) |
| Activity, 390 × 844 | [Activity](screenshots/implemented-mobile-activity.png) |
| Advanced, 390 × 844 | [Advanced](screenshots/implemented-mobile-advanced.png) |
| Needs attention, 390 × 844 | [Attention](screenshots/implemented-mobile-attention.png) |
| Onboarding Wi-Fi, 390 × 844 | [Wi-Fi](screenshots/implemented-mobile-wifi.png) |
| Home, 1280 × 900 | [Desktop Home](screenshots/implemented-desktop-home.png) |

## Remaining gates

No live device was accessed, configured, deployed or rung for this work. Review,
merge and deployment remain separate approvals. After an authorized test install:

1. Reconcile/install the real unit's inventory ID; verify its footer and Copy on
   setup and runtime interfaces, including after reboot/reset. Reimage restoration
   is manual, not implemented automatically.
2. Test actual iPhone/Safari and captive-portal layouts, clipboard behavior and
   narrow/large-text rendering on the household network.
3. Verify ringtone audio/lamp separately, then receive → play → record → review →
   send → intended-recipient delivery, NFC pairing and settings persistence.

Local tests and screenshots do not constitute physical or household acceptance.
