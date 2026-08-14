from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from internal.app import LauncherApp
from internal.app_paths import AppPaths, resolve_app_paths
from internal.updater import (
    CheckResult,
    InstallResult,
    UpdateState,
    UpdaterService,
    build_upgrade_args,
    parse_check_output,
    resolve_restart_command,
)


def make_updater_paths(root: Path) -> AppPaths:
    updater_dir = root / "updater"
    updater_dir.mkdir()
    (updater_dir / "updater-ll.exe").touch()
    return resolve_app_paths(root, development_root=None)


class UpdaterProtocolTests(unittest.TestCase):
    def test_parse_check_output_is_strict(self) -> None:
        cases = {
            "true": True,
            "TRUE": True,
            " true ": True,
            "false": False,
            " FALSE ": False,
            "": None,
            "unknown": None,
            "true\nlog": None,
        }
        for stdout, expected in cases.items():
            with self.subTest(stdout=stdout):
                self.assertIs(parse_check_output(stdout), expected)

    def test_app_paths_resolve_updater_below_base_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base_dir = Path(temporary)
            paths = resolve_app_paths(base_dir, development_root=None)

            self.assertEqual(paths.updater_dir, base_dir.resolve() / "updater")
            self.assertEqual(
                paths.updater_exe,
                base_dir.resolve() / "updater" / "updater-ll.exe",
            )

    def test_upgrade_args_use_expected_launcher_name(self) -> None:
        self.assertEqual(
            build_upgrade_args("llala-laucher.exe"),
            ["--upgrade", "--gui", "--cmd", "llala-laucher.exe start"],
        )

    def test_upgrade_args_use_actual_executable_name(self) -> None:
        self.assertEqual(
            build_upgrade_args(Path(r"C:\Program Files\App\renamed-launcher.exe")),
            ["--upgrade", "--gui", "--cmd", "renamed-launcher.exe start"],
        )

    def test_frozen_restart_command_uses_runtime_executable_name(self) -> None:
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "executable", r"C:\Program Files\App\renamed-launcher.exe"),
        ):
            command = resolve_restart_command(Path(r"C:\Program Files\App"))

        self.assertEqual(command, "renamed-launcher.exe start")


