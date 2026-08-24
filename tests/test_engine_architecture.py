from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.detector import EngineDetector
from core.engine import EnginePlugin
from core.errors import ApplySafetyError as CoreApplySafetyError
from core.models import DetectionResult, EngineId, TranslationEntry
from core.registry import EngineRegistry
from engines.registry import create_engine_registry
from engines.rpgmaker.detector import RpgMakerEngine as LegacyRpgMakerEngine
from engines.rpgmaker.engine import RpgMakerEngine
from engines.rpgmaker.extractor import RpgMakerExtractor, write_jsonl
from engines.rpgmaker.inserter import ApplySafetyError as LegacyApplySafetyError
from projects.io import write_json
from projects.manager import ProjectManager


def _write_game_json(game: Path, relative: str, payload: object) -> None:
    path = game / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def _rpgmaker_game(root: Path, engine: EngineId = EngineId.RPGMAKER_MZ) -> Path:
    core_name = "rpg_core.js" if engine == EngineId.RPGMAKER_MV else "rmmz_core.js"
    (root / "js").mkdir(parents=True)
    (root / "js" / core_name).write_text("// core", encoding="utf-8")
    _write_game_json(root, "data/System.json", {"gameTitle": "試験ゲーム"})
    _write_game_json(
        root,
        "data/MapInfos.json",
        [None, {"id": 1, "name": "第一マップ", "parentId": 0, "order": 1}],
    )
    _write_game_json(
        root,
        "data/Map001.json",
        {
            "events": [
                None,
                {
                    "id": 1,
                    "pages": [
                        {
                            "list": [
                                {"code": 101, "indent": 0, "parameters": ["", 0, 0, 2, "セリカ"]},
                                {"code": 401, "indent": 0, "parameters": ["\\C[2]こんにちは\\V[1]"]},
                                {"code": 102, "indent": 0, "parameters": [["はい", "いいえ"], 0, 0, 2, 0]},
                            ]
                        }
                    ],
                },
            ]
        },
    )
    return root


class _UnknownAdapter(EnginePlugin):
    adapter_id = "unknown-fixture"

    def detect(self, game_directory: Path) -> DetectionResult:
        return DetectionResult.unknown(confidence=17, evidence=("marker.bin",))


class _LegacyAdapterWithoutId(EnginePlugin):
    def detect(self, game_directory: Path) -> DetectionResult:
        return DetectionResult.unknown()


class EngineArchitectureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary_directory.name)
        self.game = _rpgmaker_game(self.workspace / "game")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_default_registry_maps_mv_and_mz_to_the_rpgmaker_adapter(self) -> None:
        registry = create_engine_registry()

        rpgmaker = registry.adapter_for(EngineId.RPGMAKER_MV)
        self.assertEqual(2, len(registry.engines))
        self.assertIsNotNone(rpgmaker)
        self.assertIs(registry.adapter_for(EngineId.RPGMAKER_MZ), rpgmaker)
        self.assertEqual("rpgmaker", rpgmaker.adapter_id)

    def test_registry_selects_rpgmaker_detector_adapter(self) -> None:
        selection = create_engine_registry().identify(self.game)

        self.assertTrue(selection.detected)
        self.assertIsInstance(selection.adapter, RpgMakerEngine)
        self.assertEqual(EngineId.RPGMAKER_MZ, selection.detection.engine)
        self.assertIn("js/rmmz_core.js", selection.detection.evidence)

    def test_unknown_engine_detection_keeps_best_nonconfirming_evidence(self) -> None:
        registry = EngineRegistry((_UnknownAdapter(),))

        selection = registry.identify(self.workspace / "unknown-game")

        self.assertFalse(selection.detected)
        self.assertIsNone(selection.adapter)
        self.assertEqual(17, selection.detection.confidence)
        self.assertEqual(("marker.bin",), selection.detection.evidence)

    def test_legacy_rpgmaker_import_path_is_the_same_adapter(self) -> None:
        self.assertIs(LegacyRpgMakerEngine, RpgMakerEngine)

    def test_legacy_engine_detector_and_safety_error_imports_remain_valid(self) -> None:
        result = EngineDetector((_LegacyAdapterWithoutId(),)).detect(self.game)

        self.assertFalse(result.detected)
        self.assertIs(CoreApplySafetyError, LegacyApplySafetyError)

    def test_adapter_extraction_matches_legacy_extractor_and_serialized_jsonl(self) -> None:
        legacy = RpgMakerExtractor(EngineId.RPGMAKER_MZ).extract(self.game)
        adapter = RpgMakerEngine()
        adapted = adapter.extract_entries(self.game)
        legacy_file = self.workspace / "legacy.jsonl"
        adapted_file = self.workspace / "adapted.jsonl"

        write_jsonl(legacy.entries, legacy_file)
        adapter.extract(self.game, adapted_file)

        self.assertEqual(
            [entry.to_json_dict() for entry in legacy.entries],
            [entry.to_json_dict() for entry in adapted.entries],
        )
        self.assertEqual(legacy_file.read_bytes(), adapted_file.read_bytes())

    def test_canonical_ids_and_metadata_remain_exact(self) -> None:
        entries = RpgMakerEngine().extract_entries(self.game).entries
        by_id = {entry.id: entry.to_json_dict() for entry in entries}

        speaker_id = "Map001:event1:page1:cmd101:index0:param4"
        dialogue_id = "Map001:event1:page1:cmd401:index1:param0"
        choice_id = "Map001:event1:page1:cmd102:index2:param0:choice1"
        self.assertIn(speaker_id, by_id)
        self.assertIn(dialogue_id, by_id)
        self.assertIn(choice_id, by_id)
        self.assertEqual(
            "$.events[1].pages[0].list[1].parameters[0]",
            by_id[dialogue_id]["json_path"],
        )
        self.assertEqual("data/Map001.json", by_id[dialogue_id]["file"])
        self.assertEqual("セリカ", by_id[dialogue_id]["speaker"])
        self.assertEqual(["\\C[2]", "\\V[1]"], by_id[dialogue_id]["control_codes"])
        self.assertEqual(1, by_id[dialogue_id]["event_id"])
        self.assertEqual(1, by_id[dialogue_id]["page_id"])
        self.assertEqual(1, by_id[dialogue_id]["command_index"])
        self.assertEqual(0, by_id[dialogue_id]["parameter_index"])

    def test_rpgmaker_apply_runs_through_adapter(self) -> None:
        adapter = RpgMakerEngine()
        translation_file = self.workspace / "translated.jsonl"
        output = self.workspace / "game-ko"
        records = [entry.to_json_dict() for entry in adapter.extract_entries(self.game).entries]
        target_id = "Map001:event1:page1:cmd401:index1:param0"
        for record in records:
            if record["id"] == target_id:
                record["translation"] = "\\C[2]안녕하세요\\V[1]"
        translation_file.write_text(
            "".join(
                json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
                for record in records
            ),
            encoding="utf-8",
        )

        report = adapter.apply(self.game, translation_file, output)
        document = json.loads((output / "data/Map001.json").read_text(encoding="utf-8"))

        self.assertEqual(1, report.applied)
        self.assertEqual(
            "\\C[2]안녕하세요\\V[1]",
            document["events"][1]["pages"][0]["list"][1]["parameters"][0],
        )

    def test_legacy_projects_load_and_run_qa_without_migration(self) -> None:
        project = self.workspace / "project"
        manager = ProjectManager()
        manager.create(self.game, project)
        project_file = project / "project.json"
        config = json.loads(project_file.read_text(encoding="utf-8"))
        original_keys = set(config)
        for legacy_version in ("0.5.6", "0.6.0"):
            with self.subTest(tool_version=legacy_version):
                config["tool_version"] = legacy_version
                write_json(project_file, config)

                context = manager.load(project)
                result = manager.qa(project, self.game)

                self.assertEqual(legacy_version, context.config.tool_version)
                self.assertEqual(
                    original_keys,
                    set(json.loads(project_file.read_text(encoding="utf-8"))),
                )
                self.assertEqual(0, result.report.errors)
                self.assertEqual(0, result.report.conflicts)

    def test_common_translation_entry_preserves_future_engine_metadata(self) -> None:
        entry = TranslationEntry(
            id="future:location:1",
            engine=EngineId.RPGMAKER_MZ,
            file="binary/database.dat",
            type="dialogue",
            original="text",
            extra_metadata={"binary_offset": 42, "record_kind": "message"},
        )

        payload = entry.to_json_dict()

        self.assertEqual(42, payload["binary_offset"])
        self.assertEqual("message", payload["record_kind"])


if __name__ == "__main__":
    unittest.main()
