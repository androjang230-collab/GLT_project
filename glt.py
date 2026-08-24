"""Game Localization Toolkit command-line entry point."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Sequence

from core.archive import ArchiveReport, write_archive_report
from core.logging import configure_logging, get_logger
from core.models import ApplyReport, DetectionResult, ENGINE_DISPLAY_NAMES, EngineId
from core.paths import (
    resolve_input_directory,
    resolve_input_file,
    resolve_new_directory,
    resolve_output_file,
)
from core.reports import write_dry_run_report
from core.structure import StructureReport, write_structure_report
from core.version import TOOL_VERSION
from engines.registry import create_engine_registry
from engines.wolf.archive import find_wolf_game_root
from engines.wolf.text_models import WolfTextReport, write_wolf_text_report
from engines.wolf.text_extractor import (
    WolfExtractionResult,
    write_wolf_extraction_report,
)
from engines.wolf.text_qa import WolfQaResult, write_wolf_qa_report
from engines.wolf.text_writer import WolfWriteReport, write_wolf_apply_report
from projects.manager import ProjectManager
from projects.models import ProjectError, ProjectValidationError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="glt",
        description=f"Game Localization Toolkit {TOOL_VERSION}",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="show diagnostic logging",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    detect_parser = subparsers.add_parser(
        "detect",
        help="detect the game engine",
    )
    detect_parser.add_argument(
        "game_directory",
        type=Path,
        help="game directory (absolute or relative to the current directory)",
    )

    extract_parser = subparsers.add_parser(
        "extract",
        help="extract RPG Maker MV/MZ text to UTF-8 JSONL",
    )
    extract_parser.add_argument(
        "game_directory",
        type=Path,
        help="game directory (absolute or relative to the current directory)",
    )
    extract_parser.add_argument(
        "--output",
        type=Path,
        default=Path("source.jsonl"),
        help="output JSONL path (default: ./source.jsonl)",
    )

    apply_parser = subparsers.add_parser(
        "apply",
        help="safely apply translated JSONL to a copied game directory",
    )
    apply_parser.add_argument(
        "game_directory",
        type=Path,
        help="original game directory (read-only source)",
    )
    apply_parser.add_argument(
        "translation_jsonl",
        type=Path,
        help="translated UTF-8 JSONL file",
    )
    apply_parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="new output directory; it must not already exist",
    )
    apply_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run the same preflight checks without creating output",
    )
    apply_parser.add_argument(
        "--allowlist",
        type=Path,
        help="UTF-8 Japanese literal-substring allowlist",
    )
    apply_parser.add_argument(
        "--reports",
        type=Path,
        default=Path("reports"),
        help="dry-run report directory (default: ./reports)",
    )

    qa_parser = subparsers.add_parser(
        "qa",
        help="validate a translation project without modifying the game",
    )
    qa_parser.add_argument("game_directory", type=Path)
    qa_parser.add_argument("translation_jsonl", type=Path)
    qa_parser.add_argument(
        "--reports",
        type=Path,
        default=Path("reports"),
        help="QA report directory (default: ./reports)",
    )
    qa_parser.add_argument(
        "--allowlist",
        type=Path,
        help="UTF-8 Japanese literal-substring allowlist",
    )

    inspect_parser = subparsers.add_parser(
        "inspect",
        help="inspect a detected engine's game structure without modifying it",
    )
    inspect_parser.add_argument("game_directory", type=Path)
    inspect_parser.add_argument(
        "--json",
        dest="json_output",
        type=Path,
        metavar="REPORT",
        help="create a portable machine-readable JSON report",
    )

    archive_parser = subparsers.add_parser(
        "inspect-archive",
        help="run a bounded read-only probe of a WOLF archive candidate",
    )
    archive_parser.add_argument("archive_file", type=Path)
    archive_parser.add_argument(
        "--json",
        dest="json_output",
        type=Path,
        metavar="REPORT",
        help="create a portable machine-readable JSON report outside the game",
    )

    text_parser = subparsers.add_parser(
        "wolf-text-inspect",
        help="inspect official WOLF .Auto.txt exports without modifying them",
    )
    text_parser.add_argument("export_directory", type=Path)
    text_parser.add_argument(
        "--json",
        dest="json_output",
        type=Path,
        metavar="REPORT",
        help="create a portable machine-readable JSON report outside the export",
    )

    text_extract_parser = subparsers.add_parser(
        "wolf-text-extract",
        help="extract verified WOLF .Auto.txt records to common GLT JSONL",
    )
    text_extract_parser.add_argument("export_directory", type=Path)
    text_extract_parser.add_argument("--output", type=Path, required=True)
    text_extract_parser.add_argument(
        "--report",
        type=Path,
        metavar="REPORT",
        help="create a portable JSON extraction report outside the export",
    )

    wolf_qa_parser = subparsers.add_parser(
        "wolf-text-qa",
        help="validate translated WOLF JSONL against an unchanged .Auto.txt export",
    )
    wolf_qa_parser.add_argument("translation_jsonl", type=Path)
    wolf_qa_parser.add_argument("--source", type=Path, required=True)
    wolf_qa_parser.add_argument(
        "--report",
        type=Path,
        default=Path("reports/wolf_qa_report.json"),
        help="portable QA report (default: ./reports/wolf_qa_report.json)",
    )

    wolf_apply_parser = subparsers.add_parser(
        "wolf-text-apply",
        help="create a translated copy of a WOLF .Auto.txt export directory",
    )
    wolf_apply_parser.add_argument("translation_jsonl", type=Path)
    wolf_apply_parser.add_argument("--source", type=Path, required=True)
    wolf_apply_parser.add_argument("--output", type=Path, required=True)
    wolf_apply_parser.add_argument("--dry-run", action="store_true")
    wolf_apply_parser.add_argument(
        "--report",
        type=Path,
        default=Path("reports/wolf_apply_report.json"),
        help="portable write report (default: ./reports/wolf_apply_report.json)",
    )

    font_check_parser = subparsers.add_parser(
        "font-check",
        help="inspect RPG Maker font files, references, and Korean glyph support",
    )
    font_check_parser.add_argument("game_directory", type=Path)
    font_check_parser.add_argument(
        "--reports",
        type=Path,
        default=Path("reports"),
        help="font report directory (default: ./reports)",
    )

    font_patch_parser = subparsers.add_parser(
        "font-patch",
        help="copy a game and safely patch its default font reference",
    )
    font_patch_parser.add_argument("game_directory", type=Path)
    font_patch_parser.add_argument("--font", type=Path, required=True)
    font_patch_parser.add_argument("--output", type=Path, required=True)
    font_patch_parser.add_argument("--dry-run", action="store_true")
    font_patch_parser.add_argument(
        "--reports",
        type=Path,
        default=Path("reports"),
        help="font patch report directory (default: ./reports)",
    )

    project_parser = subparsers.add_parser(
        "project",
        help="manage a portable game translation project",
    )
    project_commands = project_parser.add_subparsers(
        dest="project_command",
        required=True,
    )

    project_create = project_commands.add_parser("create")
    project_create.add_argument("game_directory", type=Path)
    project_create.add_argument("--output", type=Path, required=True)

    project_qa = project_commands.add_parser("qa")
    project_qa.add_argument("project_directory", type=Path)
    project_qa.add_argument("game_directory", type=Path)

    project_apply = project_commands.add_parser("apply")
    project_apply.add_argument("project_directory", type=Path)
    project_apply.add_argument("game_directory", type=Path)
    project_apply.add_argument("--output", type=Path, required=True)
    project_apply.add_argument("--dry-run", action="store_true")

    project_tm_fill = project_commands.add_parser("tm-fill")
    project_tm_fill.add_argument("project_directory", type=Path)

    project_tm_update = project_commands.add_parser("tm-update")
    project_tm_update.add_argument("project_directory", type=Path)

    project_font_check = project_commands.add_parser("font-check")
    project_font_check.add_argument("project_directory", type=Path)
    project_font_check.add_argument("game_directory", type=Path)

    project_font_patch = project_commands.add_parser("font-patch")
    project_font_patch.add_argument("project_directory", type=Path)
    project_font_patch.add_argument("game_directory", type=Path)
    project_font_patch.add_argument("--font", type=Path, required=True)
    project_font_patch.add_argument("--output", type=Path, required=True)
    project_font_patch.add_argument("--dry-run", action="store_true")
    return parser


def _print_detection(result: DetectionResult) -> None:
    # Kept separate from detection so another UI can reuse the same engine result.
    print(f"Detected Engine: {result.display_name}")
    print(f"Confidence: {result.confidence}%")
    print()
    print("Evidence:")
    if result.evidence:
        for evidence in result.evidence:
            print(f"- {evidence}")
    else:
        print("- No recognized engine files")


def _resolve_allowlist(explicit_path: Path | None) -> Path | None:
    if explicit_path is not None:
        return resolve_input_file(explicit_path)
    default_path = Path.cwd() / "config" / "japanese_allowlist.txt"
    return default_path.resolve() if default_path.is_file() else None


def _log_apply_issues(report: ApplyReport, logger: logging.Logger) -> None:
    for issue in report.issues:
        message = (
            f"{issue.code}: id={issue.id or '-'} "
            f"file={issue.file or '-'} path={issue.json_path or '-'}: "
            f"{issue.reason}"
        )
        if issue.severity == "warning":
            logger.warning("%s", message)
        elif issue.severity == "info":
            logger.info("%s", message)
        else:
            logger.error("%s", message)


def _print_font_report(report: object) -> None:
    print(f"Detected Engine: {ENGINE_DISPLAY_NAMES[report.engine]}")
    print()
    print("Default font:")
    print(report.default_font or "- Not identified")
    default_info = report.default_font_info
    support = (
        "UNKNOWN"
        if default_info is None or default_info.glyph_support is None
        else (
            f"{default_info.glyph_support.hangul_coverage_status} "
            f"({default_info.glyph_support.hangul_coverage_percent:.4f}%)"
        )
    )
    print()
    print("Korean glyph support:")
    print(support)
    print()
    print("Font files found:")
    if report.font_files:
        for info in report.font_files:
            glyphs = (
                "UNKNOWN"
                if info.glyph_support is None
                else (
                    f"{info.glyph_support.hangul_coverage_status} "
                    f"{info.glyph_support.hangul_coverage_percent:.4f}%"
                )
            )
            print(f"- {info.file} (Hangul Syllables: {glyphs})")
    else:
        print("- None")
    print()
    print("Font references:")
    if report.references:
        for reference in report.references:
            location = (
                f"{reference.file}:{reference.line}"
                if reference.line is not None
                else reference.file
            )
            value = reference.target or reference.font_name or "-"
            print(f"- {location} [{reference.kind}] {value}")
    else:
        print("- None")
    print()
    print("Potential issues:")
    if report.issues:
        for issue in report.issues:
            location = ""
            if issue.file:
                location = f" ({issue.file}"
                if issue.line is not None:
                    location += f":{issue.line}"
                location += ")"
            print(f"- {issue.severity.upper()} {issue.code}{location}: {issue.reason}")
    else:
        print("- None")
    if report.action == "patch":
        print()
        print(f"Dry run: {'yes' if report.dry_run else 'no'}")
        print("Planned files:")
        for path in report.planned_files:
            print(f"- {path}")
        print("Planned reference changes:")
        for change in report.planned_reference_changes:
            print(f"- {change}")
        if report.copied_font:
            print(f"Copied font: {report.copied_font}")


def _print_structure_report(report: StructureReport) -> None:
    print(f"Engine ID: {report.engine.value}")
    print()
    print("Packaging:")
    print(f"- Type: {report.packaging_type}")
    print(f"- Encryption: {report.encryption_status}")
    print(f"- Possible version: {report.possible_version or 'unknown'}")
    print(f"- Version confidence: {report.version_confidence}")

    sections = (
        ("Executable candidates", [item.file for item in report.executables]),
        ("Data directories", list(report.data_directories)),
        ("Archive/data candidates", [item.file for item in report.archive_files]),
        ("Possible text sources", [item.file for item in report.possible_text_sources]),
        ("Possible map files", [item.file for item in report.possible_map_files]),
        (
            "Possible common event files",
            [item.file for item in report.possible_common_event_files],
        ),
        ("Possible database files", [item.file for item in report.possible_database_files]),
        ("Possible font files", [item.file for item in report.possible_font_files]),
        ("Media directories", list(report.media_directories)),
        ("Unknown binary files", [item.file for item in report.unknown_binary_files]),
    )
    for title, values in sections:
        print()
        print(f"{title}:")
        if values:
            for value in values:
                print(f"- {value}")
        else:
            print("- None")
    if report.notes:
        print()
        print("Notes:")
        for note in report.notes:
            print(f"- {note}")


def _print_archive_report(report: ArchiveReport) -> None:
    print(f"File: {report.relative_path}")
    print(f"Format: {report.archive_type}")
    print(f"Extension: {report.extension or '-'}")
    print(f"Size: {report.size}")
    print(f"Header: {report.header_hex or '-'}")
    print(f"Tail: {report.tail_hex or '-'}")
    print(f"Packaging: {report.packaging}")
    print(f"Encryption: {report.encryption_status}")
    print(f"Archive generation: {report.version or 'unknown'}")
    print(f"Confidence: {report.confidence}%")
    print(f"Executable type: {report.executable_type}")
    print(
        "Can list entries: "
        f"{'yes' if report.entry_listing_supported else 'no'}"
    )
    metadata = report.extra_metadata
    print(f"Probable role: {metadata.get('archive_role', 'unknown')}")
    print(f"Text likelihood: {metadata.get('text_likelihood', 'unknown')}")
    if report.evidence:
        print()
        print("Evidence:")
        for item in report.evidence:
            print(f"- {item}")
    if report.notes:
        print()
        print("Notes:")
        for note in report.notes:
            print(f"- {note}")


def _print_wolf_text_report(report: WolfTextReport) -> None:
    print("WOLF Text Export Inspection")
    print(f"Files: {report.file_count}")
    print(f"Target: {report.target_type}")
    print(f"Encoding: {report.detected_encoding}")
    print(f"Encoding confidence: {report.encoding_confidence}")
    if report.encoding_evidence:
        print("Encoding evidence:")
        for evidence in report.encoding_evidence:
            print(f"- {evidence}")
    print(f"BOM: {report.bom}")
    print(f"Newline: {report.newline_style}")
    print(f"Final newline: {report.final_newline}")
    print()
    print("Candidate records:")
    print(f"- Total: {report.record_count}")
    print(f"- Dialogue: {report.count_type('dialogue')}")
    print(f"- Choice: {report.count_type('choice')}")
    print(f"- Database: {sum(item.type.startswith('database_') for item in report.records)}")
    print(f"- Common event: {sum(item.location.domain == 'common' for item in report.records)}")
    print(f"- Map event: {sum(item.location.domain == 'map' for item in report.records)}")
    print(f"- System: {report.count_type('system')}")
    print(f"- Unknown preserved: {len(report.unknown_records)}")
    print()
    print(
        "Canonical location schema: "
        f"v{report.location_schema_version} ({report.location_schema_status})"
    )
    print(
        "Parser issues: "
        f"{sum(item.severity == 'warning' for item in report.issues)} warning(s), "
        f"{sum(item.severity == 'error' for item in report.issues)} error(s)"
    )
    for issue in report.issues:
        location = issue.source_file or "-"
        if issue.line is not None:
            location += f":{issue.line}"
        print(f"- {issue.severity.upper()} {issue.code} ({location}): {issue.reason}")


def _handle_archive(args: argparse.Namespace, logger: logging.Logger) -> int:
    try:
        archive_file = resolve_input_file(args.archive_file)
        registry = create_engine_registry()
        adapter = registry.adapter_for_archive(archive_file)
        if adapter is None:
            raise ValueError(
                "no registered adapter supports this archive extension; "
                "supported WOLF candidates are .wolf, .wolfx, and .assets"
            )
        report = adapter.inspect_archive(archive_file)
        report_file = None
        if args.json_output is not None:
            report_file = resolve_output_file(args.json_output)
            if report_file.suffix.casefold() != ".json":
                raise ValueError("archive report must use the .json extension")
            protected_root = find_wolf_game_root(archive_file) or archive_file.parent
            if report_file == protected_root or report_file.is_relative_to(
                protected_root
            ):
                raise ValueError(
                    "archive report cannot be written inside the game or archive directory"
                )
            write_archive_report(report_file, report)
    except (OSError, RuntimeError, ValueError) as exc:
        logger.error("%s", exc)
        return 2
    _print_archive_report(report)
    if report_file is not None:
        print()
        print(f"Report: {report_file}")
    return 0


def _handle_wolf_text(args: argparse.Namespace, logger: logging.Logger) -> int:
    try:
        export_directory = resolve_input_directory(args.export_directory)
        adapter = create_engine_registry().adapter_for(EngineId.WOLF_RPG_EDITOR)
        if adapter is None:
            raise RuntimeError("WOLF adapter is not registered")
        report = adapter.inspect_text_export(export_directory)
        if not isinstance(report, WolfTextReport):
            raise RuntimeError("WOLF adapter returned an invalid text report")
        report_file = None
        if args.json_output is not None:
            report_file = resolve_output_file(args.json_output)
            if report_file.suffix.casefold() != ".json":
                raise ValueError("WOLF text report must use the .json extension")
            if report_file == export_directory or report_file.is_relative_to(
                export_directory
            ):
                raise ValueError(
                    "WOLF text report cannot be written inside the export directory"
                )
            write_wolf_text_report(report_file, report)
    except (OSError, RuntimeError, ValueError) as exc:
        logger.error("%s", exc)
        return 2
    _print_wolf_text_report(report)
    if report_file is not None:
        print()
        print(f"Report: {report_file}")
    return 3 if any(item.severity == "error" for item in report.issues) else 0


def _print_wolf_extraction_report(result: WolfExtractionResult) -> None:
    report = result.report
    print("WOLF Text Extraction Prototype")
    print(f"Files scanned: {report.files_scanned}")
    print(f"Records parsed: {report.records_parsed}")
    print(f"Verified translatable: {report.verified_translatable}")
    print(f"Experimental excluded: {report.experimental_excluded}")
    print(f"Unknown records: {report.unknown_records}")
    print(f"Excluded empty/non-text: {report.excluded_empty_or_nontext}")
    print(f"Canonical ID collisions: {report.canonical_id_collisions}")
    print(f"Decode issues: {report.decode_issues}")
    print(f"Parser warnings: {report.parser_warnings}")
    print(f"Parser errors: {report.parser_errors}")
    print(f"Output entries: {report.output_entries}")
    print(f"Blocked: {'yes' if report.blocked else 'no'}")
    print(
        "Canonical location schema: "
        f"v{report.location_schema_version} ({report.location_schema_status})"
    )


def _handle_wolf_text_extract(
    args: argparse.Namespace, logger: logging.Logger
) -> int:
    try:
        export_directory = resolve_input_directory(args.export_directory)
        output_file = resolve_output_file(args.output)
        if output_file.suffix.casefold() != ".jsonl":
            raise ValueError("WOLF extraction output must use the .jsonl extension")
        if output_file.exists():
            raise FileExistsError(f"output JSONL already exists: {output_file}")
        if output_file == export_directory or output_file.is_relative_to(
            export_directory
        ):
            raise ValueError("WOLF JSONL output cannot be inside the export directory")

        report_file = None
        if args.report is not None:
            report_file = resolve_output_file(args.report)
            if report_file.suffix.casefold() != ".json":
                raise ValueError("WOLF extraction report must use the .json extension")
            if report_file.exists():
                raise FileExistsError(
                    f"WOLF extraction report already exists: {report_file}"
                )
            if report_file == export_directory or report_file.is_relative_to(
                export_directory
            ):
                raise ValueError(
                    "WOLF extraction report cannot be inside the export directory"
                )
            if report_file == output_file:
                raise ValueError("JSONL output and report paths must be different")

        adapter = create_engine_registry().adapter_for(EngineId.WOLF_RPG_EDITOR)
        if adapter is None:
            raise RuntimeError("WOLF adapter is not registered")
        extraction = adapter.extract_text_export(export_directory, output_file)
        if not isinstance(extraction, WolfExtractionResult):
            raise RuntimeError("WOLF adapter returned an invalid extraction result")
        if report_file is not None:
            write_wolf_extraction_report(report_file, extraction.report)
    except (OSError, RuntimeError, ValueError) as exc:
        logger.error("%s", exc)
        return 2

    _print_wolf_extraction_report(extraction)
    for issue in extraction.report.issues:
        message = (
            f"{issue.get('code', 'WOLF_TEXT_ISSUE')}: "
            f"{issue.get('source_file', '-')}: {issue.get('reason', '')}"
        )
        if issue.get("severity") == "error":
            logger.error("%s", message)
        else:
            logger.warning("%s", message)
    if not extraction.report.blocked:
        print(f"Output: {output_file}")
    if report_file is not None:
        print(f"Report: {report_file}")
    return 3 if extraction.report.parser_errors or extraction.report.blocked else 0


def _print_wolf_qa_result(result: WolfQaResult) -> None:
    report = result.report
    print("WOLF Text Translation QA")
    print(f"Total entries: {report.total_entries}")
    print(f"Translated: {report.translated_entries}")
    print(f"Untranslated: {report.untranslated_entries}")
    print(f"Progress: {report.translation_percentage:.2f}%")
    print(f"Applicable: {report.applicable_entries}")
    print(f"Warnings: {report.warning_count}")
    print(f"Errors: {report.error_count}")
    print(f"Blockers: {report.blocker_count}")
    print(f"Source fingerprint: {report.source_fingerprint}")
    print(f"Files to modify: {len(report.files_to_modify)}")
    for name in report.files_to_modify:
        print(f"- {name}")


def _print_wolf_write_report(report: WolfWriteReport) -> None:
    print("WOLF Auto.txt Safe Writer")
    print(f"Dry run: {'yes' if report.dry_run else 'no'}")
    print(f"Total entries: {report.total_entries}")
    print(f"Translated entries: {report.translated_entries}")
    print(f"Untranslated entries: {report.untranslated_entries}")
    print(f"Applicable: {report.applicable_entries}")
    print(f"Applied: {report.applied_entries}")
    print(f"Skipped untranslated: {report.skipped_untranslated}")
    print(f"Skipped: {report.skipped_entries}")
    print(f"Modified files: {len(report.modified_files)}")
    for name in report.modified_files:
        print(f"- {name}")
    print(f"Untouched files: {len(report.untouched_files)}")
    print(f"Warnings: {report.warnings}")
    print(f"Errors: {report.errors}")
    print(f"Blockers: {report.blockers}")
    print(f"Source fingerprint: {report.source_fingerprint}")
    if report.output_fingerprint:
        print(f"Output fingerprint: {report.output_fingerprint}")


def _log_wolf_issues(issues: object, logger: logging.Logger) -> None:
    for issue in issues:
        message = f"{issue.issue_code}: {issue.id or issue.file or '-'}: {issue.reason}"
        if issue.severity == "warning":
            logger.warning("%s", message)
        else:
            logger.error("%s", message)


def _handle_wolf_text_qa(args: argparse.Namespace, logger: logging.Logger) -> int:
    try:
        source = resolve_input_directory(args.source)
        translation = resolve_input_file(args.translation_jsonl)
        report_file = resolve_output_file(args.report)
        if report_file.suffix.casefold() != ".json":
            raise ValueError("WOLF QA report must use the .json extension")
        if report_file == source or report_file.is_relative_to(source):
            raise ValueError("WOLF QA report cannot be inside the source directory")
        adapter = create_engine_registry().adapter_for(EngineId.WOLF_RPG_EDITOR)
        if adapter is None:
            raise RuntimeError("WOLF adapter is not registered")
        result = adapter.qa_text_export(source, translation)
        if not isinstance(result, WolfQaResult):
            raise RuntimeError("WOLF adapter returned an invalid QA result")
        write_wolf_qa_report(report_file, result.report)
    except (OSError, RuntimeError, ValueError) as exc:
        logger.error("%s", exc)
        return 2
    _log_wolf_issues(result.report.issues, logger)
    _print_wolf_qa_result(result)
    print(f"Report: {report_file}")
    return 3 if result.report.error_count or result.report.blocker_count else 0


def _handle_wolf_text_apply(args: argparse.Namespace, logger: logging.Logger) -> int:
    try:
        source = resolve_input_directory(args.source)
        translation = resolve_input_file(args.translation_jsonl)
        output = resolve_new_directory(args.output)
        report_file = resolve_output_file(args.report)
        if report_file.suffix.casefold() != ".json":
            raise ValueError("WOLF apply report must use the .json extension")
        if report_file == source or report_file.is_relative_to(source):
            raise ValueError("WOLF apply report cannot be inside the source directory")
        if report_file == output or report_file.is_relative_to(output):
            raise ValueError("WOLF apply report cannot be inside the output directory")
        adapter = create_engine_registry().adapter_for(EngineId.WOLF_RPG_EDITOR)
        if adapter is None:
            raise RuntimeError("WOLF adapter is not registered")
        report = adapter.apply_text_export(
            source, translation, output, dry_run=args.dry_run
        )
        if not isinstance(report, WolfWriteReport):
            raise RuntimeError("WOLF adapter returned an invalid write report")
        write_wolf_apply_report(report_file, report)
    except (OSError, RuntimeError, ValueError) as exc:
        logger.error("%s", exc)
        return 2
    _log_wolf_issues(report.issues, logger)
    _print_wolf_write_report(report)
    if not args.dry_run and not report.blocked:
        print(f"Output: {output}")
    print(f"Report: {report_file}")
    return 3 if report.errors or report.blockers else 0


def _handle_font(args: argparse.Namespace, logger: logging.Logger) -> int:
    try:
        game_directory = resolve_input_directory(args.game_directory)
        selection = create_engine_registry().identify(game_directory)
        if not selection.detected or selection.adapter is None:
            raise ValueError("RPG Maker MV/MZ could not be detected")
        if args.command == "font-check":
            reports_directory = resolve_new_directory(args.reports)
            report = selection.adapter.font_check(game_directory, reports_directory)
            report_path = reports_directory / "font_report.json"
        else:
            font_file = resolve_input_file(args.font)
            output_directory = resolve_new_directory(args.output)
            reports_directory = resolve_new_directory(args.reports)
            report = selection.adapter.font_patch(
                game_directory,
                font_file,
                output_directory,
                dry_run=args.dry_run,
                reports_directory=reports_directory,
            )
            report_path = reports_directory / "font_patch_report.json"
    except (OSError, RuntimeError, ValueError) as exc:
        logger.error("%s", exc)
        return 2
    _print_font_report(report)
    print(f"Report: {report_path}")
    return 3 if report.errors else 0


def _handle_project(args: argparse.Namespace, logger: logging.Logger) -> int:
    manager = ProjectManager()
    try:
        if args.project_command == "create":
            game_directory = resolve_input_directory(args.game_directory)
            project_directory = resolve_new_directory(args.output)
            result = manager.create(game_directory, project_directory)
            print(f"Project created: {result.project_directory}")
            print(f"Engine: {result.config.engine.value}")
            print(f"Game fingerprint: {result.fingerprint.value}")
            print(f"Translation entries: {result.translation_entries}")
            return 0

        project_directory = resolve_input_directory(args.project_directory)
        if args.project_command == "qa":
            game_directory = resolve_input_directory(args.game_directory)
            result = manager.qa(project_directory, game_directory)
            _log_apply_issues(result.report, logger)
            print(f"Total: {result.report.total_translation_entries}")
            print(f"Translated: {result.report.translated_entries}")
            print(f"Untranslated: {result.report.untranslated_entries}")
            print(f"Progress: {result.translation_percentage:.2f}%")
            print(f"Warnings: {result.report.warnings}")
            print(f"Errors: {result.report.errors}")
            print(f"Conflicts: {result.report.conflicts}")
            print(f"Reports: {result.reports_directory}")
            return 3 if result.report.errors or result.report.conflicts else 0

        if args.project_command == "apply":
            game_directory = resolve_input_directory(args.game_directory)
            output_directory = resolve_new_directory(args.output)
            report = manager.apply(
                project_directory,
                game_directory,
                output_directory,
                dry_run=args.dry_run,
            )
            _log_apply_issues(report, logger)
            print(f"Dry run: {'yes' if args.dry_run else 'no'}")
            print(f"Total translation entries: {report.total_translation_entries}")
            print(f"Applicable: {report.applicable}")
            print(f"Applied: {report.applied}")
            print(f"Skipped untranslated: {report.skipped_untranslated}")
            print(f"Warnings: {report.warnings}")
            print(f"Errors: {report.errors}")
            print(f"Conflicts: {report.conflicts}")
            if args.dry_run:
                print(f"Planned files: {len(report.planned_files)}")
                print(
                    f"Report: {project_directory / 'reports' / 'dry_run_report.json'}"
                )
            else:
                print(f"Output: {output_directory}")
            return 3 if report.errors or report.conflicts else 0

        if args.project_command == "tm-fill":
            result = manager.tm_fill(project_directory)
            for issue in result.issues:
                logger.error("%s: %s", issue.code, issue.reason)
            print(f"TM matches: {result.matches}")
            print(f"Filled: {result.filled}")
            print(f"Skipped existing translations: {result.skipped_existing}")
            print(f"Conflicts: {len(result.issues)}")
            return 3 if result.issues else 0

        if args.project_command == "tm-update":
            result = manager.tm_update(project_directory)
            for issue in result.issues:
                logger.error("%s: %s", issue.code, issue.reason)
            print(f"Added: {result.added}")
            print(f"Duplicates skipped: {result.duplicates}")
            print(f"Conflicts: {len(result.issues)}")
            return 3 if result.issues else 0

        if args.project_command == "font-check":
            game_directory = resolve_input_directory(args.game_directory)
            report = manager.font_check(project_directory, game_directory)
            _print_font_report(report)
            print(f"Report: {project_directory / 'reports' / 'font_report.json'}")
            return 3 if report.errors else 0

        if args.project_command == "font-patch":
            game_directory = resolve_input_directory(args.game_directory)
            font_file = resolve_input_file(args.font)
            output_directory = resolve_new_directory(args.output)
            report = manager.font_patch(
                project_directory,
                game_directory,
                font_file,
                output_directory,
                dry_run=args.dry_run,
            )
            _print_font_report(report)
            print(
                f"Report: {project_directory / 'reports' / 'font_patch_report.json'}"
            )
            return 3 if report.errors else 0
    except ProjectValidationError as exc:
        for issue in exc.issues:
            logger.error("%s: %s", issue.code, issue.reason)
        return 3
    except (OSError, RuntimeError, ValueError, ProjectError) as exc:
        logger.error("%s", exc)
        return 2
    parser_error = getattr(args, "project_command", None)
    logger.error("unsupported project command: %s", parser_error)
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(verbose=args.verbose)
    logger = get_logger(__name__)

    if args.command == "project":
        return _handle_project(args, logger)

    if args.command == "inspect-archive":
        return _handle_archive(args, logger)

    if args.command == "wolf-text-inspect":
        return _handle_wolf_text(args, logger)

    if args.command == "wolf-text-extract":
        return _handle_wolf_text_extract(args, logger)

    if args.command == "wolf-text-qa":
        return _handle_wolf_text_qa(args, logger)

    if args.command == "wolf-text-apply":
        return _handle_wolf_text_apply(args, logger)

    if args.command in {"font-check", "font-patch"}:
        return _handle_font(args, logger)

    if args.command in {"detect", "extract", "apply", "qa", "inspect"}:
        try:
            game_directory = resolve_input_directory(args.game_directory)
        except (FileNotFoundError, NotADirectoryError) as exc:
            logger.error("%s", exc)
            return 2

        logger.debug("Scanning game directory: %s", game_directory)
        selection = create_engine_registry().identify(game_directory)
        result = selection.detection
        logger.debug(
            "Detection result: engine=%s confidence=%d detected=%s",
            result.engine.value if result.engine else "unknown",
            result.confidence,
            result.detected,
        )
        _print_detection(result)
        if args.command == "detect" or not result.detected:
            return 0 if result.detected else 1
        if selection.adapter is None:
            logger.error("detected engine has no registered adapter")
            return 2
        adapter = selection.adapter

        if args.command == "inspect":
            try:
                structure = adapter.inspect_structure(game_directory)
                report_file = None
                if args.json_output is not None:
                    report_file = resolve_output_file(args.json_output)
                    if report_file.suffix.casefold() != ".json":
                        raise ValueError("structure report must use the .json extension")
                    if report_file == game_directory or report_file.is_relative_to(
                        game_directory
                    ):
                        raise ValueError(
                            "structure report cannot be written inside the game directory"
                        )
                    write_structure_report(report_file, structure)
            except (OSError, RuntimeError, ValueError) as exc:
                logger.error("%s", exc)
                return 2
            print()
            _print_structure_report(structure)
            if report_file is not None:
                print()
                print(f"Report: {report_file}")
            return 0

        if args.command == "qa":
            try:
                translation_file = resolve_input_file(args.translation_jsonl)
                reports_directory = resolve_new_directory(args.reports)
                allowlist_path = _resolve_allowlist(args.allowlist)
                qa_result = adapter.qa(
                    game_directory,
                    translation_file,
                    reports_directory,
                    allowlist_path=allowlist_path,
                )
            except (OSError, RuntimeError, ValueError) as exc:
                logger.error("%s", exc)
                return 2

            report = qa_result.report
            _log_apply_issues(report, logger)
            print()
            print(f"Total: {report.total_translation_entries}")
            print(f"Translated: {report.translated_entries}")
            print(f"Untranslated: {report.untranslated_entries}")
            print(f"Progress: {qa_result.translation_percentage:.2f}%")
            print(f"Applicable: {report.applicable}")
            print(f"Warnings: {report.warnings}")
            print(f"Errors: {report.errors}")
            print(f"Conflicts: {report.conflicts}")
            print(f"Game fingerprint: {qa_result.fingerprint.value}")
            print(f"Reports: {qa_result.reports_directory}")
            return 3 if report.errors or report.conflicts else 0

        if args.command == "apply":
            try:
                translation_file = resolve_input_file(args.translation_jsonl)
                output_directory = resolve_new_directory(args.output)
                allowlist_path = _resolve_allowlist(args.allowlist)
                report = adapter.apply(
                    game_directory,
                    translation_file,
                    output_directory,
                    dry_run=args.dry_run,
                    allowlist_path=allowlist_path,
                )
                if args.dry_run:
                    reports_directory = resolve_new_directory(args.reports)
                    if (
                        reports_directory == game_directory
                        or reports_directory.is_relative_to(game_directory)
                    ):
                        raise ValueError(
                            "reports directory cannot be inside the original game directory"
                        )
                    report_file = write_dry_run_report(reports_directory, report)
            except (OSError, RuntimeError, ValueError) as exc:
                logger.error("%s", exc)
                return 2

            _log_apply_issues(report, logger)

            print()
            print(f"Dry run: {'yes' if args.dry_run else 'no'}")
            print(f"Files copied: {report.files_copied}")
            print(f"JSON files modified: {len(report.modified_files)}")
            print(f"Total translation entries: {report.total_translation_entries}")
            print(f"Translated: {report.translated_entries}")
            print(f"Untranslated: {report.untranslated_entries}")
            print(f"Applicable: {report.applicable}")
            print(f"Applied: {report.applied}")
            print(f"Skipped untranslated: {report.skipped_untranslated}")
            print(f"Warnings: {report.warnings}")
            print(f"Errors: {report.errors}")
            print(f"Conflicts: {report.conflicts}")
            if args.dry_run:
                print(f"Planned files: {len(report.planned_files)}")
                for planned_file in report.planned_files:
                    print(f"- {planned_file}")
                print(f"Planned IDs: {len(report.planned_ids)}")
                for planned_id in report.planned_ids:
                    print(f"- {planned_id}")
                print(f"Report: {report_file}")
            else:
                print(f"Report: {output_directory / 'reports' / 'apply_report.json'}")
            return 3 if report.errors or report.conflicts else 0

        try:
            output_file = resolve_output_file(args.output)
            if output_file.suffix.casefold() != ".jsonl":
                raise ValueError("output file must use the .jsonl extension")
            if output_file.exists() and output_file.is_relative_to(game_directory):
                raise ValueError(
                    "refusing to overwrite an existing file inside the game directory"
                )
            extraction = adapter.extract(game_directory, output_file)
        except (OSError, RuntimeError, ValueError) as exc:
            logger.error("%s", exc)
            return 2

        for issue in extraction.issues:
            logger.warning("%s: %s", issue.file, issue.message)

        print()
        print(f"Extracted Strings: {len(extraction.entries)}")
        print(f"Output: {output_file}")
        if extraction.issues:
            print(f"Completed with {len(extraction.issues)} issue(s).")
            return 3
        return 0

    parser.error(f"unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
