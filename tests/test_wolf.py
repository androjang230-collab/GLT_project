from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import glt
from core.errors import UnsupportedEngineOperationError
from core.models import EngineId
from engines.registry import create_engine_registry
from engines.wolf.detector import DETECTION_THRESHOLD, WolfDetector
from engines.wolf.engine import WolfRPGEngine


def _write(path: Path, data: bytes = b"") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _unpacked_wolf(root: Path) -> Path:
    _write(root / "Game.exe", b"MZ synthetic executable")
    _write(root / "Config.exe", b"MZ config")
    _write(root / "Data/BasicData/Game.dat", b"WOLF-GAME-DATA")
    _write(root / "Data/BasicData/CommonEvent.dat", b"WOLF-COMMON")
    _write(root / "Data/BasicData/TileSetData.dat", b"WOLF-TILES")
    _write(root / "Data/BasicData/UserDataBase.dat", b"WOLF-DB")
    _write(root / "Data/MapData/Map001.mps", b"WOLF-MAP")
    _write(root / "Data/BGM/theme.ogg", b"audio")
    _write(root / "Data/Fonts/game.ttf", b"font")
    _write(root / "Data/readme.txt", "翻訳候補".encode("utf-8"))
    return root


def _packed_wolf(root: Path) -> Path:
    _write(root / "Game.exe", b"MZ synthetic executable")
    _write(root / "Data.wolf", b"synthetic encrypted archive")
    return root


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


class WolfPhaseOneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_detector_positive_unpacked_fixture_uses_multiple_evidence(self) -> None:
        game = _unpacked_wolf(self.workspace / "wolf-unpacked")

        result = WolfDetector().detect(game)

        self.assertTrue(result.detected)
        self.assertEqual(EngineId.WOLF_RPG_EDITOR, result.engine)
        self.assertGreaterEqual(result.confidence, DETECTION_THRESHOLD)
        self.assertIn("Game.exe", result.evidence)
        self.assertIn("Data/BasicData/Game.dat", result.evidence)
        self.assertIn("Data/BasicData/CommonEvent.dat", result.evidence)
        self.assertIn("Data/MapData/Map001.mps", result.evidence)

    def test_detector_positive_packed_fixture(self) -> None:
        game = _packed_wolf(self.workspace / "wolf-packed")

        result = WolfDetector().detect(game)

        self.assertTrue(result.detected)
        self.assertEqual(85, result.confidence)
        self.assertEqual(("Game.exe", "Data.wolf"), result.evidence)

    def test_detector_recognizes_official_basicdata_folder_archive_layout(self) -> None:
        game = self.workspace / "wolf-folder-packed"
        _write(game / "Game.exe", b"MZ synthetic executable")
        _write(game / "Data/BasicData.wolf", b"synthetic folder archive")

        result = WolfDetector().detect(game)

        self.assertTrue(result.detected)
        self.assertEqual(80, result.confidence)
        self.assertEqual(
            ("Game.exe", "Data", "Data/BasicData.wolf"),
            result.evidence,
        )

    def test_game_executable_alone_is_weak_and_not_confirmed(self) -> None:
        game = self.workspace / "weak"
        _write(game / "Game.exe", b"generic executable")

        result = WolfDetector().detect(game)

        self.assertFalse(result.detected)
        self.assertEqual(30, result.confidence)
        self.assertEqual(("Game.exe",), result.evidence)

    def test_data_wolf_extension_alone_is_not_enough_to_confirm(self) -> None:
        game = self.workspace / "archive-only"
        _write(game / "Data.wolf", b"not enough evidence by itself")

        result = WolfDetector().detect(game)

        self.assertFalse(result.detected)
        self.assertEqual(55, result.confidence)
        self.assertEqual(("Data.wolf",), result.evidence)

    def test_custom_assets_candidate_remains_low_confidence(self) -> None:
        game = self.workspace / "custom-assets"
        _write(game / "GamePro.exe", b"synthetic executable")
        _write(game / "Data.assets", b"ambiguous custom archive")

        result = WolfDetector().detect(game)

        self.assertFalse(result.detected)
        self.assertEqual(35, result.confidence)
        self.assertEqual(("GamePro.exe", "Data.assets"), result.evidence)

    def test_negative_and_unknown_game_fixtures_are_not_wolf(self) -> None:
        negative = self.workspace / "negative"
        _write(negative / "SomeGame.exe", b"generic")
        _write(negative / "Content/data.bin", b"generic")
        unknown = self.workspace / "empty"
        unknown.mkdir()

        negative_result = WolfDetector().detect(negative)
        registry_result = create_engine_registry().identify(unknown)

        self.assertFalse(negative_result.detected)
        self.assertEqual(0, negative_result.confidence)
        self.assertFalse(registry_result.detected)
        self.assertIsNone(registry_result.adapter)

    def test_rpgmaker_detection_wins_when_wolf_evidence_is_only_weak(self) -> None:
        game = self.workspace / "rpgmaker"
        _write(game / "js/rmmz_core.js", b"// core")
        _write(game / "data/System.json", b"{}")
        _write(game / "Game.exe", b"generic named executable")

        selection = create_engine_registry().identify(game)

        self.assertTrue(selection.detected)
        self.assertEqual(EngineId.RPGMAKER_MZ, selection.detection.engine)
        self.assertEqual("rpgmaker", selection.adapter.adapter_id)

    def test_registry_contains_wolf_adapter(self) -> None:
        registry = create_engine_registry()

        adapter = registry.adapter_for(EngineId.WOLF_RPG_EDITOR)

        self.assertIsInstance(adapter, WolfRPGEngine)
        self.assertEqual(
            {"rpgmaker", "wolf"},
            {item.adapter_id for item in registry.engines},
        )

    def test_structure_report_serializes_portable_reconnaissance(self) -> None:
        game = _unpacked_wolf(self.workspace / "wolf")

        report = WolfRPGEngine().inspect_structure(game)
        payload = report.to_json_dict()
        serialized = json.dumps(payload, ensure_ascii=False)

        self.assertEqual("wolf_rpg_editor", payload["engine_id"])
        self.assertEqual("unpacked", payload["packaging_type"])
        self.assertEqual("not_detected", payload["encryption_status"])
        self.assertIsNone(payload["possible_version"])
        self.assertEqual("unknown", payload["version_confidence"])
        self.assertEqual(".", payload["root"])
        self.assertNotIn(str(self.workspace), serialized)
        sources = {item["file"]: item for item in payload["possible_text_sources"]}
        self.assertIn("Data/BasicData/Game.dat", sources)
        self.assertIn("Data/MapData/Map001.mps", sources)
        self.assertIn("Data/readme.txt", sources)
        self.assertEqual(
            ["Data/MapData/Map001.mps"],
            [item["file"] for item in payload["possible_map_files"]],
        )
        self.assertEqual(
            ["Data/BasicData/CommonEvent.dat"],
            [item["file"] for item in payload["possible_common_event_files"]],
        )
        self.assertEqual(
            ["Data/BasicData/UserDataBase.dat"],
            [item["file"] for item in payload["possible_database_files"]],
        )
        self.assertEqual(
            hashlib.sha256(b"WOLF-GAME-DATA").hexdigest(),
            sources["Data/BasicData/Game.dat"]["sha256"],
        )
        self.assertEqual(
            b"WOLF-GAME-DATA".hex(),
            sources["Data/BasicData/Game.dat"]["header_hex"],
        )
        self.assertIn("Data/Fonts/game.ttf", {
            item["file"] for item in payload["possible_font_files"]
        })
        self.assertIn("Data/BGM", payload["media_directories"])

    def test_packed_structure_is_probable_not_certain_encryption(self) -> None:
        game = _packed_wolf(self.workspace / "packed")

        report = WolfRPGEngine().inspect_structure(game)

        self.assertEqual("packed", report.packaging_type)
        self.assertEqual("probably_encrypted", report.encryption_status)
        self.assertEqual(["Data.wolf"], [item.file for item in report.archive_files])

    def test_inspection_is_read_only(self) -> None:
        game = _unpacked_wolf(self.workspace / "readonly")
        before = _tree_bytes(game)

        WolfRPGEngine().inspect_structure(game)

        self.assertEqual(before, _tree_bytes(game))

    def test_extract_and_apply_are_explicitly_unsupported(self) -> None:
        game = _unpacked_wolf(self.workspace / "unsupported")
        adapter = WolfRPGEngine()
        output_jsonl = self.workspace / "source.jsonl"
        output_game = self.workspace / "game-ko"

        with self.assertRaisesRegex(UnsupportedEngineOperationError, "text extraction"):
            adapter.extract(game, output_jsonl)
        with self.assertRaisesRegex(UnsupportedEngineOperationError, "translation apply"):
            adapter.apply(
                game,
                self.workspace / "translated.jsonl",
                output_game,
            )

        self.assertFalse(output_jsonl.exists())
        self.assertFalse(output_game.exists())

    def test_cli_inspect_smoke_and_json_report(self) -> None:
        game = _unpacked_wolf(self.workspace / "cli-wolf")
        report_file = self.workspace / "wolf_structure.json"
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = glt.main(
                ["inspect", str(game), "--json", str(report_file)]
            )
        payload = json.loads(report_file.read_text(encoding="utf-8"))

        self.assertEqual(0, exit_code)
        self.assertIn("Detected Engine: WOLF RPG Editor", output.getvalue())
        self.assertIn("Engine ID: wolf_rpg_editor", output.getvalue())
        self.assertIn("- Type: unpacked", output.getvalue())
        self.assertEqual("wolf_rpg_editor", payload["engine_id"])

    def test_detect_cli_recognizes_wolf(self) -> None:
        game = _unpacked_wolf(self.workspace / "detect-cli")
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = glt.main(["detect", str(game)])

        self.assertEqual(0, exit_code)
        self.assertIn("Detected Engine: WOLF RPG Editor", output.getvalue())
        self.assertIn("Data/BasicData/Game.dat", output.getvalue())

    def test_cli_inspect_refuses_report_inside_game(self) -> None:
        game = _unpacked_wolf(self.workspace / "protected")
        before = _tree_bytes(game)

        with contextlib.redirect_stdout(io.StringIO()):
            exit_code = glt.main(
                ["inspect", str(game), "--json", str(game / "report.json")]
            )

        self.assertEqual(2, exit_code)
        self.assertEqual(before, _tree_bytes(game))
        self.assertFalse((game / "report.json").exists())

    def test_cli_extract_for_wolf_fails_without_output(self) -> None:
        game = _unpacked_wolf(self.workspace / "no-extract")
        output_file = self.workspace / "source.jsonl"

        with contextlib.redirect_stdout(io.StringIO()):
            exit_code = glt.main(
                ["extract", str(game), "--output", str(output_file)]
            )

        self.assertEqual(2, exit_code)
        self.assertFalse(output_file.exists())


if __name__ == "__main__":
    unittest.main()
