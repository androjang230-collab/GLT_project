from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.models import EngineId
from engines.rpgmaker.detector import RpgMakerEngine
from engines.rpgmaker.extractor import RpgMakerExtractor, find_control_codes


def _write_json(root: Path, file_name: str, document: object) -> None:
    path = root / "data" / file_name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=False),
        encoding="utf-8",
    )


def _map_document(
    commands: list[dict[str, object]],
    *,
    event_id: int = 17,
) -> dict[str, object]:
    return {
        "displayName": "",
        "events": [
            None,
            {
                "id": event_id,
                "name": "internal event name",
                "pages": [{"list": commands}],
            },
        ],
    }


class RpgMakerExtractorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.extractor = RpgMakerExtractor(EngineId.RPGMAKER_MZ)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_extracts_map_dialogue_and_mz_speaker(self) -> None:
        _write_json(
            self.root,
            "Map003.json",
            _map_document(
                [
                    {"code": 101, "parameters": ["", 0, 0, 2, "セリカ"]},
                    {"code": 401, "parameters": ["何をしているの？"]},
                    {"code": 0, "parameters": []},
                ]
            ),
        )

        result = self.extractor.extract(self.root)
        dialogue = next(entry for entry in result.entries if entry.type == "dialogue")
        speaker = next(entry for entry in result.entries if entry.type == "speaker")

        self.assertEqual("何をしているの？", dialogue.original)
        self.assertEqual("セリカ", dialogue.speaker)
        self.assertEqual("セリカ", speaker.original)
        self.assertEqual(
            "Map003:event17:page1:cmd401:index1:param0",
            dialogue.id,
        )
        self.assertEqual(1, dialogue.command_index)
        self.assertEqual(0, dialogue.parameter_index)
        self.assertEqual("$.events[1].pages[0].list[1].parameters[0]", dialogue.json_path)

    def test_extracts_each_choice(self) -> None:
        _write_json(
            self.root,
            "Map001.json",
            _map_document(
                [{"code": 102, "parameters": [["はい", "いいえ"], -1, 0, 2, 0]}]
            ),
        )

        choices = [
            entry
            for entry in self.extractor.extract(self.root).entries
            if entry.type == "choice"
        ]

        self.assertEqual(["はい", "いいえ"], [entry.original for entry in choices])
        self.assertNotEqual(choices[0].id, choices[1].id)
        self.assertTrue(choices[1].id.endswith("param0:choice1"))

    def test_extracts_scroll_text(self) -> None:
        _write_json(
            self.root,
            "Map001.json",
            _map_document(
                [
                    {"code": 105, "parameters": [2, False]},
                    {"code": 405, "parameters": ["長い物語"]},
                ]
            ),
        )

        entries = self.extractor.extract(self.root).entries

        self.assertEqual(
            ["長い物語"],
            [entry.original for entry in entries if entry.type == "scroll_text"],
        )

    def test_extracts_database_names_and_descriptions(self) -> None:
        _write_json(
            self.root,
            "Actors.json",
            [None, {"id": 1, "name": "勇者", "nickname": "光の子", "profile": "主人公"}],
        )
        _write_json(
            self.root,
            "Items.json",
            [None, {"id": 1, "name": "薬草", "description": "体力を回復する。"}],
        )

        entries = self.extractor.extract(self.root).entries
        by_id = {entry.id: entry for entry in entries}

        self.assertEqual("勇者", by_id["Actors:index1:name"].original)
        self.assertEqual("description", by_id["Actors:index1:profile"].type)
        self.assertEqual("item_name", by_id["Items:index1:name"].type)
        self.assertEqual("$[1].description", by_id["Items:index1:description"].json_path)

    def test_extracts_explicit_system_fields_but_not_switches_or_variables(self) -> None:
        _write_json(
            self.root,
            "System.json",
            {
                "gameTitle": "星の物語",
                "currencyUnit": "G",
                "elements": ["", "炎"],
                "terms": {
                    "commands": ["戦う"],
                    "messages": {"alwaysDash": "常時ダッシュ"},
                },
                "switches": ["", "秘密のスイッチ"],
                "variables": ["", "内部カウンター"],
            },
        )

        entries = self.extractor.extract(self.root).entries
        originals = {entry.original for entry in entries}

        self.assertIn("星の物語", originals)
        self.assertIn("炎", originals)
        self.assertIn("戦う", originals)
        self.assertNotIn("秘密のスイッチ", originals)
        self.assertNotIn("内部カウンター", originals)

    def test_excludes_nontranslation_fields_and_script_commands(self) -> None:
        _write_json(
            self.root,
            "Actors.json",
            [
                None,
                {
                    "id": 1,
                    "name": "勇者",
                    "faceName": "Actor1",
                    "characterName": "Hero.png",
                    "note": "<PluginSetting:秘密>",
                },
            ],
        )
        _write_json(
            self.root,
            "Map001.json",
            _map_document(
                [
                    {"code": 355, "parameters": ["console.log('秘密')"]},
                    {"code": 356, "parameters": ["Plugin Command Secret"]},
                    {"code": 250, "parameters": [{"name": "Audio/BGM/theme"}]},
                ]
            ),
        )

        originals = {
            entry.original for entry in self.extractor.extract(self.root).entries
        }

        self.assertEqual({"勇者"}, originals)

    def test_id_is_stable_when_original_text_changes(self) -> None:
        first_document = _map_document([{"code": 401, "parameters": ["原文A"]}])
        _write_json(self.root, "Map001.json", first_document)
        first = self.extractor.extract(self.root).entries[0]

        second_document = _map_document([{"code": 401, "parameters": ["原文B"]}])
        _write_json(self.root, "Map001.json", second_document)
        second = self.extractor.extract(self.root).entries[0]

        self.assertEqual(first.id, second.id)
        self.assertNotEqual(first.original, second.original)

    def test_extraction_does_not_modify_source_json(self) -> None:
        _write_json(
            self.root,
            "Map001.json",
            _map_document([{"code": 401, "parameters": ["原本保護"]}]),
        )
        source_file = self.root / "data/Map001.json"
        original_bytes = source_file.read_bytes()

        self.extractor.extract(self.root)

        self.assertEqual(original_bytes, source_file.read_bytes())

    def test_detects_control_codes_in_order(self) -> None:
        text = r"\V[1]\N[3]\C[2]\I[45]\{強調\}\V[1]"

        self.assertEqual(
            (r"\V[1]", r"\N[3]", r"\C[2]", r"\I[45]", r"\{", r"\}", r"\V[1]"),
            find_control_codes(text),
        )

    def test_malformed_json_is_reported_and_other_files_continue(self) -> None:
        data_directory = self.root / "data"
        data_directory.mkdir()
        (data_directory / "Actors.json").write_text(
            '[null, {"name": "broken"',
            encoding="utf-8",
        )
        _write_json(
            self.root,
            "Items.json",
            [None, {"name": "薬草", "description": "回復"}],
        )

        result = self.extractor.extract(self.root)

        self.assertEqual(["薬草", "回復"], [entry.original for entry in result.entries])
        self.assertEqual(1, len(result.issues))
        self.assertEqual("data/Actors.json", result.issues[0].file)
        self.assertIn("line 1, column", result.issues[0].message)

    def test_duplicate_ids_are_reported_and_not_written_twice(self) -> None:
        duplicate_event = {
            "id": 7,
            "pages": [{"list": [{"code": 401, "parameters": ["同じ位置"]}]}],
        }
        _write_json(
            self.root,
            "Map001.json",
            {"events": [duplicate_event, duplicate_event]},
        )

        result = self.extractor.extract(self.root)

        self.assertEqual(1, len(result.entries))
        self.assertEqual(1, len(result.issues))
        self.assertIn("duplicate translation ID", result.issues[0].message)

    def test_common_events_and_troop_event_pages_are_supported(self) -> None:
        _write_json(
            self.root,
            "CommonEvents.json",
            [None, {"id": 1, "name": "internal", "list": [{"code": 401, "parameters": ["共通台詞"]}]}],
        )
        _write_json(
            self.root,
            "Troops.json",
            [None, {"id": 2, "name": "internal", "pages": [{"list": [{"code": 405, "parameters": ["戦闘文"]}]}]}],
        )

        entries = self.extractor.extract(self.root).entries
        originals = {entry.original for entry in entries}

        self.assertEqual({"共通台詞", "戦闘文"}, originals)

    def test_mv_and_mz_use_the_same_extraction_rules_and_engine_id(self) -> None:
        for engine_id, core_file in (
            (EngineId.RPGMAKER_MV, "rpg_core.js"),
            (EngineId.RPGMAKER_MZ, "rmmz_core.js"),
        ):
            with self.subTest(engine=engine_id.value):
                game = self.root / engine_id.value
                (game / "js").mkdir(parents=True)
                (game / "js" / core_file).write_text("", encoding="utf-8")
                _write_json(game, "System.json", {})
                _write_json(
                    game,
                    "Map001.json",
                    _map_document([{"code": 401, "parameters": ["共通"]}]),
                )
                output = self.root / f"{engine_id.value}.jsonl"

                result = RpgMakerEngine().extract(game, output)
                payload = json.loads(output.read_text(encoding="utf-8").strip())

                self.assertEqual(engine_id, result.entries[0].engine)
                self.assertEqual(engine_id.value, payload["engine"])
                self.assertEqual("", payload["translation"])


if __name__ == "__main__":
    unittest.main()
