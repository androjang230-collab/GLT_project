"""CLI for validating and safely merging translated JSONL work parts."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

if __package__:
    from .translation_utils import TranslationUtilityError, merge_translation
else:
    from translation_utils import (  # type: ignore[import-not-found]
        TranslationUtilityError,
        merge_translation,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Merge GLT translation parts using canonical source order",
    )
    parser.add_argument("translation_work_directory", type=Path)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = merge_translation(
            args.translation_work_directory,
            args.source,
            args.output,
            dry_run=args.dry_run,
        )
    except (OSError, ValueError, TranslationUtilityError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    report = result.report
    for issue in report.issues:
        location = f" {issue.file or ''}:{issue.line or ''}".rstrip(":")
        print(f"ERROR {issue.code}{location}: {issue.reason}", file=sys.stderr)
    print(f"Status: {report.status}")
    print(f"Source entries: {report.source_entries}")
    print(f"Part entries: {report.part_entries}")
    print(f"Merged entries: {report.merged_entries}")
    print(f"Translated: {report.translated_entries}")
    print(f"Untranslated: {report.untranslated_entries}")
    print(f"Duplicate IDs: {report.duplicate_ids}")
    print(f"Unknown IDs: {report.unknown_ids}")
    print(f"Missing IDs: {report.missing_ids}")
    print(f"Metadata mismatches: {report.metadata_mismatches}")
    print(f"Report: {result.report_file}")
    if not args.dry_run and report.status == "success":
        print(f"Output: {result.output_file}")
    return 3 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
