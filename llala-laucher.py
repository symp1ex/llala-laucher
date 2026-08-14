"""Windows entry point for llala-laucher."""

from __future__ import annotations

from pathlib import Path
import sys
import tkinter as tk

from internal.app import LauncherApp
from internal.app_paths import resolve_app_paths
from internal.windows_integration import set_windows_app_user_model_id


def main() -> None:
    if getattr(sys, "frozen", False):
        base_dir = Path(sys.executable).resolve().parent
    else:
        base_dir = Path(__file__).resolve().parent
    paths = resolve_app_paths(base_dir)
    set_windows_app_user_model_id()
    root = tk.Tk()
    LauncherApp(root, paths)
    root.mainloop()


if __name__ == "__main__":
    main()
