from __future__ import annotations

import json
from pathlib import Path, PureWindowsPath
import queue
import sys
import tempfile
import time
import unittest

from app_paths import resolve_app_paths
from llama_server import build_command, default_parameter_state, detect_supported_parameters
from model_scanner import ModelInfo, ModelScanner, model_id_for_relative
from parameter_specs import PARAMETER_SPECS
from preset_manager import PresetManager
from server_process import LlamaServerProcess


class ModelScannerTests(unittest.TestCase):
    def test_empty_or_missing_model_directory_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(ModelScanner(root / "missing").scan(), [])
            (root / "empty").mkdir()
            self.assertEqual(ModelScanner(root / "empty").scan(), [])

    def test_recursively_finds_every_gguf(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "A").mkdir()
            (root / "A" / "one.gguf").touch()
            (root / "A" / "two.GGUF").touch()
            (root / "ignored.txt").touch()
            models = ModelScanner(root).scan()
            self.assertEqual([model.display_name for model in models], ["A/one.gguf", "A/two.GGUF"])
            self.assertEqual(len({model.model_id for model in models}), 2)

    def test_model_id_is_safe_stable_and_collision_resistant(self) -> None:
        first = model_id_for_relative(PureWindowsPath('Folder:One/model?.gguf'))
        again = model_id_for_relative(PureWindowsPath('Folder:One/model?.gguf'))
        other = model_id_for_relative(PureWindowsPath('Folder_One/model_.gguf'))
        self.assertEqual(first, again)
        self.assertNotEqual(first, other)
        self.assertFalse(any(character in first for character in '<>:"/\\|?*'))


class PresetTests(unittest.TestCase):
    def test_round_trip_and_unknown_parameters_are_not_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model_path = root / "models" / "model.gguf"
            model_path.parent.mkdir()
            model_path.touch()
            model = ModelInfo(model_path, Path("model.gguf"), "model.gguf", "model--abc")
            manager = PresetManager(root / "preset")
            saved = manager.save(
                model,
                "fast:gpu",
                {
                    "port": {"enabled": True, "value": 8080},
                    "future_option": {"enabled": True, "value": "accepted"},
                },
            )
            self.assertEqual(saved.name, "fast_gpu.json")
            loaded = manager.load(saved)
            self.assertEqual(loaded["parameters"]["port"]["value"], 8080)
            self.assertIn("future_option", loaded["parameters"])


class CommandTests(unittest.TestCase):
    def _files(self, root: Path) -> tuple[Path, Path]:
        server = root / "llama-server.exe"
        model = root / "models" / "model.gguf"
        model.parent.mkdir()
        server.touch()
        model.touch()
        return server, model

    def test_disabled_and_unsupported_parameters_are_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server, model = self._files(root)
            state = default_parameter_state()
            state["gpu_layers"] = {"enabled": False, "value": 99}
            state["alias"] = {"enabled": True, "value": "hidden-as-unsupported"}
            supported = {spec.key for spec in PARAMETER_SPECS} - {"alias"}
            command = build_command(server, model, state, supported_keys=supported)
            self.assertNotIn("-ngl", command)
            self.assertNotIn("--alias", command)
            self.assertEqual(command[:3], [str(server), "-m", str(model)])

    def test_original_bat_preset_builds_equivalent_argv(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        preset_path = next((repository / "llama" / "preset").rglob("original-bat.json"))
        with preset_path.open("r", encoding="utf-8") as file:
            preset = json.load(file)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server, model = self._files(root)
            command = build_command(server, model, preset["parameters"])
            self.assertEqual(
                command,
                [
                    str(server),
                    "-m",
                    str(model),
                    "--alias",
                    "qwen3.6-35b-a3b",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8080",
                    "-c",
                    "262144",
                    "-np",
                    "1",
                    "--flash-attn",
                    "on",
                    "-ctk",
                    "q8_0",
                    "-ctv",
                    "q8_0",
                    "--jinja",
                ],
            )

    def test_missing_server_detection_is_non_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = detect_supported_parameters(Path(temporary) / "missing.exe")
            self.assertIsNone(result.supported_keys)
            self.assertTrue(result.error)


class PathTests(unittest.TestCase):
    def test_local_llama_directory_wins_then_fallback_is_used(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fallback = root / "external"
            fallback.mkdir()
            (fallback / "llama-server.exe").touch()
            paths = resolve_app_paths(root, fallback)
            self.assertEqual(paths.llama_root, fallback.resolve())
            self.assertTrue(paths.using_development_fallback)

            local = root / "llama"
            local.mkdir()
            (local / "llama-server.exe").touch()
            paths = resolve_app_paths(root, fallback)
            self.assertEqual(paths.llama_root, local.resolve())
            self.assertFalse(paths.using_development_fallback)


class ServerProcessTests(unittest.TestCase):
    def test_output_capture_and_non_blocking_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            process = LlamaServerProcess()
            process.start(
                [
                    sys.executable,
                    "-u",
                    "-c",
                    "import time; print('child-ready', flush=True); time.sleep(30)",
                ],
                Path(temporary),
            )
            deadline = time.monotonic() + 5
            saw_output = False
            while time.monotonic() < deadline and not saw_output:
                try:
                    kind, value = process.events.get(timeout=0.2)
                except queue.Empty:
                    continue
                saw_output = kind == "log" and value == "child-ready"
            self.assertTrue(saw_output)

            process.stop_async(graceful_timeout=0.2, terminate_timeout=0.5)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and process.is_running():
                time.sleep(0.05)
            self.assertFalse(process.is_running())


if __name__ == "__main__":
    unittest.main()
