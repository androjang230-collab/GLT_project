"""Independent read-only QA reports for RPG Maker translation projects."""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from core.models import ApplyIssue, ApplyReport, EngineId
from core.qa import QaResult
from core.reports import write_dry_run_report
from core.version import SCHEMA_VERSION, TOOL_VERSION
from engines.rpgmaker.fingerprint import GameFingerprint, calculate_game_fingerprint
from engines.rpgmaker.inserter import (
    ApplySafetyError,
    PreflightResult,
    RpgMakerInserter,
)
from engines.rpgmaker.validator import JapaneseAllowlist, detect_japanese_scripts

class RpgMakerQa:
    """Run shared preflight checks and write portable QA artifacts."""

    def __init__(self, engine: EngineId) -> None:
        self.engine = engine

    def run(
        self,
        game_directory: Path,
        translation_file: Path,
        reports_directory: Path,
        *,
        allowlist: JapaneseAllowlist | None = None,
    ) -> QaResult:
        preflight = RpgMakerInserter(self.engine).preflight(
            game_directory,
            translation_file,
            allowlist=allowlist,
        )
        return self.write_preflight_reports(
            game_directory,
            reports_directory,
            preflight,
            allowlist=allowlist,
        )

    def write_preflight_reports(
        self,
        game_directory: Path,
        reports_directory: Path,
        preflight: PreflightResult,
        *,
        allowlist: JapaneseAllowlist | None = None,
    ) -> QaResult:
        """Write QA artifacts for a preflight enriched by project rules."""

        game_directory = game_directory.resolve()
        reports_directory = reports_directory.resolve()
        if reports_directory == game_directory or reports_directory.is_relative_to(
            game_directory
        ):
            raise ApplySafetyError(
                "reports directory cannot be inside the original game directory"
            )
        fingerprint = calculate_game_fingerprint(game_directory, self.engine)
        percentage = _translation_percentage(preflight.report)
        self._write_reports(
            game_directory,
            reports_directory,
            preflight,
            fingerprint,
            percentage,
            allowlist,
        )
        return QaResult(
            report=preflight.report,
            fingerprint=fingerprint,
            translation_percentage=percentage,
            reports_directory=reports_directory,
        )

    def _write_reports(
        self,
        game_directory: Path,
        reports_directory: Path,
        preflight: PreflightResult,
        fingerprint: GameFingerprint,
        percentage: float,
        allowlist: JapaneseAllowlist | None,
    ) -> None:
        reports_directory.mkdir(parents=True, exist_ok=True)
        report = preflight.report
        japanese_stats = _japanese_statistics(preflight, allowlist)
        qa_payload: dict[str, object] = {
            "tool_version": TOOL_VERSION,
            "schema_version": SCHEMA_VERSION,
            "engine": self.engine.value,
            "game_fingerprint": fingerprint.value,
            "total_entries": report.total_translation_entries,
            "translated": report.translated_entries,
            "untranslated": report.untranslated_entries,
            "translation_percentage": percentage,
            "applicable": report.applicable,
            "warnings": report.warnings,
            "errors": report.errors,
            "conflicts": report.conflicts,
            "japanese_remains": japanese_stats["japanese_remains"],
            "hiragana_remains": japanese_stats["hiragana_remains"],
            "katakana_remains": japanese_stats["katakana_remains"],
            "kanji_only": japanese_stats["kanji_only"],
            "control_code_errors": _issue_count(
                report,
                {"CONTROL_CODE_MISMATCH", "CONTROL_CODE_METADATA_MISMATCH"},
            ),
            "source_mismatches": _issue_count(report, {"SOURCE_TEXT_MISMATCH"}),
            "glossary_warnings": _issue_count(report, {"GLOSSARY_MISMATCH"}),
            "inconsistent_translations": _issue_count(
                report,
                {"INCONSISTENT_TRANSLATION"},
            ),
            "fingerprint_conflicts": _issue_count(
                report,
                {"GAME_FINGERPRINT_MISMATCH"},
            ),
            "issues": [_qa_issue_payload(issue) for issue in report.issues],
        }
        _write_json_atomic(reports_directory / "qa_report.json", qa_payload)
        _write_issues_csv(reports_directory / "qa_issues.csv", report.issues)
        _write_untranslated_csv(
            reports_directory / "untranslated.csv",
            preflight,
        )

        generated_at = datetime.now(timezone.utc).isoformat()
        manifest = {
            "tool_version": TOOL_VERSION,
            "schema_version": SCHEMA_VERSION,
            "engine": self.engine.value,
            "game_fingerprint": fingerprint.value,
            "file_count": sum(
                1 for path in game_directory.rglob("*") if path.is_file()
            ),
            "fingerprint_file_count": len(fingerprint.files),
            "fingerprint_files": [item.to_json_dict() for item in fingerprint.files],
            "translation_entry_count": report.total_translation_entries,
            "extraction_timestamp": generated_at,
            "manifest_generated_at": generated_at,
            "allowlist_entry_count": len(allowlist.entries) if allowlist else 0,
        }
        _write_json_atomic(reports_directory / "project_manifest.json", manifest)


