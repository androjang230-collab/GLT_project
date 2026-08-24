from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import glt
from core.translation_io import write_jsonl
from engines.wolf.text_extractor import WolfTextExtractor
from engines.wolf.text_fingerprint import calculate_wolf_source_fingerprint
from engines.wolf.text_inspector import VERIFIED_DATABASE_TEXT_FIELDS, WolfTextInspector
from engines.wolf.text_qa import WolfTextQa
from engines.wolf.text_writer import WolfTextWriter


def _event(text: str, newline: str = "\n") -> str:
    return newline.join(
        [
            "[MAPDATA_TEXT_OUTPUT]",
            "[EVENTDATA_TEXT_OUTPUT]",
            "EVENT_ID=7",
            "EVENT_PAGE_NUM=1",
            "EVENT_PAGE=0",
            "COMMAND_NUM=2",
            "WoditorEvCOMMAND_START",
            f'[101][0,1]<0>()("{text}")',
            '[999][0,1]<0>()("unknown must stay")',
            "WoditorEvCOMMAND_END",
            "[COMMAND_TEXT_START]",
            "■文章：表示",
            "■未対応",
            "[COMMAND_TEXT_END]",
            "",
        ]
    )


def _database(name: str) -> str:
    return "\n".join(
        [
            "[DATABASE_TEXT_OUTPUT]",
            "TYPE_ID=2",
            "DATATYPE_2=2000",
            "<<--CSV_START-->>",
            "ID,Name,Description,Path",
            f'0,<<!--DATANAME--!>>{name},説明文,picture/a.png',
            "<<--CSV_END-->>",
            "",
        ]
    )


class WolfRoundTripTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "Data_AutoTXT"
        self.map_file = self.source / "MapData/Map001.mps.Auto.txt"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write(self, text: str, *, encoding: str = "utf-8", bom: bytes = b"") -> None:
        self.map_file.parent.mkdir(parents=True, exist_ok=True)
        self.map_file.write_bytes(bom + text.encode(encoding))

    def _jsonl(self, translations: dict[str, str] | None = None) -> Path:
        result = WolfTextExtractor().inspect_and_convert(self.source)
        path = self.root / "translated.jsonl"
        payloads = []
        for entry in result.entries:
            payload = entry.to_json_dict()
            payload["translation"] = (translations or {}).get(entry.id, "")
            payloads.append(payload)
        path.write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in payloads),
            encoding="utf-8",
        )
        return path

    def test_qa_pass_and_empty_translation_statistics(self) -> None:
        self._write(_event(r"原文\c[2]"))
        extracted = WolfTextExtractor().inspect_and_convert(self.source)
        translated = self._jsonl({extracted.entries[0].id: r"번역\c[2]"})
        result = WolfTextQa().validate(self.source, translated)
        self.assertEqual((1, 1, 0, 1), (result.report.total_entries, result.report.translated_entries, result.report.untranslated_entries, result.report.applicable_entries))
        self.assertEqual(0, result.report.error_count)
        empty = self._jsonl()
        empty_result = WolfTextQa().validate(self.source, empty)
        self.assertEqual(1, empty_result.report.untranslated_entries)
        self.assertTrue(any(item.issue_code == "EMPTY_TRANSLATION" for item in empty_result.report.issues))

    def test_control_variable_order_and_parameter_changes_are_rejected(self) -> None:
        self._write(_event(r"原文\c[2]\variable[4]"))
        entry = WolfTextExtractor().inspect_and_convert(self.source).entries[0]
        cases = (
            (r"번역\c[3]\variable[4]", "CONTROL_TOKEN_MISMATCH"),
            (r"번역\c[2]", "VARIABLE_REFERENCE_MISMATCH"),
            (r"번역\variable[4]\c[2]", "CONTROL_TOKEN_ORDER_MISMATCH"),
        )
        for index, (translation, code) in enumerate(cases):
            path = self._jsonl({entry.id: translation})
            result = WolfTextQa().validate(self.source, path)
            self.assertTrue(any(item.issue_code == code for item in result.report.issues), index)
            self.assertEqual(0, result.report.applicable_entries)

    def test_original_id_location_and_duplicate_preflight_blockers(self) -> None:
        self._write(_event("原文"))
        path = self._jsonl()
        row = json.loads(path.read_text(encoding="utf-8"))
        row["translation"] = "번역"
        row["original"] = "다른 원문"
        path.write_text(json.dumps(row, ensure_ascii=False) + "\n" + json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
        result = WolfTextQa().validate(self.source, path)
        codes = {item.issue_code for item in result.report.issues}
        self.assertIn("DUPLICATE_ID", codes)
        self.assertIn("SOURCE_TEXT_MISMATCH", codes)
        self.assertTrue(result.report.blocked)

    def test_malformed_jsonl_is_blocker(self) -> None:
        self._write(_event("原文"))
        path = self.root / "bad.jsonl"
        path.write_text("{bad\n", encoding="utf-8")
        result = WolfTextQa().validate(self.source, path)
        self.assertTrue(result.report.blocked)
        self.assertEqual("MALFORMED_JSONL", result.report.issues[-1].issue_code)

    def test_malformed_jsonl_writer_creates_no_output(self) -> None:
        self._write(_event("原文"))
        path = self.root / "bad.jsonl"
        path.write_text("{bad\n", encoding="utf-8")
        output = self.root / "output"
        report = WolfTextWriter().apply(self.source, path, output)
        self.assertTrue(report.blocked)
        self.assertFalse(output.exists())

    def test_physical_newline_translation_is_write_blocker(self) -> None:
        self._write(_event("原文"))
        entry = WolfTextExtractor().inspect_and_convert(self.source).entries[0]
        path = self._jsonl({entry.id: "첫 줄\n둘째 줄"})
        output = self.root / "output"
        report = WolfTextWriter().apply(self.source, path, output)
        self.assertTrue(report.blocked)
        self.assertTrue(any(item.issue_code == "UNSAFE_TRANSLATION_SYNTAX" for item in report.issues))
        self.assertFalse(output.exists())

    def test_source_fingerprint_is_portable_and_change_sensitive(self) -> None:
        self._write(_event("原文"))
        copy = self.root / "other_drive_simulation"
        import shutil
        shutil.copytree(self.source, copy)
        first = calculate_wolf_source_fingerprint(self.source)
        second = calculate_wolf_source_fingerprint(copy)
        self.assertEqual(first.value, second.value)
        self.assertFalse(any(Path(item.path).is_absolute() for item in first.files))
        (copy / "MapData/Map001.mps.Auto.txt").write_bytes(b"changed")
        self.assertNotEqual(first.value, calculate_wolf_source_fingerprint(copy).value)

    def test_source_change_since_extraction_blocks_apply(self) -> None:
        self._write(_event("原文"))
        entry = WolfTextExtractor().inspect_and_convert(self.source).entries[0]
        path = self._jsonl({entry.id: "번역"})
        (self.source / "unknown.bin").write_bytes(b"new")
        output = self.root / "output"
        report = WolfTextWriter().apply(self.source, path, output)
        self.assertGreater(report.blockers, 0)
        self.assertFalse(output.exists())

    def test_noop_round_trip_is_byte_identical(self) -> None:
        self._write(_event("　原文　", "\r\n"), encoding="utf-16-le", bom=b"\xff\xfe")
        (self.source / "opaque.bin").write_bytes(b"\x00\x01keep")
        before = {p.relative_to(self.source).as_posix(): p.read_bytes() for p in self.source.rglob("*") if p.is_file()}
        output = self.root / "output"
        report = WolfTextWriter().apply(self.source, self._jsonl(), output)
        after = {p.relative_to(output).as_posix(): p.read_bytes() for p in output.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(report.source_fingerprint, report.output_fingerprint)
        self.assertEqual(0, report.modified_files.__len__())

    def test_translation_changes_only_verified_field_and_preserves_transport(self) -> None:
        self._write(_event(r"  原文\n\c[2]　", "\r\n"), encoding="utf-16-le", bom=b"\xff\xfe")
        entry = WolfTextExtractor().inspect_and_convert(self.source).entries[0]
        path = self._jsonl({entry.id: r"  번역\n\c[2]　"})
        before = self.map_file.read_bytes()
        output = self.root / "output"
        report = WolfTextWriter().apply(self.source, path, output)
        data = (output / "MapData/Map001.mps.Auto.txt").read_bytes()
        self.assertTrue(data.startswith(b"\xff\xfe"))
        text = data[2:].decode("utf-16-le")
        self.assertIn(r"  번역\n\c[2]　", text)
        self.assertIn('("unknown must stay")', text)
        self.assertEqual("CRLF", WolfTextInspector().inspect(output).newline_style)
        self.assertEqual(1, report.applied_entries)
        self.assertEqual(before, self.map_file.read_bytes())

    def test_quote_backslash_tab_and_final_newline_preserved(self) -> None:
        self._write(_event(r"原文\path").rstrip("\n"))
        entry = WolfTextExtractor().inspect_and_convert(self.source).entries[0]
        path = self._jsonl({entry.id: '따옴표" 탭\t 경로\\path'})
        output = self.root / "output"
        WolfTextWriter().apply(self.source, path, output)
        data = (output / "MapData/Map001.mps.Auto.txt").read_text(encoding="utf-8")
        self.assertIn('따옴표\\" 탭\t 경로\\path', data)
        self.assertFalse(data.endswith("\n"))

    def test_database_allowlist_comma_and_nontext_exclusion(self) -> None:
        db = self.source / "BasicData/DataBase.Auto.txt"
        db.parent.mkdir(parents=True)
        db.write_bytes(b"\xef\xbb\xbf" + _database("薬").encode("utf-8"))
        self.assertEqual(frozenset({"dataname"}), VERIFIED_DATABASE_TEXT_FIELDS)
        result = WolfTextExtractor().inspect_and_convert(self.source)
        self.assertEqual(["薬"], [item.original for item in result.entries])
        self.assertFalse(any("picture/a.png" in item.original for item in result.entries))
        path = self.root / "translated.jsonl"
        row = result.entries[0].to_json_dict()
        row["translation"] = '약,"특수"'
        path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
        output = self.root / "output"
        report = WolfTextWriter().apply(self.source, path, output)
        self.assertEqual(1, report.applied_entries)
        text = (output / "BasicData/DataBase.Auto.txt").read_text(encoding="utf-8")
        self.assertIn('"<<!--DATANAME--!>>약<<COMMA>>""특수"""', text)
        self.assertIn("説明文,picture/a.png", text)

    def test_database_reorder_exposes_provisional_row_id_risk(self) -> None:
        db = self.source / "BasicData/DataBase.Auto.txt"
        db.parent.mkdir(parents=True)
        first_text = _database("A").replace(
            "0,<<!--DATANAME--!>>A,説明文,picture/a.png",
            "0,<<!--DATANAME--!>>A,説明文,picture/a.png\n1,<<!--DATANAME--!>>B,説明文,picture/b.png",
        )
        db.write_bytes(b"\xef\xbb\xbf" + first_text.encode("utf-8"))
        first = {item.original: item.id for item in WolfTextExtractor().inspect_and_convert(self.source).entries}
        reordered = first_text.replace(
            "0,<<!--DATANAME--!>>A,説明文,picture/a.png\n1,<<!--DATANAME--!>>B,説明文,picture/b.png",
            "1,<<!--DATANAME--!>>B,説明文,picture/b.png\n0,<<!--DATANAME--!>>A,説明文,picture/a.png",
        )
        db.write_bytes(b"\xef\xbb\xbf" + reordered.encode("utf-8"))
        second = {item.original: item.id for item in WolfTextExtractor().inspect_and_convert(self.source).entries}
        self.assertNotEqual(first["A"], second["A"])

    def test_system_title_field_round_trip(self) -> None:
        game = self.source / "BasicData/Game.dat.Auto.txt"
        game.parent.mkdir(parents=True)
        game.write_bytes(
            b"\xef\xbb\xbf"
            + "[GAMESETTING_TEXT_OUTPUT]\rGAME_TITLE_MAIN=  原題　\rPICTURE_FILE=keep.png".encode("utf-8")
        )
        entry = WolfTextExtractor().inspect_and_convert(self.source).entries[0]
        path = self._jsonl({entry.id: "  번역 제목　"})
        output = self.root / "output"
        report = WolfTextWriter().apply(self.source, path, output)
        self.assertEqual(1, report.applied_entries)
        data = (output / "BasicData/Game.dat.Auto.txt").read_bytes()
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        self.assertIn("GAME_TITLE_MAIN=  번역 제목　", data[3:].decode("utf-8"))
        self.assertIn("\rPICTURE_FILE=keep.png", data[3:].decode("utf-8"))

    def test_cp932_and_mixed_newlines_are_preserved(self) -> None:
        text = _event("原文", "\n").replace("\n", "\r\n", 4).replace("\n", "\r", 2)
        self._write(text, encoding="cp932")
        entry = WolfTextExtractor().inspect_and_convert(self.source).entries[0]
        path = self._jsonl({entry.id: "訳文"})
        output = self.root / "output"
        report = WolfTextWriter().apply(self.source, path, output)
        self.assertEqual(1, report.applied_entries)
        original_decoded = self.map_file.read_bytes().decode("cp932")
        output_decoded = (output / "MapData/Map001.mps.Auto.txt").read_bytes().decode("cp932")
        self.assertEqual(original_decoded.replace("原文", "訳文"), output_decoded)

    def test_file_type_and_location_mismatches_block(self) -> None:
        self._write(_event("原文"))
        path = self._jsonl()
        row = json.loads(path.read_text(encoding="utf-8"))
        row["translation"] = "번역"
        row["file"] = "MapData/Other.mps.Auto.txt"
        row["type"] = "choice"
        row["location"]["command_index"] = 9
        path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
        result = WolfTextQa().validate(self.source, path)
        codes = {item.issue_code for item in result.report.issues}
        self.assertTrue({"FILE_MISMATCH", "TYPE_MISMATCH", "CANONICAL_ID_MISMATCH"}.issubset(codes))
        self.assertEqual(0, result.report.applicable_entries)

    def test_ambiguous_encoding_blocks_output(self) -> None:
        text = _event("English only").replace("■文章：表示", "message").replace("■未対応", "unknown")
        self._write(text, encoding="ascii")
        entry = WolfTextExtractor().inspect_and_convert(self.source).entries[0]
        manual = self._jsonl({entry.id: "Korean text"})
        output = self.root / "output"
        report = WolfTextWriter().apply(self.source, manual, output)
        self.assertGreater(report.blockers, 0)
        self.assertFalse(output.exists())

    def test_dry_run_and_cli_never_create_output(self) -> None:
        self._write(_event("原文"))
        entry = WolfTextExtractor().inspect_and_convert(self.source).entries[0]
        path = self._jsonl({entry.id: "번역"})
        output = self.root / "output"
        report_path = self.root / "report.json"
        code = glt.main(["wolf-text-apply", str(path), "--source", str(self.source), "--output", str(output), "--dry-run", "--report", str(report_path)])
        self.assertEqual(0, code)
        self.assertFalse(output.exists())
        self.assertTrue(report_path.is_file())
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertTrue(report["dry_run"])
        self.assertEqual(1, report["applicable_entries"])

    def test_qa_cli_writes_portable_report(self) -> None:
        self._write(_event("原文"))
        entry = WolfTextExtractor().inspect_and_convert(self.source).entries[0]
        path = self._jsonl({entry.id: "번역"})
        report_path = self.root / "qa.json"
        code = glt.main(
            [
                "wolf-text-qa",
                str(path),
                "--source",
                str(self.source),
                "--report",
                str(report_path),
            ]
        )
        self.assertEqual(0, code)
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(1, payload["applicable_entries"])
        self.assertNotIn(str(self.source), report_path.read_text(encoding="utf-8"))

    def test_writer_output_is_deterministic(self) -> None:
        self._write(_event("原文"))
        entry = WolfTextExtractor().inspect_and_convert(self.source).entries[0]
        path = self._jsonl({entry.id: "번역"})
        first = self.root / "first"
        second = self.root / "second"
        first_report = WolfTextWriter().apply(self.source, path, first)
        second_report = WolfTextWriter().apply(self.source, path, second)
        self.assertEqual(first_report.output_fingerprint, second_report.output_fingerprint)
        self.assertEqual(
            {p.relative_to(first).as_posix(): p.read_bytes() for p in first.rglob("*") if p.is_file()},
            {p.relative_to(second).as_posix(): p.read_bytes() for p in second.rglob("*") if p.is_file()},
        )


if __name__ == "__main__":
    unittest.main()
