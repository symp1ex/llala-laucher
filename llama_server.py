"""llama-server capability detection and centralized command generation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
from typing import Any, Mapping, Sequence

from parameter_specs import PARAMETER_SPECS, ParameterSpec


class CommandValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DetectionResult:
    help_text: str
    supported_keys: frozenset[str] | None
    error: str | None = None


def _switch_present(help_text: str, switch: str) -> bool:
    pattern = rf"(?<![\w-]){re.escape(switch)}(?![\w-])"
    return re.search(pattern, help_text) is not None


def detect_supported_parameters(
    server_path: Path,
    specs: Sequence[ParameterSpec] = PARAMETER_SPECS,
    timeout: float = 10.0,
) -> DetectionResult:
    if not server_path.is_file():
        return DetectionResult("", None, f"Server not found: {server_path}")
    try:
        completed = subprocess.run(
            [str(server_path), "--help"],
            cwd=str(server_path.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=timeout,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return DetectionResult("", None, str(exc))

    help_text = completed.stdout or ""
    if not help_text.strip():
        return DetectionResult("", None, "llama-server --help returned no text")
    supported = frozenset(
        spec.key for spec in specs if _switch_present(help_text, spec.support_cli)
    )
    return DetectionResult(help_text, supported)


def default_parameter_state(
    specs: Sequence[ParameterSpec] = PARAMETER_SPECS,
    *,
    safe_profile: bool = True,
) -> dict[str, dict[str, Any]]:
    return {
        spec.key: {
            "enabled": bool(spec.default_enabled) if safe_profile else False,
            "value": spec.default,
        }
        for spec in specs
    }


def _validated_value(spec: ParameterSpec, value: Any) -> str:
    if spec.value_type == "int":
        try:
            parsed: int | float = int(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise CommandValidationError(f"{spec.label}: enter a whole number") from exc
    elif spec.value_type == "float":
        try:
            parsed = float(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise CommandValidationError(f"{spec.label}: enter a number") from exc
    elif spec.value_type == "int_or_choice":
        text = str(value).strip().casefold()
        if text in {choice.casefold() for choice in spec.choices}:
            return text
        try:
            parsed = int(text)
        except ValueError as exc:
            choices = ", ".join(spec.choices)
            raise CommandValidationError(
                f"{spec.label}: enter a whole number or one of: {choices}"
            ) from exc
    else:
        text = str(value).strip()
        if not text:
            raise CommandValidationError(f"{spec.label}: value cannot be empty")
        if spec.value_type == "choice" and text not in spec.choices:
            raise CommandValidationError(
                f"{spec.label}: choose one of: {', '.join(spec.choices)}"
            )
        return text

    if spec.min_value is not None and parsed < spec.min_value:
        raise CommandValidationError(f"{spec.label}: minimum is {spec.min_value}")
    if spec.max_value is not None and parsed > spec.max_value:
        raise CommandValidationError(f"{spec.label}: maximum is {spec.max_value}")
    return str(parsed)


def build_command(
    server_path: Path,
    model_path: Path,
    parameter_state: Mapping[str, Mapping[str, Any]],
    specs: Sequence[ParameterSpec] = PARAMETER_SPECS,
    supported_keys: frozenset[str] | set[str] | None = None,
) -> list[str]:
    """Convert a model and structured state into the sole authoritative argv."""
    if not server_path.is_file():
        raise CommandValidationError(f"llama-server.exe not found: {server_path}")
    if not model_path.is_file():
        raise CommandValidationError(f"Model not found: {model_path}")

    command = [str(server_path), "-m", str(model_path)]
    for spec in specs:
        state = parameter_state.get(spec.key, {})
        if not bool(state.get("enabled", False)):
            continue
        if supported_keys is not None and spec.key not in supported_keys:
            continue
        if spec.value_type == "bool":
            command.append(spec.cli)
            continue
        command.extend((spec.cli, _validated_value(spec, state.get("value", spec.default))))
    return command


def format_windows_command(command: Sequence[str]) -> str:
    """Format argv for a readable Windows preview; never used for execution."""
    if not command:
        return ""
    quoted = [subprocess.list2cmdline([part]) for part in command]
    return " ^\n  ".join(quoted)
