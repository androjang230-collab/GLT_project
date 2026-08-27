"""Reusable, bounded inventory for RPG Maker plugin registries.

The inventory parses ``js/plugins.js`` as data and never executes JavaScript.
It intentionally keeps parameter values in memory for static analysis while
exposing only hashes and relative source locations through its serializable
models.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


MAX_PLUGIN_FILES = 2_000
MAX_PLUGIN_FILE_BYTES = 8 * 1024 * 1024
MAX_REGISTRY_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class PluginInventoryIssue:
    severity: str
    code: str
    reason: str
    plugin_name: str | None = None
    source_file: str | None = None


@dataclass(frozen=True, slots=True)
class PluginRecord:
    name: str
    enabled: bool | None
    load_order: int | None
    parameters: Mapping[str, Any] = field(default_factory=dict)
    source_file: str | None = None
    source_path: Path | None = None
    source_available: bool = False
    source_sha256: str | None = None
    source_size: int | None = None


@dataclass(frozen=True, slots=True)
class PluginInventory:
    registry_available: bool
    plugins: tuple[PluginRecord, ...] = ()
    issues: tuple[PluginInventoryIssue, ...] = ()

    @property
    def plugin_count(self) -> int:
        return len(self.plugins)

    @property
    def active_plugins(self) -> tuple[PluginRecord, ...]:
        """Return only plugins explicitly enabled by ``status: true``."""

        return tuple(item for item in self.plugins if item.enabled is True)

    def selected_plugins(
        self,
        *,
        active_only: bool = True,
        include_unverified: bool = False,
    ) -> tuple[PluginRecord, ...]:
        if active_only:
            return self.active_plugins
        if include_unverified:
            return tuple(item for item in self.plugins if item.enabled is not False)
        return self.plugins


def load_plugin_inventory(
    plugin_config_file: Path | None,
    plugin_source_directory: Path,
) -> PluginInventory:
    """Load a portable plugin inventory without executing plugin JavaScript."""

    source_directory = plugin_source_directory.resolve()
    if _is_linklike(source_directory):
        raise ValueError("plugin source directory cannot be a symlink or junction")
    config = plugin_config_file.resolve() if plugin_config_file is not None else None
    if config is not None and _is_linklike(config):
        raise ValueError("plugins.js cannot be a symlink or junction")

    issues: list[PluginInventoryIssue] = []
    raw_records: list[tuple[str, bool | None, int | None, Mapping[str, Any]]]
    registry_available = config is not None and config.is_file()
    if not registry_available:
        files = sorted(
            source_directory.glob("*.js"), key=lambda item: item.name.casefold()
        )
        if len(files) > MAX_PLUGIN_FILES:
            raise ValueError(f"plugin source count exceeds {MAX_PLUGIN_FILES}")
        issues.append(
            PluginInventoryIssue(
                "warning",
                "PLUGIN_REGISTRY_MISSING",
                "enabled status and load order are unavailable",
            )
        )
        raw_records = [(path.stem, None, None, {}) for path in files]
    else:
        raw_records = _read_registry(config, issues)

    plugins: list[PluginRecord] = []
    for name, enabled, load_order, parameters in raw_records:
        safe_name = Path(name).name == name and name not in {".", ".."}
        source = source_directory / f"{name}.js" if safe_name else None
        source_file = f"js/plugins/{name}.js" if safe_name else None
        available = False
        source_hash: str | None = None
        source_size: int | None = None
        if source is None:
            issues.append(
                PluginInventoryIssue(
                    "warning",
                    "PLUGIN_SOURCE_PATH_INVALID",
                    "plugin name cannot be mapped to a safe source path",
                    name,
                )
            )
        elif not source.is_file() or _is_linklike(source):
            issues.append(
                PluginInventoryIssue(
                    "warning",
                    "PLUGIN_SOURCE_MISSING",
                    "enabled plugin has no exact same-name source file"
                    if enabled is True
                    else "plugin has no exact same-name source file",
                    name,
                    source_file,
                )
            )
        else:
            try:
                resolved = source.resolve()
                if not resolved.is_relative_to(source_directory):
                    raise ValueError("plugin source resolves outside plugin directory")
                source_size = source.stat().st_size
                if source_size > MAX_PLUGIN_FILE_BYTES:
                    issues.append(
                        PluginInventoryIssue(
                            "warning",
                            "PLUGIN_SOURCE_TOO_LARGE",
                            f"source exceeds {MAX_PLUGIN_FILE_BYTES} bytes",
                            name,
                            source_file,
                        )
                    )
                else:
                    source_hash = _sha256_file(source)
                    available = True
            except (OSError, ValueError) as exc:
                issues.append(
                    PluginInventoryIssue(
                        "warning",
                        "PLUGIN_SOURCE_READ_ERROR",
                        str(exc),
                        name,
                        source_file,
                    )
                )
        plugins.append(
            PluginRecord(
                name=name,
                enabled=enabled,
                load_order=load_order,
                parameters=dict(parameters),
                source_file=source_file,
                source_path=source,
                source_available=available,
                source_sha256=source_hash,
                source_size=source_size,
            )
        )
    return PluginInventory(registry_available, tuple(plugins), tuple(issues))


def _read_registry(
    config: Path,
    issues: list[PluginInventoryIssue],
) -> list[tuple[str, bool, int, Mapping[str, Any]]]:
    try:
        if config.stat().st_size > MAX_REGISTRY_BYTES:
            raise ValueError(f"plugins.js exceeds {MAX_REGISTRY_BYTES} bytes")
        text = config.read_text(encoding="utf-8-sig")
        match = re.search(
            r"(?:var\s+)?\$plugins\s*=\s*(\[[\s\S]*\])\s*;?\s*$", text
        )
        if match is None:
            raise ValueError("plugins.js does not contain a supported $plugins array")
        payload = json.loads(_normalize_registry_array(match.group(1)))
        if not isinstance(payload, list):
            raise ValueError("plugin registry is not an array")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        issues.append(
            PluginInventoryIssue(
                "error", "PLUGIN_REGISTRY_ERROR", str(exc), source_file=config.name
            )
        )
        return []

    if len(payload) > MAX_PLUGIN_FILES:
        issues.append(
            PluginInventoryIssue(
                "warning",
                "PLUGIN_REGISTRY_TRUNCATED",
                f"plugin registry exceeds {MAX_PLUGIN_FILES} entries",
                source_file=config.name,
            )
        )
    result: list[tuple[str, bool, int, Mapping[str, Any]]] = []
    for index, item in enumerate(payload[:MAX_PLUGIN_FILES]):
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            issues.append(
                PluginInventoryIssue(
                    "error",
                    "PLUGIN_REGISTRY_ENTRY_ERROR",
                    f"plugin entry {index} is not a supported registry object",
                    source_file=config.name,
                )
            )
            return []
        if "status" in item and not isinstance(item["status"], bool):
            issues.append(
                PluginInventoryIssue(
                    "error",
                    "PLUGIN_REGISTRY_ENTRY_ERROR",
                    f"plugin entry {index} has a non-boolean status",
                    plugin_name=item["name"],
                    source_file=config.name,
                )
            )
            return []
        parameters = item.get("parameters")
        if parameters is not None and not isinstance(parameters, dict):
            issues.append(
                PluginInventoryIssue(
                    "error",
                    "PLUGIN_REGISTRY_ENTRY_ERROR",
                    f"plugin entry {index} has a non-object parameters value",
                    plugin_name=item["name"],
                    source_file=config.name,
                )
            )
            return []
        result.append(
            (
                item["name"],
                item.get("status") is True,
                index,
                parameters if isinstance(parameters, dict) else {},
            )
        )
    return result


def _normalize_registry_array(payload: str) -> str:
    """Remove only a terminal top-level array comma outside JSON strings.

    RPG Maker writes ``plugins.js`` as JavaScript and some versions leave a
    comma after the final plugin object.  The remaining bounded payload must
    still be valid JSON; no other JavaScript syntax is interpreted or guessed.
    """

    if not payload.startswith("["):
        raise ValueError("plugin registry payload does not start with an array")

    in_string = False
    escaped = False
    array_depth = 0
    object_depth = 0
    outer_close: int | None = None
    for index, character in enumerate(payload):
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "[":
            array_depth += 1
        elif character == "]":
            if array_depth == 0:
                raise ValueError("plugin registry has an unmatched closing bracket")
            array_depth -= 1
            if array_depth == 0:
                outer_close = index
                if payload[index + 1 :].strip():
                    raise ValueError(
                        "plugin registry contains unsupported syntax after its array"
                    )
        elif character == "{":
            object_depth += 1
        elif character == "}":
            if object_depth == 0:
                raise ValueError("plugin registry has an unmatched closing brace")
            object_depth -= 1

    if in_string:
        raise ValueError("plugin registry contains an unterminated string")
    if array_depth != 0 or object_depth != 0 or outer_close is None:
        raise ValueError("plugin registry contains unbalanced JSON structure")

    previous = outer_close - 1
    while previous >= 0 and payload[previous].isspace():
        previous -= 1
    if previous >= 0 and payload[previous] == ",":
        return payload[:previous] + payload[previous + 1 :]
    return payload


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_linklike(path: Path) -> bool:
    try:
        junction = getattr(path, "is_junction", None)
        return path.is_symlink() or os.path.islink(path) or bool(junction and junction())
    except OSError:
        return True


__all__ = [
    "PluginInventory",
    "PluginInventoryIssue",
    "PluginRecord",
    "load_plugin_inventory",
]
