"""Conservative read-only prototype for WOLF ``.Auto.txt`` exports."""

from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass
from pathlib import Path

from core.paths import portable_relative_path
from engines.wolf.text_models import (
    WolfRecordClassification,
    WolfLocation,
    WolfTextFileInfo,
    WolfTextIssue,
    WolfTextRecord,
    WolfTextReport,
    WolfUnknownRecord,
)


MAX_TEXT_EXPORT_FILES = 50_000
MAX_TEXT_EXPORT_FILE_BYTES = 128 * 1024 * 1024
MAX_UNKNOWN_RECORDS = 1_000

_SECTION_RE = re.compile(r"^\[([A-Z0-9_ -]+)\]$")
_KEY_VALUE_RE = re.compile(r"^([A-Z0-9_\[\]-]+)=(.*)$")
_RAW_COMMAND_RE = re.compile(r"^\[(\d+)\]")
_CONTROL_TOKEN_RE = re.compile(
    r"\\[A-Za-z]+(?:\[[^\]\r\n]*\])?|<<[^<>\r\n]+>>"
)
_DATABASE_NAME_MARKER = "<<!--DATANAME--!>>"
_DATABASE_DATA_TYPE_RE = re.compile(r"^DATATYPE_(\d+)$")

_SYSTEM_TEXT_FIELDS = {
    "GAME_TITLE_MAIN",
    "GAME_TITLE_PLUS",
    "START_TITLE_BAR_TEXT",
    "PLAYING_TITLE_BAR_TEXT",
}

# Only the explicit official-export DATANAME marker is sufficiently evidenced.
# DATATYPE_* >= 2000 cells remain experimental until field semantics are proven.
VERIFIED_DATABASE_TEXT_FIELDS = frozenset({"dataname"})


@dataclass(frozen=True, slots=True)
class _DecodedFile:
    text: str | None
    encoding: str
    encoding_confidence: str
    encoding_evidence: tuple[str, ...]
    bom: str
    newline_style: str
    final_newline: bool | None
    error: str | None = None


