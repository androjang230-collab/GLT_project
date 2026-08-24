"""WOLF-specific QA and shared writer preflight for official text exports."""

from __future__ import annotations

import json
import os
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from core.version import TOOL_VERSION
from engines.wolf.text_extractor import WolfExtractionResult, WolfTextExtractor
from engines.wolf.text_fingerprint import (
    WolfSourceFingerprint,
    calculate_wolf_source_fingerprint,
)
from engines.wolf.text_inspector import WolfTextInspector, detect_control_like_tokens
from engines.wolf.text_models import WolfLocation, WolfTextRecord


_SEVERITIES = frozenset({"warning", "error", "blocker"})
_VARIABLE_PREFIXES = (
    r"\v[",
    r"\variable[",
    r"\s[",
    r"\self[",
    r"\cself[",
    r"\udb[",
    r"\cdb[",
    r"\sdb[",
    r"\sys[",
    r"\syss[",
)


@dataclass(frozen=True, slots=True)
class WolfQaIssue:
    severity: str
    issue_code: str
    reason: str
    id: str = ""
    file: str = ""
    location: Mapping[str, object] = field(default_factory=dict)
    type: str = ""
    original: str = ""
    translation: str = ""

    def __post_init__(self) -> None:
        if self.severity not in _SEVERITIES:
            raise ValueError(f"unsupported WOLF QA severity: {self.severity}")
        if self.file and Path(self.file).is_absolute():
            raise ValueError("WOLF QA issue paths must be portable")

    def to_json_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "file": self.file,
            "location": dict(self.location),
            "type": self.type,
            "original": self.original,
            "translation": self.translation,
            "severity": self.severity,
            "issue_code": self.issue_code,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class WolfTranslationRow:
    line: int
    id: str
    engine: str
    file: str
    type: str
    original: str
    translation: str
    location: Mapping[str, object]
    source_fingerprint: str | None
    source_file_sha256: str | None
    raw: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class WolfPlannedChange:
    row: WolfTranslationRow
    record: WolfTextRecord

    @property
    def id(self) -> str:
        return self.row.id

    @property
    def file(self) -> str:
        return self.row.file


@dataclass(frozen=True, slots=True)
class WolfQaReport:
    tool_version: str
    source_fingerprint: str
    source_files: tuple[dict[str, object], ...]
    total_entries: int
    translated_entries: int
    untranslated_entries: int
    applicable_entries: int
    warning_count: int
    error_count: int
    blocker_count: int
    files_to_modify: tuple[str, ...]
    files_untouched: tuple[str, ...]
    issues: tuple[WolfQaIssue, ...]
    canonical_schema_version: int = 1
    canonical_schema_status: str = "provisional"

    @property
    def blocked(self) -> bool:
        return self.blocker_count > 0

    @property
    def translation_percentage(self) -> float:
        return (
            self.translated_entries / self.total_entries * 100.0
            if self.total_entries
            else 0.0
        )

    def to_json_dict(self) -> dict[str, object]:
        return {
            "tool_version": self.tool_version,
            "source_fingerprint": self.source_fingerprint,
            "source_files": list(self.source_files),
            "total_entries": self.total_entries,
            "translated_entries": self.translated_entries,
            "untranslated_entries": self.untranslated_entries,
            "translation_percentage": round(self.translation_percentage, 2),
            "applicable_entries": self.applicable_entries,
            "warnings": self.warning_count,
            "errors": self.error_count,
            "blockers": self.blocker_count,
            "files_to_modify": list(self.files_to_modify),
            "files_untouched": list(self.files_untouched),
            "canonical_schema_version": self.canonical_schema_version,
            "canonical_schema_status": self.canonical_schema_status,
            "issues": [item.to_json_dict() for item in self.issues],
        }


@dataclass(frozen=True, slots=True)
class WolfQaResult:
    report: WolfQaReport
    changes: tuple[WolfPlannedChange, ...]
    extraction: WolfExtractionResult
    fingerprint: WolfSourceFingerprint


