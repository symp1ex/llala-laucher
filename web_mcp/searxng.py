"""Small, bounded client for the official SearXNG JSON Search API."""

from __future__ import annotations

import json
import socket
from typing import Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from web_search_settings import normalized_searxng_url


USER_AGENT = "llala-launcher-web-mcp/1.0"
MAX_SEARCH_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_SEARCH_OUTPUT_CHARS = 32_000


class SearxNGError(RuntimeError):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def _limited_text(value: object, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def normalize_results(payload: object, max_results: int) -> list[dict[str, object]]:
    if not isinstance(payload, Mapping):
        raise SearxNGError("invalid_json", "SearXNG returned a JSON value that is not an object")
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        raise SearxNGError("invalid_json", "SearXNG JSON response does not contain a results list")

    normalized: list[dict[str, object]] = []
    used = 0
    for raw in raw_results:
        if len(normalized) >= max_results or not isinstance(raw, Mapping):
            break
        title = _limited_text(raw.get("title"), 500)
        url = _limited_text(raw.get("url"), 2_048)
        if not title or not url:
            continue
        result: dict[str, object] = {
            "title": title,
            "url": url,
            "snippet": _limited_text(raw.get("content"), 3_000),
        }
        engines = raw.get("engines")
        if isinstance(engines, list):
            result["engines"] = [_limited_text(item, 100) for item in engines[:20] if str(item).strip()]
        for source, target in (
            ("score", "score"),
            ("publishedDate", "publishedDate"),
            ("category", "category"),
            ("author", "author"),
        ):
            value = raw.get(source)
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                result[target] = _limited_text(value, 500) if isinstance(value, str) else value
        serialized_size = len(json.dumps(result, ensure_ascii=False))
        if normalized and used + serialized_size > MAX_SEARCH_OUTPUT_CHARS:
            break
        normalized.append(result)
        used += serialized_size
    return normalized


class SearxNGClient:
    def __init__(self, base_url: str, timeout: float = 15.0) -> None:
        self.base_url = normalized_searxng_url(base_url)
        self.timeout = float(timeout)
        if not 1 <= self.timeout <= 120:
            raise ValueError("timeout must be between 1 and 120 seconds")

    def _request_payload(self, params: Mapping[str, str | int]) -> object:
        url = f"{self.base_url}/search?{urlencode(params)}"
        request = Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                content_type = response.headers.get_content_type()
                body = response.read(MAX_SEARCH_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            raise SearxNGError("http_error", f"SearXNG returned HTTP {exc.code}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise SearxNGError("timeout", f"SearXNG request timed out after {self.timeout:g} seconds") from exc
        except (URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise SearxNGError("connection_error", f"Could not connect to SearXNG: {reason}") from exc
        if len(body) > MAX_SEARCH_RESPONSE_BYTES:
            raise SearxNGError("response_too_large", "SearXNG response exceeds the 2 MiB limit")
        if content_type not in {"application/json", "text/json"}:
            raise SearxNGError("invalid_content_type", f"SearXNG returned {content_type}, expected JSON")
        try:
            return json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SearxNGError("invalid_json", "SearXNG returned invalid JSON") from exc

    def search(
        self,
        query: str,
        *,
        max_results: int = 8,
        language: str | None = None,
        page: int = 1,
        time_range: str | None = None,
        category: str | None = None,
    ) -> list[dict[str, object]]:
        query = str(query).strip()
        if not query:
            raise SearxNGError("validation_error", "query must be a non-empty string")
        if not 1 <= max_results <= 20:
            raise SearxNGError("validation_error", "max_results must be between 1 and 20")
        if not 1 <= page <= 50:
            raise SearxNGError("validation_error", "page must be between 1 and 50")
        if time_range not in {None, "day", "month", "year"}:
            raise SearxNGError("validation_error", "time_range must be day, month, or year")
        if category not in {None, "general", "news"}:
            raise SearxNGError("validation_error", "category must be general or news")
        params: dict[str, str | int] = {"q": query, "format": "json", "pageno": page}
        if language:
            params["language"] = str(language).strip()
        if time_range:
            params["time_range"] = time_range
        if category:
            params["categories"] = category
        return normalize_results(self._request_payload(params), max_results)

    def test_connection(self) -> None:
        payload = self._request_payload({"q": "llala launcher connection test", "format": "json", "pageno": 1})
        normalize_results(payload, 1)
