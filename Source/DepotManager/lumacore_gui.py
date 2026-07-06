"""Tkinter UI for the LumaCore Manager tab + Add Game dialog.

This module is imported only by ``gui.py`` and is intentionally Tkinter-only.
It reuses the existing async bridge (``App.run_async`` / ``App.session``) and
the existing API fetch pipeline (``api_client.APIClient`` + ``parser``) so the
LumaCore tab feels native to the rest of the application.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import TYPE_CHECKING, Callable, Optional

from . import lumacore_games, lumacore_setup
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


# ---------------------------------------------------------------------------
# Main tab
# ---------------------------------------------------------------------------
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
        self.log = app.log_safe  # shared console logger
        self._add_dialog: Optional["AddGameDialog"] = None
        self._setup_ui()
        self.after(50, self._refresh_steam_path_display)
        self.after(100, self._refresh_games_list)

    # ------------------------------------------------------------------
    # LAYOUT
    # ------------------------------------------------------------------
    def _setup_ui(self) -> None:
        # --- Steam path section ---
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

        # --- LumaCore component section ---
        lc_frame = ttk.LabelFrame(self, text=" LumaCore Component ", padding=8)
        lc_frame.pack(fill="x", padx=4, pady=4)

        self.lc_status_var = tk.StringVar(value="Installed: ?  |  Latest: ?")
        ttk.Label(lc_frame, textvariable=self.lc_status_var).grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 5)
        )

        ttk.Button(
            lc_frame, text="Check for updates", command=self._on_check_update
        ).grid(row=1, column=0, padx=2)
        ttk.Button(lc_frame, text="Install / Update", command=self._on_install).grid(
            row=1, column=1, padx=2
        )
        ttk.Button(lc_frame, text="Uninstall", command=self._on_uninstall).grid(
            row=1, column=2, padx=2
        )

        self.complete_uninstall_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            lc_frame,
            text="Complete uninstall (also remove all managed games)",
            variable=self.complete_uninstall_var,
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))

        # ACF backup management row.
        acf_btns = ttk.Frame(lc_frame)
        acf_btns.grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Button(
            acf_btns, text="Restore ACF Backups", command=self._on_restore_acf_backups
        ).pack(side="left", padx=2)
        ttk.Button(
            acf_btns, text="Clean ACF Backups", command=self._on_clean_acf_backups
        ).pack(side="left", padx=2)

        # --- Managed games section ---
        games_frame = ttk.LabelFrame(self, text=" Managed Games ", padding=8)
        games_frame.pack(fill="both", expand=True, padx=4, pady=4)

        columns = ("appid", "name", "depots", "acf")
        self.games_tree = ttk.Treeview(
            games_frame, columns=columns, show="headings", selectmode="browse"
        )
        for col, text, w in zip(
            columns,
            ("AppID", "Name", "#Depots", "ACF"),
            (90, 320, 60, 50),
        ):
            self.games_tree.heading(col, text=text)
            self.games_tree.column(
                col, width=w, anchor="w" if col == "name" else "center"
            )
        self.games_tree.pack(side="left", fill="both", expand=True)

        sb = ttk.Scrollbar(
            games_frame, orient="vertical", command=self.games_tree.yview
        )
        self.games_tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")

        games_btns = ttk.Frame(self)
        games_btns.pack(fill="x", padx=4, pady=(2, 4))
        ttk.Button(games_btns, text="Add Game...", command=self._on_add_game).pack(
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

    # ------------------------------------------------------------------
    # STEAM PATH
    # ------------------------------------------------------------------
    def _current_steam_path(self) -> Optional[Path]:
        return get_steam_path(self.settings)

    def _is_lumacore_installed(self) -> bool:
        """True when LumaCore is installed (all 4 DLLs present in Steam dir).

        Uses the same validator as the business logic layer so the GUI and
        lumacore_games.add_game agree on what "installed" means.
        """
        steam = self._current_steam_path()
        if steam is None:
            return False
        return bool(_lc_installed_version(self.settings, steam))

    def _warn_lumacore_not_installed(self, action: str) -> None:
        """Log + messagebox telling the user to install LumaCore first.

        ``action`` is a short noun phrase describing what was blocked
        ("add a game", "update a game").
        """
        self.log(
            "[LumaCore] Cannot " + action + ": LumaCore is not installed. "
            "Use 'Install / Update' in the LumaCore Component section first."
        )
        messagebox.showwarning(
            "LumaCore not installed",
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
        # Clear any manual override so detection re-runs from registry/defaults.
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
                "Steam not found",
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
        # Sanity check: must look like a Steam dir.
        if not (path / "steam.exe").is_file() and not (path / "steamapps").is_dir():
            if not messagebox.askyesno(
                "Unlikely Steam path",
                f"The selected folder does not look like a Steam installation\n"
                f"(no steam.exe or steamapps/ found):\n\n  {path}\n\n"
                f"Use it anyway?",
            ):
                return
        set_steam_path(self.settings, path)
        save_settings(self.settings)
        self._refresh_steam_path_display()
        self.log(f"[LumaCore] Steam path set manually: {path}")

    # ------------------------------------------------------------------
    # LUMACORE COMPONENT
    # ------------------------------------------------------------------
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
        # Three distinct wordings: not installed vs. update vs. up to date.
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
            messagebox.showerror("Steam missing", "Set the Steam path first.")
            return
        if not messagebox.askyesno(
            "Install LumaCore",
            f"This will:\n"
            f"  - close Steam\n"
            f"  - download the latest LumaCore from GitHub\n"
            f"  - place 4 DLLs in:\n     {steam}\n"
            f"  - restart Steam manually afterwards\n\nContinue?",
        ):
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
            messagebox.showerror("Install failed", message)

    def _on_uninstall(self) -> None:
        steam = self._current_steam_path()
        if steam is None:
            messagebox.showerror("Steam missing", "Set the Steam path first.")
            return
        complete = self.complete_uninstall_var.get()
        installed = self._is_lumacore_installed()

        # Pre-check: enumerate managed games so we can show the user exactly
        # what is about to be removed and short-circuit the no-op cases.
        try:
            games = lumacore_games.list_installed_games(steam)
        except Exception:
            games = []
        games_count = len(games)

        if not complete:
            # Plain DLL uninstall: no game files touched.
            warn = "This will close Steam and remove the LumaCore DLLs."
            if not messagebox.askyesno("Uninstall LumaCore", warn + "\n\nContinue?"):
                return
            self.app.run_async(self._uninstall_async(steam, complete))
            return

        # complete=True: 4 branches based on installed + games_count.
        if installed and games_count == 0:
            if not messagebox.askyesno(
                "Uninstall LumaCore (complete)",
                "No managed games found.\n\n"
                "This will close Steam and remove the LumaCore DLLs only.\n"
                "No game files or ACFs will be touched.\n\nContinue?",
            ):
                return
        elif installed and games_count > 0:
            # Build a readable list of games that will be removed.
            listing = "\n".join(f"  - {g['appid']}: {g['name']}" for g in games[:20])
            if games_count > 20:
                listing += f"\n  ... and {games_count - 20} more"
            if not messagebox.askyesno(
                "Uninstall LumaCore (complete)",
                f"This will close Steam, remove LumaCore AND every managed game\n"
                f"({games_count} game(s) will be removed):\n\n{listing}\n\n"
                f"WARNING: If you have legitimately purchased any of these games\n"
                f"AFTER injecting them via DepotManager, their install state (ACF)\n"
                f"will be deleted. Steam will show them as not installed and you\n"
                f"will need to re-download them. A backup of each ACF will be saved\n"
                f"as <name>.depotmanager_bak in the same library folder, and can be\n"
                f"restored via 'Restore ACF Backups'.\n\nContinue?",
            ):
                return
        elif not installed and games_count == 0:
            # Total no-op: nothing to uninstall, nothing to clean.
            messagebox.showinfo(
                "Nothing to do",
                "LumaCore is not installed and no managed games were found.\n"
                "There is nothing to uninstall or clean up.",
            )
            return
        else:  # not installed and games_count > 0 (orphan cleanup)
            listing = "\n".join(f"  - {g['appid']}: {g['name']}" for g in games[:20])
            if games_count > 20:
                listing += f"\n  ... and {games_count - 20} more"
            if not messagebox.askyesno(
                "Clean up orphan game files",
                f"LumaCore is not installed, but {games_count} managed game(s)\n"
                f"were found (left over from a previous install):\n\n{listing}\n\n"
                f"Clean up their stplug-in lua, depotcache manifests and ACFs?\n"
                f"(Steam will NOT be closed — these files are not locked.)",
            ):
                return
        self.app.run_async(self._uninstall_async(steam, complete))

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
            messagebox.showwarning("Uninstall", message)

    # ------------------------------------------------------------------
    # MANAGED GAMES
    # ------------------------------------------------------------------
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
        for g in games:
            self.games_tree.insert(
                "",
                tk.END,
                values=(
                    g["appid"],
                    g["name"],
                    g["depot_count"],
                    "yes" if g["has_acf"] else "no",
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
            messagebox.showerror("Steam missing", "Set the Steam path first.")
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
            messagebox.showinfo("Update game", "Select a game in the list first.")
            return
        # Update reuses the same dialog with the AppID pre-filled.
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
            messagebox.showinfo("Remove game", "Select a game in the list first.")
            return
        scope = messagebox.askyesnocancel(
            "Remove game",
            f"Remove game {appid}?\n\n"
            f"Yes  = full (lua + manifests + ACF + depot keys)\n"
            f"No   = basic (lua only)\n"
            f"Cancel = abort",
        )
        if scope is None:
            return
        scope_str = "full_keys" if scope else "basic"
        steam = self._current_steam_path()
        if steam is None:
            return
        # Remove is allowed even without LumaCore installed: it is the only
        # way to clean up orphan files left by an incomplete uninstall.
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

    # ------------------------------------------------------------------
    # ACF BACKUPS
    # ------------------------------------------------------------------
    def _on_restore_acf_backups(self) -> None:
        steam = self._current_steam_path()
        if steam is None:
            messagebox.showerror("Steam missing", "Set the Steam path first.")
            return
        try:
            backups = lumacore_games.list_all_acf_backups(steam)
        except Exception as exc:
            self.log(f"[LumaCore] Cannot list ACF backups: {exc}")
            return
        if not backups:
            messagebox.showinfo(
                "No ACF backups",
                "No ACF backups were found in any Steam library.\n"
                "Backups are created automatically when a managed game is removed.",
            )
            return
        dialog = RestoreAcfDialog(self, steam, backups)
        self.wait_window(dialog)
        self._refresh_games_list()

    def _on_clean_acf_backups(self) -> None:
        steam = self._current_steam_path()
        if steam is None:
            messagebox.showerror("Steam missing", "Set the Steam path first.")
            return
        try:
            backups = lumacore_games.list_all_acf_backups(steam)
        except Exception as exc:
            self.log(f"[LumaCore] Cannot list ACF backups: {exc}")
            return
        if not backups:
            messagebox.showinfo(
                "No ACF backups",
                "No ACF backups were found. Nothing to clean.",
            )
            return
        if not messagebox.askyesno(
            "Clean ACF backups",
            f"Delete {len(backups)} ACF backup(s) from your Steam libraries?\n"
            f"This cannot be undone. The backups are no longer needed once the\n"
            f"corresponding games are either re-installed via Steam or no longer\n"
            f"wanted.\n\nContinue?",
        ):
            return
        try:
            count = lumacore_games.clean_all_acf_backups(steam)
        except Exception as exc:
            self.log(f"[LumaCore] Clean ACF backups error: {exc}")
            return
        self.log(f"[LumaCore] Deleted {count} ACF backup(s).")
        messagebox.showinfo("Done", f"Deleted {count} ACF backup(s).")


# ---------------------------------------------------------------------------
# Restore ACF Backups dialog (selective)
# ---------------------------------------------------------------------------
class RestoreAcfDialog(tk.Toplevel):
    """Modal dialog to let the user pick which ACF backups to restore.

    For each backup file (``appmanifest_<appid>.acf.depotmanager_bak``) we show
    the AppID, the game name (read from the backup content), the library path,
    the backup date, and a status flag indicating whether restoring it would
    overwrite an existing ACF (in which case restore will be skipped for
    safety).
    """

    def __init__(self, parent: LumaCoreTab, steam_path: Path, backups: list) -> None:
        super().__init__(parent)
        self.parent_tab = parent
        self.steam_path = steam_path
        self.backups: list = backups
        self.checked: dict[str, bool] = {}

        self.title("Restore ACF Backups")
        self.geometry("820x420")
        self.transient(parent)
        self.grab_set()

        self._setup_ui()

    def _setup_ui(self) -> None:
        ttk.Label(self, text="Select which ACF backups to restore:").pack(
            anchor="w", padx=8, pady=(8, 4)
        )

        tree_frame = ttk.Frame(self)
        tree_frame.pack(fill="both", expand=True, padx=8, pady=4)

        columns = ("check", "appid", "name", "library", "date", "status")
        self.tree = ttk.Treeview(
            tree_frame, columns=columns, show="headings", selectmode="none"
        )
        for col, text, w in zip(
            columns,
            ("", "AppID", "Name", "Library", "Backup date", "Status"),
            (30, 80, 220, 240, 100, 110),
        ):
            self.tree.heading(col, text=text)
            self.tree.column(
                col, width=w, anchor="w" if col in ("name", "library") else "center"
            )
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.bind("<ButtonRelease-1>", self._on_tree_click)

        info = ttk.Label(
            self,
            text="Rows marked '\u26a0 ACF exists' will be skipped on restore to avoid\n"
            "overwriting a legitimate install state.",
            foreground="gray",
            justify="left",
        )
        info.pack(anchor="w", padx=8, pady=4)

        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=8, pady=(2, 8))
        ttk.Button(btns, text="Select All", command=self._select_all).pack(
            side="left", padx=2
        )
        ttk.Button(btns, text="Deselect All", command=self._deselect_all).pack(
            side="left", padx=2
        )
        ttk.Button(btns, text="Restore Selected", command=self._on_restore).pack(
            side="right", padx=2
        )
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right", padx=2)

        self._populate()

    def _backup_info(self, backup: Path) -> dict:
        """Build a row dict for a backup file: appid, name, library, date, status."""
        # appmanifest_<appid>.acf.depotmanager_bak -> <appid>
        name = backup.name
        appid = ""
        if name.startswith("appmanifest_") and name.endswith(".acf.depotmanager_bak"):
            appid = name[len("appmanifest_") : -len(".acf.depotmanager_bak")]
        # Try to read the game name from the backup content.
        game_name = appid
        try:
            state = _read_acf_vdf(backup)
            if state and state.get("name"):
                game_name = str(state["name"])
        except Exception:
            pass
        # Library = parent dir basename (or full path if short).
        lib_label = str(backup.parent)
        # Backup date from mtime.
        try:
            import datetime

            mtime = backup.stat().st_mtime
            date_str = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")
        except OSError:
            date_str = "?"
        # Status: does the target ACF already exist?
        target_acf = backup.parent / name[: -len(".depotmanager_bak")]
        status = "\u26a0 ACF exists" if target_acf.is_file() else "Available"
        return {
            "appid": appid,
            "name": game_name,
            "library": lib_label,
            "date": date_str,
            "status": status,
            "target": target_acf,
        }

    def _populate(self) -> None:
        for row in self.tree.get_children():
            self.tree.delete(row)
        for backup in self.backups:
            info = self._backup_info(backup)
            item = self.tree.insert(
                "",
                tk.END,
                values=(
                    "\u2610",
                    info["appid"],
                    info["name"],
                    info["library"],
                    info["date"],
                    info["status"],
                ),
            )
            self.checked[item] = False

    def _on_tree_click(self, event: tk.Event) -> None:
        region = self.tree.identify_region(event.x, event.y)
        if region != "cell":
            return
        col = self.tree.identify_column(event.x)
        if col != "#1":  # only the checkbox column toggles
            return
        row = self.tree.identify_row(event.y)
        if not row:
            return
        self.checked[row] = not self.checked.get(row, False)
        symbol = "\u2611" if self.checked[row] else "\u2610"
        values = list(self.tree.item(row)["values"])
        values[0] = symbol
        self.tree.item(row, values=values)

    def _select_all(self) -> None:
        for row in self.tree.get_children():
            self.checked[row] = True
            values = list(self.tree.item(row)["values"])
            values[0] = "\u2611"
            self.tree.item(row, values=values)

    def _deselect_all(self) -> None:
        for row in self.tree.get_children():
            self.checked[row] = False
            values = list(self.tree.item(row)["values"])
            values[0] = "\u2610"
            self.tree.item(row, values=values)

    def _on_restore(self) -> None:
        selected_paths: list = []
        for row, checked in self.checked.items():
            if not checked:
                continue
            # Map row -> backup file by matching appid + library.
            values = self.tree.item(row)["values"]
            appid = str(values[1])
            lib = str(values[3])
            for backup in self.backups:
                info = self._backup_info(backup)
                if info["appid"] == appid and str(info["library"]) == lib:
                    selected_paths.append(backup)
                    break
        if not selected_paths:
            messagebox.showinfo("Restore", "No backups selected.")
            return
        try:
            restored, skipped = lumacore_games.restore_acf_backups_selected(
                self.steam_path, selected_paths
            )
        except Exception as exc:
            self.parent_tab.log(f"[LumaCore] Restore error: {exc}")
            messagebox.showerror("Error", str(exc))
            return
        self.parent_tab.log(
            f"[LumaCore] ACF restore: {restored} restored, {skipped} skipped."
        )
        messagebox.showinfo(
            "Restore complete",
            f"Restored: {restored}\nSkipped (ACF already present): {skipped}",
        )
        self.destroy()


# ---------------------------------------------------------------------------
# Add Game / Update Game dialog
# ---------------------------------------------------------------------------
class AddGameDialog(tk.Toplevel):
    """Modal dialog to fetch a lua from the configured API and inject it.

    Reuses ``APIClient.fetch_manifests`` so the fetch step is identical to the
    DepotDownloader tab. Once the inventory is built, the user picks a download
    mode and confirms; we then call ``lumacore_games.add_game``.
    """

    DOWNLOAD_MODES = (
        ("steam_native", "Steam native (Steam downloads files)"),
        ("depotdownloader", "DepotDownloaderMod (this app downloads)"),
        ("inject_only", "Inject only (no ACF, no download)"),
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
        # --- Source / API key ---
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
        selected = self.settings.get("selected_source", "morrenus")
        if selected in SOURCES:
            self.source_combo.set(SOURCES[selected]["label"])
        else:
            self.source_combo.set(SOURCES["morrenus"]["label"])

        ttk.Label(top, text="API Key:").grid(row=1, column=0, sticky="w")
        self.api_key_entry = ttk.Entry(top, width=50, show="*")
        self.api_key_entry.grid(row=1, column=1, padx=5, sticky="we")
        key_field = SOURCES[selected]["key_field"]
        self.api_key_entry.insert(0, self.settings.get(key_field, ""))

        # --- AppID + fetch ---
        mid = ttk.Frame(self, padding=6)
        mid.pack(fill="x", padx=6)
        ttk.Label(mid, text="AppID:").pack(side="left")
        self.appid_entry = ttk.Entry(mid, width=15)
        self.appid_entry.pack(side="left", padx=5)
        self.fetch_btn = ttk.Button(mid, text="Fetch", command=self._on_fetch)
        self.fetch_btn.pack(side="left")

        # --- Depots found ---
        depots_frame = ttk.LabelFrame(self, text=" Depots Found ", padding=6)
        depots_frame.pack(fill="both", expand=True, padx=6, pady=4)
        columns = ("id", "status", "key", "manifest")
        self.tree = ttk.Treeview(
            depots_frame, columns=columns, show="headings", selectmode="none"
        )
        for col, text, w in zip(
            columns,
            ("Depot ID", "Status", "Key", "Manifest File"),
            (90, 110, 200, 200),
        ):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=w)
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(depots_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")

        # --- Library + download mode + confirm ---
        bottom = ttk.Frame(self, padding=6)
        bottom.pack(fill="x", padx=6, pady=(2, 6))

        # Library row: chosen by the user (Add) or fixed to the current one
        # (Update, read-only). Populated after fetch in _populate_depots.
        lib_row = ttk.Frame(bottom)
        lib_row.pack(fill="x", pady=(0, 4))
        ttk.Label(lib_row, text="Library:").pack(side="left")
        self.library_var = tk.StringVar()
        self.library_combo = ttk.Combobox(
            lib_row, textvariable=self.library_var, state="readonly", width=60
        )
        self.library_combo.pack(side="left", padx=5, fill="x", expand=True)
        # Maps combobox display label -> library Path. Filled in _populate_depots.
        self._library_map: dict[str, Path] = {}
        # In update mode the combobox stays read-only (can't move the game).
        if self.update_mode:
            self.library_combo.config(state="disabled")

        ttk.Label(bottom, text="Mode:").pack(side="left")
        self.mode_var = tk.StringVar(value=self.DOWNLOAD_MODES[0][1])
        self.mode_combo = ttk.Combobox(
            bottom,
            textvariable=self.mode_var,
            values=[m[1] for m in self.DOWNLOAD_MODES],
            state="readonly",
            width=42,
        )
        self.mode_combo.pack(side="left", padx=5)
        self.mode_combo.current(0)

        self.confirm_btn = ttk.Button(
            bottom, text="Confirm", command=self._on_confirm, state="disabled"
        )
        self.confirm_btn.pack(side="right", padx=(5, 0))
        ttk.Button(bottom, text="Cancel", command=self.destroy).pack(side="right")

    # ------------------------------------------------------------------
    def _selected_source_key(self) -> str:
        label = self.source_var.get()
        for k, info in SOURCES.items():
            if info["label"] == label:
                return k
        return "morrenus"

    def _on_fetch(self) -> None:
        appid = self.appid_entry.get().strip()
        if not appid.isdigit():
            messagebox.showerror("Error", "AppID must be numeric.")
            return
        if not (APPID_MIN <= int(appid) <= APPID_MAX):
            messagebox.showerror(
                "Error", f"AppID out of range ({APPID_MIN}–{APPID_MAX})."
            )
            return
        source = self._selected_source_key()
        key_field = SOURCES[source]["key_field"]
        key = (
            self.settings.get(key_field, "").strip() or self.api_key_entry.get().strip()
        )
        if len(key) < 10:
            messagebox.showerror("Error", "Missing or invalid API Key.")
            return

        self.fetch_btn.config(state="disabled")
        self.confirm_btn.config(state="disabled")
        self.app.run_async(self._fetch_async(appid, key, source))

    async def _fetch_async(self, app_id: str, api_key: str, source: str) -> None:
        self.app.log_safe(f"[LumaCore] Fetching lua for AppID {app_id} ({source})...")
        # Clean previous temp dir if any.
        if self.temp_dir and self.temp_dir.exists():
            try:
                await asyncio.to_thread(shutil.rmtree, self.temp_dir)
            except OSError:
                pass
            self.temp_dir = None

        if self.app.session is None:
            messagebox.showerror("Error", "HTTP session not ready.")
            self.fetch_btn.config(state="normal")
            return

        client = APIClient(self.app.session, self.settings)
        try:
            temp_dir, local_inv = await client.fetch_manifests(app_id, api_key, source)
        except APIAuthError:
            self.app.log_safe("[LumaCore] API key rejected.")
            messagebox.showerror("Auth Error", "API Key rejected by the server.")
        except APIHTTPError as exc:
            self.app.log_safe(f"[LumaCore] HTTP {exc.status}: {exc.message}")
            messagebox.showerror("HTTP Error", f"{exc.status}: {exc.message}")
        except APINetworkError as exc:
            self.app.log_safe(f"[LumaCore] Network error: {exc}")
            messagebox.showerror("Network Error", str(exc))
        except Exception as exc:
            self.app.log_safe(f"[LumaCore] Fetch error: {exc}")
            messagebox.showerror("Error", f"Unexpected error:\n{exc}")
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
        # Mirror the main tab: drop the AppID itself from the inventory display.
        inv = {k: v for k, v in self.inventory.items() if k != appid}
        for did, info in sorted(inv.items()):
            status = (
                "✅ READY" if info["key"] and info["manifest_file"] else "⚠️ INCOMPLETE"
            )
            manifest = (
                info["manifest_file"].name if info["manifest_file"] else "Missing"
            )
            self.tree.insert(
                "",
                tk.END,
                values=(did, status, info["key"] or "Missing", manifest),
            )
        self.confirm_btn.config(state="normal" if inv else "disabled")

        # Populate the library combobox.
        self._populate_library_combo(appid)

    def _populate_library_combo(self, appid: str) -> None:
        """Fill the library combobox with all known Steam libraries.

        In ``update_mode`` the combobox is locked to the library where the
        game's ACF currently lives (read-only). In ``add`` mode the user can
        pick any library; default selection is the library with the most free
        space (via ``pick_library_default``).
        """
        libraries = get_steam_libraries(self.steam_path)
        if not libraries:
            self.library_combo.set("")
            self._library_map = {}
            return

        self._library_map = {library_label(lib): lib for lib in libraries}
        self.library_combo["values"] = list(self._library_map.keys())

        if self.update_mode:
            # Lock to the current ACF's library. If no ACF exists (e.g. the
            # game was previously injected with inject_only mode), fall back
            # to pick_library_default but keep the combobox disabled so the
            # update lands in a deterministic place.
            acf = find_acf_for_app(libraries, appid)
            if acf is not None:
                current_lib = acf.parent
                label = library_label(current_lib)
                self.library_var.set(label)
            else:
                default_lib = pick_library_default(libraries, appid)
                self.library_var.set(library_label(default_lib) if default_lib else "")
        else:
            # Add mode: default to the library with the most free space.
            default_lib = pick_library_default(libraries, appid)
            self.library_var.set(library_label(default_lib) if default_lib else "")

    def _on_confirm(self) -> None:
        appid = self.appid_entry.get().strip()
        if not appid.isdigit():
            return
        if self.temp_dir is None or not self.inventory:
            messagebox.showerror("Error", "Fetch a manifest first.")
            return
        mode = self.DOWNLOAD_MODES[self.mode_combo.current()][0]
        # Resolve the library override from the combobox (None if unset/invalid).
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
            messagebox.showerror("Error", str(exc))
            self.confirm_btn.config(state="normal")
            return

        self.app.log_safe(f"[LumaCore] {message}")

        # If depotdownloader mode, kick off the file download now.
        if ok and mode == "depotdownloader":
            await self._run_depotdownloader(appid)

        if ok:
            messagebox.showinfo("Done", message)
            self.destroy()
        else:
            messagebox.showerror("Failed", message)
            self.confirm_btn.config(state="normal")

    async def _run_depotdownloader(self, appid: str) -> None:
        """Run DepotDownloaderMod to materialise game files for this app."""
        exe_name = self.settings.get(
            "exe_name", "../DepotDownloaderMod/DepotDownloaderMod.exe"
        )
        exe_path = (
            Path(exe_name) if Path(exe_name).is_absolute() else APP_DIR / exe_name
        )
        if not exe_path.exists():
            self.app.log_safe(f"[LumaCore] DepotDownloaderMod not found: {exe_path}")
            messagebox.showerror(
                "DepotDownloaderMod missing",
                f"Could not find:\n{exe_path}\n\n"
                f"Game was injected; install DepotDownloaderMod to download files.",
            )
            return

        # Choose library + installdir (must match what add_game wrote to the ACF).
        # Prefer the library the user picked in the dialog; fall back to default.
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
        common_dir = library.parent / "common" / installdir
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

        # Patch ACF with real on-disk size.
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
