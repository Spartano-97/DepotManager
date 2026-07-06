"""Per-game management for the LumaCore integration layer.

Each game managed by DepotManager maps to:

  * ``<steam>/config/stplug-in/<appid>.lua``  — ownership + depot keys + manifest
    pins, reloaded by LumaCore at runtime via its directory watcher.
  * ``<steam>/config/config.vdf``             — AES decryption keys injected
    under ``depots.<depot_id>.DecryptionKey``.
  * ``<steam>/depotcache/<depot>_<gid>.manifest`` (+ mirror under
    ``<steam>/config/depotcache/``) — manifest blobs Steam needs to mount
    the depots.
  * ``<library>/steamapps/appmanifest_<appid>.acf`` — ACF written so Steam
    lists the app and can either download it natively (mode ``steam_native``)
    or pick up files already laid down by DepotDownloaderMod (mode
    ``depotdownloader``).

The lua + manifest + keys are produced by the existing API fetch pipeline
(``api_client.APIClient.fetch_manifests`` + ``parser.scan_directory``) and are
passed into ``add_game`` as the already-built ``inventory`` dict.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import (
    _RE_LUA_ADDAPPID,
    _RE_LUA_ADDAPPID_OWNERSHIP,
    _RE_LUA_SETMANIFEST,
    _RE_MANIFEST,
    APP_DIR,
    APPID_OWNERSHIP_FAKE,
)
from .parser import get_lua_file, manifest_gid_from_name
from .steam_path import (
    find_acf_for_app,
    get_app_name,
    get_steam_libraries,
    pick_library,
    pick_library_default,
    sanitize_installdir,
)
from .vdf_io import (
    add_depot_keys,
    read_acf,
    read_acf_depots,
    remove_acf,
    remove_depot_keys,
    write_acf,
)
from .vdf_io import (
    clean_acf_backups as _clean_acf_backups_vdf,
)
from .vdf_io import (
    list_acf_backups as _list_acf_backups_vdf,
)
from .vdf_io import (
    restore_acf_backup as _restore_acf_backup_vdf,
)

logger = logging.getLogger("DepotManager.LumaCoreGames")

STPLUG_IN_SUBDIR = "config/stplug-in"
DEPOTCACHE_SUBDIR = "depotcache"
DEPOTCACHE_CONFIG_SUBDIR = "config/depotcache"
SAVED_LUA_DIR = APP_DIR / "saved_lua"

DownloadMode = str  # "steam_native" | "depotdownloader" | "inject_only"


# ---------------------------------------------------------------------------
# LUA PARSING (for list_installed_games / remove_game)
# ---------------------------------------------------------------------------
def _parse_lua_summary(lua_path: Path) -> Dict:
    """Extract depot/key/manifest info from a stplug-in lua file.

    Returns ``{app_id, depots: [(depot_id, key)], manifest_pins: {depot: gid},
    ownership_ids: [app_id]}``.
    """
    summary: Dict = {
        "app_id": None,
        "depots": [],
        "manifest_pins": {},
        "ownership_ids": [],
    }
    try:
        text = lua_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("Cannot read %s: %s", lua_path, exc)
        return summary

    if lua_path.stem.isdigit():
        summary["app_id"] = lua_path.stem

    for did, key in _RE_LUA_ADDAPPID.findall(text):
        summary["depots"].append((did, key))
    for did in _RE_LUA_ADDAPPID_OWNERSHIP.findall(text):
        if did != APPID_OWNERSHIP_FAKE:
            summary["ownership_ids"].append(did)
    for did, gid in _RE_LUA_SETMANIFEST.findall(text):
        summary["manifest_pins"][did] = gid
    return summary


# ---------------------------------------------------------------------------
# LIST
# ---------------------------------------------------------------------------
def list_installed_games(steam_path: Path) -> List[Dict]:
    """List every game managed by DepotManager (i.e. with a stplug-in lua).

    Each entry: ``{appid, name, depot_count, has_acf, lua_path, depots}``.
    ``name`` is the ACF name when available, else the AppID.
    """
    stplug_in = steam_path / STPLUG_IN_SUBDIR
    if not stplug_in.is_dir():
        return []

    libraries = get_steam_libraries(steam_path)
    games: List[Dict] = []
    for lua in sorted(stplug_in.glob("*.lua")):
        if not lua.stem.isdigit():
            continue
        appid = lua.stem
        summary = _parse_lua_summary(lua)
        acf_path = find_acf_for_app(libraries, appid)
        name = appid
        if acf_path is not None:
            from .vdf_io import read_acf

            state = read_acf(acf_path)
            if state and state.get("name"):
                name = str(state["name"])
        games.append(
            {
                "appid": appid,
                "name": name,
                "depot_count": len(summary["depots"]),
                "has_acf": acf_path is not None,
                "lua_path": lua,
                "depots": summary["depots"],
                "manifest_pins": summary["manifest_pins"],
            }
        )
    return games


# ---------------------------------------------------------------------------
# ADD / UPDATE
# ---------------------------------------------------------------------------
def _install_lua(steam_path: Path, appid: str, lua_src: Path) -> Path:
    """Copy the lua into stplug-in and keep a local backup."""
    stplug_in = steam_path / STPLUG_IN_SUBDIR
    stplug_in.mkdir(parents=True, exist_ok=True)
    dest = stplug_in / f"{appid}.lua"
    shutil.copy2(lua_src, dest)
    logger.info("Installed lua: %s", dest)

    SAVED_LUA_DIR.mkdir(parents=True, exist_ok=True)
    backup = SAVED_LUA_DIR / f"{appid}.lua"
    shutil.copy2(lua_src, backup)
    return dest


def _install_manifests(steam_path: Path, inventory: Dict) -> int:
    """Copy every .manifest in ``inventory`` into Steam's depotcache.

    Both ``<steam>/depotcache/`` and ``<steam>/config/depotcache/`` receive a
    copy, since Steam/LumaCore may read from either depending on version.
    Returns the number of manifest files written.
    """
    primary = steam_path / DEPOTCACHE_SUBDIR
    mirror = steam_path / DEPOTCACHE_CONFIG_SUBDIR
    primary.mkdir(parents=True, exist_ok=True)
    mirror.mkdir(parents=True, exist_ok=True)

    count = 0
    for did, info in inventory.items():
        manifest = info.get("manifest_file") if isinstance(info, dict) else None
        if manifest is None:
            continue
        src = Path(manifest)
        if not src.is_absolute():
            continue
        if not src.is_file():
            continue
        name = src.name
        if not _RE_MANIFEST.match(name):
            continue
        for dest_dir in (primary, mirror):
            dest = dest_dir / name
            if dest.exists():
                continue
            try:
                shutil.copy2(src, dest)
                count += 1
            except OSError as exc:
                logger.warning("Cannot copy manifest %s -> %s: %s", src, dest, exc)
    return count


def _depot_keys_from_inventory(inventory: Dict) -> Dict[str, str]:
    """Build {depot_id: key} for every depot with a non-empty key, excluding
    the global ownership id '1'."""
    out: Dict[str, str] = {}
    for did, info in inventory.items():
        if str(did) == APPID_OWNERSHIP_FAKE:
            continue
        key = info.get("key") if isinstance(info, dict) else None
        if key:
            out[str(did)] = key
    return out


def _depots_for_acf(inventory: Dict) -> List[Tuple[str, str]]:
    """Build [(depot_id, manifest_gid)] for the ACF's InstalledDepots.

    The GID comes from the manifest filename (``<depot>_<gid>.manifest``) via
    ``parser.manifest_gid_from_name``. Depots without a manifest file are
    skipped (Steam cannot mount them).
    """
    out: List[Tuple[str, str]] = []
    for did, info in inventory.items():
        if str(did) == APPID_OWNERSHIP_FAKE:
            continue
        manifest = info.get("manifest_file") if isinstance(info, dict) else None
        if manifest is None:
            continue
        name = Path(manifest).name if not isinstance(manifest, str) else manifest
        gid = manifest_gid_from_name(name)
        if gid:
            out.append((str(did), gid))
    return out


async def add_game(
    steam_path: Path,
    session,
    settings: dict,
    appid: str,
    inventory: Dict,
    temp_dir: Path,
    download_mode: DownloadMode = "steam_native",
    progress_cb=None,
    library_override: Optional[Path] = None,
) -> Tuple[bool, str]:
    """Install a game's LumaCore artefacts (lua, keys, manifests, ACF).

    The actual file download for ``download_mode == "depotdownloader"`` is
    performed by the caller (GUI) via the existing DownloadManager, which will
    receive the chosen library + installdir from this function's return value
    via settings/inventory mutation. For ``steam_native`` Steam itself will
    download the files after the next launch.

    ``library_override``: when set, the ACF is written into this specific
    library (must be one of ``get_steam_libraries(steam_path)``). When None,
    the default heuristic is used (reuse the library of an existing ACF for
    the same appid, else the library with the most free space).

    Returns (True, human_message) on success.
    """
    if not steam_path.is_dir():
        return False, f"Steam path not found: {steam_path}"

    # Defense in depth: refuse to inject without LumaCore installed, otherwise
    # the user would get lua/keys/manifests/ACF written for nothing (Steam
    # vanilla ignores stplug-in and non-owned DecryptionKey entries).
    from .lumacore_setup import get_installed_version as _lc_installed

    if not _lc_installed(settings, steam_path):
        return False, "LumaCore is not installed. Install it before adding games."

    def _progress(pct: int, msg: str) -> None:
        if progress_cb:
            try:
                progress_cb(pct, 100, msg)
            except Exception:  # pragma: no cover
                pass

    _progress(5, "Locating lua file...")
    lua_src = get_lua_file(temp_dir, appid)
    if lua_src is None:
        return False, "No .lua file found in the fetched archive."

    _progress(15, "Installing lua into stplug-in...")
    _install_lua(steam_path, appid, lua_src)

    _progress(30, "Writing depot keys into config.vdf...")
    keys = _depot_keys_from_inventory(inventory)
    if keys:
        config_vdf = steam_path / "config" / "config.vdf"
        add_depot_keys(config_vdf, keys)

    _progress(50, "Copying manifests into depotcache...")
    written = _install_manifests(steam_path, inventory)
    logger.info("Copied %d manifest(s) to depotcache.", written)

    _progress(65, "Resolving game name...")
    name = await get_app_name(session, appid)
    installdir = sanitize_installdir(name)

    _progress(75, "Choosing Steam library...")
    libraries = get_steam_libraries(steam_path)
    if not libraries:
        return False, "No Steam library available."

    library: Optional[Path] = None
    if library_override is not None:
        # Validate the override is actually one of the known libraries.
        if library_override in libraries:
            library = library_override
            logger.info("Using user-selected library: %s", library)
        else:
            logger.warning(
                "library_override %s not in libraries %s; falling back to default.",
                library_override,
                libraries,
            )
    if library is None:
        library = pick_library_default(libraries, appid=appid)
    if library is None:
        return False, "No Steam library available."

    _progress(85, f"Writing ACF manifest to {library.name}...")
    depots = _depots_for_acf(inventory)
    acf_path = library / f"appmanifest_{appid}.acf"
    write_acf(acf_path, appid, name, installdir, depots)

    if download_mode == "steam_native":
        _progress(100, f"Game {appid} prepared. Restart Steam to download.")
        return True, (
            f"Game {appid} ({name}) injected. Restart Steam: it will appear "
            "in your library ready to install/download."
        )
    if download_mode == "inject_only":
        _progress(100, f"Game {appid} injected (no ACF download).")
        return True, f"Game {appid} ({name}) injected. ACF written."

    # depotdownloader mode: the caller (GUI) will invoke DownloadManager with
    # output_dir = <library parent>/common/<installdir> and then patch the ACF
    # size via vdf_io.update_acf_size.
    _progress(100, f"Game {appid} prepared for DepotDownloaderMod.")
    return True, f"Game {appid} ({name}) prepared. Run DepotDownloaderMod next."


async def update_game(
    steam_path: Path,
    session,
    settings: dict,
    appid: str,
    inventory: Dict,
    temp_dir: Path,
    download_mode: DownloadMode = "steam_native",
    progress_cb=None,
    library_override: Optional[Path] = None,
) -> Tuple[bool, str]:
    """Re-run add_game. The lua/keys/manifests/ACF are overwritten idempotently.

    For Update Selected the GUI passes ``library_override`` set to the library
    where the game's ACF currently lives, so the update lands in the same
    place (the library combobox is read-only in update mode).
    """
    return await add_game(
        steam_path,
        session,
        settings,
        appid,
        inventory,
        temp_dir,
        download_mode,
        progress_cb,
        library_override=library_override,
    )


# ---------------------------------------------------------------------------
# REMOVE
# ---------------------------------------------------------------------------
def remove_game(steam_path: Path, appid: str, scope: str = "full") -> Tuple[bool, str]:
    """Remove a managed game.

    Scopes:
      * ``basic``      — only delete ``stplug-in/<appid>.lua``.
      * ``full``       — also delete depot manifests and the ACF.
      * ``full_keys``  — also remove depot keys from config.vdf.
    """
    stplug_in = steam_path / STPLUG_IN_SUBDIR
    lua = stplug_in / f"{appid}.lua"
    removed: List[str] = []

    if lua.is_file():
        try:
            lua.unlink()
            removed.append(lua.name)
        except OSError as exc:
            logger.warning("Cannot remove %s: %s", lua, exc)

    # Also drop a stray lua sometimes left in config/ root.
    stray = steam_path / "config" / f"{appid}.lua"
    if stray.is_file():
        try:
            stray.unlink()
        except OSError:
            pass

    # Local backup.
    backup = SAVED_LUA_DIR / f"{appid}.lua"
    if backup.is_file():
        try:
            backup.unlink()
        except OSError:
            pass

    if scope in ("full", "full_keys"):
        libraries = get_steam_libraries(steam_path)

        # Determine depot ids + manifest gids to delete. Prefer the ACF (it
        # records exactly what was mounted); fall back to parsing the lua.
        depot_gids: Dict[str, str] = {}
        acf_path = find_acf_for_app(libraries, appid)
        if acf_path is not None:
            depot_gids = read_acf_depots(acf_path)
        if not depot_gids:
            summary = (
                _parse_lua_summary(lua) if lua.is_file() else {"manifest_pins": {}}
            )
            depot_gids = dict(summary.get("manifest_pins", {}))

        for did, gid in depot_gids.items():
            for sub in (DEPOTCACHE_SUBDIR, DEPOTCACHE_CONFIG_SUBDIR):
                m = steam_path / sub / f"{did}_{gid}.manifest"
                if m.is_file():
                    try:
                        m.unlink()
                        removed.append(m.name)
                    except OSError as exc:
                        logger.warning("Cannot remove %s: %s", m, exc)

        # Remove ACF from every library that has one. A backup copy is
        # saved by remove_acf as <name>.depotmanager_bak so the user can
        # recover it via "Restore ACF Backups" if needed.
        for lib in libraries:
            acf = lib / f"appmanifest_{appid}.acf"
            if acf.is_file():
                logger.info("Removing ACF for app %s from %s", appid, acf)
                if remove_acf(acf):
                    removed.append(acf.name)

    if scope == "full_keys":
        depot_ids = []
        if depot_gids:
            depot_ids = list(depot_gids.keys())
        else:
            summary = _parse_lua_summary(lua) if lua.is_file() else {"depots": []}
            depot_ids = [d for d, _ in summary.get("depots", [])]
        if depot_ids:
            config_vdf = steam_path / "config" / "config.vdf"
            remove_depot_keys(config_vdf, depot_ids)
            removed.append("config.vdf keys")

    return (
        True,
        f"Removed {len(removed)} artefact(s) for app {appid}: {', '.join(removed)}",
    )


def remove_all_games(steam_path: Path) -> int:
    """Remove every managed game (scope=full_keys). Returns the count."""
    games = list_installed_games(steam_path)
    for g in games:
        try:
            remove_game(steam_path, g["appid"], scope="full_keys")
        except Exception as exc:  # pragma: no cover
            logger.warning("Failed to remove game %s: %s", g["appid"], exc)
    return len(games)


# ---------------------------------------------------------------------------
# ACF BACKUP MANAGEMENT
# ---------------------------------------------------------------------------
def restore_acf_backups_selected(
    steam_path: Path, backup_paths: List[Path]
) -> Tuple[int, int]:
    """Restore the given ACF backups (selected by the user in the GUI).

    Each backup is restored via ``vdf_io.restore_acf_backup`` which skips
    restoration when the ACF already exists (e.g. the user legitimately
    re-installed the game via Steam).

    Returns ``(restored, skipped)`` counts.
    """
    restored = 0
    skipped = 0
    for backup in backup_paths:
        # The backup filename is appmanifest_<appid>.acf.depotmanager_bak;
        # restore_acf_backup expects the target ACF path (without the suffix).
        target_name = backup.name[: -len(".depotmanager_bak")]
        target_acf = backup.parent / target_name
        if _restore_acf_backup_vdf(target_acf):
            restored += 1
        else:
            skipped += 1
    logger.info(
        "ACF restore: %d restored, %d skipped (of %d selected).",
        restored,
        skipped,
        len(backup_paths),
    )
    return restored, skipped


def clean_all_acf_backups(steam_path: Path) -> int:
    """Delete every ``*.depotmanager_bak`` across all Steam libraries.

    Returns the number of backups removed.
    """
    libraries = get_steam_libraries(steam_path)
    return _clean_acf_backups_vdf(libraries)


def list_all_acf_backups(steam_path: Path) -> List[Path]:
    """Return every ``*.depotmanager_bak`` path across all Steam libraries."""
    libraries = get_steam_libraries(steam_path)
    return _list_acf_backups_vdf(libraries)
