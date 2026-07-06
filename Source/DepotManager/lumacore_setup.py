"""LumaCore component lifecycle: install, version check, uninstall.

LumaCore ships as four DLLs placed in the Steam install directory:

  * ``dwmapi.dll``  — DWM proxy loaded by Steam at startup
  * ``xinput1_4.dll`` — XInput proxy, backup load gate
  * ``LumaCore.dll`` — main hook library
  * ``LumaCorePayload.dll`` — injected into game processes for online-fix

Binaries are NOT vendored in this repository: they are downloaded at install
time from the official release page on GitHub (``KoriaPolis/LumaCore``).

Improvements over the upstream SFF implementation:

1. ``dwmapi.dll`` and ``xinput1_4.dll`` are backed up to
   ``<steam>/lumacore_backup/`` before being overwritten, and restored on
   uninstall. SFF deletes them unconditionally.
2. Version comparison is numeric (``int(V18[1:]) > int(V17[1:])``) rather than
   a fragile string inequality.
3. Optional ``complete`` uninstall also purges ``stplug-in/``, ``depotcache/``
   orphan manifests and per-game ACFs (delegated to ``lumacore_games``).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

import aiohttp

from . import lumacore_games
from .config import (
    LC_BACKUP_DIR,
    LC_BACKUP_DLLS,
    LC_DLLS,
    LC_RESET_FILES,
    LUMACORE_CHECK_INTERVAL_SEC,
    LUMACORE_PATTERN_DIR,
    LUMACORE_PATTERN_MIRRORS,
    LUMACORE_RELEASE_API,
    save_settings,
)
from .steam_process import kill_steam

logger = logging.getLogger("DepotManager.LumaCoreSetup")

ProgressCB = Optional[Callable[[int, int, str], None]]

# Regex that recognises the LumaCore tag format ("V18", "V4", ...).
_RE_LC_VERSION = re.compile(r"^V(\d+)$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# VERSION HELPERS
# ---------------------------------------------------------------------------
def _normalise_version(tag: str) -> Optional[Tuple[str, int]]:
    """Normalise a LumaCore tag into ("V", major_int).

    Returns None for tags that don't match the ``V<number>`` shape, so callers
    can fall back to a raw string comparison.
    """
    if not tag:
        return None
    m = _RE_LC_VERSION.match(tag.strip())
    if m:
        return ("V", int(m.group(1)))
    # Tolerate "LumaCore V5" release names.
    tail = tag.strip().split()[-1]
    m = _RE_LC_VERSION.match(tail)
    if m:
        return ("V", int(m.group(1)))
    return None


def _version_key(tag: str) -> Tuple[int, ...]:
    """Sort/comparison key for a LumaCore version tag.

    Higher is newer. Unknown formats degrade to a tuple that sorts below any
    known version, so "no info" never claims to be an upgrade.
    """
    norm = _normalise_version(tag)
    if norm is None:
        return (0,)
    return (norm[1],)


def is_version_newer(latest: str, installed: str) -> bool:
    """True when ``latest`` is strictly newer than ``installed``."""
    return _version_key(latest) > _version_key(installed)


def get_installed_version(settings: dict, steam_path: Optional[Path]) -> str:
    """Return the cached installed version, validating that DLLs are present.

    Returns "" when LumaCore is not actually installed (the cached value gets
    cleared by the caller via save_settings to keep things consistent).
    """
    cached = str(settings.get("lumacore_installed_version", "")).strip()
    if not cached:
        return ""
    if steam_path is None or not steam_path.is_dir():
        return ""
    # Consider installed only if every required DLL is in place. Partial state
    # means a previous install failed or was interrupted; treat as "not
    # installed" so the UI offers a fresh install.
    if not all((steam_path / dll).is_file() for dll in LC_DLLS):
        return ""
    return cached


# ---------------------------------------------------------------------------
# GITHUB RELEASE FETCH
# ---------------------------------------------------------------------------
async def fetch_latest_release(
    session: aiohttp.ClientSession, timeout: int = 10
) -> Dict:
    """GET the latest release metadata from GitHub.

    Returns a dict with: tag, name, assets (list of {name, url, size}).
    Raises aiohttp.ClientError / asyncio.TimeoutError on network failure.
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "Cache-Control": "no-cache",
        "User-Agent": "DepotManager",
    }
    async with session.get(
        LUMACORE_RELEASE_API,
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=timeout),
    ) as r:
        r.raise_for_status()
        data = await r.json()
    tag = str(data.get("tag_name") or data.get("name") or "").strip()
    assets = []
    for a in data.get("assets", []) or []:
        assets.append(
            {
                "name": str(a.get("name", "")),
                "url": str(a.get("browser_download_url", "")),
                "size": int(a.get("size", 0)),
            }
        )
    return {"tag": tag, "name": str(data.get("name", "")), "assets": assets}


