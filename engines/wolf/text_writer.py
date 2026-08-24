"""Lossless, source-oriented writer for WOLF ``.Auto.txt`` copies."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from core.version import TOOL_VERSION
from engines.wolf.text_fingerprint import calculate_wolf_source_fingerprint
from engines.wolf.text_inspector import WolfTextInspector, decode_auto_text
from engines.wolf.text_qa import WolfQaIssue, WolfQaResult, WolfTextQa


_BOMS = {
    "none": b"",
    "utf-8": b"\xef\xbb\xbf",
    "utf-16-le": b"\xff\xfe",
    "utf-16-be": b"\xfe\xff",
}


@dataclass(frozen=True, slots=True)
class WolfWriteReport:
    tool_version: str
    dry_run: bool
    source_fingerprint: str
    output_fingerprint: str
    source_files: tuple[dict[str, object], ...]
    output_files: tuple[dict[str, object], ...]
    total_entries: int
    translated_entries: int
    untranslated_entries: int
    applicable_entries: int
    applied_entries: int
    skipped_untranslated: int
    skipped_entries: int
    modified_files: tuple[str, ...]
    untouched_files: tuple[str, ...]
    warnings: int
    errors: int
    blockers: int
    issues: tuple[WolfQaIssue, ...]

    @property
    def blocked(self) -> bool:
        return self.errors > 0 or self.blockers > 0

    def to_json_dict(self) -> dict[str, object]:
        return {
            "tool_version": self.tool_version,
            "operation": "wolf_auto_txt_write",
            "dry_run": self.dry_run,
            "source_fingerprint": self.source_fingerprint,
            "output_fingerprint": self.output_fingerprint,
            "source_files": list(self.source_files),
            "output_files": list(self.output_files),
            "total_entries": self.total_entries,
            "translated_entries": self.translated_entries,
            "untranslated_entries": self.untranslated_entries,
            "applicable_entries": self.applicable_entries,
            "applied_entries": self.applied_entries,
            "skipped_untranslated": self.skipped_untranslated,
            "skipped_entries": self.skipped_entries,
            "modified_files": list(self.modified_files),
            "untouched_files": list(self.untouched_files),
            "warnings": self.warnings,
            "errors": self.errors,
            "blockers": self.blockers,
            "blocked": self.blocked,
            "issues": [item.to_json_dict() for item in self.issues],
        }


class WolfTextWriter:
    """Create a translated export directory without touching the source tree."""

    def apply(
        self,
        source: Path,
        translation_jsonl: Path,
        output: Path,
        *,
        dry_run: bool = False,
    ) -> WolfWriteReport:
        source = source.resolve()
        translation_jsonl = translation_jsonl.resolve()
        output = output.resolve()
        if output == source or output.is_relative_to(source):
            raise ValueError("WOLF output cannot be the source or inside the source")
        if source.is_relative_to(output):
            raise ValueError("WOLF output cannot contain the source directory")
        if output.exists():
            raise FileExistsError(f"WOLF output directory already exists: {output}")
        if translation_jsonl == source or translation_jsonl.is_relative_to(source):
            raise ValueError("translation JSONL cannot be inside the WOLF source directory")

        qa = WolfTextQa().validate(source, translation_jsonl)
        patched, patch_issues = _prepare_patched_files(source, qa)
        issues = list((*qa.report.issues, *patch_issues))
        all_files = tuple(item.path for item in qa.fingerprint.files)
        modified_files = tuple(
            sorted(
                (
                    name
                    for name, data in patched.items()
                    if data != (source / Path(name)).read_bytes()
                ),
                key=str.casefold,
            )
        )
        untouched_files = tuple(name for name in all_files if name not in set(modified_files))
        output_fingerprint = ""
        output_files: tuple[dict[str, object], ...] = ()
        applied = 0
        can_stage = not any(item.severity in {"error", "blocker"} for item in issues)
        if can_stage:
            if not dry_run:
                output.parent.mkdir(parents=True, exist_ok=True)
                staging_parent = output.parent
            else:
                staging_parent = Path(tempfile.gettempdir())
            staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=staging_parent))
            try:
                shutil.copytree(source, staging, dirs_exist_ok=True, copy_function=shutil.copy2)
                for portable_name, data in patched.items():
                    destination = staging / Path(portable_name)
                    _atomic_replace_bytes(destination, data)
                issues.extend(_validate_round_trip(source, staging, qa))
                staged_fingerprint = calculate_wolf_source_fingerprint(staging)
                if calculate_wolf_source_fingerprint(source).value != qa.fingerprint.value:
                    issues.append(
                        WolfQaIssue(
                            "blocker",
                            "SOURCE_CHANGED_DURING_WRITE",
                            "source changed after preflight; output was not created",
                        )
                    )
                output_fingerprint = staged_fingerprint.value
                output_files = tuple(item.to_json_dict() for item in staged_fingerprint.files)
                if not dry_run and not any(
                    item.severity in {"error", "blocker"} for item in issues
                ):
                    os.replace(staging, output)
                    applied = len(qa.changes)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)

        errors = sum(item.severity == "error" for item in issues)
        blockers = sum(item.severity == "blocker" for item in issues)
        warnings = sum(item.severity == "warning" for item in issues)

        return WolfWriteReport(
            tool_version=TOOL_VERSION,
            dry_run=dry_run,
            source_fingerprint=qa.fingerprint.value,
            output_fingerprint=output_fingerprint,
            source_files=tuple(item.to_json_dict() for item in qa.fingerprint.files),
            output_files=output_files,
            total_entries=qa.report.total_entries,
            translated_entries=qa.report.translated_entries,
            untranslated_entries=qa.report.untranslated_entries,
            applicable_entries=len(qa.changes),
            applied_entries=applied,
            skipped_untranslated=qa.report.untranslated_entries,
            skipped_entries=qa.report.total_entries - (len(qa.changes) if dry_run else applied),
            modified_files=modified_files,
            untouched_files=untouched_files,
            warnings=warnings,
            errors=errors,
            blockers=blockers,
            issues=tuple(issues),
        )


def _validate_round_trip(
    source: Path, staged: Path, qa: WolfQaResult
) -> list[WolfQaIssue]:
    """Reparse staged bytes and prove only approved record values changed."""

    issues: list[WolfQaIssue] = []
    inspector = WolfTextInspector()
    original_report = inspector.inspect(source)
    staged_report = inspector.inspect(staged)
    if any(item.severity == "error" for item in staged_report.issues):
        issues.append(
            WolfQaIssue(
                "blocker",
                "ROUND_TRIP_PARSE_FAILED",
                "translated output did not pass the WOLF text inspector",
            )
        )
        return issues

    expected = {
        change.id: _expected_inspected_text(change)
        for change in qa.changes
    }
    original_records = {item.id: item for item in original_report.records}
    staged_records = {item.id: item for item in staged_report.records}
    if tuple(sorted(original_records)) != tuple(sorted(staged_records)):
        issues.append(
            WolfQaIssue(
                "blocker",
                "ROUND_TRIP_STRUCTURE_MISMATCH",
                "record IDs changed after source-oriented patching",
            )
        )
        return issues
    for record_id, original in original_records.items():
        actual = staged_records[record_id]
        intended = expected.get(record_id, original.original)
        if actual.original != intended:
            issues.append(
                WolfQaIssue(
                    "blocker",
                    "ROUND_TRIP_VALUE_MISMATCH",
                    "reparsed output value differs from the intended translation",
                    id=record_id,
                    file=original.source_file,
                    location=original.location.to_json_dict(),
                    type=original.type,
                    original=original.original,
                    translation=intended,
                )
            )
    if tuple(item.to_json_dict() for item in original_report.unknown_records) != tuple(
        item.to_json_dict() for item in staged_report.unknown_records
    ):
        issues.append(
            WolfQaIssue(
                "blocker",
                "ROUND_TRIP_UNKNOWN_RECORD_MISMATCH",
                "unknown or unsupported records changed during writing",
            )
        )
    original_transport = {
        item.source_file: (
            item.encoding,
            item.bom,
            item.newline_style,
            item.final_newline,
        )
        for item in original_report.files
    }
    staged_transport = {
        item.source_file: (
            item.encoding,
            item.bom,
            item.newline_style,
            item.final_newline,
        )
        for item in staged_report.files
    }
    if original_transport != staged_transport:
        issues.append(
            WolfQaIssue(
                "blocker",
                "ROUND_TRIP_TRANSPORT_MISMATCH",
                "encoding, BOM, newline style, or final-newline metadata changed",
            )
        )
    return issues


def _expected_inspected_text(change: object) -> str:
    record = change.record
    translation = change.row.translation
    if record.location.domain in {"map", "common"}:
        return _escape_raw_literal(translation)
    if record.location.domain == "database" and record.location.field == "dataname":
        return translation.replace(",", "<<COMMA>>")
    return translation


def write_wolf_apply_report(path: Path, report: WolfWriteReport) -> Path:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"report already exists: {path}")
    data = (json.dumps(report.to_json_dict(), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
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


def _prepare_patched_files(source: Path, qa: WolfQaResult) -> tuple[dict[str, bytes], list[WolfQaIssue]]:
    issues: list[WolfQaIssue] = []
    changes_by_file: dict[str, list[object]] = {}
    for change in qa.changes:
        changes_by_file.setdefault(change.file, []).append(change)
    patched: dict[str, bytes] = {}
    for portable_name, changes in changes_by_file.items():
        path = source / Path(portable_name)
        data = path.read_bytes()
        decoded = decode_auto_text(data)
        if decoded.text is None or decoded.encoding_confidence == "ambiguous":
            issues.append(WolfQaIssue("blocker", "ENCODING_NOT_WRITABLE", "encoding is ambiguous or unknown", file=portable_name))
            continue
        lines = decoded.text.splitlines(keepends=True)
        initial_bodies = [_line_body(item) for item in lines]
        valid = True
        for change in changes:
            record = change.record
            line_number = record.raw_context.get("line")
            if not isinstance(line_number, int) or line_number < 1 or line_number > len(lines):
                issues.append(_change_issue(change, "blocker", "SOURCE_ANCHOR_MISMATCH", "physical source line is invalid"))
                valid = False
                continue
            expected = record.raw_context.get("raw_command", record.raw_context.get("raw"))
            if not isinstance(expected, str) or initial_bodies[line_number - 1] != expected:
                issues.append(_change_issue(change, "blocker", "SOURCE_ANCHOR_MISMATCH", "source-oriented raw line anchor changed"))
                valid = False
                continue
            try:
                body, ending = _split_line(lines[line_number - 1])
                replacement = _patch_line(body, change)
                lines[line_number - 1] = replacement + ending
            except ValueError as exc:
                issues.append(_change_issue(change, "blocker", "UNSAFE_TRANSLATION_SYNTAX", str(exc)))
                valid = False
        if not valid:
            continue
        text = "".join(lines)
        try:
            payload = text.encode(decoded.encoding, errors="strict")
        except UnicodeEncodeError as exc:
            issues.append(WolfQaIssue("blocker", "TRANSLATION_ENCODING_FAILED", f"translation cannot be encoded as {decoded.encoding} at character {exc.start}", file=portable_name))
            continue
        patched[portable_name] = _BOMS[decoded.bom] + payload
    return patched, issues


def _patch_line(line: str, change: object) -> str:
    record = change.record
    translation = change.row.translation
    if "\r" in translation or "\n" in translation:
        raise ValueError("physical newline characters are not allowed; preserve source \\n representation")
    if record.metadata.get("format") == "key_value":
        prefix = line.partition("=")[0] + "="
        return prefix + translation
    if record.location.domain in {"map", "common"}:
        index = record.location.text_index
        if index is None:
            raise ValueError("event text index is missing")
        spans = _raw_literal_spans(line)
        if index >= len(spans):
            raise ValueError("event string literal index is out of range")
        start, end = spans[index]
        return line[:start] + _escape_raw_literal(translation) + line[end:]
    if record.location.domain == "database" and record.location.field == "dataname":
        field_index = record.raw_context.get("csv_field_index")
        if not isinstance(field_index, int):
            raise ValueError("database CSV field index is missing")
        spans = _csv_field_spans(line)
        if field_index >= len(spans):
            raise ValueError("database CSV field index is out of range")
        start, end, quoted = spans[field_index]
        value = "<<!--DATANAME--!>>" + translation.replace(",", "<<COMMA>>")
        encoded = '"' + value.replace('"', '""') + '"' if quoted or '"' in value else value
        return line[:start] + encoded + line[end:]
    raise ValueError("record kind is not in the verified writer allowlist")


def _raw_literal_spans(line: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(line):
        if line[index] != '"':
            index += 1
            continue
        start = index + 1
        index = start
        while index < len(line):
            if line[index] == "\\" and index + 1 < len(line):
                index += 2
            elif line[index] == '"':
                spans.append((start, index))
                index += 1
                break
            else:
                index += 1
        else:
            raise ValueError("unterminated source string literal")
    return spans


def _escape_raw_literal(text: str) -> str:
    output: list[str] = []
    slash_run = 0
    for character in text:
        if character == "\\":
            output.append(character)
            slash_run += 1
            continue
        if character == '"' and slash_run % 2 == 0:
            output.append("\\")
        output.append(character)
        slash_run = 0
    return "".join(output)


def _csv_field_spans(line: str) -> list[tuple[int, int, bool]]:
    spans: list[tuple[int, int, bool]] = []
    index = 0
    while True:
        start = index
        quoted = index < len(line) and line[index] == '"'
        if quoted:
            index += 1
            while index < len(line):
                if line[index] == '"' and index + 1 < len(line) and line[index + 1] == '"':
                    index += 2
                elif line[index] == '"':
                    index += 1
                    break
                else:
                    index += 1
            else:
                raise ValueError("unterminated quoted CSV field")
            if index < len(line) and line[index] != ",":
                raise ValueError("unexpected data after quoted CSV field")
        else:
            while index < len(line) and line[index] != ",":
                index += 1
        spans.append((start, index, quoted))
        if index >= len(line):
            break
        index += 1
        if index == len(line):
            spans.append((index, index, False))
            break
    return spans


def _line_body(value: str) -> str:
    return _split_line(value)[0]


def _split_line(value: str) -> tuple[str, str]:
    if value.endswith("\r\n"):
        return value[:-2], "\r\n"
    if value.endswith(("\r", "\n")):
        return value[:-1], value[-1]
    return value, ""


def _change_issue(change: object, severity: str, code: str, reason: str) -> WolfQaIssue:
    row = change.row
    return WolfQaIssue(severity, code, reason, row.id, row.file, row.location, row.type, row.original, row.translation)


def _atomic_replace_bytes(path: Path, data: bytes) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", delete=False, dir=path.parent, prefix=f".{path.name}.", suffix=".tmp") as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


__all__ = ["WolfTextWriter", "WolfWriteReport", "write_wolf_apply_report"]
