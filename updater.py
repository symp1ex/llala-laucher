"""Tk-independent backend for the external application updater."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading

from app_paths import AppPaths


LOGGER = logging.getLogger(__name__)
CHECK_TIMEOUT_SECONDS = 120.0


class UpdateState(str, Enum):
    IDLE = "idle"
    CHECKING = "checking"
    AVAILABLE = "available"
    INSTALLING = "installing"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class CheckResult:
    ok: bool
    update_available: bool = False
    message: str = ""


@dataclass(frozen=True, slots=True)
class InstallResult:
    ok: bool
    message: str = ""
    pid: int | None = None


class UpdaterPathError(OSError):
    """Raised when the updater sidecar layout is unavailable or invalid."""


def parse_check_output(stdout: str) -> bool | None:
    """Parse the updater's strict, single-value stdout protocol."""
    normalized = stdout.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    return None


def build_upgrade_args(application_executable: str | Path) -> list[str]:
    """Build production upgrade arguments using the actual executable name."""
    executable_name = Path(application_executable).name
    return _build_upgrade_args_for_command(f"{executable_name} start")


def _build_upgrade_args_for_command(restart_command: str) -> list[str]:
    return ["--upgrade", "--gui", "--cmd", restart_command]


def resolve_restart_command(base_dir: Path) -> str:
    """Return the command the updater should use to restart this runtime."""
    if getattr(sys, "frozen", False):
        return f"{Path(sys.executable).name} start"
    entry_point = base_dir.resolve() / "llala-laucher.py"
    return subprocess.list2cmdline(
        [str(Path(sys.executable).resolve()), str(entry_point), "start"]
    )


class UpdaterService:
    """Validate and invoke updater-ll.exe without depending on tkinter."""

    def __init__(
        self,
        paths: AppPaths,
        *,
        restart_command: str | None = None,
        timeout: float = CHECK_TIMEOUT_SECONDS,
    ) -> None:
        self.paths = paths
        self.restart_command = restart_command or resolve_restart_command(paths.base_dir)
        self.timeout = timeout
        self._state_lock = threading.Lock()
        self._checking = False
        self._installing = False

    def _validated_updater(self) -> tuple[Path, Path]:
        updater_dir = self.paths.updater_dir
        updater_exe = self.paths.updater_exe
        try:
            if not updater_dir.exists():
                raise UpdaterPathError(f"updater directory does not exist: {updater_dir}")
            if not updater_dir.is_dir():
                raise UpdaterPathError(f"updater path is not a directory: {updater_dir}")
            if not updater_exe.exists():
                raise UpdaterPathError(f"updater executable does not exist: {updater_exe}")
            if not updater_exe.is_file():
                raise UpdaterPathError(f"updater executable path is not a file: {updater_exe}")
        except OSError as exc:
            if isinstance(exc, UpdaterPathError):
                raise
            raise UpdaterPathError(f"could not validate updater paths: {exc}") from exc
        return updater_dir, updater_exe

    def _begin_check(self) -> bool:
        with self._state_lock:
            if self._checking:
                return False
            self._checking = True
            return True

    def _end_check(self) -> None:
        with self._state_lock:
            self._checking = False

    def check(self) -> CheckResult:
        if not self._begin_check():
            message = "update check is already running"
            LOGGER.warning("[Updater] Update check skipped because another check is already running")
            return CheckResult(False, message=message)

        try:
            LOGGER.info("[Updater] Starting update check")
            updater_dir, updater_exe = self._validated_updater()
            args = ["--check"]
            LOGGER.debug("[Updater] Updater executable: %s", updater_exe)
            LOGGER.debug("[Updater] Updater working directory: %s", updater_dir)
            LOGGER.debug("[Updater] Check arguments: %s", args)
            try:
                completed = subprocess.run(
                    [str(updater_exe), *args],
                    cwd=str(updater_dir),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=False,
                    timeout=self.timeout,
                    check=False,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
            except subprocess.TimeoutExpired:
                message = "update check timed out"
                LOGGER.error("[Updater] Update check timed out after %s seconds", self.timeout)
                return CheckResult(False, message=message)
            except (OSError, subprocess.SubprocessError) as exc:
                LOGGER.error("[Updater] Update check failed: %s", exc)
                return CheckResult(False, message=str(exc))

            LOGGER.debug("[Updater] Check stdout: %r", completed.stdout)
            if completed.stderr.strip():
                LOGGER.warning("[Updater] Check stderr: %r", completed.stderr)
            if completed.returncode != 0:
                message = f"updater check exited with code {completed.returncode}"
                LOGGER.error("[Updater] Update check failed: %s", message)
                return CheckResult(False, message=message)

            update_available = parse_check_output(completed.stdout)
            if update_available is None:
                message = "unknown updater response"
                LOGGER.warning(
                    "[Updater] Update check failed: %s: stdout=%r",
                    message,
                    completed.stdout,
                )
                return CheckResult(False, message=message)

            LOGGER.info(
                "[Updater] Update check completed: update_available=%s",
                update_available,
            )
            return CheckResult(True, update_available=update_available)
        except UpdaterPathError as exc:
            LOGGER.error("[Updater] Update check failed: %s", exc)
            return CheckResult(False, message=str(exc))
        finally:
            self._end_check()

    def _begin_install(self) -> bool:
        with self._state_lock:
            if self._installing:
                return False
            self._installing = True
            return True

    def _end_failed_install(self) -> None:
        with self._state_lock:
            self._installing = False

    def install(self) -> InstallResult:
        if not self._begin_install():
            message = "update installation is already running"
            LOGGER.warning("[Updater] Update installation skipped because it is already running")
            return InstallResult(False, message=message)

        try:
            LOGGER.info("[Updater] Starting update installation")
            updater_dir, updater_exe = self._validated_updater()
            args = _build_upgrade_args_for_command(self.restart_command)
            LOGGER.debug("[Updater] Updater executable: %s", updater_exe)
            LOGGER.debug("[Updater] Updater working directory: %s", updater_dir)
            LOGGER.debug("[Updater] Upgrade arguments: %s", args)
            creation_flags = 0
            if os.name == "nt":
                creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP
            try:
                process = subprocess.Popen(
                    [str(updater_exe), *args],
                    cwd=str(updater_dir),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                    creationflags=creation_flags,
                    close_fds=True,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                self._end_failed_install()
                LOGGER.error("[Updater] Update installation failed: %s", exc)
                return InstallResult(False, message=str(exc))

            LOGGER.info(
                "[Updater] Update installation process started: pid=%s",
                process.pid,
            )
            return InstallResult(True, pid=process.pid)
        except UpdaterPathError as exc:
            self._end_failed_install()
            LOGGER.error("[Updater] Update installation failed: %s", exc)
            return InstallResult(False, message=str(exc))
