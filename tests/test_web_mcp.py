from __future__ import annotations

from contextlib import contextmanager
from io import BytesIO
import json
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

from pypdf import PdfWriter

from web_mcp.fetcher import FetchError, fetch_url, html_to_markdown, validate_public_url
from web_mcp.protocol import MCPServer, ServerConfig, serve_stdio
from web_mcp.searxng import SearxNGClient, SearxNGError, normalize_results


class MockHandler(BaseHTTPRequestHandler):
    requests: list[tuple[str, dict[str, list[str]]]] = []

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        self.__class__.requests.append((parsed.path, parse_qs(parsed.query)))
        if parsed.path == "/search":
            query = parse_qs(parsed.query).get("q", [""])[0]
            if query == "bad-json":
                self._send(200, b"not json", "application/json")
            elif query == "http-error":
                self._send(503, b"unavailable", "text/plain")
            else:
                payload = {"results": [{
                    "title": "Result",
                    "url": f"https://example.com/{query}",
                    "content": "Snippet",
                    "engines": ["mock"],
                    "score": 1.5,
                }]}
                self._send(200, json.dumps(payload).encode(), "application/json")
        elif parsed.path == "/html":
            self._send(200, b"<html><head><title>Doc</title><script>bad()</script></head><body><nav>Menu</nav><main><h1>Heading</h1><p>Hello <a href='/next'>world</a>.</p><ul><li>Item</li></ul></main></body></html>", "text/html; charset=utf-8")
        elif parsed.path == "/plain":
            self._send(200, "plain text".encode(), "text/plain; charset=utf-8")
        elif parsed.path == "/json":
            self._send(200, b'{"hello":"world"}', "application/json")
        elif parsed.path == "/large":
            self._send(200, b"x" * 5000, "text/plain")
        elif parsed.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/plain")
            self.end_headers()
        elif parsed.path == "/pdf":
            writer = PdfWriter()
            writer.add_blank_page(width=72, height=72)
            writer.add_metadata({"/Title": "PDF title"})
            output = BytesIO()
            writer.write(output)
            self._send(200, output.getvalue(), "application/pdf")
        else:
            self._send(404, b"missing", "text/plain")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@contextmanager
def mock_server():
    MockHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class SearxNGTests(unittest.TestCase):
    def test_query_parameters_and_normalization(self) -> None:
        with mock_server() as base_url:
            results = SearxNGClient(base_url).search(
                "llama cpp", max_results=3, language="ru", page=2, time_range="month", category="news"
            )
        self.assertEqual(results[0]["engines"], ["mock"])
        _path, query = MockHandler.requests[0]
        self.assertEqual(query["q"], ["llama cpp"])
        self.assertEqual(query["format"], ["json"])
        self.assertEqual(query["pageno"], ["2"])
        self.assertEqual(query["language"], ["ru"])
        self.assertEqual(query["time_range"], ["month"])
        self.assertEqual(query["categories"], ["news"])

    def test_result_count_and_field_size_are_bounded(self) -> None:
        raw = {"results": [{"title": "t" * 1000, "url": f"https://example.com/{i}", "content": "s" * 5000} for i in range(30)]}
        results = normalize_results(raw, 20)
        self.assertLessEqual(len(results), 20)
        self.assertLessEqual(len(str(results[0]["title"])), 500)
        self.assertLessEqual(len(str(results[0]["snippet"])), 3000)

    def test_http_and_invalid_json_errors(self) -> None:
        with mock_server() as base_url:
            client = SearxNGClient(base_url)
            with self.assertRaisesRegex(SearxNGError, "HTTP 503"):
                client.search("http-error")
            with self.assertRaisesRegex(SearxNGError, "invalid JSON"):
                client.search("bad-json")

    def test_timeout_and_connection_errors(self) -> None:
        client = SearxNGClient("http://127.0.0.1:8080")
        with patch("web_mcp.searxng.urlopen", side_effect=socket.timeout):
            with self.assertRaisesRegex(SearxNGError, "timed out"):
                client.search("query")
        with patch("web_mcp.searxng.urlopen", side_effect=URLError("refused")):
            with self.assertRaisesRegex(SearxNGError, "Could not connect"):
                client.search("query")


