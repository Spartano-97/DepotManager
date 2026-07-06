"""VDF / ACF read-write helpers with case-insensitive navigation.

Two Steam files matter for LumaCore integration:

* ``<steam>/config/config.vdf`` — holds depot AES decryption keys under
  ``InstallConfigStore.Software.Valve.Steam.depots.<depot_id>.DecryptionKey``.
  We add/remove keys here so LumaCore (and Steam itself) can decrypt depots.

* ``<library>/steamapps/appmanifest_<appid>.acf`` — per-game state used by
  Steam to show the game in the library and know which depots to mount.
  We write it so a natively-downloaded game appears ready to install/update.

Backup strategy (improvement over the upstream SFF implementation, which only
keeps a single rolling ``.backup`` and loses the original after the first
write):

* ``<name>.depotmanager_orig`` — created ONCE on first contact, never
  overwritten. Lets us restore the true pre-DepotManager state if needed.
* ``<name>.backup`` — created before every mutating operation, rolling.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import vdf as _vdf
except ImportError:  # pragma: no cover - vdf is a hard dep on Windows runtime.
    _vdf = None  # type: ignore

logger = logging.getLogger("DepotManager.VDFIO")


# ---------------------------------------------------------------------------
# LOW LEVEL
# ---------------------------------------------------------------------------
def read_vdf(path: Path) -> dict:
    """Parse a VDF file into a dict. Returns {} on missing/unreadable file."""
    if _vdf is None:
        raise RuntimeError("vdf module is not installed in the current interpreter.")
    if not path.is_file():
        return {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = _vdf.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as exc:
        logger.warning("Cannot parse VDF %s: %s", path, exc)
        return {}


def write_vdf(path: Path, data: dict) -> None:
    """Serialise ``data`` to VDF and write atomically (.tmp + replace)."""
    if _vdf is None:
        raise RuntimeError("vdf module is not installed in the current interpreter.")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            _vdf.dump(data, f)
        os.replace(tmp, path)
    except OSError as exc:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise exc


def _case_insensitive_get(node: dict, key: str) -> Optional[dict]:
    """Fetch a child dict by key, ignoring case. Returns None if absent or
    if the child is not a dict (caller is expected to create it then)."""
    if not isinstance(node, dict):
        return None
    key_lower = key.lower()
    for k, v in node.items():
        if k.lower() == key_lower and isinstance(v, dict):
            return v
    return None


def _case_insensitive_ensure(node: dict, key: str) -> dict:
    """Get-or-create a child dict at ``key`` (case-insensitive lookup).

    If a same-named key exists but holds a non-dict value, it is replaced
    (we never expect that to happen for the paths we walk inside config.vdf).
    """
    existing = _case_insensitive_get(node, key)
    if existing is not None:
        return existing
    node[key] = {}
    return node[key]


def _ensure_path(root: dict, path: List[str]) -> dict:
    """Walk/create a chain of nested dicts (case-insensitive at each level)."""
    node = root
    for segment in path:
        node = _case_insensitive_ensure(node, segment)
    return node


def _backup_once(path: Path) -> None:
    """Create <name>.depotmanager_orig the first time we touch a file."""
    orig = path.with_name(path.name + ".depotmanager_orig")
    if path.is_file() and not orig.exists():
        try:
            shutil.copy2(path, orig)
            logger.debug("Created one-time backup: %s", orig)
        except OSError as exc:
            logger.warning("Cannot create orig backup %s: %s", orig, exc)


def _backup_rolling(path: Path) -> None:
    """Create <name>.backup before each mutating operation (overwrites prior)."""
    if not path.is_file():
        return
    backup = path.with_name(path.name + ".backup")
    try:
        shutil.copy2(path, backup)
    except OSError as exc:
        logger.warning("Cannot create rolling backup %s: %s", backup, exc)


# ---------------------------------------------------------------------------
# config.vdf DEPOT KEYS
# ---------------------------------------------------------------------------
# Path inside config.vdf to the depots subtree. Each entry is a case-insensitive
# segment; the VDF produced by Steam uses exactly these spellings, but we stay
# case-insensitive to be robust against locale/build differences.
_CONFIG_DEPOTS_PATH = ["InstallConfigStore", "Software", "Valve", "Steam", "depots"]


def add_depot_keys(config_vdf_path: Path, depot_keys: Dict[str, str]) -> bool:
    """Insert/overwrite depot decryption keys in config.vdf.

    ``depot_keys`` maps ``depot_id`` (str) -> 64-hex AES key (str). Entries with
    an empty key value are skipped.

    Returns True on success, False if the file could not be updated.
    """
    if _vdf is None:
        raise RuntimeError("vdf module is not installed.")
    if not depot_keys:
        return True

    _backup_once(config_vdf_path)
    _backup_rolling(config_vdf_path)

    data = read_vdf(config_vdf_path)
    depots = _ensure_path(data, _CONFIG_DEPOTS_PATH)

    for depot_id, key in depot_keys.items():
        if not key:
            continue
        depot_node = _case_insensitive_ensure(depots, str(depot_id))
        depot_node["DecryptionKey"] = key

    try:
        write_vdf(config_vdf_path, data)
        logger.info("Wrote %d depot key(s) to %s", len(depot_keys), config_vdf_path)
        return True
    except OSError as exc:
        logger.error("Cannot write config.vdf %s: %s", config_vdf_path, exc)
        return False


def remove_depot_keys(config_vdf_path: Path, depot_ids: List[str]) -> bool:
    """Remove depot entries from config.vdf. Idempotent.

    Returns True on success (including when nothing was there to remove).
    """
    if _vdf is None:
        raise RuntimeError("vdf module is not installed.")
    if not depot_ids:
        return True

    if not config_vdf_path.is_file():
        return True

    _backup_rolling(config_vdf_path)
    data = read_vdf(config_vdf_path)
    depots = _case_insensitive_get(data, _CONFIG_DEPOTS_PATH[0])
    for segment in _CONFIG_DEPOTS_PATH[1:]:
        depots = _case_insensitive_get(depots, segment) if depots is not None else None
        if depots is None:
            break

    if not isinstance(depots, dict):
        return True  # nothing to remove

    targets_lower = {str(d).lower() for d in depot_ids}
    to_delete = [k for k in depots.keys() if k.lower() in targets_lower]
    for k in to_delete:
        del depots[k]

    try:
        write_vdf(config_vdf_path, data)
        logger.info("Removed %d depot key(s) from %s", len(to_delete), config_vdf_path)
        return True
    except OSError as exc:
        logger.error("Cannot write config.vdf %s: %s", config_vdf_path, exc)
        return False


# ---------------------------------------------------------------------------
# appmanifest_<appid>.acf
# ---------------------------------------------------------------------------
def write_acf(
    acf_path: Path,
    appid: str,
    name: str,
    installdir: str,
    depots: List[Tuple[str, str]],
    buildid: str = "0",
    size_on_disk: int = 0,
) -> None:
    """Write an ACF manifest so Steam lists the app and knows its depots.

    ``depots`` is a list of ``(depot_id, manifest_gid)`` tuples. The GID is
    written both to ``InstalledDepots`` (structured) and ``MountedDepots``
    (flat map), matching what Steam itself writes.
    """
    if _vdf is None:
        raise RuntimeError("vdf module is not installed.")

    installed = {}
    mounted = {}
    for depot_id, manifest_gid in depots:
        installed[str(depot_id)] = {
            "manifest": str(manifest_gid),
            "size": "0",
            "download": "0",
        }
        mounted[str(depot_id)] = str(manifest_gid)

    app_state = {
        "appid": str(appid),
        "Universe": "1",
        "name": name,
        "StateFlags": "4",
        "installdir": installdir,
        "buildid": str(buildid),
        "TargetBuildID": str(buildid),
        "BytesToDownload": "0",
        "BytesDownloaded": "0",
        "BytesToStage": "0",
        "BytesStaged": "0",
        "BytesToValidate": "0",
        "BytesValidated": "0",
        "SizeOnDisk": str(size_on_disk),
        "InstalledDepots": installed,
        "MountedDepots": mounted,
        "StagedFiles": "",
        "SharedDepots": {},
        "UserConfig": {
            "Language": "english",
            "MountedConfig": "",
            "AutoUpdate": "always",
            "AllowOtherDownloadsWhileRunning": "0",
        },
        "MountedConfig": "",
    }

    payload = {"AppState": app_state}
    acf_path.parent.mkdir(parents=True, exist_ok=True)
    write_vdf(acf_path, payload)
    logger.info("Wrote ACF %s (%d depots)", acf_path.name, len(depots))


def read_acf(acf_path: Path) -> Optional[dict]:
    """Parse an ACF file and return the inner ``AppState`` dict, or None."""
    if not acf_path.is_file():
        return None
    data = read_vdf(acf_path)
    state = data.get("AppState") if isinstance(data, dict) else None
    return state if isinstance(state, dict) else None


def read_acf_depots(acf_path: Path) -> Dict[str, str]:
    """Return ``{depot_id: manifest_gid}`` from an ACF's MountedDepots.

    MountedDepots is the simplest flat map; InstalledDepots is structured.
    We prefer MountedDepots and fall back to InstalledDepots for robustness.
    """
    state = read_acf(acf_path)
    if not state:
        return {}
    mounted = state.get("MountedDepots")
    if isinstance(mounted, dict):
        return {str(k): str(v) for k, v in mounted.items()}
    installed = state.get("InstalledDepots")
    if isinstance(installed, dict):
        out: Dict[str, str] = {}
        for did, info in installed.items():
            if isinstance(info, dict) and "manifest" in info:
                out[str(did)] = str(info["manifest"])
        return out
    return {}


def remove_acf(acf_path: Path) -> bool:
    """Delete an ACF file, keeping a backup first.

    A copy is saved as ``<acf>.depotmanager_bak`` (overwriting any prior
    backup) so that ``restore_acf_backup`` can recover it if the user
    realises the game was legitimately owned. Returns True if deleted or
    already absent.
    """
    if not acf_path.is_file():
        return True
    # Best-effort backup; never block deletion if the backup fails.
    try:
        shutil.copy2(acf_path, acf_path.with_name(acf_path.name + ".depotmanager_bak"))
        logger.debug("ACF backup saved: %s.depotmanager_bak", acf_path.name)
    except OSError as exc:
        logger.warning("Cannot back up ACF %s: %s", acf_path, exc)
    try:
        acf_path.unlink()
        logger.info("Removed ACF %s", acf_path.name)
        return True
    except OSError as exc:
        logger.warning("Cannot remove ACF %s: %s", acf_path, exc)
        return False


# ---------------------------------------------------------------------------
# ACF BACKUP MANAGEMENT
# ---------------------------------------------------------------------------
ACF_BACKUP_SUFFIX = ".depotmanager_bak"


def restore_acf_backup(acf_path: Path) -> bool:
    """Restore an ACF from its ``.depotmanager_bak`` backup.

    Safety: if the ACF already exists (e.g. the user re-installed the game
    legitimately via Steam), the backup is NOT applied — we must not
    overwrite a real install state. Returns True if restored, False if
    skipped (ACF exists) or no backup available.
    """
    backup = acf_path.with_name(acf_path.name + ACF_BACKUP_SUFFIX)
    if not backup.is_file():
        return False
    if acf_path.is_file():
        logger.info(
            "Restore skipped: ACF already present for %s (kept legitimate state).",
            acf_path.name,
        )
        return False
    try:
        shutil.copy2(backup, acf_path)
        logger.info("Restored ACF from backup: %s", acf_path.name)
        return True
    except OSError as exc:
        logger.warning("Cannot restore ACF %s from backup: %s", acf_path, exc)
        return False


def list_acf_backups(libraries: List[Path]) -> List[Path]:
    """Find every ``*.depotmanager_bak`` across the given Steam libraries."""
    backups: List[Path] = []
    for lib in libraries:
        if not lib.is_dir():
            continue
        backups.extend(sorted(lib.glob("*" + ACF_BACKUP_SUFFIX)))
    return backups


def clean_acf_backups(libraries: List[Path]) -> int:
    """Delete every ``*.depotmanager_bak`` across the given libraries.

    Returns the number of backups actually removed.
    """
    removed = 0
    for backup in list_acf_backups(libraries):
        try:
            backup.unlink()
            removed += 1
            logger.debug("Deleted ACF backup: %s", backup.name)
        except OSError as exc:
            logger.warning("Cannot delete ACF backup %s: %s", backup, exc)
    return removed


def update_acf_size(
    acf_path: Path, size_on_disk: int, buildid: Optional[str] = None
) -> bool:
    """Patch SizeOnDisk (and optionally buildid) on an existing ACF.

    Used after DepotDownloaderMod finishes writing files: the ACF was created
    with size 0, and we now know the real on-disk size.
    """
    state = read_acf(acf_path)
    if not state:
        return False
    state["SizeOnDisk"] = str(size_on_disk)
    if buildid is not None:
        state["buildid"] = str(buildid)
        state["TargetBuildID"] = str(buildid)
    try:
        write_vdf(acf_path, {"AppState": state})
        return True
    except OSError as exc:
        logger.error("Cannot patch ACF %s: %s", acf_path, exc)
        return False
