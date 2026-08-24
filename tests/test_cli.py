from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

import glt


def _phase4_fixture(workspace: Path) -> tuple[Path, Path]:
    game = workspace / "game"
    translation_file = workspace / "translated.jsonl"
    (game / "js").mkdir(parents=True)
    (game / "data").mkdir()
    (game / "js/rmmz_core.js").write_text("", encoding="utf-8")
    (game / "data/System.json").write_text("{}", encoding="utf-8")
    map_document = {
        "events": [
            None,
            {
                "id": 1,
                "pages": [
                    {"list": [{"code": 401, "parameters": ["こんにちは"]}]}
                ],
            },
        ]
    }
    (game / "data/Map001.json").write_text(
        json.dumps(map_document, ensure_ascii=False),
        encoding="utf-8",
    )
    translation_file.write_text(
        json.dumps(
            {
                "id": "Map001:event1:page1:cmd401:index0:param0",
                "engine": "rpgmaker_mz",
                "file": "data/Map001.json",
                "type": "dialogue",
                "original": "こんにちは",
                "translation": "안녕하세요",
                "json_path": "$.events[1].pages[0].list[0].parameters[0]",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return game, translation_file


class CliTests(unittest.TestCase):
    def test_detect_command_prints_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "js").mkdir()
            (root / "data").mkdir()
            (root / "js/rmmz_core.js").write_text("", encoding="utf-8")
            (root / "data/System.json").write_text("{}", encoding="utf-8")
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                exit_code = glt.main(["detect", str(root)])

        self.assertEqual(0, exit_code)
        self.assertIn("Detected Engine: RPG Maker MZ", output.getvalue())
        self.assertIn("- data/System.json", output.getvalue())

    def test_extract_command_uses_source_jsonl_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            game = workspace / "game"
            (game / "js").mkdir(parents=True)
            (game / "data").mkdir()
            (game / "js/rmmz_core.js").write_text("", encoding="utf-8")
            (game / "data/System.json").write_text("{}", encoding="utf-8")
            map_document = {
                "events": [
                    None,
                    {
                        "id": 1,
                        "pages": [
                            {
                                "list": [
                                    {"code": 401, "parameters": ["こんにちは"]}
                                ]
                            }
                        ],
                    },
                ]
            }
            (game / "data/Map001.json").write_text(
                json.dumps(map_document, ensure_ascii=False),
                encoding="utf-8",
            )
            output = io.StringIO()
            previous_directory = Path.cwd()
            try:
                os.chdir(workspace)
                with contextlib.redirect_stdout(output):
                    exit_code = glt.main(["extract", str(game)])
            finally:
                os.chdir(previous_directory)

            output_file = workspace / "source.jsonl"
            payload = json.loads(output_file.read_text(encoding="utf-8"))

        self.assertEqual(0, exit_code)
        self.assertEqual("こんにちは", payload["original"])
        self.assertEqual("rpgmaker_mz", payload["engine"])
        self.assertIn("Extracted Strings: 1", output.getvalue())

    def test_apply_command_creates_output_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            game = workspace / "game"
            output_directory = workspace / "game-ko"
            translation_file = workspace / "translated.jsonl"
            (game / "js").mkdir(parents=True)
            (game / "data").mkdir()
            (game / "js/rmmz_core.js").write_text("", encoding="utf-8")
            (game / "data/System.json").write_text("{}", encoding="utf-8")
            map_document = {
                "events": [
                    None,
                    {
                        "id": 1,
                        "pages": [
                            {
                                "list": [
                                    {"code": 401, "parameters": ["こんにちは"]}
                                ]
                            }
                        ],
                    },
                ]
            }
            (game / "data/Map001.json").write_text(
                json.dumps(map_document, ensure_ascii=False),
                encoding="utf-8",
            )
            translation_file.write_text(
                json.dumps(
                    {
                        "id": "Map001:event1:page1:cmd401:index0:param0",
                        "engine": "rpgmaker_mz",
                        "file": "data/Map001.json",
                        "type": "dialogue",
                        "original": "こんにちは",
                        "translation": "안녕하세요",
                        "json_path": "$.events[1].pages[0].list[0].parameters[0]",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                exit_code = glt.main(
                    [
                        "apply",
                        str(game),
                        str(translation_file),
                        "--output",
                        str(output_directory),
                    ]
                )

            translated_map = json.loads(
                (output_directory / "data/Map001.json").read_text(encoding="utf-8")
            )
            report_exists = (output_directory / "reports/apply_report.json").is_file()

        self.assertEqual(0, exit_code)
        self.assertEqual(
            "안녕하세요",
            translated_map["events"][1]["pages"][0]["list"][0]["parameters"][0],
        )
        self.assertIn("Applied: 1", output.getvalue())
        self.assertTrue(report_exists)

    def test_qa_command_writes_reports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            game, translations = _phase4_fixture(workspace)
            reports = workspace / "reports"
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                exit_code = glt.main(
                    [
                        "qa",
                        str(game),
                        str(translations),
                        "--reports",
                        str(reports),
                    ]
                )
            report_exists = (reports / "qa_report.json").is_file()

        self.assertEqual(0, exit_code)
        self.assertTrue(report_exists)
        self.assertIn("Progress: 100.00%", output.getvalue())

    def test_qa_command_returns_three_for_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            game, translations = _phase4_fixture(workspace)
            payload = json.loads(translations.read_text(encoding="utf-8"))
            payload["original"] = "다른 버전의 원문"
            translations.write_text(
                json.dumps(payload, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = glt.main(
                    [
                        "qa",
                        str(game),
                        str(translations),
                        "--reports",
                        str(workspace / "reports"),
                    ]
                )

        self.assertEqual(3, exit_code)

    def test_apply_dry_run_does_not_create_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            game, translations = _phase4_fixture(workspace)
            output_directory = workspace / "game-ko"
            reports = workspace / "reports"
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                exit_code = glt.main(
                    [
                        "apply",
                        str(game),
                        str(translations),
                        "--output",
                        str(output_directory),
                        "--dry-run",
                        "--reports",
                        str(reports),
                    ]
                )
            output_exists = output_directory.exists()
            report_exists = (reports / "dry_run_report.json").is_file()

        self.assertEqual(0, exit_code)
        self.assertFalse(output_exists)
        self.assertTrue(report_exists)
        self.assertIn("Planned IDs: 1", output.getvalue())

    def test_project_cli_create_qa_dry_run_and_apply(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            game, _ = _phase4_fixture(workspace)
            project = workspace / "project"
            output_directory = workspace / "game-ko"

            with contextlib.redirect_stdout(io.StringIO()):
                create_code = glt.main(
                    ["project", "create", str(game), "--output", str(project)]
                )

            records = [
                json.loads(line)
                for line in (project / "translated.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            records[0]["translation"] = "안녕하세요"
            (project / "translated.jsonl").write_text(
                "".join(
                    json.dumps(record, ensure_ascii=False) + "\n"
                    for record in records
                ),
                encoding="utf-8",
            )

            with contextlib.redirect_stdout(io.StringIO()):
                qa_code = glt.main(["project", "qa", str(project), str(game)])
                dry_run_code = glt.main(
                    [
                        "project",
                        "apply",
                        str(project),
                        str(game),
                        "--output",
                        str(output_directory),
                        "--dry-run",
                    ]
                )
            self.assertFalse(output_directory.exists())

            with contextlib.redirect_stdout(io.StringIO()):
                apply_code = glt.main(
                    [
                        "project",
                        "apply",
                        str(project),
                        str(game),
                        "--output",
                        str(output_directory),
                    ]
                )
            translated_map = json.loads(
                (output_directory / "data/Map001.json").read_text(encoding="utf-8")
            )

        self.assertEqual(0, create_code)
        self.assertEqual(0, qa_code)
        self.assertEqual(0, dry_run_code)
        self.assertEqual(0, apply_code)
        self.assertEqual(
            "안녕하세요",
            translated_map["events"][1]["pages"][0]["list"][0]["parameters"][0],
        )

    def test_project_cli_tm_fill_and_update(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            game, _ = _phase4_fixture(workspace)
            project = workspace / "project"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    0,
                    glt.main(
                        ["project", "create", str(game), "--output", str(project)]
                    ),
                )

            memory_entry = {
                "original": "こんにちは",
                "translation": "안녕하세요",
                "type": "dialogue",
                "approved": True,
            }
            (project / "translation_memory.jsonl").write_text(
                json.dumps(memory_entry, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            fill_output = io.StringIO()
            with contextlib.redirect_stdout(fill_output):
                fill_code = glt.main(["project", "tm-fill", str(project)])

            (project / "translation_memory.jsonl").write_text("", encoding="utf-8")
            update_output = io.StringIO()
            with contextlib.redirect_stdout(update_output):
                update_code = glt.main(["project", "tm-update", str(project)])
            updated_memory = json.loads(
                (project / "translation_memory.jsonl").read_text(encoding="utf-8")
            )

        self.assertEqual(0, fill_code)
        self.assertIn("Filled: 1", fill_output.getvalue())
        self.assertEqual(0, update_code)
        self.assertIn("Added: 1", update_output.getvalue())
        self.assertTrue(updated_memory["approved"])


if __name__ == "__main__":
    unittest.main()
