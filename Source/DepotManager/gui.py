import asyncio
import concurrent.futures
import logging
import os
import shutil
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk
from typing import Optional

import aiohttp

from .api_client import APIAuthError, APIClient, APIHTTPError, APINetworkError
from .config import (
    _BANNER,
    APP_DIR,
    APP_VERSION,
    APPID_MAX,
    APPID_MIN,
    BUNDLE_DIR,
    KEYS_FILE,
    SOURCES,
    load_settings,
    save_settings,
)
from .downloader import DownloadManager
from .lumacore_gui import LumaCoreTab

logger = logging.getLogger("DepotManager.GUI")


class App(tk.Tk):
    """The main Graphical User Interface window for the DepotManager application."""

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

        self.settings: dict = load_settings()
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

        # Notebook: DepotDownloader tab (existing UI) + LumaCore Manager tab.
        # The shared console stays outside the notebook so logs are visible
        # from both tabs; the Start/Stop buttons live in the Actions section
        # of the DepotDownloader tab.
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=5)
        tab_downloader = ttk.Frame(self.notebook)
        self.notebook.add(tab_downloader, text="DepotDownloaderMod Manager")
        self.lumacore_tab = LumaCoreTab(self.notebook, self)
        self.notebook.add(self.lumacore_tab, text="LumaCore Manager")

        top_frame = ttk.LabelFrame(
            tab_downloader, text=" API Configuration ", padding=10
        )
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

        # Actions section: input + fetch/load/select on row 0, Start/Stop on row 1.
        actions_frame = ttk.LabelFrame(tab_downloader, text=" Actions ", padding=10)
        actions_frame.pack(fill="x", padx=10, pady=5)

        ttk.Label(actions_frame, text="Enter AppID:").grid(row=0, column=0, sticky="w")
        self.appid_entry = ttk.Entry(actions_frame, width=15)
        self.appid_entry.grid(row=0, column=1, padx=5, sticky="w")
        self.fetch_btn = ttk.Button(
            actions_frame, text="Fetch Manifest", command=self._on_fetch_click
        )
        self.fetch_btn.grid(row=0, column=2, padx=(0, 5))
        self.load_btn = ttk.Button(
            actions_frame, text="Load Archive...", command=self._on_load_click
        )
        self.load_btn.grid(row=0, column=3, padx=(0, 5))
        ttk.Separator(actions_frame, orient="vertical").grid(
            row=0, column=4, padx=10, sticky="ns"
        )
        ttk.Button(actions_frame, text="☑ All", command=self._select_all).grid(
            row=0, column=5, padx=(0, 3)
        )
        ttk.Button(actions_frame, text="☐ None", command=self._deselect_all).grid(
            row=0, column=6
        )

        self.table_frame = ttk.LabelFrame(
            tab_downloader, text=" Depots Found ", padding=10
        )
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

        # Download action buttons — placed right below the Depots Found table,
        # mirroring the LumaCore Manager tab's Add/Update/Remove button row.
        # Both buttons stay always enabled; guards in the handlers show
        # contextual warnings instead of greying them out.
        download_btns = ttk.Frame(tab_downloader)
        download_btns.pack(fill="x", padx=10, pady=(2, 4))
        self.download_btn = ttk.Button(
            download_btns,
            text="Start Download",
            command=self._on_download_click,
        )
        self.download_btn.pack(side="left", padx=2)
        self.stop_btn = ttk.Button(
            download_btns,
            text="Stop Download",
            command=self._on_stop_click,
        )
        self.stop_btn.pack(side="left", padx=2)

        # Shared console (outside the notebook so logs are visible from both
        # tabs). The Start/Stop buttons now live in the Actions section above.
        bottom_frame = ttk.Frame(self, padding=10)
        bottom_frame.pack(fill="both", padx=10, pady=5)

        self.console = scrolledtext.ScrolledText(
            bottom_frame, height=12, bg="#1e1e1e", fg="#4CAF50", font=("Consolas", 10)
        )
        self.console.pack(fill="both", expand=True, pady=5)

        ttk.Separator(bottom_frame, orient="horizontal").pack(fill="x", pady=(8, 0))

        ttk.Label(
            bottom_frame,
            text=f"DepotManager v{APP_VERSION}",
            foreground="gray",
            font=("Consolas", 8),
        ).pack(anchor="e", pady=(3, 4))

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
        save_settings(self.settings)
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

        if self.session is None:
            raise RuntimeError("HTTP session is not initialized.")

        client = APIClient(self.session, self.settings)
        try:
            temp_dir, local_inv = await client.fetch_manifests(app_id, api_key, source)
            self.after(0, self._update_inventory_and_ui, local_inv, temp_dir)
            self.log_safe("[+] Scan completed.")

        except APIAuthError:
            self.after(
                0,
                lambda: messagebox.showerror(
                    "Auth Error", "API Key rejected by the server."
                ),
            )
        except APIHTTPError as exc:
            self.after(
                0,
                lambda: messagebox.showerror(
                    "HTTP Error", f"Server responded with {exc.status}: {exc.message}"
                ),
            )
        except APINetworkError as exc:
            self.after(
                0,
                lambda: messagebox.showerror(
                    "Network Error", f"Connection failed:\n{exc}"
                ),
            )
        except Exception as exc:
            self.after(
                0, lambda: messagebox.showerror("Error", f"Unexpected error:\n{exc}")
            )
        finally:
            self.after(0, lambda: self.fetch_btn.config(state="normal"))

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

    def _on_load_click(self) -> None:
        from tkinter import filedialog

        file_path = filedialog.askopenfilename(
            title="Select Depot Archive", filetypes=[("ZIP Archives", "*.zip")]
        )
        if not file_path:
            return

        self.load_btn.config(state="disabled")
        self.fetch_btn.config(state="disabled")
        self.run_async(self._load_local_archive(Path(file_path)))

    async def _load_local_archive(self, file_path: Path) -> None:
        self.log_safe(f"[*] Loading local archive: {file_path.name}")

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

        import tempfile

        temp_dir = Path(tempfile.mkdtemp(prefix="depot_manager_"))
        logger.debug("Temporary directory created: %s", temp_dir)
        self._current_temp_dir = temp_dir

        try:
            from .parser import safe_extract, scan_directory

            await asyncio.to_thread(safe_extract, file_path, temp_dir)
            local_inv = await asyncio.to_thread(scan_directory, temp_dir)
            appid_found = await asyncio.to_thread(
                self._find_appid_in_temp_dir, temp_dir, file_path
            )

            self.after(
                0, self._update_local_inventory_and_ui, local_inv, temp_dir, appid_found
            )
            self.log_safe("[+] Local scan completed.")

        except Exception as exc:
            logger.exception("Unexpected error loading local archive.")
            self.after(
                0,
                lambda: messagebox.showerror(
                    "Error", f"Failed to load archive:\n{exc}"
                ),
            )
        finally:
            self.after(0, lambda: self.load_btn.config(state="normal"))
            self.after(0, lambda: self.fetch_btn.config(state="normal"))

    def _find_appid_in_temp_dir(
        self, temp_dir: Path, archive_path: Path
    ) -> Optional[str]:
        archive_stem = archive_path.stem
        if archive_stem.isdigit():
            return archive_stem

        for lua_file in temp_dir.glob("*.lua"):
            if lua_file.stem.isdigit():
                return lua_file.stem

        import re
        from collections import Counter

        re_comment_header = re.compile(r"--\s*(\d+)\'s\s+Lua", re.IGNORECASE)
        re_main_app = re.compile(
            r"--\s*MAIN\s+APPLICATION\s*\r?\n\s*addappid\((\d+)", re.IGNORECASE
        )
        re_fallback = re.compile(r'addappid\((\d+),\s*\d+,\s*"[A-Za-z0-9]+"\)')

        for lua_file in temp_dir.glob("*.lua"):
            try:
                with open(lua_file, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()

                match = re_comment_header.search(content)
                if match:
                    return match.group(1)

                match = re_main_app.search(content)
                if match:
                    return match.group(1)

                matches = re_fallback.findall(content)
                if matches:
                    valid_matches = [m for m in matches if m != "1"]
                    if valid_matches:
                        return valid_matches[0]

            except OSError:
                pass

        return None

    def _update_local_inventory_and_ui(
        self, local_inv: dict, temp_dir: Path, appid: Optional[str]
    ) -> None:
        if appid:
            self.appid_entry.delete(0, tk.END)
            self.appid_entry.insert(0, appid)
            local_inv.pop(str(appid), None)
        else:
            current_appid = self.appid_entry.get().strip()
            if current_appid:
                local_inv.pop(current_appid, None)
            messagebox.showinfo(
                "AppID Required",
                "Archive loaded successfully, but the AppID could not be automatically detected.\n"
                "Please enter the correct AppID manually before downloading.",
            )

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

    # -----------------------------------------------------------------------
    # DOWNLOAD
    # -----------------------------------------------------------------------
    def _on_download_click(self) -> None:
        # Guard: refuse to start while another download is running.
        if self._inner_task is not None and not self._inner_task.done():
            messagebox.showwarning(
                "Download in progress",
                "A download is already running. Stop it first before starting a new one.",
            )
            return

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
        if not app_id.isdigit():
            messagebox.showerror("Error", "Invalid AppID: must be numeric.")
            return

        appid_int = int(app_id)
        if not (APPID_MIN <= appid_int <= APPID_MAX):
            messagebox.showerror(
                "Error", f"AppID out of range ({APPID_MIN} \u2013 {APPID_MAX})."
            )
            return

        # Start/Stop stay enabled; only fetch/load are disabled during download
        # to prevent the inventory/temp_dir from being overwritten mid-run.
        self.fetch_btn.config(state="disabled")
        self.load_btn.config(state="disabled")

        self.download_task = self.run_async(
            self._process_downloads(selected_ids, exe_path, app_id)
        )

    def _on_stop_click(self) -> None:
        if self._inner_task is not None and not self._inner_task.done():
            self.log_safe("⚠️ Stop requested, terminating processes...")
            self.loop.call_soon_threadsafe(self._inner_task.cancel)
        else:
            messagebox.showinfo(
                "No download in progress",
                "There is no active download to stop.",
            )

    async def _process_downloads(
        self, selected_ids: list, exe_path: Path, app_id: str
    ) -> None:
        self._inner_task = asyncio.current_task()

        downloader = DownloadManager(
            self.settings, self.inventory, self._current_temp_dir, self.log_safe
        )

        try:
            await downloader.run_downloads(selected_ids, exe_path, app_id)
            self.after(
                0, lambda: messagebox.showinfo("Completed", "All downloads completed.")
            )
        except asyncio.CancelledError:
            self.after(
                0,
                lambda: messagebox.showwarning(
                    "Cancelled", "Downloads successfully cancelled."
                ),
            )
        except Exception:
            self.after(
                0,
                lambda: messagebox.showwarning(
                    "Completed with errors",
                    "Depot download encountered errors. Check the log for details.",
                ),
            )
        finally:
            self._inner_task = None
            self.download_task = None
            # Start/Stop are always enabled; only fetch/load are re-enabled
            # now that the inventory/temp_dir can be safely replaced.
            self.after(0, lambda: self.fetch_btn.config(state="normal"))
            self.after(0, lambda: self.load_btn.config(state="normal"))
