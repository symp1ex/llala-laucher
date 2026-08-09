"""Versioned structured preset persistence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from model_scanner import ModelInfo, sanitize_windows_component


SCHEMA_VERSION = 1


class PresetError(ValueError):
    pass


class PresetManager:
    def __init__(self, presets_root: Path) -> None:
        self.presets_root = presets_root

    def model_directory(self, model: ModelInfo) -> Path:
        return self.presets_root / model.model_id

    def scan(self, model: ModelInfo) -> list[Path]:
        directory = self.model_directory(model)
        if not directory.is_dir():
            return []
        return sorted(
            (path for path in directory.iterdir() if path.is_file() and path.suffix.casefold() == ".json"),
            key=lambda path: path.name.casefold(),
        )

    def load(self, path: Path) -> dict[str, Any]:
        try:
            with path.open("r", encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, json.JSONDecodeError) as exc:
            raise PresetError(f"Could not read preset '{path.name}': {exc}") from exc
        if not isinstance(data, dict):
            raise PresetError("Preset root must be a JSON object")
        if data.get("schema_version") != SCHEMA_VERSION:
            raise PresetError(
                f"Unsupported preset schema_version: {data.get('schema_version')!r}"
            )
        if not isinstance(data.get("parameters"), dict):
            raise PresetError("Preset 'parameters' must be a JSON object")
        return data

    def path_for_name(self, model: ModelInfo, name: str) -> Path:
        safe_name = sanitize_windows_component(name.strip(), "preset")
        if safe_name.casefold().endswith(".json"):
            safe_name = safe_name[:-5].rstrip(". ") or "preset"
        return self.model_directory(model) / f"{safe_name}.json"

    def save(
        self,
        model: ModelInfo,
        name: str,
        parameters: Mapping[str, Mapping[str, Any]],
        description: str | None = None,
    ) -> Path:
        target = self.path_for_name(model, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        document: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "name": target.stem,
            "model": {"relative_path": model.relative_path.as_posix()},
            "parameters": {
                key: {"enabled": bool(value.get("enabled", False)), "value": value.get("value")}
                for key, value in parameters.items()
            },
        }
        if description:
            document["description"] = description
        temporary = target.with_suffix(".json.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as file:
                json.dump(document, file, ensure_ascii=False, indent=2)
                file.write("\n")
            temporary.replace(target)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise PresetError(f"Could not save preset: {exc}") from exc
        return target
