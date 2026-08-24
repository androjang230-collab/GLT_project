from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.models import EngineId
from engines.rpgmaker.detector import RpgMakerEngine
from engines.rpgmaker.extractor import RpgMakerExtractor
from engines.rpgmaker.inserter import ApplySafetyError
from engines.rpgmaker.validator import find_unexpected_changes


def _write_json(game: Path, file_name: str, document: object) -> None:
    path = game / "data" / file_name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def _read_json(game: Path, file_name: str) -> object:
    return json.loads((game / "data" / file_name).read_text(encoding="utf-8-sig"))


def _map(commands: list[dict[str, object]]) -> dict[str, object]:
    return {
        "displayName": "",
        "width": 20,
        "height": 15,
        "events": [
            None,
            {
                "id": 1,
                "name": "InternalEvent",
                "pages": [
                    {
                        "conditions": {"switch1Valid": False, "variableValue": 0},
                        "list": commands,
                    }
                ],
            },
        ],
    }


class RpgMakerInserterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary_directory.name)
        self.game = self.workspace / "game"
        self.output = self.workspace / "output"
        self.translation_file = self.workspace / "translated.jsonl"
        self._create_game(
            EngineId.RPGMAKER_MZ,
            [{"code": 401, "indent": 0, "parameters": ["こんにちは"]}],
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _create_game(
        self,
        engine: EngineId,
        commands: list[dict[str, object]],
        *,
        root: Path | None = None,
    ) -> Path:
        game = root or self.game
        core_name = "rpg_core.js" if engine == EngineId.RPGMAKER_MV else "rmmz_core.js"
        (game / "js").mkdir(parents=True, exist_ok=True)
        (game / "js" / core_name).write_text("// core", encoding="utf-8")
        _write_json(game, "System.json", {})
        _write_json(game, "Map001.json", _map(commands))
        (game / "readme.txt").write_text("unchanged", encoding="utf-8")
        return game

    def _records(
        self,
        *,
        game: Path | None = None,
        engine: EngineId = EngineId.RPGMAKER_MZ,
    ) -> list[dict[str, object]]:
        extraction = RpgMakerExtractor(engine).extract(game or self.game)
        self.assertFalse(extraction.issues)
        return [entry.to_json_dict() for entry in extraction.entries]

    def _write_records(
        self,
        records: list[dict[str, object]],
        *,
        path: Path | None = None,
    ) -> Path:
        destination = path or self.translation_file
        destination.write_text(
            "".join(
                json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
                for record in records
            ),
            encoding="utf-8",
        )
        return destination

    def test_applies_dialogue_to_separate_output(self) -> None:
        records = self._records()
        records[0]["translation"] = "안녕하세요"
        self._write_records(records)

        report = RpgMakerEngine().apply(self.game, self.translation_file, self.output)

        output_map = _read_json(self.output, "Map001.json")
        self.assertEqual(
            "안녕하세요",
            output_map["events"][1]["pages"][0]["list"][0]["parameters"][0],
        )
        self.assertEqual(1, report.applied)
        self.assertTrue(self.output.is_dir())

    def test_applies_each_choice_by_exact_json_path(self) -> None:
        _write_json(
            self.game,
            "Map001.json",
            _map([{"code": 102, "indent": 0, "parameters": [["はい", "いいえ"], -1, 0, 2, 0]}]),
        )
        records = self._records()
        records[0]["translation"] = "예"
        records[1]["translation"] = "아니요"
        self._write_records(records)

        report = RpgMakerEngine().apply(self.game, self.translation_file, self.output)

        choices = _read_json(self.output, "Map001.json")["events"][1]["pages"][0]["list"][0]["parameters"][0]
        self.assertEqual(["예", "아니요"], choices)
        self.assertEqual(2, report.applied)

    def test_applies_database_name_and_description(self) -> None:
        _write_json(
            self.game,
            "Items.json",
            [None, {"id": 1, "name": "薬草", "description": "体力を回復", "price": 50}],
        )
        records = self._records()
        for record in records:
            if record["original"] == "薬草":
                record["translation"] = "약초"
            elif record["original"] == "体力を回復":
                record["translation"] = "체력을 회복한다"
        self._write_records(records)

        RpgMakerEngine().apply(self.game, self.translation_file, self.output)

        item = _read_json(self.output, "Items.json")[1]
        self.assertEqual("약초", item["name"])
        self.assertEqual("체력을 회복한다", item["description"])
        self.assertEqual(50, item["price"])

    def test_blank_or_whitespace_translation_is_not_applied(self) -> None:
        records = self._records()
        records[0]["translation"] = "   "
        self._write_records(records)

        report = RpgMakerEngine().apply(self.game, self.translation_file, self.output)

        self.assertEqual("こんにちは", _read_json(self.output, "Map001.json")["events"][1]["pages"][0]["list"][0]["parameters"][0])
        self.assertEqual(0, report.applied)
        self.assertEqual(1, report.skipped_untranslated)

    def test_original_game_folder_remains_byte_identical(self) -> None:
        records = self._records()
        records[0]["translation"] = "번역"
        self._write_records(records)
        before = {
            path.relative_to(self.game): path.read_bytes()
            for path in self.game.rglob("*")
            if path.is_file()
        }

        RpgMakerEngine().apply(self.game, self.translation_file, self.output)

        after = {
            path.relative_to(self.game): path.read_bytes()
            for path in self.game.rglob("*")
            if path.is_file()
        }
        self.assertEqual(before, after)

    def test_existing_output_is_never_overwritten(self) -> None:
        self.output.mkdir()
        marker = self.output / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        self._write_records(self._records())

        with self.assertRaises(FileExistsError):
            RpgMakerEngine().apply(self.game, self.translation_file, self.output)

        self.assertEqual("keep", marker.read_text(encoding="utf-8"))

    def test_output_inside_source_is_rejected(self) -> None:
        self._write_records(self._records())

        with self.assertRaises(ApplySafetyError):
            RpgMakerEngine().apply(
                self.game,
                self.translation_file,
                self.game / "translated",
            )

    def test_source_text_mismatch_is_blocked_as_conflict(self) -> None:
        records = self._records()
        records[0]["original"] = "다른 버전의 원문"
        records[0]["translation"] = "번역"
        self._write_records(records)

        report = RpgMakerEngine().apply(self.game, self.translation_file, self.output)

        self.assertEqual(0, report.applied)
        self.assertEqual(1, report.conflicts)
        self.assertEqual("SOURCE_TEXT_MISMATCH", report.issues[0].code)

    def test_invalid_json_path_is_blocked(self) -> None:
        records = self._records()
        records[0]["json_path"] = "$.events[broken]"
        records[0]["translation"] = "번역"
        self._write_records(records)

        report = RpgMakerEngine().apply(self.game, self.translation_file, self.output)

        self.assertEqual(0, report.applied)
        self.assertIn("INVALID_JSON_PATH", {issue.code for issue in report.issues})

    def test_unknown_id_is_blocked(self) -> None:
        records = self._records()
        records[0]["id"] = "Map001:event999:page1:cmd401:index0:param0"
        records[0]["translation"] = "번역"
        self._write_records(records)

        report = RpgMakerEngine().apply(self.game, self.translation_file, self.output)

        self.assertEqual(0, report.applied)
        self.assertIn("UNKNOWN_ID", {issue.code for issue in report.issues})

    def test_wrong_parameter_index_metadata_is_blocked(self) -> None:
        records = self._records()
        records[0]["parameter_index"] = 99
        records[0]["translation"] = "번역"
        self._write_records(records)

        report = RpgMakerEngine().apply(self.game, self.translation_file, self.output)

        self.assertEqual(0, report.applied)
        self.assertIn("LOCATION_MISMATCH", {issue.code for issue in report.issues})

    def test_duplicate_ids_block_all_copies(self) -> None:
        records = self._records()
        records[0]["translation"] = "번역"
        self._write_records([records[0], dict(records[0])])

        report = RpgMakerEngine().apply(self.game, self.translation_file, self.output)

        self.assertEqual(0, report.applied)
        self.assertEqual(1, report.errors)
        self.assertEqual("DUPLICATE_ID", report.issues[0].code)

    def test_missing_control_code_is_blocked(self) -> None:
        _write_json(
            self.game,
            "Map001.json",
            _map([{"code": 401, "indent": 0, "parameters": [r"\C[2]\N[3]こんにちは"]}]),
        )
        records = self._records()
        records[0]["translation"] = r"\N[3]안녕하세요"
        self._write_records(records)

        report = RpgMakerEngine().apply(self.game, self.translation_file, self.output)

        self.assertEqual(0, report.applied)
        self.assertIn("CONTROL_CODE_MISMATCH", {issue.code for issue in report.issues})

    def test_changed_control_code_value_is_blocked(self) -> None:
        _write_json(
            self.game,
            "Map001.json",
            _map([{"code": 401, "indent": 0, "parameters": [r"\C[2]こんにちは"]}]),
        )
        records = self._records()
        records[0]["translation"] = r"\C[3]안녕하세요"
        self._write_records(records)

        report = RpgMakerEngine().apply(self.game, self.translation_file, self.output)

        self.assertEqual(0, report.applied)
        self.assertIn("CONTROL_CODE_MISMATCH", {issue.code for issue in report.issues})

    def test_changed_control_code_duplicate_count_is_blocked(self) -> None:
        _write_json(
            self.game,
            "Map001.json",
            _map([{"code": 401, "indent": 0, "parameters": [r"\C[2]\C[2]強調"]}]),
        )
        records = self._records()
        records[0]["translation"] = r"\C[2]강조"
        self._write_records(records)

        report = RpgMakerEngine().apply(self.game, self.translation_file, self.output)

        self.assertEqual(0, report.applied)
        self.assertIn("CONTROL_CODE_MISMATCH", {issue.code for issue in report.issues})

    def test_nontranslation_data_remains_unchanged(self) -> None:
        original = _read_json(self.game, "Map001.json")
        records = self._records()
        records[0]["translation"] = "번역"
        self._write_records(records)

        RpgMakerEngine().apply(self.game, self.translation_file, self.output)
        translated = _read_json(self.output, "Map001.json")

        allowed = {records[0]["json_path"]}
        self.assertEqual([], find_unexpected_changes(original, translated, allowed))
        self.assertEqual(original["width"], translated["width"])
        self.assertEqual(
            original["events"][1]["pages"][0]["conditions"],
            translated["events"][1]["pages"][0]["conditions"],
        )

    def test_json_structure_and_command_metadata_are_preserved(self) -> None:
        original = _read_json(self.game, "Map001.json")
        records = self._records()
        records[0]["translation"] = "번역"
        self._write_records(records)

        RpgMakerEngine().apply(self.game, self.translation_file, self.output)
        translated = _read_json(self.output, "Map001.json")

        self.assertEqual(original.keys(), translated.keys())
        self.assertEqual(len(original["events"]), len(translated["events"]))
        original_command = original["events"][1]["pages"][0]["list"][0]
        output_command = translated["events"][1]["pages"][0]["list"][0]
        self.assertEqual(original_command["code"], output_command["code"])
        self.assertEqual(original_command["indent"], output_command["indent"])

    def test_malformed_source_json_is_reported_while_valid_file_applies(self) -> None:
        records = self._records()
        records[0]["translation"] = "번역"
        self._write_records(records)
        (self.game / "data/Actors.json").write_text("[null,{broken", encoding="utf-8")

        report = RpgMakerEngine().apply(self.game, self.translation_file, self.output)

        self.assertEqual(1, report.applied)
        self.assertIn("SOURCE_JSON_ERROR", {issue.code for issue in report.issues})
        self.assertEqual(
            (self.game / "data/Actors.json").read_bytes(),
            (self.output / "data/Actors.json").read_bytes(),
        )

    def test_partial_failure_is_reported_and_valid_entry_applies(self) -> None:
        _write_json(
            self.game,
            "Map001.json",
            _map(
                [
                    {"code": 401, "indent": 0, "parameters": ["一つ目"]},
                    {"code": 401, "indent": 0, "parameters": ["二つ目"]},
                ]
            ),
        )
        records = self._records()
        records[0]["translation"] = "첫 번째"
        records[1]["original"] = "다른 원문"
        records[1]["translation"] = "두 번째"
        self._write_records(records)

        report = RpgMakerEngine().apply(self.game, self.translation_file, self.output)

        output_commands = _read_json(self.output, "Map001.json")["events"][1]["pages"][0]["list"]
        self.assertEqual("첫 번째", output_commands[0]["parameters"][0])
        self.assertEqual("二つ目", output_commands[1]["parameters"][0])
        self.assertEqual(1, report.applied)
        self.assertEqual(1, report.conflicts)

    def test_japanese_remaining_is_warning_but_translation_applies(self) -> None:
        records = self._records()
        records[0]["translation"] = "こんにちは, 안녕하세요"
        self._write_records(records)

        report = RpgMakerEngine().apply(self.game, self.translation_file, self.output)

        self.assertEqual(1, report.applied)
        self.assertEqual(1, report.warnings)
        self.assertEqual("JAPANESE_TEXT_REMAINS", report.issues[0].code)

    def test_untranslated_statistics_are_reported(self) -> None:
        _write_json(
            self.game,
            "Map001.json",
            _map(
                [
                    {"code": 401, "indent": 0, "parameters": ["一つ目"]},
                    {"code": 401, "indent": 0, "parameters": ["二つ目"]},
                ]
            ),
        )
        records = self._records()
        records[0]["translation"] = "첫 번째"
        records[1]["translation"] = ""
        self._write_records(records)

        report = RpgMakerEngine().apply(self.game, self.translation_file, self.output)

        self.assertEqual(2, report.total_translation_entries)
        self.assertEqual(1, report.translated_entries)
        self.assertEqual(1, report.untranslated_entries)
        self.assertEqual(1, report.skipped_untranslated)

    def test_report_is_saved_in_output(self) -> None:
        records = self._records()
        records[0]["translation"] = "번역"
        self._write_records(records)

        RpgMakerEngine().apply(self.game, self.translation_file, self.output)
        payload = json.loads(
            (self.output / "reports/apply_report.json").read_text(encoding="utf-8")
        )

        self.assertEqual(1, payload["applied"])
        self.assertEqual(1, payload["json_files_modified"])
        self.assertIn("files_copied", payload)

    def test_existing_source_report_is_not_overwritten(self) -> None:
        source_report = self.game / "reports/apply_report.json"
        source_report.parent.mkdir()
        source_report.write_text("original report", encoding="utf-8")
        records = self._records()
        records[0]["translation"] = "번역"
        self._write_records(records)

        with self.assertRaises(ApplySafetyError):
            RpgMakerEngine().apply(self.game, self.translation_file, self.output)

        self.assertFalse(self.output.exists())
        self.assertEqual("original report", source_report.read_text(encoding="utf-8"))

    def test_mv_and_mz_share_safe_apply_behavior(self) -> None:
        for engine in (EngineId.RPGMAKER_MV, EngineId.RPGMAKER_MZ):
            with self.subTest(engine=engine.value):
                game = self.workspace / f"game-{engine.value}"
                output = self.workspace / f"output-{engine.value}"
                translations = self.workspace / f"{engine.value}.jsonl"
                self._create_game(
                    engine,
                    [{"code": 401, "indent": 0, "parameters": ["原文"]}],
                    root=game,
                )
                records = self._records(game=game, engine=engine)
                records[0]["translation"] = "번역"
                self._write_records(records, path=translations)

                report = RpgMakerEngine().apply(game, translations, output)

                self.assertEqual(engine, report.engine)
                self.assertEqual(1, report.applied)


if __name__ == "__main__":
    unittest.main()
