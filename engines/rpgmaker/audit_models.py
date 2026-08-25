"""Read-only RPG Maker translation-coverage audit models."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class TranslationClassification(str, Enum):
    VERIFIED_TRANSLATABLE = "VERIFIED_TRANSLATABLE"
    CONDITIONAL_TRANSLATABLE = "CONDITIONAL_TRANSLATABLE"
    MIRROR = "MIRROR"
    INTERNAL = "INTERNAL"
    UNSAFE = "UNSAFE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class AuditIssue:
    severity: str
    code: str
    file: str
    reason: str


@dataclass(frozen=True, slots=True)
class StringCandidate:
    file: str
    json_path: str
    event_context: str
    command_index: int
    command_code: int
    parameter_path: str
    classification: str
    role: str
    evidence: str
    value_sha256: str
    value_length: int
    hiragana: bool
    katakana: bool
    cjk_kanji: bool
    control_codes: tuple[str, ...] = ()
    plugin_name: str | None = None
    command_name: str | None = None
    argument_path: str | None = None
    rule_id: str | None = None
    display_api: str | None = None


@dataclass(frozen=True, slots=True)
class MirrorObservation:
    file: str
    event_context: str
    source_command_index: int | None
    mirror_command_index: int
    source_code: int
    mirror_code: int
    choice_index: int | None
    relation: str
    confidence: str
    values_match: bool | None


@dataclass(frozen=True, slots=True)
class PluginInventory:
    name: str
    enabled: bool
    source_file: str | None
    registered_commands: tuple[str, ...] = ()
    declared_commands: tuple[str, ...] = ()
    text_like_arguments: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DatabaseFieldObservation:
    file: str
    json_path: str
    role: str
    classification: str
    current_extract: bool
    occurrences: int
    evidence: str


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    file_count: int
    total_size: int
    selected_content_sha256: str


@dataclass(slots=True)
class RpgMakerCoverageReport:
    tool_version: str
    report_schema_version: int
    engine: str
    data_path: str
    plugin_metadata_available: bool
    source_before: SourceSnapshot
    source_after: SourceSnapshot | None = None
    event_commands: list[dict[str, Any]] = field(default_factory=list)
    move_route_commands: list[dict[str, Any]] = field(default_factory=list)
    candidates: list[StringCandidate] = field(default_factory=list)
    mirrors: list[MirrorObservation] = field(default_factory=list)
    plugins: list[PluginInventory] = field(default_factory=list)
    database_fields: list[DatabaseFieldObservation] = field(default_factory=list)
    issues: list[AuditIssue] = field(default_factory=list)
    statistics: dict[str, Any] = field(default_factory=dict)
    actual_files_scanned: tuple[str, ...] = ()

    @property
    def source_unchanged(self) -> bool:
        return self.source_after == self.source_before

    def to_json_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["source_unchanged"] = self.source_unchanged
        payload["privacy"] = {
            "raw_game_text_persisted": False,
            "absolute_paths_persisted": False,
        }
        return payload


__all__ = [
    "AuditIssue",
    "DatabaseFieldObservation",
    "MirrorObservation",
    "PluginInventory",
    "RpgMakerCoverageReport",
    "SourceSnapshot",
    "StringCandidate",
    "TranslationClassification",
]
