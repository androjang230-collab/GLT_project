"""Safe JSONL split/merge primitives independent from engine implementation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


FORMAT_VERSION = 1
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True, slots=True)
class TranslationUtilityIssue:
    code: str
    reason: str
    file: str | None = None
    line: int | None = None
    column: int | None = None
    id: str | None = None

    def to_json_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"issue_code": self.code, "reason": self.reason}
        for name in ("file", "line", "column", "id"):
            value = getattr(self, name)
            if value is not None:
                payload[name] = value
        return payload


class TranslationUtilityError(RuntimeError):
    """Fatal path, option, or filesystem error."""


class TranslationValidationError(TranslationUtilityError):
    """One or more data-integrity errors that safely block output."""

    def __init__(self, issues: list[TranslationUtilityIssue]) -> None:
        self.issues = issues
        super().__init__("; ".join(f"{item.code}: {item.reason}" for item in issues))


@dataclass(frozen=True, slots=True)
class JsonlRow:
    payload: dict[str, Any]
    raw_line: str
    line_number: int


@dataclass(frozen=True, slots=True)
class SplitPart:
    filename: str
    entry_count: int
    first_id: str
    last_id: str
    source_file: str | None = None

    def to_json_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "filename": self.filename,
            "entry_count": self.entry_count,
            "first_id": self.first_id,
            "last_id": self.last_id,
        }
        if self.source_file is not None:
            payload["source_file"] = self.source_file
        return payload


@dataclass(frozen=True, slots=True)
class SplitResult:
    output_directory: Path
    total_entries: int
    split_mode: str
    parts: tuple[SplitPart, ...]
    source_sha256: str


@dataclass(slots=True)
class MergeReport:
    source_entries: int = 0
    part_entries: int = 0
    merged_entries: int = 0
    translated_entries: int = 0
    untranslated_entries: int = 0
    duplicate_ids: int = 0
    unknown_ids: int = 0
    missing_ids: int = 0
    metadata_mismatches: int = 0
    issues: list[TranslationUtilityIssue] = field(default_factory=list)
    status: str = "failed"
    dry_run: bool = False

    @property
    def errors(self) -> int:
        return len(self.issues)

    def to_json_dict(self) -> dict[str, object]:
        return {
            "source_entries": self.source_entries,
            "part_entries": self.part_entries,
            "merged_entries": self.merged_entries,
            "translated_entries": self.translated_entries,
            "untranslated_entries": self.untranslated_entries,
            "duplicate_ids": self.duplicate_ids,
            "unknown_ids": self.unknown_ids,
            "missing_ids": self.missing_ids,
            "metadata_mismatches": self.metadata_mismatches,
            "errors": self.errors,
            "status": self.status,
            "dry_run": self.dry_run,
            "issues": [issue.to_json_dict() for issue in self.issues],
        }


@dataclass(frozen=True, slots=True)
class MergeResult:
    report: MergeReport
    output_file: Path
    report_file: Path


def split_translation(
    source_file: Path,
    output_directory: Path,
    *,
    by_file: bool = True,
    lines: int | None = None,
    max_lines: int | None = None,
) -> SplitResult:
    """Split JSONL without changing any row value or relative row order."""

    source_file = source_file.resolve()
    output_directory = output_directory.resolve()
    if not source_file.is_file():
        raise FileNotFoundError(f"source JSONL does not exist: {source_file}")
    if output_directory.exists():
        raise FileExistsError(
            f"split output already exists; refusing to overwrite: {output_directory}"
        )
    if lines is not None and by_file:
        raise TranslationUtilityError("--by-file and --lines cannot be combined")
    if lines is not None and max_lines is not None:
        raise TranslationUtilityError("--lines and --max-lines cannot be combined")
    if lines is not None and lines <= 0:
        raise TranslationUtilityError("--lines must be a positive integer")
    if max_lines is not None and max_lines <= 0:
        raise TranslationUtilityError("--max-lines must be a positive integer")
    if not by_file and lines is None:
        raise TranslationUtilityError("line mode requires --lines")

    rows, issues = _read_jsonl_rows(source_file, source_file.name)
    issues.extend(_validate_split_rows(rows, require_file=by_file))
    if issues:
        raise TranslationValidationError(issues)

    if by_file:
        planned_parts = _split_by_file(rows, max_lines)
        split_mode = "by_file"
    else:
        assert lines is not None
        planned_parts = [
            (f"part_{index:03d}.jsonl", chunk, None)
            for index, chunk in enumerate(_chunks(rows, lines), start=1)
        ]
        split_mode = "lines"

    output_directory.parent.mkdir(parents=True, exist_ok=True)
    staging = output_directory.parent / (
        f".{output_directory.name}.glt-split-{uuid.uuid4().hex}.tmp"
    )
    source_hash = _sha256_file(source_file)
    parts: list[SplitPart] = []
    try:
        staging.mkdir()
        for filename, part_rows, source_group in planned_parts:
            _write_raw_rows_atomic(staging / filename, part_rows)
            parts.append(
                SplitPart(
                    filename=filename,
                    entry_count=len(part_rows),
                    first_id=part_rows[0].payload["id"],
                    last_id=part_rows[-1].payload["id"],
                    source_file=source_group,
                )
            )
        manifest = {
            "format_version": FORMAT_VERSION,
            "source_filename": source_file.name,
            "source_sha256": source_hash,
            "total_entries": len(rows),
            "split_mode": split_mode,
            "max_lines": max_lines if by_file else None,
            "lines": lines if not by_file else None,
            "parts": [part.to_json_dict() for part in parts],
        }
        _write_json_atomic(staging / "split_manifest.json", manifest)
        if output_directory.exists():
            raise FileExistsError(
                f"split output appeared during creation: {output_directory}"
            )
        staging.rename(output_directory)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise

    return SplitResult(
        output_directory=output_directory,
        total_entries=len(rows),
        split_mode=split_mode,
        parts=tuple(parts),
        source_sha256=source_hash,
    )


def merge_translation(
    work_directory: Path,
    source_file: Path,
    output_file: Path,
    *,
    dry_run: bool = False,
) -> MergeResult:
    """Validate parts against canonical source and merge translation only."""

    work_directory = work_directory.resolve()
    source_file = source_file.resolve()
    output_file = output_file.resolve()
    if not work_directory.is_dir():
        raise NotADirectoryError(f"translation work directory does not exist: {work_directory}")
    if not source_file.is_file():
        raise FileNotFoundError(f"canonical source JSONL does not exist: {source_file}")
    if output_file.exists():
        raise FileExistsError(f"merge output already exists: {output_file}")
    if output_file == source_file:
        raise TranslationUtilityError("merge output must differ from canonical source")

    report = MergeReport(dry_run=dry_run)
    source_rows, source_issues = _read_jsonl_rows(source_file, source_file.name)
    report.source_entries = len(source_rows)
    report.issues.extend(source_issues)
    source_by_id: dict[str, JsonlRow] = {}
    source_counts: Counter[str] = Counter()
    for row in source_rows:
        entry_id = row.payload.get("id")
        if not isinstance(entry_id, str) or not entry_id:
            report.issues.append(
                TranslationUtilityIssue(
                    "INVALID_SOURCE_ENTRY",
                    "source entry requires a non-empty string id",
                    source_file.name,
                    row.line_number,
                )
            )
            continue
        source_counts[entry_id] += 1
        source_by_id.setdefault(entry_id, row)
    for entry_id, count in sorted(source_counts.items()):
        if count > 1:
            report.issues.append(
                TranslationUtilityIssue(
                    "DUPLICATE_SOURCE_ID",
                    f"canonical source contains ID {count} times",
                    source_file.name,
                    id=entry_id,
                )
            )

    manifest, part_files, manifest_issues = _load_merge_manifest(
        work_directory,
        source_file,
        len(source_rows),
    )
    report.issues.extend(manifest_issues)

    part_rows: list[tuple[str, JsonlRow]] = []
    actual_part_counts: dict[str, int] = {}
    for part_path in part_files:
        rows, issues = _read_jsonl_rows(part_path, part_path.name)
        report.issues.extend(issues)
        actual_part_counts[part_path.name] = len(rows)
        part_rows.extend((part_path.name, row) for row in rows)
    report.part_entries = len(part_rows)
    if manifest is not None:
        _validate_manifest_part_counts(manifest, actual_part_counts, report.issues)

    locations: dict[str, list[tuple[str, JsonlRow]]] = defaultdict(list)
    for filename, row in part_rows:
        entry_id = row.payload.get("id")
        if not isinstance(entry_id, str) or not entry_id:
            report.issues.append(
                TranslationUtilityIssue(
                    "INVALID_PART_ENTRY",
                    "part entry requires a non-empty string id",
                    filename,
                    row.line_number,
                )
            )
            continue
        translation = row.payload.get("translation")
        if not isinstance(translation, str):
            report.issues.append(
                TranslationUtilityIssue(
                    "INVALID_TRANSLATION",
                    "translation must be a string",
                    filename,
                    row.line_number,
                    id=entry_id,
                )
            )
        locations[entry_id].append((filename, row))

    duplicate_ids = {entry_id for entry_id, found in locations.items() if len(found) > 1}
    report.duplicate_ids = len(duplicate_ids)
    for entry_id in sorted(duplicate_ids):
        found = locations[entry_id]
        places = [f"{filename}:{row.line_number}" for filename, row in found]
        report.issues.append(
            TranslationUtilityIssue(
                "DUPLICATE_ID",
                f"ID appears in multiple part rows: {places!r}",
                id=entry_id,
            )
        )

    unknown_ids = set(locations) - set(source_by_id)
    report.unknown_ids = len(unknown_ids)
    for entry_id in sorted(unknown_ids):
        filename, row = locations[entry_id][0]
        report.issues.append(
            TranslationUtilityIssue(
                "UNKNOWN_ID",
                "part ID does not exist in canonical source",
                filename,
                row.line_number,
                id=entry_id,
            )
        )

    missing_ids = set(source_by_id) - set(locations)
    report.missing_ids = len(missing_ids)
    for entry_id in sorted(missing_ids):
        report.issues.append(
            TranslationUtilityIssue(
                "MISSING_ID",
                "canonical source ID is absent from translation parts",
                id=entry_id,
            )
        )

    merged_payloads: list[dict[str, Any]] = []
    for source_row in source_rows:
        entry_id = source_row.payload.get("id")
        if not isinstance(entry_id, str):
            continue
        found = locations.get(entry_id, [])
        if len(found) != 1 or entry_id not in source_by_id:
            continue
        filename, part_row = found[0]
        mismatches = _metadata_mismatches(source_row.payload, part_row.payload)
        if mismatches:
            report.metadata_mismatches += 1
            report.issues.append(
                TranslationUtilityIssue(
                    "METADATA_MISMATCH",
                    f"fields changed outside translation: {mismatches!r}",
                    filename,
                    part_row.line_number,
                    id=entry_id,
                )
            )
            continue
        translation = part_row.payload.get("translation")
        if not isinstance(translation, str):
            continue
        final_entry = source_row.payload.copy()
        final_entry["translation"] = translation
        merged_payloads.append(final_entry)

    if len(merged_payloads) == len(source_rows):
        report.translated_entries = sum(
            bool(entry["translation"].strip()) for entry in merged_payloads
        )
        report.untranslated_entries = len(merged_payloads) - report.translated_entries
    report_file = work_directory / "merge_report.json"
    if report.issues:
        report.status = "failed"
    elif dry_run:
        report.status = "dry_run"
        report.merged_entries = len(merged_payloads)
    else:
        _write_jsonl_atomic(output_file, merged_payloads)
        report.status = "success"
        report.merged_entries = len(merged_payloads)
    _write_json_atomic(report_file, report.to_json_dict())
    return MergeResult(report=report, output_file=output_file, report_file=report_file)


def _read_jsonl_rows(
    path: Path,
    display_name: str,
) -> tuple[list[JsonlRow], list[TranslationUtilityIssue]]:
    rows: list[JsonlRow] = []
    issues: list[TranslationUtilityIssue] = []
    try:
        stream = path.open("r", encoding="utf-8-sig", newline="")
    except (OSError, UnicodeError) as exc:
        raise TranslationUtilityError(f"cannot read {display_name}: {exc}") from exc
    with stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            raw_line = line.rstrip("\r\n")
            try:
                payload = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                issues.append(
                    TranslationUtilityIssue(
                        "MALFORMED_JSONL",
                        exc.msg,
                        display_name,
                        line_number,
                        exc.colno,
                    )
                )
                continue
            if not isinstance(payload, dict):
                issues.append(
                    TranslationUtilityIssue(
                        "MALFORMED_JSONL",
                        "JSONL row must be an object",
                        display_name,
                        line_number,
                        1,
                    )
                )
                continue
            rows.append(JsonlRow(payload=payload, raw_line=raw_line, line_number=line_number))
    return rows, issues


def _validate_split_rows(
    rows: list[JsonlRow],
    *,
    require_file: bool,
) -> list[TranslationUtilityIssue]:
    issues: list[TranslationUtilityIssue] = []
    ids: Counter[str] = Counter()
    for row in rows:
        entry_id = row.payload.get("id")
        if not isinstance(entry_id, str) or not entry_id:
            issues.append(
                TranslationUtilityIssue(
                    "INVALID_ENTRY",
                    "entry requires a non-empty string id",
                    line=row.line_number,
                )
            )
        else:
            ids[entry_id] += 1
        if require_file and (
            not isinstance(row.payload.get("file"), str) or not row.payload["file"]
        ):
            issues.append(
                TranslationUtilityIssue(
                    "INVALID_ENTRY",
                    "--by-file requires a non-empty string file field",
                    line=row.line_number,
                    id=entry_id if isinstance(entry_id, str) else None,
                )
            )
    for entry_id, count in sorted(ids.items()):
        if count > 1:
            issues.append(
                TranslationUtilityIssue(
                    "DUPLICATE_ID",
                    f"source contains ID {count} times",
                    id=entry_id,
                )
            )
    if not rows and not issues:
        issues.append(TranslationUtilityIssue("EMPTY_JSONL", "source JSONL has no entries"))
    return issues


def _split_by_file(
    rows: list[JsonlRow],
    max_lines: int | None,
) -> list[tuple[str, list[JsonlRow], str | None]]:
    groups: dict[str, list[JsonlRow]] = {}
    for row in rows:
        source_group = row.payload["file"]
        groups.setdefault(source_group, []).append(row)
    base_names = _safe_group_names(groups)
    planned: list[tuple[str, list[JsonlRow], str | None]] = []
    for source_group, group_rows in groups.items():
        base = base_names[source_group]
        chunks = list(_chunks(group_rows, max_lines or len(group_rows)))
        if len(chunks) == 1:
            planned.append((f"{base}.jsonl", chunks[0], source_group))
        else:
            width = max(3, len(str(len(chunks))))
            for index, chunk in enumerate(chunks, start=1):
                planned.append(
                    (f"{base}_part{index:0{width}d}.jsonl", chunk, source_group)
                )
    return planned


def _safe_group_names(groups: dict[str, list[JsonlRow]]) -> dict[str, str]:
    result: dict[str, str] = {}
    used: set[str] = set()
    for source_group in groups:
        normalized = source_group.replace("\\", "/")
        raw_name = PurePosixPath(normalized).name
        stem = Path(raw_name).stem
        safe = _SAFE_NAME_RE.sub("_", stem).strip("._") or "group"
        candidate = safe
        if candidate.casefold() in used:
            suffix = hashlib.sha256(source_group.encode("utf-8")).hexdigest()[:8]
            candidate = f"{safe}_{suffix}"
        counter = 2
        while candidate.casefold() in used:
            candidate = f"{safe}_{counter}"
            counter += 1
        used.add(candidate.casefold())
        result[source_group] = candidate
    return result


def _load_merge_manifest(
    work_directory: Path,
    source_file: Path,
    source_entries: int,
) -> tuple[dict[str, Any] | None, list[Path], list[TranslationUtilityIssue]]:
    issues: list[TranslationUtilityIssue] = []
    manifest_path = work_directory / "split_manifest.json"
    all_jsonl = sorted(
        (path for path in work_directory.glob("*.jsonl") if path.is_file()),
        key=lambda path: path.name.casefold(),
    )
    if not manifest_path.is_file():
        if not all_jsonl:
            issues.append(
                TranslationUtilityIssue("NO_PART_FILES", "no JSONL part files found")
            )
        return None, all_jsonl, issues
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        issues.append(
            TranslationUtilityIssue(
                "MALFORMED_MANIFEST",
                exc.msg,
                "split_manifest.json",
                exc.lineno,
                exc.colno,
            )
        )
        return None, [], issues
    except (OSError, UnicodeError) as exc:
        raise TranslationUtilityError(f"cannot read split_manifest.json: {exc}") from exc
    if not isinstance(manifest, dict):
        issues.append(
            TranslationUtilityIssue(
                "MALFORMED_MANIFEST",
                "manifest root must be an object",
                "split_manifest.json",
            )
        )
        return None, [], issues
    if manifest.get("format_version") != FORMAT_VERSION:
        issues.append(
            TranslationUtilityIssue(
                "MANIFEST_VERSION_MISMATCH",
                f"expected format_version {FORMAT_VERSION}",
                "split_manifest.json",
            )
        )
    if manifest.get("source_filename") != source_file.name:
        issues.append(
            TranslationUtilityIssue(
                "SOURCE_FILENAME_MISMATCH",
                "manifest source_filename differs from --source filename",
                "split_manifest.json",
            )
        )
    if manifest.get("source_sha256") != _sha256_file(source_file):
        issues.append(
            TranslationUtilityIssue(
                "SOURCE_JSONL_MISMATCH",
                "canonical source SHA-256 differs from split manifest",
                "split_manifest.json",
            )
        )
    if manifest.get("total_entries") != source_entries:
        issues.append(
            TranslationUtilityIssue(
                "SOURCE_ENTRY_COUNT_MISMATCH",
                "canonical source entry count differs from split manifest",
                "split_manifest.json",
            )
        )
    raw_parts = manifest.get("parts")
    if not isinstance(raw_parts, list):
        issues.append(
            TranslationUtilityIssue(
                "MALFORMED_MANIFEST",
                "manifest parts must be an array",
                "split_manifest.json",
            )
        )
        return manifest, [], issues
    expected_names: list[str] = []
    for index, part in enumerate(raw_parts):
        filename = part.get("filename") if isinstance(part, dict) else None
        if (
            not isinstance(filename, str)
            or Path(filename).name != filename
            or not filename.casefold().endswith(".jsonl")
        ):
            issues.append(
                TranslationUtilityIssue(
                    "MALFORMED_MANIFEST",
                    f"parts[{index}] has unsafe filename",
                    "split_manifest.json",
                )
            )
            continue
        expected_names.append(filename)
    if len({name.casefold() for name in expected_names}) != len(expected_names):
        issues.append(
            TranslationUtilityIssue(
                "DUPLICATE_MANIFEST_PART",
                "manifest contains duplicate part filenames",
                "split_manifest.json",
            )
        )
    expected_set = {name.casefold() for name in expected_names}
    actual_by_name = {path.name.casefold(): path for path in all_jsonl}
    for filename in expected_names:
        if filename.casefold() not in actual_by_name:
            issues.append(
                TranslationUtilityIssue(
                    "MISSING_PART_FILE",
                    "manifest part file is missing",
                    filename,
                )
            )
    for path in all_jsonl:
        if path.name.casefold() not in expected_set:
            issues.append(
                TranslationUtilityIssue(
                    "UNEXPECTED_PART_FILE",
                    "JSONL file is not listed in split manifest and was not included",
                    path.name,
                )
            )
    part_files = [
        actual_by_name[name.casefold()]
        for name in expected_names
        if name.casefold() in actual_by_name
    ]
    return manifest, part_files, issues


def _validate_manifest_part_counts(
    manifest: dict[str, Any],
    actual_counts: dict[str, int],
    issues: list[TranslationUtilityIssue],
) -> None:
    raw_parts = manifest.get("parts")
    if not isinstance(raw_parts, list):
        return
    for part in raw_parts:
        if not isinstance(part, dict):
            continue
        filename = part.get("filename")
        expected = part.get("entry_count")
        if not isinstance(filename, str) or filename not in actual_counts:
            continue
        if not isinstance(expected, int) or isinstance(expected, bool):
            issues.append(
                TranslationUtilityIssue(
                    "MALFORMED_MANIFEST",
                    "part entry_count must be an integer",
                    "split_manifest.json",
                )
            )
        elif actual_counts[filename] != expected:
            issues.append(
                TranslationUtilityIssue(
                    "PART_ENTRY_COUNT_MISMATCH",
                    f"expected {expected}, found {actual_counts[filename]}",
                    filename,
                )
            )


def _metadata_mismatches(
    source: dict[str, Any],
    part: dict[str, Any],
) -> list[str]:
    source_metadata = {key: value for key, value in source.items() if key != "translation"}
    part_metadata = {key: value for key, value in part.items() if key != "translation"}
    keys = set(source_metadata) | set(part_metadata)
    return sorted(
        key for key in keys if source_metadata.get(key, _MISSING) != part_metadata.get(key, _MISSING)
    )


_MISSING = object()


def _chunks(rows: list[JsonlRow], size: int) -> Iterable[list[JsonlRow]]:
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def _write_raw_rows_atomic(path: Path, rows: list[JsonlRow]) -> None:
    payload = "".join(f"{row.raw_line}\n" for row in rows).encode("utf-8")
    _write_bytes_atomic(path, payload)


def _write_jsonl_atomic(path: Path, records: list[dict[str, Any]]) -> None:
    payload = "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        for record in records
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
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
            temporary_file.write(payload)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        # Creating a hard link publishes the complete temporary file while
        # atomically refusing an existing destination on Windows and POSIX.
        os.link(temporary_path, path)
        temporary_path.unlink()
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    _write_bytes_atomic(
        path,
        (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
            temporary_file.write(payload)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
