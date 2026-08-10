"""Validated launcher settings and an asynchronous-safe SearXNG API probe."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from typing import Any, Mapping
from urllib import error, parse, request


DEFAULT_SEARXNG_URL = "http://127.0.0.1:8080"
DEFAULT_MAX_RESULTS = 8
DEFAULT_TIMEOUT_SECONDS = 15.0
MAX_RESPONSE_BYTES = 1_048_576


class WebSearchSettingsError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class WebSearchSettings:
    enabled: bool = False
    url: str = DEFAULT_SEARXNG_URL
    max_results: int = DEFAULT_MAX_RESULTS
    timeout: float = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_mapping(cls, value: object) -> "WebSearchSettings":
        if not isinstance(value, Mapping):
            return cls()
        try:
            return validate_web_search_settings(
                enabled=value.get("enabled", False) is True,
                url=value.get("url", DEFAULT_SEARXNG_URL),
                max_results=value.get("max_results", DEFAULT_MAX_RESULTS),
                timeout=value.get("timeout", DEFAULT_TIMEOUT_SECONDS),
            )
        except WebSearchSettingsError:
            return cls()

    def to_mapping(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ConnectionTestResult:
    ok: bool
    message: str
    detail: str = ""


def validate_web_search_settings(
    *,
    enabled: bool,
    url: object,
    max_results: object,
    timeout: object,
) -> WebSearchSettings:
    text_url = str(url).strip().rstrip("/")
    parsed = parse.urlsplit(text_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise WebSearchSettingsError("URL must be an http:// or https:// address")
    if parsed.username is not None or parsed.password is not None:
        raise WebSearchSettingsError("URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise WebSearchSettingsError("URL must not contain a query or fragment")
    try:
        parsed_results = int(str(max_results).strip())
    except (TypeError, ValueError) as exc:
        raise WebSearchSettingsError("Results must be a whole number") from exc
    if not 1 <= parsed_results <= 20:
        raise WebSearchSettingsError("Results must be between 1 and 20")
    try:
        parsed_timeout = float(str(timeout).strip())
    except (TypeError, ValueError) as exc:
        raise WebSearchSettingsError("Timeout must be a number") from exc
    if not 1.0 <= parsed_timeout <= 120.0:
        raise WebSearchSettingsError("Timeout must be between 1 and 120 seconds")
    return WebSearchSettings(bool(enabled), text_url, parsed_results, parsed_timeout)


def test_searxng_connection(settings: WebSearchSettings) -> ConnectionTestResult:
    query = parse.urlencode({"q": "llala launcher connection test", "format": "json"})
    target = f"{settings.url}/search?{query}"
    probe = request.Request(target, headers={"User-Agent": "llala-launcher/1 SearXNG probe"})
    try:
        with request.urlopen(probe, timeout=settings.timeout) as response:
            status = getattr(response, "status", 200)
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except error.HTTPError as exc:
        exc.close()
        if exc.code in {400, 403, 406}:
            return ConnectionTestResult(
                False,
                "Error — JSON Search API may not be enabled",
                f"HTTP {exc.code}",
            )
        return ConnectionTestResult(False, f"Error — HTTP {exc.code}")
    except error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, TimeoutError):
            return ConnectionTestResult(False, "Error — connection timed out")
        return ConnectionTestResult(False, "Error — server is unavailable", str(reason))
    except TimeoutError:
        return ConnectionTestResult(False, "Error — connection timed out")
    except OSError as exc:
        return ConnectionTestResult(False, "Error — server is unavailable", str(exc))

    if status < 200 or status >= 300:
        return ConnectionTestResult(False, f"Error — HTTP {status}")
    if len(body) > MAX_RESPONSE_BYTES:
        return ConnectionTestResult(False, "Error — JSON response is too large")
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return ConnectionTestResult(False, "Error — response is not valid JSON", str(exc))
    if not isinstance(document, Mapping) or not isinstance(document.get("results"), list):
        return ConnectionTestResult(False, "Error — response is not a SearXNG Search API result")
    return ConnectionTestResult(True, "OK — JSON Search API is available")
