"""Validated launcher settings and process resolution for the web MCP server."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import sys
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit


DEFAULT_SEARXNG_URL = "http://127.0.0.1:8080"


@dataclass(frozen=True, slots=True)
class WebSearchSettings:
    enabled: bool = False
    searxng_url: str = DEFAULT_SEARXNG_URL
    max_results: int = 8
    timeout: float = 15.0

    def to_json(self) -> dict[str, bool | str | int | float]:
        return asdict(self)


def _bounded_number(value: object, default: int | float, minimum: float, maximum: float) -> int | float:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    if not minimum <= number <= maximum:
        return default
    return int(number) if isinstance(default, int) else number


def normalized_searxng_url(value: object) -> str:
    text = str(value).strip().rstrip("/")
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("SearXNG URL must be an http:// or https:// address")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("SearXNG URL must not contain embedded credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("SearXNG URL must not contain a query or fragment")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("SearXNG URL contains an invalid port") from exc
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host + (f":{port}" if port is not None else "")
    return urlunsplit((parsed.scheme, netloc, parsed.path.rstrip("/"), "", ""))


def web_settings_from_json(value: object) -> WebSearchSettings:
    raw: Mapping[object, object] = value if isinstance(value, Mapping) else {}
    url_value = raw.get("searxng_url", DEFAULT_SEARXNG_URL)
    try:
        url = normalized_searxng_url(url_value)
    except ValueError:
        url = DEFAULT_SEARXNG_URL
    return WebSearchSettings(
        enabled=raw.get("enabled", False) is True,
        searxng_url=url,
        max_results=int(_bounded_number(raw.get("max_results"), 8, 1, 20)),
        timeout=float(_bounded_number(raw.get("timeout"), 15.0, 1.0, 120.0)),
    )


def resolve_mcp_command(
    base_dir: Path,
    *,
    frozen: bool | None = None,
    python_executable: Path | None = None,
) -> list[str]:
    """Return argv for source or frozen launch without relying on the CWD."""
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if is_frozen:
        return [str(base_dir.resolve() / "mcp" / "web-mcp.exe")]
    interpreter = python_executable or Path(sys.executable)
    return [str(interpreter.resolve()), str(base_dir.resolve() / "web-mcp.py")]
