"""Fixed filesystem layout shared by Button Box services."""

from pathlib import Path


APP_DIR = Path("/opt/messagebox")
CONFIG_DIR = Path("/etc/messagebox")
DATA_DIR = Path("/var/lib/messagebox")
RUNTIME_DIR = Path("/run/messagebox")

ASSET_DIR = DATA_DIR / "assets"
QUEUE_DIR = DATA_DIR / "queue"
OUTBOX_DIR = DATA_DIR / "outbox"
STATE_DIR = DATA_DIR / "state"
SETTINGS_DIR = Path("/var/lib/messagebox-settings")
SETTINGS_FILE = SETTINGS_DIR / "settings.json"

CONTACTS_FILE = STATE_DIR / "contacts.json"
NFC_SELECTION_FILE = RUNTIME_DIR / "nfc-selection.json"
NFC_ENROLLMENT_FILE = RUNTIME_DIR / "nfc-enrollment.json"
NFC_ANNOUNCEMENT_FILE = RUNTIME_DIR / "nfc-announcement.json"
NFC_HEALTH_FILE = RUNTIME_DIR / "nfc-health"