class FetchTests(unittest.TestCase):
    def test_html_to_markdown_removes_boilerplate_and_keeps_links(self) -> None:
        title, text = html_to_markdown(
            "<html><title>T</title><body><nav>menu</nav><main><h1>H</h1><p>Read <a href='/x'>this</a></p><li>One</li></main></body></html>",
            "https://example.com/a",
        )
        self.assertEqual(title, "T")
        self.assertIn("# H", text)
        self.assertIn("[this](https://example.com/x)", text)
        self.assertIn("- One", text)
        self.assertNotIn("menu", text)

    def test_plain_json_html_and_pdf(self) -> None:
        with mock_server() as base_url, patch("web_mcp.fetcher.validate_public_url", side_effect=lambda value: value):
            plain = fetch_url(base_url + "/plain")
            structured = fetch_url(base_url + "/json")
            html = fetch_url(base_url + "/html")
            pdf = fetch_url(base_url + "/pdf")
        self.assertEqual(plain["text"], "plain text")
        self.assertIn('"hello": "world"', str(structured["text"]))
        self.assertEqual(html["title"], "Doc")
        self.assertIn("EXTERNAL/UNTRUSTED", str(html["warning"]))
        self.assertEqual(pdf["title"], "PDF title")
        self.assertIn("Page 1", str(pdf["text"]))

    def test_size_limit_and_truncated_marker(self) -> None:
        with mock_server() as base_url, patch("web_mcp.fetcher.validate_public_url", side_effect=lambda value: value):
            with self.assertRaisesRegex(FetchError, "Content-Length"):
                fetch_url(base_url + "/large", max_bytes=1024)
            result = fetch_url(base_url + "/large", max_chars=1000)
        self.assertTrue(result["truncated"])
        self.assertIn("[truncated]", str(result["text"]))

    def test_url_scheme_credentials_and_ssrf_are_rejected(self) -> None:
        with self.assertRaises(FetchError):
            validate_public_url("file:///etc/passwd")
        with self.assertRaises(FetchError):
            validate_public_url("https://user:pass@example.com/")
        with self.assertRaisesRegex(FetchError, "non-public"):
            validate_public_url("http://127.0.0.1/")
        with patch("web_mcp.fetcher.socket.getaddrinfo", return_value=[(socket.AF_INET, 0, 0, "", ("192.168.1.2", 0))]):
            with self.assertRaisesRegex(FetchError, "non-public"):
                validate_public_url("https://example.test/")

    def test_redirect_is_ssrf_checked_again(self) -> None:
        with mock_server() as base_url, patch("web_mcp.fetcher.validate_public_url", side_effect=lambda value: value) as validate:
            result = fetch_url(base_url + "/redirect")
        self.assertEqual(result["final_url"], base_url + "/plain")
        self.assertGreaterEqual(validate.call_count, 3)


class ProtocolTests(unittest.TestCase):
    def test_initialize_list_ping_and_call(self) -> None:
        server = MCPServer(ServerConfig("http://127.0.0.1:8080"))
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": "two", "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "ping"},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "web_search", "arguments": {"query": "x"}}},
        ]
        stdin = BytesIO(b"".join(json.dumps(item).encode() + b"\n" for item in messages))
        stdout = BytesIO()
        with patch.object(server.searxng, "search", return_value=[{"title": "T", "url": "https://example.com", "snippet": "S"}]):
            serve_stdio(server, stdin, stdout)
        responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual([item["id"] for item in responses], [1, "two", 3, 4])
        self.assertEqual({tool["name"] for tool in responses[1]["result"]["tools"]}, {"web_search", "web_fetch"})
        self.assertFalse(responses[3]["result"]["isError"])

    def test_stdout_contains_only_json_rpc_even_when_tool_logs_diagnostics(self) -> None:
        server = MCPServer(ServerConfig("http://127.0.0.1:8080"))
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "web_search", "arguments": {"query": "x"}}}
        stdout = BytesIO()
        with patch.object(server.searxng, "search", side_effect=RuntimeError("diagnostic")):
            serve_stdio(server, BytesIO(json.dumps(request).encode() + b"\n"), stdout)
        lines = stdout.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        response = json.loads(lines[0])
        self.assertEqual(response["id"], 1)
        self.assertTrue(response["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
