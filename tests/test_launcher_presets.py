from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from internal.app import LauncherApp
from internal.llama_server import default_parameter_state
from internal.model_scanner import ModelInfo
from internal.web_search_settings import WebSearchSettings


class Variable:
    def __init__(self, value: object = "") -> None:
        self.value = value

    def get(self) -> object:
        return self.value

    def set(self, value: object) -> None:
        self.value = value


def make_model(root: Path, name: str = "model.gguf") -> ModelInfo:
    relative = Path(name)
    return ModelInfo(root / relative, relative, relative.as_posix(), f"{relative.stem}--test")


def make_preset_app(root: Path) -> tuple[LauncherApp, ModelInfo, Mock]:
    app = LauncherApp.__new__(LauncherApp)
    model = make_model(root)
    control = Mock()
    app.settings = {}
    app.models_by_display = {model.display_name: model}
    app.model_var = Variable(model.display_name)
    app.preset_var = Variable()
    app.preset_combo = Mock()
    app.save_button = Mock()
    app.preset_manager = Mock()
    app.parameter_controls = {"port": control}
    app._append_log = Mock()
    app._schedule_preview = Mock()
    app._save_settings = Mock()
    return app, model, control


def preset_document(model: ModelInfo, port: int) -> dict[str, object]:
    return {
        "schema_version": 1,
        "model": {"relative_path": model.relative_path.as_posix()},
        "parameters": {"port": {"enabled": True, "value": port}},
    }


