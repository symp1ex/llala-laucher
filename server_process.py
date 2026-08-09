"""Non-blocking llama-server process lifecycle and output capture."""

from __future__ import annotations

import os
from pathlib import Path
import queue
import signal
import subprocess
import threading
from typing import Sequence


ProcessEvent = tuple[str, object]


class LlamaServerProcess:
    def __init__(self) -> None:
        self.events: queue.Queue[ProcessEvent] = queue.Queue()
        self._process: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()
        self._stopping = False

    @property
    def process(self) -> subprocess.Popen[bytes] | None:
        with self._lock:
            return self._process

    @property
    def pid(self) -> int | None:
        process = self.process
        return process.pid if process is not None and process.poll() is None else None

    def is_running(self) -> bool:
        process = self.process
        return process is not None and process.poll() is None

    def start(self, command: Sequence[str], cwd: Path) -> int:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                raise RuntimeError("llama-server is already running")

            creation_flags = 0
            if os.name == "nt":
                creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP
            process = subprocess.Popen(
                list(command),
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                shell=False,
                creationflags=creation_flags,
            )
            self._process = process
            self._stopping = False

        threading.Thread(target=self._read_output, args=(process,), daemon=True).start()
        threading.Thread(target=self._wait_for_exit, args=(process,), daemon=True).start()
        return process.pid

    def _read_output(self, process: subprocess.Popen[bytes]) -> None:
        stream = process.stdout
        if stream is None:
            return
        try:
            for raw_line in iter(stream.readline, b""):
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                self.events.put(("log", line))
        except (OSError, ValueError) as exc:
            if process.poll() is None:
                self.events.put(("log", f"[output reader error] {exc}"))
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _wait_for_exit(self, process: subprocess.Popen[bytes]) -> None:
        exit_code = process.wait()
        with self._lock:
            if self._process is process:
                self._process = None
                self._stopping = False
        self.events.put(("exit", exit_code))

    def stop_async(self, graceful_timeout: float = 2.0, terminate_timeout: float = 2.0) -> None:
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None or self._stopping:
                return
            self._stopping = True
        threading.Thread(
            target=self._stop_worker,
            args=(process, graceful_timeout, terminate_timeout),
            daemon=True,
        ).start()

    def _stop_worker(
        self,
        process: subprocess.Popen[bytes],
        graceful_timeout: float,
        terminate_timeout: float,
    ) -> None:
        self.events.put(("log", "Stopping llama-server..."))
        if process.poll() is not None:
            return

        if os.name == "nt":
            try:
                process.send_signal(signal.CTRL_BREAK_EVENT)
                process.wait(timeout=graceful_timeout)
                return
            except (OSError, subprocess.SubprocessError):
                pass

        try:
            process.terminate()
            process.wait(timeout=terminate_timeout)
            return
        except (OSError, subprocess.SubprocessError):
            pass

        if process.poll() is None:
            self.events.put(("log", "Graceful stop timed out; killing the process."))
            try:
                process.kill()
            except OSError as exc:
                self.events.put(("log", f"Could not kill llama-server: {exc}"))
