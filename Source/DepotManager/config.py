import json
import logging
import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# CONSTANTS & PATH RESOLUTION
# ---------------------------------------------------------------------------
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))

if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).parent
else:
    APP_DIR = Path(__file__).resolve().parent

SETTINGS_FILE = str(APP_DIR / "settings.json")
KEYS_FILE = str(APP_DIR / "keys.txt")
LOG_FILE = str(APP_DIR / "depot_manager.log")

APP_VERSION = "1.3.0"

_BANNER = (
    "  ____                  _       __  __                                    \n"
    " |  _ \\  ___ _ __   ___| |_    |  \\/  | __ _ _ __   __ _  __ _  ___ _ __ \n"
    " | | | |/ _ \\ '_ \\ / _ \\ __|   | |\\/| |/ _` | '_ \\ / _` |/ _` |/ _ \\ '__|\n"
    " | |_| |  __/ |_) | (_) | |_   | |  | | (_| | | | | (_| | (_| |  __/ |   \n"
    " |____/ \\___| .__/ \\___/ \\__|  |_|  |_|\\__,_|_| |_|\\__,_|\\__, |\\___|_|  \n"
    "            |_|                                          |___/         \n"
    f"                       ~ HighSeas Edition  v{APP_VERSION} ~\n"
    "\n"
)

DEFAULT_SETTINGS: dict = {
    "api_base_url_morrenus": "https://hubcapmanifest.com/api/v1",
    "api_base_url_ryuu": "https://generator.ryuu.lol/secure_download",
    "exe_name": "../DepotDownloaderMod/DepotDownloaderMod.exe",
    "api_key_morrenus": "",
    "api_key_ryuu": "",
    "selected_source": "morrenus",
    "max_concurrent_downloads": 1,
    "request_timeout": 30,
}

SOURCES: dict = {
    "morrenus": {
        "label": "Morrenus's API",
        "key_field": "api_key_morrenus",
    },
    "ryuu": {
        "label": "Ryuu's API",
        "key_field": "api_key_ryuu",
    },
}

APPID_MIN = 1
APPID_MAX = 2_000_000_000

# Regex patterns for Lua and Manifest files
_RE_LUA_ADDAPPID = re.compile(r'addappid\((\d+),\s*\d+,\s*"([A-Za-z0-9]+)"\)')
_RE_LUA_TABLE = re.compile(r'\[(\d+)\]\s*=\s*"([A-Za-z0-9]+)"')
_RE_MANIFEST = re.compile(r"^(\d+)_(\d+)\.manifest$")

logger = logging.getLogger("DepotManager.Config")


# ---------------------------------------------------------------------------
# SETTINGS FUNCTIONS
# ---------------------------------------------------------------------------
def load_settings() -> dict:
    """Loads settings from settings.json or returns default settings if not exists/corrupted."""
    settings = DEFAULT_SETTINGS.copy()
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            # Legacy fields migrations
            if "api_key" in loaded and not loaded.get("api_key_morrenus"):
                loaded["api_key_morrenus"] = loaded.pop("api_key")
            else:
                loaded.pop("api_key", None)
            if "api_base_url" in loaded and not loaded.get("api_base_url_morrenus"):
                loaded["api_base_url_morrenus"] = loaded.pop("api_base_url")
            else:
                loaded.pop("api_base_url", None)
            # Auto-migrate exe_name if it points to the old default relative path
            if loaded.get("exe_name") == "DepotDownloaderMod.exe":
                loaded["exe_name"] = "../DepotDownloaderMod/DepotDownloaderMod.exe"

            settings.update(loaded)
            logger.debug("Settings loaded from %s.", SETTINGS_FILE)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Cannot read settings file: %s. Using defaults.", exc)
    return settings


def save_settings(settings: dict) -> None:
    """Saves settings dict to settings.json."""
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=4)
        logger.debug("Settings saved to %s.", SETTINGS_FILE)
    except OSError as exc:
        logger.error("Cannot save settings: %s", exc)
        raise exc
