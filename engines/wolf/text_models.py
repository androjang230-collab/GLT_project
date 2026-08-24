"""Portable models for WOLF RPG Editor ``.Auto.txt`` inspection."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote


WOLF_LOCATION_SCHEMA_VERSION = 1
WOLF_LOCATION_SCHEMA_STATUS = "provisional"


class WolfRecordClassification(str, Enum):
    VERIFIED_TRANSLATABLE = "verified_translatable"
    EXPERIMENTAL_TRANSLATABLE = "experimental_translatable"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class WolfLocation:
    """Structural source location independent of text and physical line number."""

    domain: str
    source: str
    container_kind: str | None = None
    container_id: str | int | None = None
    page_id: int | None = None
    type_id: int | None = None
    record_id: int | None = None
    command_index: int | None = None
    field: str | None = None
    text_index: int | None = None
    schema_version: int = WOLF_LOCATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if Path(self.source).is_absolute():
            raise ValueError("WOLF location source must not be absolute")
        source_parts = self.source.replace("\\", "/").split("/")
        if any(part in {"", ".", ".."} for part in source_parts):
            raise ValueError("WOLF location source must be a portable relative file path")
        if self.schema_version != WOLF_LOCATION_SCHEMA_VERSION:
            raise ValueError("unsupported WOLF location schema version")
        for name in (
            "page_id",
            "type_id",
            "record_id",
            "command_index",
            "text_index",
        ):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} must not be negative")
        if (self.container_kind is None) != (self.container_id is None):
            raise ValueError("container kind and ID must be provided together")

    @property
    def canonical_id(self) -> str:
        """Return provisional v1 ID with a fixed component order."""

        parts = [
            "wolf",
            f"v{self.schema_version}",
            _component(self.domain),
            quote(self.source, safe="/-._~"),
        ]
        if self.container_kind is not None:
            parts.append(
                f"{_component(self.container_kind)}={_component(self.container_id)}"
            )
        optional = (
            ("page", self.page_id),
            ("type", self.type_id),
            ("record", self.record_id),
            ("command", self.command_index),
            ("field", self.field),
            ("text", self.text_index),
        )
        parts.extend(
            f"{name}={_component(value)}"
            for name, value in optional
            if value is not None
        )
        return ":".join(parts)

    @property
    def logical_source(self) -> str:
        """Return a proposed native-route counterpart without changing v1 IDs."""

        source = self.source
        if source.casefold().endswith(".auto.txt"):
            source = source[: -len(".Auto.txt")]
        return source if source.casefold().startswith("data/") else f"Data/{source}"

    def to_json_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "schema_status": WOLF_LOCATION_SCHEMA_STATUS,
            "domain": self.domain,
            "source": self.source,
            "logical_source": self.logical_source,
        }
        for key in (
            "container_kind",
            "container_id",
            "page_id",
            "type_id",
            "record_id",
            "command_index",
            "field",
            "text_index",
        ):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        return payload


@dataclass(frozen=True, slots=True)
class WolfTextRecord:
    """Lossless source-representation candidate for a future TranslationEntry."""

    location: WolfLocation
    type: str
    original: str
    source_file: str
    normalized_view: str | None = None
    raw_context: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    control_codes: tuple[str, ...] = ()
    classification: WolfRecordClassification = WolfRecordClassification.UNKNOWN

    @property
    def id(self) -> str:
        return self.location.canonical_id

    def to_json_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": self.id,
            "location": self.location.to_json_dict(),
            "type": self.type,
            "classification": self.classification.value,
            "original": self.original,
            "source_file": self.source_file,
            "control_codes": list(self.control_codes),
            "raw_context": dict(self.raw_context),
            "metadata": dict(self.metadata),
        }
        if self.normalized_view is not None:
            payload["normalized_view"] = self.normalized_view
        return payload


@dataclass(frozen=True, slots=True)
class WolfTextFileInfo:
    source_file: str
    size: int
    target_type: str
    encoding: str
    encoding_confidence: str
    encoding_evidence: tuple[str, ...]
    bom: str
    newline_style: str
    final_newline: bool | None
    sections: tuple[str, ...] = ()
    record_count: int = 0
    decode_error: str | None = None

    def to_json_dict(self) -> dict[str, object]:
        return {
            "source_file": self.source_file,
            "size": self.size,
            "target_type": self.target_type,
            "encoding": self.encoding,
            "encoding_confidence": self.encoding_confidence,
            "encoding_evidence": list(self.encoding_evidence),
            "bom": self.bom,
            "newline_style": self.newline_style,
            "final_newline": self.final_newline,
            "sections": list(self.sections),
            "record_count": self.record_count,
            "decode_error": self.decode_error,
        }


@dataclass(frozen=True, slots=True)
class WolfTextIssue:
    severity: str
    code: str
    reason: str
    source_file: str | None = None
    line: int | None = None

    def to_json_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "severity": self.severity,
            "code": self.code,
            "reason": self.reason,
        }
        if self.source_file is not None:
            payload["source_file"] = self.source_file
        if self.line is not None:
            payload["line"] = self.line
        return payload


@dataclass(frozen=True, slots=True)
class WolfUnknownRecord:
    source_file: str
    line: int
    kind: str
    raw: str

    def to_json_dict(self) -> dict[str, object]:
        return {
            "source_file": self.source_file,
            "line": self.line,
            "kind": self.kind,
            "raw": self.raw,
        }


@dataclass(frozen=True, slots=True)
class WolfTextReport:
    """Deterministic read-only inspection result for one export directory."""

    source_path: str
    file_count: int
    target_type: str
    detected_encoding: str
    encoding_confidence: str
    encoding_evidence: tuple[str, ...]
    bom: str
    newline_style: str
    final_newline: str
    files: tuple[WolfTextFileInfo, ...] = ()
    sections: tuple[str, ...] = ()
    records: tuple[WolfTextRecord, ...] = ()
    issues: tuple[WolfTextIssue, ...] = ()
    unknown_records: tuple[WolfUnknownRecord, ...] = ()
    fixture_kind: str = "user_supplied"
    location_schema_version: int = WOLF_LOCATION_SCHEMA_VERSION
    location_schema_status: str = WOLF_LOCATION_SCHEMA_STATUS
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if Path(self.source_path).is_absolute():
            raise ValueError("WOLF text report source path must not be absolute")

    @property
    def record_count(self) -> int:
        return len(self.records)

    def count_type(self, record_type: str) -> int:
        return sum(record.type == record_type for record in self.records)

    def to_json_dict(self) -> dict[str, object]:
        return {
            "source_path": self.source_path,
            "file_count": self.file_count,
            "target_type": self.target_type,
            "detected_encoding": self.detected_encoding,
            "encoding_confidence": self.encoding_confidence,
            "encoding_evidence": list(self.encoding_evidence),
            "bom": self.bom,
            "newline_style": self.newline_style,
            "final_newline": self.final_newline,
            "fixture_kind": self.fixture_kind,
            "location_schema_version": self.location_schema_version,
            "location_schema_status": self.location_schema_status,
            "sections": list(self.sections),
            "record_count": self.record_count,
            "candidate_dialogues": self.count_type("dialogue"),
            "candidate_choices": self.count_type("choice"),
            "candidate_database_strings": sum(
                record.type.startswith("database_") for record in self.records
            ),
            "candidate_common_events": sum(
                record.location.domain == "common" for record in self.records
            ),
            "candidate_map_events": sum(
                record.location.domain == "map" for record in self.records
            ),
            "candidate_system_strings": self.count_type("system"),
            "parser_warnings": sum(
                issue.severity == "warning" for issue in self.issues
            ),
            "parser_errors": sum(issue.severity == "error" for issue in self.issues),
            "unknown_record_count": len(self.unknown_records),
            "files": [item.to_json_dict() for item in self.files],
            "records": [record.to_json_dict() for record in self.records],
            "issues": [issue.to_json_dict() for issue in self.issues],
            "unknown_records": [item.to_json_dict() for item in self.unknown_records],
            "notes": list(self.notes),
        }


def write_wolf_text_report(path: Path, report: WolfTextReport) -> Path:
    """Atomically create a report and never overwrite an existing path."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"WOLF text report already exists: {path}")
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


def _component(value: object) -> str:
    return quote(str(value), safe="-._~")


__all__ = [
    "WOLF_LOCATION_SCHEMA_STATUS",
    "WOLF_LOCATION_SCHEMA_VERSION",
    "WolfRecordClassification",
    "WolfLocation",
    "WolfTextFileInfo",
    "WolfTextIssue",
    "WolfTextRecord",
    "WolfTextReport",
    "WolfUnknownRecord",
    "write_wolf_text_report",
]
