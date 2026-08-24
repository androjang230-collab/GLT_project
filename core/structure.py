"""Portable, engine-neutral game structure inspection reports."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from core.models import EngineId


@dataclass(frozen=True, slots=True)
class StructureFile:
    """One relative file candidate recorded without machine-specific paths."""

    file: str
    size: int
    sha256: str | None = None
    header_hex: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_json_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"file": self.file, "size": self.size}
        if self.sha256 is not None:
            payload["sha256"] = self.sha256
        if self.header_hex is not None:
            payload["header_hex"] = self.header_hex
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload


@dataclass(frozen=True, slots=True)
class StructureReport:
    """Read-only reconnaissance result shared by current and future adapters."""

    engine: EngineId
    confidence: int
    evidence: tuple[str, ...]
    root: str = "."
    executables: tuple[StructureFile, ...] = ()
    data_files: tuple[StructureFile, ...] = ()
    data_directories: tuple[str, ...] = ()
    archive_files: tuple[StructureFile, ...] = ()
    possible_text_sources: tuple[StructureFile, ...] = ()
    possible_map_files: tuple[StructureFile, ...] = ()
    possible_common_event_files: tuple[StructureFile, ...] = ()
    possible_database_files: tuple[StructureFile, ...] = ()
    possible_font_files: tuple[StructureFile, ...] = ()
    media_directories: tuple[str, ...] = ()
    unknown_binary_files: tuple[StructureFile, ...] = ()
    possible_version: str | None = None
    version_confidence: str = "unknown"
    packaging_type: str = "unknown"
    encryption_status: str = "unknown"
    relevant_files: tuple[StructureFile, ...] = ()
    notes: tuple[str, ...] = ()
    extra_metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not 0 <= self.confidence <= 100:
            raise ValueError("confidence must be between 0 and 100")
        if Path(self.root).is_absolute():
            raise ValueError("structure report root must not be absolute")

    def to_json_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "engine_id": self.engine.value,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "root": self.root,
            "executables": _files(self.executables),
            "data_files": _files(self.data_files),
            "data_directories": list(self.data_directories),
            "archive_files": _files(self.archive_files),
            "possible_text_sources": _files(self.possible_text_sources),
            "possible_map_files": _files(self.possible_map_files),
            "possible_common_event_files": _files(self.possible_common_event_files),
            "possible_database_files": _files(self.possible_database_files),
            "possible_font_files": _files(self.possible_font_files),
            "media_directories": list(self.media_directories),
            "unknown_binary_files": _files(self.unknown_binary_files),
            "possible_version": self.possible_version,
            "version_confidence": self.version_confidence,
            "packaging_type": self.packaging_type,
            "encryption_status": self.encryption_status,
            "relevant_files": _files(self.relevant_files),
            "notes": list(self.notes),
        }
        collisions = set(payload) & set(self.extra_metadata)
        if collisions:
            raise ValueError(
                f"extra metadata conflicts with structure fields: {sorted(collisions)!r}"
            )
        payload.update(self.extra_metadata)
        return payload


def write_structure_report(path: Path, report: StructureReport) -> Path:
    """Atomically create a new JSON report without overwriting an existing file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"structure report already exists: {path}")
    data = (
        json.dumps(report.to_json_dict(), ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            delete=False,
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(data)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.link(temporary_path, path)
        temporary_path.unlink()
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return path


def _files(items: tuple[StructureFile, ...]) -> list[dict[str, object]]:
    return [item.to_json_dict() for item in items]
