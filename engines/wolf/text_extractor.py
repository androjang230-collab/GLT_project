"""Verified-only WOLF ``.Auto.txt`` to common GLT JSONL extraction."""

from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from core.models import EngineId, ExtractionIssue, ExtractionResult, TranslationEntry
from engines.wolf.text_inspector import WolfTextInspector
from engines.wolf.text_fingerprint import calculate_wolf_source_fingerprint
from engines.wolf.text_models import (
    WOLF_LOCATION_SCHEMA_STATUS,
    WOLF_LOCATION_SCHEMA_VERSION,
    WolfRecordClassification,
    WolfTextRecord,
    WolfTextReport,
)


_NUMERIC_ONLY_RE = re.compile(r"^[+\-]?[0-9０-９]+(?:[.,][0-9０-９]+)*$")
_DOMAIN_ORDER = {"basic": 0, "common": 1, "database": 2, "map": 3}


@dataclass(frozen=True, slots=True)
class WolfExtractionReport:
    source_path: str = "."
    files_scanned: int = 0
    records_parsed: int = 0
    verified_translatable: int = 0
    experimental_excluded: int = 0
    unknown_records: int = 0
    excluded_empty_or_nontext: int = 0
    canonical_id_collisions: int = 0
    decode_issues: int = 0
    parser_warnings: int = 0
    parser_errors: int = 0
    output_entries: int = 0
    blocked: bool = False
    location_schema_version: int = WOLF_LOCATION_SCHEMA_VERSION
    location_schema_status: str = WOLF_LOCATION_SCHEMA_STATUS
    issues: tuple[dict[str, object], ...] = ()
    output_ids: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    source_fingerprint: str = ""
    source_files: tuple[dict[str, object], ...] = ()

    def __post_init__(self) -> None:
        if Path(self.source_path).is_absolute():
            raise ValueError("WOLF extraction report source path must not be absolute")

    def to_json_dict(self) -> dict[str, object]:
        return {
            "source_path": self.source_path,
            "files_scanned": self.files_scanned,
            "records_parsed": self.records_parsed,
            "verified_translatable": self.verified_translatable,
            "experimental_excluded": self.experimental_excluded,
            "unknown_records": self.unknown_records,
            "excluded_empty_or_nontext": self.excluded_empty_or_nontext,
            "canonical_id_collisions": self.canonical_id_collisions,
            "decode_issues": self.decode_issues,
            "parser_warnings": self.parser_warnings,
            "parser_errors": self.parser_errors,
            "output_entries": self.output_entries,
            "blocked": self.blocked,
            "location_schema_version": self.location_schema_version,
            "location_schema_status": self.location_schema_status,
            "issues": list(self.issues),
            "output_ids": list(self.output_ids),
            "notes": list(self.notes),
            "source_fingerprint": self.source_fingerprint,
            "source_files": list(self.source_files),
        }


@dataclass(slots=True)
class WolfExtractionResult(ExtractionResult):
    report: WolfExtractionReport = field(default_factory=WolfExtractionReport)


class WolfTextExtractor:
    """Convert only evidence-approved records; never modify source exports."""

    def inspect_and_convert(self, export_directory: Path) -> WolfExtractionResult:
        fingerprint = calculate_wolf_source_fingerprint(export_directory)
        inspection = WolfTextInspector().inspect(export_directory)
        entries: list[TranslationEntry] = []
        excluded = 0
        verified = [
            record
            for record in inspection.records
            if record.classification
            == WolfRecordClassification.VERIFIED_TRANSLATABLE
        ]
        experimental = sum(
            record.classification
            == WolfRecordClassification.EXPERIMENTAL_TRANSLATABLE
            for record in inspection.records
        )
        for record in sorted(verified, key=_record_sort_key):
            if not _has_translatable_content(record):
                excluded += 1
                continue
            entries.append(
                to_translation_entry(
                    record,
                    source_fingerprint=fingerprint.value,
                    source_file_sha256=fingerprint.file_hash(record.source_file),
                )
            )

        collision_count = sum(
            issue.code == "WOLF_CANONICAL_ID_COLLISION"
            for issue in inspection.issues
        )
        common_issues = [
            ExtractionIssue(issue.source_file or ".", f"{issue.code}: {issue.reason}")
            for issue in inspection.issues
        ]
        report = _build_report(
            inspection,
            entries,
            verified_count=len(verified),
            experimental_count=experimental,
            excluded_count=excluded,
            collision_count=collision_count,
            source_fingerprint=fingerprint.value,
            source_files=tuple(item.to_json_dict() for item in fingerprint.files),
        )
        return WolfExtractionResult(entries=entries, issues=common_issues, report=report)


