"""Subprocess orchestration for DepotDownloaderMod.exe."""

import asyncio
import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .config import APP_DIR, KEYS_FILE

logger = logging.getLogger("DepotManager.Downloader")

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


# --- DOWNLOAD MANAGER ---
class DownloadManager:
    """Manages downloading selected Steam depots using DepotDownloaderMod.exe."""

    def __init__(
        self,
        settings: dict,
        inventory: dict,
        current_temp_dir: Optional[Path],
        log_callback: Callable[[str], None],
        output_dir: Optional[Path] = None,
        max_downloads: int = 16,
    ) -> None:
        self.settings = settings
        self.inventory = inventory
        self.current_temp_dir = current_temp_dir
        self.log_callback = log_callback
        self.output_dir = output_dir
        self.max_downloads = max_downloads

    def _write_keys_file(self, keys_dict: Dict[str, str]) -> None:
        try:
            with open(KEYS_FILE, "w", encoding="utf-8") as f:
                for did, key in keys_dict.items():
                    f.write(f"{did};{key}\n")
            logger.debug("Keys file written: %d keys.", len(keys_dict))
        except OSError as exc:
            logger.error("Cannot write keys file: %s", exc)
            raise exc

    async def run_downloads(
        self, selected_ids: List[str], exe_path: Path, app_id: str
    ) -> None:
        """Orchestrates downloads of all selected depots with controlled concurrency."""
        keys_to_write = {
            str(did): self.inventory[str(did)]["key"]
            for did in selected_ids
            if self.inventory.get(str(did)) and self.inventory[str(did)]["key"]
        }
        await asyncio.to_thread(self._write_keys_file, keys_to_write)

        max_concurrent = self.settings.get("max_concurrent_downloads", 1)
        sem = asyncio.Semaphore(max_concurrent)

        tasks = [
            asyncio.create_task(self._download_single(did, exe_path, app_id, sem))
            for did in selected_ids
        ]

        try:
            try:
                results = await asyncio.gather(*tasks, return_exceptions=True)

                cancelled = any(isinstance(r, asyncio.CancelledError) for r in results)
                errors = [
                    r
                    for r in results
                    if isinstance(r, Exception)
                    and not isinstance(r, asyncio.CancelledError)
                ]

                if cancelled:
                    self.log_callback(
                        "[DepotDownloaderMod] --- OPERATION CANCELLED BY USER ---"
                    )
                    raise asyncio.CancelledError()
                elif errors:
                    self.log_callback(
                        f"[DepotDownloaderMod] --- COMPLETED WITH {len(errors)} ERRORS ---"
                    )
                    raise RuntimeError(
                        f"{len(errors)} depots encountered errors during download."
                    )
                else:
                    self.log_callback(
                        "[DepotDownloaderMod] --- ALL SELECTED DOWNLOADS COMPLETED ---"
                    )

            except asyncio.CancelledError:
                for t in tasks:
                    t.cancel()
                self.log_callback(
                    "[DepotDownloaderMod] --- DOWNLOAD OPERATION CANCELLED ---"
                )
                raise
        finally:
            keys_path = Path(KEYS_FILE)
            if keys_path.exists():
                try:
                    await asyncio.to_thread(keys_path.unlink)
                    logger.debug(
                        "Keys file removed immediately after download: %s", KEYS_FILE
                    )
                except OSError as exc:
                    logger.warning(
                        "Cannot remove keys file %s immediately: %s", KEYS_FILE, exc
                    )

    async def _download_single(
        self, did: str, exe_path: Path, app_id: str, sem: asyncio.Semaphore
    ) -> None:
        """Downloads a single depot using DepotDownloaderMod.exe in a subprocess."""
        info = self.inventory.get(str(did))
        if not info or not info["manifest_file"]:
            logger.warning("Depot %s: no manifest file, skipping.", did)
            self.log_callback(
                f"[DepotDownloaderMod] Depot {did}: Missing manifest file, skipping."
            )
            return

        manifest_src: Path = info["manifest_file"]

        if self.current_temp_dir is None:
            raise RuntimeError("No temporary directory is set.")

        if not manifest_src.is_absolute():
            manifest_src = self.current_temp_dir / manifest_src.name

        match = re.search(r"_(\d+)\.manifest$", manifest_src.name)
        if not match:
            logger.warning(
                "Depot %s: unparsable manifest name (%s), skipping.",
                did,
                manifest_src.name,
            )
            self.log_callback(
                f"[DepotDownloaderMod] Depot {did}: Unparsable manifest name, skipping."
            )
            return

        manifest_id = match.group(1)
        local_manifest = APP_DIR / manifest_src.name

        try:
            await asyncio.to_thread(shutil.copy, str(manifest_src), str(local_manifest))
        except OSError as exc:
            logger.error("Cannot copy manifest for depot %s: %s", did, exc)
            self.log_callback(
                f"[DepotDownloaderMod] Error copying manifest Depot {did}: {exc}"
            )
            return

        async with sem:
            self.log_callback(
                f"\n[DepotDownloaderMod] >>> Starting download Depot {did}..."
            )
            cmd = [
                str(exe_path),
                "-app",
                app_id,
                "-depot",
                str(did),
                "-manifest",
                manifest_id,
                "-manifestfile",
                local_manifest.name,
                "-depotkeys",
                KEYS_FILE,
                "-max-downloads",
                str(self.max_downloads),
            ]
            if self.output_dir is not None:
                cmd.extend(["-dir", str(self.output_dir)])
            logger.debug("Command: %s", " ".join(cmd))

            process: Optional[asyncio.subprocess.Process] = None
            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    cwd=str(APP_DIR),
                    creationflags=_NO_WINDOW,
                )

                if process.stdout is None:
                    self.log_callback(
                        f"[DepotDownloaderMod] No output from process for Depot {did}"
                    )
                    logger.error("Depot %s: stdout not available.", did)
                    return

                while True:
                    line = await process.stdout.readline()
                    if not line:
                        break
                    self.log_callback(line.decode(errors="replace").strip())

                await process.wait()
                logger.info(
                    "Depot %s: process exited with code %s.", did, process.returncode
                )
                if process.returncode != 0:
                    raise RuntimeError(
                        f"Process exited with non-zero code {process.returncode}"
                    )

            except asyncio.CancelledError:
                if process and process.returncode is None:
                    try:
                        process.terminate()
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        process.kill()
                        logger.warning("Depot %s: process forcefully killed.", did)
                self.log_callback(f"[DepotDownloaderMod] Stopped Depot {did}")
                logger.info("Depot %s cancelled by user.", did)
                raise

            except FileNotFoundError:
                logger.error("Executable not found: %s", exe_path)
                self.log_callback(
                    f"[DepotDownloaderMod] Executable not found: {exe_path}"
                )
                raise
            except OSError as exc:
                logger.exception("OS error in subprocess for Depot %s.", did)
                self.log_callback(
                    f"[DepotDownloaderMod] OS error in subprocess Depot {did}: {exc}"
                )
                raise
            except Exception as exc:
                logger.exception("Unexpected error in subprocess for Depot %s.", did)
                self.log_callback(
                    f"[DepotDownloaderMod] Unexpected error Depot {did}. See the log for details."
                )
                raise
            finally:
                if local_manifest.exists():
                    try:
                        local_manifest.unlink()
                        logger.debug("Local manifest removed: %s", local_manifest)
                    except OSError as exc:
                        logger.warning(
                            "Cannot remove local manifest %s: %s", local_manifest, exc
                        )
