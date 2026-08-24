"""WOLF adapter for inspection, official text-export round trips, and Projects."""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from core.archive import ArchiveReport
from core.engine import EnginePlugin, IssueProvider
from core.fingerprint import FingerprintFile, GameFingerprint
from core.models import ApplyIssue, ApplyReport, DetectionResult, EngineId, ExtractionResult
from core.qa import QaResult
from core.structure import StructureReport
from core.version import SCHEMA_VERSION, TOOL_VERSION
from engines.wolf.archive import WolfArchiveProbe
from engines.wolf.detector import WolfDetector
from engines.wolf.structure import WolfStructureInspector
from engines.wolf.text_inspector import WolfTextInspector
from engines.wolf.text_extractor import WolfExtractionResult, WolfTextExtractor
from engines.wolf.text_models import WolfTextReport
from engines.wolf.text_qa import WolfQaResult, WolfTextQa
from engines.wolf.text_writer import WolfTextWriter, WolfWriteReport
from core.translation_io import write_jsonl
from engines.wolf.editor_integration import (
    WolfEditorDetection,
    WolfEditorIntegrationResult,
    WolfEditorIntegrationValidator,
    WolfEditorLocator,
)
from engines.wolf.text_fingerprint import calculate_wolf_source_fingerprint
from engines.wolf.text_models import (
    WOLF_LOCATION_SCHEMA_STATUS,
    WOLF_LOCATION_SCHEMA_VERSION,
)