class WolfTextInspector:
    """Inspect official-export candidates without writing or normalizing them."""

    def inspect(
        self,
        export_directory: Path,
        *,
        fixture_kind: str = "user_supplied",
    ) -> WolfTextReport:
        if not export_directory.is_dir():
            raise NotADirectoryError(
                f"WOLF text export directory does not exist: {export_directory}"
            )

        paths, discovery_issues = _discover_auto_text_files(export_directory)
        file_infos: list[WolfTextFileInfo] = []
        records: list[WolfTextRecord] = []
        issues: list[WolfTextIssue] = list(discovery_issues)
        unknown_records: list[WolfUnknownRecord] = []
        all_sections: list[str] = []

        for path in paths:
            source_file = portable_relative_path(path, export_directory)
            target_type = _target_type(source_file)
            try:
                size = path.stat().st_size
            except OSError as exc:
                issues.append(
                    WolfTextIssue(
                        severity="error",
                        code="TEXT_FILE_STAT_FAILED",
                        reason=str(exc),
                        source_file=source_file,
                    )
                )
                continue
            if size > MAX_TEXT_EXPORT_FILE_BYTES:
                issues.append(
                    WolfTextIssue(
                        severity="error",
                        code="TEXT_FILE_TOO_LARGE",
                        reason=(
                            "file exceeds the read-only prototype limit of "
                            f"{MAX_TEXT_EXPORT_FILE_BYTES} bytes"
                        ),
                        source_file=source_file,
                    )
                )
                file_infos.append(
                    WolfTextFileInfo(
                        source_file=source_file,
                        size=size,
                        target_type=target_type,
                        encoding="unknown",
                        encoding_confidence="none",
                        encoding_evidence=("file exceeded inspection limit",),
                        bom="unknown",
                        newline_style="unknown",
                        final_newline=None,
                        decode_error="file exceeds inspection limit",
                    )
                )
                continue

            try:
                data = path.read_bytes()
            except OSError as exc:
                decoded = _DecodedFile(
                    None,
                    "unknown",
                    "none",
                    ("file read failed",),
                    "unknown",
                    "unknown",
                    None,
                    str(exc),
                )
            else:
                decoded = decode_auto_text(data)

            if decoded.text is None:
                issues.append(
                    WolfTextIssue(
                        severity="error",
                        code=(
                            "TEXT_ENCODING_AMBIGUOUS"
                            if decoded.encoding_confidence == "ambiguous"
                            else "TEXT_DECODE_FAILED"
                        ),
                        reason=decoded.error or "encoding could not be determined",
                        source_file=source_file,
                    )
                )
                file_infos.append(
                    WolfTextFileInfo(
                        source_file=source_file,
                        size=size,
                        target_type=target_type,
                        encoding=decoded.encoding,
                        encoding_confidence=decoded.encoding_confidence,
                        encoding_evidence=decoded.encoding_evidence,
                        bom=decoded.bom,
                        newline_style=decoded.newline_style,
                        final_newline=decoded.final_newline,
                        decode_error=decoded.error,
                    )
                )
                continue

            lines = decoded.text.splitlines()
            sections = tuple(
                match.group(1)
                for line in lines
                if (match := _SECTION_RE.fullmatch(line)) is not None
            )
            all_sections.extend(f"{source_file}#{section}" for section in sections)
            before = len(records)
            if not decoded.text:
                issues.append(
                    WolfTextIssue(
                        severity="warning",
                        code="EMPTY_TEXT_EXPORT",
                        reason="empty .Auto.txt file",
                        source_file=source_file,
                    )
                )
            elif "GAMESETTING_TEXT_OUTPUT" in sections or (
                "GAMESETTING_PRO_TEXT_OUTPUT" in sections
            ):
                _parse_game_settings(lines, source_file, records)
            elif "MAPDATA_TEXT_OUTPUT" in sections or (
                "EVENTDATA_TEXT_OUTPUT" in sections
            ):
                _parse_event_commands(
                    lines,
                    source_file,
                    "map",
                    records,
                    issues,
                    unknown_records,
                )
            elif "COMMON_EVENT_TEXT_OUTPUT" in sections:
                _parse_event_commands(
                    lines,
                    source_file,
                    "common",
                    records,
                    issues,
                    unknown_records,
                )
            elif "DATABASE_TEXT_OUTPUT" in sections:
                _parse_database(
                    lines,
                    source_file,
                    records,
                    issues,
                    unknown_records,
                )
            else:
                issues.append(
                    WolfTextIssue(
                        severity="warning",
                        code="UNKNOWN_TEXT_EXPORT_FORMAT",
                        reason="no supported top-level .Auto.txt section was found",
                        source_file=source_file,
                    )
                )
            file_infos.append(
                WolfTextFileInfo(
                    source_file=source_file,
                    size=size,
                    target_type=target_type,
                    encoding=decoded.encoding,
                    encoding_confidence=decoded.encoding_confidence,
                    encoding_evidence=decoded.encoding_evidence,
                    bom=decoded.bom,
                    newline_style=decoded.newline_style,
                    final_newline=decoded.final_newline,
                    sections=sections,
                    record_count=len(records) - before,
                )
            )

        ids: dict[str, WolfTextRecord] = {}
        for record in records:
            previous = ids.get(record.id)
            if previous is not None:
                issues.append(
                    WolfTextIssue(
                        severity="error",
                        code="WOLF_CANONICAL_ID_COLLISION",
                        reason=f"canonical ID is duplicated: {record.id}",
                        source_file=record.source_file,
                    )
                )
            else:
                ids[record.id] = record

        target_types = {
            info.target_type for info in file_infos if info.target_type in {"BASIC", "MAP"}
        }
        target_type = (
            "ALL"
            if target_types == {"BASIC", "MAP"}
            else next(iter(target_types))
            if len(target_types) == 1
            else "unknown"
        )
        return WolfTextReport(
            source_path=".",
            file_count=len(file_infos),
            target_type=target_type,
            detected_encoding=_aggregate(info.encoding for info in file_infos),
            encoding_confidence=_aggregate(
                info.encoding_confidence for info in file_infos
            ),
            encoding_evidence=tuple(
                dict.fromkeys(
                    evidence
                    for info in file_infos
                    for evidence in info.encoding_evidence
                )
            ),
            bom=_aggregate(info.bom for info in file_infos),
            newline_style=_aggregate(info.newline_style for info in file_infos),
            final_newline=_aggregate(
                "unknown"
                if info.final_newline is None
                else "yes"
                if info.final_newline
                else "no"
                for info in file_infos
            ),
            files=tuple(file_infos),
            sections=tuple(all_sections),
            records=tuple(records),
            issues=tuple(issues),
            unknown_records=tuple(unknown_records[:MAX_UNKNOWN_RECORDS]),
            fixture_kind=fixture_kind,
            notes=(
                "Canonical WOLF location schema v1 is provisional pending more official-export fixtures.",
                "Raw string literal contents are preserved; normalized_view only expands the observed \\n token.",
                "Unknown commands and decode failures are reported rather than silently discarded.",
            ),
        )


