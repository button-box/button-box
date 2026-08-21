"""Fixed filesystem paths owned by Wi-Fi and WhatsApp onboarding."""

from dataclasses import dataclass
from pathlib import Path


ONBOARDING_CONFIG_DIR = Path("/etc/messagebox-onboarding")
ONBOARDING_CONFIG_PATH = ONBOARDING_CONFIG_DIR / "config.json"
ONBOARDING_CONFIGURED_PATH = ONBOARDING_CONFIG_DIR / "configured"
ONBOARDING_ENABLED_PATH = ONBOARDING_CONFIG_DIR / "enabled"

ONBOARDING_STATE_DIR = Path("/var/lib/messagebox-onboarding")
ONBOARDING_STATE_PATH = ONBOARDING_STATE_DIR / "state.json"
ONBOARDING_OBSOLETE_SESSION_KEY_PATH = ONBOARDING_STATE_DIR / "session.key"

COMITUP_CONFIG_PATH = Path("/etc/comitup.conf")
COMITUP_TEMPLATE_PATH = Path("/usr/share/messagebox/onboarding/comitup.conf.template")
COMITUP_BOOT_CONFIG_PATH = Path("/boot/comitup.conf")
COMITUP_FIRMWARE_CONFIG_PATH = Path("/boot/firmware/comitup.conf")
RUNTIME_DIR = Path("/run")
INITIALIZER_LOCK_PATH = Path("/run/lock/messagebox-init-wifi-onboarding.lock")

WHATSAPP_SOCKET_PATH = Path("/run/messagebox-whatsapp-pairing/worker.sock")
WHATSAPP_PAIRING_ROOT = Path("/var/lib/messagebox/whatsapp-pairing")
WHATSAPP_LIVE_STORE = Path("/var/lib/messagebox/wacli")
WHATSAPP_CANDIDATES_PATH = WHATSAPP_LIVE_STORE / "onboarding-candidates.json"
MESSAGEBOX_HOME = Path("/var/lib/messagebox")
WACLI_PATH = Path("/usr/local/bin/wacli")


@dataclass(frozen=True)
class InitializerPaths:
    """Complete initializer path set, fixed in production and injectable in tests."""

    config_dir: Path = ONBOARDING_CONFIG_DIR
    config: Path = ONBOARDING_CONFIG_PATH
    configured: Path = ONBOARDING_CONFIGURED_PATH
    enabled: Path = ONBOARDING_ENABLED_PATH
    state_dir: Path = ONBOARDING_STATE_DIR
    state: Path = ONBOARDING_STATE_PATH
    obsolete_session_key: Path = ONBOARDING_OBSOLETE_SESSION_KEY_PATH
    template: Path = COMITUP_TEMPLATE_PATH
    comitup_config: Path = COMITUP_CONFIG_PATH
    comitup_boot_config: Path = COMITUP_BOOT_CONFIG_PATH
    comitup_firmware_config: Path = COMITUP_FIRMWARE_CONFIG_PATH
    run_dir: Path = RUNTIME_DIR
    lock: Path = INITIALIZER_LOCK_PATH


INITIALIZER_PATHS = InitializerPaths()
