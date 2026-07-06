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

APP_VERSION = "2.0.0"

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
    # --- LumaCore integration ---
    "steam_path": "",
    "lumacore_installed_version": "",
    "lumacore_last_check": 0,
    "lumacore_variant": "release",
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

# Steam's fake ownership id; filtered out from keys/ACF/depotcache.
APPID_OWNERSHIP_FAKE = "1"

# Regex patterns for Lua and Manifest files
_RE_LUA_ADDAPPID = re.compile(r'addappid\((\d+),\s*\d+,\s*"([A-Za-z0-9]+)"\)')
_RE_LUA_TABLE = re.compile(r'\[(\d+)\]\s*=\s*"([A-Za-z0-9]+)"')
_RE_MANIFEST = re.compile(r"^(\d+)_(\d+)\.manifest$")
_RE_LUA_SETMANIFEST = re.compile(r'setManifestid\(\s*(\d+)\s*,\s*"(\d+)"\s*\)')
_RE_LUA_ADDAPPID_OWNERSHIP = re.compile(r"addappid\(\s*(\d+)\s*\)")

# ---------------------------------------------------------------------------
# LUMACORE CONSTANTS
# ---------------------------------------------------------------------------
# LumaCore binaries are downloaded at install time from GitHub releases.
LUMACORE_REPO = "KoriaPolis/LumaCore"
LUMACORE_RELEASE_API = f"https://api.github.com/repos/{LUMACORE_REPO}/releases/latest"

# The four DLLs placed in the Steam root directory. dwmapi.dll + xinput1_4.dll
# are proxy DLLs that load LumaCore.dll at Steam startup.
LC_DLLS = ("dwmapi.dll", "xinput1_4.dll", "LumaCore.dll", "LumaCorePayload.dll")

# Files removed on (re)install / uninstall. Tuple of (subdir, name) relative
# to the Steam root.
LC_RESET_FILES = (
    ("", "dwmapi.dll"),
    ("", "xinput1_4.dll"),
    ("", "LumaCore.dll"),
    ("", "LumaCorePayload.dll"),
    ("bin", "lcoverlay.dll"),
)

# Only proxy DLLs eligible for backup/restore (LumaCore's own DLLs have no
# "original" to preserve).
LC_BACKUP_DLLS = ("dwmapi.dll", "xinput1_4.dll")
LC_BACKUP_DIR = "lumacore_backup"

# Cooldown for "check for update": avoid hammering GitHub API on every UI open.
LUMACORE_CHECK_INTERVAL_SEC = 6 * 60 * 60  # 6 hours

# Per-build pattern TOML mirrors (best-effort prewarm on install). {subdir} is
# "steamclient" or "steamui" or "steamclientipc", {sha} is the lowercase hex
# SHA-256 of the DLL.
LUMACORE_PATTERN_MIRRORS = (
    "https://raw.githubusercontent.com/KoriaPolis/Steam-Auto-PT/pattern/{subdir}/{sha}.toml",
    "https://cdn.jsdelivr.net/gh/KoriaPolis/Steam-Auto-PT@pattern/{subdir}/{sha}.toml",
)
LUMACORE_PATTERN_DIR = "lumacore"  # <steam>/lumacore/pattern/...

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
