from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

import glt
from core.models import EngineId
from engines.registry import create_engine_registry
from engines.wolf.text_inspector import (
    WolfTextInspector,
    decode_auto_text,
    detect_control_like_tokens,
)
from engines.wolf.text_extractor import WolfTextExtractor
from engines.wolf.text_writer import WolfTextWriter
from engines.wolf.text_models import (
    WolfLocation,
    WolfTextReport,
    write_wolf_text_report,
)


def _write_text(
    path: Path,
    text: str,
    *,
    encoding: str = "utf-8",
    bom: bytes = b"",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bom + text.encode(encoding))


def _game_text(newline: str = "\n", final: bool = True) -> str:
    lines = [
        "[GAMESETTING_TEXT_OUTPUT]",
        "GAME_TITLE_MAIN=  自作ゲーム  ",
        "GAME_TITLE_PLUS=副題",
        "START_TITLE_BAR_TEXT=開始",
        "PLAYING_TITLE_BAR_TEXT=プレイ中",
        "PICTURE_FILE=not_text.png",
    ]
    return newline.join(lines) + (newline if final else "")


def _map_text() -> str:
    return "\n".join(
        [
            "[MAPDATA_TEXT_OUTPUT]",
            "[EVENTDATA_TEXT_OUTPUT]",
            "EVENT_ID=7",
            "EVENT_PAGE_NUM=2",
            "EVENT_PAGE=1",
            "COMMAND_NUM=3",
            "WoditorEvCOMMAND_START",
            r'[101][0,1]<0>()("  「台詞」\n\c[2]次\"行  ")',
            r'[102][0,2]<0>()("はい","いいえ")',
            r'[999][0,1]<0>()("unknown")',
            "WoditorEvCOMMAND_END",
            "[COMMAND_TEXT_START]",
            "■文章:表示",
            "■選択肢：表示",
            "■未知：保持",
            "[COMMAND_TEXT_END]",
            "",
        ]
    )


def _common_text() -> str:
    return "\n".join(
        [
            "[COMMON_EVENT_TEXT_OUTPUT]",
            "COMMON_ID=12",
            "COMMAND_NUM=1",
            "WoditorEvCOMMAND_START",
            r'[101][0,1]<0>()("同じ")',
            "WoditorEvCOMMAND_END",
            "[COMMAND_TEXT_START]",
            "■文章:表示",
            "[COMMAND_TEXT_END]",
            "",
        ]
    )


def _database_text() -> str:
    return "\n".join(
        [
            "[DATABASE_TEXT_OUTPUT]",
            "TYPE_ID=2",
            "<<--CSV_START-->>",
            "ID,Name,Value",
            "0,<<!--DATANAME--!>>薬<<COMMA>>小,17",
            "1,<<!--DATANAME--!>>空欄,",
            "<<--CSV_END-->>",
            "",
        ]
    )


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


class WolfTextPhaseThreeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary_directory.name)
        self.inspector = WolfTextInspector()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_basic_fixture_preserves_original_whitespace(self) -> None:
        root = self.workspace / "basic"
        _write_text(root / "BasicData/Game.dat.Auto.txt", _game_text())

        report = self.inspector.inspect(root, fixture_kind="synthetic")

        self.assertEqual("BASIC", report.target_type)
        self.assertEqual(4, report.record_count)
        self.assertEqual("  自作ゲーム  ", report.records[0].original)
        self.assertEqual("system", report.records[0].type)
        self.assertFalse(any("not_text.png" in item.original for item in report.records))

    def test_map_dialogue_choice_unknown_and_lossless_literals(self) -> None:
        root = self.workspace / "map"
        _write_text(root / "MapData/Map001.mps.Auto.txt", _map_text())

        report = self.inspector.inspect(root, fixture_kind="synthetic")

        self.assertEqual("MAP", report.target_type)
        self.assertEqual(1, report.count_type("dialogue"))
        self.assertEqual(2, report.count_type("choice"))
        dialogue = next(item for item in report.records if item.type == "dialogue")
        expected = r'  「台詞」\n\c[2]次\"行  '
        self.assertEqual(expected, dialogue.original)
        self.assertEqual(expected.replace(r"\n", "\n"), dialogue.normalized_view)
        self.assertEqual((r"\c[2]",), dialogue.control_codes)
        self.assertEqual(1, len(report.unknown_records))

    def test_common_event_and_database_names(self) -> None:
        root = self.workspace / "all"
        _write_text(root / "BasicData/CommonEvent.dat.Auto.txt", _common_text())
        _write_text(root / "BasicData/DataBase.Auto.txt", _database_text())
        _write_text(root / "MapData/Map001.mps.Auto.txt", _map_text())

        report = self.inspector.inspect(root, fixture_kind="synthetic")

        self.assertEqual("ALL", report.target_type)
        common = [item for item in report.records if item.location.domain == "common"]
        database = [item for item in report.records if item.type == "database_name"]
        self.assertEqual(1, len(common))
        self.assertEqual(2, len(database))
        self.assertEqual("薬<<COMMA>>小", database[0].original)
        self.assertEqual("薬,小", database[0].normalized_view)

    def test_encoding_bom_and_newline_detection(self) -> None:
        cases = (
            ("utf-8", b"\xef\xbb\xbf", "utf-8", "utf-8"),
            ("cp932", b"", "cp932", "none"),
            ("utf-16-le", b"\xff\xfe", "utf-16-le", "utf-16-le"),
            ("utf-16-be", b"\xfe\xff", "utf-16-be", "utf-16-be"),
        )
        for codec, bom, expected_encoding, expected_bom in cases:
            with self.subTest(codec=codec):
                decoded = decode_auto_text(bom + "日本語\r\n".encode(codec))
                self.assertEqual(expected_encoding, decoded.encoding)
                self.assertEqual(expected_bom, decoded.bom)
                self.assertEqual("CRLF", decoded.newline_style)
                self.assertTrue(decoded.final_newline)

    def test_ascii_empty_unknown_and_lf_without_final_newline(self) -> None:
        ascii_result = decode_auto_text(b"ASCII\n")
        empty_result = decode_auto_text(b"")
        bad_result = decode_auto_text(b"\x81")
        no_final = decode_auto_text("日本語\n末尾".encode("utf-8"))

        self.assertEqual("ascii", ascii_result.encoding)
        self.assertEqual("ascii", empty_result.encoding)
        self.assertIsNone(bad_result.text)
        self.assertEqual("unknown", bad_result.encoding)
        self.assertEqual("LF", no_final.newline_style)
        self.assertFalse(no_final.final_newline)

    def test_per_file_transport_metadata_and_mixed_aggregate(self) -> None:
        root = self.workspace / "mixed"
        _write_text(root / "BasicData/Game.dat.Auto.txt", _game_text("\r\n"))
        _write_text(
            root / "MapData/Map001.mps.Auto.txt",
            _map_text(),
            encoding="utf-16-le",
            bom=b"\xff\xfe",
        )

        report = self.inspector.inspect(root)

        self.assertEqual("mixed", report.detected_encoding)
        self.assertEqual("mixed", report.bom)
        self.assertEqual("mixed", report.newline_style)
        self.assertTrue(all(item.final_newline for item in report.files))

    def test_empty_unknown_and_malformed_files_are_reported(self) -> None:
        root = self.workspace / "bad"
        _write_text(root / "Empty.Auto.txt", "")
        _write_text(root / "Unknown.Auto.txt", "[OTHER]\nvalue\n")
        _write_text(
            root / "MapData/Broken.mps.Auto.txt",
            "[MAPDATA_TEXT_OUTPUT]\nEVENT_ID=1\nEVENT_PAGE_NUM=1\n"
            "EVENT_PAGE=0\nWoditorEvCOMMAND_START\n",
        )
        (root / "Bad.Auto.txt").write_bytes(b"\x81")

        report = self.inspector.inspect(root)
        codes = {item.code for item in report.issues}

        self.assertIn("EMPTY_TEXT_EXPORT", codes)
        self.assertIn("UNKNOWN_TEXT_EXPORT_FORMAT", codes)
        self.assertIn("MALFORMED_COMMAND_BLOCK", codes)
        self.assertIn("TEXT_DECODE_FAILED", codes)

    def test_control_like_sequences_are_observed_without_rpgmaker_semantics(self) -> None:
        self.assertEqual(
            (r"\c[2]", r"\variable[4]", "<<COMMA>>"),
            detect_control_like_tokens(r"line\n\c[2]\variable[4]<<COMMA>>"),
        )

    def test_location_id_is_stable_portable_and_text_independent(self) -> None:
        first = WolfLocation(
            domain="map",
            source="MapData/Map 001.mps.Auto.txt",
            container_kind="event",
            container_id=7,
            page_id=1,
            command_index=3,
            text_index=0,
        )
        second = WolfLocation(**{
            "domain": "map",
            "source": "MapData/Map 001.mps.Auto.txt",
            "container_kind": "event",
            "container_id": 7,
            "page_id": 1,
            "command_index": 3,
            "text_index": 0,
        })
        self.assertEqual(first, second)
        self.assertEqual(first.canonical_id, second.canonical_id)
        self.assertTrue(first.canonical_id.startswith("wolf:v1:map:"))
        self.assertIn("Map%20001", first.canonical_id)
        self.assertNotIn(str(self.workspace), first.canonical_id)

    def test_location_component_encoding_is_collision_resistant(self) -> None:
        colon = WolfLocation(
            domain="basic",
            source="BasicData/Game.dat.Auto.txt",
            container_kind="container",
            container_id="a:b",
            field="x=y",
        )
        percent_text = WolfLocation(
            domain="basic",
            source="BasicData/Game.dat.Auto.txt",
            container_kind="container",
            container_id="a%3Ab",
            field="x%3Dy",
        )
        self.assertNotEqual(colon.canonical_id, percent_text.canonical_id)
        self.assertIn("a%3Ab", colon.canonical_id)
        self.assertIn("x%3Dy", colon.canonical_id)

    def test_empty_literal_tab_and_backslash_are_not_normalized(self) -> None:
        root = self.workspace / "literal"
        text = "\n".join(
            [
                "[COMMON_EVENT_TEXT_OUTPUT]",
                "COMMON_ID=1",
                "COMMAND_NUM=1",
                "WoditorEvCOMMAND_START",
                '[101][0,2]<0>()(""," \\t\\path ")',
                "WoditorEvCOMMAND_END",
                "[COMMAND_TEXT_START]",
                "■文章:表示",
                "[COMMAND_TEXT_END]",
                "",
            ]
        )
        _write_text(root / "BasicData/CommonEvent.dat.Auto.txt", text)

        report = self.inspector.inspect(root)

        self.assertEqual(["", r" \t\path "], [item.original for item in report.records])
        self.assertEqual([0, 1], [item.location.text_index for item in report.records])

    def test_duplicate_structural_location_is_reported_as_collision(self) -> None:
        root = self.workspace / "collision"
        block = "\n".join(
            [
                "COMMAND_NUM=1",
                "WoditorEvCOMMAND_START",
                '[101][0,1]<0>()("반복")',
                "WoditorEvCOMMAND_END",
                "[COMMAND_TEXT_START]",
                "■文章:表示",
                "[COMMAND_TEXT_END]",
            ]
        )
        text = "\n".join(
            [
                "[COMMON_EVENT_TEXT_OUTPUT]",
                "COMMON_ID=1",
                block,
                block,
                "",
            ]
        )
        _write_text(root / "BasicData/CommonEvent.dat.Auto.txt", text)

        report = self.inspector.inspect(root)

        self.assertEqual(2, report.record_count)
        self.assertEqual(report.records[0].id, report.records[1].id)
        self.assertTrue(
            any(item.code == "WOLF_CANONICAL_ID_COLLISION" for item in report.issues)
        )

    def test_repeated_original_at_different_locations_has_different_ids(self) -> None:
        root = self.workspace / "repeat"
        _write_text(root / "BasicData/CommonEvent.dat.Auto.txt", _common_text())
        other = _common_text().replace("COMMON_ID=12", "COMMON_ID=13")
        _write_text(root / "BasicData/CommonEvent2.dat.Auto.txt", other)

        report = self.inspector.inspect(root)
        repeated = [item for item in report.records if item.original == "同じ"]

        self.assertEqual(2, len(repeated))
        self.assertNotEqual(repeated[0].id, repeated[1].id)

    def test_parser_order_and_ids_are_deterministic(self) -> None:
        root = self.workspace / "stable"
        _write_text(root / "BasicData/Z.Game.dat.Auto.txt", _game_text())
        _write_text(root / "BasicData/a.CommonEvent.dat.Auto.txt", _common_text())

        first = self.inspector.inspect(root).to_json_dict()
        second = self.inspector.inspect(root).to_json_dict()

        self.assertEqual(first, second)
        self.assertEqual(
            [item["id"] for item in first["records"]],
            [item["id"] for item in second["records"]],
        )

    def test_report_serialization_is_portable_atomic_and_no_overwrite(self) -> None:
        root = self.workspace / "source"
        _write_text(root / "BasicData/Game.dat.Auto.txt", _game_text())
        report = self.inspector.inspect(root, fixture_kind="synthetic")
        output = self.workspace / "reports" / "wolf_text_report.json"

        write_wolf_text_report(output, report)
        loaded = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(".", loaded["source_path"])
        self.assertEqual("synthetic", loaded["fixture_kind"])
        self.assertEqual("provisional", loaded["location_schema_status"])
        self.assertNotIn(str(root), output.read_text(encoding="utf-8"))
        with self.assertRaises(FileExistsError):
            write_wolf_text_report(output, report)

    def test_inspection_is_read_only(self) -> None:
        root = self.workspace / "readonly"
        _write_text(root / "BasicData/Game.dat.Auto.txt", _game_text("\r\n"))
        _write_text(root / "MapData/Map001.mps.Auto.txt", _map_text())
        before = _tree_bytes(root)

        self.inspector.inspect(root)

        self.assertEqual(before, _tree_bytes(root))

    def test_registry_exposes_wolf_text_capability(self) -> None:
        adapter = create_engine_registry().adapter_for(EngineId.WOLF_RPG_EDITOR)
        self.assertIsNotNone(adapter)
        root = self.workspace / "adapter"
        _write_text(root / "BasicData/Game.dat.Auto.txt", _game_text())
        report = adapter.inspect_text_export(root)
        self.assertIsInstance(report, WolfTextReport)

    def test_cli_smoke_and_external_json_report(self) -> None:
        root = self.workspace / "cli"
        _write_text(root / "BasicData/Game.dat.Auto.txt", _game_text())
        output = self.workspace / "wolf_text_report.json"
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            code = glt.main(
                ["wolf-text-inspect", str(root), "--json", str(output)]
            )

        self.assertEqual(0, code)
        self.assertTrue(output.is_file())
        self.assertIn("WOLF Text Export Inspection", stdout.getvalue())
        self.assertIn("v1 (provisional)", stdout.getvalue())

    def test_cli_refuses_report_inside_export_and_does_not_write(self) -> None:
        root = self.workspace / "protected"
        _write_text(root / "BasicData/Game.dat.Auto.txt", _game_text())
        output = root / "report.json"

        code = glt.main(
            ["wolf-text-inspect", str(root), "--json", str(output)]
        )

        self.assertEqual(2, code)
        self.assertFalse(output.exists())

    def test_cli_returns_three_when_parser_has_errors(self) -> None:
        root = self.workspace / "errors"
        (root / "BasicData").mkdir(parents=True)
        (root / "BasicData/Bad.Auto.txt").write_bytes(b"\x81")
        self.assertEqual(3, glt.main(["wolf-text-inspect", str(root)]))