def to_translation_entry(
    record: WolfTextRecord,
    *,
    source_fingerprint: str | None = None,
    source_file_sha256: str | None = None,
) -> TranslationEntry:
    """Map a verified WOLF record into the unchanged common GLT schema."""

    if record.classification != WolfRecordClassification.VERIFIED_TRANSLATABLE:
        raise ValueError("only verified WOLF records can become TranslationEntry")
    location = record.location
    target_type = (
        "MAP"
        if location.domain == "map"
        else "BASIC"
        if location.domain in {"basic", "common", "database"}
        else "unknown"
    )
    metadata: dict[str, object] = {
        "location": location.to_json_dict(),
        "wolf_domain": location.domain,
        "source_auto_txt": record.source_file,
        "target_type": target_type,
        "record_classification": record.classification.value,
        "fixture_confidence": record.metadata.get("confidence", "unknown"),
        "wolf_logical_source": location.logical_source,
    }
    if location.container_kind is not None:
        metadata["wolf_container_kind"] = location.container_kind
        metadata["wolf_container_id"] = location.container_id
    if location.type_id is not None:
        metadata["database_type"] = location.type_id
    if location.record_id is not None:
        metadata["record_id"] = location.record_id
    if location.field is not None:
        metadata["field_id"] = location.field
    if location.text_index is not None:
        metadata["text_index"] = location.text_index
    command_code = record.metadata.get("command_code")
    if command_code is not None:
        metadata["command_code"] = command_code
    command_indent = record.metadata.get("command_indent")
    if command_indent is not None:
        metadata["command_indent"] = command_indent
    option_index = record.metadata.get("option_index")
    if option_index is not None:
        metadata["option_index"] = option_index
    if source_fingerprint is not None:
        metadata["source_fingerprint"] = source_fingerprint
    if source_file_sha256 is not None:
        metadata["source_file_sha256"] = source_file_sha256

    event_id = (
        int(location.container_id)
        if location.domain == "map" and isinstance(location.container_id, int)
        else None
    )
    return TranslationEntry(
        id=record.id,
        engine=EngineId.WOLF_RPG_EDITOR,
        file=record.source_file,
        type=record.type,
        original=record.original,
        translation="",
        event_id=event_id,
        page_id=location.page_id,
        command_index=location.command_index,
        control_codes=record.control_codes,
        extra_metadata=metadata,
    )


def write_wolf_extraction_report(
    path: Path, report: WolfExtractionReport
) -> Path:
    """Atomically create a portable report without overwriting."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"WOLF extraction report already exists: {path}")
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


def _build_report(
    inspection: WolfTextReport,
    entries: list[TranslationEntry],
    *,
    verified_count: int,
    experimental_count: int,
    excluded_count: int,
    collision_count: int,
    source_fingerprint: str,
    source_files: tuple[dict[str, object], ...],
) -> WolfExtractionReport:
    return WolfExtractionReport(
        files_scanned=inspection.file_count,
        records_parsed=inspection.record_count,
        verified_translatable=verified_count,
        experimental_excluded=experimental_count,
        unknown_records=len(inspection.unknown_records),
        excluded_empty_or_nontext=excluded_count,
        canonical_id_collisions=collision_count,
        decode_issues=sum(
            issue.code in {"TEXT_DECODE_FAILED", "TEXT_ENCODING_AMBIGUOUS"}
            for issue in inspection.issues
        ),
        parser_warnings=sum(
            issue.severity == "warning" for issue in inspection.issues
        ),
        parser_errors=sum(issue.severity == "error" for issue in inspection.issues),
        output_entries=len(entries),
        blocked=collision_count > 0,
        issues=tuple(issue.to_json_dict() for issue in inspection.issues),
        output_ids=tuple(entry.id for entry in entries),
        notes=(
            "Only VERIFIED_TRANSLATABLE records are included.",
            "WOLF command 102 option literals are verified from a local official Editor 3.682 export; label-only message candidates remain experimental.",
            "WOLF canonical location schema v1 remains provisional.",
        ),
        source_fingerprint=source_fingerprint,
        source_files=source_files,
    )


def _record_sort_key(record: WolfTextRecord) -> tuple[object, ...]:
    location = record.location
    return (
        _DOMAIN_ORDER.get(location.domain, 99),
        location.source.casefold(),
        location.container_kind or "",
        _sortable(location.container_id),
        _sortable(location.page_id),
        _sortable(location.type_id),
        _sortable(location.record_id),
        _sortable(location.command_index),
        location.field or "",
        _sortable(location.text_index),
    )


def _sortable(value: object | None) -> tuple[int, object]:
    if value is None:
        return (0, 0)
    if isinstance(value, int):
        return (1, value)
    return (2, str(value))


def _has_translatable_content(record: WolfTextRecord) -> bool:
    text = record.original.strip()
    if not text or _NUMERIC_ONLY_RE.fullmatch(text):
        return False
    visible = text
    for token in record.control_codes:
        visible = visible.replace(token, "")
    visible = visible.replace(r"\n", "").strip()
    return any(unicodedata.category(character)[0] in {"L", "N"} for character in visible)


__all__ = [
    "WolfExtractionReport",
    "WolfExtractionResult",
    "WolfTextExtractor",
    "to_translation_entry",
    "write_wolf_extraction_report",
]
