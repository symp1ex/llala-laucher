"""Manual end-to-end smoke: launcher argv -> llama-server -> Go stdio MCP."""

from __future__ import annotations

from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from urllib import error, request

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY))

from internal.llama_server import build_command, default_parameter_state  # noqa: E402
from internal.web_search_settings import WebSearchSettings  # noqa: E402


class MockSearXNG(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b'{"results":[{"title":"llama MCP smoke","url":"https://example.com","content":"integrated","engines":["mock"]}]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def json_request(url: str, payload: dict[str, object] | None = None) -> object:
    data = None if payload is None else json.dumps(payload).encode()
    headers = {} if data is None else {"Content-Type": "application/json"}
    with request.urlopen(request.Request(url, data=data, headers=headers), timeout=5) as response:
        return json.load(response)


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: smoke_llama_mcp.py <llama-server.exe> <model.gguf>")
    repository = REPOSITORY
    server_exe = Path(sys.argv[1]).resolve()
    model = Path(sys.argv[2]).resolve()
    mcp_exe = repository / "mcp" / "web-mcp.exe"
    searxng = ThreadingHTTPServer(("127.0.0.1", 0), MockSearXNG)
    threading.Thread(target=searxng.serve_forever, daemon=True).start()
    port = free_port()
    state = default_parameter_state()
    state["port"] = {"enabled": True, "value": port}
    state["ctx_size"] = {"enabled": True, "value": 512}
    state["parallel"] = {"enabled": True, "value": 1}
    state["gpu_layers"] = {"enabled": False, "value": 0}
    state["jinja"] = {"enabled": True, "value": True}
    command = build_command(
        server_exe,
        model,
        state,
        web_search=WebSearchSettings(
            True,
            f"http://127.0.0.1:{searxng.server_port}",
            8,
            10.0,
        ),
        web_mcp_path=mcp_exe,
        supports_mcp_servers_json=True,
    )
    command.append("--no-warmup")
    log_tail: deque[str] = deque(maxlen=80)
    process = subprocess.Popen(
        command,
        cwd=server_exe.parent,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        shell=False,
    )

    def read_log() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            log_tail.append(line.rstrip())

    threading.Thread(target=read_log, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(
                    f"llama-server exited {process.returncode}\n" + "\n".join(log_tail)
                )
            try:
                json_request(base + "/health")
                break
            except (OSError, error.URLError, json.JSONDecodeError):
                time.sleep(0.5)
        else:
            raise AssertionError("llama-server health timeout\n" + "\n".join(log_tail))
        tools = json_request(base + "/tools")
        names = {item["tool"] for item in tools}
        search_tool = next((name for name in names if name.endswith("_web_search")), None)
        fetch_tool = next((name for name in names if name.endswith("_web_fetch")), None)
        assert search_tool and fetch_tool, tools
        searched = json_request(
            base + "/tools",
            {"tool": search_tool, "params": {"query": "integration smoke"}},
        )
        assert "llama MCP smoke" in json.dumps(searched), searched
        fetched = json_request(
            base + "/tools",
            {"tool": fetch_tool, "params": {"url": "http://127.0.0.1/private"}},
        )
        assert "SSRF" in json.dumps(fetched), fetched
        print("PASS: launcher argv -> llama-server -> web-mcp -> mock SearXNG/search/fetch-error")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        searxng.shutdown()
        searxng.server_close()


if __name__ == "__main__":
    main()