class LauncherSettingsTests(unittest.TestCase):
    def test_old_settings_without_use_preset_key_default_to_false(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "laucher-settings.json"
            path.write_text(json.dumps({"last_model": "old.gguf"}), encoding="utf-8")
            app = LauncherApp.__new__(LauncherApp)
            app.paths = SimpleNamespace(settings=path)

            loaded = app._load_settings()

            self.assertIs(loaded["use_selected_preset"], False)

    def test_save_serializes_use_selected_preset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "laucher-settings.json"
            app = LauncherApp.__new__(LauncherApp)
            app.paths = SimpleNamespace(settings=path)
            app.root = Mock()
            app.root.geometry.return_value = "834x940+1+2"
            app.preset_var = Variable("fast")
            app.use_preset_var = Variable(True)
            app.web_search_settings = WebSearchSettings()
            app._selected_model = Mock(return_value=None)
            app._append_log = Mock()

            app._save_settings()

            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertIs(document["use_selected_preset"], True)
            self.assertEqual(document["last_preset"], "fast")
            self.assertEqual(
                set(document),
                {
                    "window_geometry",
                    "last_model",
                    "last_preset",
                    "use_selected_preset",
                    "web_search",
                },
            )

    def test_use_preset_toggle_saves_and_updates_preview(self) -> None:
        app = LauncherApp.__new__(LauncherApp)
        app._save_settings = Mock()
        app._schedule_preview = Mock()

        app._on_use_preset_toggled()

        app._save_settings.assert_called_once_with()
        app._schedule_preview.assert_called_once_with()


class AutomaticPresetTests(unittest.TestCase):
    def test_initial_refresh_restores_and_loads_saved_preset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app, model, control = make_preset_app(root)
            first = root / "first.json"
            saved = root / "saved.json"
            app.settings = {"last_preset": saved.stem}
            app.preset_manager.scan.return_value = [first, saved]
            app.preset_manager.load.side_effect = lambda path: preset_document(
                model, 8686 if path == saved else 8080
            )

            app._refresh_presets(initial=True)

            self.assertEqual(app.preset_var.get(), saved.stem)
            control.set_state.assert_called_once_with({"enabled": True, "value": 8686})
            app._save_settings.assert_not_called()

    def test_manual_selection_loads_parameter_state_and_saves_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app, model, control = make_preset_app(root)
            selected = root / "balanced.json"
            app.presets_by_display = {selected.stem: selected}
            app.preset_var.set(selected.stem)
            app.preset_manager.load.return_value = preset_document(model, 8181)

            app._on_preset_selected()

            control.set_state.assert_called_once_with({"enabled": True, "value": 8181})
            app._save_settings.assert_called_once_with()
            app._schedule_preview.assert_called_once_with()

    def test_model_selection_refreshes_and_loads_selected_models_preset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app, model, control = make_preset_app(root)
            selected = root / "model-default.json"
            app.preset_manager.scan.return_value = [selected]
            app.preset_manager.load.return_value = preset_document(model, 8282)
            app._update_buttons = Mock()

            app._on_model_selected()

            self.assertEqual(app.preset_var.get(), selected.stem)
            control.set_state.assert_called_once_with({"enabled": True, "value": 8282})
            app._save_settings.assert_called_once_with()

    def test_refresh_preserves_existing_preset_and_loads_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app, model, control = make_preset_app(root)
            first = root / "first.json"
            current = root / "current.json"
            app.preset_var.set(current.stem)
            app.preset_manager.scan.return_value = [first, current]
            app.preset_manager.load.side_effect = lambda path: preset_document(
                model, 8383 if path == current else 8080
            )

            app._refresh_presets()

            self.assertEqual(app.preset_var.get(), current.stem)
            control.set_state.assert_called_once_with({"enabled": True, "value": 8383})

    def test_empty_preset_list_resets_to_safe_state_without_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app, _model, control = make_preset_app(root)
            app.preset_var.set("missing")
            app.preset_manager.scan.return_value = []

            with patch("internal.app.messagebox.showerror") as showerror:
                app._refresh_presets()

            self.assertEqual(app.preset_var.get(), "")
            app.preset_combo.configure.assert_called_once_with(values=[])
            control.set_state.assert_called_once_with(
                default_parameter_state(safe_profile=True)["port"]
            )
            showerror.assert_not_called()

    def test_save_selects_and_loads_saved_preset_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app, model, control = make_preset_app(root)
            saved = root / "new-preset.json"
            control.get_state.return_value = {"enabled": True, "value": 8484}
            app.preset_manager.path_for_name.return_value = saved
            app.preset_manager.save.return_value = saved
            app.preset_manager.scan.return_value = [saved]
            app.preset_manager.load.return_value = preset_document(model, 8484)
            app.root = Mock()

            with patch("internal.app.simpledialog.askstring", return_value="new-preset"):
                app._save_preset()

            self.assertEqual(app.preset_var.get(), saved.stem)
            control.set_state.assert_called_once_with({"enabled": True, "value": 8484})
            app.preset_manager.load.assert_called_once_with(saved)
            app._save_settings.assert_called_once_with()

    def test_state_for_command_keeps_direct_json_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            preset = Path(temporary) / "direct.json"
            preset.touch()
            app = LauncherApp.__new__(LauncherApp)
            app.use_preset_var = Variable(True)
            app._selected_preset = Mock(return_value=preset)
            expected = {"port": {"enabled": True, "value": 8585}}
            app._preset_parameter_state = Mock(return_value=(expected, ["warning"], {}))

            state, warnings = app._state_for_command()

            self.assertEqual(state, expected)
            self.assertEqual(warnings, ["warning"])

    def test_state_for_command_uses_controls_when_checkbox_is_off(self) -> None:
        app = LauncherApp.__new__(LauncherApp)
        app.use_preset_var = Variable(False)
        expected = {"port": {"enabled": True, "value": 8787}}
        app._current_parameter_state = Mock(return_value=expected)
        app._selected_preset = Mock()

        state, warnings = app._state_for_command()

        self.assertEqual(state, expected)
        self.assertEqual(warnings, [])
        app._selected_preset.assert_not_called()


class PresetUiTests(unittest.TestCase):
    def test_ui_has_refresh_presets_without_load_button(self) -> None:
        source = inspect.getsource(LauncherApp._build_ui)
        class_source = inspect.getsource(LauncherApp)

        self.assertIn('text="Refresh presets"', source)
        self.assertNotIn('text="Load"', source)
        self.assertNotIn("load_button", class_source)

    def test_safe_profile_is_initialized_before_models_and_presets(self) -> None:
        source = inspect.getsource(LauncherApp.__init__)

        self.assertLess(
            source.index("self._reset_parameters(safe_profile=True)"),
            source.index("self._refresh_models(initial=True)"),
        )


if __name__ == "__main__":
    unittest.main()
