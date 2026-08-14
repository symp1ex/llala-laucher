"""Versioned structured preset persistence."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Mapping

from .model_scanner import ModelInfo, sanitize_windows_component
from .parameter_specs import PARAMETER_SPECS, ParameterSpec


SCHEMA_VERSION = 1


class PresetError(ValueError):
    pass


def normalize_preset_parameters(
    parameters: Mapping[str, Any],
    specs: tuple[ParameterSpec, ...] = PARAMETER_SPECS,
) -> tuple[dict[str, Any], list[str]]:
    """Normalize known entries while retaining unknown future-version data."""
    known = {spec.key: spec for spec in specs}
    normalized: dict[str, Any] = {}
    warnings: list[str] = []

    for spec in specs:
        if spec.key not in parameters:
            # Presets historically represented only explicit choices. Missing
            # entries must therefore stay disabled, even for safe UI defaults.
            normalized[spec.key] = {"enabled": False, "value": deepcopy(spec.default)}
            continue
        raw = parameters[spec.key]
        if not isinstance(raw, Mapping):
            warnings.append(f"Malformed preset parameter reset to default: {spec.key}")
            normalized[spec.key] = {"enabled": False, "value": deepcopy(spec.default)}
            continue
        enabled = raw.get("enabled", False)
        if not isinstance(enabled, bool):
            warnings.append(f"Invalid enabled value reset to false: {spec.key}")
            enabled = False
        value = raw.get("value", deepcopy(spec.default))
        if value is None or isinstance(value, Mapping):
            warnings.append(f"Invalid value reset to default: {spec.key}")
            value = deepcopy(spec.default)
        normalized[spec.key] = {"enabled": enabled, "value": value}

    for key, raw in parameters.items():
        if key not in known:
            normalized[key] = raw
            warnings.append(f"Unknown preset parameter preserved: {key}")
    return normalized, warnings


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
        parameters: Mapping[str, Any],
        description: str | None = None,
        *,
        preserved_parameters: Mapping[str, Any] | None = None,
    ) -> Path:
        target = self.path_for_name(model, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        merged: dict[str, Any] = dict(preserved_parameters or {})
        merged.update(parameters)
        if target.is_file():
            try:
                existing = self.load(target)
                existing_parameters = existing.get("parameters", {})
                if isinstance(existing_parameters, Mapping):
                    known_keys = {spec.key for spec in PARAMETER_SPECS}
                    for key, value in existing_parameters.items():
                        if key not in known_keys and key not in merged:
                            merged[key] = value
            except PresetError:
                # An explicitly confirmed overwrite may replace a damaged file.
                pass
        normalized, _warnings = normalize_preset_parameters(merged)
        document: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "name": target.stem,
            "model": {"relative_path": model.relative_path.as_posix()},
            "parameters": normalized,
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