def decode_auto_text(data: bytes) -> _DecodedFile:
    """Decode strict candidates and expose ambiguity instead of guessing."""

    bom = "none"
    payload = data
    codec: str | None = None
    if data.startswith(b"\xef\xbb\xbf"):
        bom, payload, codec = "utf-8", data[3:], "utf-8"
    elif data.startswith(b"\xff\xfe"):
        bom, payload, codec = "utf-16-le", data[2:], "utf-16-le"
    elif data.startswith(b"\xfe\xff"):
        bom, payload, codec = "utf-16-be", data[2:], "utf-16-be"

    if codec is not None:
        try:
            text = payload.decode(codec, errors="strict")
        except UnicodeDecodeError as exc:
            return _DecodedFile(
                text=None,
                encoding="unknown",
                encoding_confidence="none",
                encoding_evidence=(
                    f"{bom} BOM was present",
                    f"strict {codec} decode failed at byte {exc.start}",
                ),
                bom=bom,
                newline_style="unknown",
                final_newline=None,
                error=f"strict {codec} decode failed at byte {exc.start}",
            )
        newline_style, final_newline = _newline_metadata(text)
        return _DecodedFile(
            text=text,
            encoding=codec,
            encoding_confidence="confirmed",
            encoding_evidence=(f"{bom} BOM was present", "strict decode succeeded"),
            bom=bom,
            newline_style=newline_style,
            final_newline=final_newline,
        )

    if not data or all(value < 0x80 for value in data):
        text = data.decode("ascii")
        newline_style, final_newline = _newline_metadata(text)
        return _DecodedFile(
            text=text,
            encoding="ascii",
            encoding_confidence="ambiguous",
            encoding_evidence=(
                "all bytes are ASCII-compatible",
                "underlying UTF-8 or CP932 encoding cannot be distinguished",
            ),
            bom="none",
            newline_style=newline_style,
            final_newline=final_newline,
        )

    utf16 = _probable_utf16_encoding(data)
    if utf16 is not None:
        try:
            text = data.decode(utf16, errors="strict")
        except UnicodeDecodeError:
            pass
        else:
            newline_style, final_newline = _newline_metadata(text)
            return _DecodedFile(
                text=text,
                encoding=utf16,
                encoding_confidence="probable",
                encoding_evidence=(
                    "no BOM was present",
                    f"null-byte distribution indicates {utf16}",
                    "strict decode succeeded",
                ),
                bom="none",
                newline_style=newline_style,
                final_newline=final_newline,
            )

    successes: list[tuple[str, str]] = []
    failures: list[str] = []
    for candidate in ("utf-8", "cp932"):
        try:
            text = data.decode(candidate, errors="strict")
        except UnicodeDecodeError as exc:
            failures.append(f"strict {candidate} decode failed at byte {exc.start}")
        else:
            successes.append((candidate, text))
    if len(successes) == 1:
        encoding, text = successes[0]
        newline_style, final_newline = _newline_metadata(text)
        return _DecodedFile(
            text=text,
            encoding=encoding,
            encoding_confidence="probable",
            encoding_evidence=tuple(
                ["no BOM was present", *failures, f"strict {encoding} decode succeeded"]
            ),
            bom="none",
            newline_style=newline_style,
            final_newline=final_newline,
        )
    if len(successes) > 1:
        names = ", ".join(item[0] for item in successes)
        return _DecodedFile(
            text=None,
            encoding="unknown",
            encoding_confidence="ambiguous",
            encoding_evidence=(
                "no BOM was present",
                f"multiple strict decoders succeeded: {names}",
            ),
            bom="none",
            newline_style="unknown",
            final_newline=None,
            error=f"encoding is ambiguous ({names})",
        )
    return _DecodedFile(
        text=None,
        encoding="unknown",
        encoding_confidence="none",
        encoding_evidence=tuple(["no BOM was present", *failures]),
        bom="none",
        newline_style="unknown",
        final_newline=None,
        error="strict decode failed (" + "; ".join(failures) + ")",
    )


