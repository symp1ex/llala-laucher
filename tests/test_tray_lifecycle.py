from __future__ import annotations

from pathlib import Path
import queue
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, call, patch

from app import LauncherApp
from tray import PersistentTrayIcon, TrayController, systray_win32
from windows_integration import resolve_icon_path


class RecordingTray:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.args = args
        self.kwargs = kwargs
        self.start_calls = 0
        self.shutdown_calls = 0

    def start(self) -> None:
        self.start_calls += 1

    def shutdown(self) -> None:
        self.shutdown_calls += 1


class TrayControllerTests(unittest.TestCase):
    def make_controller(self) -> tuple[TrayController, queue.Queue[tuple[str, object]]]:
        events: queue.Queue[tuple[str, object]] = queue.Queue()
        controller = TrayController(events, Path("icon.ico"), tray_factory=RecordingTray)
        return controller, events

    def test_open_is_first_and_is_the_double_click_default(self) -> None:
        controller, _events = self.make_controller()
        menu_options = controller.systray.args[2]

        self.assertEqual(menu_options[0][0], "Open")
        self.assertEqual(controller.systray.kwargs["default_menu_index"], 0)
        self.assertEqual(len(menu_options), 1)

    def test_open_callback_only_queues_a_ui_event(self) -> None:
        controller, events = self.make_controller()
        open_callback = controller.systray.args[2][0][2]

        open_callback(controller.systray)

        self.assertEqual(events.get_nowait(), ("tray_open", None))

    def test_quit_callback_only_queues_a_ui_event(self) -> None:
        controller, events = self.make_controller()

        controller.systray.kwargs["on_quit"](controller.systray)

        self.assertEqual(events.get_nowait(), ("tray_quit", None))

    def test_start_and_shutdown_are_idempotent(self) -> None:
        controller, _events = self.make_controller()

        controller.start()
        controller.start()
        controller.shutdown()
        controller.shutdown()

        self.assertEqual(controller.systray.start_calls, 1)
        self.assertEqual(controller.systray.shutdown_calls, 1)


class PersistentTrayIconTests(unittest.TestCase):
    def test_failed_modify_retries_with_add(self) -> None:
        icon = PersistentTrayIcon.__new__(PersistentTrayIcon)
        icon._icon_state_lock = threading.RLock()
        icon._hwnd = 123
        icon._shutdown_requested = False
        icon._hicon = 456
        icon._hover_text = "llala-laucher"
        icon._icon_registered = True
        icon.ready_event = threading.Event()
        icon.ready_event.set()
        icon._retry_needed = False
        icon._notify_icon_locked = Mock(side_effect=[False, True])

        with patch("tray.systray_win32.NotifyData", return_value=object()):
            refreshed = icon._refresh_icon()

        self.assertTrue(refreshed)
        self.assertEqual(
            icon._notify_icon_locked.call_args_list,
            [call(systray_win32.NIM_MODIFY), call(systray_win32.NIM_ADD)],
        )
        self.assertTrue(icon.icon_registered)

    def test_missing_sidecar_uses_owned_executable_icon(self) -> None:
        icon = PersistentTrayIcon.__new__(PersistentTrayIcon)
        icon._icon = None
        icon._hicon = 0
        icon._icon_shared = True
        icon._executable_icon_path = Path("llala-laucher.exe")
        icon._release_owned_icon = Mock()

        with patch("tray.extract_executable_icon", return_value=789) as extract:
            icon._load_icon()

        extract.assert_called_once_with(Path("llala-laucher.exe"), small=False)
        icon._release_owned_icon.assert_called_once_with()
        self.assertEqual(icon._hicon, 789)
        self.assertFalse(icon._icon_shared)


def make_lifecycle_app(*, running: bool) -> LauncherApp:
    app = LauncherApp.__new__(LauncherApp)
    app.root = Mock()
    app.server_process = Mock()
    app.server_process.is_running.return_value = running
    app.tray = Mock()
    app.window_icon_handles = Mock()
    app.run_status_var = Mock()
    app.pid_var = Mock()
    app.closing = False
    app.quit_finished = False
    app.stopping = False
    app.server_url = "http://127.0.0.1:8080/"
    app._save_settings = Mock()
    app._append_log = Mock()
    app._update_buttons = Mock()
    return app


