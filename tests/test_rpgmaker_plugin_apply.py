from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from core.models import EngineId, ExtractionResult
from engines.rpgmaker.detector import RpgMakerEngine
from engines.rpgmaker.extractor import RpgMakerExtractor
from engines.rpgmaker.inserter import RpgMakerInserter
from engines.rpgmaker.plugin_contracts import ContractType
from engines.rpgmaker.plugin_inventory import load_plugin_inventory


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class RpgMakerPluginApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.game = self.workspace / "game"
        self.output = self.workspace / "output"
        self.translation = self.workspace / "translated.jsonl"
        (self.game / "data").mkdir(parents=True)
        (self.game / "js/plugins").mkdir(parents=True)
        (self.game / "js/rpg_core.js").write_text("// core", encoding="utf-8")
        (self.game / "data/System.json").write_text("{}", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_plugins(
        self,
        parameters: dict[str, object],
        source: str,
        *,
        trailing_comma: bool = True,
        crlf: bool = False,
        bom: bool = False,
    ) -> bytes:
        (self.game / "js/plugins/Visible.js").write_text(source, encoding="utf-8")
        record = {
            "name": "Visible",
            "status": True,
            "description": "unchanged description",
            "parameters": parameters,
        }
        newline = "\r\n" if crlf else "\n"
        ending = "," if trailing_comma else ""
        text = (
            "var $plugins = ["
            + newline
            + json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            + ending
            + newline
            + "];"
            + newline
        )
        payload = text.encode("utf-8-sig" if bom else "utf-8")
        (self.game / "js/plugins.js").write_bytes(payload)
        return payload

    def _write_states(self, *notes: str) -> None:
        records: list[object] = [None]
        records.extend(
            {"id": index, "name": "", "note": note, "priority": 50}
            for index, note in enumerate(notes, 1)
        )
        (self.game / "data/States.json").write_text(
            json.dumps(records, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

    def _plugin_entries(self, contract_type: str | None = None):
        result = RpgMakerExtractor(EngineId.RPGMAKER_MV).extract(self.game)
        self.assertFalse(result.issues)
        entries = [
            entry
            for entry in result.entries
            if entry.extra_metadata.get("source_kind") == "plugin_consumed_text"
            and (
                contract_type is None
                or entry.extra_metadata.get("contract_type") == contract_type
            )
        ]
        return entries

    def _write_translations(self, entries, translations: list[str]) -> None:
        rows = []
        for entry, translation in zip(entries, translations, strict=True):
            row = entry.to_json_dict()
            row["translation"] = translation
            rows.append(row)
        self.translation.write_text(
            "".join(
                json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                for row in rows
            ),
            encoding="utf-8",
        )

    def _scalar_source(self, *keys: str) -> str:
        lines = ["var parameters = PluginManager.parameters('Visible');"]
        for index, key in enumerate(keys):
            lines.append(f"var label{index} = parameters[{key!r}];")
            lines.append(f"this.addCommand(label{index}, 'command{index}');")
        return "\n".join(lines)

    def _write_block_plugin(self) -> None:
        self._write_plugins(
            {},
            r"""
var lines = state.note.split('\n');
var body = lines.slice(1, 2);
if (line.match(/^<HELP>/)) { start = true; }
if (line.match(/^<\/HELP>/)) { start = false; }
this._helpWindow.setText(body);
""",
        )

    def test_scalar_apply_changes_only_token_and_preserves_registry_format(self) -> None:
        before = self._write_plugins(
            {"Menu Label": "Begin", "Internal": "a,b,\\path"},
            self._scalar_source("Menu Label"),
            trailing_comma=True,
            crlf=True,
            bom=True,
        )
        entry = self._plugin_entries(ContractType.SCALAR_PARAMETER_TEXT.value)[0]
        self._write_translations([entry], ["시작"])

        report = RpgMakerInserter(EngineId.RPGMAKER_MV).apply(
            self.game, self.translation, self.output
        )

        after = (self.output / "js/plugins.js").read_bytes()
        source_text = before.decode("utf-8-sig")
        start = entry.extra_metadata["source_token_start"]
        end = entry.extra_metadata["source_token_end"]
        expected_text = source_text[:start] + json.dumps("시작", ensure_ascii=False) + source_text[end:]
        expected = expected_text.encode("utf-8-sig")
        self.assertEqual(expected, after)
        self.assertIn(b",\r\n];\r\n", after)
        self.assertIn(b'a,b,\\\\path', after)
        self.assertEqual(1, report.applied)

    def test_scalar_apply_escapes_quotes_backslashes_and_newline(self) -> None:
        self._write_plugins(
            {"Menu Label": "Begin"}, self._scalar_source("Menu Label")
        )
        entry = self._plugin_entries()[0]
        translated = '표시 "인용" D:\\Folder\n둘째 줄'
        self._write_translations([entry], [translated])

        RpgMakerInserter(EngineId.RPGMAKER_MV).apply(
            self.game, self.translation, self.output
        )

        inventory = load_plugin_inventory(
            self.output / "js/plugins.js", self.output / "js/plugins"
        )
        self.assertEqual(
            translated,
            inventory.active_plugins[0].parameters["Menu Label"],
        )

    def test_scalar_source_change_after_extraction_is_rejected(self) -> None:
        self._write_plugins(
            {"Menu Label": "Begin"}, self._scalar_source("Menu Label")
        )
        entry = self._plugin_entries()[0]
        self._write_translations([entry], ["시작"])
        config = self.game / "js/plugins.js"
        config.write_text(
            config.read_text(encoding="utf-8").replace('"Begin"', '"Changed"'),
            encoding="utf-8",
        )

        report = RpgMakerInserter(EngineId.RPGMAKER_MV).preflight(
            self.game, self.translation
        ).report

        self.assertEqual(0, report.applicable)
        self.assertIn(
            "SOURCE_TEXT_MISMATCH", {issue.code for issue in report.issues}
        )

    def test_wrong_scalar_storage_identity_is_rejected(self) -> None:
        self._write_plugins(
            {"Menu Label": "Begin"}, self._scalar_source("Menu Label")
        )
        entry = self._plugin_entries()[0]
        row = entry.to_json_dict()
        row["translation"] = "시작"
        row["storage_identity"] = "js/plugins.js:$plugins[9].parameters['Wrong']"
        self.translation.write_text(
            json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8"
        )

        report = RpgMakerInserter(EngineId.RPGMAKER_MV).preflight(
            self.game, self.translation
        ).report

        self.assertEqual(0, report.applicable)
        self.assertIn(
            "PLUGIN_STORAGE_IDENTITY_MISMATCH",
            {issue.code for issue in report.issues},
        )

    def test_delimited_apply_changes_body_only(self) -> None:
        original_note = "prefix\n<HELP>\nOld body\n</HELP>\nsuffix:<OTHER>keep</OTHER>"
        self._write_states(original_note)
        self._write_block_plugin()
        entry = self._plugin_entries(ContractType.DELIMITED_BLOCK_TEXT.value)[0]
        self._write_translations([entry], ["새 본문\n두 번째 줄"])

        report = RpgMakerInserter(EngineId.RPGMAKER_MV).apply(
            self.game, self.translation, self.output
        )

        states = json.loads(
            (self.output / "data/States.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            "prefix\n<HELP>\n새 본문\n두 번째 줄\n</HELP>\nsuffix:<OTHER>keep</OTHER>",
            states[1]["note"],
        )
        self.assertEqual(50, states[1]["priority"])
        self.assertEqual(1, report.applied)

    def test_multiple_blocks_in_one_note_use_stable_segments(self) -> None:
        self._write_states("<HELP>\nFirst\n</HELP>\nmid\n<HELP>\nSecond\n</HELP>")
        self._write_block_plugin()
        entries = self._plugin_entries(ContractType.DELIMITED_BLOCK_TEXT.value)
        self.assertEqual(["First", "Second"], [item.original for item in entries])
        self._write_translations(entries, ["첫 번째가 길어짐", "둘째"])

        report = RpgMakerInserter(EngineId.RPGMAKER_MV).apply(
            self.game, self.translation, self.output
        )

        note = json.loads(
            (self.output / "data/States.json").read_text(encoding="utf-8")
        )[1]["note"]
        self.assertEqual(
            "<HELP>\n첫 번째가 길어짐\n</HELP>\nmid\n<HELP>\n둘째\n</HELP>", note
        )
        self.assertEqual(2, report.applied)

    def test_note_change_after_extraction_is_rejected(self) -> None:
        self._write_states("<HELP>\nOld\n</HELP>\nunchanged")
        self._write_block_plugin()
        entry = self._plugin_entries()[0]
        self._write_translations([entry], ["새 본문"])
        states_path = self.game / "data/States.json"
        states = json.loads(states_path.read_text(encoding="utf-8"))
        states[1]["note"] += "\nchanged outside body"
        states_path.write_text(json.dumps(states, ensure_ascii=False), encoding="utf-8")

        report = RpgMakerInserter(EngineId.RPGMAKER_MV).preflight(
            self.game, self.translation
        ).report

        self.assertEqual(0, report.applicable)
        self.assertIn(
            "PLUGIN_SOURCE_FINGERPRINT_MISMATCH",
            {issue.code for issue in report.issues},
        )

    def test_multiple_scalar_edits_in_one_file_do_not_shift_offsets(self) -> None:
        self._write_plugins(
            {"First Text": "A", "Second Text": "Second"},
            self._scalar_source("First Text", "Second Text"),
        )
        entries = self._plugin_entries(ContractType.SCALAR_PARAMETER_TEXT.value)
        self.assertEqual(2, len(entries))
        self._write_translations(entries, ["아주 긴 첫 번째", "둘"])

        report = RpgMakerInserter(EngineId.RPGMAKER_MV).apply(
            self.game, self.translation, self.output
        )

        values = {
            entry.extra_metadata.get("storage_key"): entry.original
            for entry in RpgMakerExtractor(EngineId.RPGMAKER_MV)
            .extract(self.output)
            .entries
            if entry.extra_metadata.get("contract_type")
            == ContractType.SCALAR_PARAMETER_TEXT.value
        }
        self.assertEqual("아주 긴 첫 번째", values["First Text"])
        self.assertEqual("둘", values["Second Text"])
        self.assertEqual(2, report.applied)

    def test_unexpected_overlapping_plans_block_the_storage_unit(self) -> None:
        self._write_states("<HELP>\nBody\n</HELP>")
        self._write_block_plugin()
        entry = self._plugin_entries()[0]
        duplicate = replace(entry, id=entry.id + ":duplicate")
        self._write_translations([entry, duplicate], ["첫째", "둘째"])
        fake = ExtractionResult(entries=[entry, duplicate])

        with patch(
            "engines.rpgmaker.inserter.RpgMakerExtractor.extract",
            return_value=fake,
        ):
            report = RpgMakerInserter(EngineId.RPGMAKER_MV).preflight(
                self.game, self.translation
            ).report

        self.assertEqual(0, report.applicable)
        self.assertEqual(2, report.extra_metadata["plugin_contracts"]["overlap_conflicts"])
        self.assertEqual(
            2,
            sum(issue.code == "PLUGIN_EDIT_OVERLAP" for issue in report.issues),
        )

    def test_dry_run_shares_preflight_and_creates_no_output(self) -> None:
        self._write_plugins(
            {"Menu Label": "Begin"}, self._scalar_source("Menu Label")
        )
        entry = self._plugin_entries()[0]
        self._write_translations([entry], ["시작"])
        inserter = RpgMakerInserter(EngineId.RPGMAKER_MV)
        direct = inserter.preflight(
            self.game, self.translation, output_directory=self.output
        ).report

        dry = RpgMakerEngine().apply(
            self.game,
            self.translation,
            self.output,
            dry_run=True,
        )

        self.assertEqual(direct.planned_ids, dry.planned_ids)
        self.assertEqual(direct.issues, dry.issues)
        self.assertFalse(self.output.exists())
        self.assertEqual(
            ["js/plugins.js"],
            dry.extra_metadata["plugin_contracts"]["planned_files"],
        )

    def test_unsupported_regex_contract_is_not_applied(self) -> None:
        self._write_states("<Label:Old>")
        self._write_plugins(
            {},
            r"""
var matched = state.note.match(/^<Label:(.*?)>$/);
var label = matched[1];
this.drawText(label, 0, 0, 200);
""",
        )
        entry = self._plugin_entries(ContractType.REGEX_CAPTURE_TEXT.value)[0]
        self.assertFalse(entry.extra_metadata["apply_supported"])
        self._write_translations([entry], ["새 값"])

        report = RpgMakerInserter(EngineId.RPGMAKER_MV).preflight(
            self.game, self.translation
        ).report

        self.assertEqual(0, report.applicable)
        self.assertIn(
            "PLUGIN_CONTRACT_UNSUPPORTED", {issue.code for issue in report.issues}
        )

    def test_standard_and_contract_apply_share_the_safe_pipeline(self) -> None:
        self._write_states("<HELP>\nOld help\n</HELP>")
        self._write_block_plugin()
        map_document = {
            "displayName": "",
            "events": [
                None,
                {
                    "id": 1,
                    "pages": [
                        {"list": [{"code": 401, "indent": 0, "parameters": ["Old dialogue"]}]}
                    ],
                },
            ],
        }
        (self.game / "data/Map001.json").write_text(
            json.dumps(map_document), encoding="utf-8"
        )
        entries = RpgMakerExtractor(EngineId.RPGMAKER_MV).extract(self.game).entries
        selected = [
            entry
            for entry in entries
            if entry.original in {"Old dialogue", "Old help"}
        ]
        translations = [
            "새 대화" if entry.original == "Old dialogue" else "새 도움말"
            for entry in selected
        ]
        self._write_translations(selected, translations)

        report = RpgMakerInserter(EngineId.RPGMAKER_MV).apply(
            self.game, self.translation, self.output
        )

        output_map = json.loads(
            (self.output / "data/Map001.json").read_text(encoding="utf-8")
        )
        output_states = json.loads(
            (self.output / "data/States.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            "새 대화", output_map["events"][1]["pages"][0]["list"][0]["parameters"][0]
        )
        self.assertIn("새 도움말", output_states[1]["note"])
        self.assertEqual(2, report.applied)


if __name__ == "__main__":
    unittest.main()
