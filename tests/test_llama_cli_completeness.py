from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from internal.cli_inventory import (
    ACTION_META_SWITCHES,
    LAUNCHER_MANAGED_SWITCHES,
    REMOVED_SWITCHES,
    option_switches,
    parse_help_options,
    uncovered_options,
)
from internal.llama_server import build_command, default_parameter_state, format_windows_command
from internal.model_scanner import ModelInfo
from internal.parameter_specs import PARAMETER_SPECS, SPEC_BY_KEY
from internal.preset_manager import PresetManager, normalize_preset_parameters


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "tests" / "fixtures" / "llama-server-b10427-help-options.txt"
SERVER = ROOT / "llama" / "llama-server.exe"


class CliCompletenessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.snapshot_options = parse_help_options(SNAPSHOT.read_text(encoding="utf-8"))
        cls.represented = {
            switch for spec in PARAMETER_SPECS for switch in spec.all_switches
        }

    def test_b10427_snapshot_has_expected_inventory_and_zero_gaps(self) -> None:
        self.assertEqual(len(self.snapshot_options), 247)
        self.assertEqual(len(option_switches(self.snapshot_options)), 404)
        self.assertEqual(len(PARAMETER_SPECS), 235)
        self.assertEqual(uncovered_options(self.snapshot_options, self.represented), ())

    def test_every_non_spec_group_has_one_explicit_small_reason(self) -> None:
        classified = {"action": 0, "managed": 0, "removed": 0}
        for option in self.snapshot_options:
            switches = set(option.switches)
            if switches & self.represented:
                continue
            reasons = [
                name
                for name, values in (
                    ("action", ACTION_META_SWITCHES),
                    ("managed", LAUNCHER_MANAGED_SWITCHES),
                    ("removed", REMOVED_SWITCHES),
                )
                if switches & values
            ]
            self.assertEqual(len(reasons), 1, option.switches)
            classified[reasons[0]] += 1
        self.assertEqual(classified, {"action": 5, "managed": 2, "removed": 5})

    def test_actual_binary_matches_snapshot_and_version_when_present(self) -> None:
        if not SERVER.is_file():
            self.skipTest("packaged llama-server.exe is not present")
        help_text = subprocess.run(
            [str(SERVER), "--help"],
            cwd=SERVER.parent,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        ).stdout
        actual = parse_help_options(help_text)
        self.assertEqual([item.switches for item in actual], [item.switches for item in self.snapshot_options])
        self.assertEqual(uncovered_options(actual, self.represented), ())
        version = subprocess.run(
            [str(SERVER), "--version"],
            cwd=SERVER.parent,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        ).stdout
        self.assertIn("build 10427, commit 650913862", version)

    def test_only_historical_safe_profile_options_are_default_enabled(self) -> None:
        enabled = {spec.key for spec in PARAMETER_SPECS if spec.default_enabled}
        self.assertEqual(enabled, {"host", "port", "ctx_size", "parallel"})


