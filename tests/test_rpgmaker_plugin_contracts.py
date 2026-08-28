from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.models import EngineId, TranslationEntry
from core.translation_io import write_jsonl
from engines.rpgmaker.extractor import RpgMakerExtractor
from engines.rpgmaker.plugin_contracts import (
    ContractType,
    SemanticRole,
    extract_plugin_consumed_text,
)


class RpgMakerPluginContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "game"
        self.data = self.root / "data"
        self.plugins = self.root / "js/plugins"
        self.data.mkdir(parents=True)
        self.plugins.mkdir(parents=True)
        (self.data / "System.json").write_text("{}", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_plugins(
        self,
        sources: dict[str, tuple[bool, str, dict[str, object]]],
    ) -> None:
        records = []
        for name, (enabled, source, parameters) in sources.items():
            records.append(
                {
                    "name": name,
                    "status": enabled,
                    "description": "",
                    "parameters": parameters,
                }
            )
            (self.plugins / f"{name}.js").write_text(source, encoding="utf-8")
        payload = json.dumps(records, ensure_ascii=False)
        (self.root / "js/plugins.js").write_text(
            "var $plugins = " + payload[:-1] + ",];",
            encoding="utf-8",
        )

    def _plugin_entries(self, extractor: RpgMakerExtractor):
        result = extractor.extract(self.root)
        entries = [
            entry
            for entry in result.entries
            if entry.extra_metadata.get("source_kind") == "plugin_consumed_text"
        ]
        return result, entries, extractor.plugin_contract_report

    def _write_states(self, *notes: str) -> None:
        records: list[object] = [None]
        records.extend(
            {"id": index, "name": "", "note": note}
            for index, note in enumerate(notes, 1)
        )
        (self.data / "States.json").write_text(
            json.dumps(records, ensure_ascii=False), encoding="utf-8"
        )

    def _write_event_notes(self, *notes: str) -> None:
        events: list[object] = [None]
        events.extend(
            {"id": index, "name": "", "note": note, "pages": []}
            for index, note in enumerate(notes, 1)
        )
        (self.data / "Map001.json").write_text(
            json.dumps({"displayName": "", "events": events}, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_direct_scalar_visible_parameter_is_translatable_and_extracted(self) -> None:
        self._write_plugins(
            {
                "Visible": (
                    True,
                    """
var parameters = PluginManager.parameters('Visible');
var label = parameters['Menu Label'];
Window_Test.prototype.make = function() { this.addCommand(label, 'go'); };
""",
                    {"Menu Label": "Begin"},
                )
            }
        )
        extractor = RpgMakerExtractor(EngineId.RPGMAKER_MV)

        _, entries, report = self._plugin_entries(extractor)

        self.assertEqual(1, len(entries))
        entry = entries[0]
        self.assertEqual("Begin", entry.original)
        self.assertEqual("js/plugins.js", entry.file)
        self.assertEqual(SemanticRole.TRANSLATABLE_TEXT.value, entry.extra_metadata["semantic_role"])
        self.assertEqual(ContractType.SCALAR_PARAMETER_TEXT.value, entry.extra_metadata["contract_type"])
        self.assertTrue(entry.extra_metadata["apply_supported"])
        self.assertIsInstance(entry.extra_metadata["source_token_start"], int)
        self.assertEqual(1, report.summary()["extracted_entries"])
        output = self.root.parent / "plugin-source.jsonl"
        write_jsonl(entries, output)
        serialized = json.loads(output.read_text(encoding="utf-8").strip())
        self.assertEqual("Begin", serialized["original"])
        self.assertEqual("plugin_consumed_text", serialized["source_kind"])

    def test_numeric_display_formatting_is_not_extracted(self) -> None:
        self._write_plugins(
            {
                "Formatting": (
                    True,
                    """
var parameters = PluginManager.parameters('Formatting');
var size = Number(parameters['Font Size']);
Window_Test.prototype.draw = function() { this.drawTextEx('\\FS[' + size + ']', 0, 0); };
""",
                    {"Font Size": "24"},
                )
            }
        )
        extractor = RpgMakerExtractor(EngineId.RPGMAKER_MV)

        _, entries, report = self._plugin_entries(extractor)

        self.assertEqual([], entries)
        roles = {item.source_access: item.semantic_role for item in report.semantic_findings}
        self.assertEqual(
            SemanticRole.VISIBLE_FORMATTING.value,
            roles["parameters['Font Size']"],
        )

    def test_internal_control_parameter_is_not_extracted(self) -> None:
        self._write_plugins(
            {
                "Control": (
                    True,
                    """
var parameters = PluginManager.parameters('Control');
var switchId = Number(parameters['Switch ID']);
if (switchId > 0) { enabled = true; }
""",
                    {"Switch ID": "4"},
                )
            }
        )
        extractor = RpgMakerExtractor(EngineId.RPGMAKER_MV)

        _, entries, report = self._plugin_entries(extractor)

        self.assertEqual([], entries)
        self.assertIn(
            SemanticRole.INTERNAL_CONTROL.value,
            {item.semantic_role for item in report.semantic_findings},
        )

    def test_literal_note_regex_extracts_capture_without_control_syntax(self) -> None:
        self._write_states("<Label:Visible text>")
        self._write_plugins(
            {
                "RegexNote": (
                    True,
                    r"""
var matched = state.note.match(/^<Label:(.*?)>$/);
var label = matched[1];
Window_Test.prototype.draw = function() { this.drawText(label, 0, 0, 200); };
""",
                    {},
                )
            }
        )
        extractor = RpgMakerExtractor(EngineId.RPGMAKER_MV)

        _, entries, _ = self._plugin_entries(extractor)

        self.assertEqual(1, len(entries))
        self.assertEqual("Visible text", entries[0].original)
        self.assertNotIn("<Label:", entries[0].original)
        self.assertEqual(ContractType.REGEX_CAPTURE_TEXT.value, entries[0].extra_metadata["contract_type"])
        self.assertFalse(entries[0].extra_metadata["apply_supported"])
        self.assertEqual("data/States.json", entries[0].file)
        self.assertEqual("$[1].note", entries[0].json_path)

    def test_deterministic_delimited_block_extracts_body_only(self) -> None:
        self._write_states("<HELP>\nLine one\nLine two\n</HELP>")
        self._write_plugins(
            {
                "BlockNote": (
                    True,
                    r"""
var lines = state.note.split('\n');
var body = lines.slice(1, 2);
if (line.match(/^<HELP>/)) { start = true; }
if (line.match(/^<\/HELP>/)) { start = false; }
Window_Test.prototype.help = function() { this._helpWindow.setText(body); };
""",
                    {},
                )
            }
        )
        extractor = RpgMakerExtractor(EngineId.RPGMAKER_MV)

        _, entries, _ = self._plugin_entries(extractor)

        self.assertEqual(1, len(entries))
        self.assertEqual("Line one\nLine two", entries[0].original)
        self.assertEqual(ContractType.DELIMITED_BLOCK_TEXT.value, entries[0].extra_metadata["contract_type"])
        self.assertTrue(entries[0].extra_metadata["apply_supported"])

    def test_mixed_and_dynamic_parameter_sources_are_not_extracted(self) -> None:
        self._write_plugins(
            {
                "Mixed": (
                    True,
                    """
var parameters = PluginManager.parameters('Mixed');
var label = parameters['Label'];
if (label === 'CONTROL') { enabled = false; }
this.addCommand(label, 'go');
""",
                    {"Label": "Begin"},
                ),
                "Dynamic": (
                    True,
                    """
var parameters = PluginManager.parameters('Dynamic');
var label = parameters[key];
this.drawText(label, 0, 0, 200);
""",
                    {"Label": "Dynamic"},
                ),
            }
        )
        extractor = RpgMakerExtractor(EngineId.RPGMAKER_MV)

        _, entries, report = self._plugin_entries(extractor)

        self.assertEqual([], entries)
        roles = {item.plugin_name: item.semantic_role for item in report.semantic_findings}
        self.assertEqual(SemanticRole.MIXED_USE.value, roles["Mixed"])
        self.assertEqual(SemanticRole.UNKNOWN.value, roles["Dynamic"])

    def test_existing_entry_ownership_suppresses_plugin_note_entry(self) -> None:
        self._write_states("<Label:Visible>")
        self._write_plugins(
            {
                "RegexNote": (
                    True,
                    r"""
var matched = state.note.match(/^<Label:(.*?)>$/);
var label = matched[1];
this.drawText(label, 0, 0, 200);
""",
                    {},
                )
            }
        )
        existing = TranslationEntry(
            id="existing",
            engine=EngineId.RPGMAKER_MV,
            file="data/States.json",
            type="system",
            original="<Label:Visible>",
            json_path="$[1].note",
        )

        report = extract_plugin_consumed_text(
            self.root,
            EngineId.RPGMAKER_MV,
            existing_entries=(existing,),
        )

        self.assertEqual([], report.entries)
        self.assertIn("EXISTING_ENTRY_OVERLAP", {item.code for item in report.suppressions})

    def test_overlapping_contract_spans_are_suppressed(self) -> None:
        self._write_states("<A:one two>")
        self._write_plugins(
            {
                "Overlap": (
                    True,
                    r"""
var firstMatch = state.note.match(/^<A:(one two)>$/);
var first = firstMatch[1];
var secondMatch = state.note.match(/^<A:(one) two>$/);
var second = secondMatch[1];
this.drawText(first, 0, 0, 200);
this.drawText(second, 0, 0, 200);
""",
                    {},
                )
            }
        )
        extractor = RpgMakerExtractor(EngineId.RPGMAKER_MV)

        result, entries, report = self._plugin_entries(extractor)

        self.assertEqual([], entries)
        self.assertIn("PLUGIN_CONTRACT_OVERLAP", {item.code for item in report.suppressions})
        self.assertTrue(any("PLUGIN_CONTRACT_OVERLAP" in item.message for item in result.issues))

    def test_disabled_plugin_has_no_contract_extraction(self) -> None:
        self._write_plugins(
            {
                "Disabled": (
                    False,
                    """
var parameters = PluginManager.parameters('Disabled');
var label = parameters['Label'];
this.addCommand(label, 'go');
""",
                    {"Label": "Hidden"},
                )
            }
        )
        extractor = RpgMakerExtractor(EngineId.RPGMAKER_MV)

        _, entries, report = self._plugin_entries(extractor)

        self.assertEqual([], entries)
        self.assertEqual(0, report.active_plugin_count)

    def test_plugin_consumed_id_is_stable(self) -> None:
        self._write_plugins(
            {
                "Stable": (
                    True,
                    """
var parameters = PluginManager.parameters('Stable');
var label = parameters['Label'];
this.addCommand(label, 'go');
""",
                    {"Label": "Stable label"},
                )
            }
        )

        first = RpgMakerExtractor(EngineId.RPGMAKER_MV).extract(self.root)
        second = RpgMakerExtractor(EngineId.RPGMAKER_MV).extract(self.root)
        first_ids = [entry.id for entry in first.entries if entry.id.startswith("PluginConsumed:")]
        second_ids = [entry.id for entry in second.entries if entry.id.startswith("PluginConsumed:")]

        self.assertEqual(first_ids, second_ids)
        self.assertEqual(1, len(first_ids))

    def test_existing_mv356_and_mz357_ids_are_unchanged(self) -> None:
        map_document = {
            "displayName": "",
            "events": [
                None,
                {
                    "id": 1,
                    "pages": [
                        {
                            "list": [
                                {"code": 356, "indent": 0, "parameters": ["ShowInfo Visible"]},
                                {
                                    "code": 357,
                                    "indent": 0,
                                    "parameters": [
                                        "MNKR_TMLogWindowMZ",
                                        "addLog",
                                        "addLog",
                                        {"text": "MZ visible"},
                                    ],
                                },
                            ]
                        }
                    ],
                },
            ],
        }
        (self.data / "Map001.json").write_text(
            json.dumps(map_document), encoding="utf-8"
        )

        mv_entries = RpgMakerExtractor(EngineId.RPGMAKER_MV).extract(self.root).entries
        mz_entries = RpgMakerExtractor(EngineId.RPGMAKER_MZ).extract(self.root).entries

        self.assertEqual(
            "Map001:event1:page1:cmd356:index0:param0",
            next(entry.id for entry in mv_entries if entry.original == "Visible"),
        )
        self.assertEqual(
            "Map001:event1:page1:cmd357:index1:param3:arg:text",
            next(entry.id for entry in mz_entries if entry.original == "MZ visible"),
        )

    def test_dynamic_meta_tokenized_segment_binds_exact_event_note(self) -> None:
        self._write_event_notes("<displayTag:Visible Name|12|black>")
        self._write_plugins(
            {
                "BehaviorFixture": (
                    True,
                    """
FloatingPanel.prototype.read = function(keyCode) { return this.event().meta[keyCode]; };
FloatingPanel.prototype.setup = function() {
  var payload = this.read('displayTag');
  var fields = payload.split('|');
  this._caption = fields[0];
};
FloatingPanel.prototype.redraw = function() { this.drawText(this._caption, 0, 0, 200); };
""",
                    {},
                )
            }
        )

        first_result, first_entries, _ = self._plugin_entries(
            RpgMakerExtractor(EngineId.RPGMAKER_MV)
        )
        second_result, second_entries, _ = self._plugin_entries(
            RpgMakerExtractor(EngineId.RPGMAKER_MV)
        )

        self.assertEqual([], first_result.issues)
        self.assertEqual([], second_result.issues)
        self.assertEqual(1, len(first_entries))
        entry = first_entries[0]
        self.assertEqual("Visible Name", entry.original)
        self.assertEqual("data/Map001.json", entry.file)
        self.assertEqual("$.events[1].note", entry.json_path)
        self.assertEqual(
            ContractType.TOKENIZED_VISIBLE_SEGMENT.value,
            entry.extra_metadata["contract_type"],
        )
        self.assertTrue(entry.extra_metadata["apply_supported"])
        self.assertEqual(entry.id, second_entries[0].id)

    def test_ambiguous_dynamic_meta_note_binding_is_audit_only(self) -> None:
        self._write_event_notes("<captionTag:First|1><captionTag:Second|2>")
        self._write_plugins(
            {
                "AmbiguousStorage": (
                    True,
                    """
Panel.prototype.read = function(key) { return this.event().meta[key]; };
Panel.prototype.setup = function() {
  var raw = this.read('captionTag');
  var values = raw.split('|');
  this._text = values[0];
};
Panel.prototype.paint = function() { this.drawText(this._text, 0, 0, 100); };
""",
                    {},
                )
            }
        )

        _, entries, report = self._plugin_entries(
            RpgMakerExtractor(EngineId.RPGMAKER_MV)
        )

        self.assertEqual([], entries)
        finding = next(item for item in report.semantic_findings if item.source_kind == "meta")
        self.assertEqual(SemanticRole.UNKNOWN.value, finding.semantic_role)

    def test_cross_plugin_dynamic_contract_is_behavioral(self) -> None:
        self._write_event_notes(
            "<badgeText:Alpha|12><floatingCaption:Beta|blue>"
        )
        self._write_plugins(
            {
                "BadgeExample": (
                    True,
                    """
BadgeNode.prototype.fetch = function(code) { return this.event().meta[code]; };
BadgeNode.prototype.prepare = function() { var raw = this.fetch('badgeText'); var p = raw.split('|'); this._value = p[0]; };
BadgeNode.prototype.paint = function() { this.drawText(this._value, 0, 0, 100); };
""",
                    {},
                ),
                "DifferentWidget": (
                    True,
                    """
WidgetNode.prototype.obtain = function(propertyName) { return this.event().meta[propertyName]; };
WidgetNode.prototype.load = function() { var data = this.obtain('floatingCaption'); var chunks = data.split('|'); this._label = chunks[0]; };
WidgetNode.prototype.render = function() { this.drawText(this._label, 0, 0, 100); };
""",
                    {},
                ),
            }
        )

        _, entries, _ = self._plugin_entries(
            RpgMakerExtractor(EngineId.RPGMAKER_MV)
        )

        self.assertEqual({"Alpha", "Beta"}, {entry.original for entry in entries})
        self.assertEqual(
            {ContractType.TOKENIZED_VISIBLE_SEGMENT.value},
            {entry.extra_metadata["contract_type"] for entry in entries},
        )

    def test_dynamic_meta_mixed_logic_and_display_is_not_extracted(self) -> None:
        self._write_event_notes("<mixedTag:Visible|12>")
        self._write_plugins(
            {
                "MixedBehavior": (
                    True,
                    """
MixedPanel.prototype.read = function(key) { return this.event().meta[key]; };
MixedPanel.prototype.setup = function() {
  var raw = this.read('mixedTag');
  var parts = raw.split('|');
  var label = parts[0];
  if (label === 'CONTROL') { this._mode = 1; }
  this._label = label;
};
MixedPanel.prototype.paint = function() { this.drawText(this._label, 0, 0, 100); };
""",
                    {},
                )
            }
        )

        _, entries, report = self._plugin_entries(
            RpgMakerExtractor(EngineId.RPGMAKER_MV)
        )

        self.assertEqual([], entries)
        finding = next(item for item in report.semantic_findings if item.source_kind == "meta")
        self.assertNotEqual(SemanticRole.TRANSLATABLE_TEXT.value, finding.semantic_role)


if __name__ == "__main__":
    unittest.main()