def detect_control_like_tokens(text: str) -> tuple[str, ...]:
    """Observe WOLF-like tokens without assigning RPG Maker semantics."""

    return tuple(
        match.group(0)
        for match in _CONTROL_TOKEN_RE.finditer(text)
        if match.group(0).casefold() != r"\n"
    )


def _parse_game_settings(
    lines: list[str], source_file: str, records: list[WolfTextRecord]
) -> None:
    section = "game_settings_pro" if any(
        line == "[GAMESETTING_PRO_TEXT_OUTPUT]" for line in lines
    ) else "game_settings"
    for line_index, line in enumerate(lines, start=1):
        match = _KEY_VALUE_RE.fullmatch(line)
        if match is None or match.group(1) not in _SYSTEM_TEXT_FIELDS:
            continue
        field, original = match.groups()
        location = WolfLocation(
            domain="basic",
            source=source_file,
            container_kind="container",
            container_id=section,
            field=field,
        )
        records.append(
            WolfTextRecord(
                location=location,
                type="system",
                original=original,
                source_file=source_file,
                raw_context={"line": line_index, "raw": line},
                metadata={"format": "key_value", "confidence": "observed"},
                control_codes=detect_control_like_tokens(original),
                classification=WolfRecordClassification.VERIFIED_TRANSLATABLE,
            )
        )


