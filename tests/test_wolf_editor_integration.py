from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import glt
from engines.wolf.editor_integration import (
    SubprocessWolfEditorInvoker,
    WolfEditorIntegrationValidator,
    WolfEditorInvocation,
    WolfEditorLocator,
    write_wolf_editor_report,
)
from engines.wolf.text_fingerprint import calculate_wolf_source_fingerprint


def _event_export() -> str:
    return "\r\n".join(
        [
            "[MAPDATA_TEXT_OUTPUT]",
            "[EVENTDATA_TEXT_OUTPUT]",
            "EVENT_ID=1",
            "EVENT_PAGE_NUM=1",
            "EVENT_PAGE=0",
            "COMMAND_NUM=2",
            "WoditorEvCOMMAND_START",
            r'[101][0,1]<0>()("日本語\c[2]\variable[4]")',
            r'[102][0,2]<0>()("はい","いいえ")',
            "WoditorEvCOMMAND_END",
            "[COMMAND_TEXT_START]",
            "■文章：表示",
            "■選択肢：表示",
            "[COMMAND_TEXT_END]",
            "",
        ]
    )


def _database_export() -> str:
    return "\r\n".join(
        [
            "[DATABASE_TEXT_OUTPUT]",
            "TYPE_ID=2",
            "DATATYPE_2=2000",
            "<<--CSV_START-->>",
            "ID,Name,Description,Path",
            "0,<<!--DATANAME--!>>薬,説明文,picture/item.png",
            "<<--CSV_END-->>",
            "",
        ]
    )


def _game_export() -> str:
    return "\r\n".join(
        [
            "[GAMESETTING_TEXT_OUTPUT]",
            "GAME_TITLE_MAIN=試験作品",
            "PICTURE_FILE=keep.png",
            "",
        ]
    )


def _write_fixture(root: Path) -> None:
    files = {
        "MapData/Map001.mps.Auto.txt": _event_export(),
        "BasicData/DataBase.Auto.txt": _database_export(),
        "BasicData/Game.dat.Auto.txt": _game_export(),
    }
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("cp932"))


class SyntheticEditorInvoker:
    provenance = "synthetic"

    def __init__(self, *, fail_mode: str | None = None) -> None:
        self.fail_mode = fail_mode
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def invoke(
        self,
        editor: Path,
        working_directory: Path,
        *,
        mode: str,
        text_folder: str,
        target: str,
        timeout_seconds: int,
    ) -> WolfEditorInvocation:
        command = (editor.name, f"-{mode}", "-txt_folder", text_folder, "-target", target)
        self.calls.append((mode, command))
        if mode == self.fail_mode:
            return WolfEditorInvocation(
                mode, target, command, ".", 9, False, 1, 0, 0,
                hashlib.sha256(b"").hexdigest(),
                hashlib.sha256(b"").hexdigest(),
                False, False, "synthetic failure",
            )
        destination = working_directory / text_folder
        if mode == "txtoutput":
            imported = working_directory / ".synthetic_native"
            if imported.is_dir():
                shutil.copytree(imported, destination)
            else:
                destination.mkdir(parents=True)
                _write_fixture(destination)
        else:
            imported = working_directory / ".synthetic_native"
            shutil.copytree(destination, imported)
        return WolfEditorInvocation(
            mode, target, command, ".", 0, False, 1, 0, 0,
            hashlib.sha256(b"").hexdigest(),
            hashlib.sha256(b"").hexdigest(),
            destination.is_dir(), True, "completed",
        )


class WolfEditorIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "Data").mkdir()
        self.editor = self.project / "Editor.exe"
        self.editor.write_bytes(b"MZ" + b"synthetic-editor")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_locator_requires_pe_evidence_not_filename_alone(self) -> None:
        self.editor.write_bytes(b"not a PE")
        detection = WolfEditorLocator().check(self.editor, project=self.project)
        self.assertFalse(detection.detected)
        self.assertTrue(any(item.code == "EDITOR_PE_SIGNATURE_MISSING" for item in detection.issues))

    def test_locator_accepts_known_pe_candidate_without_guessing_version(self) -> None:
        detection = WolfEditorLocator().check(self.editor, project=self.project)
        self.assertTrue(detection.detected)
        self.assertEqual("Editor.exe", detection.editor_file)
        self.assertIsNone(detection.editor_version)
        self.assertNotIn(str(self.editor), json.dumps(detection.to_json_dict()))

    def test_renamed_candidate_needs_adjacent_wolf_evidence(self) -> None:
        renamed = self.root / "renamed.exe"
        renamed.write_bytes(b"MZfake")
        self.assertFalse(WolfEditorLocator().check(renamed).detected)
        self.assertTrue(WolfEditorLocator().check(renamed, project=self.project).detected)

    def test_subprocess_wrapper_uses_argument_list_shell_false_and_no_wait(self) -> None:
        output = self.project / "Export"
        output.mkdir()
        completed = subprocess.CompletedProcess([], 0, b"out", b"")
        with mock.patch("subprocess.run", return_value=completed) as run:
            result = SubprocessWolfEditorInvoker().invoke(
                self.editor,
                self.project,
                mode="txtoutput",
                text_folder="Export",
                target="ALL",
                timeout_seconds=10,
            )
        args, kwargs = run.call_args
        self.assertEqual(str(self.editor), args[0][0])
        self.assertEqual(["-txtoutput", "-txt_folder", "Export", "-target", "ALL"], args[0][1:])
        self.assertFalse(kwargs["shell"])
        self.assertNotIn("capture_output", kwargs)
        self.assertTrue(hasattr(kwargs["stdout"], "read"))
        self.assertTrue(hasattr(kwargs["stderr"], "read"))
        self.assertNotIn("-wait", args[0])
        self.assertTrue(result.success)

    def test_subprocess_timeout_is_reported_without_retry(self) -> None:
        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(["Editor.exe"], 1, b"partial", b"error"),
        ) as run:
            result = SubprocessWolfEditorInvoker().invoke(
                self.editor,
                self.project,
                mode="txtoutput",
                text_folder="Export",
                target="BASIC",
                timeout_seconds=1,
            )
        self.assertTrue(result.timed_out)
        self.assertFalse(result.success)
        self.assertEqual(1, run.call_count)

    def test_subprocess_zero_exit_without_output_is_failure(self) -> None:
        completed = subprocess.CompletedProcess([], 0, b"", b"")
        with mock.patch("subprocess.run", return_value=completed):
            result = SubprocessWolfEditorInvoker().invoke(
                self.editor,
                self.project,
                mode="txtoutput",
                text_folder="MissingExport",
                target="MAP",
                timeout_seconds=10,
            )
        self.assertFalse(result.success)
        self.assertFalse(result.output_exists)
        self.assertIn("not created", result.reason)

    def test_inspect_mode_exports_and_plans_but_never_imports(self) -> None:
        before = calculate_wolf_source_fingerprint(self.project).value
        invoker = SyntheticEditorInvoker()
        result = WolfEditorIntegrationValidator(invoker).validate(
            self.project,
            editor=self.editor,
            allow_editor_import=False,
        )
        self.assertTrue(result.report.txtoutput_success)
        self.assertIsNone(result.report.txtinput_success)
        self.assertEqual(["txtoutput"], [item[0] for item in invoker.calls])
        self.assertEqual("NOT VERIFIED", result.report.official_verification)
        self.assertEqual(before, calculate_wolf_source_fingerprint(self.project).value)
        self.assertFalse(result.workspace_preserved)

    def test_opt_in_isolated_roundtrip_records_cp932_utf8_and_comma(self) -> None:
        before = calculate_wolf_source_fingerprint(self.project).value
        invoker = SyntheticEditorInvoker()
        workspace = self.root / "integration"
        result = WolfEditorIntegrationValidator(invoker).validate(
            self.project,
            editor=self.editor,
            allow_editor_import=True,
            workspace=workspace,
            keep_workspace=True,
        )
        report = result.report
        self.assertTrue(report.txtinput_success)
        self.assertTrue(report.reexport_success)
        self.assertTrue(report.editor_noop_roundtrip["comparison"]["semantic_equal"])
        self.assertTrue(report.glt_noop_roundtrip["comparison"]["semantic_equal"])
        trials = {item.name: item for item in report.encoding_trials}
        self.assertEqual("rejected", trials["source_encoding"].status)
        self.assertIn(trials["utf8_bom"].status, {"accepted", "normalized"})
        self.assertEqual("rejected", trials["utf8_no_bom"].status)
        self.assertIn("TEXT_ENCODING_AMBIGUOUS", trials["utf8_no_bom"].reason)
        self.assertLessEqual(trials["utf8_bom"].translated_entry_count, 3)
        self.assertIn("choice", trials["utf8_bom"].translated_entry_types)
        self.assertTrue(trials["utf8_bom"].control_codes_preserved)
        self.assertFalse(trials["utf8_bom"].mojibake_found)
        trial_rows = [
            json.loads(line)
            for line in (workspace / "utf8_bom.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        translated_rows = [row for row in trial_rows if row["translation"].strip()]
        self.assertLessEqual(len(translated_rows), 3)
        self.assertTrue(
            any(
                "GLT 0.7.6 한국어 왕복 테스트입니다." in row["translation"]
                for row in translated_rows
            )
        )
        self.assertEqual("NOT VERIFIED", report.korean_roundtrip)
        self.assertEqual("NOT VERIFIED", report.comma_roundtrip)
        self.assertTrue(trials["utf8_bom"].comma_preserved)
        self.assertEqual("NOT VERIFIED", report.official_verification)
        self.assertEqual("NOT VERIFIED", report.choice_validation["status"])
        self.assertEqual(["dataname"], report.database_validation["verified_database_text_fields"])
        self.assertEqual(before, calculate_wolf_source_fingerprint(self.project).value)
        self.assertTrue(result.workspace_preserved)

    def test_failed_txtoutput_preserves_forensic_workspace_and_source(self) -> None:
        before = calculate_wolf_source_fingerprint(self.project).value
        workspace = self.root / "failed"
        result = WolfEditorIntegrationValidator(
            SyntheticEditorInvoker(fail_mode="txtoutput")
        ).validate(
            self.project,
            editor=self.editor,
            workspace=workspace,
        )
        self.assertGreater(result.report.blockers, 0)
        self.assertTrue(result.workspace_preserved)
        self.assertTrue(workspace.is_dir())
        self.assertEqual(before, calculate_wolf_source_fingerprint(self.project).value)

    def test_report_is_portable_atomic_and_no_overwrite(self) -> None:
        result = WolfEditorIntegrationValidator(SyntheticEditorInvoker()).validate(
            self.project,
            editor=self.editor,
            workspace=self.root / "portable_workspace",
            keep_workspace=True,
        )
        report = self.root / "integration.json"
        write_wolf_editor_report(report, result.report)
        text = report.read_text(encoding="utf-8")
        self.assertNotIn(str(self.root), text)
        self.assertEqual("synthetic", json.loads(text)["fixture_kind"])
        with self.assertRaises(FileExistsError):
            write_wolf_editor_report(report, result.report)

    def test_workspace_inside_original_project_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot be inside"):
            WolfEditorIntegrationValidator(SyntheticEditorInvoker()).validate(
                self.project,
                editor=self.editor,
                workspace=self.project / "integration",
            )

    def test_cli_missing_editor_reports_not_verified_without_execution(self) -> None:
        report = self.root / "check.json"
        code = glt.main(
            [
                "wolf-editor-check",
                str(self.root / "MissingEditor.exe"),
                "--report",
                str(report),
            ]
        )
        self.assertEqual(3, code)
        self.assertFalse(json.loads(report.read_text(encoding="utf-8"))["editor_detected"])

    def test_validate_cli_without_editor_is_not_verified_and_read_only(self) -> None:
        self.editor.unlink()
        before = calculate_wolf_source_fingerprint(self.project).value
        report = self.root / "validate.json"
        code = glt.main(
            [
                "wolf-editor-validate",
                str(self.project),
                "--report",
                str(report),
            ]
        )
        self.assertEqual(3, code)
        payload = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual("NOT VERIFIED", payload["official_verification"])
        self.assertFalse(payload["editor_detected"])
        self.assertEqual(before, calculate_wolf_source_fingerprint(self.project).value)


if __name__ == "__main__":
    unittest.main()
