from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from tools import merge_translation as merge_cli
from tools import split_translation as split_cli
from tools.translation_utils import (
    TranslationUtilityError,
    TranslationValidationError,
    merge_translation,
    split_translation,
)


def _entries() -> list[dict[str, object]]:
    return [
        {
            "id": "Map001:event1:page1:cmd401:index0:param0",
            "engine": "rpgmaker_mz",
            "file": "data/Map001.json",
            "type": "dialogue",
            "original": "一番目\\V[1]",
            "translation": "",
            "json_path": "$.events[1].pages[0].list[0].parameters[0]",
            "event_id": 1,
            "page_id": 1,
            "command_index": 0,
            "parameter_index": 0,
            "map_id": 1,
            "map_name": "第一マップ",
            "control_codes": ["\\V[1]"],
        },
        {
            "id": "Map001:event1:page1:cmd401:index1:param0",
            "engine": "rpgmaker_mz",
            "file": "data/Map001.json",
            "type": "dialogue",
            "original": "二番目",
            "translation": "기존 번역",
            "json_path": "$.events[1].pages[0].list[1].parameters[0]",
            "event_id": 1,
            "page_id": 1,
            "command_index": 1,
            "parameter_index": 0,
            "map_id": 1,
            "map_name": "第一マップ",
        },
        {
            "id": "Map002:event2:page1:cmd102:index0:param0",
            "engine": "rpgmaker_mz",
            "file": "data/Map002.json",
            "type": "choice",
            "original": "選択肢",
            "translation": "",
            "json_path": "$.events[2].pages[0].list[0].parameters[0][0]",
            "event_id": 2,
            "page_id": 1,
            "command_index": 0,
            "parameter_index": 0,
            "map_id": 2,
            "map_name": "第二マップ",
        },
        {
            "id": "Map001:event3:page2:cmd405:index7:param0",
            "engine": "rpgmaker_mz",
            "file": "data/Map001.json",
            "type": "scroll_text",
            "original": "四番目",
            "translation": "",
            "json_path": "$.events[3].pages[1].list[7].parameters[0]",
            "event_id": 3,
            "page_id": 2,
            "command_index": 7,
            "parameter_index": 0,
            "map_id": 1,
            "map_name": "第一マップ",
        },
        {
            "id": "Items:index1:name",
            "engine": "rpgmaker_mz",
            "file": "data/Items.json",
            "type": "item_name",
            "original": "薬草",
            "translation": "",
            "json_path": "$[1].name",
        },
    ]


