from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import glt
from core.archive import ArchiveReport, write_archive_report
from engines.registry import create_engine_registry
from engines.wolf.archive import (
    MAX_ARCHIVE_SAMPLE_BYTES,
    WOLF_ARCHIVE_FORMATS,
    WolfArchiveProbe,
)
from engines.wolf.engine import WolfRPGEngine


def _write(path: Path, data: bytes = b"") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _sample_a(root: Path) -> Path:
    _write(root / "Game.exe", b"MZ synthetic Game")
    _write(root / "Config.exe", b"MZ synthetic Config")
    _write(root / "Data/BasicData.wolf", b"A" * 96)
    _write(root / "Data/MapData.wolf", b"B" * 96)
    _write(root / "Data/Script.wolf", b"C" * 96)
    _write(root / "Data/SystemFile.wolf", b"D" * 96)
    return root


def _sample_b(root: Path) -> Path:
    _write(root / "GamePro.exe", b"MZ synthetic GamePro")
    _write(root / "Config.exe", b"MZ synthetic Config")
    for name in (
        "BasicData.wolf",
        "MapData.wolf",
        "Game.wolf",
        "Text_Script.wolf",
        "mdb.wolf",
        "tdb.wolf",
    ):
        _write(root / "Data" / name, (name * 8).encode("ascii"))
    _write(root / "Data/font_1_honokamin.ttf.wolfx", b"font encrypted candidate")
    _write(root / "Data/kiloji_1.ttf.wolfx", b"other font candidate")
    return root


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


class WolfArchivePhaseTwoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary_directory.name)
        self.probe = WolfArchiveProbe()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_archive_format_catalog_separates_knowledge_levels(self) -> None:
        wolf = WOLF_ARCHIVE_FORMATS[".wolf"]
        wolfx = WOLF_ARCHIVE_FORMATS[".wolfx"]
        self.assertTrue(wolf.verified)
        self.assertTrue(wolf.probable)
        self.assertIn("binary header signature", wolf.unknown)
        self.assertNotEqual(wolf.format, wolfx.format)
        self.assertIsNone(wolf.header)

    def test_wolf_probe_is_portable_and_does_not_claim_entry_listing(self) -> None:
        game = _sample_a(self.workspace / "SampleA")
        report = self.probe.probe(game / "Data/BasicData.wolf")

        self.assertEqual("Data/BasicData.wolf", report.relative_path)
        self.assertEqual("wolf_archive", report.archive_type)
        self.assertEqual("probably_encrypted", report.encryption_status)
        self.assertEqual("game", report.executable_type)
        self.assertEqual("possible_basic_data", report.extra_metadata["archive_role"])
        self.assertFalse(report.entry_listing_supported)
        self.assertIsNone(report.entry_count)
        self.assertEqual((), report.entries)
        self.assertNotIn(str(game), json.dumps(report.to_json_dict()))

    def test_wolfx_probe_uses_separate_type_and_font_role(self) -> None:
        game = _sample_b(self.workspace / "SampleB")
        report = self.probe.probe(game / "Data/font_1_honokamin.ttf.wolfx")

        self.assertEqual("wolfx_individual_encrypted_file", report.archive_type)
        self.assertEqual("individual_encrypted_file", report.packaging)
        self.assertEqual("game_pro", report.executable_type)
        self.assertEqual("possible_font_asset", report.extra_metadata["archive_role"])
        self.assertEqual("low", report.extra_metadata["text_likelihood"])
        self.assertTrue(any("separately" in note for note in report.notes))

    def test_unknown_archive_probe_stays_unknown(self) -> None:
        candidate = self.workspace / "mystery.bin"
        _write(candidate, b"not a verified archive")
        report = self.probe.probe(candidate)

        self.assertEqual("unknown", report.archive_type)
        self.assertEqual("unknown", report.encryption_status)
        self.assertEqual("unknown", report.version_confidence)
        self.assertFalse(report.entry_listing_supported)

    def test_zero_byte_and_short_files_are_safe(self) -> None:
        zero = self.workspace / "zero.wolf"
        short = self.workspace / "short.wolfx"
        _write(zero)
        _write(short, b"abc")

        zero_report = self.probe.probe(zero)
        short_report = self.probe.probe(short)
        self.assertEqual("", zero_report.header_hex)
        self.assertEqual(0, zero_report.extra_metadata["sample_metrics"]["bytes_sampled"])
        self.assertEqual("616263", short_report.header_hex)
        self.assertEqual("616263", short_report.tail_hex)

    def test_large_archive_reads_only_the_documented_sample_budget(self) -> None:
        archive = self.workspace / "large.wolf"
        with archive.open("wb") as stream:
            stream.seek(64 * 1024 * 1024 - 1)
            stream.write(b"Z")

        report = self.probe.probe(archive)
        metrics = report.extra_metadata["sample_metrics"]
        self.assertEqual(64 * 1024 * 1024, report.size)
        self.assertLessEqual(metrics["bytes_sampled"], MAX_ARCHIVE_SAMPLE_BYTES)
        self.assertNotIn("sha256", report.to_json_dict())

    def test_probe_is_read_only(self) -> None:
        game = _sample_b(self.workspace / "readonly")
        before = _tree_bytes(game)
        self.probe.probe(game / "Data/Text_Script.wolf")
        self.assertEqual(before, _tree_bytes(game))

    def test_sample_a_and_b_detection_confidence_and_structure_roles(self) -> None:
        sample_a = _sample_a(self.workspace / "A")
        sample_b = _sample_b(self.workspace / "B")
        engine = WolfRPGEngine()

        self.assertEqual(82, engine.detect(sample_a).confidence)
        self.assertEqual(87, engine.detect(sample_b).confidence)
        report_a = engine.inspect_structure(sample_a)
        report_b = engine.inspect_structure(sample_b)
        roles_a = {
            item.file: item.metadata["probable_role"]
            for item in report_a.archive_files
        }
        roles_b = {
            item.file: item.metadata["probable_role"]
            for item in report_b.archive_files
        }
        self.assertEqual("possible_basic_data", roles_a["Data/BasicData.wolf"])
        self.assertEqual("possible_script_or_text_data", roles_a["Data/Script.wolf"])
        self.assertEqual("possible_map_data", roles_b["Data/MapData.wolf"])
        self.assertEqual(
            "possible_database_or_text_data", roles_b["Data/mdb.wolf"]
        )
        self.assertEqual("possible_font_asset", roles_b["Data/kiloji_1.ttf.wolfx"])

    def test_game_gamepro_and_renamed_executables_are_distinct(self) -> None:
        game = _sample_a(self.workspace / "game")
        pro = _sample_b(self.workspace / "pro")
        renamed = self.workspace / "renamed"
        _write(renamed / "MyTitle.exe", b"MZ renamed")
        _write(renamed / "Data/BasicData.wolf", b"archive")

        self.assertEqual(
            "game", self.probe.probe(game / "Data/BasicData.wolf").executable_type
        )
        self.assertEqual(
            "game_pro", self.probe.probe(pro / "Data/BasicData.wolf").executable_type
        )
        renamed_report = self.probe.probe(renamed / "Data/BasicData.wolf")
        self.assertEqual("renamed_candidate", renamed_report.executable_type)
        self.assertEqual("MyTitle.exe", renamed_report.executable_file)
        self.assertIsNone(renamed_report.version)

    def test_archive_report_serialization_and_atomic_no_overwrite(self) -> None:
        game = _sample_a(self.workspace / "report-game")
        report = self.probe.probe(game / "Data/MapData.wolf")
        output = self.workspace / "outside" / "archive_report.json"

        write_archive_report(output, report)
        loaded = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual("Data/MapData.wolf", loaded["relative_path"])
        self.assertFalse(loaded["entry_listing_supported"])
        with self.assertRaises(FileExistsError):
            write_archive_report(output, report)

    def test_registry_selects_wolf_archive_capability(self) -> None:
        registry = create_engine_registry()
        self.assertEqual("wolf", registry.adapter_for_archive(Path("Data.wolf")).adapter_id)
        self.assertEqual("wolf", registry.adapter_for_archive(Path("font.ttf.wolfx")).adapter_id)
        self.assertIsNone(registry.adapter_for_archive(Path("unknown.bin")))

    def test_cli_probe_and_external_json_report(self) -> None:
        game = _sample_a(self.workspace / "cli-game")
        output = self.workspace / "archive_report.json"
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = glt.main(
                [
                    "inspect-archive",
                    str(game / "Data/BasicData.wolf"),
                    "--json",
                    str(output),
                ]
            )
        self.assertEqual(0, code)
        self.assertTrue(output.is_file())
        self.assertIn("Can list entries: no", stdout.getvalue())

    def test_cli_refuses_report_inside_game(self) -> None:
        game = _sample_a(self.workspace / "protected")
        code = glt.main(
            [
                "inspect-archive",
                str(game / "Data/BasicData.wolf"),
                "--json",
                str(game / "report.json"),
            ]
        )
        self.assertEqual(2, code)
        self.assertFalse((game / "report.json").exists())

    def test_archive_report_rejects_absolute_persisted_paths(self) -> None:
        with self.assertRaises(ValueError):
            ArchiveReport(
                path=str((self.workspace / "absolute.wolf").resolve()),
                relative_path="Data/absolute.wolf",
                size=1,
                extension=".wolf",
                archive_type="wolf_archive",
                header_hex="00",
                tail_hex="00",
                packaging="archive_candidate",
                encryption_status="probably_encrypted",
                version=None,
                version_confidence="unknown",
                confidence=50,
            )


if __name__ == "__main__":
    unittest.main()