class WolfRPGEngine(EnginePlugin):
    """Keep native/archive mutation unsupported; write only copied .Auto.txt trees."""

    adapter_id = "wolf"
    supported_engines = frozenset({EngineId.WOLF_RPG_EDITOR})
    archive_extensions = frozenset({".wolf", ".wolfx", ".assets"})

    def detect(self, game_directory: Path) -> DetectionResult:
        return WolfDetector().detect(game_directory)

    def detect_project_source(self, source_directory: Path) -> DetectionResult:
        """Recognize an official ``.Auto.txt`` export as a Project source."""

        try:
            report = WolfTextInspector().inspect(source_directory)
        except (OSError, ValueError):
            return DetectionResult.unknown()
        evidence = tuple(item.source_file for item in report.files[:20])
        if report.file_count:
            has_errors = any(issue.severity == "error" for issue in report.issues)
            return DetectionResult(
                engine=EngineId.WOLF_RPG_EDITOR,
                confidence=90 if has_errors else 99,
                evidence=evidence,
                detected=True,
            )
        return DetectionResult.unknown(evidence=evidence)

    def project_source_mode(self, source_directory: Path) -> str:
        return "auto_txt"

    def extract_entries(self, game_directory: Path) -> ExtractionResult:
        return WolfTextExtractor().inspect_and_convert(game_directory)

    def project_extraction_errors(
        self,
        extraction: ExtractionResult,
    ) -> tuple[object, ...]:
        if not isinstance(extraction, WolfExtractionResult):
            return tuple(extraction.issues)
        return tuple(
            issue
            for issue in extraction.report.issues
            if issue.get("severity") == "error"
        )

    def project_metadata(
        self,
        source_directory: Path,
        extraction: ExtractionResult,
    ) -> Mapping[str, object]:
        inspection = WolfTextInspector().inspect(source_directory)
        observations: dict[tuple[str, str, str], int] = {}
        for item in inspection.files:
            key = (item.encoding, item.bom, item.newline_style)
            observations[key] = observations.get(key, 0) + 1
        report = (
            extraction.report
            if isinstance(extraction, WolfExtractionResult)
            else None
        )
        return {
            "wolf_location_schema": {
                "version": WOLF_LOCATION_SCHEMA_VERSION,
                "status": WOLF_LOCATION_SCHEMA_STATUS,
            },
            "source_fingerprint": (
                report.source_fingerprint if report is not None else ""
            ),
            "source_file_count": inspection.file_count,
            "extraction_summary": {
                "verified_entries": (
                    report.output_entries if report is not None else 0
                ),
                "experimental_excluded": (
                    report.experimental_excluded if report is not None else 0
                ),
                "unknown_records": (
                    report.unknown_records if report is not None else 0
                ),
            },
            "encoding_observations": [
                {
                    "encoding": encoding,
                    "bom": bom,
                    "newline_style": newline,
                    "file_count": count,
                }
                for (encoding, bom, newline), count in sorted(observations.items())
            ],
            "editor_validation": {
                "status": "not_recorded_for_auto_txt_source",
                "version": None,
                "sha256": None,
            },
            "verified_scope": {
                "event_commands": [101],
                "choice_option_command": 102,
                "choice_cancel_default": "unverified",
                "database_fields": ["dataname"],
                "experimental_records_included": False,
            },
            "limitations": {
                "choice": "option_text_verified; cancel_default_unverified",
                "database_description_help": "unsupported",
                "cp932_to_korean": "not_verified",
                "native_archive": "unsupported",
            },
        }

    def fingerprint(
        self,
        game_directory: Path,
        engine: EngineId,
    ) -> GameFingerprint:
        if engine is not EngineId.WOLF_RPG_EDITOR:
            raise ValueError(f"unsupported WOLF engine: {engine.value}")
        fingerprint = calculate_wolf_source_fingerprint(game_directory)
        return GameFingerprint(
            value=fingerprint.value,
            files=tuple(
                FingerprintFile(file=item.path, sha256=item.sha256)
                for item in fingerprint.files
            ),
        )

    def qa(
        self,
        game_directory: Path,
        translation_file: Path,
        reports_directory: Path,
        *,
        allowlist_path: Path | None = None,
        issue_provider: IssueProvider | None = None,
    ) -> QaResult:
        del allowlist_path  # WOLF Project QA currently uses structural/token checks.
        source = game_directory.resolve()
        if not self.detect_project_source(source).detected:
            raise self._unsupported("translation QA")
        reports = reports_directory.resolve()
        if reports == source or reports.is_relative_to(source):
            raise ValueError("WOLF Project reports cannot be inside the source directory")
        result = WolfTextQa().validate(source, translation_file)
        additional = _project_issues(translation_file, issue_provider)
        report = _common_qa_report(result, additional)
        percentage = (
            report.translated_entries / report.total_translation_entries * 100.0
            if report.total_translation_entries
            else 0.0
        )
        fingerprint = self.fingerprint(source, EngineId.WOLF_RPG_EDITOR)
        _write_project_qa_reports(reports, report, fingerprint, percentage)
        return QaResult(report, fingerprint, percentage, reports)

    def apply(
        self,
        game_directory: Path,
        translation_file: Path,
        output_directory: Path,
        *,
        dry_run: bool = False,
        allowlist_path: Path | None = None,
        issue_provider: IssueProvider | None = None,
    ) -> ApplyReport:
        del allowlist_path
        if not self.detect_project_source(game_directory).detected:
            raise self._unsupported("translation apply")
        preflight = WolfTextQa().validate(game_directory, translation_file)
        additional = _project_issues(translation_file, issue_provider)
        common_preflight = _common_qa_report(preflight, additional)
        if common_preflight.errors or common_preflight.conflicts:
            return common_preflight
        written = WolfTextWriter().apply(
            game_directory,
            translation_file,
            output_directory,
            dry_run=dry_run,
        )
        report = _common_write_report(written, additional)
        report.planned_ids = [item.id for item in preflight.changes]
        return report

    def inspect_structure(self, game_directory: Path) -> StructureReport:
        return WolfStructureInspector().inspect(game_directory)

    def inspect_archive(self, archive_file: Path) -> ArchiveReport:
        return WolfArchiveProbe().probe(archive_file)

    def inspect_text_export(self, export_directory: Path) -> WolfTextReport:
        return WolfTextInspector().inspect(export_directory)

    def extract_text_export(
        self, export_directory: Path, output_file: Path
    ) -> WolfExtractionResult:
        export_directory = export_directory.resolve()
        output_file = output_file.resolve()
        if output_file == export_directory or output_file.is_relative_to(
            export_directory
        ):
            raise ValueError("WOLF JSONL output cannot be inside the export directory")
        result = WolfTextExtractor().inspect_and_convert(export_directory)
        if not result.report.blocked:
            write_jsonl(result.entries, output_file, overwrite=False)
        return result

    def qa_text_export(
        self, export_directory: Path, translation_file: Path
    ) -> WolfQaResult:
        return WolfTextQa().validate(export_directory, translation_file)

    def apply_text_export(
        self,
        export_directory: Path,
        translation_file: Path,
        output_directory: Path,
        *,
        dry_run: bool = False,
    ) -> WolfWriteReport:
        return WolfTextWriter().apply(
            export_directory,
            translation_file,
            output_directory,
            dry_run=dry_run,
        )

    def check_editor(
        self, editor: Path | None, *, project: Path | None = None
    ) -> WolfEditorDetection:
        locator = WolfEditorLocator()
        resolved = locator.resolve(project, editor)
        return locator.check(
            resolved,
            project=project,
            provenance=("explicit_path" if editor is not None else "auto_resolved"),
        )

    def validate_editor_integration(
        self,
        project: Path,
        *,
        editor: Path | None = None,
        target: str = "ALL",
        allow_editor_import: bool = False,
        workspace: Path | None = None,
        keep_workspace: bool = False,
        timeout_seconds: int = 120,
    ) -> WolfEditorIntegrationResult:
        return WolfEditorIntegrationValidator().validate(
            project,
            editor=editor,
            target=target,
            allow_editor_import=allow_editor_import,
            workspace=workspace,
            keep_workspace=keep_workspace,
            timeout_seconds=timeout_seconds,
        )


