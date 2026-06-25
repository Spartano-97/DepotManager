import logging
import zipfile
from pathlib import Path

from .config import _RE_LUA_ADDAPPID, _RE_LUA_TABLE, _RE_MANIFEST

logger = logging.getLogger("DepotManager.Parser")


def safe_extract(zip_path: Path, extract_to: Path) -> None:
    """Extracts a ZIP file safely while preventing Zip Slip vulnerabilities."""
    extract_to_res = extract_to.resolve()
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            member_path = (extract_to / member.filename).resolve()
            try:
                member_path.relative_to(extract_to_res)
            except ValueError:
                raise PermissionError(
                    f"Zip Slip detected: '{member.filename}' attempts to escape the extraction directory."
                )
        zf.extractall(extract_to)


def scan_directory(temp_dir: Path) -> dict:
    """Scans the temp directory for Lua scripts and Steam manifests to build the depot inventory."""
    inv: dict = {}

    for lua_file in temp_dir.glob("*.lua"):
        try:
            with open(lua_file, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            if "\ufffd" in content:
                logger.warning("Lua file with problematic encoding: %s", lua_file.name)

        except OSError as exc:
            logger.warning("Cannot read %s: %s", lua_file.name, exc)
            continue

        matches = _RE_LUA_ADDAPPID.findall(content) or _RE_LUA_TABLE.findall(content)

        for did, key in matches:
            inv.setdefault(did, {"key": None, "manifest_file": None})["key"] = key

    for m in temp_dir.glob("*.manifest"):
        match = _RE_MANIFEST.match(m.name)
        if match:
            did = match.group(1)
            inv.setdefault(did, {"key": None, "manifest_file": None})[
                "manifest_file"
            ] = m

    return inv
