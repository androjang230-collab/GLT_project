from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.models import EngineId
from engines.rpgmaker.detector import RpgMakerEngine
from engines.rpgmaker.extractor import RpgMakerExtractor
from engines.rpgmaker.fingerprint import (
    calculate_game_fingerprint,
    calculate_legacy_game_fingerprint_0_8_1,
)
from projects.io import read_jsonl, write_jsonl
from projects.manager import ProjectManager


def _write_json(game: Path, name: str, value: object) -> None:
    path = game / "data" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def _read_json(root: Path, name: str) -> object:
    return json.loads((root / "data" / name).read_text(encoding="utf-8"))


def _commands() -> list[dict[str, object]]:
    return [
        {"code": 320, "indent": 0, "parameters": [1, "旧名"]},
        {"code": 324, "indent": 0, "parameters": [1, "旧二つ名"]},
        {"code": 325, "indent": 0, "parameters": [1, "一行目\n二行目"]},
    ]


def _map(commands: list[dict[str, object]]) -> dict[str, object]:
    return {
        "width": 20,
        "events": [
            None,
            {
                "id": 4,
                "name": "internal",
                "pages": [
                    {
                        "conditions": {"switch1Valid": False},
                        "list": commands,
                    }
                ],
            },
        ],
    }


def _game(root: Path, engine: EngineId = EngineId.RPGMAKER_MZ) -> Path:
    core = "rpg_core.js" if engine == EngineId.RPGMAKER_MV else "rmmz_core.js"
    (root / "js").mkdir(parents=True)
    (root / "js" / core).write_text("// synthetic core", encoding="utf-8")
    _write_json(root, "System.json", {})
    _write_json(root, "Map001.json", _map(_commands()))
    _write_json(
        root,
        "Classes.json",
        [
            None,
            {
                "id": 1,
                "name": "剣士",
                "note": "<internal>",
                "traits": [{"code": 11, "dataId": 1, "value": 1.0}],
                "learnings": [{"level": 2, "skillId": 3, "note": "keep"}],
                "expParams": [30, 20, 30, 30],
                "params": [[1, 2], [3, 4]],
            },
        ],
    )
    return root


def _entries(game: Path, engine: EngineId = EngineId.RPGMAKER_MZ):
    result = RpgMakerExtractor(engine).extract(game)
    if result.issues:
        raise AssertionError(result.issues)
    return result.entries


class RpgMakerStandardExpansionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.game = _game(self.workspace / "game")
        self.translation = self.workspace / "translated.jsonl"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _records(self) -> list[dict[str, object]]:
        return [entry.to_json_dict() for entry in _entries(self.game)]

    def _write_records(self, records: list[dict[str, object]]) -> None:
        self.translation.write_text(
            "".join(
                json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
                for record in records
            ),
            encoding="utf-8",
        )

    def _record(self, records: list[dict[str, object]], code: int) -> dict[str, object]:
        marker = f":cmd{code}:"
        return next(record for record in records if marker in str(record["id"]))

    def _apply_one(self, code: int, translation: str):
        records = self._records()
        self._record(records, code)["translation"] = translation
        self._write_records(records)
        output = self.workspace / f"output-{code}"
        report = RpgMakerEngine().apply(self.game, self.translation, output)
        return output, report

    def test_320_extraction_uses_existing_event_canonical_shape(self) -> None:
        entry = next(item for item in _entries(self.game) if item.command_index == 0)
        self.assertEqual("Map001:event4:page1:cmd320:index0:param1", entry.id)
        self.assertEqual("actor_name", entry.type)
        self.assertEqual("$.events[1].pages[0].list[0].parameters[1]", entry.json_path)
        self.assertEqual((4, 1, 0, 1), (entry.event_id, entry.page_id, entry.command_index, entry.parameter_index))

    def test_320_apply_changes_only_parameter_one(self) -> None:
        output, report = self._apply_one(320, "새 이름")
        command = _read_json(output, "Map001.json")["events"][1]["pages"][0]["list"][0]
        self.assertEqual([1, "새 이름"], command["parameters"])
        self.assertEqual(1, report.applied)

    def test_320_blank_translation_is_noop(self) -> None:
        output, report = self._apply_one(320, "   ")
        command = _read_json(output, "Map001.json")["events"][1]["pages"][0]["list"][0]
        self.assertEqual("旧名", command["parameters"][1])
        self.assertEqual(0, report.applied)
        self.assertEqual(4, report.skipped_untranslated)

    def test_320_source_mismatch_is_conflict(self) -> None:
        records = self._records()
        record = self._record(records, 320)
        record["original"] = "別バージョン"
        record["translation"] = "새 이름"
        self._write_records(records)
        output = self.workspace / "mismatch-output"
        report = RpgMakerEngine().apply(self.game, self.translation, output)
        command = _read_json(output, "Map001.json")["events"][1]["pages"][0]["list"][0]
        self.assertEqual("旧名", command["parameters"][1])
        self.assertIn("SOURCE_TEXT_MISMATCH", {issue.code for issue in report.issues})

    def test_324_extraction(self) -> None:
        entry = next(item for item in _entries(self.game) if item.command_index == 1)
        self.assertEqual("Map001:event4:page1:cmd324:index1:param1", entry.id)
        self.assertEqual("actor_nickname", entry.type)
        self.assertEqual(1, entry.parameter_index)

    def test_324_apply_changes_only_parameter_one(self) -> None:
        output, report = self._apply_one(324, "새 별명")
        command = _read_json(output, "Map001.json")["events"][1]["pages"][0]["list"][1]
        self.assertEqual([1, "새 별명"], command["parameters"])
        self.assertEqual(1, report.applied)

    def test_324_blank_translation_is_noop(self) -> None:
        output, report = self._apply_one(324, "")
        command = _read_json(output, "Map001.json")["events"][1]["pages"][0]["list"][1]
        self.assertEqual("旧二つ名", command["parameters"][1])
        self.assertEqual(0, report.applied)

    def test_325_extraction(self) -> None:
        entry = next(item for item in _entries(self.game) if item.command_index == 2)
        self.assertEqual("Map001:event4:page1:cmd325:index2:param1", entry.id)
        self.assertEqual("description", entry.type)

    def test_325_multiline_profile_round_trips(self) -> None:
        translated = "첫 줄\n둘째 줄"
        output, report = self._apply_one(325, translated)
        command = _read_json(output, "Map001.json")["events"][1]["pages"][0]["list"][2]
        self.assertEqual(translated, command["parameters"][1])
        self.assertEqual(1, report.applied)

    def test_classes_name_extraction_skips_null_and_reuses_db_id(self) -> None:
        entry = next(item for item in _entries(self.game) if item.file == "data/Classes.json")
        self.assertEqual("Classes:index1:name", entry.id)
        self.assertEqual("class_name", entry.type)
        self.assertEqual("$[1].name", entry.json_path)
        self.assertEqual("剣士", entry.original)
        self.assertEqual(1, sum(item.file == "data/Classes.json" for item in _entries(self.game)))

    def test_classes_name_apply_preserves_every_other_field(self) -> None:
        before = _read_json(self.game, "Classes.json")
        records = self._records()
        record = next(item for item in records if item["file"] == "data/Classes.json")
        record["translation"] = "검사"
        self._write_records(records)
        output = self.workspace / "class-output"
        report = RpgMakerEngine().apply(self.game, self.translation, output)
        after = _read_json(output, "Classes.json")
        self.assertEqual("검사", after[1]["name"])
        self.assertEqual({key: value for key, value in before[1].items() if key != "name"}, {key: value for key, value in after[1].items() if key != "name"})
        self.assertIsNone(after[0])
        self.assertEqual(1, report.applied)

    def test_common_event_and_troop_share_event_traversal(self) -> None:
        _write_json(self.game, "CommonEvents.json", [None, {"id": 8, "name": "internal", "list": [{"code": 320, "parameters": [1, "共通名"]}]}])
        _write_json(self.game, "Troops.json", [None, {"id": 9, "name": "internal", "pages": [{"list": [{"code": 324, "parameters": [1, "戦闘名"]}]}]}])
        entries = _entries(self.game)
        common = next(item for item in entries if item.file == "data/CommonEvents.json")
        troop = next(item for item in entries if item.file == "data/Troops.json")
        self.assertEqual("CommonEvents:commonEvent8:cmd320:index0:param1", common.id)
        self.assertEqual("Troops:troop9:page1:cmd324:index0:param1", troop.id)

    def test_qa_uses_existing_validation_for_new_entries(self) -> None:
        records = self._records()
        for record in records:
            record["translation"] = {
                "actor_name": "새 이름",
                "actor_nickname": "새 별명",
                "description": "새 설명",
                "class_name": "검사",
            }[str(record["type"])]
        self._write_records(records)
        result = RpgMakerEngine().qa(self.game, self.translation, self.workspace / "qa")
        self.assertEqual(4, result.report.applicable)
        self.assertEqual(0, result.report.errors)
        self.assertEqual(0, result.report.conflicts)

    def test_project_create_qa_and_apply_include_new_entries(self) -> None:
        project = self.workspace / "project"
        output = self.workspace / "project-output"
        manager = ProjectManager()
        created = manager.create(self.game, project)
        self.assertEqual(4, created.translation_entries)
        records = read_jsonl(project / "translated.jsonl")
        for record in records:
            record["translation"] = "프로젝트 번역"
        write_jsonl(project / "translated.jsonl", records)
        qa = manager.qa(project, self.game)
        applied = manager.apply(project, self.game, output)
        self.assertEqual(4, qa.report.applicable)
        self.assertEqual(4, applied.applied)

    def test_pre_0_8_2_project_fingerprint_remains_compatible(self) -> None:
        project = self.workspace / "legacy-project"
        manager = ProjectManager()
        manager.create(self.game, project)
        legacy = calculate_legacy_game_fingerprint_0_8_1(
            self.game,
            EngineId.RPGMAKER_MZ,
        )
        project_file = project / "project.json"
        config = json.loads(project_file.read_text(encoding="utf-8"))
        config["tool_version"] = "0.8.1"
        config["game_fingerprint"] = legacy.value
        project_file.write_text(json.dumps(config), encoding="utf-8")
        for name in ("source.jsonl", "translated.jsonl"):
            path = project / name
            records = [
                record
                for record in read_jsonl(path)
                if record["file"] != "data/Classes.json"
                and not any(
                    marker in str(record["id"])
                    for marker in (":cmd320:", ":cmd324:", ":cmd325:")
                )
            ]
            write_jsonl(path, records)
        result = manager.qa(project, self.game)
        self.assertNotIn(
            "GAME_FINGERPRINT_MISMATCH",
            {issue.code for issue in result.report.issues},
        )

    def test_current_fingerprint_includes_classes_json(self) -> None:
        before = calculate_game_fingerprint(self.game, EngineId.RPGMAKER_MZ)
        classes = _read_json(self.game, "Classes.json")
        classes[1]["name"] = "変更後"
        _write_json(self.game, "Classes.json", classes)
        after = calculate_game_fingerprint(self.game, EngineId.RPGMAKER_MZ)
        self.assertNotEqual(before.value, after.value)

    def test_mv_and_mz_share_new_extraction_and_apply(self) -> None:
        for engine in (EngineId.RPGMAKER_MV, EngineId.RPGMAKER_MZ):
            with self.subTest(engine=engine.value):
                root = self.workspace / engine.value
                game = _game(root, engine)
                entries = _entries(game, engine)
                self.assertTrue(
                    all(
                        any(f":cmd{code}:" in item.id for item in entries)
                        for code in (320, 324, 325)
                    )
                )
                records = [item.to_json_dict() for item in entries]
                target = next(item for item in records if ":cmd320:" in str(item["id"]))
                target["translation"] = "번역명"
                translation = self.workspace / f"{engine.value}.jsonl"
                translation.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records), encoding="utf-8")
                output = self.workspace / f"output-{engine.value}"
                report = RpgMakerEngine().apply(game, translation, output)
                self.assertEqual(1, report.applied)


if __name__ == "__main__":
    unittest.main()
