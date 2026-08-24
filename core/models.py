"""Common data models shared by engine modules and user interfaces."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class EngineId(str, Enum):
    RPGMAKER_MV = "rpgmaker_mv"
    RPGMAKER_MZ = "rpgmaker_mz"
    WOLF_RPG_EDITOR = "wolf_rpg_editor"


ENGINE_DISPLAY_NAMES: dict[EngineId, str] = {
    EngineId.RPGMAKER_MV: "RPG Maker MV",
    EngineId.RPGMAKER_MZ: "RPG Maker MZ",
    EngineId.WOLF_RPG_EDITOR: "WOLF RPG Editor",
}


@dataclass(frozen=True, slots=True)
class DetectionResult:
    """An engine detection result with project-relative evidence paths."""

    engine: EngineId | None
    confidence: int
    evidence: tuple[str, ...] = ()
    detected: bool = False

    def __post_init__(self) -> None:
        if not 0 <= self.confidence <= 100:
            raise ValueError("confidence must be between 0 and 100")
        if self.detected and self.engine is None:
            raise ValueError("a detected result must identify an engine")

    @property
    def display_name(self) -> str:
        if self.engine is None or not self.detected:
            return "Unknown"
        return ENGINE_DISPLAY_NAMES[self.engine]

    @classmethod
    def unknown(
        cls,
        *,
        confidence: int = 0,
        evidence: tuple[str, ...] = (),
    ) -> "DetectionResult":
        return cls(
            engine=None,
            confidence=confidence,
            evidence=evidence,
            detected=False,
        )


@dataclass(frozen=True, slots=True)
class TranslationEntry:
    """A portable translation unit tied to an exact source location.

    ``extra_metadata`` lets future adapters preserve engine-specific fields
    without teaching common core code about their location model.  It is
    flattened during serialization and is empty for all legacy RPG Maker rows.
    """

    id: str
    engine: EngineId
    file: str
    type: str
    original: str
    translation: str = ""
    speaker: str | None = None
    json_path: str | None = None
    event_id: int | None = None
    page_id: int | None = None
    command_index: int | None = None
    parameter_index: int | None = None
    map_id: int | None = None
    map_name: str | None = None
    control_codes: tuple[str, ...] = ()
    extra_metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_json_dict(self) -> dict[str, object]:
        """Serialize required fields first and omit absent optional metadata."""

        payload: dict[str, object] = {
            "id": self.id,
            "engine": self.engine.value,
            "file": self.file,
            "type": self.type,
            "original": self.original,
            "translation": self.translation,
        }
        optional = (
            ("speaker", self.speaker),
            ("json_path", self.json_path),
            ("event_id", self.event_id),
            ("page_id", self.page_id),
            ("command_index", self.command_index),
            ("parameter_index", self.parameter_index),
            ("map_id", self.map_id),
            ("map_name", self.map_name),
        )
        for key, value in optional:
            if value is not None:
                payload[key] = value
        if self.control_codes:
            payload["control_codes"] = list(self.control_codes)
        collisions = set(payload) & set(self.extra_metadata)
        if collisions:
            raise ValueError(
                f"extra metadata conflicts with standard fields: {sorted(collisions)!r}"
            )
        payload.update(self.extra_metadata)
        return payload


@dataclass(frozen=True, slots=True)
class ExtractionIssue:
    """A recoverable problem encountered while reading a source file."""

    file: str
    message: str


@dataclass(slots=True)
class ExtractionResult:
    """Entries plus recoverable issues from a complete extraction pass."""

    entries: list[TranslationEntry] = field(default_factory=list)
    issues: list[ExtractionIssue] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ApplyIssue:
    """A warning, error, or source conflict produced during safe apply."""

    severity: str
    code: str
    reason: str
    id: str | None = None
    file: str | None = None
    json_path: str | None = None
    type: str | None = None
    original: str | None = None
    translation: str | None = None

    def to_json_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "severity": self.severity,
            "code": self.code,
            "reason": self.reason,
        }
        for key in ("id", "file", "json_path", "type", "original", "translation"):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        return payload


@dataclass(slots=True)
class ApplyReport:
    """Portable summary of one apply operation; contains no absolute paths."""

    engine: EngineId
    files_copied: int = 0
    modified_files: list[str] = field(default_factory=list)
    total_translation_entries: int = 0
    translated_entries: int = 0
    untranslated_entries: int = 0
    applicable: int = 0
    applied: int = 0
    skipped_untranslated: int = 0
    issues: list[ApplyIssue] = field(default_factory=list)
    planned_files: list[str] = field(default_factory=list)
    planned_ids: list[str] = field(default_factory=list)
    extra_metadata: dict[str, object] = field(default_factory=dict)

    @property
    def warnings(self) -> int:
        return sum(issue.severity == "warning" for issue in self.issues)

    @property
    def errors(self) -> int:
        return sum(issue.severity == "error" for issue in self.issues)

    @property
    def conflicts(self) -> int:
        return sum(issue.severity == "conflict" for issue in self.issues)

    def to_json_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "engine": self.engine.value,
            "files_copied": self.files_copied,
            "json_files_modified": len(self.modified_files),
            "modified_files": self.modified_files,
            "total_translation_entries": self.total_translation_entries,
            "translated": self.translated_entries,
            "untranslated": self.untranslated_entries,
            "applicable": self.applicable,
            "applied": self.applied,
            "skipped_untranslated": self.skipped_untranslated,
            "warnings": self.warnings,
            "errors": self.errors,
            "conflicts": self.conflicts,
            "planned_files": self.planned_files,
            "planned_ids": self.planned_ids,
            "issues": [issue.to_json_dict() for issue in self.issues],
        }
        collisions = set(payload) & set(self.extra_metadata)
        if collisions:
            raise ValueError(
                f"apply report metadata conflicts with standard fields: {sorted(collisions)!r}"
            )
        payload.update(self.extra_metadata)
        return payload
