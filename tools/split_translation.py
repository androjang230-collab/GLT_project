"""CLI for safely splitting a translation JSONL into smaller work files."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

if __package__:
    from .translation_utils import (
        TranslationUtilityError,
        TranslationValidationError,
        split_translation,
    )
else:
    from translation_utils import (  # type: ignore[import-not-found]
        TranslationUtilityError,
        TranslationValidationError,
        split_translation,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Split GLT translation JSONL without modifying any entry",
    )
    parser.add_argument("translated_jsonl", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--by-file", action="store_true")
    parser.add_argument("--max-lines", type=int)
    parser.add_argument("--lines", type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    by_file = args.by_file or args.lines is None
    try:
        result = split_translation(
            args.translated_jsonl,
            args.output,
            by_file=by_file,
            lines=args.lines,
            max_lines=args.max_lines,
        )
    except TranslationValidationError as exc:
        for issue in exc.issues:
            location = f" {issue.file or ''}:{issue.line or ''}".rstrip(":")
            print(f"ERROR {issue.code}{location}: {issue.reason}", file=sys.stderr)
        return 3
    except (OSError, ValueError, TranslationUtilityError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"Mode: {result.split_mode}")
    print(f"Total entries: {result.total_entries}")
    print(f"Parts: {len(result.parts)}")
    for part in result.parts:
        print(f"- {part.filename}: {part.entry_count}")
    print(f"Manifest: {result.output_directory / 'split_manifest.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
