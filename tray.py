"""Thread-safe Windows notification-area integration for the launcher."""

from __future__ import annotations

import ctypes
import logging
import os
from pathlib import Path
import queue
import threading
import time
from typing import Callable

from infi.systray import SysTrayIcon
from infi.systray import win32_adapter as systray_win32

from windows_integration import extract_executable_icon


LOGGER = logging.getLogger(__name__)

_register_window_message = ctypes.WinDLL("user32", use_last_error=True).RegisterWindowMessageW
_register_window_message.argtypes = (ctypes.c_wchar_p,)
_register_window_message.restype = ctypes.c_uint


class PersistentTrayIcon(SysTrayIcon):
    """SysTrayIcon that survives Explorer restarts and shuts down safely."""

    RETRY_INTERVAL_SECONDS = 1.5
    RETRY_JOIN_TIMEOUT_SECONDS = 2.0
    TASKBAR_CREATED_RETRY_DELAY_SECONDS = 0.75
    MESSAGE_JOIN_TIMEOUT_SECONDS = 2.0

    def __init__(
        self,
        *args: object,
        executable_icon_path: Path | None = None,
        **kwargs: object,
    ) -> None:
        self.window_ready_event = threading.Event()
        self.ready_event = threading.Event()
        self._icon_state_lock = threading.RLock()
        self._retry_condition = threading.Condition(self._icon_state_lock)
        self._retry_stop_event = threading.Event()
        self._retry_needed = False
        self._retry_delay_seconds = self.RETRY_INTERVAL_SECONDS
        self._retry_thread: threading.Thread | None = None
        self._icon_registered = False
        self._lifecycle_lock = threading.Lock()
        self._start_requested = False
        self._shutdown_requested = False
        self._executable_icon_path = executable_icon_path

        super().__init__(*args, **kwargs)
        self._taskbar_created_message = self._register_taskbar_created_handler()

    def _register_taskbar_created_handler(self) -> int:
        message_id = _register_window_message("TaskbarCreated")
        if not message_id:
            raise ctypes.WinError(ctypes.get_last_error())

        # infi.systray 0.1.12.1 registers this message through the ANSI API.
        # Python 3 passes a Unicode buffer, so the resulting ID is not the
        # actual TaskbarCreated message. Replace it with the W API result.
        for registered_id, handler in tuple(self._message_dict.items()):
            if handler == self._restart:
                del self._message_dict[registered_id]
        self._message_dict[message_id] = self._restart
        LOGGER.debug("Registered Unicode TaskbarCreated message: %s", message_id)
        return message_id

    @property
    def icon_registered(self) -> bool:
        with self._icon_state_lock:
            return self._icon_registered

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._start_requested or self._shutdown_requested:
                return
            self._start_requested = True
            self._retry_thread = threading.Thread(
                target=self._run_retry,
                name="llala-tray-icon-retry",
                daemon=True,
            )
            self._retry_thread.start()
        try:
            super().start()
        except Exception:
            self._stop_retry()
            raise

    def shutdown(self) -> None:
        with self._lifecycle_lock:
            if self._shutdown_requested:
                return
            self._shutdown_requested = True

        self._stop_retry()
        message_thread = self._message_loop_thread
        if message_thread is threading.current_thread():
            if self._hwnd:
                systray_win32.PostMessage(self._hwnd, systray_win32.WM_CLOSE, 0, 0)
            return

        deadline = time.monotonic() + self.MESSAGE_JOIN_TIMEOUT_SECONDS
        while message_thread is not None and message_thread.is_alive() and not self._hwnd:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self.window_ready_event.wait(min(0.05, remaining))
        if self._hwnd:
            systray_win32.PostMessage(self._hwnd, systray_win32.WM_CLOSE, 0, 0)
        if message_thread is not None and message_thread.is_alive():
            message_thread.join(self.MESSAGE_JOIN_TIMEOUT_SECONDS)
            if message_thread.is_alive():
                LOGGER.warning("Tray message-loop thread did not stop within the timeout")

    def _create_window(self) -> None:
        super()._create_window()
        self.window_ready_event.set()
        if self._shutdown_requested and self._hwnd:
            systray_win32.PostMessage(self._hwnd, systray_win32.WM_CLOSE, 0, 0)

    def _load_icon(self) -> None:
        if self._icon is not None and os.path.isfile(self._icon):
            super()._load_icon()
            return

        executable = self._executable_icon_path
        handle = extract_executable_icon(executable, small=False) if executable is not None else 0
        if handle:
            self._release_owned_icon()
            self._hicon = handle
            self._icon_shared = False
            return
        super()._load_icon()

    def update(self, icon: str | None = None, hover_text: str | None = None) -> bool:
        with self._icon_state_lock:
            if icon:
                self._icon = icon
                self._load_icon()
            if hover_text:
                self._hover_text = hover_text
            return self._refresh_icon_locked()

    def _refresh_icon(self) -> bool:
        with self._icon_state_lock:
            return self._refresh_icon_locked()

    def _refresh_icon_locked(self) -> bool:
        if self._hwnd is None or self._shutdown_requested:
            return False
        if self._hicon == 0:
            self._load_icon()

        self._notify_id = systray_win32.NotifyData(
            self._hwnd,
            0,
            systray_win32.NIF_ICON | systray_win32.NIF_MESSAGE | systray_win32.NIF_TIP,
            systray_win32.WM_USER + 20,
            self._hicon,
            self._hover_text,
        )
        message = systray_win32.NIM_MODIFY if self._icon_registered else systray_win32.NIM_ADD
        if self._notify_icon_locked(message):
            self._set_icon_registered_locked(True)
            return True

        if message == systray_win32.NIM_MODIFY:
            self._set_icon_registered_locked(False)
            if self._notify_icon_locked(systray_win32.NIM_ADD):
                self._set_icon_registered_locked(True)
                return True

        self._set_icon_registered_locked(False)
        self._schedule_retry_locked()
        return False

    def _notify_icon_locked(self, message: int) -> bool:
        return bool(systray_win32.Shell_NotifyIcon(message, ctypes.byref(self._notify_id)))

    def _restart(self, hwnd: object, msg: int, wparam: int, lparam: int) -> int:
        del hwnd, wparam, lparam
        with self._icon_state_lock:
            self._set_icon_registered_locked(False)
            self._schedule_retry_locked(self.TASKBAR_CREATED_RETRY_DELAY_SECONDS)
        LOGGER.info("Explorer restarted; scheduling tray icon registration (message %s)", msg)
        return 0

    def _destroy(self, hwnd: object, msg: int, wparam: int, lparam: int) -> object:
        self._stop_retry()
        with self._icon_state_lock:
            self._set_icon_registered_locked(False)
            self.window_ready_event.clear()
        result = super()._destroy(hwnd, msg, wparam, lparam)
        with self._icon_state_lock:
            self._release_owned_icon()
        return result

    def _release_owned_icon(self) -> None:
        if not self._icon_shared and self._hicon:
            systray_win32.DestroyIcon(self._hicon)
            self._hicon = 0

    def _set_icon_registered_locked(self, registered: bool) -> None:
        self._icon_registered = registered
        if registered:
            self.ready_event.set()
            self._retry_needed = False
        else:
            self.ready_event.clear()

    def _schedule_retry_locked(self, delay_seconds: float | None = None) -> None:
        if self._retry_stop_event.is_set() or self._hwnd is None:
            return
        self._retry_delay_seconds = delay_seconds or self.RETRY_INTERVAL_SECONDS
        self._retry_needed = True
        self._retry_condition.notify_all()

    def _run_retry(self) -> None:
        while not self._retry_stop_event.is_set():
            with self._retry_condition:
                while not self._retry_needed and not self._retry_stop_event.is_set():
                    self._retry_condition.wait()
                if self._retry_stop_event.is_set():
                    return
                retry_delay_seconds = self._retry_delay_seconds

            if self._retry_stop_event.wait(retry_delay_seconds):
                return
            with self._icon_state_lock:
                if not self._retry_needed:
                    continue
            try:
                self._refresh_icon()
            except Exception:
                if not self._retry_stop_event.is_set():
                    LOGGER.warning("Could not re-register tray icon", exc_info=True)

    def _stop_retry(self) -> None:
        with self._retry_condition:
            self._retry_stop_event.set()
            self._retry_needed = False
            self._retry_condition.notify_all()
            retry_thread = self._retry_thread
        if (
            retry_thread is not None
            and retry_thread.is_alive()
            and retry_thread is not threading.current_thread()
        ):
            retry_thread.join(self.RETRY_JOIN_TIMEOUT_SECONDS)


class TrayController:
    """Queue-only bridge between infi.systray callbacks and tkinter."""

    def __init__(
        self,
        events: queue.Queue[tuple[str, object]],
        icon_path: Path | None,
        executable_icon_path: Path | None = None,
        tray_factory: Callable[..., PersistentTrayIcon] = PersistentTrayIcon,
    ) -> None:
        self._events = events
        self._lock = threading.Lock()
        self._started = False
        self._shutdown = False
        self.systray = tray_factory(
            str(icon_path) if icon_path is not None else None,
            "llala-launcher",
            (("Open", None, self._queue_open),),
            on_quit=self._queue_quit,
            default_menu_index=0,
            executable_icon_path=executable_icon_path,
        )

    def _queue_open(self, _systray: SysTrayIcon) -> None:
        self._events.put(("tray_open", None))

    def _queue_quit(self, _systray: SysTrayIcon) -> None:
        self._events.put(("tray_quit", None))

    def start(self) -> None:
        with self._lock:
            if self._started or self._shutdown:
                return
            self._started = True
        self.systray.start()

    def shutdown(self) -> None:
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
        self.systray.shutdown()