__all__ = ["WolfRPGEngine"]


@dataclass(frozen=True, slots=True)
class _ProjectRecord:
    id: str
    file: str
    type: str
    original: str
    translation: str
    json_path: str | None = None


def _project_issues(
    translation_file: Path,
    issue_provider: IssueProvider | None,
) -> list[ApplyIssue]:
    if issue_provider is None:
        return []
    records: list[_ProjectRecord] = []
    try:
        lines = translation_file.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError):
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict) or not all(
            isinstance(value.get(name), str)
            for name in ("id", "file", "type", "original", "translation")
        ):
            continue
        records.append(
            _ProjectRecord(
                id=value["id"],
                file=value["file"],
                type=value["type"],
                original=value["original"],
                translation=value["translation"],
            )
        )
    return issue_provider(records)


def _common_issue(issue: object) -> ApplyIssue:
    severity = getattr(issue, "severity")
    return ApplyIssue(
        severity="conflict" if severity == "blocker" else severity,
        code=getattr(issue, "issue_code"),
        reason=getattr(issue, "reason"),
        id=getattr(issue, "id") or None,
        file=getattr(issue, "file") or None,
        type=getattr(issue, "type") or None,
        original=getattr(issue, "original") or None,
        translation=getattr(issue, "translation") or None,
    )


def _common_qa_report(
    result: WolfQaResult,
    additional: list[ApplyIssue],
) -> ApplyReport:
    return ApplyReport(
        engine=EngineId.WOLF_RPG_EDITOR,
        total_translation_entries=result.report.total_entries,
        translated_entries=result.report.translated_entries,
        untranslated_entries=result.report.untranslated_entries,
        applicable=result.report.applicable_entries,
        skipped_untranslated=result.report.untranslated_entries,
        issues=[*(_common_issue(item) for item in result.report.issues), *additional],
        planned_files=list(result.report.files_to_modify),
        planned_ids=[item.id for item in result.changes],
        extra_metadata={
            "source_mode": "auto_txt",
            "source_fingerprint": result.fingerprint.value,
            "output_fingerprint": "",
            "blockers": result.report.blocker_count,
            "modified_logical_sources": list(result.report.files_to_modify),
            "editor_import_performed": False,
        },
    )


