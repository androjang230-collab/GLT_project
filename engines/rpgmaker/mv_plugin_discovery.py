"""Bounded lexical discovery for RPG Maker MV plugin commands.

The analyzer intentionally recognizes a small, auditable JavaScript subset. It
does not execute JavaScript and does not treat a display API elsewhere in the
same file as evidence for a command branch.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Iterator


MAX_PLUGIN_BYTES = 8 * 1024 * 1024
MAX_TOTAL_PLUGIN_BYTES = 512 * 1024 * 1024
MAX_PLUGIN_FILES = 2_000
MAX_HANDLERS = 10_000

APPLY_VERIFIED = "APPLY_VERIFIED"
DISCOVERED_VERIFIED = "DISCOVERED_VERIFIED"
CONDITIONAL = "CONDITIONAL_TRANSLATABLE"
INTERNAL = "INTERNAL"
UNSAFE = "UNSAFE"
UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class MvPluginDiscovery:
    command: str
    plugin_name: str
    plugin_file: str
    load_order: int | None
    enabled: bool | None
    handler_kind: str
    command_normalization: str
    consumed_arguments: tuple[int, ...]
    argument_mode: str
    helper_chain: tuple[str, ...]
    sink: str | None
    sink_kind: str
    classification: str
    confidence: str
    space_policy: str
    payload_start: int | None = None
    unresolved_reason: str | None = None

    def matches(self, prefix: str) -> bool:
        if self.command_normalization == "upper":
            return prefix.upper() == self.command
        if self.command_normalization == "lower":
            return prefix.lower() == self.command
        return prefix == self.command

    def to_json_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DiscoveryIssue:
    severity: str
    code: str
    plugin_name: str | None
    plugin_file: str | None
    reason: str

    def to_json_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class MvPluginDiscoveryReport:
    registry_available: bool
    plugin_count: int = 0
    enabled_plugin_count: int = 0
    source_file_count: int = 0
    handler_count: int = 0
    source_resolved_plugins: int = 0
    source_fingerprint_before: str = ""
    source_fingerprint_after: str = ""
    source_file_bytes: int = 0
    observations: list[MvPluginDiscovery] = field(default_factory=list)
    issues: list[DiscoveryIssue] = field(default_factory=list)

    @property
    def source_unchanged(self) -> bool:
        return self.source_fingerprint_before == self.source_fingerprint_after

    def runtime_rule_for(self, prefix: str) -> MvPluginDiscovery | None:
        matching = [
            item for item in self.observations
            if item.classification == APPLY_VERIFIED and item.matches(prefix)
        ]
        return matching[0] if len(matching) == 1 else None

    def to_json_dict(self) -> dict[str, object]:
        counts = Counter(item.classification for item in self.observations)
        return {
            "registry_available": self.registry_available,
            "plugin_count": self.plugin_count,
            "enabled_plugin_count": self.enabled_plugin_count,
            "source_file_count": self.source_file_count,
            "source_resolved_plugins": self.source_resolved_plugins,
            "plugin_command_handler_count": self.handler_count,
            "classification_counts": dict(sorted(counts.items())),
            "source_file_bytes": self.source_file_bytes,
            "source_fingerprint_before": self.source_fingerprint_before,
            "source_fingerprint_after": self.source_fingerprint_after,
            "source_unchanged": self.source_unchanged,
            "observations": [item.to_json_dict() for item in self.observations],
            "issues": [item.to_json_dict() for item in self.issues],
        }


@dataclass(frozen=True, slots=True)
class _PluginRecord:
    name: str
    enabled: bool | None
    load_order: int | None
    source: Path


@dataclass(frozen=True, slots=True)
class _Function:
    name: str
    parameters: tuple[str, ...]
    body: str
    kind: str


@dataclass(frozen=True, slots=True)
class _Flow:
    mode: str
    indices: tuple[int, ...]
    start: int | None
    numeric: bool = False


def discover_mv_plugin_commands(
    plugin_config_file: Path | None,
    plugin_source_directory: Path,
) -> MvPluginDiscoveryReport:
    """Analyze enabled MV plugin sources without executing or modifying them."""

    if _is_linklike(plugin_source_directory):
        raise ValueError("plugin source directory cannot be a symlink or junction")
    source_directory = plugin_source_directory.resolve()
    config = plugin_config_file.resolve() if plugin_config_file is not None else None
    if plugin_config_file is not None and _is_linklike(plugin_config_file):
        raise ValueError("plugins.js cannot be a symlink or junction")
    registry, registry_issues = _load_registry(config, source_directory)
    report = MvPluginDiscoveryReport(registry_available=config is not None and config.is_file())
    report.issues.extend(registry_issues)
    report.plugin_count = len(registry)
    report.enabled_plugin_count = sum(item.enabled is True for item in registry)

    selected = [item.source for item in registry if item.enabled is not False and item.source.is_file()]
    report.source_file_count = len(selected)
    report.source_file_bytes = sum(path.stat().st_size for path in selected)
    if report.source_file_bytes > MAX_TOTAL_PLUGIN_BYTES:
        raise ValueError(
            f"selected plugin source bytes exceed {MAX_TOTAL_PLUGIN_BYTES}"
        )
    report.source_fingerprint_before = _fingerprint_sources(config, selected, source_directory)

    observations: list[MvPluginDiscovery] = []
    handler_count = 0
    for plugin in registry:
        if plugin.enabled is False:
            continue
        if (
            not plugin.source.is_file()
            or _is_linklike(plugin.source)
            or not plugin.source.resolve().is_relative_to(source_directory)
        ):
            report.issues.append(
                DiscoveryIssue(
                    "warning", "PLUGIN_SOURCE_MISSING", plugin.name, None,
                    "enabled plugin has no exact same-name source file",
                )
            )
            continue
        if plugin.source.stat().st_size > MAX_PLUGIN_BYTES:
            report.issues.append(
                DiscoveryIssue(
                    "warning", "PLUGIN_SOURCE_TOO_LARGE", plugin.name,
                    plugin.source.name, f"source exceeds {MAX_PLUGIN_BYTES} bytes",
                )
            )
            continue
        try:
            source = plugin.source.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            report.issues.append(
                DiscoveryIssue("warning", "PLUGIN_SOURCE_READ_ERROR", plugin.name, plugin.source.name, str(exc))
            )
            continue
        report.source_resolved_plugins += 1
        functions = _extract_functions(_strip_comments(source))
        direct = [item for item in functions if item.name == "Game_Interpreter.prototype.pluginCommand"]
        handler_count += len(direct)
        for handler in direct:
            observations.extend(_analyze_handler(plugin, handler, functions))
        if handler_count > MAX_HANDLERS:
            raise ValueError(f"pluginCommand handler count exceeds {MAX_HANDLERS}")

    report.handler_count = handler_count
    report.observations = _resolve_ambiguities(observations)
    report.source_fingerprint_after = _fingerprint_sources(config, selected, source_directory)
    if not report.source_unchanged:
        report.issues.append(
            DiscoveryIssue("error", "PLUGIN_SOURCE_CHANGED", None, None, "plugin source fingerprint changed during discovery")
        )
    return report


def _load_registry(
    config: Path | None,
    source_directory: Path,
) -> tuple[list[_PluginRecord], list[DiscoveryIssue]]:
    issues: list[DiscoveryIssue] = []
    if config is None or not config.is_file():
        files = sorted(source_directory.glob("*.js"), key=lambda item: item.name.casefold())
        if len(files) > MAX_PLUGIN_FILES:
            raise ValueError(f"plugin source count exceeds {MAX_PLUGIN_FILES}")
        issues.append(
            DiscoveryIssue("warning", "PLUGIN_REGISTRY_MISSING", None, None, "enabled status and load order are unavailable")
        )
        return [
            _PluginRecord(path.stem, None, None, path)
            for path in files
        ], issues
    try:
        text = config.read_text(encoding="utf-8-sig")
        match = re.search(r"(?:var\s+)?\$plugins\s*=\s*(\[[\s\S]*\])\s*;?\s*$", text)
        if match is None:
            raise ValueError("plugins.js does not contain a supported $plugins array")
        payload = json.loads(match.group(1))
    except (OSError, UnicodeError, ValueError) as exc:
        issues.append(DiscoveryIssue("error", "PLUGIN_REGISTRY_ERROR", None, config.name, str(exc)))
        return [], issues
    if not isinstance(payload, list):
        return [], [DiscoveryIssue("error", "PLUGIN_REGISTRY_ERROR", None, config.name, "plugin registry is not an array")]
    records: list[_PluginRecord] = []
    for index, item in enumerate(payload[:MAX_PLUGIN_FILES]):
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        name = item["name"]
        safe = Path(name).name == name and name not in {".", ".."}
        source = source_directory / f"{name}.js" if safe else source_directory / "__invalid__.js"
        records.append(_PluginRecord(name, bool(item.get("status")), index, source))
    return records, issues


def _strip_comments(source: str) -> str:
    output: list[str] = []
    index = 0
    quote: str | None = None
    while index < len(source):
        char = source[index]
        next_char = source[index + 1] if index + 1 < len(source) else ""
        if quote:
            output.append(char)
            if char == "\\" and index + 1 < len(source):
                output.append(source[index + 1]); index += 2; continue
            if char == quote:
                quote = None
            index += 1; continue
        if char in {"'", '"', "`"}:
            quote = char; output.append(char); index += 1; continue
        if char == "/" and next_char == "/":
            end = source.find("\n", index + 2)
            index = len(source) if end < 0 else end
            output.append("\n"); continue
        if char == "/" and next_char == "*":
            end = source.find("*/", index + 2)
            index = len(source) if end < 0 else end + 2
            output.append(" "); continue
        output.append(char); index += 1
    return "".join(output)


_FUNCTION_ASSIGN = re.compile(
    r"(?P<name>(?:Game_Interpreter\.prototype\.)?[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*=\s*function\s*\((?P<params>[^)]*)\)\s*\{"
)
_FUNCTION_DECL = re.compile(r"function\s+(?P<name>[A-Za-z_$][\w$]*)\s*\((?P<params>[^)]*)\)\s*\{")


def _extract_functions(source: str) -> list[_Function]:
    found: list[tuple[int, _Function]] = []
    for pattern, kind in ((_FUNCTION_ASSIGN, "assignment"), (_FUNCTION_DECL, "declaration")):
        for match in pattern.finditer(source):
            end = _balanced_end(source, match.end() - 1, "{", "}")
            if end is None:
                continue
            params = tuple(part.strip() for part in match.group("params").split(",") if part.strip())
            found.append((match.start(), _Function(match.group("name"), params, source[match.end():end - 1], kind)))
    return [item for _, item in sorted(found, key=lambda pair: pair[0])]


def _balanced_end(text: str, start: int, opening: str, closing: str) -> int | None:
    if start >= len(text) or text[start] != opening:
        return None
    depth = 0; quote: str | None = None; index = start
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\": index += 2; continue
            if char == quote: quote = None
        elif char in {"'", '"', "`"}: quote = char
        elif char == opening: depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0: return index + 1
        index += 1
    return None


def _analyze_handler(plugin: _PluginRecord, handler: _Function, functions: list[_Function]) -> list[MvPluginDiscovery]:
    if len(handler.parameters) < 2:
        return []
    command_var, args_var = handler.parameters[:2]
    bodies = [(handler.body, "direct", ())]
    for helper_name in _called_dispatch_helpers(handler.body, command_var, args_var):
        full = f"Game_Interpreter.prototype.{helper_name}"
        helper = next((item for item in functions if item.name == full and len(item.parameters) >= 2), None)
        if helper is not None:
            bodies.append((helper.body, "helper_dispatch", (helper_name,)))
    results: list[MvPluginDiscovery] = []
    for body, kind, dispatch_chain in bodies:
        command_name = command_var if kind == "direct" else next(
            item.parameters[0] for item in functions if item.name == f"Game_Interpreter.prototype.{dispatch_chain[0]}"
        )
        argument_name = args_var if kind == "direct" else next(
            item.parameters[1] for item in functions if item.name == f"Game_Interpreter.prototype.{dispatch_chain[0]}"
        )
        aliases = {command_name}
        aliases.update(re.findall(rf"(?:var|let|const)\s+([A-Za-z_$][\w$]*)\s*=\s*{re.escape(command_name)}\s*;", body))
        for literal, branch, normalization in _command_branches(body, aliases):
            results.append(
                _classify_branch(plugin, literal, normalization, branch, argument_name, functions, kind, dispatch_chain)
            )
    return results


def _called_dispatch_helpers(body: str, command_var: str, args_var: str) -> set[str]:
    pattern = re.compile(
        rf"this\.([A-Za-z_$][\w$]*)\s*\(\s*{re.escape(command_var)}\s*,\s*{re.escape(args_var)}\s*\)"
    )
    return set(pattern.findall(body))


def _command_branches(body: str, aliases: set[str]) -> Iterator[tuple[str, str, str]]:
    alias_pattern = "|".join(re.escape(item) for item in sorted(aliases, key=len, reverse=True))
    expression = rf"(?:{alias_pattern})(?:\.to(?:Upper|Lower)Case\(\))?"
    compare = re.compile(rf"(?P<expr>{expression})\s*={{2,3}}\s*(['\"])(?P<literal>.*?)\2")
    for match in compare.finditer(body):
        branch = _branch_after_comparison(body, match.end())
        if branch is not None:
            yield match.group("literal"), branch, _command_normalization(match.group("expr"))
    switch = re.compile(rf"switch\s*\(\s*(?P<expr>{expression})\s*\)\s*\{{")
    for match in switch.finditer(body):
        end = _balanced_end(body, match.end() - 1, "{", "}")
        if end is None:
            continue
        switch_body = body[match.end():end - 1]
        cases = list(re.finditer(r"case\s*(['\"])(.*?)\1\s*:", switch_body))
        for index, case in enumerate(cases):
            stop = cases[index + 1].start() if index + 1 < len(cases) else len(switch_body)
            yield case.group(2), switch_body[case.end():stop], _command_normalization(match.group("expr"))


def _command_normalization(expression: str) -> str:
    if ".toUpperCase()" in expression:
        return "upper"
    if ".toLowerCase()" in expression:
        return "lower"
    return "exact"


def _branch_after_comparison(body: str, position: int) -> str | None:
    brace = body.find("{", position)
    semicolon = body.find(";", position)
    if brace >= 0 and (semicolon < 0 or brace < semicolon):
        end = _balanced_end(body, brace, "{", "}")
        return body[brace + 1:end - 1] if end else None
    if semicolon >= 0:
        return body[position:semicolon + 1]
    return None


_DISPLAY_SINKS = (
    (re.compile(r"\$gameMessage\.add\s*\("), 0, "$gameMessage.add"),
    (re.compile(r"(?:\.|\b)drawTextEx\s*\("), 0, "drawTextEx"),
    (re.compile(r"(?:\.|\b)drawText\s*\("), 0, "drawText"),
    (re.compile(r"CommonPopupManager\.showInfo\s*\("), 1, "CommonPopupManager.showInfo"),
)
_INTERNAL_HINT = re.compile(
    r"(?:movement|movePlayer|moveEvent|unlockClass|removeClass|\$gameSwitches|"
    r"\$gameVariables|\$gameScreen|showPicture|AudioManager|shop|save|actorId|"
    r"classId|itemId|skillId|mapId|eventId|setValue|ImageManager|AudioManager)", re.I
)


def _classify_branch(
    plugin: _PluginRecord,
    command: str,
    command_normalization: str,
    branch: str,
    args_var: str,
    functions: list[_Function],
    handler_kind: str,
    dispatch_chain: tuple[str, ...],
) -> MvPluginDiscovery:
    aliases = _local_aliases(branch)
    display = _find_display_flow(branch, args_var, aliases)
    helper_chain = list(dispatch_chain)
    if display is None:
        display, helper = _find_one_helper_flow(branch, args_var, aliases, functions)
        if helper:
            helper_chain.append(helper)
    if display is not None:
        flow, sink = display
        apply_safe = flow.mode in {"single_token", "fixed_index", "joined_remainder", "joined_slice"}
        space_policy = "safe" if flow.mode in {"joined_remainder", "joined_slice"} else "requires_protection"
        classification = APPLY_VERIFIED if apply_safe else DISCOVERED_VERIFIED
        return _observation(plugin, command, command_normalization, handler_kind, flow, tuple(helper_chain), sink, "display", classification, "high", space_policy)
    flows = list(_all_flows(branch, args_var, aliases))
    if re.search(rf"\beval\s*\([^)]*\b{re.escape(args_var)}\b", branch):
        flow = flows[0] if flows else _Flow("unknown", (), None)
        return _observation(plugin, command, command_normalization, handler_kind, flow, tuple(helper_chain), "eval", "unsafe", UNSAFE, "high", "unknown")
    if any(flow.numeric for flow in flows) or (_INTERNAL_HINT.search(branch) and (flows or not re.search(rf"\b{re.escape(args_var)}\b", branch))):
        flow = flows[0] if flows else _Flow("identifier", (), None)
        mode = "numeric" if any(item.numeric for item in flows) else "identifier"
        flow = replace(flow, mode=mode)
        return _observation(plugin, command, command_normalization, handler_kind, flow, tuple(helper_chain), _internal_sink(branch), "internal", INTERNAL, "high", "unknown")
    flow = flows[0] if flows else _Flow("unknown", (), None)
    reason = "argument flow does not reach a recognized display or internal sink"
    return _observation(plugin, command, command_normalization, handler_kind, flow, tuple(helper_chain), None, "unknown", UNKNOWN, "low", "unknown", reason)


def _observation(
    plugin: _PluginRecord, command: str, command_normalization: str,
    handler_kind: str, flow: _Flow,
    helper_chain: tuple[str, ...], sink: str | None, sink_kind: str,
    classification: str, confidence: str, space_policy: str,
    unresolved_reason: str | None = None,
) -> MvPluginDiscovery:
    if plugin.enabled is None and classification == APPLY_VERIFIED:
        classification = DISCOVERED_VERIFIED
        confidence = "medium"
        unresolved_reason = "plugin registry is unavailable; enabled/load-order status is unverified"
    return MvPluginDiscovery(
        command=command, plugin_name=plugin.name, plugin_file=plugin.source.name,
        load_order=plugin.load_order, enabled=plugin.enabled,
        handler_kind=handler_kind, command_normalization=command_normalization,
        consumed_arguments=flow.indices,
        argument_mode=flow.mode, helper_chain=helper_chain, sink=sink,
        sink_kind=sink_kind, classification=classification,
        confidence=confidence, space_policy=space_policy,
        payload_start=flow.start, unresolved_reason=unresolved_reason,
    )


def _local_aliases(body: str) -> dict[str, str]:
    return {
        name: expression.strip()
        for name, expression in re.findall(
            r"(?:var|let|const)\s+([A-Za-z_$][\w$]*)\s*=\s*([^;]+);", body
        )
    }


def _find_display_flow(body: str, args_var: str, aliases: dict[str, str]) -> tuple[_Flow, str] | None:
    for pattern, argument_index, sink in _DISPLAY_SINKS:
        for match in pattern.finditer(body):
            end = _balanced_end(body, match.end() - 1, "(", ")")
            if end is None:
                continue
            arguments = _split_arguments(body[match.end():end - 1])
            if argument_index >= len(arguments):
                continue
            flow = _parse_flow(arguments[argument_index], args_var, aliases)
            if flow is not None:
                return flow, sink
    return None


def _find_one_helper_flow(
    body: str, args_var: str, aliases: dict[str, str], functions: list[_Function]
) -> tuple[tuple[_Flow, str] | None, str | None]:
    calls = re.finditer(r"(?<!function\s)(?P<name>[A-Za-z_$][\w$]*)\s*\(", body)
    for call in calls:
        name = call.group("name")
        if name in {"if", "switch", "Number", "parseInt", "eval"}:
            continue
        end = _balanced_end(body, call.end() - 1, "(", ")")
        if end is None:
            continue
        actual = _split_arguments(body[call.end():end - 1])
        helper = next((item for item in functions if item.name == name and item.parameters), None)
        if helper is None:
            continue
        for index, expression in enumerate(actual):
            flow = _parse_flow(expression, args_var, aliases)
            if flow is None or index >= len(helper.parameters):
                continue
            parameter = helper.parameters[index]
            helper_display = _find_display_flow(helper.body, parameter, _local_aliases(helper.body))
            if helper_display is not None:
                return (flow, helper_display[1]), name
    return None, None


def _all_flows(body: str, args_var: str, aliases: dict[str, str]) -> Iterator[_Flow]:
    expressions = [body, *aliases.values()]
    seen: set[_Flow] = set()
    for expression in expressions:
        for candidate in re.findall(rf"(?:Number|parseInt)?\s*\(?'?{re.escape(args_var)}[^;,)]*\)?", expression):
            flow = _parse_flow(candidate, args_var, aliases)
            if flow is not None and flow not in seen:
                seen.add(flow); yield flow


def _parse_flow(expression: str, args_var: str, aliases: dict[str, str], depth: int = 0) -> _Flow | None:
    text = expression.strip()
    if depth < 3 and re.fullmatch(r"[A-Za-z_$][\w$]*", text) and text in aliases:
        return _parse_flow(aliases[text], args_var, aliases, depth + 1)
    escaped = re.escape(args_var)
    numeric = re.search(rf"(?:Number|parseInt)\s*\(\s*{escaped}\[(\d+)\]", text)
    if numeric:
        index = int(numeric.group(1)); return _Flow("numeric", (index,), index, True)
    sliced = re.search(rf"{escaped}\.slice\(\s*(\d+)\s*\)\.join\(\s*['\"] ['\"]\s*\)", text)
    if sliced:
        start = int(sliced.group(1)); return _Flow("joined_slice", (), start)
    joined = re.search(rf"{escaped}\.join\(\s*['\"] ['\"]\s*\)", text)
    if joined:
        return _Flow("joined_remainder", (), 0)
    indices = tuple(int(value) for value in re.findall(rf"{escaped}\[(\d+)\]", text))
    if len(indices) == 1:
        return _Flow("single_token" if indices[0] == 0 else "fixed_index", indices, indices[0])
    if len(indices) > 1:
        return _Flow("multiple_fixed", indices, min(indices))
    if re.search(rf"\b{escaped}\b", text):
        return _Flow("unknown", (), None)
    return None


def _split_arguments(value: str) -> list[str]:
    result: list[str] = []; start = 0; depth = 0; quote: str | None = None
    for index, char in enumerate(value):
        if quote:
            if char == "\\": continue
            if char == quote: quote = None
        elif char in {"'", '"', "`"}: quote = char
        elif char in "([{": depth += 1
        elif char in ")]}": depth -= 1
        elif char == "," and depth == 0:
            result.append(value[start:index].strip()); start = index + 1
    result.append(value[start:].strip())
    return result


def _internal_sink(branch: str) -> str:
    match = _INTERNAL_HINT.search(branch)
    return match.group(0) if match else "internal argument consumer"


def _resolve_ambiguities(observations: list[MvPluginDiscovery]) -> list[MvPluginDiscovery]:
    grouped: dict[str, list[MvPluginDiscovery]] = {}
    for item in observations:
        normalized = (
            item.command.upper() if item.command_normalization == "upper" else
            item.command.lower() if item.command_normalization == "lower" else
            item.command
        )
        grouped.setdefault(normalized, []).append(item)
    result: list[MvPluginDiscovery] = []
    for command, items in grouped.items():
        plugins = {(item.plugin_name, item.plugin_file) for item in items}
        if len(plugins) > 1:
            items = [
                replace(
                    item, classification=UNKNOWN, confidence="low",
                    unresolved_reason="command is handled by multiple enabled plugins; load-order semantics are ambiguous",
                )
                for item in items
            ]
        result.extend(items)
    return sorted(result, key=lambda item: (item.command.casefold(), item.load_order if item.load_order is not None else 10**9, item.plugin_file.casefold()))


def _fingerprint_sources(config: Path | None, files: list[Path], root: Path) -> str:
    digest = hashlib.sha256(b"glt-mv-plugin-discovery-v1\0")
    selected: list[tuple[str, Path]] = []
    if config is not None and config.is_file():
        selected.append((f"registry/{config.name}", config))
    selected.extend((f"plugin/{path.name}", path) for path in files)
    for relative, path in sorted(selected, key=lambda item: item[0].casefold()):
        digest.update(relative.encode("utf-8")); digest.update(b"\0")
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _is_linklike(path: Path) -> bool:
    try:
        junction = getattr(path, "is_junction", None)
        return path.is_symlink() or os.path.islink(path) or bool(junction and junction())
    except OSError:
        return True


__all__ = [
    "APPLY_VERIFIED", "CONDITIONAL", "DISCOVERED_VERIFIED", "INTERNAL",
    "MvPluginDiscovery", "MvPluginDiscoveryReport", "UNKNOWN", "UNSAFE",
    "discover_mv_plugin_commands",
]