class UpdaterServiceTests(unittest.TestCase):
    def test_check_uses_exact_protocol_argv_and_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = make_updater_paths(Path(temporary))
            completed = subprocess.CompletedProcess([], 0, stdout="false", stderr="")
            with patch("internal.updater.subprocess.run", return_value=completed) as run:
                result = UpdaterService(paths).check()

            self.assertEqual(result, CheckResult(True, update_available=False))
            run.assert_called_once_with(
                [str(paths.updater_exe), "--check"],
                cwd=str(paths.updater_dir),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                timeout=120.0,
                check=False,
                text=True,
                encoding="utf-8",
                errors="replace",
            )

    def test_second_parallel_check_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = make_updater_paths(Path(temporary))
            started = threading.Event()
            release = threading.Event()

            def run_process(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
                started.set()
                release.wait(timeout=5)
                return subprocess.CompletedProcess([], 0, stdout="false", stderr="")

            service = UpdaterService(paths)
            first_result: list[CheckResult] = []
            with patch("internal.updater.subprocess.run", side_effect=run_process):
                worker = threading.Thread(target=lambda: first_result.append(service.check()))
                worker.start()
                self.assertTrue(started.wait(timeout=2))
                second = service.check()
                release.set()
                worker.join(timeout=2)

            self.assertFalse(second.ok)
            self.assertEqual(second.message, "update check is already running")
            self.assertEqual(first_result, [CheckResult(True, update_available=False)])

    def test_timeout_returns_error_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = make_updater_paths(Path(temporary))
            service = UpdaterService(paths, timeout=0.01)
            with patch(
                "internal.updater.subprocess.run",
                side_effect=subprocess.TimeoutExpired(["updater-ll.exe", "--check"], 0.01),
            ):
                result = service.check()

            self.assertFalse(result.ok)
            self.assertEqual(result.message, "update check timed out")

    def test_failed_check_start_returns_error_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = make_updater_paths(Path(temporary))
            service = UpdaterService(paths)
            with patch("internal.updater.subprocess.run", side_effect=OSError("start failed")):
                result = service.check()

            self.assertFalse(result.ok)
            self.assertEqual(result.message, "start failed")

    def test_missing_updater_is_non_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = resolve_app_paths(Path(temporary), development_root=None)
            result = UpdaterService(paths).check()

            self.assertFalse(result.ok)
            self.assertIn("updater directory does not exist", result.message)

    def test_successful_install_is_detached_and_not_waited_for(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = make_updater_paths(Path(temporary))
            process = Mock(pid=42)
            service = UpdaterService(paths, restart_command="llala-laucher.exe start")
            with patch("internal.updater.subprocess.Popen", return_value=process) as popen:
                result = service.install()

            self.assertEqual(result, InstallResult(True, pid=42))
            popen.assert_called_once_with(
                [
                    str(paths.updater_exe),
                    "--upgrade",
                    "--gui",
                    "--cmd",
                    "llala-laucher.exe start",
                ],
                cwd=str(paths.updater_dir),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
                close_fds=True,
            )
            process.wait.assert_not_called()

    def test_failed_install_start_returns_error_result_and_allows_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = make_updater_paths(Path(temporary))
            service = UpdaterService(paths, restart_command="llala-laucher.exe start")
            with patch("internal.updater.subprocess.Popen", side_effect=OSError("start failed")):
                first = service.install()
                second = service.install()

            self.assertEqual(first.message, "start failed")
            self.assertEqual(second.message, "start failed")

    def test_successful_install_rejects_a_second_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = make_updater_paths(Path(temporary))
            service = UpdaterService(paths, restart_command="llala-laucher.exe start")
            with patch("internal.updater.subprocess.Popen", return_value=Mock(pid=42)) as popen:
                first = service.install()
                second = service.install()

            self.assertTrue(first.ok)
            self.assertFalse(second.ok)
            self.assertEqual(second.message, "update installation is already running")
            self.assertEqual(popen.call_count, 1)


def make_update_app(state: UpdateState) -> LauncherApp:
    app = LauncherApp.__new__(LauncherApp)
    app.update_state = state
    app.root = Mock()
    app._append_log = Mock()

    def set_state(next_state: UpdateState, message: str = "") -> None:
        app.update_state = next_state
        app.update_message = message

    app._set_update_state = Mock(side_effect=set_state)
    return app


class LauncherUpdateStateTests(unittest.TestCase):
    def test_check_result_transitions(self) -> None:
        cases = [
            (CheckResult(True, False), UpdateState.IDLE),
            (CheckResult(True, True), UpdateState.AVAILABLE),
            (CheckResult(False, message="bad response"), UpdateState.ERROR),
        ]
        for result, expected in cases:
            with self.subTest(result=result):
                app = make_update_app(UpdateState.CHECKING)
                app._handle_update_check_result(result)
                self.assertEqual(app.update_state, expected)

    def test_failed_install_returns_to_available(self) -> None:
        app = make_update_app(UpdateState.INSTALLING)

        app._handle_update_install_result(InstallResult(False, message="start failed"))

        self.assertEqual(app.update_state, UpdateState.AVAILABLE)
        app.root.after.assert_not_called()

    def test_successful_install_schedules_graceful_quit(self) -> None:
        app = make_update_app(UpdateState.INSTALLING)
        app._request_quit = Mock()

        app._handle_update_install_result(InstallResult(True, pid=42))

        app.root.after.assert_called_once_with(
            LauncherApp.UPDATE_SHUTDOWN_DELAY_MS,
            app._request_quit,
        )


if __name__ == "__main__":
    unittest.main()
