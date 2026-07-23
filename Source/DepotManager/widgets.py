"""Reusable custom Tkinter widgets and dialogs."""

from __future__ import annotations

import tkinter as tk
from tkinter import simpledialog, ttk
from typing import Any, List, Optional, Tuple


class CustomMessageBox(simpledialog.Dialog):
    """Modal dialog with custom button labels.

    Returns the value associated with the clicked button, or None if
    the dialog is cancelled (Escape / close button).
    """

    def __init__(
        self,
        parent: tk.Misc,
        title: str,
        message: str,
        choices: List[Tuple[str, Any]],
    ):
        self.message = message
        self.choices = choices
        self.result: Optional[Any] = None
        super().__init__(parent, title)

    def body(self, master: tk.Misc) -> tk.Misc:
        label = ttk.Label(master, text=self.message, wraplength=400, justify="left")
        label.pack(padx=20, pady=(20, 10))
        return label

    def buttonbox(self) -> None:
        box = ttk.Frame(self)
        for text, value in self.choices:
            btn = ttk.Button(
                box, text=text, width=18, command=lambda v=value: self._select(v)
            )
            btn.pack(side="left", padx=5, pady=5)
        self.bind("<Escape>", lambda event: self._select(None))
        box.pack()

    def _select(self, value: Any) -> None:
        self.result = value
        self.destroy()

class ToolTip:
    """Tooltip overing over widget."""

    def __init__(self, widget: tk.Widget, text: str, delay: int = 500):
        self.widget = widget
        self.text = text
        self.delay = delay
        self.tip_window: Optional[tk.Toplevel] = None
        self.id: Optional[str] = None

        widget.bind("<Enter>", self._on_enter)
        widget.bind("<Leave>", self._on_leave)

    def _on_enter(self, event=None) -> None:
        self.id = self.widget.after(self.delay, self._show_tip)

    def _on_leave(self, event=None) -> None:
        if self.id:
            self.widget.after_cancel(self.id)
            self.id = None
        self._hide_tip()

    def _show_tip(self) -> None:
        if self.tip_window:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 5
        self.tip_window = tk.Toplevel(self.widget)
        self.tip_window.wm_overrideredirect(True)
        self.tip_window.wm_geometry(f"+{x}+{y}")
        label = tk.Label(
            self.tip_window,
            text=self.text,
            justify="left",
            background="#ffffe0",
            relief="solid",
            borderwidth=1,
            font=("Consolas", 9),
            wraplength=300,
        )
        label.pack()

    def _hide_tip(self) -> None:
        if self.tip_window:
            self.tip_window.destroy()
            self.tip_window = None
