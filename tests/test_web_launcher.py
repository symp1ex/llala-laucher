from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from llama_server import CommandValidationError, build_command, default_parameter_state
from web_search_settings import WebSearchSettings, resolve_mcp_command, web_settings_from_json


class WebLauncherCommandTests(unittest.TestCase):
    def _files(self, root: Path) -> tuple[Path, Path, Path, Path]:
        server = root / "llama" / "llama-server.exe"
        model = root / "llama" / "models" / "quoted model.gguf"
        python = root / "Python 3.13" / "python.exe"
        entrypoint = root / "web-mcp.py"
        for path in (server, model, python, entrypoint):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        return server, model, python, entrypoint

    def test_mcp_configuration_is_cursor_compatible_json_and_windows_argv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server, model, python, entrypoint = self._files(root)
            command = build_command(
                server,
                model,
                default_parameter_state(),
                web_search=WebSearchSettings(True, "http://192.168.1.50:8080", 8, 15),
                mcp_command=[str(python), str(entrypoint)],
                mcp_supported=True,
            )
            index = command.index("--mcp-servers-json")
            config = json.loads(command[index + 1])
            definition = config["mcpServers"]["web"]
            self.assertEqual(definition["command"], str(python))
            self.assertEqual(definition["args"][:1], [str(entrypoint)])
            self.assertEqual(definition["args"][-6:], [
                "--searxng-url", "http://192.168.1.50:8080",
                "--max-results", "8", "--timeout", "15",
            ])

    def test_disabled_web_search_adds_no_mcp_argument(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server, model, python, entrypoint = self._files(root)
            command = build_command(
                server,
                model,
                default_parameter_state(),
                web_search=WebSearchSettings(),
                mcp_command=[str(python), str(entrypoint)],
                mcp_supported=False,
            )
            self.assertNotIn("--mcp-servers-json", command)

    def test_enabled_web_search_requires_confirmed_server_support(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server, model, python, entrypoint = self._files(root)
            for supported in (False, None):
                with self.subTest(supported=supported), self.assertRaises(CommandValidationError):
                    build_command(
                        server,
                        model,
                        default_parameter_state(),
                        web_search=WebSearchSettings(enabled=True),
                        mcp_command=[str(python), str(entrypoint)],
                        mcp_supported=supported,
                    )

    def test_enabled_web_search_requires_mcp_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server, model, _python, _entrypoint = self._files(root)
            with self.assertRaisesRegex(CommandValidationError, "MCP executable not found"):
                build_command(
                    server,
                    model,
                    default_parameter_state(),
                    web_search=WebSearchSettings(enabled=True),
                    mcp_command=[str(root / "mcp" / "web-mcp.exe")],
                    mcp_supported=True,
                )


class WebSettingsTests(unittest.TestCase):
    def test_settings_round_trip_and_backward_compatible_defaults(self) -> None:
        settings = WebSearchSettings(True, "http://192.168.1.50:8080", 12, 22.5)
        self.assertEqual(web_settings_from_json(settings.to_json()), settings)
        self.assertEqual(web_settings_from_json(None), WebSearchSettings())
        self.assertEqual(web_settings_from_json({"max_results": "bad"}), WebSearchSettings())

    def test_source_and_frozen_mcp_resolution(self) -> None:
        root = Path("C:/Portable/Llala")
        source = resolve_mcp_command(
            root,
            frozen=False,
            python_executable=Path("C:/Python313/python.exe"),
        )
        self.assertTrue(source[0].replace("\\", "/").endswith("Python313/python.exe"))
        self.assertTrue(source[1].replace("\\", "/").endswith("Portable/Llala/web-mcp.py"))
        frozen = resolve_mcp_command(root, frozen=True)
        self.assertTrue(frozen[0].replace("\\", "/").endswith("Portable/Llala/mcp/web-mcp.exe"))


if __name__ == "__main__":
    unittest.main()