def _write_jsonl(path: Path, entries: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
            for entry in entries
        ),
        encoding="utf-8",
    )


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TranslationSplitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary_directory.name)
        self.source = self.workspace / "translated.jsonl"
        self.output = self.workspace / "translation_work"
        _write_jsonl(self.source, _entries())

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_default_by_file_split_preserves_rows_metadata_and_order(self) -> None:
        source_before = self.source.read_bytes()

        result = split_translation(self.source, self.output)

        self.assertEqual(source_before, self.source.read_bytes())
        self.assertEqual("by_file", result.split_mode)
        self.assertEqual(
            {"Map001.jsonl", "Map002.jsonl", "Items.jsonl"},
            {part.filename for part in result.parts},
        )
        map_rows = _read_jsonl(self.output / "Map001.jsonl")
        expected = [entry for entry in _entries() if entry["file"] == "data/Map001.json"]
        self.assertEqual(expected, map_rows)
        self.assertEqual(
            [expected[0]["id"], expected[1]["id"], expected[2]["id"]],
            [row["id"] for row in map_rows],
        )

    def test_by_file_max_lines_splits_large_group_at_row_boundaries(self) -> None:
        result = split_translation(self.source, self.output, max_lines=2)

        names = [part.filename for part in result.parts]
        self.assertIn("Map001_part001.jsonl", names)
        self.assertIn("Map001_part002.jsonl", names)
        first = _read_jsonl(self.output / "Map001_part001.jsonl")
        second = _read_jsonl(self.output / "Map001_part002.jsonl")
        self.assertEqual(2, len(first))
        self.assertEqual(1, len(second))
        self.assertTrue(all(isinstance(row, dict) for row in first + second))

    def test_line_mode_splits_by_entry_count(self) -> None:
        result = split_translation(
            self.source,
            self.output,
            by_file=False,
            lines=2,
        )

        self.assertEqual("lines", result.split_mode)
        self.assertEqual(
            ["part_001.jsonl", "part_002.jsonl", "part_003.jsonl"],
            [part.filename for part in result.parts],
        )
        restored = [
            row
            for filename in ("part_001.jsonl", "part_002.jsonl", "part_003.jsonl")
            for row in _read_jsonl(self.output / filename)
        ]
        self.assertEqual(_entries(), restored)

    def test_manifest_contains_portable_source_hash_and_part_metadata(self) -> None:
        result = split_translation(self.source, self.output, max_lines=2)
        manifest_text = (self.output / "split_manifest.json").read_text(encoding="utf-8")
        manifest = json.loads(manifest_text)

        self.assertEqual(1, manifest["format_version"])
        self.assertEqual("translated.jsonl", manifest["source_filename"])
        self.assertEqual(5, manifest["total_entries"])
        self.assertEqual("by_file", manifest["split_mode"])
        self.assertEqual(2, manifest["max_lines"])
        self.assertEqual(_sha256(self.source), manifest["source_sha256"])
        self.assertEqual(result.parts[0].first_id, manifest["parts"][0]["first_id"])
        self.assertNotIn(str(self.workspace), manifest_text)

    def test_unsafe_source_path_is_converted_to_safe_filename(self) -> None:
        entries = _entries()
        entries[0]["file"] = "../../outside/evil?.json"
        _write_jsonl(self.source, entries)

        result = split_translation(self.source, self.output)

        self.assertTrue(all("/" not in part.filename for part in result.parts))
        self.assertTrue(all("\\" not in part.filename for part in result.parts))
        self.assertTrue(all((self.output / part.filename).is_file() for part in result.parts))

    def test_existing_output_directory_is_protected(self) -> None:
        self.output.mkdir()
        marker = self.output / "keep.txt"
        marker.write_text("keep", encoding="utf-8")

        with self.assertRaises(FileExistsError):
            split_translation(self.source, self.output)

        self.assertEqual("keep", marker.read_text(encoding="utf-8"))

    def test_malformed_source_jsonl_is_blocked_without_partial_output(self) -> None:
        self.source.write_text('{"id":"ok"}\n{"id": broken}\n', encoding="utf-8")

        with self.assertRaises(TranslationValidationError) as caught:
            split_translation(self.source, self.output, by_file=False, lines=1)

        self.assertIn("MALFORMED_JSONL", {issue.code for issue in caught.exception.issues})
        self.assertFalse(self.output.exists())

    def test_by_file_and_lines_conflict_is_rejected(self) -> None:
        with self.assertRaises(TranslationUtilityError):
            split_translation(self.source, self.output, by_file=True, lines=2)

    def test_split_cli_default_and_line_mode(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            default_code = split_cli.main(
                [str(self.source), "--output", str(self.output)]
            )
        line_output = self.workspace / "line-work"
        with contextlib.redirect_stdout(io.StringIO()):
            line_code = split_cli.main(
                [
                    str(self.source),
                    "--lines",
                    "2",
                    "--output",
                    str(line_output),
                ]
            )

        self.assertEqual(0, default_code)
        self.assertEqual(0, line_code)
        self.assertTrue((self.output / "split_manifest.json").is_file())
        self.assertTrue((line_output / "part_001.jsonl").is_file())


class TranslationMergeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary_directory.name)
        self.source = self.workspace / "translated.jsonl"
        self.work = self.workspace / "translation_work"
        self.output = self.workspace / "translated_merged.jsonl"
        _write_jsonl(self.source, _entries())
        split_translation(self.source, self.work, max_lines=2)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _part_files(self, work: Path | None = None) -> list[Path]:
        target = work or self.work
        manifest = json.loads((target / "split_manifest.json").read_text(encoding="utf-8"))
        return [target / part["filename"] for part in manifest["parts"]]

    def _translate_and_reverse_parts(self) -> dict[str, str]:
        translations: dict[str, str] = {}
        for part in self._part_files():
            rows = _read_jsonl(part)
            for index, row in enumerate(rows):
                translation = f"번역-{row['id']}"
                row["translation"] = translation
                translations[row["id"]] = translation
            _write_jsonl(part, list(reversed(rows)))
        return translations

    def _fresh_case(self, name: str) -> tuple[Path, Path]:
        work = self.workspace / f"work-{name}"
        output = self.workspace / f"merged-{name}.jsonl"
        split_translation(self.source, work, max_lines=2)
        return work, output

    def test_merge_restores_source_order_and_applies_translation_only(self) -> None:
        source_before = self.source.read_bytes()
        translations = self._translate_and_reverse_parts()
        part_before = {path: path.read_bytes() for path in self._part_files()}

        result = merge_translation(self.work, self.source, self.output)
        merged = _read_jsonl(self.output)

        self.assertEqual("success", result.report.status)
        self.assertEqual(source_before, self.source.read_bytes())
        self.assertEqual(part_before, {path: path.read_bytes() for path in self._part_files()})
        self.assertEqual([row["id"] for row in _entries()], [row["id"] for row in merged])
        for canonical, final in zip(_entries(), merged):
            expected = canonical.copy()
            expected["translation"] = translations[canonical["id"]]
            self.assertEqual(expected, final)
        self.assertEqual(5, result.report.translated_entries)
        self.assertEqual(0, result.report.untranslated_entries)
        self.assertTrue((self.work / "merge_report.json").is_file())
        self.assertFalse(list(self.output.parent.glob(f".{self.output.name}.*.tmp")))

    def test_duplicate_id_is_blocked_and_locations_are_reported(self) -> None:
        parts = self._part_files()
        duplicate = _read_jsonl(parts[0])[0]
        rows = _read_jsonl(parts[1])
        rows.append(duplicate)
        _write_jsonl(parts[1], rows)

        result = merge_translation(self.work, self.source, self.output)

        issue = next(issue for issue in result.report.issues if issue.code == "DUPLICATE_ID")
        self.assertIn(":", issue.reason)
        self.assertEqual(1, result.report.duplicate_ids)
        self.assertFalse(self.output.exists())

    def test_unknown_id_is_blocked(self) -> None:
        part = self._part_files()[0]
        rows = _read_jsonl(part)
        unknown = rows[0].copy()
        unknown["id"] = "Unknown:id"
        rows.append(unknown)
        _write_jsonl(part, rows)

        result = merge_translation(self.work, self.source, self.output)

        self.assertIn("UNKNOWN_ID", {issue.code for issue in result.report.issues})
        self.assertEqual(1, result.report.unknown_ids)
        self.assertFalse(self.output.exists())

    def test_missing_id_is_blocked(self) -> None:
        part = self._part_files()[0]
        rows = _read_jsonl(part)
        rows.pop()
        _write_jsonl(part, rows)

        result = merge_translation(self.work, self.source, self.output)

        self.assertIn("MISSING_ID", {issue.code for issue in result.report.issues})
        self.assertEqual(1, result.report.missing_ids)
        self.assertFalse(self.output.exists())

    def test_metadata_changes_are_blocked(self) -> None:
        changes: dict[str, object] = {
            "original": "변조 원문",
            "file": "data/Other.json",
            "json_path": "$.wrong",
            "engine": "rpgmaker_mv",
            "type": "ui",
            "control_codes": ["\\N[9]"],
            "map_name": "변조 맵",
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                work, output = self._fresh_case(field)
                part = self._part_files(work)[0]
                rows = _read_jsonl(part)
                rows[0][field] = value
                _write_jsonl(part, rows)

                result = merge_translation(work, self.source, output)

                mismatch = next(
                    issue for issue in result.report.issues if issue.code == "METADATA_MISMATCH"
                )
                self.assertIn(field, mismatch.reason)
                self.assertFalse(output.exists())

    def test_source_hash_mismatch_is_blocked(self) -> None:
        source = _read_jsonl(self.source)
        source[0]["translation"] = "source changed after split"
        _write_jsonl(self.source, source)

        result = merge_translation(self.work, self.source, self.output)

        self.assertIn("SOURCE_JSONL_MISMATCH", {issue.code for issue in result.report.issues})
        self.assertFalse(self.output.exists())

    def test_malformed_part_is_blocked_with_location(self) -> None:
        part = self._part_files()[0]
        part.write_text('{"id": broken}\n', encoding="utf-8")

        result = merge_translation(self.work, self.source, self.output)

        issue = next(issue for issue in result.report.issues if issue.code == "MALFORMED_JSONL")
        self.assertEqual(part.name, issue.file)
        self.assertEqual(1, issue.line)
        self.assertIsNotNone(issue.column)
        self.assertFalse(self.output.exists())

    def test_empty_translation_is_allowed_and_counted(self) -> None:
        result = merge_translation(self.work, self.source, self.output)

        self.assertEqual("success", result.report.status)
        self.assertEqual(1, result.report.translated_entries)
        self.assertEqual(4, result.report.untranslated_entries)
        self.assertEqual(_entries(), _read_jsonl(self.output))

    def test_existing_output_is_never_overwritten(self) -> None:
        self.output.write_text("keep", encoding="utf-8")

        with self.assertRaises(FileExistsError):
            merge_translation(self.work, self.source, self.output)

        self.assertEqual("keep", self.output.read_text(encoding="utf-8"))

    def test_dry_run_validates_without_output(self) -> None:
        translations = self._translate_and_reverse_parts()

        result = merge_translation(
            self.work,
            self.source,
            self.output,
            dry_run=True,
        )

        self.assertEqual("dry_run", result.report.status)
        self.assertEqual(5, result.report.merged_entries)
        self.assertEqual(len(translations), result.report.translated_entries)
        self.assertFalse(self.output.exists())
        self.assertTrue(result.report_file.is_file())

    def test_manifest_missing_and_unexpected_parts_are_blocked(self) -> None:
        missing = self._part_files()[0]
        missing.unlink()
        (self.work / "extra.jsonl").write_text("{}\n", encoding="utf-8")

        result = merge_translation(self.work, self.source, self.output)
        codes = {issue.code for issue in result.report.issues}

        self.assertIn("MISSING_PART_FILE", codes)
        self.assertIn("UNEXPECTED_PART_FILE", codes)
        self.assertFalse(self.output.exists())

    def test_merge_cli_dry_run_and_success(self) -> None:
        dry_output = io.StringIO()
        with contextlib.redirect_stdout(dry_output):
            dry_code = merge_cli.main(
                [
                    str(self.work),
                    "--source",
                    str(self.source),
                    "--output",
                    str(self.output),
                    "--dry-run",
                ]
            )
        with contextlib.redirect_stdout(io.StringIO()):
            merge_code = merge_cli.main(
                [
                    str(self.work),
                    "--source",
                    str(self.source),
                    "--output",
                    str(self.output),
                ]
            )

        self.assertEqual(0, dry_code)
        self.assertEqual(0, merge_code)
        self.assertIn("Status: dry_run", dry_output.getvalue())
        self.assertTrue(self.output.is_file())


if __name__ == "__main__":
    unittest.main()
