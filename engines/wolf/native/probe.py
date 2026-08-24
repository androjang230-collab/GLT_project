"""Conservative read-only probe for unpacked WOLF native project files."""

from __future__ import annotations

import hashlib
import math
import os
from collections import Counter
from pathlib import Path
from typing import Iterable, Sequence

from core.paths import portable_relative_path
from engines.wolf.native.models import (
    EvidenceGrade,
    NativeDocument,
    NativeFileProbe,
    NativeOracleMapping,
    NativeStringCorrelation,
    WolfNativeResearchReport,
    write_native_research_report,
)
from engines.wolf.text_inspector import WolfTextInspector
from engines.wolf.text_models import WolfTextRecord


MAX_NATIVE_FILES = 10_000
MAX_NATIVE_FILE_BYTES = 256 * 1024 * 1024
MAX_CORRELATION_FILE_BYTES = 64 * 1024 * 1024
MAX_CORRELATION_STRINGS_PER_FILE = 64
MAX_CORRELATION_TEXT_CHARS = 4_096
MAX_MATCH_OFFSETS = 8
HEADER_BYTES = 64
IO_CHUNK_BYTES = 1024 * 1024

_GAME_MAGIC = b"\x00W\x00\x00OL\x00FM"
_TABLE_MAGIC_PREFIX = b"\x00W\x00\x00OL"
_MAP_MAGIC = b"\x00" * 10 + b"WOLFM\x00"


class WolfNativeProbe:
    """Inventory and correlate files without constructing a native writer."""

    def inspect(
        self,
        game_directory: Path,
        *,
        oracle_directory: Path | None = None,
    ) -> WolfNativeResearchReport:
        if not game_directory.is_dir():
            raise NotADirectoryError(f"WOLF project directory does not exist: {game_directory}")
        paths, issues = _discover_native_files(game_directory)
        files: list[NativeFileProbe] = []
        documents: list[NativeDocument] = []
        usable_paths: dict[str, Path] = {}

        for path in paths:
            source = portable_relative_path(path, game_directory)
            try:
                file_probe = _probe_file(path, source)
            except (OSError, ValueError) as exc:
                issues.append(f"{source}: {exc}")
                continue
            files.append(file_probe)
            usable_paths[source.casefold()] = path
            documents.append(
                NativeDocument(
                    source=source,
                    format_family=file_probe.role,
                    version_marker_hex=file_probe.version_marker_hex,
                    parse_scope="signature_only",
                    evidence_grade=file_probe.evidence_grade,
                )
            )

        mappings: list[NativeOracleMapping] = []
        correlations: list[NativeStringCorrelation] = []
        if oracle_directory is not None:
            if not oracle_directory.is_dir():
                raise NotADirectoryError(
                    f"WOLF Auto.txt oracle directory does not exist: {oracle_directory}"
                )
            oracle = WolfTextInspector().inspect(
                oracle_directory, fixture_kind="official_editor_export_oracle"
            )
            issues.extend(
                f"oracle {item.code}: {item.reason}"
                for item in oracle.issues
                if item.severity == "error"
            )
            records_by_source: dict[str, list[WolfTextRecord]] = {}
            for record in oracle.records:
                records_by_source.setdefault(record.source_file.casefold(), []).append(record)

            oracle_names = {
                info.source_file.casefold(): info.source_file for info in oracle.files
            }
            for file_probe in files:
                expected = _expected_oracle_source(file_probe.source)
                actual = oracle_names.get(expected.casefold()) if expected else None
                if actual is None:
                    mappings.append(
                        NativeOracleMapping(
                            native_source=file_probe.source,
                            oracle_source=None,
                            mapping_status="no_exact_filename_counterpart",
                            evidence_grade=EvidenceGrade.E,
                        )
                    )
                    continue
                records = records_by_source.get(actual.casefold(), [])
                mappings.append(
                    NativeOracleMapping(
                        native_source=file_probe.source,
                        oracle_source=actual,
                        mapping_status="exact_portable_filename",
                        oracle_record_count=len(records),
                        evidence_grade=EvidenceGrade.A,
                    )
                )
                native_path = usable_paths[file_probe.source.casefold()]
                if file_probe.size > MAX_CORRELATION_FILE_BYTES:
                    issues.append(
                        f"{file_probe.source}: correlation skipped above "
                        f"{MAX_CORRELATION_FILE_BYTES} bytes"
                    )
                    continue
                try:
                    native_data = native_path.read_bytes()
                except OSError as exc:
                    issues.append(f"{file_probe.source}: correlation read failed: {exc}")
                    continue
                correlations.extend(
                    _correlate_records(file_probe.source, actual, native_data, records)
                )

        return WolfNativeResearchReport(
            files=tuple(files),
            mappings=tuple(mappings),
            correlations=tuple(correlations),
            documents=tuple(documents),
            issues=tuple(issues),
            limits={
                "max_native_files": MAX_NATIVE_FILES,
                "max_native_file_bytes": MAX_NATIVE_FILE_BYTES,
                "max_correlation_file_bytes": MAX_CORRELATION_FILE_BYTES,
                "max_correlation_strings_per_file": MAX_CORRELATION_STRINGS_PER_FILE,
                "max_correlation_text_chars": MAX_CORRELATION_TEXT_CHARS,
                "max_match_offsets": MAX_MATCH_OFFSETS,
                "header_bytes": HEADER_BYTES,
            },
            source_path=".",
            oracle_path="oracle" if oracle_directory is not None else None,
        )