def _common_write_report(
    result: WolfWriteReport,
    additional: list[ApplyIssue],
) -> ApplyReport:
    return ApplyReport(
        engine=EngineId.WOLF_RPG_EDITOR,
        files_copied=len(result.output_files) if not result.dry_run else 0,
        modified_files=list(result.modified_files),
        total_translation_entries=result.total_entries,
        translated_entries=result.translated_entries,
        untranslated_entries=result.untranslated_entries,
        applicable=result.applicable_entries,
        applied=result.applied_entries,
        skipped_untranslated=result.skipped_untranslated,
        issues=[*(_common_issue(item) for item in result.issues), *additional],
        planned_files=list(result.modified_files),
        planned_ids=[],
        extra_metadata={
            "source_mode": "auto_txt",
            "source_fingerprint": result.source_fingerprint,
            "output_fingerprint": result.output_fingerprint,
            "blockers": result.blockers,
            "modified_logical_sources": list(result.modified_files),
            "editor_import_performed": False,
        },
    )


def _write_project_qa_reports(
    reports: Path,
    report: ApplyReport,
    fingerprint: GameFingerprint,
    percentage: float,
) -> None:
    reports.mkdir(parents=True, exist_ok=True)
    payload = {
        "tool_version": TOOL_VERSION,
        "schema_version": SCHEMA_VERSION,
        "engine": EngineId.WOLF_RPG_EDITOR.value,
        "source_mode": "auto_txt",
        "game_fingerprint": fingerprint.value,
        "translation_percentage": round(percentage, 2),
        **report.to_json_dict(),
    }
    _atomic_text(
        reports / "qa_report.json",
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )

    issue_stream = io.StringIO(newline="")
    writer = csv.writer(issue_stream, lineterminator="\n")
    writer.writerow(
        ("id", "file", "json_path", "type", "original", "translation", "severity", "issue_code", "reason")
    )
    for issue in report.issues:
        writer.writerow(
            (
                issue.id or "",
                issue.file or "",
                issue.json_path or "",
                issue.type or "",
                issue.original or "",
                issue.translation or "",
                issue.severity,
                issue.code,
                issue.reason,
            )
        )
    _atomic_text(reports / "qa_issues.csv", issue_stream.getvalue())

    untranslated_stream = io.StringIO(newline="")
    writer = csv.writer(untranslated_stream, lineterminator="\n")
    writer.writerow(("id", "file", "type", "original"))
    for issue in report.issues:
        if issue.code == "EMPTY_TRANSLATION":
            writer.writerow((issue.id or "", issue.file or "", issue.type or "", issue.original or ""))
    _atomic_text(reports / "untranslated.csv", untranslated_stream.getvalue())

    generated_at = datetime.now(timezone.utc).isoformat()
    manifest = {
        "tool_version": TOOL_VERSION,
        "schema_version": SCHEMA_VERSION,
        "engine": EngineId.WOLF_RPG_EDITOR.value,
        "source_mode": "auto_txt",
        "game_fingerprint": fingerprint.value,
        "file_count": len(fingerprint.files),
        "fingerprint_file_count": len(fingerprint.files),
        "fingerprint_files": [item.to_json_dict() for item in fingerprint.files],
        "translation_entry_count": report.total_translation_entries,
        "extraction_timestamp": generated_at,
        "manifest_generated_at": generated_at,
        "wolf_location_schema": {
            "version": WOLF_LOCATION_SCHEMA_VERSION,
            "status": WOLF_LOCATION_SCHEMA_STATUS,
        },
    }
    _atomic_text(
        reports / "project_manifest.json",
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )


def _atomic_text(path: Path, text: str) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            delete=False,
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
