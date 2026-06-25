import asyncio
import concurrent.futures
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import zipfile
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk
from typing import Optional

import aiohttp

# ---------------------------------------------------------------------------
# CONSTANTS
# ---------------------------------------------------------------------------
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))

if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).parent
else:
    APP_DIR = Path(__file__).parent

SETTINGS_FILE = str(APP_DIR / "settings.json")
KEYS_FILE = str(APP_DIR / "keys.txt")
APP_VERSION = "1.1.1"
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
    "exe_name": "DepotDownloaderMod.exe",
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

_RE_LUA_ADDAPPID = re.compile(r'addappid\((\d+),\s*\d+,\s*"([A-Za-z0-9]+)"\)')
_RE_LUA_TABLE = re.compile(r'\[(\d+)\]\s*=\s*"([A-Za-z0-9]+)"')
_RE_MANIFEST = re.compile(r"^(\d+)_(\d+)\.manifest$")

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------
LOG_FILE = str(APP_DIR / "depot_manager.log")

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("DepotManager")


# ---------------------------------------------------------------------------
# APPLICATION
# ---------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("DepotManager - HighSeas Edition")
        self.geometry("950x780")

        icon_path = BUNDLE_DIR / "icon.ico"
        if icon_path.exists():
            try:
                self.iconbitmap(str(icon_path))
            except tk.TclError:
                pass

        self.settings: dict = self._load_settings()
        self.inventory: dict = {}
        self.checked_depots: dict[str, bool] = {}
        self.download_task: Optional[concurrent.futures.Future] = None
        self._inner_task: Optional[asyncio.Task] = None
        self._current_temp_dir: Optional[Path] = None

        self.log_buffer: list[str] = []
        self.log_timer_active: bool = False

        self._session_ready = threading.Event()
        self.loop = asyncio.new_event_loop()
        self.session: Optional[aiohttp.ClientSession] = None

        threading.Thread(target=self._run_async_loop, daemon=True).start()

        if not self._session_ready.wait(timeout=10):
            logger.critical("HTTP session could not be initialized within 10s.")
            messagebox.showerror("Critical Error", "Cannot start HTTP session.")
            self.destroy()
            return

        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self._setup_ui()
        self._load_settings_into_ui()
        self._print_banner()
        logger.info("Application started successfully.")

    # -----------------------------------------------------------------------
    # LIFECYCLE & ASYNC BRIDGE
    # -----------------------------------------------------------------------
    def _print_banner(self) -> None:
        self.console.insert(tk.END, _BANNER)

    def on_close(self) -> None:
        self._flush_logs()

        async def _cleanup() -> None:
            if self._current_temp_dir and self._current_temp_dir.exists():
                try:
                    await asyncio.to_thread(shutil.rmtree, self._current_temp_dir)
                    logger.info(
                        "Temporary directory removed: %s", self._current_temp_dir
                    )
                except OSError as exc:
                    logger.warning(
                        "Cannot remove temporary directory %s: %s",
                        self._current_temp_dir,
                        exc,
                    )

            keys_path = Path(KEYS_FILE)
            if keys_path.exists():
                try:
                    await asyncio.to_thread(keys_path.unlink)
                    logger.info("Keys file removed: %s", KEYS_FILE)
                except OSError as exc:
                    logger.warning("Cannot remove keys file %s: %s", KEYS_FILE, exc)

            if self.session and not self.session.closed:
                await self.session.close()
                logger.info("HTTP session closed.")

        future = asyncio.run_coroutine_threadsafe(_cleanup(), self.loop)
        try:
            future.result(timeout=5)
        except Exception as exc:
            logger.warning("Error during cleanup on close: %s", exc)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.destroy()

    def _run_async_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._init_session())
        self.loop.run_forever()

    async def _init_session(self) -> None:
        self.session = aiohttp.ClientSession()
        self._session_ready.set()
        logger.debug("HTTP session initialized.")

    def run_async(self, coro) -> concurrent.futures.Future:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    # -----------------------------------------------------------------------
    # SETTINGS
    # -----------------------------------------------------------------------
    def _load_settings(self) -> dict:
        settings = DEFAULT_SETTINGS.copy()
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if "api_key" in loaded and not loaded.get("api_key_morrenus"):
                    loaded["api_key_morrenus"] = loaded.pop("api_key")
                else:
                    loaded.pop("api_key", None)
                if "api_base_url" in loaded and not loaded.get("api_base_url_morrenus"):
                    loaded["api_base_url_morrenus"] = loaded.pop("api_base_url")
                else:
                    loaded.pop("api_base_url", None)
                settings.update(loaded)
                logger.debug("Settings loaded from %s.", SETTINGS_FILE)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Cannot read settings file: %s. Using defaults.", exc)
        return settings

    def _save_settings(self) -> None:
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, indent=4)
            logger.debug("Settings saved to %s.", SETTINGS_FILE)
        except OSError as exc:
            logger.error("Cannot save settings: %s", exc)
            messagebox.showerror("I/O Error", f"Cannot save settings:\n{exc}")

    # -----------------------------------------------------------------------
    # LOGGING UI
    # -----------------------------------------------------------------------
    def log_safe(self, message: str) -> None:
        logger.info(message)
        self.after(0, self._append_to_log, message)

    def _append_to_log(self, message: str) -> None:
        self.log_buffer.append(message)
        if not self.log_timer_active:
            self.log_timer_active = True
            self.after(150, self._flush_logs)

    def _flush_logs(self) -> None:
        if self.log_buffer:
            messages = "\n".join(self.log_buffer) + "\n"
            self.console.insert(tk.END, messages)
            self.console.see(tk.END)
            self.log_buffer.clear()
        self.log_timer_active = False

    # -----------------------------------------------------------------------
    # GRAPHICAL INTERFACE
    # -----------------------------------------------------------------------
    def _setup_ui(self) -> None:
        ttk.Style()

        top_frame = ttk.LabelFrame(self, text=" Configuration ", padding=10)
        top_frame.pack(fill="x", padx=10, pady=5)

        ttk.Label(top_frame, text="Source:").grid(row=0, column=0, sticky="w")
        self.source_var = tk.StringVar()
        self.source_combo = ttk.Combobox(
            top_frame,
            textvariable=self.source_var,
            values=[info["label"] for info in SOURCES.values()],
            state="readonly",
            width=20,
        )
        self.source_combo.grid(row=0, column=1, padx=5, sticky="w")
        self.source_combo.bind("<<ComboboxSelected>>", self._on_source_change)

        ttk.Label(top_frame, text="API Key:").grid(row=1, column=0, sticky="w")
        self.api_key_entry = ttk.Entry(top_frame, width=50, show="*")
        self.api_key_entry.grid(row=1, column=1, padx=5)
        ttk.Button(top_frame, text="Save Key", command=self._save_api_key).grid(
            row=1, column=2
        )

        mid_frame = ttk.Frame(self, padding=10)
        mid_frame.pack(fill="x", padx=10)

        ttk.Label(mid_frame, text="Enter AppID:").pack(side="left")
        self.appid_entry = ttk.Entry(mid_frame, width=15)
        self.appid_entry.pack(side="left", padx=5)
        self.fetch_btn = ttk.Button(
            mid_frame, text="Fetch Manifest", command=self._on_fetch_click
        )
        self.fetch_btn.pack(side="left")
        ttk.Separator(mid_frame, orient="vertical").pack(side="left", padx=10, fill="y")
        ttk.Button(mid_frame, text="☑ All", command=self._select_all).pack(
            side="left", padx=(0, 3)
        )
        ttk.Button(mid_frame, text="☐ None", command=self._deselect_all).pack(
            side="left"
        )

        self.table_frame = ttk.LabelFrame(self, text=" Depots Found ", padding=10)
        self.table_frame.pack(fill="both", expand=True, padx=10, pady=5)

        columns = ("check", "id", "status", "key", "manifest")
        self.tree = ttk.Treeview(
            self.table_frame, columns=columns, show="headings", selectmode="none"
        )
        for col, text in zip(
            columns, ["", "Depot ID", "Status", "Key", "Manifest File"]
        ):
            self.tree.heading(col, text=text)
        self.tree.column("check", width=30, anchor="center", stretch=False)
        self.tree.column("id", width=100)
        self.tree.column("status", width=120)

        self.tree.bind("<ButtonRelease-1>", self._on_tree_click)

        scrollbar = ttk.Scrollbar(
            self.table_frame, orient="vertical", command=self.tree.yview
        )
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        bottom_frame = ttk.Frame(self, padding=10)
        bottom_frame.pack(fill="both", padx=10, pady=5)

        self.console = scrolledtext.ScrolledText(
            bottom_frame, height=12, bg="#1e1e1e", fg="#4CAF50", font=("Consolas", 10)
        )
        self.console.pack(fill="both", expand=True, pady=5)

        btn_frame = ttk.Frame(bottom_frame)
        btn_frame.pack(fill="x")

        ttk.Separator(bottom_frame, orient="horizontal").pack(fill="x", pady=(8, 0))

        ttk.Label(
            bottom_frame,
            text=f"DepotManager v{APP_VERSION}",
            foreground="gray",
            font=("Consolas", 8),
        ).pack(anchor="e", pady=(3, 4))

        self.download_btn = ttk.Button(
            btn_frame,
            text="▶ START DOWNLOAD",
            command=self._on_download_click,
            state="disabled",
        )
        self.download_btn.pack(side="left", fill="x", expand=True, padx=(0, 5))

        self.stop_btn = ttk.Button(
            btn_frame, text="🛑 STOP", command=self._on_stop_click, state="disabled"
        )
        self.stop_btn.pack(side="right", fill="x", expand=True, padx=(5, 0))

    def _load_settings_into_ui(self) -> None:
        selected = self.settings.get("selected_source", "morrenus")
        if selected not in SOURCES:
            selected = "morrenus"
        self.source_var.set(SOURCES[selected]["label"])
        key_field = SOURCES[selected]["key_field"]
        self.api_key_entry.insert(0, self.settings.get(key_field, ""))

    def _get_selected_source_key(self) -> str:
        label = self.source_var.get()
        for key, info in SOURCES.items():
            if info["label"] == label:
                return key
        return "morrenus"

    def _on_source_change(self, event=None) -> None:
        source_key = self._get_selected_source_key()
        self.settings["selected_source"] = source_key
        self.api_key_entry.delete(0, tk.END)
        key_field = SOURCES[source_key]["key_field"]
        self.api_key_entry.insert(0, self.settings.get(key_field, ""))

    def _save_api_key(self) -> None:
        key = self.api_key_entry.get().strip()
        if len(key) < 10:
            messagebox.showwarning("Warning", "The API Key seems too short.")
            return
        source_key = self._get_selected_source_key()
        key_field = SOURCES[source_key]["key_field"]
        self.settings[key_field] = key
        self.settings["selected_source"] = source_key
        self._save_settings()
        messagebox.showinfo("Success", "Settings saved.")

    def _on_tree_click(self, event: tk.Event) -> None:
        region = self.tree.identify_region(event.x, event.y)
        if region != "cell":
            return
        col = self.tree.identify_column(event.x)
        if col != "#1":
            return
        row = self.tree.identify_row(event.y)
        if not row:
            return
        did = str(self.tree.item(row)["values"][1])
        self.checked_depots[did] = not self.checked_depots.get(did, False)
        symbol = "☑" if self.checked_depots[did] else "☐"
        values = list(self.tree.item(row)["values"])
        values[0] = symbol
        self.tree.item(row, values=values)

    def _select_all(self) -> None:
        for row in self.tree.get_children():
            did = str(self.tree.item(row)["values"][1])
            self.checked_depots[did] = True
            values = list(self.tree.item(row)["values"])
            values[0] = "☑"
            self.tree.item(row, values=values)

    def _deselect_all(self) -> None:
        for row in self.tree.get_children():
            did = str(self.tree.item(row)["values"][1])
            self.checked_depots[did] = False
            values = list(self.tree.item(row)["values"])
            values[0] = "☐"
            self.tree.item(row, values=values)

    # -----------------------------------------------------------------------
    # FETCH & SCAN
    # -----------------------------------------------------------------------
    def _on_fetch_click(self) -> None:
        appid_str = self.appid_entry.get().strip()
        source_key = self._get_selected_source_key()
        key_field = SOURCES[source_key]["key_field"]
        key = (
            self.settings.get(key_field, "").strip() or self.api_key_entry.get().strip()
        )

        if not appid_str.isdigit():
            messagebox.showerror("Error", "Invalid AppID: must be numeric.")
            return
        appid_int = int(appid_str)
        if not (APPID_MIN <= appid_int <= APPID_MAX):
            messagebox.showerror(
                "Error", f"AppID out of range ({APPID_MIN} \u2013 {APPID_MAX})."
            )
            return
        if len(key) < 10:
            messagebox.showerror("Error", "Missing or invalid API Key.")
            return

        self.fetch_btn.config(state="disabled")
        self.run_async(self._fetch_and_scan(appid_str, key, source_key))

    async def _fetch_and_scan(self, app_id: str, api_key: str, source: str) -> None:
        self.log_safe(
            f"[*] API request for AppID: {app_id} (source: {SOURCES[source]['label']})"
        )
        timeout = aiohttp.ClientTimeout(total=self.settings["request_timeout"])

        if source == "ryuu":
            url = self.settings["api_base_url_ryuu"]
            headers = {"User-Agent": "DepotManager/2.0"}
            params: Optional[dict] = {"appid": app_id, "auth_code": api_key}
        else:
            base = self.settings["api_base_url_morrenus"].rstrip("/")
            url = f"{base}/manifest/{app_id}"
            headers = {"User-Agent": "DepotManager/2.0", "X-API-Key": api_key}
            params = None

        if self._current_temp_dir and self._current_temp_dir.exists():
            try:
                await asyncio.to_thread(shutil.rmtree, self._current_temp_dir)
                logger.debug(
                    "Old temporary directory removed: %s", self._current_temp_dir
                )
            except OSError as exc:
                logger.warning(
                    "Cannot remove old temp_dir %s: %s", self._current_temp_dir, exc
                )

        temp_dir = Path(tempfile.mkdtemp(prefix="depot_manager_"))
        self._current_temp_dir = temp_dir
        logger.debug("Temporary directory created: %s", temp_dir)

        if self.session is None:
            raise RuntimeError("HTTP session is not initialized.")

        try:
            async with self.session.get(
                url, headers=headers, params=params, timeout=timeout
            ) as r:
                if r.status in (401, 403):
                    self.after(
                        0,
                        lambda: messagebox.showerror(
                            "Auth Error", "API Key rejected by the server."
                        ),
                    )
                    logger.warning(
                        "API Key rejected (HTTP %s) for AppID %s.", r.status, app_id
                    )
                    return
                try:
                    r.raise_for_status()
                except aiohttp.ClientResponseError as exc:
                    logger.error("HTTP error %s: %s", exc.status, exc.message)
                    self.after(
                        0,
                        lambda exc=exc: messagebox.showerror(
                            "HTTP Error",
                            f"Server responded with {exc.status}: {exc.message}",
                        ),
                    )
                    return

                data = await r.read()

            zip_path = temp_dir / "data.zip"
            await asyncio.to_thread(self._write_file, zip_path, data)
            await asyncio.to_thread(self._safe_extract, zip_path, temp_dir)

            local_inv = await asyncio.to_thread(self._scan_directory, temp_dir)
            self.after(0, self._update_inventory_and_ui, local_inv, temp_dir)
            self.log_safe("[+] Scan completed.")

        except aiohttp.ClientConnectionError as exc:
            logger.exception("Connection error during fetch.")
            self.after(
                0,
                lambda exc=exc: messagebox.showerror(
                    "Network Error", f"Connection failed:\n{exc}"
                ),
            )
        except aiohttp.ClientError as exc:
            logger.exception("aiohttp client error.")
            self.after(0, lambda exc=exc: messagebox.showerror("HTTP Error", str(exc)))
        except zipfile.BadZipFile:
            logger.exception("Downloaded file is not a valid ZIP.")
            self.after(
                0,
                lambda: messagebox.showerror(
                    "Error", "The downloaded file is not a valid ZIP archive."
                ),
            )
        except Exception as exc:
            logger.exception("Unexpected error in _fetch_and_scan.")
            self.after(
                0,
                lambda exc=exc: messagebox.showerror(
                    "Error", f"Unexpected error:\n{exc}"
                ),
            )
        finally:
            self.after(0, lambda: self.fetch_btn.config(state="normal"))

    # -----------------------------------------------------------------------
    # UTILITY I/O
    # -----------------------------------------------------------------------
    @staticmethod
    def _write_file(path: Path, data: bytes) -> None:
        with open(path, "wb") as f:
            f.write(data)

    @staticmethod
    def _safe_extract(zip_path: Path, extract_to: Path) -> None:
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

    @staticmethod
    def _scan_directory(temp_dir: Path) -> dict:
        inv: dict = {}

        for lua_file in temp_dir.glob("*.lua"):
            try:
                with open(lua_file, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                if "\ufffd" in content:
                    logger.warning(
                        "Lua file with problematic encoding: %s", lua_file.name
                    )

            except OSError as exc:
                logger.warning("Cannot read %s: %s", lua_file.name, exc)
                continue

            matches = _RE_LUA_ADDAPPID.findall(content) or _RE_LUA_TABLE.findall(
                content
            )

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

    def _update_inventory_and_ui(self, local_inv: dict, temp_dir: Path) -> None:
        app_id = self.appid_entry.get().strip()
        local_inv.pop(app_id, None)
        self.table_frame.config(text=f" Depots Found ({len(local_inv)}) ")

        self.inventory = local_inv
        self._current_temp_dir = temp_dir
        self.checked_depots = {}

        for item in self.tree.get_children():
            self.tree.delete(item)

        for did, info in sorted(self.inventory.items()):
            self.checked_depots[did] = False
            status = (
                "✅ READY" if info["key"] and info["manifest_file"] else "⚠️ INCOMPLETE"
            )
            manifest_name = (
                info["manifest_file"].name if info["manifest_file"] else "Missing"
            )
            self.tree.insert(
                "",
                tk.END,
                values=("☐", did, status, info["key"] or "Missing", manifest_name),
            )

        self.download_btn.config(state="normal")

    # -----------------------------------------------------------------------
    # DOWNLOAD
    # -----------------------------------------------------------------------
    def _on_download_click(self) -> None:
        exe_name = self.settings["exe_name"]
        exe_path = (
            Path(exe_name) if Path(exe_name).is_absolute() else APP_DIR / exe_name
        )

        if not exe_path.exists():
            messagebox.showerror("Exec Error", f"Executable not found: {exe_path}")
            return

        selected_ids = [did for did, checked in self.checked_depots.items() if checked]
        if not selected_ids:
            messagebox.showwarning(
                "Warning", "Select at least one depot using the checkboxes."
            )
            return

        app_id = self.appid_entry.get().strip()

        self.download_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.fetch_btn.config(state="disabled")

        self.download_task = self.run_async(
            self._process_downloads(selected_ids, exe_path, app_id)
        )

    def _on_stop_click(self) -> None:
        if self._inner_task and not self._inner_task.done():
            self.stop_btn.config(state="disabled")
            self.log_safe("⚠️ Stop requested, terminating processes...")
            self.loop.call_soon_threadsafe(self._inner_task.cancel)

    async def _process_downloads(
        self, selected_ids: list, exe_path: Path, app_id: str
    ) -> None:
        self._inner_task = asyncio.current_task()

        keys_to_write = {
            str(did): self.inventory[str(did)]["key"]
            for did in selected_ids
            if self.inventory[str(did)]["key"]
        }
        await asyncio.to_thread(self._write_keys_file, keys_to_write)

        max_concurrent = self.settings["max_concurrent_downloads"]
        sem = asyncio.Semaphore(max_concurrent)

        tasks = [
            asyncio.create_task(self._download_single(did, exe_path, app_id, sem))
            for did in selected_ids
        ]

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
                self.log_safe("--- 🛑 OPERATION CANCELLED BY USER ---")
                self.after(
                    0,
                    lambda: messagebox.showwarning(
                        "Cancelled", "Downloads successfully cancelled."
                    ),
                )
            elif errors:
                self.log_safe(f"--- ⚠️ COMPLETED WITH {len(errors)} ERRORS ---")
                self.after(
                    0,
                    lambda: messagebox.showwarning(
                        "Completed with errors",
                        f"{len(errors)} depots encountered errors. Check the log for details.",
                    ),
                )
            else:
                self.log_safe("--- ✅ ALL SELECTED DOWNLOADS COMPLETED ---")
                self.after(
                    0,
                    lambda: messagebox.showinfo(
                        "Completed", "All downloads completed."
                    ),
                )

        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()
            self.log_safe("--- 🛑 PARENT TASK CANCELLED ---")
        except Exception:
            logger.exception("Unexpected error in _process_downloads.")
        finally:
            self._inner_task = None
            self.download_task = None
            self.after(0, lambda: self.download_btn.config(state="normal"))
            self.after(0, lambda: self.stop_btn.config(state="disabled"))
            self.after(0, lambda: self.fetch_btn.config(state="normal"))

    async def _download_single(
        self, did: str, exe_path: Path, app_id: str, sem: asyncio.Semaphore
    ) -> None:
        info = self.inventory[str(did)]
        if not info["manifest_file"]:
            logger.warning("Depot %s: no manifest file, skipping.", did)
            return

        manifest_src: Path = info["manifest_file"]

        if self._current_temp_dir is None:
            raise RuntimeError("No temporary directory is set.")

        if not manifest_src.is_absolute():
            manifest_src = self._current_temp_dir / manifest_src.name

        match = re.search(r"_(\d+)\.manifest$", manifest_src.name)
        if not match:
            logger.warning(
                "Depot %s: unparsable manifest name (%s), skipping.",
                did,
                manifest_src.name,
            )
            return

        manifest_id = match.group(1)
        local_manifest = APP_DIR / manifest_src.name

        try:
            await asyncio.to_thread(shutil.copy, str(manifest_src), str(local_manifest))
        except OSError as exc:
            logger.error("Cannot copy manifest for depot %s: %s", did, exc)
            self.log_safe(f"❌ Error copying manifest Depot {did}: {exc}")
            return

        async with sem:
            self.log_safe(f"\n>>> Starting download Depot {did}...")
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
                "16",
            ]
            logger.debug("Command: %s", " ".join(cmd))

            process: Optional[asyncio.subprocess.Process] = None
            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )

                if process.stdout is None:
                    self.log_safe(f"❌ No output from process for Depot {did}")
                    logger.error("Depot %s: stdout not available.", did)
                    return

                while True:
                    line = await process.stdout.readline()
                    if not line:
                        break
                    self.log_safe(line.decode(errors="replace").strip())

                await process.wait()
                logger.info(
                    "Depot %s: process exited with code %s.", did, process.returncode
                )

            except asyncio.CancelledError:
                if process and process.returncode is None:
                    try:
                        process.terminate()
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        process.kill()
                        logger.warning("Depot %s: process forcefully killed.", did)
                self.log_safe(f"🛑 Stopped Depot {did}")
                logger.info("Depot %s cancelled by user.", did)
                raise

            except FileNotFoundError:
                logger.error("Executable not found: %s", exe_path)
                self.log_safe(f"❌ Executable not found: {exe_path}")
            except OSError as exc:
                logger.exception("OS error in subprocess for Depot %s.", did)
                self.log_safe(f"❌ OS error in subprocess Depot {did}: {exc}")
            except Exception:
                logger.exception("Unexpected error in subprocess for Depot %s.", did)
                self.log_safe(
                    f"❌ Unexpected error Depot {did}. See the log for details."
                )
            finally:
                if local_manifest.exists():
                    try:
                        local_manifest.unlink()
                        logger.debug("Local manifest removed: %s", local_manifest)
                    except OSError as exc:
                        logger.warning(
                            "Cannot remove local manifest %s: %s", local_manifest, exc
                        )

    @staticmethod
    def _write_keys_file(keys_dict: dict) -> None:
        try:
            with open(KEYS_FILE, "w", encoding="utf-8") as f:
                for did, key in keys_dict.items():
                    f.write(f"{did};{key}\n")
            logger.debug("Keys file written: %d keys.", len(keys_dict))
        except OSError as exc:
            logger.error("Cannot write keys file: %s", exc)
            raise


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app = App()
    app.mainloop()