def _pick_asset(assets: list, variant: str) -> Optional[Dict]:
    """Choose the right downloadable asset for ``variant`` ('release'/'debug').

    Preference order:
      1. Exact case-insensitive match on ``Release.zip`` / ``Debug.zip``.
      2. Any ``<variant>.zip``.
      3. First generic ``.zip`` that is not "Source code".
    """
    if not assets:
        return None
    target = ("Release.zip" if variant == "release" else "Debug.zip").lower()

    for a in assets:
        if a["name"].lower() == target:
            return a
    for a in assets:
        if a["name"].lower().startswith(variant.lower()) and a["name"].lower().endswith(
            ".zip"
        ):
            return a
    for a in assets:
        n = a["name"].lower()
        if n.endswith(".zip") and "source" not in n:
            return a
    return None


# ---------------------------------------------------------------------------
# UPDATE CHECK
# ---------------------------------------------------------------------------
async def check_for_update(
    settings: dict, session: aiohttp.ClientSession, force: bool = False
) -> Dict:
    """Compare the installed LumaCore version against the latest GitHub release.

    Respects a 6h cooldown (configurable via ``LUMACORE_CHECK_INTERVAL_SEC``)
    unless ``force`` is set. Returns::

        {
          "installed": str,        # "" if not installed
          "latest": str,           # "" if unknown
          "update_available": bool,
          "checked_at": float,     # epoch seconds
          "source": "remote"|"cache",
        }
    """
    import time

    now = time.time()
    last_check = float(settings.get("lumacore_last_check", 0) or 0)
    cached_latest = str(settings.get("lumacore_latest_version", "")).strip()

    from .steam_path import get_steam_path

    steam_path = get_steam_path(settings)
    installed = get_installed_version(settings, steam_path)

    use_cache = (
        (not force)
        and (now - last_check < LUMACORE_CHECK_INTERVAL_SEC)
        and cached_latest
    )
    if use_cache:
        return {
            "installed": installed,
            "latest": cached_latest,
            "update_available": is_version_newer(cached_latest, installed)
            if cached_latest
            else False,
            "checked_at": last_check,
            "source": "cache",
        }

    try:
        release = await fetch_latest_release(session)
        latest = release["tag"]
        settings["lumacore_latest_version"] = latest
        settings["lumacore_last_check"] = now
        save_settings(settings)
        return {
            "installed": installed,
            "latest": latest,
            "update_available": is_version_newer(latest, installed)
            if installed
            else bool(latest),
            "checked_at": now,
            "source": "remote",
        }
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        logger.warning("LumaCore update check failed: %s", exc)
        return {
            "installed": installed,
            "latest": cached_latest,
            "update_available": is_version_newer(cached_latest, installed)
            if cached_latest
            else False,
            "checked_at": last_check,
            "source": "cache" if cached_latest else "error",
        }


