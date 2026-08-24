"""User-defined literal Glossary loading and non-mutating QA checks."""

from __future__ import annotations

import csv
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from core.engine import TranslationRecordView
from core.models import ApplyIssue
from projects.models import ProjectError


GLOSSARY_HEADER = ("source", "target", "type", "locked")


@dataclass(frozen=True, slots=True)
class GlossaryEntry:
    source: str
    target: str
    type: str
    locked: bool


def load_glossary(path: Path) -> list[GlossaryEntry]:
    try:
        stream = path.open("r", encoding="utf-8-sig", newline="")
    except (OSError, UnicodeError) as exc:
        raise ProjectError(f"cannot read glossary.csv: {exc}") from exc
    entries: list[GlossaryEntry] = []
    with stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != list(GLOSSARY_HEADER):
            raise ProjectError(
                f"glossary.csv header must be {','.join(GLOSSARY_HEADER)}"
            )
        for line_number, row in enumerate(reader, start=2):
            source = (row.get("source") or "").strip()
            target = (row.get("target") or "").strip()
            entry_type = (row.get("type") or "").strip()
            locked_text = (row.get("locked") or "").strip().casefold()
            if not source:
                raise ProjectError(f"glossary.csv line {line_number}: source is required")
            if locked_text not in {"true", "false"}:
                raise ProjectError(
                    f"glossary.csv line {line_number}: locked must be true or false"
                )
            if locked_text == "true" and not target:
                raise ProjectError(
                    f"glossary.csv line {line_number}: locked target is required"
                )
            entries.append(
                GlossaryEntry(
                    source=source,
                    target=target,
                    type=entry_type,
                    locked=locked_text == "true",
                )
            )
    return entries


def glossary_issues(
    records: Sequence[TranslationRecordView],
    glossary: list[GlossaryEntry],
) -> list[ApplyIssue]:
    """Warn only for user-registered locked literal terms."""

    issues: list[ApplyIssue] = []
    locked_entries = [entry for entry in glossary if entry.locked]
    for record in records:
        if not record.translation.strip():
            continue
        for entry in locked_entries:
            if entry.source not in record.original:
                continue
            if entry.target in record.translation:
                continue
            issues.append(
                ApplyIssue(
                    severity="warning",
                    code="GLOSSARY_MISMATCH",
                    reason=(
                        f"locked glossary target {entry.target!r} is missing for "
                        f"source term {entry.source!r}"
                    ),
                    id=record.id,
                    file=record.file,
                    json_path=record.json_path,
                    type=record.type,
                    original=record.original,
                    translation=record.translation,
                )
            )
    return issues


def inconsistent_translation_issues(
    records: Sequence[TranslationRecordView],
) -> list[ApplyIssue]:
    """Warn when an exact original has multiple non-empty translations."""

    grouped: dict[str, dict[str, TranslationRecordView]] = {}
    for record in records:
        translation = record.translation.strip()
        if not translation:
            continue
        grouped.setdefault(record.original, {}).setdefault(translation, record)

    issues: list[ApplyIssue] = []
    for original, translations in grouped.items():
        if len(translations) < 2:
            continue
        representative = next(iter(translations.values()))
        issues.append(
            ApplyIssue(
                severity="warning",
                code="INCONSISTENT_TRANSLATION",
                reason=(
                    "same original has multiple translations: "
                    f"{sorted(translations)!r}"
                ),
                id=representative.id,
                file=representative.file,
                json_path=representative.json_path,
                type=representative.type,
                original=original,
                translation=representative.translation,
            )
        )
    return issues
