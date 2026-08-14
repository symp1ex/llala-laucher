"""Application path resolution for llala-laucher."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


# Temporary development fallback. Remove this value after copying llama.cpp into
# <launcher>/llama. The local directory always wins when it contains the server.
DEVELOPMENT_LLAMA_ROOT: Path | None = Path(
    r"D:\\itt\\llala-laucher\\llama"
)


@dataclass(frozen=True, slots=True)
class AppPaths:
    base_dir: Path
    updater_dir: Path
    updater_exe: Path
    llama_root: Path
    server: Path
    models: Path
    presets: Path
    settings: Path
    web_mcp: Path
    using_development_fallback: bool


def resolve_app_paths(
    base_dir: Path,
    development_root: Path | None = DEVELOPMENT_LLAMA_ROOT,
) -> AppPaths:
    """Resolve all paths without depending on the current working directory."""
    base_dir = base_dir.resolve()
    local_root = base_dir / "llama"
    local_server = local_root / "llama-server.exe"
    using_fallback = False

    if local_server.is_file():
        llama_root = local_root
    elif development_root is not None and (development_root / "llama-server.exe").is_file():
        llama_root = development_root.resolve()
        using_fallback = True
    else:
        llama_root = local_root

    return AppPaths(
        base_dir=base_dir,
        updater_dir=base_dir / "updater",
        updater_exe=base_dir / "updater" / "updater-ll.exe",
        llama_root=llama_root,
        server=llama_root / "llama-server.exe",
        models=llama_root / "models",
        presets=llama_root / "preset",
        settings=base_dir / "laucher-settings.json",
        web_mcp=base_dir / "mcp" / "web-mcp.exe",
        using_development_fallback=using_fallback,
    )