# ---------------------------------------------------------------------------
# INSTALL / UNINSTALL
# ---------------------------------------------------------------------------
def _reset_lumacore_files(steam_path: Path) -> Tuple[int, list]:
    """Delete every file in LC_RESET_FILES. Returns (removed_count, failures)."""
    removed = 0
    failures = []
    for subdir, name in LC_RESET_FILES:
        target = steam_path / subdir / name if subdir else steam_path / name
        if not target.exists():
            continue
        try:
            target.unlink()
            removed += 1
        except OSError as exc:
            logger.warning("Cannot remove %s: %s", target, exc)
            failures.append(str(target))
    return removed, failures


def _backup_proxy_dlls(steam_path: Path) -> int:
    """Copy dwmapi.dll / xinput1_4.dll to <steam>/lumacore_backup/ if present.

    We only back up DLLs that may have a legitimate pre-existing source (i.e.
    a different proxy tool the user had installed). LumaCore's own DLLs and
    lcoverlay.dll have no "original" to preserve.
    """
    backup_dir = steam_path / LC_BACKUP_DIR
    backup_dir.mkdir(parents=True, exist_ok=True)
    backed_up = 0
    for dll in LC_BACKUP_DLLS:
        src = steam_path / dll
        if src.is_file():
            try:
                shutil.copy2(src, backup_dir / dll)
                backed_up += 1
                logger.info("Backed up %s -> %s", src, backup_dir / dll)
            except OSError as exc:
                logger.warning("Cannot back up %s: %s", src, exc)
    return backed_up


def _restore_proxy_dlls(steam_path: Path) -> int:
    """Restore previously backed-up proxy DLLs (inverse of _backup_proxy_dlls)."""
    backup_dir = steam_path / LC_BACKUP_DIR
    if not backup_dir.is_dir():
        return 0
    restored = 0
    for dll in LC_BACKUP_DLLS:
        src = backup_dir / dll
        if src.is_file():
            try:
                shutil.copy2(src, steam_path / dll)
                restored += 1
                logger.info("Restored %s from backup", dll)
            except OSError as exc:
                logger.warning("Cannot restore %s: %s", dll, exc)
    return restored


def _extract_dlls_from_zip(zip_path: Path, steam_path: Path) -> int:
    """Extract the 4 LumaCore DLLs from a ZIP archive into ``steam_path``.

    DLLs are matched by basename, case-insensitive, regardless of any nested
    folder structure inside the archive. Returns the number of DLLs written.
    """
    targets_lower = {d.lower() for d in LC_DLLS}
    written = 0
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            if member.is_dir():
                continue
            base = Path(member.filename).name.lower()
            if base not in targets_lower:
                continue
            dest = steam_path / base  # canonical case as in LC_DLLS via lookup
            # Map back to the canonical casing used by LC_DLLS so the file
            # written to disk matches what LumaCore expects.
            for canonical in LC_DLLS:
                if canonical.lower() == base:
                    dest = steam_path / canonical
                    break
            with zf.open(member) as src, open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst)
            written += 1
            logger.debug("Extracted %s -> %s", member.filename, dest)
    return written


def _sha256_of_file(path: Path) -> Optional[str]:
    """Hex SHA-256 of a file, or None if unreadable."""
    if not path.is_file():
        return None
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError as exc:
        logger.warning("Cannot hash %s: %s", path, exc)
        return None


