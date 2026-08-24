"""Portable logical models for read-only WOLF native-format research."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from core.version import TOOL_VERSION


NATIVE_RESEARCH_SCHEMA_VERSION = 1


class EvidenceGrade(str, Enum):
    """Evidence quality used by the Phase 9 research report."""

    A = "A"  # actual sample + official Editor oracle + independent implementation
    B = "B"  # actual sample + independent implementation
    C = "C"  # multiple independent implementations
    D = "D"  # single implementation/source
    E = "E"  # hypothesis or unknown


def _portable_source(value: str) -> str:
    normalized = value.replace("\\", "/")
    if Path(normalized).is_absolute() or any(
        part in {"", ".", ".."} for part in normalized.split("/")
    ):
        raise ValueError("native source must be a portable relative path")
    return normalized


@dataclass(frozen=True, slots=True)
class NativeLocation:
    """Logical WOLF location; byte offsets are intentionally not identifiers."""

    source: str
    domain: str
    record_kind: str | None = None
    record_id: int | str | None = None
    page_id: int | None = None
    command_index: int | None = None
    field: str | None = None
    text_index: int | None = None
    byte_offset_evidence: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _portable_source(self.source))
        for value in self.byte_offset_evidence:
            if value < 0:
                raise ValueError("byte offsets must not be negative")

    def logical_components(self) -> Mapping[str, object]:
        """Return canonical candidates without including physical offsets."""

        result: dict[str, object] = {"source": self.source, "domain": self.domain}
        for name in (
            "record_kind",
            "record_id",
            "page_id",
            "command_index",
            "field",
            "text_index",
        ):
            value = getattr(self, name)
            if value is not None:
                result[name] = value
        return result

    def to_json_dict(self) -> dict[str, object]:
        result = dict(self.logical_components())
        result["byte_offset_evidence"] = list(self.byte_offset_evidence)
        return result


@dataclass(frozen=True, slots=True)
class NativeTextField:
    location: NativeLocation
    source_text_sha256: str
    source_text_length: int
    encoding_evidence: tuple[str, ...] = ()
    evidence_grade: EvidenceGrade = EvidenceGrade.E

    def to_json_dict(self) -> dict[str, object]:
        return {
            "location": self.location.to_json_dict(),
            "source_text_sha256": self.source_text_sha256,
            "source_text_length": self.source_text_length,
            "encoding_evidence": list(self.encoding_evidence),
            "evidence_grade": self.evidence_grade.value,
        }


@dataclass(frozen=True, slots=True)
class NativeRecord:
    kind: str
    logical_id: int | str | None = None
    fields: tuple[NativeTextField, ...] = ()
    children: tuple["NativeRecord", ...] = ()

    def to_json_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "logical_id": self.logical_id,
            "fields": [item.to_json_dict() for item in self.fields],
            "children": [item.to_json_dict() for item in self.children],
        }


@dataclass(frozen=True, slots=True)
class NativeDocument:
    source: str
    format_family: str
    version_marker_hex: str | None = None
    records: tuple[NativeRecord, ...] = ()
    parse_scope: str = "signature_only"
    evidence_grade: EvidenceGrade = EvidenceGrade.E

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _portable_source(self.source))

    def to_json_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "format_family": self.format_family,
            "version_marker_hex": self.version_marker_hex,
            "parse_scope": self.parse_scope,
            "evidence_grade": self.evidence_grade.value,
            "records": [item.to_json_dict() for item in self.records],
        }


@dataclass(frozen=True, slots=True)
class NativeFileProbe:
    source: str
    role: str
    size: int
    sha256: str
    header_hex: str
    entropy_bits_per_byte: float
    signature: str
    signature_status: str
    version_marker_hex: str | None
    evidence_grade: EvidenceGrade
    notes: tuple[str, ...] = ()

    def to_json_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["evidence_grade"] = self.evidence_grade.value
        payload["notes"] = list(self.notes)
        return payload


@dataclass(frozen=True, slots=True)
class NativeOracleMapping:
    native_source: str
    oracle_source: str | None
    mapping_status: str
    oracle_record_count: int = 0
    evidence_grade: EvidenceGrade = EvidenceGrade.E

    def to_json_dict(self) -> dict[str, object]:
        return {
            "native_source": self.native_source,
            "oracle_source": self.oracle_source,
            "mapping_status": self.mapping_status,
            "oracle_record_count": self.oracle_record_count,
            "evidence_grade": self.evidence_grade.value,
        }


@dataclass(frozen=True, slots=True)
class NativeStringCorrelation:
    native_source: str
    oracle_source: str
    source_text_sha256: str
    source_text_length: int
    matches: Mapping[str, tuple[int, ...]] = field(default_factory=dict)
    logical_location: Mapping[str, Any] = field(default_factory=dict)
    evidence_grade: EvidenceGrade = EvidenceGrade.E

    def to_json_dict(self) -> dict[str, object]:
        return {
            "native_source": self.native_source,
            "oracle_source": self.oracle_source,
            "source_text_sha256": self.source_text_sha256,
            "source_text_length": self.source_text_length,
            "matches": {key: list(value) for key, value in self.matches.items()},
            "logical_location": dict(self.logical_location),
            "evidence_grade": self.evidence_grade.value,
        }


@dataclass(frozen=True, slots=True)
class WolfNativeResearchReport:
    files: tuple[NativeFileProbe, ...]
    mappings: tuple[NativeOracleMapping, ...] = ()
    correlations: tuple[NativeStringCorrelation, ...] = ()
    documents: tuple[NativeDocument, ...] = ()
    issues: tuple[str, ...] = ()
    limits: Mapping[str, int] = field(default_factory=dict)
    source_path: str = "."
    oracle_path: str | None = None
    tool_version: str = TOOL_VERSION
    schema_version: int = NATIVE_RESEARCH_SCHEMA_VERSION
    mode: str = "read_only_research"

    def __post_init__(self) -> None:
        if Path(self.source_path).is_absolute():
            raise ValueError("native report source path must not be absolute")
        if self.oracle_path is not None and Path(self.oracle_path).is_absolute():
            raise ValueError("native report oracle path must not be absolute")

    def to_json_dict(self) -> dict[str, object]:
        return {
            "tool_version": self.tool_version,
            "schema_version": self.schema_version,
            "mode": self.mode,
            "source_path": self.source_path,
            "oracle_path": self.oracle_path,
            "file_count": len(self.files),
            "mapping_count": len(self.mappings),
            "correlation_count": len(self.correlations),
            "files": [item.to_json_dict() for item in self.files],
            "mappings": [item.to_json_dict() for item in self.mappings],
            "correlations": [item.to_json_dict() for item in self.correlations],
            "documents": [item.to_json_dict() for item in self.documents],
            "issues": list(self.issues),
            "limits": dict(self.limits),
            "privacy": {
                "absolute_paths_persisted": False,
                "source_text_persisted": False,
            },
        }


def write_native_research_report(
    path: Path, report: WolfNativeResearchReport
) -> Path:
    """Atomically create a portable research report without overwriting."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"native research report already exists: {path}")
    data = (json.dumps(report.to_json_dict(), ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )
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


__all__ = [
    "EvidenceGrade",
    "NATIVE_RESEARCH_SCHEMA_VERSION",
    "NativeDocument",
    "NativeFileProbe",
    "NativeLocation",
    "NativeOracleMapping",
    "NativeRecord",
    "NativeStringCorrelation",
    "NativeTextField",
    "WolfNativeResearchReport",
    "write_native_research_report",
]
