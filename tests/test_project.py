from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from core.models import EngineId
from projects.io import read_jsonl, write_jsonl
from projects.manager import ProjectManager
from projects.models import ProjectError, ProjectValidationError


def _write_json(game: Path, name: str, payload: object) -> None:
    path = game / "data" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def _game(
    root: Path,
    engine: EngineId = EngineId.RPGMAKER_MZ,
    texts: tuple[str, ...] = ("テスト用地名へ向かう。",),
) -> Path:
    core = "rpg_core.js" if engine == EngineId.RPGMAKER_MV else "rmmz_core.js"
    (root / "js").mkdir(parents=True)
    (root / "js" / core).write_text("// core", encoding="utf-8")
    _write_json(root, "System.json", {})
    _write_json(
        root,
        "MapInfos.json",
        [None, {"id": 1, "name": "テストマップ", "parentId": 0, "order": 1}],
    )
    commands = [
        {"code": 401, "parameters": [text]}
        for text in texts
    ]
    _write_json(
        root,
        "Map001.json",
        {
            "events": [
                None,
                {"id": 1, "pages": [{"list": commands}]},
            ]
        },
    )
    (root / "readme.txt").write_text("original", encoding="utf-8")
    return root


def _tree_bytes(root: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


class ProjectManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary_directory.name)
        self.game = _game(self.workspace / "game")
        self.project = self.workspace / "project"
        self.manager = ProjectManager()
        self.create_result = self.manager.create(self.game, self.project)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _translation_records(self) -> list[dict[str, object]]:
        return read_jsonl(self.project / "translated.jsonl")

    def _write_translations(self, records: list[dict[str, object]]) -> None:
        write_jsonl(self.project / "translated.jsonl", records)

    def test_project_create_builds_portable_empty_structure(self) -> None:
        expected = {
            "project.json",
            "source.jsonl",
            "translated.jsonl",
            "glossary.csv",
            "translation_memory.jsonl",
            "config/japanese_allowlist.txt",
            "reports",
        }
        actual = {
            path.relative_to(self.project).as_posix()
            for path in self.project.rglob("*")
        }

        self.assertTrue(expected.issubset(actual))
        project_text = (self.project / "project.json").read_text(encoding="utf-8")
        config = json.loads(project_text)
        self.assertEqual(1, config["project_version"])
        self.assertEqual("0.8.0", config["tool_version"])
        self.assertNotIn(str(self.workspace), project_text)
        self.assertEqual(
            "source,target,type,locked\n",
            (self.project / "glossary.csv").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            b"",
            (self.project / "translation_memory.jsonl").read_bytes(),
        )
        self.assertEqual(
            b"",
            (self.project / "config/japanese_allowlist.txt").read_bytes(),
        )

    def test_source_and_translated_jsonl_are_created_compatible(self) -> None:
        source = read_jsonl(self.project / "source.jsonl")
        translated = self._translation_records()

        self.assertEqual(source, translated)
        self.assertTrue(source)
        self.assertEqual("", translated[0]["translation"])

    def test_project_can_move_to_another_path(self) -> None:
        moved = self.workspace / "other-pc" / "moved-project"
        moved.parent.mkdir()
        shutil.copytree(self.project, moved)

        context = self.manager.load(moved)
        result = self.manager.qa(moved, self.game)

        self.assertEqual(moved.resolve(), context.root)
        self.assertEqual(0, result.report.conflicts)

    def test_project_rejects_windows_absolute_member_path(self) -> None:
        project_file = self.project / "project.json"
        config = json.loads(project_file.read_text(encoding="utf-8"))
        config["translation_file"] = "D:/MachineSpecific/translated.jsonl"
        project_file.write_text(json.dumps(config), encoding="utf-8")

        with self.assertRaises(ProjectError):
            self.manager.load(self.project)

    def test_project_qa_accepts_matching_fingerprint(self) -> None:
        result = self.manager.qa(self.project, self.game)

        self.assertEqual(0, result.report.conflicts)
        self.assertEqual(
            self.create_result.fingerprint.value,
            result.fingerprint.value,
        )

    def test_fingerprint_mismatch_is_reported_and_blocks_apply(self) -> None:
        document = json.loads(
            (self.game / "data/Map001.json").read_text(encoding="utf-8")
        )
        document["events"][1]["pages"][0]["list"][0]["parameters"][0] = "変更"
        _write_json(self.game, "Map001.json", document)

        qa_result = self.manager.qa(self.project, self.game)
        output = self.workspace / "blocked-output"

        self.assertIn(
            "GAME_FINGERPRINT_MISMATCH",
            {issue.code for issue in qa_result.report.issues},
        )
        with self.assertRaises(ProjectValidationError):
            self.manager.apply(self.project, self.game, output)
        self.assertFalse(output.exists())

    def test_project_dry_run_and_apply(self) -> None:
        records = self._translation_records()
        records[0]["translation"] = "테스트 장소로 향한다."
        self._write_translations(records)
        dry_output = self.workspace / "dry-output"
        output = self.workspace / "applied-output"

        dry = self.manager.apply(
            self.project,
            self.game,
            dry_output,
            dry_run=True,
        )
        applied = self.manager.apply(self.project, self.game, output)

        self.assertFalse(dry_output.exists())
        self.assertEqual(1, dry.applicable)
        self.assertEqual(1, applied.applied)
        output_map = json.loads(
            (output / "data/Map001.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            "테스트 장소로 향한다.",
            output_map["events"][1]["pages"][0]["list"][0]["parameters"][0],
        )

    def test_unregistered_glossary_terms_are_ignored(self) -> None:
        records = self._translation_records()
        records[0]["translation"] = "등록되지 않은 번역"
        self._write_translations(records)

        result = self.manager.qa(self.project, self.game)

        self.assertNotIn("GLOSSARY_MISMATCH", {issue.code for issue in result.report.issues})

    def test_locked_glossary_match_and_mismatch(self) -> None:
        (self.project / "glossary.csv").write_text(
            "source,target,type,locked\nテスト用地名,테스트 장소,place,true\n",
            encoding="utf-8",
        )
        records = self._translation_records()
        records[0]["translation"] = "테스트 장소로 향한다."
        self._write_translations(records)
        matched = self.manager.qa(self.project, self.game)

        records[0]["translation"] = "다른 장소로 향한다."
        self._write_translations(records)
        mismatched = self.manager.qa(self.project, self.game)

        self.assertNotIn("GLOSSARY_MISMATCH", {issue.code for issue in matched.report.issues})
        self.assertIn("GLOSSARY_MISMATCH", {issue.code for issue in mismatched.report.issues})
        glossary_issue = next(
            issue for issue in mismatched.report.issues if issue.code == "GLOSSARY_MISMATCH"
        )
        self.assertEqual("warning", glossary_issue.severity)

    def test_unlocked_glossary_never_forces_translation(self) -> None:
        (self.project / "glossary.csv").write_text(
            "source,target,type,locked\nテスト用地名,테스트 장소,place,false\n",
            encoding="utf-8",
        )
        records = self._translation_records()
        records[0]["translation"] = "전혀 다른 표현"
        self._write_translations(records)

        result = self.manager.qa(self.project, self.game)

        self.assertNotIn("GLOSSARY_MISMATCH", {issue.code for issue in result.report.issues})

    def test_tm_fill_uses_only_approved_exact_match(self) -> None:
        (self.project / "translation_memory.jsonl").write_text(
            json.dumps(
                {
                    "original": "テスト用地名へ向かう。",
                    "translation": "승인된 정확 일치",
                    "type": "dialogue",
                    "approved": True,
                },
                ensure_ascii=False,
            )
            + "\n"
            + json.dumps(
                {
                    "original": "テスト用地名へ向かう。",
                    "translation": "미승인",
                    "type": "dialogue",
                    "approved": False,
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        result = self.manager.tm_fill(self.project)
        records = self._translation_records()

        self.assertEqual(1, result.matches)
        self.assertEqual(1, result.filled)
        self.assertEqual("승인된 정확 일치", records[0]["translation"])

    def test_tm_fill_does_not_overwrite_existing_translation(self) -> None:
        records = self._translation_records()
        records[0]["translation"] = "기존 번역"
        self._write_translations(records)
        (self.project / "translation_memory.jsonl").write_text(
            json.dumps(
                {
                    "original": records[0]["original"],
                    "translation": "TM 번역",
                    "type": records[0]["type"],
                    "approved": True,
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        result = self.manager.tm_fill(self.project)

        self.assertEqual(0, result.filled)
        self.assertEqual(1, result.skipped_existing)
        self.assertEqual("기존 번역", self._translation_records()[0]["translation"])

    def test_tm_update_adds_completed_and_prevents_duplicate(self) -> None:
        records = self._translation_records()
        records[0]["translation"] = "완료 번역"
        self._write_translations(records)

        first = self.manager.tm_update(self.project)
        second = self.manager.tm_update(self.project)
        memory = read_jsonl(self.project / "translation_memory.jsonl")

        self.assertEqual(1, first.added)
        self.assertEqual(0, second.added)
        self.assertEqual(1, second.duplicates)
        self.assertEqual(1, len(memory))
        self.assertTrue(memory[0]["approved"])

    def test_tm_update_conflict_does_not_overwrite(self) -> None:
        records = self._translation_records()
        records[0]["translation"] = "새 번역"
        self._write_translations(records)
        existing = {
            "original": records[0]["original"],
            "translation": "기존 TM 번역",
            "type": records[0]["type"],
            "approved": True,
        }
        write_jsonl(self.project / "translation_memory.jsonl", [existing])

        result = self.manager.tm_update(self.project)

        self.assertIn("TM_TRANSLATION_CONFLICT", {issue.code for issue in result.issues})
        self.assertEqual([existing], read_jsonl(self.project / "translation_memory.jsonl"))

    def test_inconsistent_translation_is_warning(self) -> None:
        game = _game(
            self.workspace / "repeated-game",
            texts=("同じ原文", "同じ原文"),
        )
        project = self.workspace / "repeated-project"
        self.manager.create(game, project)
        records = read_jsonl(project / "translated.jsonl")
        records[0]["translation"] = "첫 번역"
        records[1]["translation"] = "둘째 번역"
        write_jsonl(project / "translated.jsonl", records)

        result = self.manager.qa(project, game)

        issue = next(
            issue for issue in result.report.issues if issue.code == "INCONSISTENT_TRANSLATION"
        )
        self.assertEqual("warning", issue.severity)

    def test_mapinfos_name_is_added_without_guessing(self) -> None:
        record = self._translation_records()[0]

        self.assertEqual(1, record["map_id"])
        self.assertEqual("テストマップ", record["map_name"])

    def test_all_project_operations_leave_original_game_unchanged(self) -> None:
        before = _tree_bytes(self.game)
        self.manager.qa(self.project, self.game)
        self.manager.tm_fill(self.project)
        self.manager.tm_update(self.project)
        self.manager.apply(
            self.project,
            self.game,
            self.workspace / "dry-only",
            dry_run=True,
        )
        self.manager.apply(
            self.project,
            self.game,
            self.workspace / "copied-game",
        )

        self.assertEqual(before, _tree_bytes(self.game))

    def test_mv_and_mz_projects_share_behavior(self) -> None:
        for engine in (EngineId.RPGMAKER_MV, EngineId.RPGMAKER_MZ):
            with self.subTest(engine=engine.value):
                game = _game(self.workspace / f"game-{engine.value}", engine)
                project = self.workspace / f"project-{engine.value}"
                created = self.manager.create(game, project)
                qa_result = self.manager.qa(project, game)

                self.assertEqual(engine, created.config.engine)
                self.assertEqual(engine, qa_result.report.engine)


if __name__ == "__main__":
    unittest.main()
