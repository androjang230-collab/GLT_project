from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from core.models import EngineId
from core.version import SCHEMA_VERSION, TOOL_VERSION
from engines.rpgmaker.detector import RpgMakerEngine
from engines.rpgmaker.extractor import RpgMakerExtractor
from engines.rpgmaker.fingerprint import calculate_game_fingerprint
from engines.rpgmaker.inserter import ApplySafetyError


def _write_json(game: Path, name: str, payload: object) -> None:
    path = game / "data" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def _map(commands: list[dict[str, object]]) -> dict[str, object]:
    return {
        "events": [
            None,
            {"id": 1, "pages": [{"list": commands}]},
        ]
    }


def _tree_bytes(root: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


class RpgMakerQaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary_directory.name)
        self.game = self._create_game(
            self.workspace / "game",
            EngineId.RPGMAKER_MZ,
            [{"code": 401, "parameters": ["こんにちは"]}],
        )
        self.translation_file = self.workspace / "translated.jsonl"
        self.reports = self.workspace / "reports"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def _create_game(
        root: Path,
        engine: EngineId,
        commands: list[dict[str, object]],
    ) -> Path:
        core = "rpg_core.js" if engine == EngineId.RPGMAKER_MV else "rmmz_core.js"
        (root / "js").mkdir(parents=True)
        (root / "js" / core).write_text("// engine core", encoding="utf-8")
        _write_json(root, "System.json", {})
        _write_json(root, "Map001.json", _map(commands))
        return root

    @staticmethod
    def _records(game: Path, engine: EngineId) -> list[dict[str, object]]:
        result = RpgMakerExtractor(engine).extract(game)
        if result.issues:
            raise AssertionError(result.issues)
        return [entry.to_json_dict() for entry in result.entries]

    def _write_records(
        self,
        records: list[dict[str, object]],
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

    def test_qa_passes_clean_translation_and_writes_all_reports(self) -> None:
        records = self._records(self.game, EngineId.RPGMAKER_MZ)
        records[0]["translation"] = "안녕하세요"
        self._write_records(records)

        result = RpgMakerEngine().qa(self.game, self.translation_file, self.reports)

        self.assertEqual(0, result.report.errors)
        self.assertEqual(0, result.report.conflicts)
        self.assertEqual(1, result.report.applicable)
        self.assertTrue((self.reports / "qa_report.json").is_file())
        self.assertTrue((self.reports / "qa_issues.csv").is_file())
        self.assertTrue((self.reports / "untranslated.csv").is_file())
        self.assertTrue((self.reports / "project_manifest.json").is_file())

    def test_untranslated_statistics_and_progress(self) -> None:
        _write_json(
            self.game,
            "Map001.json",
            _map(
                [
                    {"code": 401, "parameters": ["一つ"]},
                    {"code": 401, "parameters": ["二つ"]},
                ]
            ),
        )
        records = self._records(self.game, EngineId.RPGMAKER_MZ)
        records[0]["translation"] = "하나"
        records[1]["translation"] = "   "
        self._write_records(records)

        result = RpgMakerEngine().qa(self.game, self.translation_file, self.reports)
        payload = json.loads((self.reports / "qa_report.json").read_text(encoding="utf-8"))

        self.assertEqual(2, result.report.total_translation_entries)
        self.assertEqual(1, result.report.untranslated_entries)
        self.assertEqual(50.0, result.translation_percentage)
        self.assertEqual(50.0, payload["translation_percentage"])
        untranslated_csv = (self.reports / "untranslated.csv").read_text(encoding="utf-8-sig")
        self.assertIn("二つ", untranslated_csv)

    def test_hiragana_and_katakana_remain_as_warning(self) -> None:
        records = self._records(self.game, EngineId.RPGMAKER_MZ)
        records[0]["translation"] = "ひらがな カタカナ"
        self._write_records(records)

        result = RpgMakerEngine().qa(self.game, self.translation_file, self.reports)
        payload = json.loads((self.reports / "qa_report.json").read_text(encoding="utf-8"))

        self.assertEqual(1, result.report.warnings)
        self.assertEqual(1, payload["hiragana_remains"])
        self.assertEqual(1, payload["katakana_remains"])
        self.assertEqual(1, payload["japanese_remains"])

    def test_cjk_only_is_info_not_warning(self) -> None:
        records = self._records(self.game, EngineId.RPGMAKER_MZ)
        records[0]["translation"] = "魔王"
        self._write_records(records)

        result = RpgMakerEngine().qa(self.game, self.translation_file, self.reports)
        payload = json.loads((self.reports / "qa_report.json").read_text(encoding="utf-8"))

        self.assertEqual(0, result.report.warnings)
        self.assertIn("CJK_KANJI_REMAINS", {issue.code for issue in result.report.issues})
        self.assertEqual(1, payload["kanji_only"])

    def test_literal_allowlist_suppresses_japanese_warning(self) -> None:
        records = self._records(self.game, EngineId.RPGMAKER_MZ)
        records[0]["translation"] = "이름은 セリカ입니다"
        self._write_records(records)
        allowlist = self.workspace / "japanese_allowlist.txt"
        allowlist.write_text("# intentional name\nセリカ\n", encoding="utf-8")

        result = RpgMakerEngine().qa(
            self.game,
            self.translation_file,
            self.reports,
            allowlist_path=allowlist,
        )

        self.assertEqual(0, result.report.warnings)
        self.assertNotIn("JAPANESE_TEXT_REMAINS", {issue.code for issue in result.report.issues})

    def test_control_code_mismatch_is_error(self) -> None:
        _write_json(
            self.game,
            "Map001.json",
            _map([{"code": 401, "parameters": [r"\C[2]こんにちは"]}]),
        )
        records = self._records(self.game, EngineId.RPGMAKER_MZ)
        records[0]["translation"] = "안녕하세요"
        self._write_records(records)

        result = RpgMakerEngine().qa(self.game, self.translation_file, self.reports)
        payload = json.loads((self.reports / "qa_report.json").read_text(encoding="utf-8"))

        self.assertIn("CONTROL_CODE_MISMATCH", {issue.code for issue in result.report.issues})
        self.assertEqual(1, payload["control_code_errors"])

    def test_source_mismatch_is_conflict(self) -> None:
        records = self._records(self.game, EngineId.RPGMAKER_MZ)
        records[0]["original"] = "다른 버전"
        records[0]["translation"] = "번역"
        self._write_records(records)

        result = RpgMakerEngine().qa(self.game, self.translation_file, self.reports)
        payload = json.loads((self.reports / "qa_report.json").read_text(encoding="utf-8"))

        self.assertEqual(1, result.report.conflicts)
        self.assertEqual(1, payload["source_mismatches"])

    def test_duplicate_id_is_error(self) -> None:
        records = self._records(self.game, EngineId.RPGMAKER_MZ)
        records[0]["translation"] = "번역"
        self._write_records([records[0], dict(records[0])])

        result = RpgMakerEngine().qa(self.game, self.translation_file, self.reports)

        self.assertIn("DUPLICATE_ID", {issue.code for issue in result.report.issues})
        self.assertEqual(0, result.report.applicable)

    def test_file_type_and_json_path_mismatches_are_distinct(self) -> None:
        mutations = (
            ("file", "data/System.json", "FILE_MISMATCH"),
            ("type", "choice", "TYPE_MISMATCH"),
            (
                "json_path",
                "$.events[1].pages[0].list[0].parameters[1]",
                "JSON_PATH_MISMATCH",
            ),
        )
        for index, (field, value, expected_code) in enumerate(mutations):
            with self.subTest(field=field):
                records = self._records(self.game, EngineId.RPGMAKER_MZ)
                records[0][field] = value
                records[0]["translation"] = "번역"
                translations = self._write_records(
                    records,
                    self.workspace / f"mismatch-{index}.jsonl",
                )
                result = RpgMakerEngine().qa(
                    self.game,
                    translations,
                    self.workspace / f"mismatch-reports-{index}",
                )

                self.assertIn(
                    expected_code,
                    {issue.code for issue in result.report.issues},
                )

    def test_malformed_translation_jsonl_is_reported(self) -> None:
        self.translation_file.write_text('{"id": broken\n', encoding="utf-8")

        result = RpgMakerEngine().qa(self.game, self.translation_file, self.reports)

        self.assertIn(
            "MALFORMED_TRANSLATION_JSONL",
            {issue.code for issue in result.report.issues},
        )
        self.assertEqual(1, result.report.total_translation_entries)

    def test_malformed_source_json_is_reported(self) -> None:
        records = self._records(self.game, EngineId.RPGMAKER_MZ)
        self._write_records(records)
        (self.game / "data/Actors.json").write_text("[null,{broken", encoding="utf-8")

        result = RpgMakerEngine().qa(self.game, self.translation_file, self.reports)

        self.assertIn("SOURCE_JSON_ERROR", {issue.code for issue in result.report.issues})

    def test_dry_run_does_not_create_output(self) -> None:
        records = self._records(self.game, EngineId.RPGMAKER_MZ)
        records[0]["translation"] = "번역"
        self._write_records(records)
        output = self.workspace / "dry-output"

        report = RpgMakerEngine().apply(
            self.game,
            self.translation_file,
            output,
            dry_run=True,
        )

        self.assertFalse(output.exists())
        self.assertEqual(1, report.applicable)
        self.assertEqual(0, report.applied)
        self.assertEqual(["data/Map001.json"], report.planned_files)

    def test_dry_run_and_apply_share_preflight_results(self) -> None:
        records = self._records(self.game, EngineId.RPGMAKER_MZ)
        records[0]["translation"] = "こんにちは 번역"
        self._write_records(records)
        dry_output = self.workspace / "dry-output"
        actual_output = self.workspace / "actual-output"

        dry = RpgMakerEngine().apply(
            self.game,
            self.translation_file,
            dry_output,
            dry_run=True,
        )
        actual = RpgMakerEngine().apply(
            self.game,
            self.translation_file,
            actual_output,
        )

        self.assertEqual(dry.applicable, actual.applicable)
        self.assertEqual(dry.planned_ids, actual.planned_ids)
        self.assertEqual(dry.warnings, actual.warnings)
        self.assertEqual(dry.errors, actual.errors)
        self.assertEqual(dry.conflicts, actual.conflicts)

    def test_fingerprint_is_stable_and_path_independent(self) -> None:
        first = calculate_game_fingerprint(self.game, EngineId.RPGMAKER_MZ)
        copied_game = self.workspace / "another-drive-location"
        shutil.copytree(self.game, copied_game)
        second = calculate_game_fingerprint(copied_game, EngineId.RPGMAKER_MZ)

        self.assertEqual(first.value, second.value)
        self.assertEqual(first.value, calculate_game_fingerprint(self.game, EngineId.RPGMAKER_MZ).value)

    def test_fingerprint_changes_when_selected_game_file_changes(self) -> None:
        before = calculate_game_fingerprint(self.game, EngineId.RPGMAKER_MZ)
        map_document = json.loads((self.game / "data/Map001.json").read_text(encoding="utf-8"))
        map_document["events"][1]["pages"][0]["list"][0]["parameters"][0] = "変更"
        _write_json(self.game, "Map001.json", map_document)

        after = calculate_game_fingerprint(self.game, EngineId.RPGMAKER_MZ)

        self.assertNotEqual(before.value, after.value)

    def test_project_manifest_is_portable_and_versioned(self) -> None:
        records = self._records(self.game, EngineId.RPGMAKER_MZ)
        self._write_records(records)

        RpgMakerEngine().qa(self.game, self.translation_file, self.reports)
        manifest_text = (self.reports / "project_manifest.json").read_text(encoding="utf-8")
        manifest = json.loads(manifest_text)

        self.assertEqual(TOOL_VERSION, manifest["tool_version"])
        self.assertEqual(SCHEMA_VERSION, manifest["schema_version"])
        self.assertEqual(1, manifest["translation_entry_count"])
        self.assertNotIn(str(self.workspace), manifest_text)
        self.assertTrue(manifest["fingerprint_files"])

    def test_qa_does_not_modify_original_game(self) -> None:
        records = self._records(self.game, EngineId.RPGMAKER_MZ)
        records[0]["translation"] = "번역"
        self._write_records(records)
        before = _tree_bytes(self.game)

        RpgMakerEngine().qa(self.game, self.translation_file, self.reports)

        self.assertEqual(before, _tree_bytes(self.game))

    def test_reports_inside_game_are_rejected(self) -> None:
        self._write_records(self._records(self.game, EngineId.RPGMAKER_MZ))

        with self.assertRaises(ApplySafetyError):
            RpgMakerEngine().qa(
                self.game,
                self.translation_file,
                self.game / "reports",
            )

    def test_mv_and_mz_use_same_qa_rules(self) -> None:
        for engine in (EngineId.RPGMAKER_MV, EngineId.RPGMAKER_MZ):
            with self.subTest(engine=engine.value):
                game = self._create_game(
                    self.workspace / engine.value,
                    engine,
                    [{"code": 401, "parameters": ["原文"]}],
                )
                records = self._records(game, engine)
                records[0]["translation"] = "번역"
                translations = self._write_records(
                    records,
                    self.workspace / f"{engine.value}.jsonl",
                )
                reports = self.workspace / f"reports-{engine.value}"

                result = RpgMakerEngine().qa(game, translations, reports)

                self.assertEqual(engine, result.report.engine)
                self.assertEqual(1, result.report.applicable)


if __name__ == "__main__":
    unittest.main()
