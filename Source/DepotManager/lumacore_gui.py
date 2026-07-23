"""Tkinter UI for the LumaCore Manager tab and Add Game dialog."""

from __future__ import annotations

import asyncio
import logging
import shutil
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from .widgets import CustomMessageBox, ToolTip
from typing import TYPE_CHECKING, Callable, Optional

from . import lumacore_games, lumacore_setup, steam_process
from .api_client import APIAuthError, APIClient, APIHTTPError, APINetworkError
from .config import APP_DIR, APPID_MAX, APPID_MIN, SOURCES, save_settings
from .downloader import DownloadManager
from .lumacore_setup import get_installed_version as _lc_installed_version
from .parser import scan_directory
from .steam_path import (
    find_acf_for_app,
    get_steam_libraries,
    get_steam_path,
    library_label,
    pick_library_default,
    sanitize_installdir,
    set_steam_path,
)
from .vdf_io import read_acf as _read_acf_vdf

logger = logging.getLogger("DepotManager.LumaCoreGUI")

if TYPE_CHECKING:  # pragma: no cover
    from .gui import App


# --- LUMACORE TAB ---
class LumaCoreTab(ttk.Frame):
    """The LumaCore Manager tab. Inserted into the main window's Notebook."""

    def __init__(
        self,
        parent: tk.Widget,
        app: "App",
    ) -> None:
        super().__init__(parent, padding=6)
        self.app = app
        self.settings = app.settings
        self.log = app.log_safe
        self._add_dialog: Optional["AddGameDialog"] = None
        self._setup_ui()
        self.after(50, self._refresh_steam_path_display)
        self.after(100, self._refresh_games_list)
        self._auto_check_updates()

    def _setup_ui(self) -> None:
        steam_frame = ttk.LabelFrame(self, text=" Steam Installation ", padding=8)
        steam_frame.pack(fill="x", padx=4, pady=4)

        ttk.Label(steam_frame, text="Path:").grid(row=0, column=0, sticky="w")
        self.steam_path_var = tk.StringVar(value="(not detected)")
        ttk.Entry(
            steam_frame, textvariable=self.steam_path_var, state="readonly", width=60
        ).grid(row=0, column=1, padx=5, sticky="we")
        ttk.Button(steam_frame, text="Detect", command=self._on_detect_steam).grid(
            row=0, column=2
        )
        ttk.Button(steam_frame, text="Browse...", command=self._on_browse_steam).grid(
            row=0, column=3, padx=(3, 0)
        )
        steam_frame.columnconfigure(1, weight=1)

        ttk.Label(steam_frame, text="SteamID32:").grid(
            row=1, column=0, sticky="w", pady=(5, 0)
        )

        id_container = ttk.Frame(steam_frame)
        id_container.grid(row=1, column=1, columnspan=3, pady=(5, 0), sticky="w")

        self.steam_id_var = tk.StringVar(value=self.settings.get("steam_id_32", ""))
        self.steam_id_entry = ttk.Entry(
            id_container, textvariable=self.steam_id_var, width=15
        )
        self.steam_id_entry.pack(side="left", padx=2)
        ToolTip(
            self.steam_id_entry,
            "SteamID32 of the user profile to apply Steam Cloud Fix to.\n"
            "Useful when managing multiple local Steam profiles.",
        )

        ttk.Label(id_container, text="  Steam API Key:").pack(side="left", padx=2)
        self.steam_api_key_var = tk.StringVar(
            value=self.settings.get("steam_api_key", "")
        )
        self.steam_api_key_entry = ttk.Entry(
            id_container, textvariable=self.steam_api_key_var, width=32, show="*"
        )
        self.steam_api_key_entry.pack(side="left", padx=2)
        ToolTip(
            self.steam_api_key_entry,
            "Steam Web API Key used to fetch achievement schemas.\n"
            "Required for complete achievement data when adding or updating games.",
        )

        ttk.Button(
            id_container, text="Save", command=self._on_save_steam_credentials
        ).pack(side="left", padx=5)

        lc_frame = ttk.LabelFrame(self, text=" LumaCore Component ", padding=8)
        lc_frame.pack(fill="x", padx=4, pady=4)

        self.lc_status_var = tk.StringVar(value="Installed: ?  |  Latest: ?")
        ttk.Label(lc_frame, textvariable=self.lc_status_var).pack(
            anchor="w", pady=(0, 8)
        )

        button_row = ttk.Frame(lc_frame)
        button_row.pack(anchor="w", fill="x")

        # 1. LumaCore Component
        ttk.Button(
            button_row, text="Check For Updates", command=self._on_check_update
        ).pack(side="left", padx=2)
        ttk.Button(button_row, text="Install / Update", command=self._on_install).pack(
            side="left", padx=2
        )
        ttk.Button(button_row, text="Uninstall", command=self._on_uninstall).pack(
            side="left", padx=2
        )

        ttk.Separator(button_row, orient="vertical").pack(
            side="left", fill="y", padx=10
        )

        # 2. Utility Steam
        ttk.Button(
            button_row, text="Restart Steam", command=self._on_restart_steam
        ).pack(side="left", padx=2)
        ttk.Button(
            button_row, text="Steam Cloud Fix", command=self._on_steam_cloud_fix
        ).pack(side="left", padx=2)

        games_frame = ttk.LabelFrame(self, text=" Managed Games ", padding=8)
        games_frame.pack(fill="both", expand=True, padx=4, pady=4)

        columns = ("appid", "name", "depots", "acf")
        self.games_tree = ttk.Treeview(
            games_frame, columns=columns, show="headings", selectmode="browse"
        )
        for col, text in zip(
            columns,
            ("AppID", "Name", "#Depots", "ACF"),
        ):
            self.games_tree.heading(col, text=text)
        self.games_tree.column("appid", width=100, anchor="w", stretch=False)
        self.games_tree.column("name", width=150, anchor="w")
        self.games_tree.column("depots", width=100, anchor="e", stretch=False)
        self.games_tree.column("acf", width=100, anchor="center", stretch=False)

        self.games_tree.pack(side="left", fill="both", expand=True)

        sb = ttk.Scrollbar(
            games_frame, orient="vertical", command=self.games_tree.yview
        )
        self.games_tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")

        games_btns = ttk.Frame(self)
        games_btns.pack(fill="x", padx=4, pady=(2, 4))
        ttk.Button(games_btns, text="Add Game", command=self._on_add_game).pack(
            side="left", padx=2
        )
        ttk.Button(
            games_btns, text="Update Selected", command=self._on_update_game
        ).pack(side="left", padx=2)
        ttk.Button(
            games_btns, text="Remove Selected", command=self._on_remove_game
        ).pack(side="left", padx=2)
        ttk.Button(
            games_btns, text="Refresh List", command=self._refresh_games_list
        ).pack(side="left", padx=2)

    # --- STEAM PATH/ID32 ---
    def _current_steam_path(self) -> Optional[Path]:
        return get_steam_path(self.settings)

    def _is_lumacore_installed(self) -> bool:
        steam = self._current_steam_path()
        if steam is None:
            return False
        return bool(_lc_installed_version(self.settings, steam))

    def _warn_lumacore_not_installed(self, action: str) -> None:
        self.log(
            "[LumaCore] Cannot " + action + ": LumaCore is not installed. "
            "Use 'Install / Update' in the LumaCore Component section first."
        )
        messagebox.showwarning(
            "LumaCore Not Installed",
            "LumaCore is not installed in your Steam directory.\n\n"
            "This action requires the LumaCore DLLs to be present in the Steam "
            "folder.\n\n"
            "Use the 'Install / Update' button in the LumaCore Component "
            "section to set it up, then try again.",
        )

    def _refresh_steam_path_display(self) -> None:
        p = self._current_steam_path()
        self.steam_path_var.set(str(p) if p else "(not detected)")

    def _on_detect_steam(self) -> None:
        self.settings["steam_path"] = ""
        p = get_steam_path(self.settings)
        if p:
            self.settings["steam_path"] = str(p)
            save_settings(self.settings)
            self._refresh_steam_path_display()
            self.log(f"[LumaCore] Steam detected: {p}")
        else:
            self.log("[LumaCore] Steam not found. Use Browse... to set it manually.")
            messagebox.showwarning(
                "Steam Not Found",
                "Could not locate Steam automatically. Please use Browse... to select "
                "your Steam installation directory (the one containing steam.exe).",
            )

    def _on_browse_steam(self) -> None:
        choice = filedialog.askdirectory(
            title="Select Steam installation directory",
            initialdir=r"C:\Program Files (x86)",
        )
        if not choice:
            return
        path = Path(choice)
        if not (path / "steam.exe").is_file() and not (path / "steamapps").is_dir():
            choice = CustomMessageBox(
                self,
                "Unlikely Steam Path",
                f"The selected folder does not look like a Steam installation\n"
                f"(no steam.exe or steamapps/ found):\n\n  {path}",
                [("Use Anyway", "use"), ("Cancel", None)],
            ).result
            if choice != "use":
                return
        set_steam_path(self.settings, path)
        save_settings(self.settings)
        self._refresh_steam_path_display()
        self.log(f"[LumaCore] Steam path set manually: {path}")

    def _on_save_steam_credentials(self) -> None:
        steam_id = self.steam_id_var.get().strip()
        api_key = self.steam_api_key_var.get().strip()

        if steam_id and not steam_id.isdigit():
            messagebox.showerror("Invalid SteamID32", "The SteamID32 must be numeric.")
            return

        self.settings["steam_id_32"] = steam_id
        self.settings["steam_api_key"] = api_key
        save_settings(self.settings)
        self.log(
            f"[LumaCore] Saved credentials (ID: {steam_id or '(cleared)'}, "
            f"API Key: {'[SET]' if api_key else '(empty)'})"
        )
        messagebox.showinfo("Saved", "Credentials saved successfully.")

    # --- LUMACORE COMPONENT ---
    def _auto_check_updates(self) -> None:
        self.app.run_async(self._check_update_async())

    def _on_check_update(self) -> None:
        self.app.run_async(self._check_update_async())

    async def _check_update_async(self) -> None:
        self.log("[LumaCore] Checking for updates...")
        try:
            info = await lumacore_setup.check_for_update(
                self.settings, self.app.session, force=True
            )
        except Exception as exc:
            self.log(f"[LumaCore] Update check failed: {exc}")
            return
        installed = info["installed"]
        latest = info["latest"] or "unknown"
        self.lc_status_var.set(
            f"Installed: {installed or 'not installed'}  |  Latest: {latest}"
        )
        if not installed:
            self.log(
                f"[LumaCore] LumaCore {latest} is available to install. "
                f"(Source: {info['source']})"
            )
        elif info["update_available"]:
            self.log(
                f"[LumaCore] Update available: {latest} (installed: {installed}). "
                f"Source: {info['source']}."
            )
        else:
            self.log(f"[LumaCore] Up to date ({installed}). Source: {info['source']}.")

    def _on_install(self) -> None:
        steam = self._current_steam_path()
        if steam is None:
            messagebox.showerror("Steam Missing", "Set the Steam path first.")
            return
        choice = CustomMessageBox(
            self,
            "Install LumaCore",
            f"This will:\n"
            f"  - Close Steam\n"
            f"  - Download the latest LumaCore from GitHub\n"
            f"  - Place LumaCore DLLs in:\n     {steam}\n\n"
            f"Restart Steam manually afterwards.",
            [("Install", "install"), ("Cancel", None)],
        ).result
        if choice != "install":
            return
        self.app.run_async(self._install_async(steam))

    async def _install_async(self, steam: Path) -> None:
        self.log("[LumaCore] Installing... (Steam will be closed)")

        def cb(pct: int, total: int, msg: str) -> None:
            self.log(f"[LumaCore] {pct}% - {msg}")

        try:
            ok, message = await lumacore_setup.install_lumacore(
                steam, self.app.session, self.settings, progress_cb=cb
            )
        except Exception as exc:
            self.log(f"[LumaCore] Install error: {exc}")
            return
        self.log(f"[LumaCore] {message}")
        if ok:
            await self._check_update_async()
        else:
            messagebox.showerror("Install Failed", message)

    def _on_uninstall(self) -> None:
        steam = self._current_steam_path()
        if steam is None:
            messagebox.showerror("Steam Missing", "Set the Steam path first.")
            return

        installed = self._is_lumacore_installed()
        try:
            games = lumacore_games.list_installed_games(steam)
        except Exception:
            games = []
        games_count = len(games)

        if installed and games_count > 0:
            listing = "\n".join(f"  - {g['appid']}: {g['name']}" for g in games[:20])
            if games_count > 20:
                listing += f"\n  ... and {games_count - 20} more"

            msg = (
                f"Close Steam, remove LumaCore DLLs, and remove "
                f"{games_count} managed game(s).\n\n"
                f"WARNING: If you select 'Full Uninstall' and you have legitimately purchased any of these games\n"
                f"AFTER injecting them via DepotManager, their install state (ACF)\n"
                f"will be deleted. Steam will show them as not installed. Use\n"
                f"Steam's 'Verify integrity of game files' to rebuild the ACF.\n\n"
                f"{listing}"
            )
            response = CustomMessageBox(
                self,
                "Uninstall LumaCore",
                msg,
                [
                    ("Full Uninstall", "full"),
                    ("Deactivate", "normal"),
                    ("Cancel", None),
                ],
            ).result
        elif installed and games_count == 0:
            msg = (
                "No managed games found.\n"
                "Close Steam and remove LumaCore DLLs."
            )
            response = CustomMessageBox(
                self,
                "Uninstall LumaCore",
                msg,
                [("Uninstall", "full"), ("Cancel", None)],
            ).result
        elif not installed and games_count == 0:
            messagebox.showinfo(
                "Nothing To Do",
                "LumaCore is not installed and no managed games were found.\nThere is nothing to uninstall or clean up.",
            )
            return
        else:
            listing = "\n".join(f"  - {g['appid']}: {g['name']}" for g in games[:20])
            if games_count > 20:
                listing += f"\n  ... and {games_count - 20} more"
            msg = (
                f"LumaCore is not installed, but {games_count} managed game(s)\n"
                f"were found (left over from a previous install):\n\n{listing}\n\n"
                f"Clean up their files (lua, manifests, ACF, depot keys)?\n"
                f"Steam will NOT be closed, these files are not locked."
            )
            response = CustomMessageBox(
                self,
                "Clean Up Orphan Game Files",
                msg,
                [("Clean Up", "full"), ("Cancel", None)],
            ).result

        if response == "full":
            self.app.run_async(self._uninstall_async(steam, complete=True))
        elif response == "normal":
            self.app.run_async(self._uninstall_async(steam, complete=False))

    async def _uninstall_async(self, steam: Path, complete: bool) -> None:
        self.log("[LumaCore] Uninstalling... (Steam will be closed)")

        def cb(pct: int, total: int, msg: str) -> None:
            self.log(f"[LumaCore] {pct}% - {msg}")

        try:
            ok, message = await asyncio.to_thread(
                lumacore_setup.uninstall_lumacore,
                steam,
                self.settings,
                cb,
                complete,
            )
        except Exception as exc:
            self.log(f"[LumaCore] Uninstall error: {exc}")
            return
        self.log(f"[LumaCore] {message}")
        if ok:
            self.lc_status_var.set("Installed: not installed  |  Latest: ?")
            self._refresh_games_list()
        else:
            messagebox.showerror("Uninstall Failed", message)

    def _on_restart_steam(self) -> None:
        choice = CustomMessageBox(
            self,
            "Restart Steam",
            "This will force-close Steam and restart it immediately.",
            [("Restart", "restart"), ("Cancel", None)],
        ).result
        if choice != "restart":
            return

        self.log("[LumaCore] Restarting Steam...")
        self.app.run_async(self._restart_steam_async())

    async def _restart_steam_async(self) -> None:
        try:
            success = await asyncio.to_thread(
                steam_process.restart_steam, self.settings
            )
            if success:
                self.log("[LumaCore] Steam restarted successfully.")
            else:
                self.log("[LumaCore] Failed to restart Steam.")
                messagebox.showerror(
                    "Restart Failed",
                    "Could not restart Steam. Ensure the path is correct.",
                )
        except Exception as exc:
            self.log(f"[LumaCore] Restart error: {exc}")
            messagebox.showerror(
                "Restart Failed",
                f"Failed to restart Steam:\n{exc}\n\nSee depot_manager.log for details.",
            )

    # --- MANAGED GAMES ---
    def _refresh_games_list(self) -> None:
        for row in self.games_tree.get_children():
            self.games_tree.delete(row)
        steam = self._current_steam_path()
        if steam is None:
            return
        try:
            games = lumacore_games.list_installed_games(steam)
        except Exception as exc:
            self.log(f"[LumaCore] Cannot list games: {exc}")
            return
        self.log(f"[LumaCore] Scan complete: found {len(games)} managed game(s).")
        for g in games:
            self.games_tree.insert(
                "",
                tk.END,
                values=(
                    g["appid"],
                    g["name"],
                    g["depot_count"],
                    "PRESENT" if g["has_acf"] else "MISSING",
                ),
            )

    def _selected_appid(self) -> Optional[str]:
        sel = self.games_tree.selection()
        if not sel:
            return None
        return str(self.games_tree.item(sel[0])["values"][0])

    def _on_add_game(self) -> None:
        steam = self._current_steam_path()
        if steam is None:
            messagebox.showerror("Steam Missing", "Set the Steam path first.")
            return
        if not self._is_lumacore_installed():
            self._warn_lumacore_not_installed("add a game")
            return
        if self._add_dialog is not None and self._add_dialog.winfo_exists():
            self._add_dialog.lift()
            return
        self._add_dialog = AddGameDialog(self, steam)
        self.wait_window(self._add_dialog)
        self._add_dialog = None
        self._refresh_games_list()

    def _on_update_game(self) -> None:
        appid = self._selected_appid()
        if appid is None:
            messagebox.showwarning(
                "No Game Selected", "Select a game in the list first."
            )
            return
        steam = self._current_steam_path()
        if steam is None:
            return
        if not self._is_lumacore_installed():
            self._warn_lumacore_not_installed("update a game")
            return
        self._add_dialog = AddGameDialog(
            self, steam, preset_appid=appid, update_mode=True
        )
        self.wait_window(self._add_dialog)
        self._add_dialog = None
        self._refresh_games_list()

    def _on_remove_game(self) -> None:
        appid = self._selected_appid()
        if appid is None:
            messagebox.showwarning(
                "No Game Selected", "Select a game in the list first."
            )
            return
        scope = CustomMessageBox(
            self,
            "Remove Game",
            f"Remove game {appid}?",
            [
                ("Full Remove", "full"),
                ("Normal Remove", "normal"),
                ("Cancel", None),
            ],
        ).result
        if scope is None:
            return
        scope_str = "full_keys" if scope == "full" else "basic"
        steam = self._current_steam_path()
        if steam is None:
            return
        if not self._is_lumacore_installed():
            self.log(
                f"[LumaCore] LumaCore not installed — cleaning up orphan files "
                f"for app {appid} (scope={scope_str})."
            )
        else:
            self.log(f"[LumaCore] Removing game {appid} (scope={scope_str})...")

        try:
            ok, message = lumacore_games.remove_game(steam, appid, scope=scope_str)
        except Exception as exc:
            self.log(f"[LumaCore] Remove error: {exc}")
            return

        self.log(f"[LumaCore] {message}")
        self._refresh_games_list()

    # --- STEAM CLOUD FIX ---
    def _on_steam_cloud_fix(self) -> None:
        steam = self._current_steam_path()
        if steam is None:
            messagebox.showerror("Steam Missing", "Set the Steam path first.")
            return

        steam_id = self.settings.get("steam_id_32", "").strip()
        if not steam_id:
            messagebox.showwarning(
                "SteamID32 Missing",
                "Please enter your SteamID32 in the field under Steam Installation before using this tool.",
            )
            return

        msg = (
            f"Steam Cloud Fix Management for profile: {steam_id}\n\n"
            "This utility prevents Steam Cloud sync errors and client lockups\n"
            "for games injected with LumaCore."
        )
        response = CustomMessageBox(
            self,
            "Steam Cloud Fix",
            msg,
            [
                ("Activate Fix", "activate"),
                ("Deactivate Fix", "deactivate"),
                ("Cancel", None),
            ],
        ).result

        if response == "activate":
            self.app.run_async(
                self._steam_cloud_fix_async(steam, steam_id, activate=True)
            )
        elif response == "deactivate":
            self.app.run_async(
                self._steam_cloud_fix_async(steam, steam_id, activate=False)
            )

    async def _steam_cloud_fix_async(
        self, steam: Path, steam_id: str, activate: bool
    ) -> None:
        if activate:
            self.log(
                f"[LumaCore] Bulk-activating Steam Cloud Fix for user {steam_id}..."
            )
            try:
                success, fail = await asyncio.to_thread(
                    lumacore_games.apply_steam_cloud_fix_all, steam, steam_id
                )
                self.log(
                    f"[LumaCore] Steam Cloud Fix: {success} applied, {fail} failed."
                )
                if fail > 0:
                    messagebox.showwarning(
                        "Completed With Warnings",
                        f"Fix applied successfully to {success} games.\n"
                        f"Could not apply to {fail} games. See depot_manager.log for details.",
                    )
                else:
                    messagebox.showinfo(
                        "Done",
                        f"Steam Cloud Fix applied successfully for all {success} managed games.",
                    )
            except Exception as exc:
                self.log(f"[LumaCore] Error applying Steam Cloud Fix: {exc}")
                messagebox.showerror(
                    "Steam Cloud Fix Failed",
                    f"Failed to apply Steam Cloud Fix:\n{exc}\n\nSee depot_manager.log for details.",
                )
        else:
            self.log(
                f"[LumaCore] Bulk-deactivating Steam Cloud Fix for user {steam_id}..."
            )
            try:
                success, fail = await asyncio.to_thread(
                    lumacore_games.remove_steam_cloud_fix_all, steam, steam_id
                )
                self.log(
                    f"[LumaCore] Steam Cloud Fix: {success} removed, {fail} failed."
                )
                messagebox.showinfo(
                    "Done",
                    f"Steam Cloud Fix disabled and cleaned for all {success} managed games.",
                )
            except Exception as exc:
                self.log(f"[LumaCore] Error removing Steam Cloud Fix: {exc}")
                messagebox.showerror(
                    "Steam Cloud Fix Failed",
                    f"Failed to remove Steam Cloud Fix:\n{exc}\n\nSee depot_manager.log for details.",
                )


