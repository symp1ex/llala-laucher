"""llama-server capability detection and centralized command generation."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Mapping, Sequence

from parameter_specs import PARAMETER_SPECS, ParameterSpec
from web_search_settings import WebSearchSettings, normalized_searxng_url


class CommandValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DetectionResult:
    help_text: str
    supported_keys: frozenset[str] | None
    error: str | None = None
    mcp_supported: bool | None = None


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
    return DetectionResult(
        help_text,
        supported,
        mcp_supported=_switch_present(help_text, "--mcp-servers-json"),
    )


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
    web_search: WebSearchSettings | None = None,
    mcp_command: Sequence[str] | None = None,
    mcp_supported: bool | None = None,
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

    if web_search is not None and web_search.enabled:
        if mcp_supported is not True:
            if mcp_supported is False:
                detail = "this llama-server build does not support --mcp-servers-json"
            else:
                detail = "support for --mcp-servers-json could not be confirmed"
            raise CommandValidationError(f"Web search cannot be enabled: {detail}")
        if not mcp_command:
            raise CommandValidationError("Web search cannot be enabled: MCP launch command is missing")
        executable = Path(mcp_command[0])
        if not executable.is_file():
            raise CommandValidationError(f"Web MCP executable not found: {executable}")
        if len(mcp_command) > 1 and str(mcp_command[1]).casefold().endswith(".py"):
            entrypoint = Path(mcp_command[1])
            if not entrypoint.is_file():
                raise CommandValidationError(f"Web MCP source entrypoint not found: {entrypoint}")
        try:
            searxng_url = normalized_searxng_url(web_search.searxng_url)
        except ValueError as exc:
            raise CommandValidationError(str(exc)) from exc
        if not 1 <= web_search.max_results <= 20:
            raise CommandValidationError("Web search results must be between 1 and 20")
        if not 1 <= web_search.timeout <= 120:
            raise CommandValidationError("Web search timeout must be between 1 and 120 seconds")
        server_args = list(mcp_command[1:]) + [
            "--searxng-url",
            searxng_url,
            "--max-results",
            str(web_search.max_results),
            "--timeout",
            f"{web_search.timeout:g}",
        ]
        config = {
            "mcpServers": {
                "web": {
                    "command": str(executable),
                    "args": server_args,
                }
            }
        }
        command.extend(
            (
                "--mcp-servers-json",
                json.dumps(config, ensure_ascii=False, separators=(",", ":")),
            )
        )
    return command


def format_windows_command(command: Sequence[str]) -> str:
    """Format argv for a readable Windows preview; never used for execution."""
    if not command:
        return ""
    quoted = [subprocess.list2cmdline([part]) for part in command]
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
