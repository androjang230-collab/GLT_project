from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import glt
from core.models import EngineId
from engines.wolf.text_fingerprint import calculate_wolf_source_fingerprint
from projects.io import read_jsonl, write_jsonl
from projects.manager import ProjectManager
from projects.models import ProjectError, ProjectValidationError
from tools.translation_utils import merge_translation, split_translation


def _event_export(message: str = r"原文\c[2]") -> str:
    return "\r\n".join(
        (
            "[MAPDATA_TEXT_OUTPUT]",
            "[EVENTDATA_TEXT_OUTPUT]",
            "EVENT_ID=7",
            "EVENT_PAGE_NUM=1",
            "EVENT_PAGE=0",
            "COMMAND_NUM=6",
            "WoditorEvCOMMAND_START",
            f'[101][0,1]<0>()("{message}")',
            '[102][1,2]<0>(50)("はい","いいえ")',
            '[401][1,0]<0>(2)()',
            '[401][1,0]<0>(3)()',
            '[499][0,0]<0>()()',
            '[103][0,1]<0>()("experimental")',
            "WoditorEvCOMMAND_END",
            "[COMMAND_TEXT_START]",
            "■文章：表示",
            "■選択肢",
            "■分岐1",
            "■分岐2",
            "■終了",
            "■文章に似た未検証",
            "[COMMAND_TEXT_END]",
            "",
        )
    )


def _auto_txt(root: Path) -> Path:
    source = root / "Data_AutoTXT"
    path = source / "MapData/Map001.mps.Auto.txt"
    path.parent.mkdir(parents=True)
    path.write_text(_event_export(), encoding="utf-8", newline="")
    return source


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


class WolfProjectIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = _auto_txt(self.root)
        self.project = self.root / "project"
        self.manager = ProjectManager()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _create(self) -> None:
        self.manager.create(
            self.source,
            self.project,
            engine=EngineId.WOLF_RPG_EDITOR,
        )

    def _translate_first(self) -> list[dict[str, object]]:
        rows = read_jsonl(self.project / "translated.jsonl")
        rows[0]["translation"] = r"번역\c[2]"
        write_jsonl(self.project / "translated.jsonl", rows)
        return rows

    def test_create_uses_common_portable_project_and_blank_jsonl(self) -> None:
        before = _tree(self.source)
        result = self.manager.create(
            self.source,
            self.project,
            engine=EngineId.WOLF_RPG_EDITOR,
        )
        config_text = (self.project / "project.json").read_text(encoding="utf-8")
        config = json.loads(config_text)
        source_rows = read_jsonl(self.project / "source.jsonl")
        translated = read_jsonl(self.project / "translated.jsonl")

        self.assertEqual(EngineId.WOLF_RPG_EDITOR, result.config.engine)
        self.assertEqual("auto_txt", config["engine_metadata"]["source_mode"])
        self.assertEqual("provisional", config["engine_metadata"]["wolf_location_schema"]["status"])
        self.assertNotIn(str(self.root), config_text)
        self.assertEqual(source_rows, translated)
        self.assertTrue(source_rows)
        self.assertTrue(all(row["translation"] == "" for row in translated))
        self.assertTrue(all(row["engine"] == "wolf_rpg_editor" for row in translated))
        self.assertFalse(any(row["original"] == "experimental" for row in translated))
        self.assertEqual(before, _tree(self.source))

    def test_create_auto_detects_auto_txt_source(self) -> None:
        result = self.manager.create(self.source, self.project)
        self.assertEqual(EngineId.WOLF_RPG_EDITOR, result.config.engine)

    def test_project_rejects_absolute_engine_metadata_path(self) -> None:
        self._create()
        project_file = self.project / "project.json"
        config = json.loads(project_file.read_text(encoding="utf-8"))
        config["engine_metadata"]["editor_path"] = "C:/Users/Private/Editor.exe"
        project_file.write_text(json.dumps(config), encoding="utf-8")

        with self.assertRaises(ProjectError):
            self.manager.load(self.project)

    def test_qa_apply_and_dry_run_share_preflight(self) -> None:
        self._create()
        self._translate_first()
        before = _tree(self.source)
        qa = self.manager.qa(self.project, self.source)
        dry_output = self.root / "dry-output"
        dry = self.manager.apply(
            self.project, self.source, dry_output, dry_run=True
        )
        output = self.root / "translated-output"
        applied = self.manager.apply(self.project, self.source, output)

        self.assertEqual(qa.report.applicable, dry.applicable)
        self.assertEqual(qa.report.planned_ids, dry.planned_ids)
        self.assertFalse(dry_output.exists())
        self.assertEqual(1, applied.applied)
        self.assertIn(r"번역\c[2]", (output / "MapData/Map001.mps.Auto.txt").read_text(encoding="utf-8"))
        self.assertEqual(before, _tree(self.source))
        self.assertTrue((self.project / "reports/qa_report.json").is_file())
        self.assertTrue((self.project / "reports/qa_issues.csv").is_file())
        self.assertTrue((self.project / "reports/untranslated.csv").is_file())
        manifest_text = (self.project / "reports/project_manifest.json").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(str(self.root), manifest_text)
        manifest = json.loads(manifest_text)
        self.assertEqual("auto_txt", manifest["source_mode"])
        self.assertEqual(len(manifest["fingerprint_files"]), manifest["file_count"])
        self.assertTrue((self.project / "reports/dry_run_report.json").is_file())
        apply_report = json.loads(
            (self.project / "reports/apply_report.json").read_text(encoding="utf-8")
        )
        self.assertEqual("auto_txt", apply_report["source_mode"])
        self.assertFalse(apply_report["editor_import_performed"])
        self.assertTrue(apply_report["output_fingerprint"])

    def test_project_glossary_tm_and_split_merge_are_engine_neutral(self) -> None:
        self._create()
        rows = read_jsonl(self.project / "translated.jsonl")
        original = rows[0]["original"]
        (self.project / "translation_memory.jsonl").write_text(
            json.dumps(
                {
                    "original": original,
                    "translation": r"TM 번역\c[2]",
                    "type": rows[0]["type"],
                    "approved": True,
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        fill = self.manager.tm_fill(self.project)
        self.assertEqual(1, fill.filled)
        (self.project / "translation_memory.jsonl").write_text("", encoding="utf-8")
        update = self.manager.tm_update(self.project)
        self.assertEqual(1, update.added)
        self.assertEqual(0, len(update.issues))

        (self.project / "glossary.csv").write_text(
            "source,target,type,locked\n原文,필수용어,term,true\n",
            encoding="utf-8",
        )
        qa = self.manager.qa(self.project, self.source)
        self.assertIn("GLOSSARY_MISMATCH", {issue.code for issue in qa.report.issues})

        work = self.root / "split"
        split_translation(self.project / "translated.jsonl", work, max_lines=1)
        merged = self.root / "merged.jsonl"
        result = merge_translation(
            work,
            self.project / "translated.jsonl",
            merged,
        )
        self.assertEqual(len(rows), result.report.merged_entries)

    def test_fingerprint_is_path_independent_and_change_blocks_apply(self) -> None:
        self._create()
        copied = self.root / "moved-source"
        shutil.copytree(self.source, copied)
        self.assertEqual(
            calculate_wolf_source_fingerprint(self.source).value,
            calculate_wolf_source_fingerprint(copied).value,
        )
        self.manager.qa(self.project, copied)
        target = copied / "MapData/Map001.mps.Auto.txt"
        target.write_text(_event_export("変更"), encoding="utf-8", newline="")
        qa = self.manager.qa(self.project, copied)
        self.assertIn("GAME_FINGERPRINT_MISMATCH", {issue.code for issue in qa.report.issues})
        with self.assertRaises(ProjectValidationError):
            self.manager.apply(self.project, copied, self.root / "blocked")
        self.assertFalse((self.root / "blocked").exists())

    def test_source_mode_mismatch_blocks_apply(self) -> None:
        self._create()
        project_file = self.project / "project.json"
        config = json.loads(project_file.read_text(encoding="utf-8"))
        config["engine_metadata"]["source_mode"] = "editor_project"
        project_file.write_text(
            json.dumps(config, ensure_ascii=False), encoding="utf-8"
        )
        qa = self.manager.qa(self.project, self.source)
        self.assertIn(
            "PROJECT_SOURCE_MODE_MISMATCH",
            {issue.code for issue in qa.report.issues},
        )
        with self.assertRaises(ProjectValidationError):
            self.manager.apply(
                self.project,
                self.source,
                self.root / "mode-mismatch-output",
            )

    def test_malformed_auto_txt_is_reported_by_project_qa(self) -> None:
        self._create()
        target = self.source / "MapData/Map001.mps.Auto.txt"
        target.write_bytes(b"\x81")

        result = self.manager.qa(self.project, self.source)

        codes = {issue.code for issue in result.report.issues}
        self.assertIn("TEXT_DECODE_FAILED", codes)
        self.assertIn("GAME_FINGERPRINT_MISMATCH", codes)
        self.assertGreater(result.report.conflicts, 0)

    def test_cli_wolf_project_end_to_end(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = glt.main(
                [
                    "project",
                    "create",
                    str(self.source),
                    "--engine",
                    "wolf_rpg_editor",
                    "--output",
                    str(self.project),
                ]
            )
        self.assertEqual(0, code)
        self.assertIn("Engine: WOLF RPG Editor", output.getvalue())
        self.assertIn("Engine ID: wolf_rpg_editor", output.getvalue())
        self.assertIn("Source mode: auto_txt", output.getvalue())
        self.assertIn("Canonical schema: wolf:v1 provisional", output.getvalue())
        self.assertIn("Native archive apply: unsupported", output.getvalue())


if __name__ == "__main__":
    unittest.main()
