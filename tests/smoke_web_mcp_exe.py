"""Manual smoke test for the production Go MCP executable."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ctypes
import json
from pathlib import Path
import subprocess
import threading


class MockSearXNG(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = json.dumps(
            {
                "results": [
                    {
                        "title": "Smoke result",
                        "url": "https://example.com/article",
                        "content": "Smoke snippet",
                        "engines": ["mock"],
                    }
                ]
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def send(process: subprocess.Popen[str], request: dict[str, object]) -> dict[str, object]:
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
    process.stdin.flush()
    line = process.stdout.readline()
    if not line:
        raise AssertionError("MCP stdout closed before a response")
    response = json.loads(line)
    if not isinstance(response, dict):
        raise AssertionError(f"MCP response is not an object: {response!r}")
    return response


def visible_windows_for_pid(pid: int) -> list[int]:
    if not hasattr(ctypes, "windll"):
        return []
    windows: list[int] = []
    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    @callback_type
    def inspect(window: int, _parameter: int) -> bool:
        process_id = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(window, ctypes.byref(process_id))
        if process_id.value == pid and user32.IsWindowVisible(window):
            windows.append(window)
        return True

    user32.EnumWindows(inspect, 0)
    return windows


def main() -> None:
    repository = Path(__file__).resolve().parents[1]
    executable = repository / "mcp" / "web-mcp.exe"
    if not executable.is_file():
        raise SystemExit(f"missing production executable: {executable}")
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockSearXNG)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        [
            str(executable),
            "--searxng-url",
            f"http://127.0.0.1:{server.server_port}",
            "--max-results",
            "8",
            "--timeout",
            "5",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        creationflags=creationflags,
        shell=False,
    )
    try:
        initialized = send(
            process,
            {
                "jsonrpc": "2.0",
                "id": "initialize-smoke",
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "smoke", "version": "1"},
                },
            },
        )
        assert initialized["id"] == "initialize-smoke"
        assert visible_windows_for_pid(process.pid) == []
        assert process.stdin is not None
        process.stdin.write(
            '{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}\n'
        )
        process.stdin.flush()
        listed = send(
            process,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        names = {tool["name"] for tool in listed["result"]["tools"]}
        assert names == {"web_search", "web_fetch"}, names
        searched = send(
            process,
            {
                "jsonrpc": "2.0",
                "id": "search-smoke",
                "method": "tools/call",
                "params": {"name": "web_search", "arguments": {"query": "smoke"}},
            },
        )
        assert "Smoke result" in searched["result"]["content"][0]["text"]
        assert process.stdin is not None
        process.stdin.close()
        return_code = process.wait(timeout=5)
        assert return_code == 0, return_code
        assert process.stderr is not None
        diagnostics = process.stderr.read()
        assert diagnostics == "", diagnostics
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        server.shutdown()
        server.server_close()
    print("PASS: production web-mcp.exe stdio/search/EOF smoke; stderr clean; CREATE_NO_WINDOW used")


if __name__ == "__main__":
    main()
