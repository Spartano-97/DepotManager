"""Steam process management (kill/is_running) for Windows runtime."""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from typing import Tuple

logger = logging.getLogger("DepotManager.SteamProcess")

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

_STEAM_PROCESSES = ("steam.exe", "steamservice.exe", "steamwebhelper.exe")


# --- PROCESS MANAGEMENT ---
def is_windows() -> bool:
    return sys.platform == "win32"


def _run(cmd: Tuple[str, ...], timeout: int = 10) -> int:
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
    for image in _STEAM_PROCESSES:
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

    Returns True if no Steam process is still running at the end of the
    wait window. On non-Windows hosts this is a no-op returning True.
    """
    if not is_windows():
        logger.debug("kill_steam called on non-Windows host; no-op.")
        return True

    if not is_steam_running():
        return True

    logger.info("Terminating Steam processes: %s", ", ".join(_STEAM_PROCESSES))
    for image in _STEAM_PROCESSES:
        _run(("taskkill", "/F", "/IM", image), timeout=8)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_steam_running():
            logger.info("Steam processes released handles.")
            return True
        time.sleep(0.5)

    still_running = is_steam_running()
    logger.warning("Steam still running after %ds: %s", timeout, still_running)
    return not still_running


def start_steam(settings: dict) -> bool:
    """Launches Steam in the background with the correct working directory."""
    from .steam_path import get_steam_path

    steam_path = get_steam_path(settings)
    if not steam_path or not (steam_path / "steam.exe").is_file():
        logger.error("Steam executable not found at: %s", steam_path)
        return False

    logger.info("Starting Steam from: %s", steam_path)
    try:
        subprocess.Popen(
            [str(steam_path / "steam.exe")],
            cwd=str(steam_path),
            creationflags=_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except OSError as exc:
        logger.error("Failed to launch Steam: %s", exc)
        return False


def restart_steam(settings: dict) -> bool:
    """Force-kills Steam and then restarts it."""
    logger.info("Restarting Steam...")

    kill_steam()
    time.sleep(2)
    return start_steam(settings)
