"""Steam process management (Windows runtime only).

Used before installing / uninstalling LumaCore DLLs, which are write-locked by
``steam.exe`` while Steam is running. All functions are no-ops on non-Windows
hosts so that the rest of the codebase can import this module safely from WSL
during development.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from typing import Tuple

logger = logging.getLogger("DepotManager.SteamProcess")

# Windows-only flag to prevent child processes from spawning a visible console
# window. On non-Windows hosts the value is 0 (no-op, default creation flags).
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# Processes that may hold a handle on the DLLs we are about to replace.
# steamwebhelper.exe spawns many children; killing the parent is enough.
_STEAM_PROCESSES = ("steam.exe", "steamservice.exe", "steamwebhelper.exe")


def is_windows() -> bool:
    return sys.platform == "win32"


def _run(cmd: Tuple[str, ...], timeout: int = 10) -> int:
    """Run a command without raising, returning the exit code (or -1 on error)."""
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            creationflags=_NO_WINDOW,
        )
        return proc.returncode
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("Command %s failed: %s", cmd, exc)
        return -1


def is_steam_running() -> bool:
    """True if at least one Steam process is currently running."""
    if not is_windows():
        return False
    # tasklist returns 0 (found) / 1 (not found). We filter by image name.
    for image in _STEAM_PROCESSES:
        rc = _run(
            ("tasklist", "/FI", f"IMAGENAME eq {image}", "/NH"),
            timeout=5,
        )
        # tasklist prints "INFO: No tasks are running..." on no match and still
        # exits 0, so we cannot rely on the return code alone. Re-run with
        # output capture and check for the image name string instead.
        try:
            result = subprocess.run(
                ("tasklist", "/FI", f"IMAGENAME eq {image}", "/NH", "/FO", "CSV"),
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                creationflags=_NO_WINDOW,
            )
            if image.lower() in result.stdout.lower():
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
    return False


def kill_steam(timeout: int = 15) -> bool:
    """Force-kill all Steam processes and wait for handles to be released.

    Returns True if, at the end of the wait window, no Steam process is still
    running. Returns False if something is still alive (DLLs may stay locked).
    On non-Windows hosts this is a no-op returning True.
    """
    if not is_windows():
        logger.debug("kill_steam called on non-Windows host; no-op.")
        return True

    if not is_steam_running():
        return True

    logger.info("Terminating Steam processes: %s", ", ".join(_STEAM_PROCESSES))
    for image in _STEAM_PROCESSES:
        _run(("taskkill", "/F", "/IM", image), timeout=8)

    # Poll for handle release. taskkill /F is synchronous for the kernel-side
    # teardown, but Steam child processes (webhelper) can take a moment to
    # actually drop their file locks.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_steam_running():
            logger.info("Steam processes released handles.")
            return True
        time.sleep(0.5)

    still_running = is_steam_running()
    logger.warning("Steam still running after %ds: %s", timeout, still_running)
    return not still_running
