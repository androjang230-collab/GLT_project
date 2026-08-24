"""Portable models for bounded, read-only archive reconnaissance."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class ArchiveFormatInfo:
    """Evidence catalog that keeps facts separate from hypotheses and gaps."""

    format: str
    extensions: tuple[str, ...]
    header: str | None
    generation: str
    encryption: str
    compression: str
    confidence: int
    verified: tuple[str, ...] = ()
    probable: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not 0 <= self.confidence <= 100:
            raise ValueError("confidence must be between 0 and 100")

    def to_json_dict(self) -> dict[str, object]:
        return {
            "format": self.format,
            "extensions": list(self.extensions),
            "header": self.header,
            "generation": self.generation,
            "encryption": self.encryption,
            "compression": self.compression,
            "confidence": self.confidence,
            "knowledge": {
                "verified": list(self.verified),
                "probable": list(self.probable),
                "unknown": list(self.unknown),
            },
            "sources": list(self.sources),
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class ArchiveReport:
    """Engine-neutral result containing no machine-specific absolute path."""

    path: str
    relative_path: str
    size: int
    extension: str
    archive_type: str
    header_hex: str
    tail_hex: str
    packaging: str
    encryption_status: str
    version: str | None
    version_confidence: str
    confidence: int
    evidence: tuple[str, ...] = ()
    executable_type: str = "unknown"
    executable_file: str | None = None
    companion_files: tuple[str, ...] = ()
    entry_listing_supported: bool = False
    entry_count: int | None = None
    entries: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    extra_metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if Path(self.path).is_absolute() or Path(self.relative_path).is_absolute():
            raise ValueError("archive report paths must not be absolute")
        if not 0 <= self.confidence <= 100:
            raise ValueError("confidence must be between 0 and 100")
        if not self.entry_listing_supported and (
            self.entry_count is not None or self.entries
        ):
            raise ValueError("unsupported entry listing cannot contain entries")

    def to_json_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "path": self.path,
            "relative_path": self.relative_path,
            "size": self.size,
            "extension": self.extension,
            "archive_type": self.archive_type,
            "header_hex": self.header_hex,
            "tail_hex": self.tail_hex,
            "packaging": self.packaging,
            "encryption_status": self.encryption_status,
            "version": self.version,
            "version_confidence": self.version_confidence,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "executable_type": self.executable_type,
            "executable_file": self.executable_file,
            "companion_files": list(self.companion_files),
            "entry_listing_supported": self.entry_listing_supported,
            "entry_count": self.entry_count,
            "entries": list(self.entries),
            "notes": list(self.notes),
        }
        collisions = set(payload) & set(self.extra_metadata)
        if collisions:
            raise ValueError(
                f"extra metadata conflicts with archive fields: {sorted(collisions)!r}"
            )
        payload.update(self.extra_metadata)
        return payload


def write_archive_report(path: Path, report: ArchiveReport) -> Path:
    """Atomically create a new JSON report without replacing an existing one."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"archive report already exists: {path}")
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


__all__ = ["ArchiveFormatInfo", "ArchiveReport", "write_archive_report"]
