"""Bounded, privacy-preserving RPG Maker MV/MZ coverage audit.

This module observes translation-shaped data.  It never creates TranslationEntry
objects and therefore cannot silently expand the extractor's allowlist.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from core.models import EngineId
from core.version import TOOL_VERSION
from engines.rpgmaker.audit_models import (
    AuditIssue,
    DatabaseFieldObservation,
    MirrorObservation,
    PluginInventory,
    RpgMakerCoverageReport,
    SourceSnapshot,
    StringCandidate,
    TranslationClassification as Class,
)
from engines.rpgmaker.extractor import find_control_codes
from engines.rpgmaker.plugin_rules import (
    INTERNAL as PLUGIN_INTERNAL,
    VERIFIED as PLUGIN_VERIFIED,
    classify_mv_command,
    extract_runtime_payload,
    iter_mz_argument_texts,
    parse_editor_annotation,
)
from engines.rpgmaker.mv_plugin_discovery import (
    APPLY_VERIFIED,
    DISCOVERED_VERIFIED,
    INTERNAL as DISCOVERY_INTERNAL,
    UNKNOWN as DISCOVERY_UNKNOWN,
    UNSAFE as DISCOVERY_UNSAFE,
    MvPluginDiscovery,
    discover_mv_plugin_commands,
)
from engines.rpgmaker.plugin_inventory import (
    PluginInventory as ActivePluginInventory,
    load_plugin_inventory,
)
from engines.rpgmaker.plugin_visibility import analyze_plugin_visibility
from engines.rpgmaker.validator import detect_japanese_scripts


MAX_JSON_FILE_BYTES = 128 * 1024 * 1024
MAX_PLUGIN_FILE_BYTES = 8 * 1024 * 1024
MAX_FILES = 20_000
MAX_EVENT_COMMANDS = 2_000_000
MAX_RECURSION_DEPTH = 32
MAX_CANDIDATE_STRING_LENGTH = 1_048_576
MAX_SELECTED_SOURCE_BYTES = 4 * 1024 * 1024 * 1024
REPORT_SCHEMA_VERSION = 1


# The complete stock MV/MZ event-list code family (including continuation and
# branch rows). Names are stable English labels used only for audit reports.
_EVENT_NAMES = dict(
    (
        (0, "End"), (101, "Show Text"), (401, "Text Data"),
        (102, "Show Choices"), (402, "When Choice"),
        (403, "When Cancel"), (404, "End Choices"),
        (103, "Input Number"), (104, "Select Item"),
        (105, "Show Scrolling Text"), (405, "Scroll Text Data"),
        (121, "Control Switches"), (122, "Control Variables"),
        (123, "Control Self Switch"), (124, "Control Timer"),
        (111, "Conditional Branch"), (411, "Else"),
        (412, "End Conditional"), (112, "Loop"),
        (413, "Repeat Above"), (113, "Break Loop"),
        (115, "Exit Event Processing"), (117, "Common Event"),
        (118, "Label"), (119, "Jump to Label"),
        (108, "Comment"), (408, "Comment Continuation"),
        (125, "Change Gold"), (126, "Change Items"),
        (127, "Change Weapons"), (128, "Change Armor"),
        (129, "Change Party Member"), (311, "Change HP"),
        (312, "Change MP"), (326, "Change TP"),
        (313, "Change State"), (314, "Recover All"),
        (315, "Change EXP"), (316, "Change Level"),
        (317, "Change Parameter"), (318, "Change Skill"),
        (319, "Change Equipment"), (320, "Change Name"),
        (321, "Change Class"), (324, "Change Nickname"),
        (325, "Change Profile"), (201, "Transfer Player"),
        (202, "Set Vehicle Location"), (203, "Set Event Location"),
        (204, "Scroll Map"), (205, "Set Movement Route"),
        (505, "Movement Route Data"), (206, "Get On/Off Vehicle"),
        (211, "Change Transparency"), (216, "Change Followers"),
        (217, "Gather Followers"), (212, "Show Animation"),
        (213, "Show Balloon Icon"), (214, "Erase Event"),
        (221, "Fadeout Screen"), (222, "Fadein Screen"),
        (223, "Tint Screen"), (224, "Flash Screen"),
        (225, "Shake Screen"), (230, "Wait"),
        (231, "Show Picture"), (232, "Move Picture"),
        (233, "Rotate Picture"), (234, "Tint Picture"),
        (235, "Erase Picture"), (236, "Set Weather Effect"),
        (241, "Play BGM"), (242, "Fadeout BGM"),
        (243, "Save BGM"), (244, "Replay BGM"),
        (245, "Play BGS"), (246, "Fadeout BGS"),
        (249, "Play ME"), (250, "Play SE"), (251, "Stop SE"),
        (261, "Play Movie"), (301, "Battle Processing"),
        (601, "If Win"), (602, "If Escape"), (603, "If Lose"),
        (604, "End Battle Processing"), (302, "Shop Processing"),
        (605, "Shop Goods"), (303, "Name Input Processing"),
        (351, "Open Menu Screen"), (352, "Open Save Screen"),
        (353, "Game Over"), (354, "Return to Title"),
        (132, "Change Battle BGM"), (133, "Change Victory ME"),
        (139, "Change Defeat ME"), (140, "Change Vehicle BGM"),
        (134, "Change Save Access"), (135, "Change Menu Access"),
        (136, "Change Encounter"), (137, "Change Formation Access"),
        (138, "Change Window Color"), (322, "Change Actor Images"),
        (323, "Change Vehicle Image"), (281, "Change Map Name Display"),
        (282, "Change Tileset"), (283, "Change Battle Back"),
        (284, "Change Parallax"), (285, "Get Location Info"),
        (331, "Change Enemy HP"), (332, "Change Enemy MP"),
        (342, "Change Enemy TP"), (333, "Change Enemy State"),
        (334, "Enemy Recover All"), (335, "Enemy Appear"),
        (336, "Enemy Transform"), (337, "Show Battle Animation"),
        (339, "Force Action"), (340, "Abort Battle"),
        (355, "Script"), (655, "Script Continuation"),
        (356, "Plugin Command (MV)"), (357, "Plugin Command (MZ)"),
        (657, "Plugin Command Annotation"),
    )
)

_CURRENT_EVENT_CODES = {101, 401, 102, 405, 320, 324, 325}
_VERIFIED_EVENT_CODES = set(_CURRENT_EVENT_CODES)
_INTERNAL_STRING_CODES = {108, 408, 118, 119}
_SCRIPT_CODES = {355, 655}
_ASSET_STRING_CODES = {
    101, 132, 133, 139, 140, 231, 241, 245, 249, 250, 261, 283, 284,
    322, 323,
}
_DISPLAY_API = re.compile(
    r"(?:\$gameMessage\s*\.\s*add|\.\s*drawText(?:Ex)?\s*\(|"
    r"Window_[A-Za-z0-9_]+|\.\s*addText\s*\()"
)
_REGISTER_COMMAND = re.compile(
    r"PluginManager\s*\.\s*registerCommand\s*\(\s*([^,]+?)\s*,\s*"
    r"(['\"])(.*?)\2",
    re.DOTALL,
)
_ANNOTATION = re.compile(r"^\s*\*?\s*@(?P<tag>command|arg|type)\s+(?P<value>.+?)\s*$", re.MULTILINE)
_TEXT_ARG_HINT = re.compile(r"(?:text|message|label|caption|description|help|log)", re.I)


@dataclass(frozen=True, slots=True)
class _CommandLocation:
    file: str
    context: str
    json_prefix: str
    index: int


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()


def _safe_identifier(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if len(value) <= 128 and re.fullmatch(r"[A-Za-z0-9_.:$-]+", value):
        return value
    return f"sha256:{_hash_text(value)}"


def _safe_path_key(value: Any) -> str:
    text = str(value)
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,127}", text):
        return text
    return f"sha256_{_hash_text(text)}"


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_linklike(path: Path) -> bool:
    return path.is_symlink() or (
        hasattr(path, "is_junction") and path.is_junction()
    )


def _iter_strings(value: Any, *, path: str = "", depth: int = 0) -> Iterator[tuple[str, str]]:
    if depth > MAX_RECURSION_DEPTH:
        raise ValueError(f"nested value exceeds depth limit {MAX_RECURSION_DEPTH}")
    if isinstance(value, str):
        if len(value) > MAX_CANDIDATE_STRING_LENGTH:
            raise ValueError(
                f"string exceeds length limit {MAX_CANDIDATE_STRING_LENGTH}"
            )
        yield path, value
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _iter_strings(item, path=f"{path}[{index}]", depth=depth + 1)
    elif isinstance(value, dict):
        for key, item in value.items():
            key_text = _safe_path_key(key)
            yield from _iter_strings(item, path=f"{path}.{key_text}", depth=depth + 1)


def _candidate(
    value: str,
    location: _CommandLocation,
    command_code: int,
    parameter_path: str,
    classification: Class,
    role: str,
    evidence: str,
    **extra: Any,
) -> StringCandidate:
    if len(value) > MAX_CANDIDATE_STRING_LENGTH:
        raise ValueError(
            f"candidate string exceeds length limit {MAX_CANDIDATE_STRING_LENGTH}"
        )
    scripts = detect_japanese_scripts(value)
    return StringCandidate(
        file=location.file,
        json_path=f"{location.json_prefix}[{location.index}].parameters{parameter_path}",
        event_context=location.context,
        command_index=location.index,
        command_code=command_code,
        parameter_path=parameter_path,
        classification=classification.value,
        role=role,
        evidence=evidence,
        value_sha256=_hash_text(value),
        value_length=len(value),
        hiragana=scripts.hiragana,
        katakana=scripts.katakana,
        cjk_kanji=scripts.cjk_kanji,
        control_codes=find_control_codes(value),
        **extra,
    )


class RpgMakerCoverageAuditor:
    """Inventory standard and extension text surfaces without changing source."""

    def __init__(
        self,
        engine: EngineId,
        *,
        plugin_config_file: Path | None = None,
        plugin_source_directory: Path | None = None,
        data_directory: Path | None = None,
        sample_name: str | None = None,
    ) -> None:
        if engine not in {EngineId.RPGMAKER_MV, EngineId.RPGMAKER_MZ}:
            raise ValueError(f"unsupported engine: {engine}")
        self.engine = engine
        self._mv_plugin_prefix_counts: Counter[tuple[str, str, str]] = Counter()
        self._plugin_config_file = plugin_config_file
        self._plugin_source_directory = plugin_source_directory
        self._data_directory = data_directory
        self._sample_name = sample_name
        self._discovery_by_command: dict[str, MvPluginDiscovery] = {}

    def audit(self, game_directory: Path) -> RpgMakerCoverageReport:
        game_directory = game_directory.resolve()
        self._mv_plugin_prefix_counts.clear()
        before = _snapshot(game_directory, self._data_directory)
        report = RpgMakerCoverageReport(
            tool_version=TOOL_VERSION,
            report_schema_version=REPORT_SCHEMA_VERSION,
            engine=self.engine.value,
            data_path="data",
            plugin_metadata_available=(
                self._plugin_config_file is not None
                and self._plugin_config_file.is_file()
            ) or (game_directory / "js/plugins.js").is_file(),
            source_before=before,
        )
        config = self._plugin_config_file
        sources = self._plugin_source_directory
        if config is None:
            default = game_directory / "js/plugins.js"
            config = default if default.is_file() else None
        if sources is None:
            sources = game_directory / "js/plugins"
        active_inventory: ActivePluginInventory | None = None
        discovery = None
        if (config is not None and config.is_file()) or sources.is_dir():
            try:
                active_inventory = load_plugin_inventory(config, sources)
            except (OSError, UnicodeError, ValueError) as exc:
                report.issues.append(
                    AuditIssue("error", "PLUGIN_INVENTORY_ERROR", "js/plugins.js", str(exc))
                )
        if active_inventory is not None:
            report.issues.extend(
                AuditIssue(
                    item.severity,
                    item.code,
                    item.source_file or "js/plugins.js",
                    item.reason,
                )
                for item in active_inventory.issues
                if not (
                    item.code == "PLUGIN_REGISTRY_MISSING"
                    and self.engine == EngineId.RPGMAKER_MZ
                    and active_inventory.plugin_count == 0
                )
            )
        if self.engine == EngineId.RPGMAKER_MV:
            if active_inventory is not None:
                discovery = discover_mv_plugin_commands(
                    config, sources, inventory=active_inventory
                )
                report.plugin_discovery = discovery.to_json_dict()
                existing = {
                    (
                        item.code,
                        Path(item.file).name if item.file else "",
                        item.reason,
                    )
                    for item in report.issues
                }
                for item in discovery.issues:
                    key = (
                        item.code,
                        Path(item.plugin_file).name if item.plugin_file else "",
                        item.reason,
                    )
                    if key not in existing:
                        report.issues.append(
                            AuditIssue(
                                item.severity,
                                item.code,
                                item.plugin_file or "",
                                item.reason,
                            )
                        )
                        existing.add(key)
                if self._sample_name is not None:
                    report.plugin_discovery["sample"] = self._sample_name
                unique: dict[str, list[MvPluginDiscovery]] = defaultdict(list)
                for item in discovery.observations:
                    unique[item.command].append(item)
                self._discovery_by_command = {
                    command: items[0] for command, items in unique.items()
                    if len(items) == 1
                }
            else:
                self._discovery_by_command = {}
        if active_inventory is not None:
            visibility = analyze_plugin_visibility(
                active_inventory,
                command_discovery=discovery,
            )
            report.plugin_visibility = visibility.to_json_dict()
            existing = {
                (
                    item.code,
                    Path(item.file).name if item.file else "",
                    item.reason,
                )
                for item in report.issues
            }
            for item in visibility.issues:
                key = (
                    item.code,
                    Path(item.source_file).name if item.source_file else "",
                    item.reason,
                )
                if key not in existing:
                    report.issues.append(
                        AuditIssue(
                            item.severity,
                            item.code,
                            item.source_file or "",
                            item.reason,
                        )
                    )
                    existing.add(key)
        counters: dict[int, Counter[str]] = defaultdict(Counter)
        move_counters: dict[int, Counter[str]] = defaultdict(Counter)
        file_code_counts: dict[str, Counter[int]] = defaultdict(Counter)
        scanned: list[str] = []
        command_total = 0

        for path in _data_files(game_directory, self._data_directory):
            relative = path.relative_to(game_directory).as_posix()
            scanned.append(relative)
            try:
                document = _load_bounded_json(path)
            except (OSError, UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
                report.issues.append(AuditIssue("error", "DATA_READ_ERROR", relative, str(exc)))
                continue
            for commands, context, prefix in _event_lists(path.name, document):
                for index, command in enumerate(commands):
                    command_total += 1
                    if command_total > MAX_EVENT_COMMANDS:
                        raise ValueError(f"event command count exceeds limit {MAX_EVENT_COMMANDS}")
                    if isinstance(command, dict) and isinstance(command.get("code"), int):
                        file_code_counts[relative][command["code"]] += 1
                    self._observe_command(
                        report,
                        counters,
                        move_counters,
                        command,
                        _CommandLocation(relative, context, prefix, index),
                        commands,
                    )

        report.plugins = _plugin_inventory(
            game_directory, report.issues, inventory=active_inventory
        )
        report.database_fields = _database_inventory(game_directory, report.issues, self._data_directory)
        report.event_commands = _event_inventory(counters)
        report.move_route_commands = _move_inventory(move_counters)
        report.actual_files_scanned = tuple(scanned)
        report.source_after = _snapshot(game_directory, self._data_directory)
        if not report.source_unchanged:
            report.issues.append(
                AuditIssue("error", "SOURCE_CHANGED_DURING_AUDIT", "", "source snapshot differs after audit")
            )
        self._finish_statistics(report, command_total, file_code_counts)
        return report

    def _observe_command(
        self,
        report: RpgMakerCoverageReport,
        counters: dict[int, Counter[str]],
        move_counters: dict[int, Counter[str]],
        command: Any,
        location: _CommandLocation,
        command_list: list[Any],
    ) -> None:
        if not isinstance(command, dict) or not isinstance(command.get("code"), int):
            report.issues.append(AuditIssue("warning", "MALFORMED_COMMAND", location.file, location.json_prefix))
            return
        code = command["code"]
        params = command.get("parameters", [])
        counters[code]["occurrences"] += 1
        if not isinstance(params, list):
            counters[code]["malformed"] += 1
            report.issues.append(AuditIssue("warning", "MALFORMED_PARAMETERS", location.file, f"command {location.index} code {code}"))
            return
        counters[code][f"shape:{_value_shape(params)}"] += 1
        try:
            strings = list(_iter_strings(params))
            if strings:
                counters[code]["string_occurrences"] += 1
            if code == 101 and len(params) > 4 and _text(params[4]):
                if params and _text(params[0]):
                    counters[code][Class.INTERNAL.value] += 1
                self._add(report, counters, code, _candidate(params[4], location, code, "[4]", Class.VERIFIED_TRANSLATABLE, "speaker", "stock MZ name box; current extractor"), current=True)
            elif code in {401, 405} and params and _text(params[0]):
                role = "dialogue" if code == 401 else "scroll_text"
                self._add(report, counters, code, _candidate(params[0], location, code, "[0]", Class.VERIFIED_TRANSLATABLE, role, "stock message runtime; current extractor"), current=True)
            elif code == 102 and params and isinstance(params[0], list):
                for choice_index, value in enumerate(params[0]):
                    if _text(value):
                        self._add(report, counters, code, _candidate(value, location, code, f"[0][{choice_index}]", Class.VERIFIED_TRANSLATABLE, "choice", "stock choice window; current extractor"), current=True)
            elif code == 402:
                self._observe_choice_mirror(report, counters, params, location, command_list)
            elif code in {320, 324, 325} and len(params) > 1 and _text(params[1]):
                role = {320: "actor_name", 324: "actor_nickname", 325: "actor_profile"}[code]
                self._add(
                    report,
                    counters,
                    code,
                    _candidate(
                        params[1],
                        location,
                        code,
                        "[1]",
                        Class.VERIFIED_TRANSLATABLE,
                        role,
                        "stock runtime stores value displayed by actor UI; current extractor",
                    ),
                    current=True,
                )
            elif code in _INTERNAL_STRING_CODES:
                counters[code][Class.INTERNAL.value] += len(strings)
            elif code == 122 and len(params) > 4 and params[3] == 4 and _text(params[4]):
                self._add(report, counters, code, _candidate(params[4], location, code, "[4]", Class.UNSAFE, "script_operand", "evaluated JavaScript operand; never generic translation"))
            elif code in _SCRIPT_CODES and params and _text(params[0]):
                script = _script_block_containing(command_list, location.index)
                display = _DISPLAY_API.search(script)
                classification = Class.CONDITIONAL_TRANSLATABLE if display else Class.UNSAFE
                evidence = "display API pattern; candidate detection only" if display else "evaluated JavaScript; semantics unknown"
                self._add(report, counters, code, _candidate(params[0], location, code, "[0]", classification, "script", evidence, display_api=display.group(0) if display else None))
            elif code == 356 and params and _text(params[0]):
                match = classify_mv_command(params[0])
                discovery = next(
                    (
                        item for item in self._discovery_by_command.values()
                        if item.matches(match.prefix)
                    ),
                    None,
                )
                effective = _effective_mv_classification(match.classification, discovery)
                safe_prefix = _safe_identifier(match.prefix) or "<unknown>"
                self._mv_plugin_prefix_counts[
                    (safe_prefix, match.rule_id or (discovery.classification if discovery else "<none>"), effective)
                ] += 1
                if effective == Class.INTERNAL.value:
                    counters[code][Class.INTERNAL.value] += 1
                else:
                    classification = Class(effective)
                    evidence = (
                        f"verified rule {match.rule_id}; payload only"
                        if match.classification == PLUGIN_VERIFIED
                        else (
                            f"source flow {discovery.classification}: {discovery.sink or discovery.unresolved_reason}"
                            if discovery is not None
                            else "text-like payload; plugin-specific rule required"
                        )
                    )
                    value = _discovery_payload_value(params[0], match.payload, discovery)
                    self._add(
                        report,
                        counters,
                        code,
                        _candidate(
                            value,
                            location,
                            code,
                            "[0]",
                            classification,
                            "mv_plugin_payload",
                            evidence,
                            command_name=_safe_identifier(match.prefix),
                            argument_path="payload",
                            rule_id=match.rule_id or ("mv_source_discovery_v1" if discovery else None),
                            plugin_name=_safe_identifier(discovery.plugin_name) if discovery else None,
                            plugin_file=discovery.plugin_file if discovery else None,
                            handler_evidence=discovery.handler_kind if discovery else None,
                            consumed_arguments=discovery.consumed_arguments if discovery else (),
                            argument_mode=discovery.argument_mode if discovery else None,
                            helper_chain=discovery.helper_chain if discovery else (),
                            sink=discovery.sink if discovery else None,
                            confidence=discovery.confidence if discovery else None,
                            space_policy=discovery.space_policy if discovery else None,
                            unresolved_reason=discovery.unresolved_reason if discovery else None,
                        ),
                        current=(
                            match.classification == PLUGIN_VERIFIED
                            or (discovery is not None and discovery.classification == APPLY_VERIFIED)
                        ),
                    )
            elif code == 357:
                plugin = _safe_identifier(params[0]) if len(params) > 0 else None
                command_name = _safe_identifier(params[1]) if len(params) > 1 else None
                counters[code][Class.INTERNAL.value] += sum(
                    isinstance(params[index], str) and bool(params[index])
                    for index in range(min(3, len(params)))
                )
                if len(params) > 3:
                    for match in iter_mz_argument_texts(plugin or "", command_name or "", params[3]):
                        classification = Class(match.classification)
                        evidence = (
                            f"verified rule {match.rule_id}; selected argument only"
                            if match.classification == PLUGIN_VERIFIED
                            else "text-like structured argument; plugin-specific rule required"
                        )
                        self._add(
                            report,
                            counters,
                            code,
                            _candidate(
                                match.value,
                                location,
                                code,
                                f"[3].{match.path}",
                                classification,
                                "mz_plugin_argument",
                                evidence,
                                plugin_name=plugin,
                                command_name=command_name,
                                argument_path=f".{match.path}",
                                rule_id=match.rule_id,
                            ),
                            current=match.classification == PLUGIN_VERIFIED,
                        )
            elif code == 657 and params and _text(params[0]):
                self._observe_plugin_annotation(report, counters, params[0], location, command_list)
            elif code == 205:
                _observe_move_route(report, move_counters, params, location)
            elif strings:
                # Strings in stock commands are mostly filenames, identifiers,
                # enum values, or opaque plugin data. They are counted but not
                # emitted as translation candidates.
                classification = Class.INTERNAL if code in _ASSET_STRING_CODES or code in _EVENT_NAMES else Class.UNKNOWN
                if classification is Class.UNKNOWN:
                    for parameter_path, value in strings:
                        if _text(value):
                            self._add(report, counters, code, _candidate(value, location, code, parameter_path, Class.UNKNOWN, "unknown_command_string", "unrecognized event command; human review required"))
                else:
                    counters[code][classification.value] += len(strings)
        except ValueError as exc:
            counters[code]["limit_errors"] += 1
            report.issues.append(AuditIssue("error", "BOUNDED_VALUE_REJECTED", location.file, str(exc)))

    @staticmethod
    def _add(report: RpgMakerCoverageReport, counters: dict[int, Counter[str]], code: int, item: StringCandidate, *, current: bool = False) -> None:
        report.candidates.append(item)
        counters[code][item.classification] += 1
        if current:
            counters[code]["current_extract"] += 1

    def _observe_choice_mirror(self, report: RpgMakerCoverageReport, counters: dict[int, Counter[str]], params: list[Any], location: _CommandLocation, commands: list[Any]) -> None:
        choice_index = params[0] if params and isinstance(params[0], int) else None
        value = params[1] if len(params) > 1 and isinstance(params[1], str) else None
        source_index: int | None = None
        source_value: str | None = None
        current = commands[location.index]
        current_indent = current.get("indent") if isinstance(current, dict) else None
        for index in range(location.index - 1, -1, -1):
            previous = commands[index]
            if (
                isinstance(previous, dict)
                and previous.get("code") == 102
                and (current_indent is None or previous.get("indent") == current_indent)
            ):
                previous_parameters = previous.get("parameters", [])
                choices = (
                    previous_parameters[0]
                    if isinstance(previous_parameters, list) and previous_parameters
                    else []
                )
                if isinstance(choices, list) and choice_index is not None and 0 <= choice_index < len(choices):
                    source_index = index
                    source_value = choices[choice_index] if isinstance(choices[choice_index], str) else None
                break
        report.mirrors.append(MirrorObservation(location.file, location.context, source_index, location.index, 102, 402, choice_index, "editor branch label; runtime branches by numeric index", "CONFIRMED", None if value is None or source_value is None else value == source_value))
        if _text(value):
            self._add(report, counters, 402, _candidate(value, location, 402, "[1]", Class.MIRROR, "choice_branch_mirror", "stock runtime ignores label and uses parameter[0] index"))

    def _observe_plugin_annotation(self, report: RpgMakerCoverageReport, counters: dict[int, Counter[str]], value: str, location: _CommandLocation, commands: list[Any]) -> None:
        source_index = location.index - 1
        while source_index >= 0:
            previous = commands[source_index]
            previous_code = previous.get("code") if isinstance(previous, dict) else None
            if previous_code == 357:
                break
            if previous_code != 657:
                source_index = -1
                break
            source_index -= 1
        related = source_index >= 0
        values_match: bool | None = None
        parsed = parse_editor_annotation(value)
        if related and parsed is not None:
            source = commands[source_index]
            source_params = source.get("parameters", [])
            args = source_params[3] if len(source_params) > 3 else None
            if isinstance(args, dict) and isinstance(args.get(parsed[0]), str):
                values_match = args[parsed[0]] == parsed[1]
        relation = "matched editor mirror" if values_match is True else (
            "mismatched editor mirror" if values_match is False else
            ("unresolved editor annotation" if related else "standalone editor annotation")
        )
        report.mirrors.append(MirrorObservation(location.file, location.context, source_index if related else None, location.index, 357, 657, None, relation, "CONFIRMED" if related else "INFERRED", values_match))
        classification = Class.MIRROR if related else Class.INTERNAL
        self._add(report, counters, 657, _candidate(value, location, 657, "[0]", classification, "plugin_command_annotation", "editor annotation only; never an independent translation entry"))

    def _finish_statistics(
        self,
        report: RpgMakerCoverageReport,
        command_total: int,
        file_code_counts: dict[str, Counter[int]],
    ) -> None:
        class_counts = Counter(item.classification for item in report.candidates)
        represented_internal = Counter(
            item.command_code
            for item in report.candidates
            if item.classification == Class.INTERNAL.value
        )
        for row in report.event_commands:
            class_counts[Class.INTERNAL.value] += max(
                0,
                row.get("internal_strings", 0) - represented_internal[row["code"]],
            )
        current = sum(row["current_extracted_entries"] for row in report.event_commands)
        verified = class_counts[Class.VERIFIED_TRANSLATABLE.value]
        report.statistics = {
            "total_event_commands": command_total,
            "unique_command_codes_observed": sum(row["occurrences"] > 0 for row in report.event_commands),
            "standard_command_codes_catalogued": len(_EVENT_NAMES),
            "string_bearing_command_occurrences": sum(row["string_occurrences"] for row in report.event_commands),
            "classification_counts": {item.value: class_counts[item.value] for item in Class},
            "coverage": {
                "denominator_definition": "observed entries classified VERIFIED_TRANSLATABLE; UNKNOWN and conditional/plugin/script candidates excluded",
                "known_verified_player_visible_entries": verified,
                "currently_extracted": current,
                "known_missed_verified_candidates": max(0, verified - current),
                "percentage": round(current * 100 / verified, 2) if verified else 100.0,
            },
            "database_coverage": _database_coverage(report.database_fields),
            "plugin_candidates": sum(item.command_code in {356, 357} for item in report.candidates),
            "plugin_command_coverage": _plugin_command_statistics(
                report,
                self._mv_plugin_prefix_counts,
                self._discovery_by_command,
            ),
            "plugin_visibility": {
                key: value
                for key, value in report.plugin_visibility.items()
                if key not in {"findings", "issues"}
            },
            "script_candidates": sum(item.command_code in {355, 655, 122} for item in report.candidates),
            "mirror_mismatches": sum(item.values_match is False for item in report.mirrors),
            "errors": sum(item.severity == "error" for item in report.issues),
            "warnings": sum(item.severity == "warning" for item in report.issues),
            "event_occurrences_by_file": {
                file: {str(code): count for code, count in sorted(counts.items())}
                for file, counts in sorted(file_code_counts.items())
            },
        }


def _data_files(game: Path, data_directory: Path | None = None) -> list[Path]:
    data = data_directory.resolve() if data_directory is not None else game / "data"
    if not data.is_dir():
        raise ValueError("data directory does not exist")
    if _is_linklike(data):
        raise ValueError("data directory cannot be a symlink or junction")
    allowed = {
        "Actors.json", "Classes.json", "Skills.json", "Items.json",
        "Weapons.json", "Armors.json", "Enemies.json", "Troops.json",
        "States.json", "Animations.json", "Tilesets.json", "CommonEvents.json",
        "System.json", "MapInfos.json",
    }
    paths: list[Path] = []
    for path in data.iterdir():
        relevant = path.name in allowed or re.fullmatch(
            r"Map\d+\.json", path.name, re.I
        )
        if not relevant:
            continue
        if _is_linklike(path):
            raise ValueError(f"data source cannot be a symlink or junction: {path.name}")
        if path.is_file():
            paths.append(path)
    if len(paths) > MAX_FILES:
        raise ValueError(f"data file count exceeds limit {MAX_FILES}")
    return sorted(paths, key=lambda item: item.name.casefold())


def _load_bounded_json(path: Path) -> Any:
    size = path.stat().st_size
    if size > MAX_JSON_FILE_BYTES:
        raise ValueError(f"JSON file exceeds size limit {MAX_JSON_FILE_BYTES}: {size}")
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    _check_depth(document)
    return document


def _check_depth(value: Any, depth: int = 0) -> None:
    if depth > MAX_RECURSION_DEPTH:
        raise ValueError(f"JSON structure exceeds depth limit {MAX_RECURSION_DEPTH}")
    if isinstance(value, dict):
        for item in value.values():
            _check_depth(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _check_depth(item, depth + 1)
    elif isinstance(value, str) and len(value) > MAX_CANDIDATE_STRING_LENGTH:
        raise ValueError(f"string exceeds length limit {MAX_CANDIDATE_STRING_LENGTH}")


def _event_lists(file_name: str, document: Any) -> Iterator[tuple[list[Any], str, str]]:
    if re.fullmatch(r"Map\d+\.json", file_name, re.I) and isinstance(document, dict):
        events = document.get("events", [])
        if isinstance(events, list):
            for event_index, event in enumerate(events):
                if not isinstance(event, dict):
                    continue
                event_id = event.get("id", event_index)
                pages = event.get("pages", [])
                if isinstance(pages, list):
                    for page_index, page in enumerate(pages):
                        if isinstance(page, dict) and isinstance(page.get("list"), list):
                            yield page["list"], f"event:{event_id}:page:{page_index + 1}", f"$.events[{event_index}].pages[{page_index}].list"
    elif file_name == "CommonEvents.json" and isinstance(document, list):
        for index, event in enumerate(document):
            if isinstance(event, dict) and isinstance(event.get("list"), list):
                event_id = event.get("id", index)
                yield event["list"], f"common_event:{event_id}", f"$[{index}].list"
    elif file_name == "Troops.json" and isinstance(document, list):
        for troop_index, troop in enumerate(document):
            if not isinstance(troop, dict):
                continue
            troop_id = troop.get("id", troop_index)
            pages = troop.get("pages", [])
            if isinstance(pages, list):
                for page_index, page in enumerate(pages):
                    if isinstance(page, dict) and isinstance(page.get("list"), list):
                        yield page["list"], f"troop:{troop_id}:page:{page_index + 1}", f"$[{troop_index}].pages[{page_index}].list"


def _script_block_containing(commands: list[Any], index: int) -> str:
    current = commands[index] if 0 <= index < len(commands) else None
    while isinstance(current, dict) and current.get("code") == 655 and index > 0:
        previous = commands[index - 1]
        if not isinstance(previous, dict) or previous.get("code") not in {355, 655}:
            break
        index -= 1
        current = previous
    lines: list[str] = []
    while index < len(commands):
        item = commands[index]
        if not isinstance(item, dict) or item.get("code") not in ({355} if not lines else {655}):
            break
        params = item.get("parameters", [])
        if params and _text(params[0]):
            lines.append(params[0])
        index += 1
    return "\n".join(lines)


def _value_shape(value: Any, depth: int = 0) -> str:
    if depth >= 3:
        return type(value).__name__
    if isinstance(value, list):
        return "[" + ",".join(_value_shape(item, depth + 1) for item in value[:8]) + (",..." if len(value) > 8 else "") + "]"
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda item: str(item[0]))[:8]
        return "{" + ",".join(f"{_safe_path_key(key)}:{_value_shape(item, depth + 1)}" for key, item in items) + (",..." if len(value) > 8 else "") + "}"
    return {str: "str", int: "int", float: "float", bool: "bool", type(None): "null"}.get(type(value), type(value).__name__)


def _plugin_command_statistics(
    report: RpgMakerCoverageReport,
    mv_prefix_counts: Counter[tuple[str, str, str]],
    discovery_by_command: dict[str, MvPluginDiscovery],
) -> dict[str, Any]:
    mv = Counter(
        item.classification
        for item in report.candidates
        if item.command_code == 356
    )
    mz = Counter(
        (
            item.plugin_name or "<unknown>",
            item.command_name or "<unknown>",
            item.argument_path or "$",
            item.classification,
        )
        for item in report.candidates
        if item.command_code == 357
    )
    mirrors = Counter(
        "matched" if item.values_match is True else
        "mismatched" if item.values_match is False else
        "unknown"
        for item in report.mirrors
        if item.mirror_code == 657
    )
    standalone = sum(
        item.mirror_code == 657 and item.source_command_index is None
        for item in report.mirrors
    )
    mv_row = next((row for row in report.event_commands if row["code"] == 356), None)
    mz_row = next((row for row in report.event_commands if row["code"] == 357), None)
    return {
        "mv_356": {
            "occurrences": mv_row["occurrences"] if mv_row else 0,
            "verified": mv[Class.VERIFIED_TRANSLATABLE.value],
            "conditional": mv[Class.CONDITIONAL_TRANSLATABLE.value],
            "internal": mv_row["internal_strings"] if mv_row else 0,
            "prefixes": [
                _mv_prefix_statistics_row(
                    prefix,
                    rule,
                    classification,
                    count,
                    discovery_by_command,
                )
                for (prefix, rule, classification), count in sorted(
                    mv_prefix_counts.items()
                )
            ],
        },
        "mz_357": {
            "occurrences": mz_row["occurrences"] if mz_row else 0,
            "arguments": [
                {
                    "plugin_name": plugin,
                    "plugin_command": command,
                    "argument_path": path,
                    "classification": classification,
                    "count": count,
                }
                for (plugin, command, path, classification), count in sorted(mz.items())
            ],
            "internal_strings": mz_row["internal_strings"] if mz_row else 0,
        },
        "code_657": {
            "matched": mirrors["matched"],
            "mismatched": mirrors["mismatched"],
            "unknown": mirrors["unknown"],
            "standalone": standalone,
        },
    }


def _mv_prefix_statistics_row(
    prefix: str,
    rule: str,
    classification: str,
    count: int,
    discovery_by_command: dict[str, MvPluginDiscovery],
) -> dict[str, object]:
    discovery = next(
        (
            item for command, item in discovery_by_command.items()
            if (
                (_safe_identifier(command) or "<unknown>") == prefix
                or (
                    item.command_normalization in {"upper", "lower"}
                    and (_safe_identifier(command) or "<unknown>").casefold() == prefix.casefold()
                )
            )
        ),
        None,
    )
    return {
        "prefix": prefix,
        "occurrence_count": count,
        "classification": classification,
        "discovery_classification": discovery.classification if discovery else None,
        "rule_id": None if rule == "<none>" else rule,
        "enabled_plugin": discovery.plugin_name if discovery else None,
        "plugin_file": discovery.plugin_file if discovery else None,
        "handler_evidence": discovery.handler_kind if discovery else None,
        "consumed_arguments": list(discovery.consumed_arguments) if discovery else [],
        "argument_mode": discovery.argument_mode if discovery else None,
        "helper_chain": list(discovery.helper_chain) if discovery else [],
        "sink": discovery.sink if discovery else None,
        "confidence": discovery.confidence if discovery else None,
        "space_policy": discovery.space_policy if discovery else None,
        "unresolved_reason": (
            discovery.unresolved_reason if discovery else
            "no unique enabled-plugin command handler was resolved"
        ),
    }


def _effective_mv_classification(
    fallback: str,
    discovery: MvPluginDiscovery | None,
) -> str:
    if fallback == PLUGIN_VERIFIED:
        return Class.VERIFIED_TRANSLATABLE.value
    if fallback == PLUGIN_INTERNAL:
        return Class.INTERNAL.value
    if discovery is None:
        return fallback
    return {
        APPLY_VERIFIED: Class.VERIFIED_TRANSLATABLE.value,
        DISCOVERED_VERIFIED: Class.CONDITIONAL_TRANSLATABLE.value,
        DISCOVERY_INTERNAL: Class.INTERNAL.value,
        DISCOVERY_UNSAFE: Class.UNSAFE.value,
        DISCOVERY_UNKNOWN: Class.UNKNOWN.value,
    }.get(discovery.classification, fallback)


def _discovery_payload_value(
    raw: str,
    fallback_payload: str,
    discovery: MvPluginDiscovery | None,
) -> str:
    if discovery is None:
        return fallback_payload
    payload = extract_runtime_payload(
        raw,
        discovery.argument_mode,
        discovery.payload_start,
    )
    return payload.payload if payload is not None else fallback_payload


_MOVE_NAMES = {
    0: "End", 1: "Move Down", 2: "Move Left", 3: "Move Right", 4: "Move Up",
    5: "Move Lower Left", 6: "Move Lower Right", 7: "Move Upper Left", 8: "Move Upper Right",
    9: "Move Random", 10: "Move Toward Player", 11: "Move Away From Player", 12: "Move Forward",
    13: "Move Backward", 14: "Jump", 15: "Wait", 16: "Turn Down", 17: "Turn Left",
    18: "Turn Right", 19: "Turn Up", 20: "Turn 90 Right", 21: "Turn 90 Left",
    22: "Turn 180", 23: "Turn 90 Random", 24: "Turn Random", 25: "Turn Toward Player",
    26: "Turn Away From Player", 27: "Switch On", 28: "Switch Off", 29: "Change Speed",
    30: "Change Frequency", 31: "Walking Animation On", 32: "Walking Animation Off",
    33: "Stepping Animation On", 34: "Stepping Animation Off", 35: "Direction Fix On",
    36: "Direction Fix Off", 37: "Through On", 38: "Through Off", 39: "Transparent On",
    40: "Transparent Off", 41: "Change Image", 42: "Change Opacity", 43: "Change Blend Mode",
    44: "Play SE", 45: "Script",
}


def _observe_move_route(report: RpgMakerCoverageReport, counters: dict[int, Counter[str]], params: list[Any], location: _CommandLocation) -> None:
    if len(params) < 2 or not isinstance(params[1], dict):
        return
    route = params[1].get("list", [])
    if not isinstance(route, list):
        return
    observed = sum(item["occurrences"] for item in counters.values())
    if observed + len(route) > MAX_EVENT_COMMANDS:
        raise ValueError(f"move-route command count exceeds limit {MAX_EVENT_COMMANDS}")
    for route_index, command in enumerate(route):
        if not isinstance(command, dict) or not isinstance(command.get("code"), int):
            continue
        code = command["code"]
        route_params = command.get("parameters", [])
        counters[code]["occurrences"] += 1
        strings = list(_iter_strings(route_params)) if isinstance(route_params, list) else []
        if strings:
            counters[code]["string_occurrences"] += 1
        if code in {41, 44}:
            counters[code][Class.INTERNAL.value] += len(strings)
        elif code == 45 and route_params and isinstance(route_params[0], str):
            counters[code][Class.UNSAFE.value] += 1
            route_location = _CommandLocation(location.file, location.context, f"{location.json_prefix}[{location.index}].parameters[1].list", route_index)
            report.candidates.append(
                _candidate(
                    route_params[0],
                    route_location,
                    205,
                    "[0]",
                    Class.UNSAFE,
                    "move_route_script",
                    "move-route code 45 evaluates JavaScript",
                    argument_path="move_route_code:45",
                )
            )
        elif strings:
            counters[code][Class.UNKNOWN.value] += len(strings)


def _event_inventory(counters: dict[int, Counter[str]]) -> list[dict[str, Any]]:
    codes = sorted(set(_EVENT_NAMES) | set(counters))
    rows = []
    for code in codes:
        count = counters[code]
        if code in _VERIFIED_EVENT_CODES:
            classification = Class.VERIFIED_TRANSLATABLE.value
        elif code in {402, 657}:
            classification = Class.MIRROR.value
        elif code in {356, 357}:
            classification = Class.CONDITIONAL_TRANSLATABLE.value
        elif code in {122, 355, 655}:
            classification = Class.UNSAFE.value
        elif code in _EVENT_NAMES:
            classification = Class.INTERNAL.value
        else:
            classification = Class.UNKNOWN.value
        rows.append({
            "code": code,
            "name": _EVENT_NAMES.get(code, "Unknown"),
            "mv_support": code not in {357, 657},
            "mz_support": True,
            "parameter_structure": _parameter_summary(code),
            "runtime_purpose": _runtime_purpose(code),
            "classification": classification,
            "evidence_grade": "C" if code in _EVENT_NAMES else "UNKNOWN",
            "occurrences": count["occurrences"],
            "string_occurrences": count["string_occurrences"],
            "current_extracted_entries": count["current_extract"],
            "verified_entries": count[Class.VERIFIED_TRANSLATABLE.value],
            "conditional_entries": count[Class.CONDITIONAL_TRANSLATABLE.value],
            "mirror_entries": count[Class.MIRROR.value],
            "internal_strings": count[Class.INTERNAL.value],
            "unsafe_strings": count[Class.UNSAFE.value],
            "unknown_strings": count[Class.UNKNOWN.value],
            "malformed": count["malformed"],
            "observed_parameter_shapes": [
                {"shape": key[6:], "occurrences": value}
                for key, value in sorted(count.items())
                if key.startswith("shape:")
            ],
        })
    return rows


def _parameter_summary(code: int) -> str:
    return {
        101: "[faceName, faceIndex, background, position, name?]",
        401: "[text]", 102: "[[choices], cancel, default, position, background]",
        402: "[choiceIndex, editorLabel]", 405: "[text]",
        108: "[comment]", 408: "[commentLine]", 118: "[label]", 119: "[label]",
        122: "[startId, endId, operation, operandType, operand...]",
        205: "[characterId, moveRoute]", 320: "[actorId, name]",
        324: "[actorId, nickname]", 325: "[actorId, profile]",
        355: "[scriptLine]", 655: "[scriptLine]", 356: "[rawCommand]",
        357: "[pluginName, commandName, editorName, argsObject]", 657: "[editorAnnotation]",
    }.get(code, "stock command parameters; see official event-code reference")


def _runtime_purpose(code: int) -> str:
    if code in {101, 401, 102, 405, 320, 324, 325}:
        return "player-visible standard text"
    if code in {402, 657}:
        return "editor mirror/annotation"
    if code in {355, 655, 122}:
        return "evaluated script or script-bearing variant"
    if code in {356, 357}:
        return "plugin-defined behavior"
    if code in _ASSET_STRING_CODES:
        return "asset/configuration reference"
    return "stock control/data command"


def _move_inventory(counters: dict[int, Counter[str]]) -> list[dict[str, Any]]:
    rows = []
    for code in sorted(set(_MOVE_NAMES) | set(counters)):
        count = counters[code]
        classification = Class.UNSAFE if code == 45 else Class.INTERNAL if code in _MOVE_NAMES else Class.UNKNOWN
        role = "script" if code == 45 else "asset_reference" if code in {41, 44} else "route_control"
        rows.append({"code": code, "name": _MOVE_NAMES.get(code, "Unknown"), "role": role, "classification": classification.value, "occurrences": count["occurrences"], "string_occurrences": count["string_occurrences"]})
    return rows


_DB_RULES = (
    ("Actors.json", "[*].name", "player_visible", True),
    ("Actors.json", "[*].nickname", "player_visible", True),
    ("Actors.json", "[*].profile", "player_visible", True),
    ("Actors.json", "[*].note", "note_script", False),
    ("Classes.json", "[*].name", "player_visible", True),
    ("Classes.json", "[*].note", "note_script", False),
    ("Skills.json", "[*].name", "player_visible", True),
    ("Skills.json", "[*].description", "player_visible", True),
    ("Skills.json", "[*].message1", "player_visible", True),
    ("Skills.json", "[*].message2", "player_visible", True),
    ("Items.json", "[*].name", "player_visible", True),
    ("Items.json", "[*].description", "player_visible", True),
    ("Weapons.json", "[*].name", "player_visible", True),
    ("Weapons.json", "[*].description", "player_visible", True),
    ("Armors.json", "[*].name", "player_visible", True),
    ("Armors.json", "[*].description", "player_visible", True),
    ("Enemies.json", "[*].name", "player_visible", True),
    ("Enemies.json", "[*].battlerName", "asset_reference", False),
    ("Troops.json", "[*].name", "editor_internal", False),
    ("States.json", "[*].name", "player_visible", True),
    ("States.json", "[*].message1", "player_visible", True),
    ("States.json", "[*].message2", "player_visible", True),
    ("States.json", "[*].message3", "player_visible", True),
    ("States.json", "[*].message4", "player_visible", True),
    ("Animations.json", "[*].name", "editor_internal", False),
    ("Tilesets.json", "[*].name", "editor_internal", False),
    ("Tilesets.json", "[*].tilesetNames", "asset_reference", False),
    ("CommonEvents.json", "[*].name", "editor_internal", False),
    ("System.json", "$.gameTitle", "player_visible", True),
    ("System.json", "$.currencyUnit", "player_visible", True),
    ("System.json", "$.elements[*]", "player_visible", True),
    ("System.json", "$.skillTypes[*]", "player_visible", True),
    ("System.json", "$.weaponTypes[*]", "player_visible", True),
    ("System.json", "$.armorTypes[*]", "player_visible", True),
    ("System.json", "$.equipTypes[*]", "player_visible", True),
    ("System.json", "$.terms.*", "player_visible", True),
    ("System.json", "$.switches[*]", "internal_identifier", False),
    ("System.json", "$.variables[*]", "internal_identifier", False),
    ("MapInfos.json", "[*].name", "editor_internal", False),
    ("MapXXX.json", "$.displayName", "player_visible", True),
    ("MapXXX.json", "$.events[*].name", "editor_internal", False),
)


def _database_inventory(
    game: Path,
    issues: list[AuditIssue],
    data_directory: Path | None = None,
) -> list[DatabaseFieldObservation]:
    documents: dict[str, Any] = {}
    for path in _data_files(game, data_directory):
        try:
            documents[path.name] = _load_bounded_json(path)
        except Exception:
            continue  # already reported during event scan
    rows: list[DatabaseFieldObservation] = []
    for file_name, path_pattern, role, current in _DB_RULES:
        matched_docs = []
        if file_name == "MapXXX.json":
            matched_docs = [doc for name, doc in documents.items() if re.fullmatch(r"Map\d+\.json", name, re.I)]
        elif file_name in documents:
            matched_docs = [documents[file_name]]
        count = sum(_count_db_values(doc, path_pattern) for doc in matched_docs)
        classification = Class.VERIFIED_TRANSLATABLE if role == "player_visible" else Class.UNSAFE if role == "note_script" else Class.INTERNAL
        rows.append(DatabaseFieldObservation(file_name, path_pattern, role, classification.value, current, count, "stock database schema and current extractor allowlist"))
    return rows


def _database_coverage(rows: list[DatabaseFieldObservation]) -> dict[str, Any]:
    verified = sum(row.occurrences for row in rows if row.classification == Class.VERIFIED_TRANSLATABLE.value)
    current = sum(row.occurrences for row in rows if row.classification == Class.VERIFIED_TRANSLATABLE.value and row.current_extract)
    return {
        "denominator_definition": "observed standard database values classified VERIFIED_TRANSLATABLE",
        "known_verified_player_visible_entries": verified,
        "currently_extracted": current,
        "known_missed_verified_candidates": max(0, verified - current),
        "percentage": round(current * 100 / verified, 2) if verified else 100.0,
    }


def _count_db_values(document: Any, pattern: str) -> int:
    if pattern.startswith("[*]."):
        field = pattern[4:]
        if not isinstance(document, list):
            return 0
        return sum(isinstance(row, dict) and _nonempty_or_collection(row.get(field)) for row in document)
    if pattern.startswith("$.") and pattern.endswith("[*]"):
        field = pattern[2:-3]
        value = document.get(field) if isinstance(document, dict) else None
        return sum(_text(item) for item in value) if isinstance(value, list) else 0
    if pattern == "$.terms.*":
        terms = document.get("terms") if isinstance(document, dict) else None
        return sum(1 for _, value in _iter_strings(terms or {}) if _text(value))
    if pattern.startswith("$."):
        value = document.get(pattern[2:]) if isinstance(document, dict) else None
        return int(_text(value))
    return 0


def _nonempty_or_collection(value: Any) -> bool:
    return _text(value) or (isinstance(value, list) and bool(value))


def _plugin_inventory(
    game: Path,
    issues: list[AuditIssue],
    *,
    inventory: ActivePluginInventory | None = None,
) -> list[PluginInventory]:
    if inventory is None:
        config = game / "js/plugins.js"
        sources = game / "js/plugins"
        if not config.is_file() or not sources.is_dir():
            return []
        try:
            inventory = load_plugin_inventory(config, sources)
        except (OSError, UnicodeError, ValueError) as exc:
            issues.append(
                AuditIssue("warning", "PLUGIN_METADATA_ERROR", "js/plugins.js", str(exc))
            )
            return []
        for item in inventory.issues:
            issues.append(
                AuditIssue(
                    item.severity,
                    item.code,
                    item.source_file or "js/plugins.js",
                    item.reason,
                )
            )
    result: list[PluginInventory] = []
    for record in inventory.plugins[:MAX_FILES]:
        name = record.name
        registered: set[str] = set()
        declared: set[str] = set()
        text_args: set[str] = set()
        source_relative = record.source_file if record.source_available else None
        if record.source_available and record.source_path is not None:
            try:
                plugin_text = record.source_path.read_text(encoding="utf-8-sig")
                registered.update(match.group(3) for match in _REGISTER_COMMAND.finditer(plugin_text))
                last_arg: str | None = None
                for annotation in _ANNOTATION.finditer(plugin_text):
                    tag, value = annotation.group("tag"), annotation.group("value").strip()
                    if tag == "command":
                        declared.add(value.split()[0])
                    elif tag == "arg":
                        last_arg = value.split()[0]
                        if _TEXT_ARG_HINT.search(last_arg):
                            text_args.add(last_arg)
                    elif tag == "type" and last_arg and value.split()[0] in {"string", "multiline_string", "note"}:
                        text_args.add(last_arg)
            except (OSError, UnicodeError) as exc:
                issues.append(AuditIssue("warning", "PLUGIN_SOURCE_ERROR", source_relative, str(exc)))
        result.append(
            PluginInventory(
                _safe_identifier(name) or "",
                record.enabled is True,
                source_relative,
                tuple(sorted(registered)),
                tuple(sorted(declared)),
                tuple(sorted(text_args)),
            )
        )
    return result


def _snapshot(game: Path, data_directory: Path | None = None) -> SourceSnapshot:
    count = 0
    total = 0
    selected_total = 0
    digest = hashlib.sha256(b"glt-rpgmaker-audit-source-v1\0")
    selected: list[Path] = []
    for root, directories, files in os.walk(game, followlinks=False):
        directories[:] = sorted(
            name for name in directories if not _is_linklike(Path(root) / name)
        )
        for name in sorted(files):
            path = Path(root) / name
            if _is_linklike(path):
                continue
            count += 1
            if count > MAX_FILES:
                raise ValueError(f"game file count exceeds limit {MAX_FILES}")
            size = path.stat().st_size
            total += size
            relative = path.relative_to(game).as_posix()
            flat_data = data_directory is not None and path.parent.resolve() == data_directory.resolve()
            if (
                (relative.startswith("data/") and relative.endswith(".json"))
                or (flat_data and relative.endswith(".json"))
                or relative == "js/plugins.js"
                or (relative.startswith("js/plugins/") and relative.endswith(".js"))
            ):
                selected.append(path)
                selected_total += size
                if selected_total > MAX_SELECTED_SOURCE_BYTES:
                    raise ValueError(
                        "selected audit source bytes exceed limit "
                        f"{MAX_SELECTED_SOURCE_BYTES}"
                    )
    for path in sorted(selected, key=lambda item: item.relative_to(game).as_posix()):
        relative = path.relative_to(game).as_posix()
        digest.update(relative.encode("utf-8")); digest.update(b"\0")
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return SourceSnapshot(count, total, digest.hexdigest())


def write_coverage_report(path: Path, report: RpgMakerCoverageReport) -> Path:
    if path.exists():
        raise FileExistsError(f"RPG Maker audit report already exists: {path}")
    payload = json.dumps(report.to_json_dict(), ensure_ascii=False, indent=2) + "\n"
    _atomic_new_bytes(path, payload.encode("utf-8"))
    return path


def write_candidate_csv(path: Path, report: RpgMakerCoverageReport) -> Path:
    if path.exists():
        raise FileExistsError(f"RPG Maker candidate CSV already exists: {path}")
    stream = io.StringIO(newline="")
    fields = ["file", "json_path", "event_context", "command_index", "command_code", "parameter_path", "classification", "role", "evidence", "value_sha256", "value_length", "hiragana", "katakana", "cjk_kanji", "control_codes", "plugin_name", "command_name", "argument_path", "rule_id", "plugin_file", "handler_evidence", "consumed_arguments", "argument_mode", "helper_chain", "sink", "confidence", "space_policy", "unresolved_reason", "display_api"]
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    for item in report.candidates:
        row = {field: getattr(item, field) for field in fields}
        for field in ("control_codes", "consumed_arguments", "helper_chain"):
            row[field] = json.dumps(getattr(item, field), ensure_ascii=False)
        writer.writerow(row)
    _atomic_new_bytes(path, stream.getvalue().encode("utf-8-sig"))
    return path


def _atomic_new_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", delete=False, dir=path.parent, prefix=f".{path.name}.", suffix=".tmp") as stream:
            temporary = Path(stream.name)
            stream.write(payload); stream.flush(); os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise FileExistsError(f"output already exists: {path}") from exc
        temporary.unlink()
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


__all__ = [
    "MAX_CANDIDATE_STRING_LENGTH", "MAX_JSON_FILE_BYTES", "MAX_RECURSION_DEPTH",
    "REPORT_SCHEMA_VERSION", "RpgMakerCoverageAuditor", "write_candidate_csv",
    "write_coverage_report",
]
