"""GGUF model discovery and stable preset directory identifiers."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path, PurePath
import re


_WINDOWS_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


@dataclass(frozen=True, slots=True)
class ModelInfo:
    path: Path
    relative_path: Path
    display_name: str
    model_id: str


def sanitize_windows_component(value: str, fallback: str = "item") -> str:
    """Return a usable, non-reserved Windows file-name component."""
    value = _WINDOWS_FORBIDDEN.sub("_", value).strip().rstrip(". ")
    value = re.sub(r"\s+", " ", value)
    if not value:
        value = fallback
    if value.upper() in _RESERVED_NAMES:
        value = f"_{value}"
    return value


def model_id_for_relative(relative_path: PurePath) -> str:
    """Build a readable ID with a case-insensitive collision-resistant suffix."""
    without_suffix = relative_path.with_suffix("")
    readable = "__".join(
        sanitize_windows_component(part, "model") for part in without_suffix.parts
    )
    readable = readable[:100].rstrip(". _-") or "model"
    normalized = relative_path.as_posix().casefold()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:10]
    return f"{readable}--{digest}"


class ModelScanner:
    def __init__(self, models_root: Path) -> None:
        self.models_root = models_root

    def scan(self) -> list[ModelInfo]:
        if not self.models_root.is_dir():
            return []

        models: list[ModelInfo] = []
        for path in self.models_root.rglob("*"):
            if not path.is_file() or path.suffix.casefold() != ".gguf":
                continue
            relative = path.relative_to(self.models_root)
            display = relative.as_posix()
            models.append(
                ModelInfo(
                    path=path.resolve(),
                    relative_path=relative,
                    display_name=display,
                    model_id=model_id_for_relative(relative),
                )
            )
        return sorted(models, key=lambda model: model.display_name.casefold())
