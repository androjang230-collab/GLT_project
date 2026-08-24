from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import glt
from core.models import EngineId, TranslationEntry
from core.translation_io import write_jsonl
from engines.rpgmaker.extractor import write_jsonl as legacy_write_jsonl
from engines.wolf.engine import WolfRPGEngine
from engines.wolf.text_extractor import (
    WolfTextExtractor,
    to_translation_entry,
    write_wolf_extraction_report,
)
from engines.wolf.text_inspector import WolfTextInspector, decode_auto_text
from engines.wolf.text_models import WolfRecordClassification


def _write(path: Path, text: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode(encoding))


def _event_export(
    *,
    domain: str = "map",
    container_id: int = 7,
    page_id: int = 1,
    commands: tuple[tuple[int, tuple[str, ...], str], ...] = (),
) -> str:
    if domain == "map":
        prefix = [
            "[MAPDATA_TEXT_OUTPUT]",
            "[EVENTDATA_TEXT_OUTPUT]",
            f"EVENT_ID={container_id}",
            "EVENT_PAGE_NUM=2",
            f"EVENT_PAGE={page_id}",
        ]
    else:
        prefix = ["[COMMON_EVENT_TEXT_OUTPUT]", f"COMMON_ID={container_id}"]
    raw = []
    labels = []
    for code, strings, label in commands:
        arguments = ",".join(f'"{value}"' for value in strings)
        raw.append(f"[{code}][0,{len(strings)}]<0>()({arguments})")
        labels.append(label)
    return "\n".join(
        [
            *prefix,
            f"COMMAND_NUM={len(raw)}",
            "WoditorEvCOMMAND_START",
            *raw,
            "WoditorEvCOMMAND_END",
            "[COMMAND_TEXT_START]",
            *labels,
            "[COMMAND_TEXT_END]",
            "",
        ]
    )


def _database_export(rows: tuple[str, ...]) -> str:
    return "\n".join(
        [
            "[DATABASE_TEXT_OUTPUT]",
            "TYPE_ID=4",
            "DATATYPE_1=2000",
            "DATATYPE_2=2000",
            "<<--CSV_START-->>",
            "ID,Name,Description",
            *rows,
            "<<--CSV_END-->>",
            "",
        ]
    )


def _tree(root: Path) -> dict[str, bytes]:
    return {
        item.relative_to(root).as_posix(): item.read_bytes()
        for item in root.rglob("*")
        if item.is_file()
    }


class WolfPhaseFourExtractionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary_directory.name)
        self.export = self.workspace / "Data_AutoTXT"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_verified_code_101_first_slot_is_extracted(self) -> None:
        text = _event_export(
            commands=((101, (r"문장\c[2]",), "■文章：表示"),)
        )
        _write(self.export / "MapData/Map001.mps.Auto.txt", text)

        result = WolfTextExtractor().inspect_and_convert(self.export)

        self.assertEqual(1, len(result.entries))
        entry = result.entries[0]
        self.assertEqual(EngineId.WOLF_RPG_EDITOR, entry.engine)
        self.assertEqual("dialogue", entry.type)
        self.assertEqual(r"문장\c[2]", entry.original)
        self.assertEqual((r"\c[2]",), entry.control_codes)
        self.assertEqual("101", entry.extra_metadata["command_code"])

    def test_code_101_extra_string_slots_remain_experimental(self) -> None:
        text = _event_export(
            commands=((101, ("사용 문장", "무시 슬롯"), "■文章：表示"),)
        )
        _write(self.export / "MapData/Map001.mps.Auto.txt", text)

        inspection = WolfTextInspector().inspect(self.export)
        result = WolfTextExtractor().inspect_and_convert(self.export)

        self.assertEqual(
            [
                WolfRecordClassification.VERIFIED_TRANSLATABLE,
                WolfRecordClassification.EXPERIMENTAL_TRANSLATABLE,
            ],
            [record.classification for record in inspection.records],
        )
        self.assertEqual(["사용 문장"], [entry.original for entry in result.entries])

    def test_label_only_message_with_unverified_code_is_excluded(self) -> None:
        text = _event_export(commands=((103, ("comment",), "■文章：見本"),))
        _write(self.export / "MapData/Map001.mps.Auto.txt", text)

        inspection = WolfTextInspector().inspect(self.export)
        result = WolfTextExtractor().inspect_and_convert(self.export)

        self.assertEqual(
            WolfRecordClassification.EXPERIMENTAL_TRANSLATABLE,
            inspection.records[0].classification,
        )
        self.assertEqual([], result.entries)
        self.assertEqual(1, result.report.experimental_excluded)

    def test_code_102_choice_is_verified_without_localized_label_dependency(self) -> None:
        text = _event_export(
            commands=((102, ("예", "아니요"), "■문장 선택지:/ 【1】예/ 【2】아니요"),)
        )
        _write(self.export / "MapData/Map001.mps.Auto.txt", text)

        inspection = WolfTextInspector().inspect(self.export)
        result = WolfTextExtractor().inspect_and_convert(self.export)

        self.assertTrue(
            all(
                record.classification
                == WolfRecordClassification.VERIFIED_TRANSLATABLE
                for record in inspection.records
            )
        )
        self.assertEqual(["예", "아니요"], [entry.original for entry in result.entries])
        self.assertEqual(
            [0, 1],
            [entry.extra_metadata["option_index"] for entry in result.entries],
        )
        self.assertEqual(2, result.report.output_entries)
        self.assertEqual(0, result.report.experimental_excluded)

    def test_dataname_is_extracted_but_markerless_description_is_not(self) -> None:
        _write(
            self.export / "BasicData/DataBase.Auto.txt",
            _database_export(("0,<<!--DATANAME--!>>薬日本語,説明文",)),
        )

        result = WolfTextExtractor().inspect_and_convert(self.export)
        inspection = WolfTextInspector().inspect(self.export)

        self.assertEqual(["薬日本語"], [entry.original for entry in result.entries])
        self.assertEqual("database_name", result.entries[0].type)
        self.assertEqual(4, result.entries[0].extra_metadata["database_type"])
        self.assertEqual(0, result.entries[0].extra_metadata["record_id"])
        description = next(
            record for record in inspection.records if record.type == "database_text"
        )
        self.assertEqual("説明文", description.original)
        self.assertEqual(
            WolfRecordClassification.EXPERIMENTAL_TRANSLATABLE,
            description.classification,
        )

    def test_database_record_ids_are_stable_and_structural(self) -> None:
        data = _database_export(
            (
                "0,<<!--DATANAME--!>>同じ日本語,first",
                "1,<<!--DATANAME--!>>同じ日本語,second",
            )
        )
        _write(self.export / "BasicData/DataBase.Auto.txt", data)

        first = WolfTextExtractor().inspect_and_convert(self.export)
        second = WolfTextExtractor().inspect_and_convert(self.export)

        self.assertEqual(
            [entry.id for entry in first.entries],
            [entry.id for entry in second.entries],
        )
        self.assertNotEqual(first.entries[0].id, first.entries[1].id)
        self.assertEqual([0, 1], [e.extra_metadata["record_id"] for e in first.entries])

    def test_system_fields_use_common_translation_entry(self) -> None:
        _write(
            self.export / "BasicData/Game.dat.Auto.txt",
            "[GAMESETTING_TEXT_OUTPUT]\nGAME_TITLE_MAIN=作品名\n",
        )
        result = WolfTextExtractor().inspect_and_convert(self.export)
        entry = result.entries[0]

        self.assertIsInstance(entry, TranslationEntry)
        self.assertEqual("system", entry.type)
        self.assertEqual("BASIC", entry.extra_metadata["target_type"])
        self.assertEqual("Data/BasicData/Game.dat", entry.extra_metadata["wolf_logical_source"])

    def test_empty_numeric_punctuation_and_control_only_are_excluded(self) -> None:
        commands = (
            (101, ("",), "■文章：表示"),
            (101, ("12345",), "■文章：表示"),
            (101, ("……",), "■文章：表示"),
            (101, (r"\c[2]\variable[4]",), "■文章：表示"),
            (101, ("실제 문장",), "■文章：表示"),
        )
        _write(
            self.export / "BasicData/CommonEvent.dat.Auto.txt",
            _event_export(domain="common", commands=commands),
        )

        result = WolfTextExtractor().inspect_and_convert(self.export)

        self.assertEqual(["실제 문장"], [entry.original for entry in result.entries])
        self.assertEqual(4, result.report.excluded_empty_or_nontext)

    def test_common_repeated_text_has_distinct_command_locations(self) -> None:
        commands = (
            (101, ("同じ",), "■文章：表示"),
            (101, ("同じ",), "■文章：表示"),
        )
        _write(
            self.export / "BasicData/CommonEvent.dat.Auto.txt",
            _event_export(domain="common", container_id=9, commands=commands),
        )
        result = WolfTextExtractor().inspect_and_convert(self.export)

        self.assertEqual(2, len(result.entries))
        self.assertNotEqual(result.entries[0].id, result.entries[1].id)
        self.assertEqual([0, 1], [entry.command_index for entry in result.entries])

    def test_map_repeated_text_has_distinct_page_locations(self) -> None:
        for page in (0, 1):
            _write(
                self.export / f"MapData/Page{page}.mps.Auto.txt",
                _event_export(
                    container_id=2,
                    page_id=page,
                    commands=((101, ("はい",), "■文章：表示"),),
                ),
            )
        result = WolfTextExtractor().inspect_and_convert(self.export)
        self.assertEqual(2, len(result.entries))
        self.assertNotEqual(result.entries[0].id, result.entries[1].id)

    def test_common_jsonl_field_order_and_metadata_are_compatible(self) -> None:
        _write(
            self.export / "MapData/Map001.mps.Auto.txt",
            _event_export(commands=((101, ("文章",), "■文章：表示"),)),
        )
        result = WolfTextExtractor().inspect_and_convert(self.export)
        payload = result.entries[0].to_json_dict()

        self.assertEqual(
            ["id", "engine", "file", "type", "original", "translation"],
            list(payload)[:6],
        )
        self.assertEqual("wolf_rpg_editor", payload["engine"])
        self.assertIn("location", payload)
        self.assertEqual("provisional", payload["location"]["schema_status"])

    def test_converter_rejects_experimental_record(self) -> None:
        _write(
            self.export / "MapData/Map001.mps.Auto.txt",
            _event_export(commands=((103, ("label only",), "■文章：見本"),)),
        )
        record = WolfTextInspector().inspect(self.export).records[0]
        with self.assertRaisesRegex(ValueError, "only verified"):
            to_translation_entry(record)

    def test_deterministic_jsonl_is_byte_identical(self) -> None:
        _write(
            self.export / "BasicData/CommonEvent.dat.Auto.txt",
            _event_export(
                domain="common",
                commands=(
                    (101, ("B",), "■文章：表示"),
                    (101, ("A",), "■文章：表示"),
                ),
            ),
        )
        first = WolfTextExtractor().inspect_and_convert(self.export)
        second = WolfTextExtractor().inspect_and_convert(self.export)
        one = self.workspace / "one.jsonl"
        two = self.workspace / "two.jsonl"
        write_jsonl(first.entries, one, overwrite=False)
        write_jsonl(second.entries, two, overwrite=False)
        self.assertEqual(one.read_bytes(), two.read_bytes())

    def test_rpgmaker_legacy_writer_is_the_same_common_serializer(self) -> None:
        entry = TranslationEntry(
            id="Map001:event1:page1:cmd401:index0:param0",
            engine=EngineId.RPGMAKER_MZ,
            file="data/Map001.json",
            type="dialogue",
            original="原文",
        )
        one = self.workspace / "common.jsonl"
        two = self.workspace / "legacy.jsonl"
        write_jsonl([entry], one)
        legacy_write_jsonl([entry], two)
        self.assertEqual(one.read_bytes(), two.read_bytes())

    def test_extraction_report_contains_required_counts_and_is_portable(self) -> None:
        _write(
            self.export / "MapData/Map001.mps.Auto.txt",
            _event_export(
                commands=(
                    (101, ("verified",), "■文章：表示"),
                    (102, ("choice",), "■選択肢：表示"),
                    (999, ("unknown",), "■未知：保持"),
                )
            ),
        )
        result = WolfTextExtractor().inspect_and_convert(self.export)
        path = self.workspace / "extract_report.json"
        write_wolf_extraction_report(path, result.report)
        payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(1, payload["files_scanned"])
        self.assertEqual(2, payload["verified_translatable"])
        self.assertEqual(0, payload["experimental_excluded"])
        self.assertEqual(1, payload["unknown_records"])
        self.assertEqual(2, payload["output_entries"])
        self.assertNotIn(str(self.export), path.read_text(encoding="utf-8"))
        with self.assertRaises(FileExistsError):
            write_wolf_extraction_report(path, result.report)

    def test_collision_blocks_output_without_suffix_or_overwrite(self) -> None:
        block = _event_export(
            domain="common", commands=((101, ("반복",), "■文章：表示"),)
        )
        duplicated = block.replace("[COMMAND_TEXT_END]\n", "[COMMAND_TEXT_END]\n" + "\n".join(block.splitlines()[2:]) + "\n")
        _write(self.export / "BasicData/CommonEvent.dat.Auto.txt", duplicated)
        output = self.workspace / "blocked.jsonl"

        result = WolfRPGEngine().extract_text_export(self.export, output)

        self.assertTrue(result.report.blocked)
        self.assertGreater(result.report.canonical_id_collisions, 0)
        self.assertFalse(output.exists())

    def test_parser_warning_is_preserved_in_extraction_report(self) -> None:
        text = _event_export(commands=((101, ("text",), "■文章：表示"),)).replace(
            "COMMAND_NUM=1", "COMMAND_NUM=9"
        )
        _write(self.export / "MapData/Map001.mps.Auto.txt", text)
        result = WolfTextExtractor().inspect_and_convert(self.export)
        self.assertGreater(result.report.parser_warnings, 0)
        self.assertTrue(
            any(issue["code"] == "COMMAND_COUNT_MISMATCH" for issue in result.report.issues)
        )

    def test_encoding_ambiguity_is_explicit_and_not_replacement_decoded(self) -> None:
        decoded = decode_auto_text(b"\xc2\xa9")
        self.assertEqual("unknown", decoded.encoding)
        self.assertEqual("ambiguous", decoded.encoding_confidence)
        self.assertIsNone(decoded.text)
        self.assertIn("multiple strict decoders", " ".join(decoded.encoding_evidence))

    def test_source_export_is_byte_identical_after_extraction(self) -> None:
        _write(
            self.export / "MapData/Map001.mps.Auto.txt",
            _event_export(commands=((101, ("text",), "■文章：表示"),)),
        )
        before = _tree(self.export)
        WolfRPGEngine().extract_text_export(self.export, self.workspace / "out.jsonl")
        self.assertEqual(before, _tree(self.export))

    def test_output_inside_export_and_existing_output_are_refused(self) -> None:
        _write(
            self.export / "MapData/Map001.mps.Auto.txt",
            _event_export(commands=((101, ("text",), "■文章：表示"),)),
        )
        with self.assertRaisesRegex(ValueError, "cannot be inside"):
            WolfRPGEngine().extract_text_export(
                self.export, self.export / "source.jsonl"
            )
        existing = self.workspace / "existing.jsonl"
        existing.write_text("keep", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            WolfRPGEngine().extract_text_export(self.export, existing)
        self.assertEqual("keep", existing.read_text(encoding="utf-8"))

    def test_cli_smoke_writes_jsonl_and_report(self) -> None:
        _write(
            self.export / "MapData/Map001.mps.Auto.txt",
            _event_export(commands=((101, ("text",), "■文章：表示"),)),
        )
        output = self.workspace / "source.jsonl"
        report = self.workspace / "report.json"
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = glt.main(
                [
                    "wolf-text-extract",
                    str(self.export),
                    "--output",
                    str(output),
                    "--report",
                    str(report),
                ]
            )
        self.assertEqual(0, code)
        self.assertTrue(output.is_file())
        self.assertTrue(report.is_file())
        self.assertIn("Output entries: 1", stdout.getvalue())

    def test_cli_refuses_outputs_inside_export(self) -> None:
        self.export.mkdir(parents=True)
        output = self.export / "source.jsonl"
        code = glt.main(
            [
                "wolf-text-extract",
                str(self.export),
                "--output",
                str(output),
            ]
        )
        self.assertEqual(2, code)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
