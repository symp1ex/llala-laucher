"""Small reusable tkinter widgets used by the launcher."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any, Callable

from .parameter_specs import ParameterSpec


class Tooltip:
    def __init__(self, widget: tk.Widget, text: str, delay_ms: int = 500) -> None:
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self._after_id: str | None = None
        self._window: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event: tk.Event[Any]) -> None:
        self._cancel()
        self._after_id = self.widget.after(self.delay_ms, self._show)

    def _cancel(self) -> None:
        if self._after_id is not None:
            self.widget.after_cancel(self._after_id)
            self._after_id = None

    def _show(self) -> None:
        if not self.text or self._window is not None:
            return
        x = self.widget.winfo_rootx() + 16
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        window = tk.Toplevel(self.widget)
        window.wm_overrideredirect(True)
        window.wm_geometry(f"+{x}+{y}")
        label = tk.Label(
            window,
            text=self.text,
            justify="left",
            relief="solid",
            borderwidth=1,
            background="#ffffe0",
            padx=6,
            pady=4,
            wraplength=420,
        )
        label.pack()
        self._window = window

    def _hide(self, _event: tk.Event[Any] | None = None) -> None:
        self._cancel()
        if self._window is not None:
            self._window.destroy()
            self._window = None


class ScrollableFrame(ttk.Frame):
    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.content = ttk.Frame(self.canvas, padding=10)
        self._window_id = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.content.bind("<Configure>", self._on_content_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Enter>", self._bind_wheel)
        self.canvas.bind("<Leave>", self._unbind_wheel)

    def _on_content_configure(self, _event: tk.Event[Any]) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event: tk.Event[Any]) -> None:
        self.canvas.itemconfigure(self._window_id, width=event.width)

    def _bind_wheel(self, _event: tk.Event[Any]) -> None:
        self.canvas.bind_all("<MouseWheel>", self._on_wheel, add="+")

    def _unbind_wheel(self, _event: tk.Event[Any]) -> None:
        self.canvas.unbind_all("<MouseWheel>")

    def _on_wheel(self, event: tk.Event[Any]) -> None:
        self.canvas.yview_scroll(int(-event.delta / 120), "units")


class ParameterControl:
    def __init__(
        self,
        parent: ttk.Frame,
        spec: ParameterSpec,
        row: int,
        on_change: Callable[[], None],
    ) -> None:
        self.spec = spec
        self.on_change = on_change
        self.supported = True
        self.enabled_var = tk.BooleanVar(value=spec.default_enabled)
        self.value_var = tk.StringVar(value=str(spec.default))
        self.label_var = tk.StringVar(value=self._label_text())

        self.checkbox = ttk.Checkbutton(
            parent,
            textvariable=self.label_var,
            variable=self.enabled_var,
            command=self._enabled_changed,
        )
        self.checkbox.grid(row=row, column=0, sticky="w", padx=(0, 12), pady=5)
        Tooltip(self.checkbox, spec.tooltip)

        self.value_widget: ttk.Widget | None = None
        if spec.value_type != "bool":
            self.value_widget = self._make_value_widget(parent)
            self.value_widget.grid(row=row, column=1, sticky="ew", pady=5)
            Tooltip(self.value_widget, spec.tooltip)
            self.value_var.trace_add("write", self._value_changed)
        parent.columnconfigure(1, weight=1)
        self._update_widget_state()

    def _label_text(self) -> str:
        suffix = "" if self.supported else " (unsupported by this llama-server)"
        return f"{self.spec.label}  [{self.spec.cli}]{suffix}"

    def _make_value_widget(self, parent: ttk.Frame) -> ttk.Widget:
        spec = self.spec
        if spec.value_type == "choice":
            return ttk.Combobox(parent, textvariable=self.value_var, values=spec.choices, state="readonly")
        if spec.value_type == "int_or_choice":
            return ttk.Combobox(parent, textvariable=self.value_var, values=spec.choices)
        if spec.value_type in {"int", "float"}:
            increment = 1 if spec.value_type == "int" else 0.1
            return ttk.Spinbox(
                parent,
                textvariable=self.value_var,
                from_=spec.min_value if spec.min_value is not None else -2_147_483_648,
                to=spec.max_value if spec.max_value is not None else 2_147_483_647,
                increment=increment,
            )
        return ttk.Entry(parent, textvariable=self.value_var, show="*" if spec.value_type == "secret" else "")

    def _enabled_changed(self) -> None:
        self._update_widget_state()
        self.on_change()

    def _value_changed(self, *_args: object) -> None:
        self.on_change()

    def _update_widget_state(self) -> None:
        if not self.supported:
            self.checkbox.state(["disabled"])
        else:
            self.checkbox.state(["!disabled"])
        if self.value_widget is None:
            return
        enabled = self.supported and self.enabled_var.get()
        if isinstance(self.value_widget, ttk.Combobox) and self.spec.value_type == "choice":
            self.value_widget.configure(state="readonly" if enabled else "disabled")
        else:
            self.value_widget.configure(state="normal" if enabled else "disabled")

    def set_supported(self, supported: bool) -> None:
        self.supported = supported
        self.label_var.set(self._label_text())
        self._update_widget_state()

    def get_state(self) -> dict[str, Any]:
        if self.spec.value_type == "bool":
            return {"enabled": self.enabled_var.get(), "value": True}
        value: Any = self.value_var.get()
        try:
            if self.spec.value_type == "int":
                value = int(value)
            elif self.spec.value_type == "float":
                value = float(value)
        except (TypeError, ValueError):
            pass
        return {"enabled": self.enabled_var.get(), "value": value}

    def set_state(self, state: dict[str, Any]) -> None:
        self.enabled_var.set(bool(state.get("enabled", False)))
        if self.spec.value_type != "bool":
            self.value_var.set(str(state.get("value", self.spec.default)))
        self._update_widget_state()
