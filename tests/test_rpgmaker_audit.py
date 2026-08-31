from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import glt
from core.models import EngineId
from engines.rpgmaker import audit as audit_module
from engines.rpgmaker.audit import (
    RpgMakerCoverageAuditor,
    write_candidate_csv,
    write_coverage_report,
)


def _write_json(game: Path, name: str, value: object) -> None:
    data = game / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / name).write_text(
        json.dumps(value, ensure_ascii=False), encoding="utf-8"
    )


def _game(root: Path, engine: EngineId = EngineId.RPGMAKER_MZ) -> Path:
    game = root / "game"
    (game / "js/plugins").mkdir(parents=True)
    core = "rmmz_core.js" if engine == EngineId.RPGMAKER_MZ else "rpg_core.js"
    (game / "js" / core).write_text("// synthetic", encoding="utf-8")
    _write_json(game, "System.json", {})
    return game


def _map(commands: list[dict[str, object]]) -> dict[str, object]:
    return {
        "displayName": "",
        "events": [
            None,
            {"id": 7, "name": "internal", "pages": [{"list": commands}]},
        ],
    }


def _command(code: int, parameters: object, indent: int = 0) -> dict[str, object]:
    return {"code": code, "indent": indent, "parameters": parameters}


class RpgMakerCoverageAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _audit(
        self,
        commands: list[dict[str, object]],
        engine: EngineId = EngineId.RPGMAKER_MZ,
    ):
        game = _game(self.workspace, engine)
        _write_json(game, "Map001.json", _map(commands))
        return RpgMakerCoverageAuditor(engine).audit(game)

    def test_catalog_contains_every_stock_event_code(self) -> None:
        report = self._audit([_command(code, []) for code in audit_module._EVENT_NAMES])
        rows = {row["code"]: row for row in report.event_commands}
        self.assertEqual(set(audit_module._EVENT_NAMES), set(rows))
        self.assertTrue(all(rows[code]["occurrences"] == 1 for code in rows))

    def test_unknown_code_and_string_are_reported_for_review(self) -> None:
        report = self._audit([_command(999, ["未知の表示候補"])])
        candidate = report.candidates[0]
        self.assertEqual("UNKNOWN", candidate.classification)
        self.assertNotIn("未知", json.dumps(report.to_json_dict(), ensure_ascii=False))

    def test_malformed_command_and_parameters_continue(self) -> None:
        report = self._audit([
            {"code": "bad", "parameters": []},
            _command(401, "not-a-list"),
            _command(401, ["正常"]),
        ])
        self.assertEqual(2, len(report.issues))
        self.assertEqual(1, report.statistics["coverage"]["currently_extracted"])

    def test_string_and_non_string_parameters_are_distinguished(self) -> None:
        report = self._audit([
            _command(401, [1]),
            _command(401, ["表示"]),
            _command(401, [""]),
            _command(401, ["   "]),
        ])
        self.assertEqual(1, len(report.candidates))

    def test_map_common_event_and_troop_lists_are_observed(self) -> None:
        game = _game(self.workspace)
        _write_json(game, "Map001.json", _map([_command(401, ["map"])]))
        _write_json(game, "CommonEvents.json", [None, {"id": 2, "name": "x", "list": [_command(401, ["common"])]}])
        _write_json(game, "Troops.json", [None, {"id": 3, "name": "x", "pages": [{"list": [_command(405, ["troop"])]}]}])
        report = RpgMakerCoverageAuditor(EngineId.RPGMAKER_MZ).audit(game)
        self.assertEqual({"event:7:page:1", "common_event:2", "troop:3:page:1"}, {item.event_context for item in report.candidates})

    def test_101_401_102_405_are_current_verified_entries(self) -> None:
        report = self._audit([
            _command(101, ["Face", 0, 0, 2, "話者"]),
            _command(401, ["台詞"]),
            _command(102, [["一", "二"], -1, 0, 2, 0]),
            _command(405, ["長文"]),
        ])
        self.assertEqual(5, report.statistics["coverage"]["currently_extracted"])
        self.assertEqual({"speaker", "dialogue", "choice", "scroll_text"}, {item.role for item in report.candidates})

    def test_320_324_325_are_now_current_verified_entries(self) -> None:
        report = self._audit([
            _command(320, [1, "新しい名前"]),
            _command(324, [1, "新しい二つ名"]),
            _command(325, [1, "新しいプロフィール"]),
        ])
        coverage = report.statistics["coverage"]
        self.assertEqual(0, coverage["known_missed_verified_candidates"])
        self.assertEqual(3, coverage["currently_extracted"])

    def test_comments_and_labels_are_internal_even_with_japanese(self) -> None:
        report = self._audit([
            _command(108, ["日本語コメント"]), _command(408, ["続き"]),
            _command(118, ["開始位置"]), _command(119, ["開始位置"]),
        ])
        self.assertEqual([], report.candidates)
        rows = {row["code"]: row for row in report.event_commands}
        self.assertTrue(all(rows[code]["internal_strings"] == 1 for code in (108, 408, 118, 119)))

    def test_control_variable_script_operand_is_unsafe(self) -> None:
        report = self._audit([_command(122, [1, 1, 0, 4, "'internal'"])])
        self.assertEqual("UNSAFE", report.candidates[0].classification)
        self.assertEqual("script_operand", report.candidates[0].role)

    def test_move_route_inventory_separates_assets_and_script(self) -> None:
        route = {"repeat": False, "skippable": True, "wait": False, "list": [
            {"code": 41, "parameters": ["Actor1", 0]},
            {"code": 44, "parameters": [{"name": "Bell", "volume": 90}]},
            {"code": 45, "parameters": ["this.stepCount += 1"]},
        ]}
        report = self._audit([_command(205, [0, route])])
        rows = {row["code"]: row for row in report.move_route_commands}
        self.assertEqual("asset_reference", rows[41]["role"])
        self.assertEqual("asset_reference", rows[44]["role"])
        self.assertEqual("UNSAFE", rows[45]["classification"])
        self.assertEqual(["move_route_script"], [item.role for item in report.candidates])

    def test_script_block_display_api_is_conditional(self) -> None:
        report = self._audit([
            _command(355, ["$gameMessage.add("]),
            _command(655, ["'synthetic text');"]),
            _command(355, ["$gameVariables.setValue(1, 'key')"]),
        ])
        self.assertEqual(["CONDITIONAL_TRANSLATABLE", "CONDITIONAL_TRANSLATABLE", "UNSAFE"], [item.classification for item in report.candidates])

    def test_mv_356_preserves_payload_and_control_code_metadata(self) -> None:
        report = self._audit([_command(356, [r"SHOW_LOG \C[2]synthetic message"])], EngineId.RPGMAKER_MV)
        item = report.candidates[0]
        self.assertEqual("SHOW_LOG", item.command_name)
        self.assertEqual((r"\C[2]",), item.control_codes)
        self.assertEqual("CONDITIONAL_TRANSLATABLE", item.classification)

    def test_mv_source_discovery_metadata_is_linked_to_observed_prefix(self) -> None:
        game = _game(self.workspace, EngineId.RPGMAKER_MV)
        _write_json(game, "Map001.json", _map([_command(356, ["Notice old words"])]))
        (game / "js/plugins.js").write_text(
            'var $plugins = [{"name":"NoticePlugin","status":true,"description":"","parameters":{}}];',
            encoding="utf-8",
        )
        (game / "js/plugins/NoticePlugin.js").write_text("""
Game_Interpreter.prototype.pluginCommand = function(command, args) {
 if (command === 'Notice') { $gameMessage.add(args.join(' ')); }
};
""", encoding="utf-8")
        report = RpgMakerCoverageAuditor(EngineId.RPGMAKER_MV).audit(game)
        row = next(item for item in report.statistics["plugin_command_coverage"]["mv_356"]["prefixes"] if item["prefix"] == "Notice")
        self.assertEqual("APPLY_VERIFIED", row["discovery_classification"])
        self.assertEqual("joined_remainder", row["argument_mode"])
        self.assertEqual("$gameMessage.add", row["sink"])
        self.assertTrue(report.plugin_discovery["source_unchanged"])

    def test_mv_map_dispatch_updates_semantic_audit_classification(self) -> None:
        game = _game(self.workspace, EngineId.RPGMAKER_MV)
        _write_json(game, "Map001.json", _map([
            _command(356, ["QX_SOUND sound_asset"]),
            _command(356, ["QX_NOTICE visible words"]),
        ]))
        (game / "js/plugins.js").write_text(
            'var $plugins = [{"name":"MapRoutes","status":true,"description":"","parameters":{}}];',
            encoding="utf-8",
        )
        (game / "js/plugins/MapRoutes.js").write_text("""
const prefix = 'QX_';
const routes = new Map();
function registerRoute(name, method) { routes.set(prefix + name, method); }
registerRoute('SOUND', 'playSound');
registerRoute('NOTICE', 'showNotice');
Game_Interpreter.prototype.pluginCommand = function(command, args) {
 const target = routes.get(command.toUpperCase());
 if (target) { this[target](args); }
};
Game_Interpreter.prototype.playSound = function(values) {
 AudioManager.playSe({name: values[0]});
};
Game_Interpreter.prototype.showNotice = function(values) {
 $gameMessage.add(values.join(' '));
};
""", encoding="utf-8")
        report = RpgMakerCoverageAuditor(EngineId.RPGMAKER_MV).audit(game)
        rows = {
            item["prefix"]: item
            for item in report.statistics["plugin_command_coverage"]["mv_356"]["prefixes"]
        }
        self.assertEqual("INTERNAL", rows["QX_SOUND"]["classification"])
        self.assertEqual("INTERNAL", rows["QX_SOUND"]["discovery_classification"])
        self.assertEqual("AudioManager", rows["QX_SOUND"]["sink"])
        self.assertEqual("VERIFIED_TRANSLATABLE", rows["QX_NOTICE"]["classification"])
        self.assertEqual("APPLY_VERIFIED", rows["QX_NOTICE"]["discovery_classification"])
        self.assertEqual("joined_remainder", rows["QX_NOTICE"]["argument_mode"])
        self.assertEqual(1, report.statistics["plugin_command_coverage"]["mv_356"]["verified"])
        self.assertEqual(1, report.statistics["plugin_command_coverage"]["mv_356"]["internal"])

    def test_mz_357_recursively_observes_nested_arguments(self) -> None:
        report = self._audit([_command(357, ["LogPlugin", "addLog", "Add Log", {"text": "表示文", "nested": {"lines": ["一", "二"]}, "count": 3}])])
        self.assertEqual(3, len(report.candidates))
        self.assertEqual({".text", ".nested.lines[0]", ".nested.lines[1]"}, {item.argument_path for item in report.candidates})
        self.assertTrue(all(item.plugin_name == "LogPlugin" for item in report.candidates))

    def test_657_is_a_mirror_of_preceding_357_across_annotations(self) -> None:
        report = self._audit([
            _command(357, ["P", "C", "C", {"text": "x"}]),
            _command(657, ["text = x"]), _command(657, ["other = y"]),
        ])
        self.assertEqual([0, 0], [item.source_command_index for item in report.mirrors])
        self.assertTrue(all(item.classification == "MIRROR" for item in report.candidates if item.command_code == 657))

    def test_choice_mirror_detects_match_and_mismatch(self) -> None:
        report = self._audit([
            _command(102, [["A", "B"], -1, 0, 2, 0]),
            _command(402, [0, "A"]), _command(402, [1, "old B"]),
        ])
        self.assertEqual([True, False], [item.values_match for item in report.mirrors])
        self.assertEqual(1, report.statistics["mirror_mismatches"])

    def test_nested_choice_mirror_uses_indent_identity(self) -> None:
        report = self._audit([
            _command(102, [["outer"], -1], 0), _command(402, [0, "outer"], 0),
            _command(102, [["inner"], -1], 1), _command(402, [0, "inner"], 1),
            _command(402, [0, "outer"], 0),
        ])
        self.assertEqual([0, 2, 0], [item.source_command_index for item in report.mirrors])
        self.assertTrue(all(item.values_match for item in report.mirrors))

    def test_choice_cancel_duplicate_control_and_empty_values_do_not_break_audit(self) -> None:
        report = self._audit([
            _command(102, [[r"\C[2]same", r"\C[2]same", ""], -2, 1, 2, 0]),
            _command(402, [0, r"\C[2]same"]), _command(402, [1, r"\C[2]same"]),
            _command(403, []),
        ])
        choices = [item for item in report.candidates if item.role == "choice"]
        self.assertEqual(2, len(choices))
        self.assertTrue(all(item.control_codes == (r"\C[2]",) for item in choices))

    def test_picture_and_audio_filenames_are_not_candidates(self) -> None:
        report = self._audit([
            _command(231, [1, "PictureName", 0, 0, 0, 0, 100, 100, 255, 0]),
            _command(241, [{"name": "MusicName", "volume": 90, "pitch": 100, "pan": 0}]),
        ])
        self.assertEqual([], report.candidates)

    def test_database_inventory_marks_classes_name_current(self) -> None:
        game = _game(self.workspace)
        _write_json(game, "Classes.json", [None, {"id": 1, "name": "ClassName", "note": ""}])
        report = RpgMakerCoverageAuditor(EngineId.RPGMAKER_MZ).audit(game)
        row = next(item for item in report.database_fields if item.file == "Classes.json" and item.json_path == "[*].name")
        self.assertEqual(1, row.occurrences)
        self.assertTrue(row.current_extract)
        self.assertEqual(0, report.statistics["database_coverage"]["known_missed_verified_candidates"])

    def test_system_visible_and_internal_arrays_are_separated(self) -> None:
        game = _game(self.workspace)
        _write_json(game, "System.json", {"gameTitle": "Title", "elements": ["", "Fire"], "terms": {"basic": ["Level"]}, "switches": ["", "Internal Switch"], "variables": ["", "Internal Variable"]})
        report = RpgMakerCoverageAuditor(EngineId.RPGMAKER_MZ).audit(game)
        rows = {(item.file, item.json_path): item for item in report.database_fields}
        self.assertTrue(rows[("System.json", "$.elements[*]")].current_extract)
        self.assertEqual("internal_identifier", rows[("System.json", "$.switches[*]")].role)

    def test_plugin_inventory_reads_registration_without_parameter_values(self) -> None:
        game = _game(self.workspace)
        (game / "js/plugins.js").write_text('var $plugins = [{"name":"AuditPlugin","status":true,"description":"","parameters":{"Secret":"do not persist"}}];', encoding="utf-8")
        (game / "js/plugins/AuditPlugin.js").write_text("""/*:
 * @command addLog
 * @arg messageText
 * @type multiline_string
 */
PluginManager.registerCommand('AuditPlugin', 'addLog', args => {});
""", encoding="utf-8")
        report = RpgMakerCoverageAuditor(EngineId.RPGMAKER_MZ).audit(game)
        plugin = report.plugins[0]
        self.assertEqual(("addLog",), plugin.registered_commands)
        self.assertEqual(("messageText",), plugin.text_like_arguments)
        self.assertNotIn("do not persist", json.dumps(report.to_json_dict()))

    def test_malformed_json_is_reported_and_other_files_continue(self) -> None:
        game = _game(self.workspace)
        (game / "data/Actors.json").write_text("[broken", encoding="utf-8")
        _write_json(game, "Map001.json", _map([_command(401, ["ok"])]))
        report = RpgMakerCoverageAuditor(EngineId.RPGMAKER_MZ).audit(game)
        self.assertTrue(any(item.code == "DATA_READ_ERROR" for item in report.issues))
        self.assertEqual(1, report.statistics["coverage"]["currently_extracted"])

    def test_json_depth_limit_is_bounded(self) -> None:
        game = _game(self.workspace)
        nested: object = "leaf"
        for _ in range(8):
            nested = {"value": nested}
        _write_json(game, "Map001.json", nested)
        with mock.patch.object(audit_module, "MAX_RECURSION_DEPTH", 4):
            report = RpgMakerCoverageAuditor(EngineId.RPGMAKER_MZ).audit(game)
        self.assertTrue(any("depth limit" in item.reason for item in report.issues))

    def test_json_size_and_string_limits_are_bounded(self) -> None:
        game = _game(self.workspace)
        _write_json(game, "Map001.json", _map([_command(401, ["1234567890"])]))
        with mock.patch.object(audit_module, "MAX_JSON_FILE_BYTES", 10):
            size_report = RpgMakerCoverageAuditor(EngineId.RPGMAKER_MZ).audit(game)
        self.assertTrue(any("size limit" in item.reason for item in size_report.issues))
        with mock.patch.object(audit_module, "MAX_CANDIDATE_STRING_LENGTH", 5):
            string_report = RpgMakerCoverageAuditor(EngineId.RPGMAKER_MZ).audit(game)
        self.assertTrue(any("string exceeds" in item.reason for item in string_report.issues))

    def test_audit_is_read_only_and_paths_are_portable(self) -> None:
        game = _game(self.workspace)
        _write_json(game, "Map001.json", _map([_command(401, ["text"])]))
        before = {path.relative_to(game).as_posix(): path.read_bytes() for path in game.rglob("*") if path.is_file()}
        report = RpgMakerCoverageAuditor(EngineId.RPGMAKER_MZ).audit(game)
        after = {path.relative_to(game).as_posix(): path.read_bytes() for path in game.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertTrue(report.source_unchanged)
        self.assertTrue(all(not Path(item.file).is_absolute() for item in report.candidates))

    def test_report_and_csv_are_privacy_preserving_and_reject_overwrite(self) -> None:
        report = self._audit([_command(401, ["private synthetic text"])] )
        json_path = self.workspace / "report.json"
        csv_path = self.workspace / "candidates.csv"
        write_coverage_report(json_path, report)
        write_candidate_csv(csv_path, report)
        self.assertNotIn("private synthetic text", json_path.read_text(encoding="utf-8"))
        self.assertNotIn("private synthetic text", csv_path.read_text(encoding="utf-8-sig"))
        with self.assertRaises(FileExistsError):
            write_coverage_report(json_path, report)
        with self.assertRaises(FileExistsError):
            write_candidate_csv(csv_path, report)

    def test_cli_report_inside_source_is_rejected(self) -> None:
        game = _game(self.workspace)
        with contextlib.redirect_stdout(io.StringIO()):
            code = glt.main(["rpgmaker-audit", str(game), "--report", str(game / "report.json")])
        self.assertEqual(2, code)
        self.assertFalse((game / "report.json").exists())

    def test_cli_mv_and_mz_complete_without_outputs(self) -> None:
        for engine in (EngineId.RPGMAKER_MV, EngineId.RPGMAKER_MZ):
            with self.subTest(engine=engine.value):
                workspace = self.workspace / engine.value
                workspace.mkdir()
                game = _game(workspace, engine)
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = glt.main(["rpgmaker-audit", str(game)])
                self.assertEqual(0, code)
                self.assertIn("Source unchanged: yes", output.getvalue())
                self.assertEqual([], list(game.rglob("report.json")))

    def test_cli_creates_new_json_and_csv_outside_game(self) -> None:
        game = _game(self.workspace)
        _write_json(game, "Map001.json", _map([_command(401, ["synthetic"])]))
        report_path = self.workspace / "audit.json"
        csv_path = self.workspace / "candidates.csv"
        with contextlib.redirect_stdout(io.StringIO()):
            code = glt.main([
                "rpgmaker-audit", str(game),
                "--report", str(report_path),
                "--csv", str(csv_path),
            ])
        self.assertEqual(0, code)
        self.assertEqual("0.9.2", json.loads(report_path.read_text(encoding="utf-8"))["tool_version"])
        self.assertTrue(csv_path.read_text(encoding="utf-8-sig").startswith("file,json_path,"))

    def test_cli_accepts_external_read_only_plugin_evidence(self) -> None:
        game = _game(self.workspace, EngineId.RPGMAKER_MV)
        _write_json(game, "Map001.json", _map([_command(356, ["External text"])]))
        evidence = self.workspace / "evidence"
        sources = evidence / "plugins"
        sources.mkdir(parents=True)
        config = evidence / "plugins.js"
        config.write_text(
            'var $plugins = [{"name":"External","status":true,"description":"","parameters":{}}];',
            encoding="utf-8",
        )
        (sources / "External.js").write_text("""
Game_Interpreter.prototype.pluginCommand = function(command, args) {
 if (command === 'External') { $gameMessage.add(args[0]); }
};
""", encoding="utf-8")
        before = {path.relative_to(evidence): path.read_bytes() for path in evidence.rglob("*") if path.is_file()}
        report_path = self.workspace / "external-audit.json"
        with contextlib.redirect_stdout(io.StringIO()):
            code = glt.main([
                "rpgmaker-audit", str(game), "--plugins-config", str(config),
                "--plugin-source", str(sources), "--report", str(report_path),
            ])
        after = {path.relative_to(evidence): path.read_bytes() for path in evidence.rglob("*") if path.is_file()}
        self.assertEqual(0, code)
        self.assertEqual(before, after)
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(1, payload["plugin_discovery"]["enabled_plugin_count"])


if __name__ == "__main__":
    unittest.main()