def _parse_event_commands(
    lines: list[str],
    source_file: str,
    domain: str,
    records: list[WolfTextRecord],
    issues: list[WolfTextIssue],
    unknown_records: list[WolfUnknownRecord],
) -> None:
    container_id: int | None = None
    page_id: int | None = None
    page_count_seen = False
    expected_commands: int | None = None
    index = 0
    while index < len(lines):
        line = lines[index]
        match = _KEY_VALUE_RE.fullmatch(line)
        if match is not None:
            key, value = match.groups()
            if domain == "common" and key == "COMMON_ID" and value.isdigit():
                container_id = int(value)
                page_id = None
            elif domain == "map" and key == "EVENT_ID" and value.isdigit():
                container_id = int(value)
                page_id = None
                page_count_seen = False
            elif domain == "map" and key == "EVENT_PAGE_NUM":
                page_count_seen = True
            elif (
                domain == "map"
                and key == "EVENT_PAGE"
                and page_count_seen
                and value.isdigit()
            ):
                page_id = int(value)
            elif key == "COMMAND_NUM" and value.isdigit():
                expected_commands = int(value)

        if line != "WoditorEvCOMMAND_START":
            index += 1
            continue
        raw_start = index
        raw_end = _find_line(lines, "WoditorEvCOMMAND_END", raw_start + 1)
        if raw_end is None:
            issues.append(
                WolfTextIssue(
                    severity="error",
                    code="MALFORMED_COMMAND_BLOCK",
                    reason="WoditorEvCOMMAND_END was not found",
                    source_file=source_file,
                    line=raw_start + 1,
                )
            )
            return
        text_start = _find_line(lines, "[COMMAND_TEXT_START]", raw_end + 1)
        text_end = (
            _find_line(lines, "[COMMAND_TEXT_END]", text_start + 1)
            if text_start is not None
            else None
        )
        raw_commands = [
            (line_number + 1, lines[line_number])
            for line_number in range(raw_start + 1, raw_end)
            if _RAW_COMMAND_RE.match(lines[line_number])
        ]
        human_commands: list[tuple[int, str]] = []
        if text_start is not None and text_end is not None:
            human_commands = [
                (line_number + 1, lines[line_number])
                for line_number in range(text_start + 1, text_end)
                if lines[line_number].lstrip().startswith(("■", "▼"))
            ]
        if expected_commands is not None and expected_commands != len(raw_commands):
            issues.append(
                WolfTextIssue(
                    severity="warning",
                    code="COMMAND_COUNT_MISMATCH",
                    reason=(
                        f"COMMAND_NUM={expected_commands}, parsed={len(raw_commands)}"
                    ),
                    source_file=source_file,
                    line=raw_start + 1,
                )
            )
        if human_commands and len(human_commands) != len(raw_commands):
            issues.append(
                WolfTextIssue(
                    severity="warning",
                    code="COMMAND_TEXT_ALIGNMENT_UNKNOWN",
                    reason=(
                        f"raw commands={len(raw_commands)}, text commands={len(human_commands)}"
                    ),
                    source_file=source_file,
                    line=text_start + 1 if text_start is not None else None,
                )
            )

        for command_index, (line_number, raw_command) in enumerate(raw_commands):
            code_match = _RAW_COMMAND_RE.match(raw_command)
            code = code_match.group(1) if code_match is not None else "unknown"
            label = (
                human_commands[command_index][1]
                if command_index < len(human_commands)
                else ""
            )
            literals = _extract_raw_string_literals(raw_command)
            record_type, classification = _event_record_type(code, label)
            if record_type is None:
                if literals and len(unknown_records) < MAX_UNKNOWN_RECORDS:
                    unknown_records.append(
                        WolfUnknownRecord(
                            source_file=source_file,
                            line=line_number,
                            kind=f"event_command_{code}",
                            raw=raw_command,
                        )
                    )
                continue
            if container_id is None or (domain == "map" and page_id is None):
                issues.append(
                    WolfTextIssue(
                        severity="error",
                        code="COMMAND_LOCATION_INCOMPLETE",
                        reason="event/common container metadata was not available",
                        source_file=source_file,
                        line=line_number,
                    )
                )
                continue
            for text_index, original in enumerate(literals):
                slot_classification = classification
                if code == "101" and text_index != 0:
                    slot_classification = (
                        WolfRecordClassification.EXPERIMENTAL_TRANSLATABLE
                    )
                location = WolfLocation(
                    domain=domain,
                    source=source_file,
                    container_kind="event" if domain == "map" else "common",
                    container_id=container_id,
                    page_id=page_id if domain == "map" else None,
                    command_index=command_index,
                    text_index=text_index,
                )
                records.append(
                    WolfTextRecord(
                        location=location,
                        type=record_type,
                        original=original,
                        normalized_view=original.replace(r"\n", "\n"),
                        source_file=source_file,
                        raw_context={
                            "line": line_number,
                            "raw_command": raw_command,
                            "command_text": label,
                        },
                        metadata={
                            "command_code": code,
                            "string_representation": "auto_txt_raw_literal",
                            "confidence": (
                                "public_repository_observed"
                                if slot_classification
                                == WolfRecordClassification.VERIFIED_TRANSLATABLE
                                else "experimental"
                            ),
                        },
                        control_codes=detect_control_like_tokens(original),
                        classification=slot_classification,
                    )
                )
        index = (text_end + 1) if text_end is not None else (raw_end + 1)


