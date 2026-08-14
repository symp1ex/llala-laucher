from __future__ import annotations

import json
from pathlib import Path
import queue
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib import error as url_error

from internal.app import LauncherApp
from internal.app_paths import resolve_app_paths
from internal.llama_server import (
    CommandValidationError,
    build_command,
    default_parameter_state,
    detect_supported_parameters,
    validate_web_mcp_executable,
)
from internal.web_search_settings import (
    ConnectionTestResult,
    WebSearchSettings,
    test_searxng_connection,
)


class WebSearchCommandTests(unittest.TestCase):
    def make_files(self, root: Path) -> tuple[Path, Path, Path]:
        server = root / "llama-server.exe"
        model = root / "models" / "model.gguf"
        mcp = root / "mcp" / "web-mcp.exe"
        model.parent.mkdir()
        mcp.parent.mkdir()
        for path in (server, model, mcp):
            path.touch()
        return server, model, mcp

    def test_enabled_search_builds_cursor_json_and_windows_safe_argv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "portable folder"
            root.mkdir()
            server, model, mcp = self.make_files(root)
            settings = WebSearchSettings(True, "http://192.168.1.50:8080", 8, 15.0)

            command = build_command(
                server,
                model,
                default_parameter_state(),
                web_search=settings,
                web_mcp_path=mcp,
                supports_mcp_servers_json=True,
            )

            index = command.index("--mcp-servers-json")
            document = json.loads(command[index + 1])
            config = document["mcpServers"]["web-search"]
            self.assertEqual(config["command"], str(mcp.resolve()))
            self.assertEqual(
                config["args"],
                [
                    "--searxng-url",
                    "http://192.168.1.50:8080",
                    "--max-results",
                    "8",
                    "--timeout",
                    "15.0",
                ],
            )
            self.assertEqual(config["timeout_ms"], 20_000)

    def test_disabled_search_omits_mcp_argument(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            server, model, mcp = self.make_files(Path(temporary))
            command = build_command(
                server,
                model,
                default_parameter_state(),
                web_search=WebSearchSettings(),
                web_mcp_path=mcp,
            )
            self.assertNotIn("--mcp-servers-json", command)

    def test_enabled_search_requires_llama_mcp_support(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            server, model, mcp = self.make_files(Path(temporary))
            with self.assertRaisesRegex(CommandValidationError, "does not support"):
                build_command(
                    server,
                    model,
                    default_parameter_state(),
                    web_search=WebSearchSettings(enabled=True),
                    web_mcp_path=mcp,
                )

    def test_enabled_search_requires_mcp_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            server, model, mcp = self.make_files(Path(temporary))
            mcp.unlink()
            with self.assertRaisesRegex(CommandValidationError, "not found"):
                build_command(
                    server,
                    model,
                    default_parameter_state(),
                    web_search=WebSearchSettings(enabled=True),
                    web_mcp_path=mcp,
                    supports_mcp_servers_json=True,
                )

    def test_detection_reports_mcp_switch_independently(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            server = Path(temporary) / "llama-server.exe"
            server.touch()
            with patch("internal.llama_server.subprocess.run") as run:
                run.return_value.stdout = "--host --mcp-servers-json"
                result = detect_supported_parameters(server)
            self.assertTrue(result.supports_mcp_servers_json)

    def test_executable_self_check_failure_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "web-mcp.exe"
            executable.touch()
            with patch("internal.llama_server.subprocess.run", side_effect=OSError("bad image")):
                with self.assertRaisesRegex(CommandValidationError, "Could not start"):
                    validate_web_mcp_executable(executable)


class WebSearchSettingsTests(unittest.TestCase):
    def test_round_trip_through_existing_settings_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "laucher-settings.json"
            path.write_text(
                json.dumps(
                    {
                        "last_model": "old.gguf",
                        "web_search": {
                            "enabled": True,
                            "url": "http://192.168.1.50:8080/",
                            "max_results": 12,
                            "timeout": 22,
                        },
                    }
                ),
                encoding="utf-8",
            )
            app = LauncherApp.__new__(LauncherApp)
            app.paths = SimpleNamespace(settings=path)
            loaded = app._load_settings()
            settings = WebSearchSettings.from_mapping(loaded.get("web_search"))
            self.assertEqual(settings, WebSearchSettings(True, "http://192.168.1.50:8080", 12, 22.0))

    def test_old_and_damaged_settings_are_backward_compatible(self) -> None:
        self.assertEqual(WebSearchSettings.from_mapping(None), WebSearchSettings())
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "laucher-settings.json"
            path.write_text("{broken", encoding="utf-8")
            app = LauncherApp.__new__(LauncherApp)
            app.paths = SimpleNamespace(settings=path)
            self.assertEqual(app._load_settings(), {})

    def test_save_writes_web_search_without_renaming_settings_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "laucher-settings.json"
            app = LauncherApp.__new__(LauncherApp)
            app.paths = SimpleNamespace(settings=path)
            app.root = Mock()
            app.root.geometry.return_value = "900x900+1+2"
            app.preset_var = Mock()
            app.preset_var.get.return_value = "preset"
            app.web_search_settings = WebSearchSettings(
                True, "http://192.168.1.50:8080", 9, 17.5
            )
            app._selected_model = Mock(return_value=None)
            app._append_log = Mock()
            app._save_settings()
            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                document["web_search"],
                {
                    "enabled": True,
                    "url": "http://192.168.1.50:8080",
                    "max_results": 9,
                    "timeout": 17.5,
                },
            )

    def test_production_mcp_path_is_always_next_to_launcher_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = resolve_app_paths(root, development_root=None)
            self.assertEqual(paths.web_mcp, root.resolve() / "mcp" / "web-mcp.exe")

    def test_checkbox_change_saves_and_schedules_preview(self) -> None:
        app = LauncherApp.__new__(LauncherApp)
        app.web_search_settings = WebSearchSettings()
        app.web_search_enabled_var = Mock()
        app.web_search_enabled_var.get.return_value = True
        app._save_settings = Mock()
        app._schedule_preview = Mock()
        app._on_web_search_toggled()
        self.assertTrue(app.web_search_settings.enabled)
        app._save_settings.assert_called_once_with()
        app._schedule_preview.assert_called_once_with()


class WebSearchWindowTests(unittest.TestCase):
    def test_repeated_open_raises_existing_window(self) -> None:
        app = LauncherApp.__new__(LauncherApp)
        window = Mock()
        window.winfo_exists.return_value = True
        app.web_search_window = window
        app._create_web_search_settings_window = Mock()
        app._open_web_search_settings()
        window.deiconify.assert_called_once_with()
        window.lift.assert_called_once_with()
        window.focus_force.assert_called_once_with()
        app._create_web_search_settings_window.assert_not_called()

    def test_connection_test_runs_in_worker_and_queues_result(self) -> None:
        app = LauncherApp.__new__(LauncherApp)
        draft = WebSearchSettings(url="http://192.168.1.50:8080")
        app.web_search_test_button = Mock()
        app.web_search_test_status_var = Mock()
        app.background_events = queue.Queue()
        app._read_web_search_dialog_settings = Mock(return_value=draft)
        app._save_settings = Mock()
        expected = ConnectionTestResult(True, "OK")

        with (
            patch("internal.app.test_searxng_connection", return_value=expected) as test_connection,
            patch("internal.app.threading.Thread") as thread,
        ):
            app._start_searxng_test()
            test_connection.assert_not_called()
            thread.assert_called_once()
            thread.return_value.start.assert_called_once_with()
            target = thread.call_args.kwargs["target"]
            target()

        test_connection.assert_called_once_with(draft)
        app._save_settings.assert_not_called()
        self.assertEqual(app.background_events.get_nowait(), ("searxng_test", expected))

    def test_save_commits_dialog_values_and_updates_preview(self) -> None:
        app = LauncherApp.__new__(LauncherApp)
        app.web_search_settings = WebSearchSettings(enabled=True)
        saved = WebSearchSettings(True, "http://192.168.1.50:8080", 12, 20.0)
        app._read_web_search_dialog_settings = Mock(return_value=saved)
        app._save_settings = Mock()
        app._schedule_preview = Mock()
        app.web_search_url_var = Mock()
        app.web_search_results_var = Mock()
        app.web_search_timeout_var = Mock()
        app.web_search_test_status_var = Mock()

        app._save_web_search_settings()

        self.assertEqual(app.web_search_settings, saved)
        app._save_settings.assert_called_once_with()
        app._schedule_preview.assert_called_once_with()
        app.web_search_test_status_var.set.assert_called_once_with("Settings saved")

    def test_close_discards_unsaved_dialog_values(self) -> None:
        app = LauncherApp.__new__(LauncherApp)
        window = Mock()
        app.web_search_window = window
        app._save_settings = Mock()

        app._close_web_search_settings()

        window.destroy.assert_called_once_with()
        app._save_settings.assert_not_called()
        self.assertIsNone(app.web_search_window)


class SearXNGProbeTests(unittest.TestCase):
    def test_probe_calls_json_search_api(self) -> None:
        response = Mock()
        response.status = 200
        response.read.return_value = b'{"results": []}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch("internal.web_search_settings.request.urlopen", return_value=response) as urlopen:
            result = test_searxng_connection(WebSearchSettings())
        self.assertTrue(result.ok)
        called_url = urlopen.call_args.args[0].full_url
        self.assertIn("/search?", called_url)
        self.assertIn("format=json", called_url)

    def test_probe_rejects_non_json_response(self) -> None:
        response = Mock()
        response.status = 200
        response.read.return_value = b"<html>disabled</html>"
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch("internal.web_search_settings.request.urlopen", return_value=response):
            result = test_searxng_connection(WebSearchSettings())
        self.assertFalse(result.ok)
        self.assertIn("valid JSON", result.message)

    def test_probe_distinguishes_http_error_and_timeout(self) -> None:
        http_failure = url_error.HTTPError(
            "http://127.0.0.1/search", 403, "Forbidden", None, None
        )
        with patch("internal.web_search_settings.request.urlopen", side_effect=http_failure):
            result = test_searxng_connection(WebSearchSettings())
        self.assertFalse(result.ok)
        self.assertIn("JSON Search API", result.message)

        with patch(
            "internal.web_search_settings.request.urlopen",
            side_effect=url_error.URLError(TimeoutError()),
        ):
            result = test_searxng_connection(WebSearchSettings())
        self.assertFalse(result.ok)
        self.assertIn("timed out", result.message)


if __name__ == "__main__":
    unittest.main()