class LauncherLifecycleTests(unittest.TestCase):
    def test_window_close_only_withdraws(self) -> None:
        app = make_lifecycle_app(running=True)

        app._on_close()

        app.root.withdraw.assert_called_once_with()
        app.server_process.stop_async.assert_not_called()
        app.root.destroy.assert_not_called()

    def test_open_restores_and_focuses_the_existing_root(self) -> None:
        app = make_lifecycle_app(running=False)

        app._show_main_window()

        app.root.deiconify.assert_called_once_with()
        app.root.state.assert_called_once_with("normal")
        app.root.lift.assert_called_once_with()
        app.root.after_idle.assert_called_once_with(app._focus_main_window)

    def test_quit_waits_for_running_server_exit_event(self) -> None:
        app = make_lifecycle_app(running=True)

        app._request_quit()

        app.server_process.stop_async.assert_called_once_with()
        app.root.destroy.assert_not_called()
        app.tray.shutdown.assert_not_called()

        app._handle_server_event("exit", 0)

        app._save_settings.assert_called_once_with()
        app.tray.shutdown.assert_called_once_with()
        app.root.destroy.assert_called_once_with()

    def test_quit_while_stop_is_in_progress_is_safe(self) -> None:
        app = make_lifecycle_app(running=True)
        app.stopping = True

        app._request_quit()
        app._request_quit()

        app.server_process.stop_async.assert_called_once_with()
        app.root.destroy.assert_not_called()
        app._handle_server_event("exit", 7)
        app.root.destroy.assert_called_once_with()

    def test_quit_with_stopped_server_finishes_immediately(self) -> None:
        app = make_lifecycle_app(running=False)

        app._request_quit()

        app.server_process.stop_async.assert_not_called()
        app.tray.shutdown.assert_called_once_with()
        app.root.destroy.assert_called_once_with()
        app.window_icon_handles.close.assert_called_once_with()

    def test_repeated_quit_is_idempotent(self) -> None:
        app = make_lifecycle_app(running=False)

        app._request_quit()
        app._request_quit()

        app._save_settings.assert_called_once_with()
        app.tray.shutdown.assert_called_once_with()
        app.root.destroy.assert_called_once_with()

    def test_tray_quit_event_is_processed_in_ui_handler(self) -> None:
        app = make_lifecycle_app(running=True)

        app._handle_background_event("tray_quit", None)

        app.server_process.stop_async.assert_called_once_with()
        app.root.destroy.assert_not_called()


class IconResolverTests(unittest.TestCase):
    def test_source_mode_prefers_project_root_icon(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base_dir = Path(temporary)
            icon = base_dir / "icon.ico"
            icon.touch()
            with patch.object(sys, "frozen", False, create=True):
                self.assertEqual(resolve_icon_path(base_dir), icon.resolve())

    def test_frozen_mode_uses_icon_next_to_executable_first(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "dist" / "llala-laucher.exe"
            executable.parent.mkdir()
            executable.touch()
            sidecar = executable.parent / "icon.ico"
            sidecar.touch()
            bundled = root / "bundle" / "icon.ico"
            bundled.parent.mkdir()
            bundled.touch()

            with (
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "executable", str(executable)),
                patch.object(sys, "_MEIPASS", str(bundled.parent), create=True),
            ):
                self.assertEqual(resolve_icon_path(root), sidecar.resolve())

    def test_frozen_mode_falls_back_to_meipass_data_icon(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "dist" / "llala-laucher.exe"
            executable.parent.mkdir()
            executable.touch()
            bundled = root / "bundle" / "icon.ico"
            bundled.parent.mkdir()
            bundled.touch()

            with (
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "executable", str(executable)),
                patch.object(sys, "_MEIPASS", str(bundled.parent), create=True),
            ):
                self.assertEqual(resolve_icon_path(root), bundled.resolve())


if __name__ == "__main__":
    unittest.main()
