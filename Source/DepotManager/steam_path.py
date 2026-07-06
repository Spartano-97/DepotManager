"""Steam installation discovery and library resolution (Windows runtime).

This module is Windows-oriented (uses ``winreg`` to read the Steam path from the
registry) but every function degrades gracefully when run on non-Windows hosts
or when the registry is unavailable: callers get ``None`` / empty lists and can
fall back to a manual path stored in ``settings.json``.

All network access (app name lookup) is async and reuses the existing
``aiohttp.ClientSession`` owned by the GUI.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys
from pathlib import Path
from typing import List, Optional

try:  # Windows-only stdlib module; absent on WSL/Linux.
    import winreg  # type: ignore
except ImportError:  # pragma: no cover - exercised only on non-Windows hosts.
    winreg = None  # type: ignore

try:
    import vdf as _vdf
except ImportError:  # pragma: no cover - vdf is a hard dep on Windows runtime.
    _vdf = None  # type: ignore

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore

logger = logging.getLogger("DepotManager.SteamPath")

# Steam registry key (HKCU per-user, HKLM all-users fallback).
_STEAM_REG_KEY = r"Software\Valve\Steam"
_STEAM_REG_VALUE = "SteamPath"

# Fallback paths when the registry is unreadable.
_DEFAULT_STEAM_PATHS = (
    Path(r"C:\Program Files (x86)\Steam"),
    Path(r"C:\Program Files\Steam"),
)

# Steam Store endpoint for AppID -> name resolution.
_STORE_APPDETAILS = "https://store.steampowered.com/api/appdetails"


def is_windows() -> bool:
    """True when running on a Windows interpreter (not WSL/Linux)."""
    return sys.platform == "win32"


def _read_registry_steam_path() -> Optional[Path]:
    """Read Steam install path from HKCU then HKLM. Returns None if unreadable."""
    if winreg is None:
        return None
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, _STEAM_REG_KEY) as key:
                value, _ = winreg.QueryValueEx(key, _STEAM_REG_VALUE)
            if value:
                path = Path(value)
                if path.is_dir():
                    return path
        except OSError:
            continue
    return None


def get_steam_path(settings: dict) -> Optional[Path]:
    """Resolve the Steam installation directory.

    Order of precedence:
      1. ``settings["steam_path"]`` if it points to a valid directory
         (user override persisted across sessions).
      2. Windows registry (HKCU then HKLM).
      3. Hard-coded default paths.

    Returns None when Steam cannot be located.
    """
    configured = settings.get("steam_path", "").strip()
    if configured:
        path = Path(configured)
        if path.is_dir():
            return path
        logger.warning("Configured steam_path does not exist: %s", path)

    registry_path = _read_registry_steam_path()
    if registry_path is not None:
        return registry_path

    for fallback in _DEFAULT_STEAM_PATHS:
        if fallback.is_dir():
            return fallback

    return None


def set_steam_path(settings: dict, path: Path) -> None:
    """Persist a user-chosen Steam path into settings (caller must save_settings)."""
    settings["steam_path"] = str(path)


def get_steam_libraries(steam_path: Path) -> List[Path]:
    """Return all Steam library folders, including the root one.

    Parses ``<steam>/steamapps/libraryfolders.vdf``. The Steam install dir is
    always the first entry. Duplicates and non-existent paths are filtered.
    """
    libraries: List[Path] = []
    if steam_path.is_dir():
        libraries.append(steam_path / "steamapps")

    vdf_path = steam_path / "steamapps" / "libraryfolders.vdf"
    if not vdf_path.is_file():
        logger.debug("libraryfolders.vdf not found at %s", vdf_path)
        return libraries

    if _vdf is None:
        logger.warning("vdf module unavailable; cannot parse libraryfolders.vdf")
        return libraries

    try:
        with open(vdf_path, "r", encoding="utf-8", errors="replace") as f:
            data = _vdf.load(f)
    except (OSError, ValueError) as exc:
        logger.warning("Cannot parse %s: %s", vdf_path, exc)
        return libraries

    # Structure: {"libraryfolders": {"0": {"path": "..."}, "1": {...}, ...}}
    root = data.get("libraryfolders", {}) if isinstance(data, dict) else {}
    for entry in root.values():
        if not isinstance(entry, dict):
            continue
        raw = entry.get("path")
        if not raw:
            continue
        lib_apps = Path(raw) / "steamapps"
        if lib_apps.is_dir() and lib_apps not in libraries:
            libraries.append(lib_apps)
    return libraries


def find_acf_for_app(libraries: List[Path], appid: str) -> Optional[Path]:
    """Locate ``appmanifest_<appid>.acf`` across all libraries. None if absent."""
    target = f"appmanifest_{appid}.acf"
    for lib in libraries:
        candidate = lib / target
        if candidate.is_file():
            return candidate
    return None


def pick_library(
    libraries: List[Path],
    appid: Optional[str] = None,
    required_bytes: int = 0,
) -> Optional[Path]:
    """Choose the library to install a game into.

    If ``appid`` is given and an ACF already exists for it in some library, that
    library is reused (so updates land in the same place). Otherwise the first
    library with enough free space is returned. ``required_bytes=0`` short-
    circuits the space check.
    """
    if not libraries:
        return None

    if appid:
        existing = find_acf_for_app(libraries, appid)
        if existing is not None:
            return existing.parent  # library steamapps dir

    if required_bytes <= 0:
        return libraries[0]

    for lib in libraries:
        try:
            usage = shutil.disk_usage(lib)
        except OSError:
            continue
        if usage.free >= required_bytes:
            return lib
    return libraries[0]


def library_label(library: Path) -> str:
    """Human-readable label for a library, including free space in GB.

    Example: ``"D:\\SteamLibrary\\steamapps  (1823 GB free)"``.
    On disk-usage failure the free-space suffix is omitted.
    """
    try:
        usage = shutil.disk_usage(library)
        free_gb = usage.free // (1 << 30)
        return f"{library}  ({free_gb} GB free)"
    except OSError:
        return str(library)


def pick_library_default(
    libraries: List[Path], appid: Optional[str] = None
) -> Optional[Path]:
    """Choose a default library for the Add Game dialog.

    Selection order:
      1. If an ACF for ``appid`` already exists in some library, reuse that
         (used by Update Selected to keep the game in its current location).
      2. Otherwise, the library with the most free space (heuristic for
         "where the user likely wants a new big game to land").

    Returns None only when ``libraries`` is empty.
    """
    if not libraries:
        return None

    if appid:
        existing = find_acf_for_app(libraries, appid)
        if existing is not None:
            return existing.parent

    best: Optional[Path] = None
    best_free = -1
    for lib in libraries:
        try:
            usage = shutil.disk_usage(lib)
        except OSError:
            continue
        if usage.free > best_free:
            best_free = usage.free
            best = lib
    return best if best is not None else libraries[0]


async def get_app_name(
    session: "aiohttp.ClientSession", appid: str, timeout: int = 15
) -> str:
    """Fetch the store name for an AppID. Falls back to str(appid) on any error."""
    if aiohttp is None or session is None:
        return appid
    url = f"{_STORE_APPDETAILS}?appids={appid}&filters=basic&l=en"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            if r.status != 200:
                logger.debug("appdetails HTTP %s for %s", r.status, appid)
                return appid
            payload = await r.json(content_type=None)
        entry = payload.get(appid, {}) if isinstance(payload, dict) else {}
        if entry.get("success") and isinstance(entry.get("data"), dict):
            name = entry["data"].get("name")
            if name:
                return str(name)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        logger.debug("appdetails lookup failed for %s: %s", appid, exc)
    return appid


def sanitize_installdir(name: str) -> str:
    """Make a string safe to use as a Steam ``installdir`` folder name.

    Uses ``pathvalidate`` when available (handles Windows reserved names like
    CON/PRN/NUL/COM1-9/LPT1-9 and illegal characters); falls back to a small
    regex-based sanitizer otherwise.
    """
    if not name:
        return "game"
    try:
        from pathvalidate import sanitize_filename

        cleaned = sanitize_filename(name, platform="windows").strip()
        # sanitize_filename can return empty for inputs made only of illegal
        # chars; guard against that.
        return cleaned or "game"
    except ImportError:
        import re

        cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().rstrip(". ")
        return cleaned or "game"