@unittest.skipUnless(
    os.environ.get("GLT_WOLF_AUTOTXT_FIXTURE"),
    "set GLT_WOLF_AUTOTXT_FIXTURE to an external official export directory",
)
class OptionalOfficialWolfTextFixtureTests(unittest.TestCase):
    def test_external_official_export_is_read_only_and_deterministic(self) -> None:
        root = Path(os.environ["GLT_WOLF_AUTOTXT_FIXTURE"]).resolve()
        before = _tree_bytes(root)
        inspector = WolfTextInspector()

        first = inspector.inspect(root, fixture_kind="official-export-derived")
        second = inspector.inspect(root, fixture_kind="official-export-derived")
        extracted_first = WolfTextExtractor().inspect_and_convert(root)
        extracted_second = WolfTextExtractor().inspect_and_convert(root)

        self.assertEqual(first.to_json_dict(), second.to_json_dict())
        self.assertEqual(
            [entry.to_json_dict() for entry in extracted_first.entries],
            [entry.to_json_dict() for entry in extracted_second.entries],
        )
        self.assertEqual(
            extracted_first.report.to_json_dict(),
            extracted_second.report.to_json_dict(),
        )
        self.assertEqual(before, _tree_bytes(root))

    def test_external_official_export_noop_round_trip_is_byte_identical(self) -> None:
        root = Path(os.environ["GLT_WOLF_AUTOTXT_FIXTURE"]).resolve()
        extracted = WolfTextExtractor().inspect_and_convert(root)
        if extracted.report.blocked:
            self.skipTest("external fixture has extraction blockers")
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            jsonl = workspace / "source.jsonl"
            jsonl.write_text(
                "".join(
                    json.dumps(entry.to_json_dict(), ensure_ascii=False) + "\n"
                    for entry in extracted.entries
                ),
                encoding="utf-8",
            )
            output = workspace / "roundtrip"
            before = _tree_bytes(root)
            report = WolfTextWriter().apply(root, jsonl, output)
            self.assertFalse(report.blocked)
            self.assertEqual(before, _tree_bytes(output))
            self.assertEqual(before, _tree_bytes(root))


if __name__ == "__main__":
    unittest.main()