def _parse_database(
    lines: list[str],
    source_file: str,
    records: list[WolfTextRecord],
    issues: list[WolfTextIssue],
    unknown_records: list[WolfUnknownRecord],
) -> None:
    type_id: int | None = None
    data_types: dict[int, int] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        match = _KEY_VALUE_RE.fullmatch(line)
        if match is not None:
            key, value = match.groups()
            if key == "TYPE_ID" and value.isdigit():
                type_id = int(value)
                data_types = {}
            else:
                data_type_match = _DATABASE_DATA_TYPE_RE.fullmatch(key)
                if data_type_match is not None and value.isdigit():
                    data_types[int(data_type_match.group(1))] = int(value)
        if line != "<<--CSV_START-->>":
            index += 1
            continue
        end = _find_line(lines, "<<--CSV_END-->>", index + 1)
        if end is None:
            issues.append(
                WolfTextIssue(
                    severity="error",
                    code="MALFORMED_DATABASE_CSV_BLOCK",
                    reason="<<--CSV_END-->> was not found",
                    source_file=source_file,
                    line=index + 1,
                )
            )
            return
        data_lines = lines[index + 1 : end]
        header: list[str] = []
        if data_lines:
            try:
                header = next(csv.reader([data_lines[0]], strict=True))
            except (csv.Error, StopIteration):
                header = []
        for record_id, (offset, row) in enumerate(
            zip(range(index + 2, end + 1), data_lines[1:])
        ):
            try:
                cells = next(csv.reader([row], strict=True))
            except (csv.Error, StopIteration) as exc:
                issues.append(
                    WolfTextIssue(
                        severity="warning",
                        code="MALFORMED_DATABASE_CSV_ROW",
                        reason=str(exc),
                        source_file=source_file,
                        line=offset + 1,
                    )
                )
                if len(unknown_records) < MAX_UNKNOWN_RECORDS:
                    unknown_records.append(
                        WolfUnknownRecord(
                            source_file=source_file,
                            line=offset + 1,
                            kind="database_csv_row",
                            raw=row,
                        )
                    )
                continue
            for field_index, cell in enumerate(cells):
                marker_index = cell.find(_DATABASE_NAME_MARKER)
                if marker_index < 0:
                    data_type = data_types.get(field_index)
                    if data_type is None or data_type < 2000 or type_id is None:
                        continue
                    location = WolfLocation(
                        domain="database",
                        source=source_file,
                        container_kind="database",
                        container_id=Path(source_file).name,
                        type_id=type_id,
                        record_id=record_id,
                        field=f"item{field_index}",
                    )
                    records.append(
                        WolfTextRecord(
                            location=location,
                            type="database_text",
                            original=cell,
                            source_file=source_file,
                            raw_context={
                                "line": offset + 1,
                                "raw": row,
                                "csv_field_index": field_index,
                            },
                            metadata={
                                "data_type_code": data_type,
                                "field_label": (
                                    header[field_index]
                                    if field_index < len(header)
                                    else ""
                                ),
                                "confidence": "public_repository_observed",
                                "translation_status": "experimental",
                            },
                            control_codes=detect_control_like_tokens(cell),
                            classification=(
                                WolfRecordClassification.EXPERIMENTAL_TRANSLATABLE
                            ),
                        )
                    )
                    continue
                original = cell[marker_index + len(_DATABASE_NAME_MARKER) :]
                if type_id is None:
                    issues.append(
                        WolfTextIssue(
                            severity="error",
                            code="DATABASE_TYPE_ID_MISSING",
                            reason="database name was found without TYPE_ID",
                            source_file=source_file,
                            line=offset + 1,
                        )
                    )
                    continue
                location = WolfLocation(
                    domain="database",
                    source=source_file,
                    container_kind="database",
                    container_id=Path(source_file).name,
                    type_id=type_id,
                    record_id=record_id,
                    field="dataname",
                )
                records.append(
                    WolfTextRecord(
                        location=location,
                        type="database_name",
                        original=original,
                        normalized_view=original.replace("<<COMMA>>", ","),
                        source_file=source_file,
                        raw_context={
                            "line": offset + 1,
                            "raw": row,
                            "csv_field_index": field_index,
                        },
                        metadata={
                            "marker": _DATABASE_NAME_MARKER,
                            "confidence": "verified_marker",
                        },
                        control_codes=detect_control_like_tokens(original),
                        classification=WolfRecordClassification.VERIFIED_TRANSLATABLE,
                    )
                )
        index = end + 1


def _event_record_type(
    code: str, label: str
) -> tuple[str | None, WolfRecordClassification]:
    visible = label[label.find("■") :] if "■" in label else label
    if code == "101":
        return "dialogue", WolfRecordClassification.VERIFIED_TRANSLATABLE
    if visible.startswith(("■文章:", "■文章：")):
        return "dialogue", WolfRecordClassification.EXPERIMENTAL_TRANSLATABLE
    if (
        visible.startswith(("■選択肢:", "■選択肢：", "■選択肢の表示"))
        and "強制中断" not in visible
    ):
        return "choice", WolfRecordClassification.EXPERIMENTAL_TRANSLATABLE
    return None, WolfRecordClassification.UNKNOWN


