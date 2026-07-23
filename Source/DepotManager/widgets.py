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
