"""Exact-match UTF-8 JSONL Translation Memory operations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.models import ApplyIssue
from projects.io import read_jsonl, write_jsonl
from projects.models import ProjectError, TmOperationResult


@dataclass(frozen=True, slots=True)
class TranslationMemoryEntry:
    original: str
    translation: str
    type: str
    approved: bool
    speaker: str | None = None
    notes: str | None = None

    def to_json_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "original": self.original,
            "translation": self.translation,
            "type": self.type,
            "approved": self.approved,
        }
        if self.speaker is not None:
            payload["speaker"] = self.speaker
        if self.notes is not None:
            payload["notes"] = self.notes
        return payload


def tm_fill(translation_file: Path, tm_file: Path) -> TmOperationResult:
    translation_records = read_jsonl(translation_file)
    memory = _load_tm(tm_file)
    approved: dict[str, set[str]] = {}
    for entry in memory:
        if entry.approved and entry.translation.strip():
            approved.setdefault(entry.original, set()).add(entry.translation)

    issues: list[ApplyIssue] = []
    usable: dict[str, str] = {}
    for original, translations in approved.items():
        if len(translations) == 1:
            usable[original] = next(iter(translations))
        else:
            issues.append(
                ApplyIssue(
                    severity="conflict",
                    code="TM_TRANSLATION_CONFLICT",
                    reason=(
                        f"approved TM has multiple translations for {original!r}: "
                        f"{sorted(translations)!r}"
                    ),
                    original=original,
                )
            )

    matches = 0
    filled = 0
    skipped_existing = 0
    changed = False
    for record in translation_records:
        original = record.get("original")
        translation = record.get("translation")
        if not isinstance(original, str) or not isinstance(translation, str):
            raise ProjectError("translated.jsonl entries require string original/translation")
        target = usable.get(original)
        if target is None:
            continue
        matches += 1
        if translation.strip():
            skipped_existing += 1
            continue
        record["translation"] = target
        filled += 1
        changed = True
    if changed:
        write_jsonl(translation_file, translation_records)
    return TmOperationResult(
        matches=matches,
        filled=filled,
        skipped_existing=skipped_existing,
        issues=tuple(issues),
    )


def tm_update(translation_file: Path, tm_file: Path) -> TmOperationResult:
    translation_records = read_jsonl(translation_file)
    memory = _load_tm(tm_file)
    existing_pairs = {(entry.original, entry.translation) for entry in memory}
    existing_by_original: dict[str, set[str]] = {}
    for entry in memory:
        existing_by_original.setdefault(entry.original, set()).add(entry.translation)

    candidates: dict[str, dict[str, dict[str, Any]]] = {}
    for record in translation_records:
        original = record.get("original")
        translation = record.get("translation")
        entry_type = record.get("type")
        if not all(isinstance(value, str) for value in (original, translation, entry_type)):
            raise ProjectError("translated.jsonl entries require original/translation/type")
        if not translation.strip():
            continue
        candidates.setdefault(original, {}).setdefault(translation, record)

    additions: list[TranslationMemoryEntry] = []
    issues: list[ApplyIssue] = []
    duplicates = 0
    for original, translations in candidates.items():
        combined = set(translations) | existing_by_original.get(original, set())
        if len(combined) > 1:
            representative = next(iter(translations.values()))
            issues.append(
                ApplyIssue(
                    severity="conflict",
                    code="TM_TRANSLATION_CONFLICT",
                    reason=(
                        f"original {original!r} has conflicting translations: "
                        f"{sorted(combined)!r}"
                    ),
                    original=original,
                    translation=representative["translation"],
                    type=representative["type"],
                )
            )
            continue
        translation, record = next(iter(translations.items()))
        if (original, translation) in existing_pairs:
            duplicates += 1
            continue
        speaker = record.get("speaker")
        additions.append(
            TranslationMemoryEntry(
                original=original,
                translation=translation,
                type=record["type"],
                approved=True,
                speaker=speaker if isinstance(speaker, str) else None,
            )
        )
    if additions:
        payloads = [entry.to_json_dict() for entry in memory]
        payloads.extend(entry.to_json_dict() for entry in additions)
        write_jsonl(tm_file, payloads)
    return TmOperationResult(
        added=len(additions),
        duplicates=duplicates,
        issues=tuple(issues),
    )


def _load_tm(path: Path) -> list[TranslationMemoryEntry]:
    records = read_jsonl(path)
    entries: list[TranslationMemoryEntry] = []
    for index, record in enumerate(records, start=1):
        required = ("original", "translation", "type")
        if not all(isinstance(record.get(field), str) for field in required):
            raise ProjectError(f"translation_memory.jsonl entry {index} has invalid fields")
        if not isinstance(record.get("approved"), bool):
            raise ProjectError(
                f"translation_memory.jsonl entry {index}: approved must be boolean"
            )
        speaker = record.get("speaker")
        notes = record.get("notes")
        if speaker is not None and not isinstance(speaker, str):
            raise ProjectError(f"translation_memory.jsonl entry {index}: invalid speaker")
        if notes is not None and not isinstance(notes, str):
            raise ProjectError(f"translation_memory.jsonl entry {index}: invalid notes")
        entries.append(
            TranslationMemoryEntry(
                original=record["original"],
                translation=record["translation"],
                type=record["type"],
                approved=record["approved"],
                speaker=speaker,
                notes=notes,
            )
        )
    return entries
