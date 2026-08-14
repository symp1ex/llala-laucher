"""Windows shell identity and application icon helpers."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
import sys
import tkinter as tk
from typing import Any


LOGGER = logging.getLogger(__name__)
APP_USER_MODEL_ID = "llala.launcher"

WM_SETICON = 0x0080
ICON_SMALL = 0
ICON_BIG = 1
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
SM_CXICON = 11
SM_CYICON = 12
SM_CXSMICON = 49
SM_CYSMICON = 50


def resolve_icon_path(base_dir: Path) -> Path | None:
    """Return the first standalone icon available for source or frozen use."""
    base_dir = base_dir.resolve()
    if getattr(sys, "frozen", False):
        candidates = [Path(sys.executable).resolve().parent / "icon.ico"]
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass).resolve() / "icon.ico")
        candidates.append(base_dir / "icon.ico")
    else:
        candidates = [base_dir / "icon.ico"]

    seen: set[Path] = set()
    for candidate in candidates:
        if candidate not in seen and candidate.is_file():
            return candidate
        seen.add(candidate)
    return None


def frozen_executable_path() -> Path | None:
    """Return the executable whose icon resource may be used as a fallback."""
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve()


def client_size_for_outer_window(
    root: Any,
    outer_width: int,
    outer_height: int,
) -> tuple[int, int]:
    """Convert a desired outer window size to the tkinter client size."""
    if os.name != "nt":
        return outer_width, outer_height

    root.update_idletasks()
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    child_hwnd = int(root.winfo_id())

    get_parent = user32.GetParent
    get_parent.argtypes = (wintypes.HWND,)
    get_parent.restype = wintypes.HWND
    hwnd = int(get_parent(child_hwnd) or child_hwnd)

    get_window_rect = user32.GetWindowRect
    get_window_rect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    get_window_rect.restype = wintypes.BOOL
    get_client_rect = user32.GetClientRect
    get_client_rect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    get_client_rect.restype = wintypes.BOOL

    window_rect = wintypes.RECT()
    client_rect = wintypes.RECT()
    if not get_window_rect(hwnd, ctypes.byref(window_rect)) or not get_client_rect(
        hwnd,
        ctypes.byref(client_rect),
    ):
        LOGGER.warning("Could not measure the native window frame; using client dimensions")
        return outer_width, outer_height

    frame_width = max(
        0,
        (window_rect.right - window_rect.left) - (client_rect.right - client_rect.left),
    )
    frame_height = max(
        0,
        (window_rect.bottom - window_rect.top) - (client_rect.bottom - client_rect.top),
    )
    return max(1, outer_width - frame_width), max(1, outer_height - frame_height)


def set_windows_app_user_model_id(app_id: str = APP_USER_MODEL_ID) -> None:
    """Set a stable Windows taskbar identity before creating the Tk root."""
    if os.name != "nt":
        return

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    setter = shell32.SetCurrentProcessExplicitAppUserModelID
    setter.argtypes = (ctypes.c_wchar_p,)
    setter.restype = ctypes.c_long
    result = setter(app_id)
    if result < 0:
        LOGGER.warning("Could not set AppUserModelID %s (HRESULT %#x)", app_id, result & 0xFFFFFFFF)


def extract_executable_icon(executable: Path, *, small: bool) -> int:
    """Extract one owned HICON from an executable resource."""
    if os.name != "nt" or not executable.is_file():
        return 0

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    extract_icon = shell32.ExtractIconExW
    extract_icon.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_int,
        ctypes.POINTER(wintypes.HICON),
        ctypes.POINTER(wintypes.HICON),
        wintypes.UINT,
    )
    extract_icon.restype = wintypes.UINT

    handle = wintypes.HICON()
    large_icons = None if small else ctypes.byref(handle)
    small_icons = ctypes.byref(handle) if small else None
    count = extract_icon(str(executable), 0, large_icons, small_icons, 1)
    return int(handle.value or 0) if count else 0


def _load_file_icon(icon_path: Path, width: int, height: int) -> int:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    load_image = user32.LoadImageW
    load_image.argtypes = (
        wintypes.HINSTANCE,
        ctypes.c_wchar_p,
        wintypes.UINT,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    )
    load_image.restype = wintypes.HANDLE
    handle = load_image(None, str(icon_path), IMAGE_ICON, width, height, LR_LOADFROMFILE)
    return int(handle or 0)


@dataclass
class WindowIconHandles:
    """Owned HICON values kept alive for the lifetime of a Tk window."""

    handles: list[int] = field(default_factory=list)

    def close(self) -> None:
        if os.name != "nt" or not self.handles:
            return
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        destroy_icon = user32.DestroyIcon
        destroy_icon.argtypes = (wintypes.HICON,)
        destroy_icon.restype = wintypes.BOOL
        for handle in self.handles:
            destroy_icon(handle)
        self.handles.clear()


def apply_window_icon(
    root: Any,
    icon_path: Path | None,
    executable_fallback: Path | None = None,
) -> WindowIconHandles:
    """Apply ICO or frozen EXE icons to Tk and the native top-level HWND."""
    if icon_path is not None:
        try:
            root.iconbitmap(default=str(icon_path))
        except (OSError, tk.TclError) as exc:
            LOGGER.warning("Could not apply tkinter icon %s: %s", icon_path, exc)

    owned = WindowIconHandles()
    if os.name != "nt":
        return owned

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    get_system_metrics = user32.GetSystemMetrics
    get_system_metrics.argtypes = (ctypes.c_int,)
    get_system_metrics.restype = ctypes.c_int

    if icon_path is not None:
        large = _load_file_icon(
            icon_path,
            get_system_metrics(SM_CXICON),
            get_system_metrics(SM_CYICON),
        )
        small = _load_file_icon(
            icon_path,
            get_system_metrics(SM_CXSMICON),
            get_system_metrics(SM_CYSMICON),
        )
    elif executable_fallback is not None:
        large = extract_executable_icon(executable_fallback, small=False)
        small = extract_executable_icon(executable_fallback, small=True)
    else:
        return owned

    owned.handles.extend(handle for handle in (large, small) if handle)
    if not owned.handles:
        LOGGER.warning("Could not load a native Windows application icon")
        return owned

    root.update_idletasks()
    hwnd = int(root.winfo_id())
    get_parent = user32.GetParent
    get_parent.argtypes = (wintypes.HWND,)
    get_parent.restype = wintypes.HWND
    parent_hwnd = int(get_parent(hwnd) or 0)
    targets = {hwnd}
    if parent_hwnd:
        targets.add(parent_hwnd)

    send_message = user32.SendMessageW
    send_message.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    send_message.restype = wintypes.LPARAM
    for target in targets:
        if small:
            send_message(target, WM_SETICON, ICON_SMALL, small)
        if large:
            send_message(target, WM_SETICON, ICON_BIG, large)
    return owned