def correlate_known_strings(
    data: bytes,
    texts: Sequence[str],
) -> tuple[tuple[str, int, dict[str, tuple[int, ...]]], ...]:
    """Find bounded exact byte evidence in three plausible encodings.

    The return value contains a text hash and length, never source text.  A byte
    offset is evidence only and is not suitable for a canonical identifier.
    """

    results: list[tuple[str, int, dict[str, tuple[int, ...]]]] = []
    for text in texts[:MAX_CORRELATION_STRINGS_PER_FILE]:
        if not text or len(text) > MAX_CORRELATION_TEXT_CHARS:
            continue
        matches: dict[str, tuple[int, ...]] = {}
        for encoding in ("utf-8", "cp932", "utf-16le"):
            try:
                needle = text.encode(encoding)
            except UnicodeEncodeError:
                continue
            offsets = _find_offsets(data, needle)
            if offsets:
                matches[encoding] = offsets
        results.append(
            (hashlib.sha256(text.encode("utf-8")).hexdigest(), len(text), matches)
        )
    return tuple(results)


def _discover_native_files(root: Path) -> tuple[list[Path], list[str]]:
    candidates: list[Path] = []
    issues: list[str] = []
    search_roots = (root / "Data" / "BasicData", root / "Data" / "MapData")
    for search_root in search_roots:
        if not search_root.is_dir():
            continue
        for current, directory_names, file_names in os.walk(search_root, followlinks=False):
            current_path = Path(current)
            # Backups are not runtime native sources and can multiply evidence.
            directory_names[:] = [
                name
                for name in sorted(directory_names, key=str.casefold)
                if not name.casefold().startswith("autobackup")
                and not (current_path / name).is_symlink()
            ]
            for name in sorted(file_names, key=str.casefold):
                path = current_path / name
                if path.is_symlink() or path.suffix.casefold() not in {".dat", ".mps"}:
                    continue
                candidates.append(path)
                if len(candidates) >= MAX_NATIVE_FILES:
                    issues.append(f"native discovery stopped at {MAX_NATIVE_FILES} files")
                    return candidates, issues
    return candidates, issues


def _probe_file(path: Path, source: str) -> NativeFileProbe:
    size = path.stat().st_size
    if size > MAX_NATIVE_FILE_BYTES:
        raise ValueError(f"file exceeds read-only probe limit of {MAX_NATIVE_FILE_BYTES} bytes")
    digest = hashlib.sha256()
    frequencies: Counter[int] = Counter()
    header = b""
    total = 0
    with path.open("rb") as stream:
        while chunk := stream.read(IO_CHUNK_BYTES):
            if not header:
                header = chunk[:HEADER_BYTES]
            digest.update(chunk)
            frequencies.update(chunk)
            total += len(chunk)
    role = _role(source)
    signature, status, version_marker, grade, notes = _classify_signature(role, header)
    return NativeFileProbe(
        source=source,
        role=role,
        size=size,
        sha256=digest.hexdigest(),
        header_hex=header.hex(),
        entropy_bits_per_byte=round(_entropy(frequencies, total), 6),
        signature=signature,
        signature_status=status,
        version_marker_hex=version_marker,
        evidence_grade=grade,
        notes=notes,
    )


def _role(source: str) -> str:
    name = Path(source).name.casefold()
    if name == "game.dat":
        return "game_dat"
    if name == "commonevent.dat":
        return "common_event_dat"
    if name.endswith("database.dat"):
        return "database_dat"
    if name.endswith(".mps"):
        return "map_mps"
    return "other_dat"


