"""tkinter application for configuring and running llama-server."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path
import queue
import re
import threading
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
import traceback
from typing import Any, Mapping
import webbrowser

from .app_paths import AppPaths
from .llama_server import (
    CommandValidationError,
    DetectionResult,
    build_command,
    build_server_url,
    default_parameter_state,
    detect_supported_parameters,
    format_windows_command,
    validate_web_mcp_executable,
)
from .model_scanner import ModelInfo, ModelScanner
from .parameter_specs import CATEGORIES, PARAMETER_SPECS, SPEC_BY_KEY
from .preset_manager import PresetError, PresetManager
from .server_process import LlamaServerProcess
from .tray import TrayController
from .updater import CheckResult, InstallResult, UpdateState, UpdaterService
from version import VERSION
from .widgets import ParameterControl, ScrollableFrame, Tooltip
from .windows_integration import (
    apply_window_icon,
    client_size_for_outer_window,
    frozen_executable_path,
    resolve_icon_path,
)
from .web_search_settings import (
    ConnectionTestResult,
    WebSearchSettings,
    WebSearchSettingsError,
    test_searxng_connection,
    validate_web_search_settings,
)


LOGGER = logging.getLogger(__name__)


class LauncherApp:
    PREVIEW_DELAY_MS = 250
    UPDATE_SHUTDOWN_DELAY_MS = 2_000
    # Vista ttk: old preset buttons used 76 + 5 + 76 px; the shared new column uses 91 px.
    WINDOW_OUTER_WIDTH = 834
    WINDOW_OUTER_HEIGHT = 940

    _UPDATE_TRANSITIONS = {
        UpdateState.IDLE: frozenset({UpdateState.CHECKING}),
        UpdateState.CHECKING: frozenset(
            {UpdateState.IDLE, UpdateState.AVAILABLE, UpdateState.ERROR}
        ),
        UpdateState.AVAILABLE: frozenset({UpdateState.INSTALLING}),
        UpdateState.INSTALLING: frozenset({UpdateState.AVAILABLE}),
        UpdateState.ERROR: frozenset({UpdateState.CHECKING}),
    }

    def __init__(self, root: tk.Tk, paths: AppPaths, version: str = VERSION) -> None:
        self.root = root
        self.paths = paths
        self.version = version
        self.updater = UpdaterService(paths)
        self.update_state = UpdateState.IDLE
        self.update_activity_visible = False
        self.auto_update_check_started = False
        self.model_scanner = ModelScanner(paths.models)
        self.preset_manager = PresetManager(paths.presets)
        self.server_process = LlamaServerProcess()
        self.background_events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.models_by_display: dict[str, ModelInfo] = {}
        self.presets_by_display: dict[str, Path] = {}
        self.parameter_controls: dict[str, ParameterControl] = {}
        self.supported_keys: frozenset[str] | None = None
        self.supports_mcp_servers_json = False
        self.preview_after_id: str | None = None
        self.closing = False
        self.quit_finished = False
        self.stopping = False
        self.server_url: str | None = None
        self.settings = self._load_settings()
        self.web_search_settings = WebSearchSettings.from_mapping(
            self.settings.get("web_search")
        )
        self.web_search_window: tk.Toplevel | None = None
        icon_path = resolve_icon_path(paths.base_dir)
        executable_icon = frozen_executable_path()
        self.tray = TrayController(self.background_events, icon_path, executable_icon)

        self.root.title("llala-laucher")
        self.window_icon_handles = apply_window_icon(self.root, icon_path, executable_icon)
        client_width, client_height = client_size_for_outer_window(
            self.root,
            self.WINDOW_OUTER_WIDTH,
            self.WINDOW_OUTER_HEIGHT,
        )
        self.root.minsize(client_width, client_height)
        saved_geometry = str(self.settings.get("window_geometry", ""))
        position_match = re.fullmatch(r"\d+x\d+([+-]\d+[+-]\d+)", saved_geometry)
        saved_position = position_match.group(1) if position_match else ""
        self.root.geometry(f"{client_width}x{client_height}{saved_position}")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.server_status_var = tk.StringVar()
        self.model_var = tk.StringVar()
        self.preset_var = tk.StringVar()
        self.use_preset_var = tk.BooleanVar(
            value=bool(self.settings.get("use_selected_preset", False))
        )
        self.run_status_var = tk.StringVar(value="Status: Stopped")
        self.pid_var = tk.StringVar(value="PID: -")
        self.web_search_enabled_var = tk.BooleanVar(
            value=self.web_search_settings.enabled
        )
        self.update_text_var = tk.StringVar(value=f"v{self.version}")

        self._build_ui()
        self._update_server_status()
        self._reset_parameters(safe_profile=True)
        self._refresh_models(initial=True)
        self._start_capability_detection()
        self._update_buttons()
        self._schedule_preview()
        self.root.after(100, self._poll_events)
        self.tray.start()
        self.root.after(0, self._start_automatic_update_check)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        server_row = ttk.Frame(outer)
        server_row.pack(fill="x", pady=(0, 7))
        ttk.Label(server_row, textvariable=self.server_status_var).pack(side="left", fill="x", expand=True)
        self.detect_button = ttk.Button(server_row, text="Recheck CLI", command=self._start_capability_detection)
        self.detect_button.pack(side="right")

        selection = ttk.LabelFrame(outer, text="Model and preset", padding=8)
        selection.pack(fill="x", pady=(0, 8))
        selection.columnconfigure(1, weight=1)

        ttk.Label(selection, text="Model:").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.model_combo = ttk.Combobox(selection, textvariable=self.model_var, state="readonly")
        self.model_combo.grid(row=0, column=1, sticky="ew")
        self.model_combo.bind("<<ComboboxSelected>>", self._on_model_selected)
        ttk.Button(selection, text="Refresh models", command=self._refresh_models).grid(
            row=0,
            column=2,
            sticky="ew",
            padx=(8, 0),
        )

        ttk.Label(selection, text="Preset:").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(7, 0))
        self.preset_combo = ttk.Combobox(selection, textvariable=self.preset_var, state="readonly")
        self.preset_combo.grid(row=1, column=1, sticky="ew", pady=(7, 0))
        self.preset_combo.bind("<<ComboboxSelected>>", self._on_preset_selected)
        ttk.Button(
            selection,
            text="Refresh presets",
            command=self._refresh_presets,
        ).grid(
            row=1,
            column=2,
            sticky="ew",
            padx=(8, 0),
            pady=(7, 0),
        )

        notebook = ttk.Notebook(outer)
        notebook.pack(fill="both", expand=True, pady=(0, 8))
        category_frames: dict[str, ttk.Frame] = {}
        for category in CATEGORIES:
            scroller = ScrollableFrame(notebook)
            notebook.add(scroller, text=category)
            category_frames[category] = scroller.content

        category_rows = {category: 0 for category in CATEGORIES}
        for spec in PARAMETER_SPECS:
            parent = category_frames[spec.category]
            row = category_rows[spec.category]
            control = ParameterControl(parent, spec, row, self._schedule_preview)
            self.parameter_controls[spec.key] = control
            category_rows[spec.category] += 1

        preview_frame = ttk.LabelFrame(outer, text="Command preview", padding=6)
        preview_frame.pack(fill="x", pady=(0, 8))
        self.preview_text = tk.Text(preview_frame, height=7, wrap="none", font=("Consolas", 9))
        preview_scroll = ttk.Scrollbar(preview_frame, orient="horizontal", command=self.preview_text.xview)
        self.preview_text.configure(xscrollcommand=preview_scroll.set, state="disabled")
        self.preview_text.pack(fill="x")
        preview_scroll.pack(fill="x")

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(0, 8))
        self.start_button = ttk.Button(actions, text="Start", command=self._start_server)
        self.start_button.grid(row=0, column=0, sticky="w")
        self.stop_button = ttk.Button(actions, text="Stop", command=self._stop_server)
        self.stop_button.grid(row=0, column=1, sticky="w", padx=(5, 0))
        self.open_web_button = ttk.Button(actions, text="Open Web UI", command=self._open_web_ui)
        self.open_web_button.grid(row=0, column=2, sticky="w", padx=(5, 0))

        ttk.Separator(actions, orient="vertical").grid(
            row=0,
            column=3,
            sticky="ns",
            padx=(12, 8),
        )
        self.save_button = ttk.Button(actions, text="Save preset", command=self._save_preset)
        self.save_button.grid(row=0, column=4, sticky="w")
        ttk.Button(actions, text="Clear", command=self._clear).grid(
            row=0,
            column=5,
            sticky="w",
            padx=(5, 0),
        )

        use_preset = ttk.Checkbutton(
            actions,
            text="Start using selected preset",
            variable=self.use_preset_var,
            command=self._on_use_preset_toggled,
        )
        use_preset.grid(row=1, column=0, columnspan=3, sticky="w", pady=(5, 0))
        Tooltip(use_preset, "Launch directly from the preset without replacing current UI values.")

        actions.columnconfigure(6, weight=1)
        ttk.Label(actions, textvariable=self.run_status_var).grid(
            row=0,
            column=7,
            sticky="w",
        )
        ttk.Label(actions, textvariable=self.pid_var).grid(
            row=0,
            column=8,
            sticky="e",
            padx=(10, 0),
        )

        web_search_row = ttk.Frame(actions)
        web_search_row.grid(row=1, column=7, columnspan=2, sticky="w", pady=(5, 0))
        self.web_search_check = ttk.Checkbutton(
            web_search_row,
            text="Web search (SearXNG)",
            variable=self.web_search_enabled_var,
            command=self._on_web_search_toggled,
        )
        self.web_search_check.pack(side="left")
        self.web_search_settings_button = ttk.Button(
            web_search_row,
            text="⚙",
            width=3,
            command=self._open_web_search_settings,
        )
        self.web_search_settings_button.pack(side="left", padx=(5, 0))
        Tooltip(self.web_search_settings_button, "Configure the external SearXNG server.")

        output_frame = ttk.LabelFrame(outer, text="Server output", padding=6)
        output_frame.pack(fill="both", expand=False)
        self.output_text = tk.Text(output_frame, height=10, wrap="word", font=("Consolas", 9), state="disabled")
        output_scroll = ttk.Scrollbar(output_frame, command=self.output_text.yview)
        self.output_text.configure(yscrollcommand=output_scroll.set)
        self.output_text.pack(side="left", fill="both", expand=True)
        output_scroll.pack(side="right", fill="y")

        footer = ttk.Frame(outer)
        footer.pack(fill="x", pady=(2, 0))
        style = ttk.Style(self.root)
        style.configure("Updater.Version.TLabel", foreground="#6b6b6b")
        style.configure("Updater.Attention.TLabel", foreground="#b24a4a")
        self.update_action = ttk.Label(
            footer,
            textvariable=self.update_text_var,
            style="Updater.Version.TLabel",
            cursor="hand2",
            takefocus=True,
            padding=(2, 0),
        )
        self.update_action.pack(side="right")
        self.update_action.bind("<Button-1>", self._activate_update_action)
        self.update_action.bind("<Return>", self._activate_update_action)
        self.update_action.bind("<space>", self._activate_update_action)
        self.update_tooltip = Tooltip(self.update_action, "Click to check for updates.")
        self.update_activity = ttk.Progressbar(
            footer,
            mode="indeterminate",
            length=36,
            maximum=10,
        )

    def _set_update_state(self, state: UpdateState, message: str = "") -> None:
        current = self.update_state
        if state != current and state not in self._UPDATE_TRANSITIONS[current]:
            LOGGER.warning("[Updater] Ignoring invalid state transition: %s -> %s", current, state)
            return

        self.update_state = state
        attention = state in {
            UpdateState.AVAILABLE,
            UpdateState.INSTALLING,
            UpdateState.ERROR,
        }
        if state == UpdateState.ERROR:
            text = "Update error"
        elif state in {UpdateState.AVAILABLE, UpdateState.INSTALLING}:
            text = "Install update"
        else:
            text = f"v{self.version}"
        actionable = state in {UpdateState.IDLE, UpdateState.AVAILABLE, UpdateState.ERROR}
        self.update_text_var.set(text)
        self.update_action.configure(
            cursor="hand2" if actionable else "",
            style="Updater.Attention.TLabel" if attention else "Updater.Version.TLabel",
        )

        busy = state in {UpdateState.CHECKING, UpdateState.INSTALLING}
        if busy and not self.update_activity_visible:
            self.update_activity.pack(side="right", padx=(0, 5))
            self.update_activity.start(12)
            self.update_activity_visible = True
        elif not busy and self.update_activity_visible:
            self.update_activity.stop()
            self.update_activity.pack_forget()
            self.update_activity_visible = False

        default_tooltips = {
            UpdateState.IDLE: "Click to check for updates.",
            UpdateState.CHECKING: "Checking for updates...",
            UpdateState.AVAILABLE: "Click to install the available update.",
            UpdateState.INSTALLING: "Starting update installation...",
            UpdateState.ERROR: "Click to retry the update check.",
        }
        self.update_tooltip.text = message or default_tooltips[state]

    def _start_automatic_update_check(self) -> None:
        if self.auto_update_check_started:
            return
        self.auto_update_check_started = True
        self._start_update_check()

    def _activate_update_action(self, _event: tk.Event[Any] | None = None) -> None:
        if self.update_state in {UpdateState.IDLE, UpdateState.ERROR}:
            self._start_update_check()
        elif self.update_state == UpdateState.AVAILABLE:
            self._start_update_installation()

    def _start_update_check(self) -> None:
        if self.closing or self.quit_finished:
            return
        if self.update_state not in {UpdateState.IDLE, UpdateState.ERROR}:
            return
        self._set_update_state(UpdateState.CHECKING, "Checking for updates...")

        def worker() -> None:
            self.background_events.put(("update_check", self.updater.check()))

        threading.Thread(target=worker, daemon=True).start()

    def _start_update_installation(self) -> None:
        if self.closing or self.quit_finished or self.update_state != UpdateState.AVAILABLE:
            return
        self._set_update_state(UpdateState.INSTALLING, "Starting update installation...")

        def worker() -> None:
            self.background_events.put(("update_install", self.updater.install()))

        threading.Thread(target=worker, daemon=True).start()

    def _load_settings(self) -> dict[str, Any]:
        try:
            with self.paths.settings.open("r", encoding="utf-8") as file:
                data = json.load(file)
            if not isinstance(data, dict):
                return {}
            data.setdefault("use_selected_preset", False)
            return data
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_settings(self) -> None:
        model = self._selected_model()
        data = {
            "window_geometry": self.root.geometry(),
            "last_model": model.relative_path.as_posix() if model else "",
            "last_preset": self.preset_var.get(),
            "use_selected_preset": bool(self.use_preset_var.get()),
            "web_search": getattr(
                self,
                "web_search_settings",
                WebSearchSettings(),
            ).to_mapping(),
        }
        try:
            with self.paths.settings.open("w", encoding="utf-8", newline="\n") as file:
                json.dump(data, file, ensure_ascii=False, indent=2)
                file.write("\n")
        except OSError as exc:
            self._append_log(f"Could not save launcher settings: {exc}")

    def _update_server_status(self) -> None:
        if self.paths.server.is_file():
            fallback = " (temporary development fallback)" if self.paths.using_development_fallback else ""
            self.server_status_var.set(f"llama-server.exe: found{fallback} — {self.paths.server}")
        else:
            self.server_status_var.set(f"llama-server.exe: NOT FOUND — expected {self.paths.server}")

    def _refresh_models(self, initial: bool = False) -> None:
        current = self._selected_model()
        wanted = current.relative_path.as_posix() if current else str(self.settings.get("last_model", ""))
        try:
            models = self.model_scanner.scan()
        except OSError as exc:
            models = []
            messagebox.showerror("Model scan failed", f"Could not scan models:\n{exc}")
        self.models_by_display = {model.display_name: model for model in models}
        values = list(self.models_by_display)
        self.model_combo.configure(values=values)
        selected = next(
            (model.display_name for model in models if model.relative_path.as_posix() == wanted),
            values[0] if values else "",
        )
        self.model_var.set(selected)
        self._refresh_presets(initial=initial)
        if not models and not initial:
            self._append_log(f"No .gguf models found under {self.paths.models}")
        self._update_buttons()

    def _on_model_selected(self, _event: tk.Event[Any] | None = None) -> None:
        self._refresh_presets()
        self._update_buttons()

    def _selected_model(self) -> ModelInfo | None:
        return self.models_by_display.get(self.model_var.get())

    def _refresh_presets(
        self,
        initial: bool = False,
        preferred: str | None = None,
    ) -> None:
        model = self._selected_model()
        current = preferred if preferred is not None else self.preset_var.get()
        wanted = current or (str(self.settings.get("last_preset", "")) if initial else "")
        paths = self.preset_manager.scan(model) if model else []
        self.presets_by_display = {path.stem: path for path in paths}
        values = list(self.presets_by_display)
        self.preset_combo.configure(values=values)
        self.preset_var.set(wanted if wanted in self.presets_by_display else (values[0] if values else ""))
        self.save_button.configure(state="normal" if model else "disabled")
        self._apply_selected_preset()
        if not initial:
            self._save_settings()

    def _on_preset_selected(self, _event: tk.Event[Any] | None = None) -> None:
        self._apply_selected_preset()
        self._save_settings()

    def _selected_preset(self) -> Path | None:
        return self.presets_by_display.get(self.preset_var.get())

    def _reset_parameters(self, safe_profile: bool) -> None:
        state = default_parameter_state(safe_profile=safe_profile)
        for key, control in self.parameter_controls.items():
            control.set_state(state[key])
        self._schedule_preview()

    def _current_parameter_state(self) -> dict[str, dict[str, Any]]:
        return {key: control.get_state() for key, control in self.parameter_controls.items()}

    def _preset_parameter_state(
        self,
        path: Path,
    ) -> tuple[dict[str, dict[str, Any]], list[str], dict[str, Any]]:
        document = self.preset_manager.load(path)
        raw_parameters = document["parameters"]
        state = default_parameter_state(safe_profile=False)
        warnings: list[str] = []
        for key, raw_state in raw_parameters.items():
            if key not in SPEC_BY_KEY:
                warnings.append(f"Unknown preset parameter ignored: {key}")
                continue
            if not isinstance(raw_state, Mapping):
                warnings.append(f"Malformed preset parameter ignored: {key}")
                continue
            state[key] = {
                "enabled": bool(raw_state.get("enabled", False)),
                "value": raw_state.get("value", SPEC_BY_KEY[key].default),
            }
        return state, warnings, document

    def _state_for_command(self) -> tuple[dict[str, dict[str, Any]], list[str]]:
        if not self.use_preset_var.get():
            return self._current_parameter_state(), []
        preset = self._selected_preset()
        if preset is None or not preset.is_file():
            raise CommandValidationError("Select an existing preset or turn off 'Start using selected preset'.")
        state, warnings, _document = self._preset_parameter_state(preset)
        return state, warnings

    def _apply_selected_preset(self) -> None:
        path = self._selected_preset()
        if path is None:
            self._reset_parameters(safe_profile=True)
            return
        try:
            state, warnings, document = self._preset_parameter_state(path)
        except PresetError as exc:
            messagebox.showerror("Could not load preset", str(exc))
            self._schedule_preview()
            return
        for key, control in self.parameter_controls.items():
            control.set_state(state[key])
        self._report_preset_warnings(warnings, document)
        self._append_log(f"Loaded preset: {path}")
        self._schedule_preview()

    def _on_use_preset_toggled(self) -> None:
        self._save_settings()
        self._schedule_preview()

    def _report_preset_warnings(self, warnings: list[str], document: Mapping[str, Any]) -> None:
        model = self._selected_model()
        preset_model = document.get("model", {})
        preset_relative = preset_model.get("relative_path") if isinstance(preset_model, Mapping) else None
        if model and preset_relative and preset_relative != model.relative_path.as_posix():
            warnings.append(
                f"Preset was saved for '{preset_relative}', selected model is '{model.relative_path.as_posix()}'."
            )
        for warning in warnings:
            self._append_log(f"WARNING: {warning}")

    def _save_preset(self) -> None:
        model = self._selected_model()
        if model is None:
            messagebox.showerror("Save preset", "Select a model first.")
            return
        name = simpledialog.askstring("Save preset", "Preset name:", parent=self.root)
        if name is None:
            return
        if not name.strip():
            messagebox.showerror("Save preset", "Preset name cannot be empty.")
            return
        target = self.preset_manager.path_for_name(model, name)
        if target.exists() and not messagebox.askyesno(
            "Overwrite preset", f"'{target.name}' already exists. Overwrite it?", parent=self.root
        ):
            return
        try:
            saved = self.preset_manager.save(model, name, self._current_parameter_state())
        except PresetError as exc:
            messagebox.showerror("Could not save preset", str(exc))
            return
        self._append_log(f"Saved preset: {saved}")
        self._refresh_presets(preferred=saved.stem)

    def _clear(self) -> None:
        self._reset_parameters(safe_profile=True)
        self._append_log("Parameters reset to the safe universal profile.")

    def _on_web_search_toggled(self) -> None:
        self.web_search_settings = replace(
            self.web_search_settings,
            enabled=bool(self.web_search_enabled_var.get()),
        )
        self._save_settings()
        self._schedule_preview()

    def _open_web_search_settings(self) -> None:
        window = self.web_search_window
        if window is not None and bool(window.winfo_exists()):
            window.deiconify()
            window.lift()
            window.focus_force()
            return
        self.web_search_window = self._create_web_search_settings_window()

    def _create_web_search_settings_window(self) -> tk.Toplevel:
        window = tk.Toplevel(self.root)
        window.title("Web search (SearXNG)")
        window.resizable(False, False)
        window.transient(self.root)
        container = ttk.Frame(window, padding=12)
        container.grid(row=0, column=0, sticky="nsew")
        container.columnconfigure(0, weight=1)

        self.web_search_url_var = tk.StringVar(value=self.web_search_settings.url)
        self.web_search_results_var = tk.StringVar(
            value=str(self.web_search_settings.max_results)
        )
        self.web_search_timeout_var = tk.StringVar(
            value=str(self.web_search_settings.timeout)
        )
        self.web_search_test_status_var = tk.StringVar(value="Not tested")

        ttk.Label(container, text="URL:").grid(row=0, column=0, sticky="w")
        url_entry = ttk.Entry(container, textvariable=self.web_search_url_var, width=42)
        url_entry.grid(row=1, column=0, sticky="ew", pady=(2, 9))
        self.web_search_test_button = ttk.Button(
            container,
            text="Test connection",
            command=self._start_searxng_test,
        )
        self.web_search_test_button.grid(row=2, column=0, sticky="w", pady=(0, 12))

        ttk.Label(container, text="Results:").grid(row=3, column=0, sticky="w")
        results = ttk.Spinbox(
            container,
            from_=1,
            to=20,
            width=8,
            textvariable=self.web_search_results_var,
        )
        results.grid(row=4, column=0, sticky="w", pady=(2, 9))
        ttk.Label(container, text="Timeout (s):").grid(row=5, column=0, sticky="w")
        timeout = ttk.Entry(
            container,
            textvariable=self.web_search_timeout_var,
            width=12,
        )
        timeout.grid(row=6, column=0, sticky="w", pady=(2, 12))
        ttk.Label(container, text="Status:").grid(row=7, column=0, sticky="w")
        ttk.Label(
            container,
            textvariable=self.web_search_test_status_var,
            wraplength=315,
        ).grid(row=8, column=0, sticky="w", pady=(2, 0))
        self.web_search_save_button = ttk.Button(
            container,
            text="Save",
            command=self._save_web_search_settings,
        )
        self.web_search_save_button.grid(row=9, column=0, sticky="e", pady=(12, 0))
        window.protocol("WM_DELETE_WINDOW", self._close_web_search_settings)
        window.bind("<Destroy>", self._on_web_search_window_destroyed, add=True)
        url_entry.focus_set()
        return window

    def _read_web_search_dialog_settings(
        self, *, show_error: bool
    ) -> WebSearchSettings | None:
        try:
            settings = validate_web_search_settings(
                enabled=bool(self.web_search_enabled_var.get()),
                url=self.web_search_url_var.get(),
                max_results=self.web_search_results_var.get(),
                timeout=self.web_search_timeout_var.get(),
            )
        except WebSearchSettingsError as exc:
            self.web_search_test_status_var.set(f"Error — {exc}")
            if show_error:
                messagebox.showerror("Web search settings", str(exc), parent=self.web_search_window)
            return None
        return settings

    def _save_web_search_settings(self) -> None:
        settings = self._read_web_search_dialog_settings(show_error=True)
        if settings is None:
            return
        changed = settings != self.web_search_settings
        self.web_search_settings = settings
        self._save_settings()
        if changed and settings.enabled:
            self._schedule_preview()
        self.web_search_url_var.set(settings.url)
        self.web_search_results_var.set(str(settings.max_results))
        self.web_search_timeout_var.set(str(settings.timeout))
        self.web_search_test_status_var.set("Settings saved")

    def _start_searxng_test(self) -> None:
        settings = self._read_web_search_dialog_settings(show_error=True)
        if settings is None:
            return
        self.web_search_test_button.configure(state="disabled")
        self.web_search_test_status_var.set("Checking…")

        def worker() -> None:
            result = test_searxng_connection(settings)
            self.background_events.put(("searxng_test", result))

        threading.Thread(target=worker, daemon=True).start()

    def _handle_searxng_test_result(self, value: object) -> None:
        result = (
            value
            if isinstance(value, ConnectionTestResult)
            else ConnectionTestResult(False, "Error — invalid test result")
        )
        window = self.web_search_window
        if window is None or not bool(window.winfo_exists()):
            return
        self.web_search_test_button.configure(state="normal")
        self.web_search_test_status_var.set(result.message)
        if result.detail:
            LOGGER.warning("SearXNG connection test: %s", result.detail)

    def _close_web_search_settings(self) -> None:
        window = self.web_search_window
        if window is None:
            return
        window.destroy()
        self.web_search_window = None

    def _on_web_search_window_destroyed(self, event: tk.Event[Any]) -> None:
        if event.widget is self.web_search_window:
            self.web_search_window = None

    def _start_capability_detection(self) -> None:
        if not self.paths.server.is_file():
            self.supported_keys = None
            self.supports_mcp_servers_json = False
            self._apply_supported_state()
            self._update_buttons()
            return
        self.detect_button.configure(state="disabled")
        self._append_log("Reading llama-server --help...")

        def worker() -> None:
            result = detect_supported_parameters(self.paths.server)
            self.background_events.put(("detection", result))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_supported_state(self) -> None:
        for key, control in self.parameter_controls.items():
            control.set_supported(self.supported_keys is None or key in self.supported_keys)
        self._schedule_preview()

    def _schedule_preview(self) -> None:
        if not hasattr(self, "preview_text"):
            return
        if self.preview_after_id is not None:
            self.root.after_cancel(self.preview_after_id)
        self.preview_after_id = self.root.after(self.PREVIEW_DELAY_MS, self._update_preview)

    def _update_preview(self) -> None:
        self.preview_after_id = None
        model = self._selected_model()
        try:
            if model is None:
                preview = f"Select a GGUF model. Models directory: {self.paths.models}"
            else:
                state, warnings = self._state_for_command()
                command = build_command(
                    self.paths.server,
                    model.path,
                    state,
                    supported_keys=self.supported_keys,
                    web_search=self.web_search_settings,
                    web_mcp_path=self.paths.web_mcp,
                    supports_mcp_servers_json=self.supports_mcp_servers_json,
                )
                preview = format_windows_command(command)
                omitted = self._unsupported_enabled_keys(state)
                notes = warnings + ([f"Unsupported selected parameters omitted: {', '.join(omitted)}"] if omitted else [])
                if notes:
                    preview += "\n\n# " + "\n# ".join(notes)
        except (CommandValidationError, PresetError) as exc:
            preview = f"Cannot build command: {exc}"
        self._replace_text(self.preview_text, preview)

    def _unsupported_enabled_keys(self, state: Mapping[str, Mapping[str, Any]]) -> list[str]:
        if self.supported_keys is None:
            return []
        return [
            key
            for key, item in state.items()
            if bool(item.get("enabled", False)) and key not in self.supported_keys
        ]

    def _start_server(self) -> None:
        if self.server_process.is_running():
            messagebox.showwarning("llama-server", "llama-server is already running.")
            return
        model = self._selected_model()
        if model is None:
            messagebox.showerror("Cannot start", "Select a GGUF model first.")
            return
        try:
            state, warnings = self._state_for_command()
            command = build_command(
                self.paths.server,
                model.path,
                state,
                supported_keys=self.supported_keys,
                web_search=self.web_search_settings,
                web_mcp_path=self.paths.web_mcp,
                supports_mcp_servers_json=self.supports_mcp_servers_json,
            )
            server_url = build_server_url(state, self.supported_keys)
            if self.web_search_settings.enabled:
                validate_web_mcp_executable(self.paths.web_mcp)
        except (CommandValidationError, PresetError) as exc:
            messagebox.showerror("Cannot start llama-server", str(exc))
            return

        for warning in warnings:
            self._append_log(f"WARNING: {warning}")
        omitted = self._unsupported_enabled_keys(state)
        if omitted:
            self._append_log(f"WARNING: unsupported parameters omitted: {', '.join(omitted)}")
        ctx_state = state.get("ctx_size", {})
        try:
            if bool(ctx_state.get("enabled")) and int(ctx_state.get("value", 0)) >= 131_072:
                self._append_log("WARNING: very large context sizes may require a large amount of RAM/VRAM.")
        except (TypeError, ValueError):
            pass

        self._append_log("Starting command:\n" + format_windows_command(command))
        try:
            pid = self.server_process.start(command, self.paths.llama_root)
        except (OSError, RuntimeError) as exc:
            self._append_log(traceback.format_exc())
            messagebox.showerror("Could not start llama-server", str(exc))
            return
        self.server_url = server_url
        self.stopping = False
        self.run_status_var.set("Status: Running")
        self.pid_var.set(f"PID: {pid}")
        self._update_buttons()

    def _stop_server(self) -> None:
        if not self.server_process.is_running():
            return
        self.stopping = True
        self.run_status_var.set("Status: Stopping")
        self.server_process.stop_async()
        self._update_buttons()

    def _open_web_ui(self) -> None:
        if not self.server_process.is_running() or self.server_url is None:
            messagebox.showinfo("Web UI", "Start llama-server first.")
            return
        try:
            opened = webbrowser.open(self.server_url, new=2)
        except (webbrowser.Error, OSError) as exc:
            messagebox.showerror("Could not open Web UI", str(exc))
            return
        if not opened:
            messagebox.showerror(
                "Could not open Web UI",
                f"The default browser could not be opened.\nOpen this address manually:\n{self.server_url}",
            )
            return
        self._append_log(f"Opened Web UI: {self.server_url}")

    def _update_buttons(self) -> None:
        running = self.server_process.is_running()
        can_start = self.paths.server.is_file() and self._selected_model() is not None and not running
        self.start_button.configure(state="normal" if can_start else "disabled")
        self.stop_button.configure(state="normal" if running and not self.stopping else "disabled")
        self.open_web_button.configure(
            state="normal" if running and not self.stopping and self.server_url else "disabled"
        )

    def _poll_events(self) -> None:
        try:
            while True:
                kind, value = self.server_process.events.get_nowait()
                self._handle_server_event(kind, value)
                if self.quit_finished:
                    return
        except queue.Empty:
            pass

        try:
            while True:
                kind, value = self.background_events.get_nowait()
                self._handle_background_event(kind, value)
                if self.quit_finished:
                    return
        except queue.Empty:
            pass

        if not self.quit_finished:
            self.root.after(100, self._poll_events)

    def _handle_server_event(self, kind: str, value: object) -> None:
        if kind == "log":
            self._append_log(str(value))
        elif kind == "exit":
            self.stopping = False
            self.server_url = None
            self.run_status_var.set(f"Status: Stopped (exit code {value})")
            self.pid_var.set("PID: -")
            self._append_log(f"llama-server exited with code {value}.")
            self._update_buttons()
            if self.closing:
                self._finish_quit()

    def _handle_background_event(self, kind: str, value: object) -> None:
        if kind == "detection":
            self._handle_detection(value)
        elif kind == "update_check":
            self._handle_update_check_result(value)
        elif kind == "update_install":
            self._handle_update_install_result(value)
        elif kind == "searxng_test":
            self._handle_searxng_test_result(value)
        elif kind == "tray_open":
            self._show_main_window()
        elif kind == "tray_quit":
            self._request_quit()

    def _handle_update_check_result(self, value: object) -> None:
        result = (
            value
            if isinstance(value, CheckResult)
            else CheckResult(False, message="invalid updater check result")
        )
        if not result.ok:
            message = result.message or "update check failed"
            self._set_update_state(UpdateState.ERROR, message)
            self._append_log(f"[Updater] Update check failed: {message}")
        elif result.update_available:
            self._set_update_state(UpdateState.AVAILABLE, "Update available")
        else:
            self._set_update_state(UpdateState.IDLE, "Application is up to date")

    def _handle_update_install_result(self, value: object) -> None:
        result = (
            value
            if isinstance(value, InstallResult)
            else InstallResult(False, message="invalid updater install result")
        )
        if not result.ok:
            message = result.message or "failed to start update installation"
            self._set_update_state(UpdateState.AVAILABLE, message)
            self._append_log(f"[Updater] Update installation failed: {message}")
            return

        self._append_log(f"[Updater] Update installation process started: pid={result.pid}")
        self._append_log("[Updater] Application shutdown scheduled after update installation start")
        LOGGER.info("[Updater] Application shutdown scheduled after update installation start")
        self.root.after(self.UPDATE_SHUTDOWN_DELAY_MS, self._request_quit)

    def _handle_detection(self, value: object) -> None:
        result = value if isinstance(value, DetectionResult) else DetectionResult("", None, "Invalid result")
        self.detect_button.configure(state="normal")
        self.supported_keys = result.supported_keys
        self.supports_mcp_servers_json = result.supports_mcp_servers_json
        self._apply_supported_state()
        if result.error:
            self._append_log(
                f"Could not inspect llama-server --help: {result.error}. Built-in parameter list remains available."
            )
        else:
            unsupported = len(PARAMETER_SPECS) - len(result.supported_keys or ())
            self._append_log(
                f"CLI detection complete: {len(result.supported_keys or ())} supported, {unsupported} unsupported."
            )

    def _replace_text(self, widget: tk.Text, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _append_log(self, text: str) -> None:
        if not hasattr(self, "output_text"):
            return
        self.output_text.configure(state="normal")
        self.output_text.insert("end", text.rstrip("\n") + "\n")
        self.output_text.see("end")
        self.output_text.configure(state="disabled")

    def _on_close(self) -> None:
        self._hide_main_window()

    def _hide_main_window(self) -> None:
        if self.closing or self.quit_finished:
            return
        self.root.withdraw()

    def _show_main_window(self) -> None:
        if self.closing or self.quit_finished:
            return
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.after_idle(self._focus_main_window)

    def _focus_main_window(self) -> None:
        if self.closing or self.quit_finished:
            return
        self.root.lift()
        self.root.focus_force()

    def _request_quit(self) -> None:
        if self.closing or self.quit_finished:
            return
        self.closing = True
        if self.server_process.is_running():
            self._stop_server()
            return
        self._finish_quit()

    def _finish_quit(self) -> None:
        if self.quit_finished:
            return
        self.quit_finished = True
        self._save_settings()
        try:
            self.tray.shutdown()
        except Exception:
            LOGGER.warning("Could not shut down tray cleanly", exc_info=True)
        try:
            self.root.destroy()
        finally:
            self.window_icon_handles.close()

    def _finish_close(self) -> None:
        """Compatibility alias for the former full-close helper."""
        self._finish_quit()