# --- ADD GAME DIALOG ---
class AddGameDialog(tk.Toplevel):
    """Modal dialog to fetch a lua from the configured API and inject it.

    Reuses ``APIClient.fetch_manifests`` so the fetch step is identical to the
    DepotDownloader tab. Once the inventory is built, the user picks a download
    mode and confirms; we then call ``lumacore_games.add_game``.
    """

    DOWNLOAD_MODES = (
        ("steam_native", "Steam native (Steam downloads files)"),
        ("depotdownloader", "DepotDownloaderMod (this app downloads)"),
    )

    def __init__(
        self,
        parent: LumaCoreTab,
        steam_path: Path,
        preset_appid: str = "",
        update_mode: bool = False,
    ) -> None:
        super().__init__(parent)
        self.parent_tab = parent
        self.app = parent.app
        self.settings = parent.settings
        self.steam_path = steam_path
        self.update_mode = update_mode
        self.inventory: dict = {}
        self.temp_dir: Optional[Path] = None

        self.title("Update Game" if update_mode else "Add Game")
        self.geometry("720x560")
        self.transient(parent)
        self.grab_set()

        self._setup_ui(preset_appid)
        if preset_appid:
            self.appid_entry.insert(0, preset_appid)

    def _setup_ui(self, preset_appid: str) -> None:
        top = ttk.LabelFrame(self, text=" Source ", padding=8)
        top.pack(fill="x", padx=6, pady=4)

        ttk.Label(top, text="Source:").grid(row=0, column=0, sticky="w")
        self.source_var = tk.StringVar()
        self.source_combo = ttk.Combobox(
            top,
            textvariable=self.source_var,
            values=[info["label"] for info in SOURCES.values()],
            state="readonly",
            width=20,
        )
        self.source_combo.grid(row=0, column=1, padx=5, sticky="w")
        self.source_combo.bind("<<ComboboxSelected>>", self._on_source_change)
        selected = self.settings.get("selected_source", "morrenus")
        if selected in SOURCES:
            self.source_combo.set(SOURCES[selected]["label"])
        else:
            self.source_combo.set(SOURCES["morrenus"]["label"])

        ttk.Label(top, text="API Key:").grid(row=1, column=0, sticky="w")
        self.api_key_entry = ttk.Entry(top, width=50, show="*")
        self.api_key_entry.grid(row=1, column=1, padx=5, sticky="we")
        ttk.Button(top, text="Save", command=self._save_api_key).grid(row=1, column=2, padx=5)
        key_field = SOURCES[selected]["key_field"]
        self.api_key_entry.insert(0, self.settings.get(key_field, ""))

        mid = ttk.Frame(self, padding=6)
        mid.pack(fill="x", padx=6)
        ttk.Label(mid, text="AppID:").pack(side="left")
        self.appid_entry = ttk.Entry(mid, width=15)
        self.appid_entry.pack(side="left", padx=5)
        self.fetch_btn = ttk.Button(mid, text="Fetch", command=self._on_fetch)
        self.fetch_btn.pack(side="left")

        depots_frame = ttk.LabelFrame(self, text=" Depots Found ", padding=6)
        depots_frame.pack(fill="both", expand=True, padx=6, pady=4)
        columns = ("id", "status", "key", "manifest")
        self.tree = ttk.Treeview(
            depots_frame, columns=columns, show="headings", selectmode="none"
        )
        for col, text, w in zip(
            columns,
            ("Depot ID", "Status", "Key", "Manifest File"),
            (100, 100, 200, 200),
        ):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=w)
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(depots_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")

        bottom = ttk.Frame(self, padding=6)
        bottom.pack(fill="x", padx=6, pady=(2, 6))

        mode_row = ttk.Frame(bottom)
        mode_row.pack(fill="x", pady=(0, 4))
        ttk.Label(mode_row, text="Mode:").pack(side="left")
        self.mode_var = tk.StringVar(value=self.DOWNLOAD_MODES[0][0])
        for mode_key, mode_label in self.DOWNLOAD_MODES:
            ttk.Radiobutton(
                mode_row,
                text=mode_label,
                value=mode_key,
                variable=self.mode_var,
            ).pack(side="left", padx=5)

        lib_row = ttk.Frame(bottom)
        lib_row.pack(fill="x", pady=(0, 4))
        ttk.Label(lib_row, text="Library:").pack(side="left")
        self.library_var = tk.StringVar()
        self.library_combo = ttk.Combobox(
            lib_row, textvariable=self.library_var, state="readonly", width=60
        )
        self.library_combo.pack(side="left", padx=5, fill="x", expand=True)
        self._library_map: dict[str, Path] = {}
        if self.update_mode:
            self.library_combo.config(state="disabled")

        self.mode_var.trace_add("write", self._on_mode_change)
        self._on_mode_change()

        self.confirm_btn = ttk.Button(
            bottom, text="Confirm", command=self._on_confirm, state="disabled"
        )
        self.confirm_btn.pack(side="right", padx=(5, 0))
        ttk.Button(bottom, text="Cancel", command=self.destroy).pack(side="right")

    def _on_mode_change(self, *args) -> None:
        if self.update_mode:
            return
        if self.mode_var.get() == "steam_native":
            self.library_combo.config(state="disabled")
        else:
            self.library_combo.config(state="readonly")

    def _selected_source_key(self) -> str:
        label = self.source_var.get()
        for k, info in SOURCES.items():
            if info["label"] == label:
                return k
        return "morrenus"

    def _on_source_change(self, event=None) -> None:
        source_key = self._selected_source_key()
        self.api_key_entry.delete(0, tk.END)
        key_field = SOURCES[source_key]["key_field"]
        self.api_key_entry.insert(0, self.settings.get(key_field, ""))

    def _save_api_key(self) -> None:
        key = self.api_key_entry.get().strip()
        if len(key) < 10:
            messagebox.showwarning("API Key Too Short", "The API key is too short.")
            return
        source_key = self._selected_source_key()
        key_field = SOURCES[source_key]["key_field"]
        self.settings[key_field] = key
        self.settings["selected_source"] = source_key
        save_settings(self.settings)
        messagebox.showinfo("Saved", "API key saved successfully.")

    def _on_fetch(self) -> None:
        appid = self.appid_entry.get().strip()
        if not appid.isdigit():
            messagebox.showerror("Invalid AppID", "AppID must be numeric.")
            return
        if not (APPID_MIN <= int(appid) <= APPID_MAX):
            messagebox.showerror(
                "Invalid AppID", f"AppID out of range ({APPID_MIN} \u2013 {APPID_MAX})."
            )
            return
        source = self._selected_source_key()
        key_field = SOURCES[source]["key_field"]
        key = (
            self.settings.get(key_field, "").strip() or self.api_key_entry.get().strip()
        )
        if len(key) < 10:
            messagebox.showerror("Missing API Key", "Missing or invalid API key.")
            return

        self.fetch_btn.config(state="disabled")
        self.confirm_btn.config(state="disabled")
        self.app.run_async(self._fetch_async(appid, key, source))

    async def _fetch_async(self, app_id: str, api_key: str, source: str) -> None:
        self.app.log_safe(f"[LumaCore] Fetching lua for AppID {app_id} ({source})...")
        if self.temp_dir and self.temp_dir.exists():
            try:
                await asyncio.to_thread(shutil.rmtree, self.temp_dir)
            except OSError:
                pass
            self.temp_dir = None

        if self.app.session is None:
            messagebox.showerror("Session Error", "HTTP session not ready.")
            self.fetch_btn.config(state="normal")
            return

        client = APIClient(self.app.session, self.settings)
        try:
            temp_dir, local_inv = await client.fetch_manifests(app_id, api_key, source)
        except APIAuthError:
            self.app.log_safe("[LumaCore] API key rejected.")
            messagebox.showerror("Auth Error", "API key rejected by the server.")
        except APIHTTPError as exc:
            self.app.log_safe(f"[LumaCore] HTTP {exc.status}: {exc.message}")
            messagebox.showerror(
                "HTTP Error", f"Server responded with {exc.status}: {exc.message}"
            )
        except APINetworkError as exc:
            self.app.log_safe(f"[LumaCore] Network error: {exc}")
            messagebox.showerror(
                "Network Error",
                f"Connection failed:\n{exc}\n\nSee depot_manager.log for details.",
            )
        except Exception as exc:
            self.app.log_safe(f"[LumaCore] Fetch error: {exc}")
            messagebox.showerror(
                "Unexpected Error",
                f"Unexpected error:\n{exc}\n\nSee depot_manager.log for details.",
            )
        else:
            self.temp_dir = temp_dir
            self.inventory = local_inv
            self.after(0, self._populate_depots)
            self.app.log_safe(f"[LumaCore] Fetched {len(local_inv)} depot(s).")
        finally:
            self.after(0, lambda: self.fetch_btn.config(state="normal"))

    def _populate_depots(self) -> None:
        for row in self.tree.get_children():
            self.tree.delete(row)
        appid = self.appid_entry.get().strip()
        inv = {k: v for k, v in self.inventory.items() if k != appid}
        for did, info in sorted(inv.items()):
            status = "READY" if info["key"] and info["manifest_file"] else "INCOMPLETE"
            manifest = (
                info["manifest_file"].name if info["manifest_file"] else "MISSING"
            )
            self.tree.insert(
                "",
                tk.END,
                values=(did, status, info["key"] or "MISSING", manifest),
            )
        self.confirm_btn.config(state="normal" if inv else "disabled")

        self._populate_library_combo(appid)

    def _populate_library_combo(self, appid: str) -> None:
        libraries = get_steam_libraries(self.steam_path)
        if not libraries:
            self.library_combo.set("")
            self._library_map = {}
            return

        self._library_map = {library_label(lib): lib for lib in libraries}
        self.library_combo["values"] = list(self._library_map.keys())

        if self.update_mode:
            acf = find_acf_for_app(libraries, appid)
            if acf is not None:
                current_lib = acf.parent
                label = library_label(current_lib)
                self.library_var.set(label)
            else:
                default_lib = pick_library_default(libraries, appid)
                self.library_var.set(library_label(default_lib) if default_lib else "")
        else:
            default_lib = pick_library_default(libraries, appid)
            self.library_var.set(library_label(default_lib) if default_lib else "")

    def _on_confirm(self) -> None:
        appid = self.appid_entry.get().strip()
        if not appid.isdigit():
            return
        if self.temp_dir is None or not self.inventory:
            messagebox.showerror("No Manifest Loaded", "Fetch a manifest first.")
            return
        mode = self.mode_var.get()
        library_override = self._library_map.get(self.library_var.get())
        self.confirm_btn.config(state="disabled")
        self.app.run_async(self._confirm_async(appid, mode, library_override))

    async def _confirm_async(
        self, appid: str, mode: str, library_override: Optional[Path]
    ) -> None:
        self.app.log_safe(f"[LumaCore] Injecting game {appid} (mode={mode})...")

        def cb(pct: int, total: int, msg: str) -> None:
            self.app.log_safe(f"[LumaCore] {pct}% - {msg}")

        try:
            if self.update_mode:
                ok, message = await lumacore_games.update_game(
                    self.steam_path,
                    self.app.session,
                    self.settings,
                    appid,
                    self.inventory,
                    self.temp_dir,
                    download_mode=mode,
                    progress_cb=cb,
                    library_override=library_override,
                )
            else:
                ok, message = await lumacore_games.add_game(
                    self.steam_path,
                    self.app.session,
                    self.settings,
                    appid,
                    self.inventory,
                    self.temp_dir,
                    download_mode=mode,
                    progress_cb=cb,
                    library_override=library_override,
                )
        except Exception as exc:
            self.app.log_safe(f"[LumaCore] Add error: {exc}")
            messagebox.showerror(
                "Add Game Failed",
                f"Failed to add game:\n{exc}\n\nSee depot_manager.log for details.",
            )
            self.confirm_btn.config(state="normal")
            return

        self.app.log_safe(f"[LumaCore] {message}")

        if ok and mode == "depotdownloader":
            await self._run_depotdownloader(appid)

        if ok:
            messagebox.showinfo("Done", message)
            self.destroy()
        else:
            messagebox.showerror("Add Game Failed", message)
            self.confirm_btn.config(state="normal")

    async def _run_depotdownloader(self, appid: str) -> None:
        exe_name = self.settings.get(
            "exe_name", "../DepotDownloaderMod/DepotDownloaderMod.exe"
        )
        exe_path = (
            Path(exe_name) if Path(exe_name).is_absolute() else APP_DIR / exe_name
        )
        if not exe_path.exists():
            self.app.log_safe(f"[LumaCore] DepotDownloaderMod not found: {exe_path}")
            messagebox.showerror(
                "DepotDownloaderMod Missing",
                f"Could not find:\n{exe_path}\n\n"
                f"Game was injected; install DepotDownloaderMod to download files.",
            )
            return

        library = self._library_map.get(self.library_var.get())
        if library is None:
            libraries = get_steam_libraries(self.steam_path)
            library = (
                pick_library_default(libraries, appid=appid) if libraries else None
            )
        if library is None:
            self.app.log_safe("[LumaCore] No Steam library available for download.")
            return
        from .vdf_io import read_acf, update_acf_size

        acf_path = library / f"appmanifest_{appid}.acf"
        state = read_acf(acf_path)
        installdir = state.get("installdir", str(appid)) if state else str(appid)
        common_dir = library / "common" / installdir
        common_dir.mkdir(parents=True, exist_ok=True)

        selected = [did for did in self.inventory if did != appid]
        if not selected:
            return

        self.app.log_safe(
            f"[LumaCore] Downloading {len(selected)} depot(s) via DepotDownloaderMod..."
        )
        downloader = DownloadManager(
            self.settings,
            self.inventory,
            self.temp_dir,
            self.app.log_safe,
            output_dir=common_dir,
            max_downloads=32,
        )
        try:
            await downloader.run_downloads(selected, exe_path, appid)
        except Exception as exc:
            self.app.log_safe(f"[LumaCore] Download error: {exc}")
            return

        try:
            total_size = sum(
                f.stat().st_size for f in common_dir.rglob("*") if f.is_file()
            )
            update_acf_size(acf_path, total_size)
            self.app.log_safe(
                f"[LumaCore] ACF patched: {total_size // (1 << 20)} MB on disk."
            )
        except Exception as exc:
            self.app.log_safe(f"[LumaCore] ACF size patch failed: {exc}")
