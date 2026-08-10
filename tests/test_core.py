from __future__ import annotations

import json
from pathlib import Path, PureWindowsPath
import queue
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from app import LauncherApp
from app_paths import resolve_app_paths
from llama_server import (
    build_command,
    build_server_url,
    default_parameter_state,
    detect_supported_parameters,
)
from model_scanner import ModelInfo, ModelScanner, model_id_for_relative
from parameter_specs import PARAMETER_SPECS, SPEC_BY_KEY
from preset_manager import PresetManager
from server_process import LlamaServerProcess


class ParameterSpecTests(unittest.TestCase):
    def test_sampling_parameter_specs(self) -> None:
        expected = {
            "temperature": ("--temp", "float", 0.8, 0.0, 10.0),
            "top_p": ("--top-p", "float", 0.95, 0.0, 1.0),
            "top_k": ("--top-k", "int", 40, 0, 1_000_000),
            "min_p": ("--min-p", "float", 0.05, 0.0, 1.0),
            "repeat_penalty": ("--repeat-penalty", "float", 1.0, 0.0, 10.0),
        }

        for key, (cli, value_type, default, minimum, maximum) in expected.items():
            with self.subTest(key=key):
                spec = SPEC_BY_KEY[key]
                self.assertEqual(spec.cli, cli)
                self.assertEqual(spec.support_cli, cli)
                self.assertEqual(spec.category, "Sampling")
                self.assertEqual(spec.value_type, value_type)
                self.assertEqual(spec.default, default)
                self.assertFalse(spec.default_enabled)
                self.assertEqual(spec.min_value, minimum)
                self.assertEqual(spec.max_value, maximum)

    def test_required_research_controls_are_present_without_duplicates(self) -> None:
        required = {
            "ctx_size",
            "parallel",
            "flash_attn",
            "cache_type_k",
            "cache_type_v",
            "gpu_layers",
            "fit",
            "cpu_moe",
            "n_cpu_moe",
            "override_tensor",
            "threads",
            "threads_batch",
            "batch_size",
            "ubatch_size",
            "device",
            "main_gpu",
            "split_mode",
            "tensor_split",
            "load_mode",
            "no_kv_offload",
            "swa_full",
            "temperature",
            "top_p",
            "top_k",
            "min_p",
            "repeat_penalty",
        }
        keys = [spec.key for spec in PARAMETER_SPECS]

        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(required - set(keys), set())


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

    def test_qwen3_coder_next_baseline_loads_without_unknown_parameters(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        relative_path = Path("Qwen3-Coder-Next-MXFP4_MOE.gguf")
        model = ModelInfo(
            repository / "llama" / "models" / relative_path,
            relative_path,
            relative_path.as_posix(),
            model_id_for_relative(relative_path),
        )
        manager = PresetManager(repository / "llama" / "preset")
        preset_path = manager.path_for_name(model, "qwen3-coder-next-mxfp4-baseline")

        app = LauncherApp.__new__(LauncherApp)
        app.preset_manager = manager
        state, warnings, document = app._preset_parameter_state(preset_path)

        self.assertEqual(warnings, [])
        self.assertEqual(set(document["parameters"]) - set(SPEC_BY_KEY), set())
        self.assertFalse(state["min_p"]["enabled"])
        self.assertFalse(state["repeat_penalty"]["enabled"])


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

    def test_sampling_parameters_build_expected_argv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server, model = self._files(root)
            state = default_parameter_state(safe_profile=False)
            state["temperature"] = {"enabled": True, "value": 1.0}
            state["top_p"] = {"enabled": True, "value": 0.95}
            state["top_k"] = {"enabled": True, "value": 40}
            state["min_p"] = {"enabled": False, "value": 0.05}
            state["repeat_penalty"] = {"enabled": False, "value": 1.0}

            command = build_command(server, model, state)

            self.assertEqual(
                command,
                [
                    str(server),
                    "-m",
                    str(model),
                    "--temp",
                    "1.0",
                    "--top-p",
                    "0.95",
                    "--top-k",
                    "40",
                ],
            )
            self.assertNotIn("--min-p", command)
            self.assertNotIn("--repeat-penalty", command)

    def test_qwen3_coder_next_baseline_builds_equivalent_argv(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        relative_path = Path("Qwen3-Coder-Next-MXFP4_MOE.gguf")
        model_info = ModelInfo(
            repository / "llama" / "models" / relative_path,
            relative_path,
            relative_path.as_posix(),
            model_id_for_relative(relative_path),
        )
        manager = PresetManager(repository / "llama" / "preset")
        preset_path = manager.path_for_name(model_info, "qwen3-coder-next-mxfp4-baseline")
        preset = manager.load(preset_path)

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
                    "qwen3-coder-next",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8080",
                    "-c",
                    "32768",
                    "-np",
                    "1",
                    "--flash-attn",
                    "on",
                    "-ctk",
                    "q8_0",
                    "-ctv",
                    "q8_0",
                    "--jinja",
                    "--fit",
                    "on",
                    "--temp",
                    "1.0",
                    "--top-p",
                    "0.95",
                    "--top-k",
                    "40",
                ],
            )

    def test_sampling_capability_detection_uses_each_support_switch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            server = Path(temporary) / "llama-server.exe"
            server.touch()
            help_text = "--temp --top-p --top-k --repeat-penalty"
            with patch("llama_server.subprocess.run") as run:
                run.return_value.stdout = help_text
                result = detect_supported_parameters(server)

            self.assertIsNone(result.error)
            self.assertIn("temperature", result.supported_keys or ())
            self.assertIn("top_p", result.supported_keys or ())
            self.assertIn("top_k", result.supported_keys or ())
            self.assertIn("repeat_penalty", result.supported_keys or ())
            self.assertNotIn("min_p", result.supported_keys or ())

    def test_mcp_capability_detection_is_independent_of_parameter_specs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            server = Path(temporary) / "llama-server.exe"
            server.touch()
            with patch("llama_server.subprocess.run") as run:
                run.return_value.stdout = "--host --mcp-servers-json"
                result = detect_supported_parameters(server)
            self.assertTrue(result.mcp_supported)

    def test_missing_server_detection_is_non_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = detect_supported_parameters(Path(temporary) / "missing.exe")
            self.assertIsNone(result.supported_keys)
            self.assertTrue(result.error)

    def test_server_url_uses_effective_address(self) -> None:
        state = default_parameter_state()
        state["host"] = {"enabled": True, "value": "0.0.0.0"}
        state["port"] = {"enabled": True, "value": 9090}
        self.assertEqual(build_server_url(state), "http://127.0.0.1:9090/")

        state["host"] = {"enabled": True, "value": "::1"}
        self.assertEqual(build_server_url(state), "http://[::1]:9090/")


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
