from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from engines.rpgmaker.mv_plugin_discovery import (
    APPLY_VERIFIED,
    INTERNAL,
    UNKNOWN,
    discover_mv_plugin_commands,
)


class MvPluginDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.plugins = self.root / "plugins"
        self.plugins.mkdir()
        self.config = self.root / "plugins.js"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write(self, sources: dict[str, str], statuses: dict[str, bool] | None = None):
        statuses = statuses or {name: True for name in sources}
        records = []
        for name, enabled in statuses.items():
            records.append({"name": name, "status": enabled, "description": "", "parameters": {}})
        self.config.write_text("var $plugins = " + json.dumps(records) + ";", encoding="utf-8")
        for name, source in sources.items():
            (self.plugins / f"{name}.js").write_text(source, encoding="utf-8")
        return discover_mv_plugin_commands(self.config, self.plugins)

    def test_direct_branch_args_zero_reaches_display(self) -> None:
        report = self._write({"Direct": """
var old = Game_Interpreter.prototype.pluginCommand;
Game_Interpreter.prototype.pluginCommand = function(command, args) {
 old.call(this, command, args);
 if (command === 'Notice') { $gameMessage.add(args[0]); }
};
"""})
        item = report.observations[0]
        self.assertEqual(APPLY_VERIFIED, item.classification)
        self.assertEqual("single_token", item.argument_mode)
        self.assertEqual("requires_protection", item.space_policy)
        self.assertEqual((0,), item.consumed_arguments)

    def test_switch_join_and_slice_modes(self) -> None:
        report = self._write({"Switch": """
Game_Interpreter.prototype.pluginCommand = function(command, args) {
 switch (command.toUpperCase()) {
 case 'POPUP': $gameMessage.add(args.join(' ')); break;
 case 'TAIL': $gameMessage.add(args.slice(2).join(" ")); break;
 }
};
"""})
        rows = {item.command: item for item in report.observations}
        self.assertEqual("joined_remainder", rows["POPUP"].argument_mode)
        self.assertEqual("joined_slice", rows["TAIL"].argument_mode)
        self.assertEqual(2, rows["TAIL"].payload_start)
        self.assertTrue(all(item.space_policy == "safe" for item in rows.values()))
        self.assertTrue(rows["POPUP"].matches("popup"))

    def test_multiple_fixed_arguments_are_discovered_but_not_apply_verified(self) -> None:
        report = self._write({"Multi": """
Game_Interpreter.prototype.pluginCommand = function(command, args) {
 if (command === 'Multi') { $gameMessage.add(args[0] + ':' + args[2]); }
};
"""})
        item = report.observations[0]
        self.assertEqual("DISCOVERED_VERIFIED", item.classification)
        self.assertEqual("multiple_fixed", item.argument_mode)
        self.assertEqual((0, 2), item.consumed_arguments)

    def test_local_variable_and_one_helper_hop_reach_display(self) -> None:
        report = self._write({"Helpers": """
function showNotice(text) { $gameMessage.add(text); }
Game_Interpreter.prototype.pluginCommand = function(command, args) {
 if (command == 'Local') { var text = args[0]; $gameMessage.add(text); }
 if (command == 'Helper') { showNotice(args.join(' ')); }
};
"""})
        rows = {item.command: item for item in report.observations}
        self.assertEqual(APPLY_VERIFIED, rows["Local"].classification)
        self.assertEqual(("showNotice",), rows["Helper"].helper_chain)
        self.assertEqual("joined_remainder", rows["Helper"].argument_mode)

    def test_alias_wrapper_and_helper_dispatch_are_detected(self) -> None:
        report = self._write({"Wrapper": """
var upstream = Game_Interpreter.prototype.pluginCommand;
Game_Interpreter.prototype.pluginCommand = function(command, args) {
 upstream.call(this, command, args);
 this.routePlugin(command, args);
};
Game_Interpreter.prototype.routePlugin = function(cmd, argv) {
 var copied = cmd;
 if (copied === 'Wrapped') { $gameMessage.add(argv[1]); }
};
"""})
        item = report.observations[0]
        self.assertEqual("helper_dispatch", item.handler_kind)
        self.assertEqual("fixed_index", item.argument_mode)
        self.assertEqual(("routePlugin",), item.helper_chain)

    def test_numeric_identifier_and_asset_sinks_are_internal(self) -> None:
        report = self._write({"Internal": """
Game_Interpreter.prototype.pluginCommand = function(command, args) {
 if (command === 'Numeric') { var id = Number(args[0]); $gameVariables.setValue(1, id); }
 if (command === 'Key') { $gameSwitches.setValue(args[0], true); }
 if (command === 'Asset') { ImageManager.loadPicture(args[0]); }
 if (command === 'ShopKey') { this.openShop(args[0]); }
 if (command === 'ClassKey') { this.unlockClass(args[0]); }
 if (command === 'SaveKey') { this.save(args[0]); }
};
"""})
        rows = {item.command: item for item in report.observations}
        self.assertTrue(all(rows[name].classification == INTERNAL for name in rows))
        self.assertEqual("numeric", rows["Numeric"].argument_mode)

    def test_unrelated_display_and_dynamic_dispatch_are_not_verified(self) -> None:
        report = self._write({"FalsePositive": """
function unrelated() { this.drawText('constant', 0, 0); }
Game_Interpreter.prototype.pluginCommand = function(command, args) {
 if (command === '日本語命令') { this.route[args[0]](); }
 if (command === 'OtherBranch') { $gameMessage.add(args[0]); }
 this[command](args);
};
"""})
        rows = {item.command: item for item in report.observations}
        self.assertEqual(UNKNOWN, rows["日本語命令"].classification)
        self.assertEqual(APPLY_VERIFIED, rows["OtherBranch"].classification)

    def test_japanese_comment_and_dynamic_command_construction_create_no_rule(self) -> None:
        report = self._write({"Dynamic": """
// command === '日本語コメント' and drawText(args[0])
Game_Interpreter.prototype.pluginCommand = function(command, args) {
 var route = 'prefix_' + command;
 this[route](args);
};
"""})
        self.assertEqual([], report.observations)

    def test_eval_is_unsafe_and_not_verified(self) -> None:
        report = self._write({"Eval": """
Game_Interpreter.prototype.pluginCommand = function(command, args) {
 if (command === 'Run') { eval(args[0]); }
};
"""})
        self.assertEqual("UNSAFE", report.observations[0].classification)

    def test_disabled_plugin_and_missing_source_are_not_analyzed(self) -> None:
        report = self._write(
            {"Off": "Game_Interpreter.prototype.pluginCommand=function(command,args){if(command==='Off'){$gameMessage.add(args[0]);}};"},
            {"Off": False, "Missing": True},
        )
        self.assertEqual([], report.observations)
        self.assertTrue(any(item.code == "PLUGIN_SOURCE_MISSING" for item in report.issues))

    def test_duplicate_command_across_enabled_plugins_is_ambiguous(self) -> None:
        body = "Game_Interpreter.prototype.pluginCommand=function(command,args){if(command==='Same'){$gameMessage.add(args[0]);}};"
        report = self._write({"A": body, "B": body})
        self.assertEqual(2, len(report.observations))
        self.assertTrue(all(item.classification == UNKNOWN for item in report.observations))
        self.assertTrue(all("load-order" in (item.unresolved_reason or "") for item in report.observations))

    def test_source_fingerprint_is_read_only_and_stable(self) -> None:
        report = self._write({"Stable": "Game_Interpreter.prototype.pluginCommand=function(command,args){};"})
        self.assertTrue(report.source_unchanged)
        self.assertEqual(report.source_fingerprint_before, report.source_fingerprint_after)

    def test_malformed_plugin_registry_is_reported(self) -> None:
        self.config.write_text("var notPlugins = [];", encoding="utf-8")
        report = discover_mv_plugin_commands(self.config, self.plugins)
        self.assertEqual([], report.observations)
        self.assertTrue(any(item.code == "PLUGIN_REGISTRY_ERROR" for item in report.issues))

    def test_generic_command_transform_and_reconstruction_helpers(self) -> None:
        report = self._write({"Transformed": """
var normalizeRoute = function(value) { return (value || '').toUpperCase(); };
var rebuildWords = function(parts) { return parts.join(' '); };
$display.queueWindowText = function(value) { this.windowText = value; };
Game_Interpreter.prototype.pluginCommand = function(command, args) {
 var localCommand = command;
 var localArgs = args;
 this.routeTransformed(localCommand, localArgs);
};
Game_Interpreter.prototype.routeTransformed = function(cmd, argv) {
 switch (normalizeRoute(cmd)) {
 case 'ANNOUNCE': $display.queueWindowText(rebuildWords(argv)); break;
 }
};
"""})
        item = report.observations[0]
        self.assertEqual("ANNOUNCE", item.command)
        self.assertEqual("upper", item.command_normalization)
        self.assertEqual("joined_remainder", item.argument_mode)
        self.assertEqual("safe", item.space_policy)
        self.assertEqual(APPLY_VERIFIED, item.classification)

    def test_command_dispatch_helper_depth_two_is_bounded(self) -> None:
        report = self._write({"TwoHop": """
Game_Interpreter.prototype.pluginCommand = function(command, args) {
 this.firstRoute(command, args);
};
Game_Interpreter.prototype.firstRoute = function(cmd, argv) {
 this.secondRoute(cmd, argv);
};
Game_Interpreter.prototype.secondRoute = function(name, values) {
 if (name.toLowerCase() === 'twostep') { $gameMessage.add(values[0]); }
};
"""})
        item = report.observations[0]
        self.assertEqual(("firstRoute", "secondRoute"), item.helper_chain)
        self.assertEqual("lower", item.command_normalization)
        self.assertEqual("single_token", item.argument_mode)

    def test_display_configuration_and_dynamic_helper_are_not_verified(self) -> None:
        report = self._write({"Conservative": """
Game_Screen.prototype.setWindowTextAlign = function(value) {
 this.windowTextAlign = value;
};
Game_Interpreter.prototype.pluginCommand = function(command, args) {
 if (command === 'ConfigOnly') { $gameScreen.setWindowTextAlign(args[0]); }
 if (command === 'Dynamic') { this[args[0]](args); }
};
"""})
        rows = {item.command: item for item in report.observations}
        self.assertNotEqual(APPLY_VERIFIED, rows["ConfigOnly"].classification)
        self.assertEqual(UNKNOWN, rows["Dynamic"].classification)


if __name__ == "__main__":
    unittest.main()