async def _prewarm_patterns(steam_path: Path, session: aiohttp.ClientSession) -> int:
    """Best-effort download of per-build pattern TOMLs.

    Pattern files are keyed by the SHA-256 of the corresponding Steam DLL.
    Mirrors are tried in order; failures are non-fatal. Returns the number of
    patterns successfully cached.
    """
    pattern_root = steam_path / LUMACORE_PATTERN_DIR / "pattern"
    pattern_root.mkdir(parents=True, exist_ok=True)

    # (steam_dll_basename, subdir_in_repo). subdir "" is the default bucket.
    candidates = (
        ("steamclient64.dll", ""),
        ("steamui.dll", ""),
        ("steamclient64.dll", "steamclientipc"),
    )

    downloaded = 0
    for dll_name, subdir in candidates:
        sha = _sha256_of_file(steam_path / dll_name)
        if not sha:
            continue
        # Local cache layout mirrors the repo: pattern/[subdir/]<sha>.toml
        local_dir = pattern_root / subdir if subdir else pattern_root
        local_dir.mkdir(parents=True, exist_ok=True)
        local_file = local_dir / f"{sha}.toml"
        if local_file.exists():
            downloaded += 1
            continue

        for mirror_template in LUMACORE_PATTERN_MIRRORS:
            url = mirror_template.format(subdir=subdir, sha=sha)
            try:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=15)
                ) as r:
                    if r.status != 200:
                        continue
                    data = await r.read()
                tmp = local_file.with_suffix(".toml.tmp")
                tmp.write_bytes(data)
                tmp.replace(local_file)
                downloaded += 1
                logger.debug("Pattern cached: %s", local_file.name)
                break
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                continue
    return downloaded


async def install_lumacore(
    steam_path: Path,
    session: aiohttp.ClientSession,
    settings: dict,
    progress_cb: ProgressCB = None,
    variant: Optional[str] = None,
) -> Tuple[bool, str]:
    """Download and install LumaCore into the given Steam directory.

    Steps:
      1. Validate steam_path.
      2. Kill Steam (so DLLs are not locked).
      3. Backup any pre-existing dwmapi.dll / xinput1_4.dll.
      4. Remove previous LumaCore files.
      5. Download the release ZIP from GitHub.
      6. Extract the 4 DLLs into steam_path.
      7. Verify all 4 DLLs are present.
      8. Best-effort prewarm of pattern TOMLs.
      9. Persist the installed version into settings.

    Returns (True, message) on success, (False, reason) on failure.
    """
    if not steam_path.is_dir():
        return False, f"Steam path not found: {steam_path}"

    if variant is None:
        variant = str(settings.get("lumacore_variant", "release")).lower()
    if variant not in ("release", "debug"):
        variant = "release"

    def _progress(percent: int, message: str) -> None:
        if progress_cb:
            try:
                progress_cb(percent, 100, message)
            except Exception:  # pragma: no cover - UI callback must never throw
                pass

    _progress(0, "Closing Steam...")
    kill_steam(timeout=15)

    _progress(5, "Backing up existing proxy DLLs...")
    _backup_proxy_dlls(steam_path)

    _progress(10, "Removing previous LumaCore files...")
    _reset_lumacore_files(steam_path)

    _progress(20, "Fetching latest release info...")
    try:
        release = await fetch_latest_release(session)
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        return False, f"Cannot reach GitHub: {exc}"
    tag = release["tag"]
    if not tag:
        return False, "Latest release has no tag name."

    asset = _pick_asset(release["assets"], variant)
    if asset is None:
        return False, f"No {variant}.zip asset in release {tag}."

    _progress(30, f"Downloading {asset['name']} ({asset['size'] // 1024} KB)...")
    with tempfile.TemporaryDirectory(prefix="depotmgr_lc_") as tmpdir:
        zip_path = Path(tmpdir) / asset["name"]
        try:
            async with session.get(asset["url"], timeout=None) as r:
                r.raise_for_status()
                total = int(r.headers.get("Content-Length", 0)) or asset["size"]
                done = 0
                with open(zip_path, "wb") as f:
                    async for chunk in r.content.iter_chunked(1 << 19):  # 512 KB
                        f.write(chunk)
                        done += len(chunk)
                        if total:
                            pct = 30 + int(done * 60 / total)
                            _progress(pct, f"Downloading... {done // 1024} KB")
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            return False, f"Download failed: {exc}"

        _progress(90, "Extracting DLLs...")
        try:
            written = _extract_dlls_from_zip(zip_path, steam_path)
        except (zipfile.BadZipFile, OSError) as exc:
            return False, f"Extraction failed: {exc}"
        if written != len(LC_DLLS):
            return False, f"Expected {len(LC_DLLS)} DLLs, extracted {written}."

    _progress(95, "Verifying installation...")
    for dll in LC_DLLS:
        if not (steam_path / dll).is_file():
            return False, f"DLL missing after install: {dll}"

    _progress(97, "Prewarming pattern cache...")
    try:
        await _prewarm_patterns(steam_path, session)
    except Exception as exc:  # best-effort
        logger.warning("Pattern prewarm failed: %s", exc)

    settings["lumacore_installed_version"] = tag
    save_settings(settings)

    _progress(100, f"LumaCore {tag} installed. Restart Steam to activate.")
    return True, f"LumaCore {tag} installed successfully. Restart Steam."


