"""llama-server capability detection and centralized command generation."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Mapping, Sequence

from .cli_inventory import option_switches, parse_help_options
from .parameter_specs import PARAMETER_SPECS, ParameterSpec
from .web_search_settings import WebSearchSettings, WebSearchSettingsError, validate_web_search_settings


class CommandValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DetectionResult:
    help_text: str
    supported_keys: frozenset[str] | None
    error: str | None = None
    supports_mcp_servers_json: bool = False
    supported_switches: frozenset[str] | None = None


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
    parsed_switches = option_switches(parse_help_options(help_text))
    supported = frozenset(
        spec.key for spec in specs if parsed_switches.intersection(spec.all_switches)
    )
    return DetectionResult(
        help_text,
        supported,
        supports_mcp_servers_json=_switch_present(help_text, "--mcp-servers-json"),
        supported_switches=parsed_switches,
    )


def default_parameter_state(
    specs: Sequence[ParameterSpec] = PARAMETER_SPECS,
    *,
    safe_profile: bool = True,
) -> dict[str, dict[str, Any]]:
    return {
        spec.key: {
            "enabled": bool(spec.default_enabled) if safe_profile else False,
            "value": deepcopy(spec.default),
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


def _json_list(spec: ParameterSpec, value: Any) -> list[Any]:
    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise CommandValidationError(f"{spec.label}: enter a JSON array") from exc
    if not isinstance(parsed, list):
        raise CommandValidationError(f"{spec.label}: enter a JSON array")
    if not parsed:
        raise CommandValidationError(f"{spec.label}: the array cannot be empty")
    return parsed


def _validated_argv_values(spec: ParameterSpec, value: Any) -> list[list[str]]:
    """Return value groups; each group follows one occurrence of the switch."""
    if spec.value_type == "string_list":
        values = _json_list(spec, value)
        result: list[list[str]] = []
        for item in values:
            if isinstance(item, (dict, list)) or not str(item):
                raise CommandValidationError(f"{spec.label}: array items must be non-empty scalars")
            result.append([str(item)])
        return result
    if spec.value_type == "int_list":
        values = _json_list(spec, value)
        if len(values) != spec.arity:
            raise CommandValidationError(
                f"{spec.label}: expected {spec.arity} array items, got {len(values)}"
            )
        converted: list[str] = []
        for item in values:
            try:
                converted.append(str(int(str(item).strip())))
            except (TypeError, ValueError) as exc:
                raise CommandValidationError(
                    f"{spec.label}: every array item must be a whole number"
                ) from exc
        return [converted]
    return [[_validated_value(spec, value)]]


def _toggle_is_positive(spec: ParameterSpec, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().casefold()
    if normalized in {"on", "true", "1", "yes"}:
        return True
    if normalized in {"off", "false", "0", "no"}:
        return False
    raise CommandValidationError(f"{spec.label}: choose on or off")


def _available_switch(
    candidates: Sequence[str], supported_switches: frozenset[str] | set[str] | None
) -> str | None:
    if not candidates:
        return None
    if supported_switches is None:
        return candidates[0]
    return next((switch for switch in candidates if switch in supported_switches), None)


def build_command(
    server_path: Path,
    model_path: Path,
    parameter_state: Mapping[str, Mapping[str, Any]],
    specs: Sequence[ParameterSpec] = PARAMETER_SPECS,
    supported_keys: frozenset[str] | set[str] | None = None,
    supported_switches: frozenset[str] | set[str] | None = None,
    *,
    web_search: WebSearchSettings | None = None,
    web_mcp_path: Path | None = None,
    supports_mcp_servers_json: bool = False,
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
        if spec.value_type == "toggle":
            positive = _toggle_is_positive(spec, state.get("value", spec.default))
            candidates = spec.positive_switches if positive else spec.negative_switches
            switch = _available_switch(candidates, supported_switches)
            if switch is not None:
                command.append(switch)
            continue
        switch = _available_switch(spec.positive_switches, supported_switches)
        if switch is None:
            continue
        if spec.value_type == "bool":
            command.append(switch)
            continue
        groups = _validated_argv_values(spec, state.get("value", spec.default))
        for group in groups:
            command.append(switch)
            command.extend(group)
    if web_search is not None and web_search.enabled:
        if not supports_mcp_servers_json:
            raise CommandValidationError(
                "Web search is enabled, but this llama-server does not support --mcp-servers-json"
            )
        if web_mcp_path is None or not web_mcp_path.is_file():
            expected = web_mcp_path or Path("mcp") / "web-mcp.exe"
            raise CommandValidationError(f"Web search MCP executable not found: {expected}")
        try:
            validated = validate_web_search_settings(
                enabled=True,
                url=web_search.url,
                max_results=web_search.max_results,
                timeout=web_search.timeout,
            )
        except WebSearchSettingsError as exc:
            raise CommandValidationError(f"Web search: {exc}") from exc
        config = {
            "mcpServers": {
                "web-search": {
                    "command": str(web_mcp_path.resolve()),
                    "args": [
                        "--searxng-url",
                        validated.url,
                        "--max-results",
                        str(validated.max_results),
                        "--timeout",
                        str(validated.timeout),
                    ],
                    "timeout_ms": int(validated.timeout * 1000) + 5_000,
                }
            }
        }
        command.extend(
            ("--mcp-servers-json", json.dumps(config, ensure_ascii=False, separators=(",", ":")))
        )
    return command


def validate_web_mcp_executable(path: Path, timeout: float = 5.0) -> None:
    """Fail before starting llama-server when the configured MCP EXE cannot run."""
    if not path.is_file():
        raise CommandValidationError(f"Web search MCP executable not found: {path}")
    creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        completed = subprocess.run(
            [str(path), "--check"],
            cwd=str(path.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
            shell=False,
            creationflags=creation_flags,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CommandValidationError(f"Could not start Web search MCP executable: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        suffix = f": {detail}" if detail else ""
        raise CommandValidationError(
            f"Web search MCP executable self-check failed (exit {completed.returncode}){suffix}"
        )


def format_windows_command(command: Sequence[str]) -> str:
    """Format argv for a readable Windows preview; never used for execution."""
    if not command:
        return ""
    secret_switches = {
        switch
        for spec in PARAMETER_SPECS
        if spec.value_type == "secret"
        for switch in spec.all_switches
    } | {"--mcp-servers-json"}
    display = list(command)
    for index, part in enumerate(display[:-1]):
        if part in secret_switches:
            display[index + 1] = "<redacted>"
    quoted = [subprocess.list2cmdline([part]) for part in display]
    return " ^\n  ".join(quoted)


def build_server_url(
    parameter_state: Mapping[str, Mapping[str, Any]],
    supported_keys: frozenset[str] | set[str] | None = None,
) -> str:
    """Return the browser URL matching the effective host and port."""
    host_state = parameter_state.get("host", {})
    port_state = parameter_state.get("port", {})
    host_enabled = bool(host_state.get("enabled", False))
    port_enabled = bool(port_state.get("enabled", False))
    if supported_keys is not None:
        host_enabled = host_enabled and "host" in supported_keys
        port_enabled = port_enabled and "port" in supported_keys

    host = str(host_state.get("value", "127.0.0.1")).strip() if host_enabled else "127.0.0.1"
    port_value = port_state.get("value", 8080) if port_enabled else 8080
    try:
        port = int(str(port_value).strip())
    except (TypeError, ValueError) as exc:
        raise CommandValidationError("Port: enter a whole number") from exc
    if not 1 <= port <= 65535:
        raise CommandValidationError("Port: value must be between 1 and 65535")
    if not host:
        raise CommandValidationError("Host: value cannot be empty")
    if host.casefold().endswith(".sock"):
        raise CommandValidationError("Web UI cannot be opened for a Unix socket address")
    if host in {"0.0.0.0", "::", "[::]", "*"}:
        host = "127.0.0.1"
    elif ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}/"