class WolfTextQa:
    """Validate common JSONL against verified records in a current export."""

    def validate(self, source: Path, translation_jsonl: Path) -> WolfQaResult:
        source = source.resolve()
        translation_jsonl = translation_jsonl.resolve()
        if translation_jsonl == source or translation_jsonl.is_relative_to(source):
            raise ValueError("translation JSONL cannot be inside the WOLF source directory")
        before = calculate_wolf_source_fingerprint(source)
        extraction = WolfTextExtractor().inspect_and_convert(source)
        rows, load_issues = _load_rows(translation_jsonl)
        issues: list[WolfQaIssue] = list(load_issues)

        for parser_issue in extraction.report.issues:
            severity = "blocker" if parser_issue.get("severity") == "error" else "warning"
            issues.append(
                WolfQaIssue(
                    severity,
                    str(parser_issue.get("code", "SOURCE_PARSE_ISSUE")),
                    str(parser_issue.get("reason", "source export inspection issue")),
                    file=str(parser_issue.get("source_file", "")),
                )
            )
        if extraction.report.canonical_id_collisions:
            issues.append(
                WolfQaIssue(
                    "blocker",
                    "SOURCE_ID_COLLISION",
                    "source export contains duplicate canonical IDs",
                )
            )

        # ExtractionResult intentionally exposes common entries only; inspection is
        # repeated here to retain lossless raw anchors for the writer.
        inspection = WolfTextInspector().inspect(source)
        record_map = {
            record.id: record
            for record in inspection.records
            if record.id in set(extraction.report.output_ids)
        }

        duplicate_ids = {
            item.id for item in rows if sum(row.id == item.id for row in rows) > 1
        }
        for duplicate in sorted(duplicate_ids):
            row = next(item for item in rows if item.id == duplicate)
            issues.append(_row_issue(row, "blocker", "DUPLICATE_ID", "ID is duplicated"))

        changes: list[WolfPlannedChange] = []
        for row in rows:
            row_issue_start = len(issues)
            if not row.id or not row.file or not row.type:
                issues.append(
                    _row_issue(
                        row,
                        "blocker",
                        "MALFORMED_TRANSLATION_ENTRY",
                        "id, file, and type must be non-empty strings",
                    )
                )
            if not _is_portable_member(row.file):
                issues.append(
                    _row_issue(
                        row,
                        "blocker",
                        "UNSAFE_FILE_PATH",
                        "file must be a portable relative path without parent traversal",
                    )
                )
            location = _parse_location(row, issues)
            if location is not None and location.canonical_id != row.id:
                issues.append(
                    _row_issue(
                        row,
                        "blocker",
                        "CANONICAL_ID_MISMATCH",
                        "ID does not match the structural location metadata",
                    )
                )
            record = record_map.get(row.id)
            if record is None:
                issues.append(
                    _row_issue(
                        row,
                        "blocker",
                        "UNKNOWN_LOCATION",
                        "canonical ID does not resolve to a verified source record",
                    )
                )
                continue
            if row.engine != "wolf_rpg_editor":
                issues.append(_row_issue(row, "blocker", "ENGINE_MISMATCH", "engine must be wolf_rpg_editor"))
            if row.file != record.source_file:
                issues.append(_row_issue(row, "blocker", "FILE_MISMATCH", "file does not match the resolved source record"))
            if row.type != record.type:
                issues.append(_row_issue(row, "blocker", "TYPE_MISMATCH", "type does not match the resolved source record"))
            if location is not None and location.to_json_dict() != record.location.to_json_dict():
                issues.append(_row_issue(row, "blocker", "LOCATION_MISMATCH", "location metadata differs from the current source"))
            if row.original != record.original:
                issues.append(_row_issue(row, "blocker", "SOURCE_TEXT_MISMATCH", "original does not exactly match the current source"))
            if row.source_fingerprint is None:
                issues.append(_row_issue(row, "warning", "SOURCE_FINGERPRINT_MISSING", "legacy JSONL has no source fingerprint"))
            elif row.source_fingerprint != before.value:
                issues.append(_row_issue(row, "blocker", "SOURCE_FINGERPRINT_MISMATCH", "source directory fingerprint changed since extraction"))
            current_file_hash = before.file_hash(record.source_file)
            if row.source_file_sha256 is not None and row.source_file_sha256 != current_file_hash:
                issues.append(_row_issue(row, "blocker", "SOURCE_FILE_FINGERPRINT_MISMATCH", "source file hash changed since extraction"))
            if not row.translation.strip():
                issues.append(_row_issue(row, "warning", "EMPTY_TRANSLATION", "empty or whitespace-only translation is skipped"))
            else:
                _validate_tokens(row, record, issues)

            new_issues = issues[row_issue_start:]
            if (
                row.id not in duplicate_ids
                and row.translation.strip()
                and not any(item.severity in {"error", "blocker"} for item in new_issues)
            ):
                changes.append(WolfPlannedChange(row, record))

        after = calculate_wolf_source_fingerprint(source)
        if after.value != before.value:
            issues.append(WolfQaIssue("blocker", "SOURCE_CHANGED_DURING_QA", "source directory changed during QA"))
            changes.clear()

        all_files = tuple(item.path for item in before.files)
        files_to_modify = tuple(sorted({item.file for item in changes}, key=str.casefold))
        files_untouched = tuple(item for item in all_files if item not in set(files_to_modify))
        translated = sum(bool(row.translation.strip()) for row in rows)
        report = WolfQaReport(
            tool_version=TOOL_VERSION,
            source_fingerprint=before.value,
            source_files=tuple(item.to_json_dict() for item in before.files),
            total_entries=len(rows),
            translated_entries=translated,
            untranslated_entries=len(rows) - translated,
            applicable_entries=len(changes),
            warning_count=sum(item.severity == "warning" for item in issues),
            error_count=sum(item.severity == "error" for item in issues),
            blocker_count=sum(item.severity == "blocker" for item in issues),
            files_to_modify=files_to_modify,
            files_untouched=files_untouched,
            issues=tuple(issues),
        )
        return WolfQaResult(report, tuple(changes), extraction, before)


