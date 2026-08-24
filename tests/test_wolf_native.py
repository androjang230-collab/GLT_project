from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import glt
from engines.wolf.native.binary import BoundedBinaryReader, NativeFormatError
from engines.wolf.native.models import (
    EvidenceGrade,
    NativeDocument,
    NativeLocation,
    NativeRecord,
    NativeTextField,
    WolfNativeResearchReport,
    write_native_research_report,
)
from engines.wolf.native.probe import WolfNativeProbe, correlate_known_strings


GAME_MAGIC = b"\x00W\x00\x00OL\x00FM"
TABLE_PREFIX = b"\x00W\x00\x00OL"
MAP_MAGIC = b"\x00" * 10 + b"WOLFM\x00"


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _fixture(root: Path) -> Path:
    _write(root / "Data/BasicData/Game.dat", GAME_MAGIC + b"\x55game")
    _write(
        root / "Data/BasicData/CommonEvent.dat",
        TABLE_PREFIX + b"\x55FC\x00common",
    )
    _write(
        root / "Data/BasicData/CDataBase.dat",
        TABLE_PREFIX + b"\x55FM\x00database",
    )
    _write(root / "Data/MapData/Map001.mps", MAP_MAGIC + b"\x55map")
    _write(root / "Data/BasicData/AutoBackup1/Game.dat", b"backup")
    return root


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


class BoundedBinaryReaderTests(unittest.TestCase):
    def test_reads_little_endian_and_length_prefixed_nul_bytes(self) -> None:
        reader = BoundedBinaryReader(b"AB" + b"\x04\x00\x00\x00abc\x00")
        reader.expect(b"AB")
        self.assertEqual(b"abc", reader.read_length_prefixed_bytes(require_nul=True))
        self.assertEqual(0, reader.remaining)

    def test_truncated_input_fails_closed(self) -> None:
        reader = BoundedBinaryReader(b"\x04\x00\x00\x00ab")
        with self.assertRaisesRegex(NativeFormatError, "truncated input"):
            reader.read_length_prefixed_bytes()

    def test_untrusted_length_cannot_trigger_large_allocation(self) -> None:
        reader = BoundedBinaryReader(
            b"\xff\xff\xff\x7f", max_allocation=32
        )
        with self.assertRaisesRegex(NativeFormatError, "allocation limit"):
            reader.read_length_prefixed_bytes()

    def test_missing_nul_is_rejected(self) -> None:
        reader = BoundedBinaryReader(b"\x03\x00\x00\x00abc")
        with self.assertRaisesRegex(NativeFormatError, "NUL terminator"):
            reader.read_length_prefixed_bytes(require_nul=True)


class NativeLogicalModelTests(unittest.TestCase):
    def test_offsets_are_evidence_not_logical_identity(self) -> None:
        location = NativeLocation(
            source="Data/MapData/Map001.mps",
            domain="map",
            record_kind="event",
            record_id=2,
            command_index=4,
            byte_offset_evidence=(100, 200),
        )
        self.assertNotIn("byte_offset", location.logical_components())
        self.assertEqual([100, 200], location.to_json_dict()["byte_offset_evidence"])

    def test_absolute_native_source_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "portable relative"):
            NativeLocation(source=str(Path.cwd().resolve()), domain="map")

    def test_document_model_is_not_a_translation_entry(self) -> None:
        field = NativeTextField(
            location=NativeLocation(
                source="Data/BasicData/Game.dat", domain="game", field="title"
            ),
            source_text_sha256="0" * 64,
            source_text_length=3,
            evidence_grade=EvidenceGrade.C,
        )
        document = NativeDocument(
            source="Data/BasicData/Game.dat",
            format_family="game_dat",
            records=(NativeRecord(kind="settings", fields=(field,)),),
        )
        payload = document.to_json_dict()
        self.assertNotIn("translation", json.dumps(payload))
        self.assertEqual("signature_only", payload["parse_scope"])


class WolfNativeProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_probe_classifies_supported_native_signatures(self) -> None:
        game = _fixture(self.workspace / "game")
        report = WolfNativeProbe().inspect(game)
        files = {item.source: item for item in report.files}

        self.assertEqual(4, len(files))
        self.assertEqual("matched", files["Data/BasicData/Game.dat"].signature_status)
        self.assertEqual(
            "common_event_dat", files["Data/BasicData/CommonEvent.dat"].role
        )
        self.assertEqual("database_dat", files["Data/BasicData/CDataBase.dat"].role)
        self.assertEqual("map_mps", files["Data/MapData/Map001.mps"].role)
        self.assertTrue(all(item.evidence_grade is EvidenceGrade.B for item in files.values()))

    def test_probe_is_read_only_and_excludes_editor_backups(self) -> None:
        game = _fixture(self.workspace / "game")
        before = _tree_hashes(game)
        report = WolfNativeProbe().inspect(game)

        self.assertEqual(before, _tree_hashes(game))
        self.assertFalse(any("AutoBackup" in item.source for item in report.files))

    def test_malformed_signature_is_reported_without_parsing(self) -> None:
        game = self.workspace / "game"
        _write(game / "Data/BasicData/Game.dat", b"not-wolf")
        report = WolfNativeProbe().inspect(game)
        self.assertEqual("not_matched", report.files[0].signature_status)
        self.assertEqual("signature_only", report.documents[0].parse_scope)

    def test_file_size_limit_fails_closed_per_file(self) -> None:
        game = self.workspace / "game"
        _write(game / "Data/BasicData/Game.dat", b"1234")
        with mock.patch("engines.wolf.native.probe.MAX_NATIVE_FILE_BYTES", 3):
            report = WolfNativeProbe().inspect(game)
        self.assertEqual((), report.files)
        self.assertIn("exceeds read-only probe limit", report.issues[0])

    def test_known_string_correlation_is_hash_only_and_encoding_aware(self) -> None:
        text = "テスト"
        data = b"head" + text.encode("utf-8") + b"mid" + text.encode("utf-16le")
        results = correlate_known_strings(data, [text])
        text_hash, length, matches = results[0]

        self.assertEqual(hashlib.sha256(text.encode("utf-8")).hexdigest(), text_hash)
        self.assertEqual(len(text), length)
        self.assertIn("utf-8", matches)
        self.assertIn("utf-16le", matches)
        self.assertNotIn(text, json.dumps(results, ensure_ascii=False))

    def test_report_is_portable_and_atomic_no_overwrite(self) -> None:
        game = _fixture(self.workspace / "game")
        report = WolfNativeProbe().inspect(game)
        output = self.workspace / "outside" / "native.json"
        write_native_research_report(output, report)
        serialized = output.read_text(encoding="utf-8")

        self.assertNotIn(str(self.workspace), serialized)
        self.assertFalse(json.loads(serialized)["privacy"]["absolute_paths_persisted"])
        with self.assertRaises(FileExistsError):
            write_native_research_report(output, report)

    def test_cli_creates_external_report_without_modifying_game(self) -> None:
        game = _fixture(self.workspace / "game")
        output = self.workspace / "reports" / "native.json"
        before = _tree_hashes(game)
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            exit_code = glt.main(
                ["wolf-native-probe", str(game), "--report", str(output)]
            )

        self.assertEqual(0, exit_code)
        self.assertEqual(before, _tree_hashes(game))
        self.assertTrue(output.is_file())
        self.assertIn("Writes to game/oracle: none", stdout.getvalue())

    def test_cli_rejects_report_inside_game(self) -> None:
        game = _fixture(self.workspace / "game")
        output = game / "native.json"
        before = _tree_hashes(game)
        with contextlib.redirect_stdout(io.StringIO()):
            exit_code = glt.main(
                ["wolf-native-probe", str(game), "--report", str(output)]
            )
        self.assertEqual(2, exit_code)
        self.assertFalse(output.exists())
        self.assertEqual(before, _tree_hashes(game))


if __name__ == "__main__":
    unittest.main()