def _classify_signature(
    role: str, header: bytes
) -> tuple[str, str, str | None, EvidenceGrade, tuple[str, ...]]:
    if role == "game_dat":
        matched = header.startswith(_GAME_MAGIC)
        marker = header[9:10].hex() if len(header) > 9 else None
        return (
            _GAME_MAGIC.hex(),
            "matched" if matched else "not_matched",
            marker,
            EvidenceGrade.B if matched else EvidenceGrade.E,
            ("magic matched local official sample and public format declaration",)
            if matched
            else (),
        )
    if role in {"common_event_dat", "database_dat"}:
        subtype = b"FC\x00" if role == "common_event_dat" else b"FM\x00"
        matched = (
            len(header) >= 10
            and header.startswith(_TABLE_MAGIC_PREFIX)
            and header[7:10] == subtype
        )
        marker = header[6:7].hex() if len(header) > 6 else None
        return (
            (_TABLE_MAGIC_PREFIX + b"?" + subtype).hex(),
            "matched" if matched else "not_matched",
            marker,
            EvidenceGrade.B if matched else EvidenceGrade.E,
            ("container signature matched local official sample and public format declaration",)
            if matched
            else (),
        )
    if role == "map_mps":
        matched = header.startswith(_MAP_MAGIC)
        marker = header[16:17].hex() if len(header) > 16 else None
        notes: tuple[str, ...] = ()
        if matched and len(header) > 24:
            notes = (
                "magic matched; later observed 3.682 markers differ from older public schema constraints",
            )
        return (
            _MAP_MAGIC.hex(),
            "matched" if matched else "not_matched",
            marker,
            EvidenceGrade.B if matched else EvidenceGrade.E,
            notes,
        )
    return ("unknown", "not_evaluated", None, EvidenceGrade.E, ())


def _expected_oracle_source(native_source: str) -> str | None:
    parts = Path(native_source).parts
    if len(parts) != 3 or parts[0].casefold() != "data":
        return None
    directory, name = parts[1], parts[2]
    if directory.casefold() == "basicdata":
        # Official output spells this one with a capital B in DataBase.
        if name.casefold() == "sysdatabase.dat":
            return "BasicData/SysDataBase.Auto.txt"
        if name.casefold() in {"cdatabase.dat", "database.dat"}:
            return f"BasicData/{name[:-4]}.Auto.txt"
        return f"BasicData/{name}.Auto.txt"
    if directory.casefold() == "mapdata" and name.casefold().endswith(".mps"):
        return f"MapData/{name}.Auto.txt"
    return None


def _correlate_records(
    native_source: str,
    oracle_source: str,
    native_data: bytes,
    records: Sequence[WolfTextRecord],
) -> Iterable[NativeStringCorrelation]:
    selected: list[WolfTextRecord] = []
    seen: set[str] = set()
    for record in records:
        text = record.original
        if not text or len(text) > MAX_CORRELATION_TEXT_CHARS or text in seen:
            continue
        seen.add(text)
        selected.append(record)
        if len(selected) >= MAX_CORRELATION_STRINGS_PER_FILE:
            break
    evidence = correlate_known_strings(native_data, [item.original for item in selected])
    for record, (text_hash, text_length, matches) in zip(selected, evidence):
        if not matches:
            continue
        yield NativeStringCorrelation(
            native_source=native_source,
            oracle_source=oracle_source,
            source_text_sha256=text_hash,
            source_text_length=text_length,
            matches=matches,
            logical_location=record.location.to_json_dict(),
            evidence_grade=EvidenceGrade.A,
        )


def _find_offsets(data: bytes, needle: bytes) -> tuple[int, ...]:
    if not needle:
        return ()
    offsets: list[int] = []
    start = 0
    while len(offsets) < MAX_MATCH_OFFSETS:
        offset = data.find(needle, start)
        if offset < 0:
            break
        offsets.append(offset)
        start = offset + 1
    return tuple(offsets)


def _entropy(frequencies: Counter[int], total: int) -> float:
    if total == 0:
        return 0.0
    return -sum(
        (count / total) * math.log2(count / total) for count in frequencies.values()
    )


__all__ = [
    "HEADER_BYTES",
    "MAX_CORRELATION_FILE_BYTES",
    "MAX_CORRELATION_STRINGS_PER_FILE",
    "MAX_NATIVE_FILE_BYTES",
    "MAX_NATIVE_FILES",
    "WolfNativeProbe",
    "correlate_known_strings",
    "write_native_research_report",
]