def write_wolf_qa_report(path: Path, report: WolfQaReport) -> Path:
    return _atomic_new_json(path, report.to_json_dict())


def _load_rows(path: Path) -> tuple[list[WolfTranslationRow], list[WolfQaIssue]]:
    if not path.is_file():
        raise FileNotFoundError(f"translation JSONL does not exist: {path}")
    rows: list[WolfTranslationRow] = []
    issues: list[WolfQaIssue] = []
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                issues.append(WolfQaIssue("blocker", "MALFORMED_JSONL", f"line {line_number}, column {exc.colno}: {exc.msg}"))
                continue
            if not isinstance(value, dict):
                issues.append(WolfQaIssue("blocker", "MALFORMED_TRANSLATION_ENTRY", f"line {line_number} is not a JSON object"))
                continue
            try:
                required = {name: value[name] for name in ("id", "engine", "file", "type", "original", "translation")}
            except KeyError as exc:
                issues.append(WolfQaIssue("blocker", "MALFORMED_TRANSLATION_ENTRY", f"line {line_number} is missing {exc.args[0]}"))
                continue
            if not all(isinstance(item, str) for item in required.values()):
                issues.append(WolfQaIssue("blocker", "MALFORMED_TRANSLATION_ENTRY", f"line {line_number} required fields must be strings"))
                continue
            location = value.get("location", {})
            if not isinstance(location, dict):
                location = {}
            rows.append(
                WolfTranslationRow(
                    line_number,
                    required["id"], required["engine"], required["file"],
                    required["type"], required["original"], required["translation"],
                    location,
                    value.get("source_fingerprint") if isinstance(value.get("source_fingerprint"), str) else None,
                    value.get("source_file_sha256") if isinstance(value.get("source_file_sha256"), str) else None,
                    value,
                )
            )
    return rows, issues


def _parse_location(row: WolfTranslationRow, issues: list[WolfQaIssue]) -> WolfLocation | None:
    location = dict(row.location)
    allowed = {
        "schema_version", "domain", "source", "container_kind", "container_id",
        "page_id", "type_id", "record_id", "command_index", "field", "text_index",
    }
    try:
        kwargs = {key: value for key, value in location.items() if key in allowed}
        if not {"domain", "source"}.issubset(kwargs):
            raise ValueError("domain and source are required")
        parsed = WolfLocation(**kwargs)
    except (TypeError, ValueError) as exc:
        issues.append(_row_issue(row, "blocker", "MALFORMED_STRUCTURAL_METADATA", str(exc)))
        return None
    return parsed


def _validate_tokens(row: WolfTranslationRow, record: WolfTextRecord, issues: list[WolfQaIssue]) -> None:
    original = tuple(token for token in record.control_codes if token != "<<COMMA>>")
    translated = tuple(token for token in detect_control_like_tokens(row.translation) if token != "<<COMMA>>")
    if original == translated:
        return
    original_variables = tuple(token for token in original if _is_variable(token))
    translated_variables = tuple(token for token in translated if _is_variable(token))
    if original_variables != translated_variables:
        issues.append(_row_issue(row, "error", "VARIABLE_REFERENCE_MISMATCH", "variable-reference type, order, multiplicity, or parameters changed"))
    elif Counter(original) == Counter(translated):
        issues.append(_row_issue(row, "error", "CONTROL_TOKEN_ORDER_MISMATCH", "control-like token order changed"))
    else:
        issues.append(_row_issue(row, "error", "CONTROL_TOKEN_MISMATCH", "control-like token type, multiplicity, or parameters changed"))


def _is_variable(token: str) -> bool:
    folded = token.casefold()
    return folded.startswith(_VARIABLE_PREFIXES)


def _row_issue(row: WolfTranslationRow, severity: str, code: str, reason: str) -> WolfQaIssue:
    safe_file = row.file if _is_portable_member(row.file) else ""
    safe_location = (
        row.location
        if _is_portable_member(str(row.location.get("source", "")))
        else {}
    )
    return WolfQaIssue(severity, code, reason, row.id, safe_file, safe_location, row.type, row.original, row.translation)


def _is_portable_member(value: str) -> bool:
    if not value or Path(value).is_absolute():
        return False
    normalized = value.replace("\\", "/")
    return not any(part in {"", ".", ".."} for part in normalized.split("/"))


def _atomic_new_json(path: Path, payload: Mapping[str, Any]) -> Path:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"report already exists: {path}")
    data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", delete=False, dir=path.parent, prefix=f".{path.name}.", suffix=".tmp") as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        temporary.unlink()
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


__all__ = [
    "WolfPlannedChange", "WolfQaIssue", "WolfQaReport", "WolfQaResult",
    "WolfTextQa", "WolfTranslationRow", "write_wolf_qa_report",
]