class DeclarativeArgvTests(unittest.TestCase):
    def test_secret_values_are_present_in_argv_but_redacted_from_preview(self) -> None:
        command = ["llama-server.exe", "--api-key", "private-value", "--hf-token", "token-value"]
        preview = format_windows_command(command)
        self.assertNotIn("private-value", preview)
        self.assertNotIn("token-value", preview)
        self.assertEqual(command[-1], "token-value")

    def test_all_value_shapes_are_serialized_as_real_argv_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server = root / "llama-server.exe"
            model = root / "model.gguf"
            server.touch()
            model.touch()
            state = default_parameter_state(safe_profile=False)
            state.update(
                {
                    "metrics": {"enabled": True, "value": True},
                    "port": {"enabled": True, "value": 9090},
                    "temperature": {"enabled": True, "value": 0.25},
                    "split_mode": {"enabled": True, "value": "row"},
                    "alias": {"enabled": True, "value": "demo"},
                    "logit_bias": {"enabled": True, "value": ["42+1", "7-0.5"]},
                    "ui": {"enabled": True, "value": "off"},
                    "control_vector_layer_range": {"enabled": True, "value": [2, 9]},
                }
            )
            command = build_command(server, model, state)

            self.assertIn("--metrics", command)
            self.assertEqual(command[command.index("--port") + 1], "9090")
            self.assertEqual(command[command.index("--temp") + 1], "0.25")
            self.assertEqual(command[command.index("--split-mode") + 1], "row")
            self.assertEqual(command[command.index("--alias") + 1], "demo")
            self.assertEqual(command.count("--logit-bias"), 2)
            self.assertIn("--no-ui", command)
            range_index = command.index("--control-vector-layer-range")
            self.assertEqual(command[range_index + 1 : range_index + 3], ["2", "9"])
            self.assertEqual(command.count("-m"), 1)
            self.assertNotIn("--model", command)

    def test_unsupported_enabled_option_is_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server = root / "llama-server.exe"
            model = root / "model.gguf"
            server.touch()
            model.touch()
            state = default_parameter_state(safe_profile=False)
            state["metrics"] = {"enabled": True, "value": True}
            supported = {spec.key for spec in PARAMETER_SPECS} - {"metrics"}
            self.assertNotIn("--metrics", build_command(server, model, state, supported_keys=supported))


class PresetNormalizationTests(unittest.TestCase):
    def test_partial_port_preset_does_not_gain_new_switches(self) -> None:
        normalized, _warnings = normalize_preset_parameters(
            {"port": {"enabled": True, "value": 8080}}
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server = root / "llama-server.exe"
            model = root / "model.gguf"
            server.touch()
            model.touch()
            self.assertEqual(
                build_command(server, model, normalized),
                [str(server), "-m", str(model), "--port", "8080"],
            )

    def test_partial_and_incomplete_preset_gets_safe_defaults(self) -> None:
        normalized, warnings = normalize_preset_parameters(
            {
                "port": {"enabled": True, "value": 8080},
                "host": {"value": "0.0.0.0"},
                "ctx_size": {"enabled": True},
                "metrics": {},
                "future_option": {"enabled": True, "value": "accepted"},
                "threads": "broken",
            }
        )
        self.assertTrue(normalized["port"]["enabled"])
        self.assertEqual(normalized["port"]["value"], 8080)
        self.assertFalse(normalized["host"]["enabled"])
        self.assertEqual(normalized["host"]["value"], "0.0.0.0")
        self.assertTrue(normalized["ctx_size"]["enabled"])
        self.assertEqual(normalized["ctx_size"]["value"], SPEC_BY_KEY["ctx_size"].default)
        self.assertEqual(normalized["metrics"], {"enabled": False, "value": True})
        self.assertEqual(normalized["threads"], {"enabled": False, "value": -1})
        self.assertEqual(normalized["future_option"]["value"], "accepted")
        self.assertEqual(set(SPEC_BY_KEY) - set(normalized), set())
        self.assertTrue(warnings)

    def test_save_normalizes_and_preserves_unknown_target_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model_path = root / "model.gguf"
            model = ModelInfo(model_path, Path("model.gguf"), "model.gguf", "model--id")
            manager = PresetManager(root / "preset")
            target = manager.path_for_name(model, "roundtrip")
            target.parent.mkdir(parents=True)
            target.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "parameters": {
                            "future_option": {"enabled": True, "value": "accepted"}
                        },
                    }
                ),
                encoding="utf-8",
            )
            saved = manager.save(
                model,
                "roundtrip",
                {"port": {"enabled": True, "value": 8181}},
            )
            document = manager.load(saved)
            self.assertEqual(set(SPEC_BY_KEY) - set(document["parameters"]), set())
            self.assertEqual(document["parameters"]["port"]["value"], 8181)
            self.assertEqual(document["parameters"]["future_option"]["value"], "accepted")


if __name__ == "__main__":
    unittest.main()