def uninstall_lumacore(
    steam_path: Path,
    settings: dict,
    progress_cb: ProgressCB = None,
    complete: bool = False,
) -> Tuple[bool, str]:
    """Remove LumaCore DLLs and (optionally) every per-game artefact.

    With ``complete=False`` only the four DLLs (+ lcoverlay.dll) are removed and
    the cached version is cleared. With ``complete=True`` all stplug-in lua
    files, depotcache manifests written by DepotManager and per-game ACFs are
    purged too (delegated to lumacore_games.remove_all_games).

    Proxy DLLs that were backed up at install time are restored.
    """
    if not steam_path.is_dir():
        return False, f"Steam path not found: {steam_path}"

    def _progress(percent: int, message: str) -> None:
        if progress_cb:
            try:
                progress_cb(percent, 100, message)
            except Exception:  # pragma: no cover
                pass

    # Guard: detect whether LumaCore is actually installed before killing
    # Steam. Killing Steam when there is nothing to remove is disruptive and
    # misleading (the user is told to "restart Steam" for a no-op).
    installed = bool(get_installed_version(settings, steam_path))

    if not installed and not complete:
        # Nothing to remove and no orphan cleanup requested.
        logger.info("uninstall_lumacore: nothing to remove (LumaCore not installed).")
        return True, "LumaCore is not installed. Nothing to uninstall."

    if not installed and complete:
        # LumaCore absent but the user wants orphan game cleanup. stplug-in
        # lua and depotcache manifests are NOT locked by a running Steam, so
        # we skip kill_steam and go straight to remove_all_games.
        logger.info(
            "uninstall_lumacore: LumaCore not installed, running orphan cleanup only."
        )
        _progress(50, "Cleaning up orphan game files...")
        games_removed = 0
        try:
            games_removed = lumacore_games.remove_all_games(steam_path)
        except Exception as exc:  # pragma: no cover
            logger.warning("remove_all_games failed: %s", exc)
        _progress(100, "Orphan cleanup done.")
        return (
            True,
            f"LumaCore was not installed. Cleaned up {games_removed} orphan game(s).",
        )

    _progress(0, "Closing Steam...")
    kill_steam(timeout=15)

    _progress(40, "Removing LumaCore DLLs...")
    removed, failures = _reset_lumacore_files(steam_path)

    _progress(70, "Restoring backed-up proxy DLLs...")
    _restore_proxy_dlls(steam_path)

    settings["lumacore_installed_version"] = ""
    save_settings(settings)

    games_removed = 0
    if complete:
        _progress(80, "Removing all managed games...")
        try:
            games_removed = lumacore_games.remove_all_games(steam_path)
        except Exception as exc:  # pragma: no cover
            logger.warning("remove_all_games failed: %s", exc)

    _progress(
        100, f"LumaCore deactivated. Removed {removed} DLL(s), {games_removed} game(s)."
    )
    if failures:
        return (
            False,
            f"Removed {removed} DLL(s), failed to remove: {', '.join(failures)}",
        )
    return (
        True,
        f"LumaCore deactivated. Removed {removed} DLL(s), {games_removed} game(s). Restart Steam.",
    )