def _translation_percentage(report: ApplyReport) -> float:
    if report.total_translation_entries == 0:
        return 0.0
    return round(
        report.translated_entries / report.total_translation_entries * 100,
        2,
    )


def _japanese_statistics(
    preflight: PreflightResult,
    allowlist: JapaneseAllowlist | None,
) -> dict[str, int]:
    hiragana = 0
    katakana = 0
    kanji_only = 0
    japanese = 0
    for record in preflight.records:
        if not record.translation.strip():
            continue
        if allowlist is not None and allowlist.allows(record.translation):
            continue
        scripts = detect_japanese_scripts(record.translation)
        if scripts.hiragana:
            hiragana += 1
        if scripts.katakana:
            katakana += 1
        if scripts.kana:
            japanese += 1
        elif scripts.cjk_kanji:
            kanji_only += 1
    return {
        "japanese_remains": japanese,
        "hiragana_remains": hiragana,
        "katakana_remains": katakana,
        "kanji_only": kanji_only,
    }


def _issue_count(report: ApplyReport, codes: set[str]) -> int:
    return sum(issue.code in codes for issue in report.issues)


def _qa_issue_payload(issue: ApplyIssue) -> dict[str, object]:
    payload = issue.to_json_dict()
    payload["issue_code"] = payload.pop("code")
    return payload


def _write_issues_csv(path: Path, issues: list[ApplyIssue]) -> None:
    fieldnames = (
        "id",
        "file",
        "json_path",
        "type",
        "original",
        "translation",
        "severity",
        "issue_code",
        "reason",
    )
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fieldnames)
    writer.writeheader()
    for issue in issues:
        writer.writerow(
            {
                "id": issue.id or "",
                "file": issue.file or "",
                "json_path": issue.json_path or "",
                "type": issue.type or "",
                "original": issue.original or "",
                "translation": issue.translation or "",
                "severity": issue.severity,
                "issue_code": issue.code,
                "reason": issue.reason,
            }
        )
    _write_bytes_atomic(path, stream.getvalue().encode("utf-8-sig"))


def _write_untranslated_csv(path: Path, preflight: PreflightResult) -> None:
    fieldnames = ("id", "file", "json_path", "type", "original", "translation")
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fieldnames)
    writer.writeheader()
    for record in preflight.records:
        if record.translation.strip():
            continue
        writer.writerow(
            {
                "id": record.id,
                "file": record.file,
                "json_path": record.json_path,
                "type": record.type,
                "original": record.original,
                "translation": record.translation,
            }
        )
    _write_bytes_atomic(path, stream.getvalue().encode("utf-8-sig"))


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
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
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