def _extract_raw_string_literals(raw_command: str) -> tuple[str, ...]:
    literals: list[str] = []
    index = 0
    while index < len(raw_command):
        if raw_command[index] != '"':
            index += 1
            continue
        index += 1
        value: list[str] = []
        while index < len(raw_command):
            character = raw_command[index]
            if character == "\\" and index + 1 < len(raw_command):
                value.append(character)
                value.append(raw_command[index + 1])
                index += 2
                continue
            if character == '"':
                literals.append("".join(value))
                index += 1
                break
            value.append(character)
            index += 1
        else:
            break
    return tuple(literals)


def _discover_auto_text_files(
    root: Path,
) -> tuple[list[Path], list[WolfTextIssue]]:
    paths: list[Path] = []
    issues: list[WolfTextIssue] = []
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        safe_directories: list[str] = []
        for name in sorted(directory_names, key=str.casefold):
            candidate = current_path / name
            if candidate.is_symlink():
                issues.append(
                    WolfTextIssue(
                        severity="warning",
                        code="SYMLINK_SKIPPED",
                        reason="symbolic-link directory was not followed",
                        source_file=portable_relative_path(candidate, root),
                    )
                )
            else:
                safe_directories.append(name)
        directory_names[:] = safe_directories
        for name in sorted(file_names, key=str.casefold):
            candidate = current_path / name
            if candidate.is_symlink():
                issues.append(
                    WolfTextIssue(
                        severity="warning",
                        code="SYMLINK_SKIPPED",
                        reason="symbolic-link file was not read",
                        source_file=portable_relative_path(candidate, root),
                    )
                )
                continue
            if name.casefold().endswith(".auto.txt"):
                paths.append(candidate)
                if len(paths) >= MAX_TEXT_EXPORT_FILES:
                    issues.append(
                        WolfTextIssue(
                            severity="warning",
                            code="TEXT_FILE_LIMIT_REACHED",
                            reason=(
                                f"inspection stopped after {MAX_TEXT_EXPORT_FILES} files"
                            ),
                        )
                    )
                    directory_names[:] = []
                    break
        if len(paths) >= MAX_TEXT_EXPORT_FILES:
            break
    return sorted(paths, key=lambda path: path.as_posix().casefold()), issues


def _target_type(source_file: str) -> str:
    parts = tuple(part.casefold() for part in Path(source_file).parts)
    if "basicdata" in parts:
        return "BASIC"
    if source_file.casefold().endswith(".mps.auto.txt") or "mapdata" in parts:
        return "MAP"
    return "unknown"


def _probable_utf16_encoding(data: bytes) -> str | None:
    if len(data) < 4 or len(data) % 2:
        return None
    even = data[0::2]
    odd = data[1::2]
    even_zero = even.count(0) / len(even)
    odd_zero = odd.count(0) / len(odd)
    if odd_zero > 0.3 and even_zero < 0.1:
        return "utf-16-le"
    if even_zero > 0.3 and odd_zero < 0.1:
        return "utf-16-be"
    return None


def _newline_metadata(text: str) -> tuple[str, bool]:
    crlf = text.count("\r\n")
    without_crlf = text.replace("\r\n", "")
    lf = without_crlf.count("\n")
    cr = without_crlf.count("\r")
    present = [name for name, count in (("CRLF", crlf), ("LF", lf), ("CR", cr)) if count]
    style = present[0] if len(present) == 1 else "mixed" if present else "none"
    return style, text.endswith(("\r\n", "\n", "\r"))


def _aggregate(values) -> str:
    unique = set(values)
    if not unique:
        return "unknown"
    return next(iter(unique)) if len(unique) == 1 else "mixed"


def _find_line(lines: list[str], expected: str, start: int) -> int | None:
    for index in range(start, len(lines)):
        if lines[index] == expected:
            return index
    return None


__all__ = [
    "MAX_TEXT_EXPORT_FILE_BYTES",
    "WolfTextInspector",
    "decode_auto_text",
    "detect_control_like_tokens",
]
