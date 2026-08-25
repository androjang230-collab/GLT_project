from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.models import EngineId
from engines.rpgmaker.audit import RpgMakerCoverageAuditor
from engines.rpgmaker.extractor import RpgMakerExtractor
from engines.rpgmaker.inserter import RpgMakerInserter
from projects.io import read_jsonl, write_jsonl
from projects.manager import ProjectManager


def _command(code: int, parameters: object) -> dict[str, object]:
    return {"code": code, "indent": 0, "parameters": parameters}


def _write_game(root: Path, engine: EngineId, commands: list[dict[str, object]]) -> Path:
    game = root / "game"
    (game / "js").mkdir(parents=True)
    core = "rpg_core.js" if engine == EngineId.RPGMAKER_MV else "rmmz_core.js"
    (game / "js" / core).write_text("// synthetic", encoding="utf-8")
    data = game / "data"
    data.mkdir()
    (data / "System.json").write_text("{}", encoding="utf-8")
    document = {
        "displayName": "",
        "events": [None, {"id": 1, "name": "internal", "pages": [{"list": commands}]}],
    }
    (data / "Map001.json").write_text(
        json.dumps(document, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    return game


def _document(game: Path) -> dict[str, object]:
    return json.loads((game / "data/Map001.json").read_text(encoding="utf-8"))


class RpgMakerPluginTextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _apply(self, game: Path, engine: EngineId, translation: str):
        entry = RpgMakerExtractor(engine).extract(game).entries[0]
        payload = entry.to_json_dict()
        payload["translation"] = translation
        jsonl = self.root / "translation.jsonl"
        jsonl.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
        output = self.root / "output"
        report = RpgMakerInserter(engine).apply(game, jsonl, output)
        return report, output, payload

    def test_mv_verified_payload_extracts_without_prefix_and_applies_with_prefix(self) -> None:
        game = _write_game(
            self.root,
            EngineId.RPGMAKER_MV,
            [_command(356, [r"インフォ表示   \C[2]お知らせ"])],
        )
        entry = RpgMakerExtractor(EngineId.RPGMAKER_MV).extract(game).entries[0]
        self.assertEqual(r"\C[2]お知らせ", entry.original)
        self.assertEqual("verified", entry.extra_metadata["classification"])
        report, output, _ = self._apply(game, EngineId.RPGMAKER_MV, r"\C[2]알림")
        raw = _document(output)["events"][1]["pages"][0]["list"][0]["parameters"][0]
        self.assertEqual(r"インフォ表示   \C[2]알림", raw)
        self.assertEqual(1, report.applied)

    def test_mv_internal_commands_are_excluded_and_unknown_text_is_conditional(self) -> None:
        commands = [
            _command(356, ["P_SHAKE 5 10"]),
            _command(356, ["P_SPIN_RELATIVE 90"]),
            _command(356, ["D_TEXT_SETTING WINDOW 1"]),
            _command(356, ["UNKNOWN_MOVE 1 2"]),
            _command(356, ["UNKNOWN_CMD 表示候補"]),
        ]
        game = _write_game(self.root, EngineId.RPGMAKER_MV, commands)
        self.assertEqual([], RpgMakerExtractor(EngineId.RPGMAKER_MV).extract(game).entries)
        report = RpgMakerCoverageAuditor(EngineId.RPGMAKER_MV).audit(game)
        row = report.statistics["plugin_command_coverage"]["mv_356"]
        self.assertEqual({"verified": 0, "conditional": 1, "internal": 4}, {
            key: row[key] for key in ("verified", "conditional", "internal")
        })

    def test_mv_control_code_loss_is_blocked(self) -> None:
        game = _write_game(
            self.root,
            EngineId.RPGMAKER_MV,
            [_command(356, [r"インフォ表示 \V[1]件"])],
        )
        report, output, _ = self._apply(game, EngineId.RPGMAKER_MV, "한 건")
        self.assertTrue(output.exists())
        self.assertEqual(0, report.applied)
        self.assertTrue(any(issue.code == "CONTROL_CODE_MISMATCH" for issue in report.issues))

    def test_mz_verified_rule_extracts_only_selected_path_and_applies(self) -> None:
        args = {"text": r"\N[1]ログ", "file": "Actor1.png", "nested": {"messageText": "候補"}}
        commands = [_command(357, ["MNKR_TMLogWindowMZ", "addLog", "Add Log", args])]
        game = _write_game(self.root, EngineId.RPGMAKER_MZ, commands)
        extraction = RpgMakerExtractor(EngineId.RPGMAKER_MZ).extract(game)
        self.assertEqual(1, len(extraction.entries))
        self.assertTrue(extraction.entries[0].json_path.endswith(".parameters[3].text"))
        report, output, _ = self._apply(game, EngineId.RPGMAKER_MZ, r"\N[1]로그")
        output_args = _document(output)["events"][1]["pages"][0]["list"][0]["parameters"][3]
        self.assertEqual(r"\N[1]로그", output_args["text"])
        self.assertEqual("Actor1.png", output_args["file"])
        self.assertEqual("候補", output_args["nested"]["messageText"])
        self.assertEqual(1, report.applied)

    def test_mz_nested_text_candidates_are_conditional_and_internal_args_excluded(self) -> None:
        args = {"file": "Actor1.png", "id": "internal", "nested": {"messageText": "表示候補"}}
        game = _write_game(
            self.root,
            EngineId.RPGMAKER_MZ,
            [_command(357, ["OtherPlugin", "show", "Editor Label", args])],
        )
        report = RpgMakerCoverageAuditor(EngineId.RPGMAKER_MZ).audit(game)
        candidates = [item for item in report.candidates if item.command_code == 357]
        self.assertEqual(1, len(candidates))
        self.assertEqual("CONDITIONAL_TRANSLATABLE", candidates[0].classification)
        self.assertEqual(".nested.messageText", candidates[0].argument_path)

    def test_mz_plugin_or_command_mismatch_blocks_apply(self) -> None:
        game = _write_game(
            self.root,
            EngineId.RPGMAKER_MZ,
            [_command(357, ["MNKR_TMLogWindowMZ", "addLog", "Add Log", {"text": "ログ"}])],
        )
        entry = RpgMakerExtractor(EngineId.RPGMAKER_MZ).extract(game).entries[0].to_json_dict()
        entry["translation"] = "로그"
        jsonl = self.root / "translation.jsonl"
        jsonl.write_text(json.dumps(entry, ensure_ascii=False) + "\n", encoding="utf-8")
        document = _document(game)
        document["events"][1]["pages"][0]["list"][0]["parameters"][1] = "otherCommand"
        (game / "data/Map001.json").write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        report = RpgMakerInserter(EngineId.RPGMAKER_MZ).preflight(game, jsonl).report
        self.assertEqual(0, report.applicable)
        self.assertTrue(any(issue.code == "UNKNOWN_ID" for issue in report.issues))

    def test_657_matching_mirror_syncs_and_mismatch_is_preserved_with_warning(self) -> None:
        commands = [
            _command(357, ["MNKR_TMLogWindowMZ", "addLog", "Add Log", {"text": "ログ"}]),
            _command(657, ["text = ログ"]),
            _command(657, ["text = 古い表示"]),
        ]
        game = _write_game(self.root, EngineId.RPGMAKER_MZ, commands)
        report, output, _ = self._apply(game, EngineId.RPGMAKER_MZ, "로그")
        rows = _document(output)["events"][1]["pages"][0]["list"]
        self.assertEqual("text = 로그", rows[1]["parameters"][0])
        self.assertEqual("text = 古い表示", rows[2]["parameters"][0])
        self.assertTrue(any(issue.code == "PLUGIN_MIRROR_MISMATCH" for issue in report.issues))

    def test_657_standalone_is_not_a_translation_entry(self) -> None:
        game = _write_game(
            self.root,
            EngineId.RPGMAKER_MZ,
            [_command(657, ["text = 単独注釈"])],
        )
        self.assertEqual([], RpgMakerExtractor(EngineId.RPGMAKER_MZ).extract(game).entries)
        report = RpgMakerCoverageAuditor(EngineId.RPGMAKER_MZ).audit(game)
        self.assertEqual("INTERNAL", report.candidates[0].classification)
        self.assertEqual(1, report.statistics["plugin_command_coverage"]["code_657"]["standalone"])

    def test_verified_plugin_entry_uses_existing_project_qa_and_apply_flow(self) -> None:
        game = _write_game(
            self.root,
            EngineId.RPGMAKER_MZ,
            [_command(357, ["MNKR_TMLogWindowMZ", "addLog", "Add Log", {"text": "ログ"}])],
        )
        project = self.root / "project"
        manager = ProjectManager()
        created = manager.create(game, project)
        self.assertEqual(1, created.translation_entries)
        records = read_jsonl(project / "translated.jsonl")
        self.assertEqual("plugin_command", records[0]["source_kind"])
        records[0]["translation"] = "로그"
        write_jsonl(project / "translated.jsonl", records)
        qa = manager.qa(project, game)
        self.assertEqual(1, qa.report.applicable)
        output = self.root / "project-output"
        applied = manager.apply(project, game, output)
        self.assertEqual(1, applied.applied)


if __name__ == "__main__":
    unittest.main()
