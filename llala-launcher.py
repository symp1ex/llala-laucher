"""Windows entry point for llala-launcher."""

from __future__ import annotations

from pathlib import Path
import tkinter as tk

from app import LauncherApp
from app_paths import resolve_app_paths


def main() -> None:
    base_dir = Path(__file__).resolve().parent
    paths = resolve_app_paths(base_dir)
    root = tk.Tk()
    LauncherApp(root, paths)
    root.mainloop()


if __name__ == "__main__":
    main()
