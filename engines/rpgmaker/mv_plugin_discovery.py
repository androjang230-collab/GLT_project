"""Bounded lexical discovery for RPG Maker MV plugin commands.

The analyzer intentionally recognizes a small, auditable JavaScript subset. It
does not execute JavaScript and does not treat a display API elsewhere in the
same file as evidence for a command branch.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Iterator

from engines.rpgmaker.plugin_inventory import (
    PluginInventory as ActivePluginInventory,
    load_plugin_inventory,
)


MAX_PLUGIN_BYTES = 8 * 1024 * 1024
MAX_TOTAL_PLUGIN_BYTES = 512 * 1024 * 1024
MAX_HANDLERS = 10_000
MAX_LOCAL_MAPS = 128
MAX_MAP_REGISTRATIONS = 2_048

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
    body_start: int
    body_end: int


@dataclass(frozen=True, slots=True)
class _Flow:
    mode: str
    indices: tuple[int, ...]
    start: int | None
    numeric: bool = False


@dataclass(frozen=True, slots=True)
class _MapDispatchSite:
    map_name: str
    normalization: str
    argument_wrapper: str | None


def discover_mv_plugin_commands(
    plugin_config_file: Path | None,
    plugin_source_directory: Path,
    *,
    inventory: ActivePluginInventory | None = None,
) -> MvPluginDiscoveryReport:
    """Analyze enabled MV plugin sources without executing or modifying them."""

    if _is_linklike(plugin_source_directory):
        raise ValueError("plugin source directory cannot be a symlink or junction")
    source_directory = plugin_source_directory.resolve()
    config = plugin_config_file.resolve() if plugin_config_file is not None else None
    if plugin_config_file is not None and _is_linklike(plugin_config_file):
        raise ValueError("plugins.js cannot be a symlink or junction")
    inventory = inventory or load_plugin_inventory(config, source_directory)
    registry = [
        _PluginRecord(
            item.name,
            item.enabled,
            item.load_order,
            item.source_path or source_directory / "__invalid__.js",
        )
        for item in inventory.plugins
    ]
    report = MvPluginDiscoveryReport(registry_available=config is not None and config.is_file())
    enabled_by_name = {item.name: item.enabled for item in inventory.plugins}
    report.issues.extend(
        DiscoveryIssue(
            item.severity,
            item.code,
            item.plugin_name,
            Path(item.source_file).name if item.source_file else None,
            item.reason,
        )
        for item in inventory.issues
        if item.plugin_name is None or enabled_by_name.get(item.plugin_name) is not False
    )
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
            continue
        if plugin.source.stat().st_size > MAX_PLUGIN_BYTES:
            continue
        try:
            source = plugin.source.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            report.issues.append(
                DiscoveryIssue("warning", "PLUGIN_SOURCE_READ_ERROR", plugin.name, plugin.source.name, str(exc))
            )
            continue
        report.source_resolved_plugins += 1
        stripped_source = _strip_comments(source)
        functions = _extract_functions(stripped_source)
        direct = [item for item in functions if item.name == "Game_Interpreter.prototype.pluginCommand"]
        handler_count += len(direct)
        for handler in direct:
            observations.extend(
                _analyze_handler(plugin, handler, functions, stripped_source)
            )
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
    inventory = load_plugin_inventory(config, source_directory)
    records = [
        _PluginRecord(
            item.name,
            item.enabled,
            item.load_order,
            item.source_path or source_directory / "__invalid__.js",
        )
        for item in inventory.plugins
    ]
    issues = [
        DiscoveryIssue(
            item.severity,
            item.code,
            item.plugin_name,
            Path(item.source_file).name if item.source_file else None,
            item.reason,
        )
        for item in inventory.issues
    ]
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
            found.append(
                (
                    match.start(),
                    _Function(
                        match.group("name"),
                        params,
                        source[match.end():end - 1],
                        kind,
                        match.end(),
                        end - 1,
                    ),
                )
            )
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


def _analyze_handler(
    plugin: _PluginRecord,
    handler: _Function,
    functions: list[_Function],
    source: str,
) -> list[MvPluginDiscovery]:
    if len(handler.parameters) < 2:
        return []
    results: list[MvPluginDiscovery] = []
    for body, command_name, argument_name, kind, dispatch_chain in _dispatch_bodies(
        handler, functions
    ):
        for literal, branch, normalization in _command_branches(
            body, command_name, functions
        ):
            results.append(
                _classify_branch(plugin, literal, normalization, branch, argument_name, functions, kind, dispatch_chain)
            )
    results.extend(
        _analyze_map_dispatch(plugin, handler, functions, source)
    )
    return results


_LOCAL_MAP_DECLARATION = re.compile(
    r"(?:var|let|const)\s+([A-Za-z_$][\w$]*)\s*=\s*new\s+Map\s*\(\s*\)\s*;"
)


def _analyze_map_dispatch(
    plugin: _PluginRecord,
    handler: _Function,
    functions: list[_Function],
    source: str,
) -> list[MvPluginDiscovery]:
    """Resolve a small, literal Map registry tied to pluginCommand dispatch."""

    if plugin.enabled is not True:
        return []
    map_names = [match.group(1) for match in _LOCAL_MAP_DECLARATION.finditer(source)]
    if len(map_names) > MAX_LOCAL_MAPS:
        return []
    declared_once = {name for name in map_names if map_names.count(name) == 1}
    sites = [
        site for site in _map_dispatch_sites(handler, functions)
        if site.map_name in declared_once
    ]
    grouped_sites: dict[str, list[_MapDispatchSite]] = {}
    for site in sites:
        grouped_sites.setdefault(site.map_name, []).append(site)

    results: list[MvPluginDiscovery] = []
    for map_name, candidates in grouped_sites.items():
        distinct = {
            (item.normalization, item.argument_wrapper) for item in candidates
        }
        if len(distinct) != 1 or not _map_is_stable(source, map_name):
            continue
        registrations = _literal_map_registrations(
            source, map_name, functions
        )
        if registrations is None:
            continue
        site = candidates[0]
        for command, method_name in registrations.items():
            if not _registration_matches_normalization(command, site.normalization):
                continue
            if not re.fullmatch(r"[A-Za-z_$][\w$]*", method_name):
                continue
            target = _resolve_function(f"this.{method_name}", functions)
            if target is None or not target.parameters:
                continue
            helper_chain = tuple(
                value for value in (site.argument_wrapper, method_name) if value
            )
            results.append(
                _classify_branch(
                    plugin,
                    command,
                    site.normalization,
                    target.body,
                    target.parameters[0],
                    functions,
                    "map_dispatch",
                    helper_chain,
                )
            )
    return results


def _map_dispatch_sites(
    handler: _Function,
    functions: list[_Function],
) -> list[_MapDispatchSite]:
    command_var, args_var = handler.parameters[:2]
    expressions = _command_expressions(handler.body, command_var, functions)
    result: list[_MapDispatchSite] = []
    lookup = re.compile(
        r"(?:var|let|const)\s+(?P<target>[A-Za-z_$][\w$]*)\s*=\s*"
        r"(?P<map>[A-Za-z_$][\w$]*)\.get\s*\("
    )
    for match in lookup.finditer(handler.body):
        end = _balanced_end(handler.body, match.end() - 1, "(", ")")
        if end is None:
            continue
        lookup_arguments = _split_arguments(handler.body[match.end():end - 1])
        if len(lookup_arguments) != 1:
            continue
        expression = lookup_arguments[0].strip()
        normalization = expressions.get(expression)
        if normalization is None:
            normalization = _resolve_command_transform(
                expression, command_var, functions
            )
        if normalization is None:
            continue
        target = match.group("target")
        if _assignment_count(handler.body, target) != 1:
            continue
        wrappers = _guarded_computed_forwarding(
            handler.body, target, args_var, functions
        )
        for wrapper in wrappers:
            result.append(
                _MapDispatchSite(match.group("map"), normalization, wrapper)
            )
    return result


def _guarded_computed_forwarding(
    body: str,
    lookup_target: str,
    args_var: str,
    functions: list[_Function],
) -> list[str | None]:
    target_aliases = _direct_alias_names(body, lookup_target)
    argument_aliases = _direct_alias_names(body, args_var)
    result: list[str | None] = []
    for guarded_name in sorted(target_aliases):
        guard = re.compile(
            rf"\bif\s*\(\s*{re.escape(guarded_name)}\s*\)\s*\{{"
        )
        for match in guard.finditer(body):
            end = _balanced_end(body, match.end() - 1, "{", "}")
            if end is None:
                continue
            branch = body[match.end():end - 1]
            computed = re.compile(
                r"\bthis\s*\[\s*(?P<target>[A-Za-z_$][\w$]*)\s*\]\s*\("
            )
            for call in computed.finditer(branch):
                if call.group("target") not in target_aliases:
                    continue
                call_end = _balanced_end(branch, call.end() - 1, "(", ")")
                if call_end is None:
                    continue
                arguments = _split_arguments(branch[call.end():call_end - 1])
                if len(arguments) != 1:
                    continue
                forwarded, wrapper = _forwarded_argument_wrapper(
                    arguments[0], argument_aliases, functions
                )
                if forwarded and wrapper not in result:
                    result.append(wrapper)
    return result


def _forwarded_argument_wrapper(
    expression: str,
    argument_aliases: set[str],
    functions: list[_Function],
) -> tuple[bool, str | None]:
    value = expression.strip()
    if value in argument_aliases:
        return True, None
    calls = list(_iter_named_calls_with_positions(value))
    if len(calls) != 1:
        return False, None
    start, end, name, arguments = calls[0]
    if start != 0 or end != len(value) or len(arguments) != 1:
        return False, None
    if arguments[0].strip() not in argument_aliases:
        return False, None
    helper = _resolve_function(name, functions)
    if helper is None or not _is_array_identity_wrapper(helper):
        return False, None
    return True, _short_function_name(helper.name)


def _is_array_identity_wrapper(helper: _Function) -> bool:
    if len(helper.parameters) != 1:
        return False
    parameter = helper.parameters[0]
    escaped = re.escape(parameter)
    returns = re.findall(r"\breturn\s+([^;]+);", helper.body)
    if len(returns) != 1 or returns[0].strip() not in {
        parameter, f"({parameter})"
    }:
        return False
    if re.search(
        rf"\b{escaped}\s*\.\s*(?:pop|push|shift|unshift|splice|sort|reverse|concat|slice)\s*\(",
        helper.body,
    ):
        return False
    if re.search(rf"\b{escaped}\s*\.\s*length\s*=", helper.body):
        return False
    if re.search(rf"\bdelete\s+{escaped}\s*\[", helper.body):
        return False
    if re.search(rf"(?<![.\w$]){escaped}\s*=(?!=)", helper.body):
        return False
    if re.search(
        rf"(?:var|let|const)\s+[A-Za-z_$][\w$]*\s*=\s*{escaped}\s*;",
        helper.body,
    ):
        return False
    if any(
        argument.strip() == parameter
        for _, _, _, arguments in _iter_named_calls_with_positions(helper.body)
        for argument in arguments
    ):
        return False

    writes = list(
        re.finditer(
            rf"\b{escaped}\s*\[\s*(?P<index>[^\]]+)\s*\]\s*=\s*(?P<value>[^;]+);",
            helper.body,
        )
    )
    for write in writes:
        index = write.group("index").strip()
        value = write.group("value").strip()
        calls = list(_iter_named_calls_with_positions(value))
        if len(calls) != 1:
            return False
        start, end, _, arguments = calls[0]
        if start != 0 or end != len(value) or len(arguments) != 1:
            return False
        expected = re.sub(r"\s+", "", f"{parameter}[{index}]")
        if re.sub(r"\s+", "", arguments[0]) != expected:
            return False
        if not re.fullmatch(r"\d+", index):
            loop = re.compile(
                rf"for\s*\([^;]*\b{re.escape(index)}\b[^;]*;"
                rf"[^;]*\b{re.escape(index)}\b\s*<\s*{escaped}\.length\s*;"
                rf"[^)]*(?:\+\+\s*{re.escape(index)}|{re.escape(index)}\s*\+\+)\s*\)"
            )
            if loop.search(helper.body) is None:
                return False
    remaining_writes = re.sub(
        rf"\b{escaped}\s*\[\s*[^\]]+\s*\]\s*=\s*[^;]+;",
        "",
        helper.body,
    )
    if re.search(rf"\b{escaped}\s*\[.*?\]\s*=", remaining_writes):
        return False
    return True


def _map_is_stable(source: str, map_name: str) -> bool:
    if _assignment_count(source, map_name) != 1:
        return False
    escaped = re.escape(map_name)
    alias = re.search(
        rf"(?:var|let|const)\s+(?!{escaped}\b)[A-Za-z_$][\w$]*\s*=\s*{escaped}\s*;",
        source,
    )
    if alias is not None:
        return False
    return not any(
        argument.strip() == map_name
        for _, _, _, arguments in _iter_named_calls_with_positions(source)
        for argument in arguments
    )


def _assignment_count(source: str, name: str) -> int:
    return len(
        re.findall(
            rf"(?<![.\w$]){re.escape(name)}\s*"
            rf"(?:=(?!=)|\+=|-=|\*=|/=|%=|\+\+|--)",
            source,
        )
    )


def _literal_map_registrations(
    source: str,
    map_name: str,
    functions: list[_Function],
) -> dict[str, str] | None:
    constants = _static_string_constants(source)
    template_positions: set[int] = set()
    templates: dict[str, tuple[_Function, str, str]] = {}
    receiver = f"{map_name}.set"
    for helper in functions:
        calls = [
            (start, arguments)
            for start, _, name, arguments in _iter_named_calls_with_positions(helper.body)
            if name == receiver
        ]
        if len(calls) != 1 or not helper.parameters or "." in helper.name:
            continue
        start, arguments = calls[0]
        if len(arguments) != 2:
            continue
        placeholders = {name: name for name in helper.parameters}
        if (
            _resolve_static_string(arguments[0], constants, placeholders) is None
            or _resolve_static_string(arguments[1], constants, placeholders) is None
        ):
            continue
        templates[helper.name] = (helper, arguments[0], arguments[1])
        template_positions.add(helper.body_start + start)

    registrations: list[tuple[str, str]] = []
    calls = list(_iter_named_calls_with_positions(source))
    for start, _, name, arguments in calls:
        if name in {f"{map_name}.delete", f"{map_name}.clear"}:
            return None
        if name != receiver or start in template_positions:
            continue
        if _position_inside_function(start, functions):
            return None
        if len(arguments) != 2:
            return None
        key = _resolve_static_string(arguments[0], constants, {})
        method = _resolve_static_string(arguments[1], constants, {})
        if key is None or method is None:
            return None
        registrations.append((key, method))

    for helper_name, (helper, key_expression, method_expression) in templates.items():
        helper_calls = [
            (start, arguments)
            for start, _, name, arguments in calls if name == helper_name
        ]
        for start, arguments in helper_calls:
            if _position_inside_function(start, functions):
                return None
            if len(arguments) != len(helper.parameters):
                return None
            bindings: dict[str, str] = {}
            for parameter, argument in zip(helper.parameters, arguments):
                value = _resolve_static_string(argument, constants, {})
                if value is None:
                    return None
                bindings[parameter] = value
            key = _resolve_static_string(key_expression, constants, bindings)
            method = _resolve_static_string(
                method_expression, constants, bindings
            )
            if key is None or method is None:
                return None
            registrations.append((key, method))

    if not registrations or len(registrations) > MAX_MAP_REGISTRATIONS:
        return None
    grouped: dict[str, list[str]] = {}
    for key, method in registrations:
        grouped.setdefault(key, []).append(method)
    return {
        key: methods[0]
        for key, methods in grouped.items()
        if len(methods) == 1
    }


def _position_inside_function(position: int, functions: list[_Function]) -> bool:
    return any(
        helper.body_start <= position < helper.body_end for helper in functions
    )


def _static_string_constants(source: str) -> dict[str, str]:
    candidates: dict[str, list[str]] = {}
    declaration = re.compile(
        r"(?:var|let|const)\s+([A-Za-z_$][\w$]*)\s*=\s*"
        r"((?:'(?:\\.|[^'\\])*')|(?:\"(?:\\.|[^\"\\])*\"))\s*;"
    )
    for match in declaration.finditer(source):
        value = _decode_js_string_literal(match.group(2))
        if value is not None:
            candidates.setdefault(match.group(1), []).append(value)
    return {
        name: values[0]
        for name, values in candidates.items()
        if len(values) == 1 and _assignment_count(source, name) == 1
    }


def _resolve_static_string(
    expression: str,
    constants: dict[str, str],
    bindings: dict[str, str],
) -> str | None:
    value = expression.strip()
    while value.startswith("(") and value.endswith(")"):
        end = _balanced_end(value, 0, "(", ")")
        if end != len(value):
            break
        value = value[1:-1].strip()
    parts = _split_top_level_plus(value)
    if parts is None:
        return None
    resolved: list[str] = []
    for part in parts:
        token = part.strip()
        literal = _decode_js_string_literal(token)
        if literal is not None:
            resolved.append(literal)
        elif token in bindings:
            resolved.append(bindings[token])
        elif token in constants:
            resolved.append(constants[token])
        else:
            return None
    return "".join(resolved)


def _split_top_level_plus(expression: str) -> list[str] | None:
    result: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    index = 0
    while index < len(expression):
        char = expression[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth < 0:
                return None
        elif char == "+" and depth == 0:
            result.append(expression[start:index])
            start = index + 1
        index += 1
    if quote or depth != 0:
        return None
    result.append(expression[start:])
    return result if all(part.strip() for part in result) else None


def _decode_js_string_literal(value: str) -> str | None:
    if len(value) < 2 or value[0] not in {"'", '"'} or value[-1] != value[0]:
        return None
    result: list[str] = []
    index = 1
    escapes = {
        "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t",
        "v": "\v", "0": "\0", "\\": "\\", "'": "'", '"': '"', "/": "/",
    }
    while index < len(value) - 1:
        char = value[index]
        if char != "\\":
            result.append(char)
            index += 1
            continue
        index += 1
        if index >= len(value) - 1:
            return None
        escaped = value[index]
        if escaped in escapes:
            result.append(escapes[escaped])
            index += 1
            continue
        if escaped in {"x", "u"}:
            width = 2 if escaped == "x" else 4
            digits = value[index + 1:index + 1 + width]
            if len(digits) != width or not re.fullmatch(r"[0-9A-Fa-f]+", digits):
                return None
            result.append(chr(int(digits, 16)))
            index += width + 1
            continue
        return None
    return "".join(result)


def _registration_matches_normalization(command: str, normalization: str) -> bool:
    if normalization == "upper":
        return command == command.upper()
    if normalization == "lower":
        return command == command.lower()
    return normalization == "exact"


def _dispatch_bodies(
    handler: _Function,
    functions: list[_Function],
    *,
    max_depth: int = 2,
) -> Iterator[tuple[str, str, str, str, tuple[str, ...]]]:
    """Yield direct and statically named command/args helper bodies."""

    queue = [(handler, (), 0)]
    visited = {handler.name}
    while queue:
        current, chain, depth = queue.pop(0)
        command_var, args_var = current.parameters[:2]
        yield (
            current.body,
            command_var,
            args_var,
            "direct" if not chain else "helper_dispatch",
            chain,
        )
        if depth >= max_depth:
            continue
        command_aliases = _direct_alias_names(current.body, command_var)
        args_aliases = _direct_alias_names(current.body, args_var)
        for call in _iter_named_calls(current.body):
            if len(call[1]) < 2:
                continue
            if call[1][0].strip() not in command_aliases or call[1][1].strip() not in args_aliases:
                continue
            helper = _resolve_function(call[0], functions)
            if helper is None or len(helper.parameters) < 2 or helper.name in visited:
                continue
            visited.add(helper.name)
            queue.append((helper, chain + (_short_function_name(helper.name),), depth + 1))


def _direct_alias_names(body: str, source: str) -> set[str]:
    aliases = {source}
    changed = True
    while changed:
        changed = False
        for name, value in re.findall(
            r"(?:var|let|const)\s+([A-Za-z_$][\w$]*)\s*=\s*([A-Za-z_$][\w$]*)\s*;",
            body,
        ):
            if value in aliases and name not in aliases:
                aliases.add(name)
                changed = True
    return aliases


def _command_branches(
    body: str,
    command_var: str,
    functions: list[_Function],
) -> Iterator[tuple[str, str, str]]:
    expressions = _command_expressions(body, command_var, functions)
    seen: set[tuple[int, str]] = set()
    for expression, normalization in sorted(
        expressions.items(), key=lambda item: len(item[0]), reverse=True
    ):
        escaped = re.escape(expression)
        compare = re.compile(
            rf"(?P<expr>{escaped})\s*={{2,3}}\s*(['\"])(?P<literal>.*?)\2"
        )
        for match in compare.finditer(body):
            marker = (match.start(), match.group("literal"))
            if marker in seen:
                continue
            branch = _branch_after_comparison(body, match.end())
            if branch is not None:
                seen.add(marker)
                yield match.group("literal"), branch, normalization
        switch = re.compile(rf"switch\s*\(\s*{escaped}\s*\)\s*\{{")
        for match in switch.finditer(body):
            end = _balanced_end(body, match.end() - 1, "{", "}")
            if end is None:
                continue
            switch_body = body[match.end():end - 1]
            cases = _top_level_cases(switch_body)
            for index, (literal, case_start, content_start) in enumerate(cases):
                marker = (match.start() + case_start, literal)
                if marker in seen:
                    continue
                stop = cases[index + 1][1] if index + 1 < len(cases) else len(switch_body)
                seen.add(marker)
                yield literal, switch_body[content_start:stop], normalization


def _top_level_cases(body: str) -> list[tuple[str, int, int]]:
    cases: list[tuple[str, int, int]] = []
    index = 0
    depth = 0
    quote: str | None = None
    pattern = re.compile(r"case\s*(['\"])(.*?)\1\s*:")
    while index < len(body):
        char = body[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in {"'", '"', "`"}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth = max(0, depth - 1)
        elif depth == 0:
            match = pattern.match(body, index)
            if match is not None:
                cases.append((match.group(2), match.start(), match.end()))
                index = match.end()
                continue
        index += 1
    return cases


def _command_expressions(
    body: str,
    command_var: str,
    functions: list[_Function],
) -> dict[str, str]:
    expressions = {
        command_var: "exact",
        f"{command_var}.toUpperCase()": "upper",
        f"{command_var}.toLowerCase()": "lower",
    }
    for name, value in re.findall(
        r"(?:var|let|const)\s+([A-Za-z_$][\w$]*)\s*=\s*([^;]+);",
        body,
    ):
        normalization = _resolve_command_transform(value.strip(), command_var, functions)
        if normalization is not None:
            expressions[name] = normalization
    for helper in functions:
        if len(helper.parameters) != 1 or "." in helper.name:
            continue
        normalization = _function_command_transform(helper, functions, set(), 0)
        if normalization is not None:
            expressions[f"{helper.name}({command_var})"] = normalization
    return expressions


def _resolve_command_transform(
    expression: str,
    source_var: str,
    functions: list[_Function],
) -> str | None:
    compact = re.sub(r"\s+", "", expression)
    source = re.escape(source_var)
    if re.fullmatch(source, compact):
        return "exact"
    if re.fullmatch(rf"{source}\.toUpperCase\(\)", compact):
        return "upper"
    if re.fullmatch(rf"{source}\.toLowerCase\(\)", compact):
        return "lower"
    match = re.fullmatch(r"([A-Za-z_$][\w$]*)\((.*)\)", compact)
    if match and match.group(2) == source_var:
        helper = _resolve_function(match.group(1), functions)
        if helper is not None and len(helper.parameters) == 1:
            return _function_command_transform(helper, functions, set(), 0)
    return None


def _function_command_transform(
    helper: _Function,
    functions: list[_Function],
    visited: set[str],
    depth: int,
) -> str | None:
    if depth > 2 or helper.name in visited or len(helper.parameters) != 1:
        return None
    returns = re.findall(r"\breturn\s+([^;]+);", helper.body)
    if len(returns) != 1:
        return None
    expression = returns[0]
    parameter = helper.parameters[0]
    compact = re.sub(r"\s+", "", expression)
    if parameter not in compact:
        return None
    if compact.endswith(".toUpperCase()"):
        return "upper"
    if compact.endswith(".toLowerCase()"):
        return "lower"
    if compact in {parameter, f"({parameter})"}:
        return "exact"
    nested = re.fullmatch(r"([A-Za-z_$][\w$]*)\((.*)\)", compact)
    if nested and nested.group(2) == parameter:
        target = _resolve_function(nested.group(1), functions)
        if target is not None:
            return _function_command_transform(
                target, functions, visited | {helper.name}, depth + 1
            )
    return None


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
_DISPLAY_STATE_TERMS = ("picture", "window", "popup", "notice", "message", "info")
_DISPLAY_CONFIG_TERMS = ("align", "color", "font", "position", "setting", "config", "size")
_INTERNAL_HINT = re.compile(
    r"(?:movement|movePlayer|moveEvent|unlockClass|removeClass|\$gameSwitches|"
    r"\$gameVariables|\$gameScreen|showPicture|AudioManager|shop|save|actorId|"
    r"classId|itemId|skillId|mapId|eventId|setValue|ImageManager|AudioManager)", re.I
)
_ARRAY_MUTATION = re.compile(r"\.\s*(?:pop|push|shift|unshift|splice)\s*\(")


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
    display = _find_display_flow(branch, args_var, aliases, functions)
    helper_chain = list(dispatch_chain)
    if display is None:
        display, helpers = _find_helper_flow(
            branch,
            args_var,
            aliases,
            functions,
            0,
            max(0, 2 - len(dispatch_chain)),
            set(),
        )
        helper_chain.extend(helpers)
    if display is not None:
        flow, sink = display
        if _ARRAY_MUTATION.search(branch):
            transformed = _optional_numeric_tail_flow(branch, args_var, flow)
            flow = transformed or _Flow("unknown", (), None)
        apply_safe = flow.mode in {
            "single_token", "fixed_index", "joined_remainder", "joined_slice",
            "joined_optional_numeric_tail",
        }
        space_policy = "safe" if flow.mode.startswith("joined_") else "requires_protection"
        classification = APPLY_VERIFIED if apply_safe else DISCOVERED_VERIFIED
        return _observation(plugin, command, command_normalization, handler_kind, flow, tuple(helper_chain), sink, "display", classification, "high", space_policy)
    flows = list(_all_flows(branch, args_var, aliases))
    if re.search(rf"\beval\s*\([^)]*\b{re.escape(args_var)}\b", branch):
        flow = flows[0] if flows else _Flow("unknown", (), None)
        return _observation(plugin, command, command_normalization, handler_kind, flow, tuple(helper_chain), "eval", "unsafe", UNSAFE, "high", "unknown")
    if any(flow.numeric for flow in flows) or (_INTERNAL_HINT.search(branch) and (flows or not re.search(rf"\b{re.escape(args_var)}\b", branch))):
        indices = tuple(sorted({index for item in flows for index in item.indices}))
        numeric = bool(flows) and all(item.numeric for item in flows)
        flow = _Flow(
            "numeric" if numeric else "identifier",
            indices,
            min(indices) if indices else None,
            numeric,
        )
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


def _find_display_flow(
    body: str,
    args_var: str,
    aliases: dict[str, str],
    functions: list[_Function],
) -> tuple[_Flow, str] | None:
    for pattern, argument_index, sink in _DISPLAY_SINKS:
        for match in pattern.finditer(body):
            end = _balanced_end(body, match.end() - 1, "(", ")")
            if end is None:
                continue
            arguments = _split_arguments(body[match.end():end - 1])
            if argument_index >= len(arguments):
                continue
            flow = _parse_flow(arguments[argument_index], args_var, aliases, functions)
            if flow is not None:
                return flow, sink
    for callee, arguments in _iter_named_calls(body):
        if not arguments or not _is_display_state_sink(callee, functions):
            continue
        flow = _parse_flow(arguments[0], args_var, aliases, functions)
        if flow is not None:
            return flow, callee
    return None


def _find_helper_flow(
    body: str,
    args_var: str,
    aliases: dict[str, str],
    functions: list[_Function],
    depth: int,
    max_depth: int,
    visited: set[str],
) -> tuple[tuple[_Flow, str] | None, tuple[str, ...]]:
    if depth >= max_depth:
        return None, ()
    for name, actual in _iter_named_calls(body):
        if _short_function_name(name) in {"if", "switch", "Number", "parseInt", "eval"}:
            continue
        helper = _resolve_function(name, functions)
        if helper is None or not helper.parameters or helper.name in visited:
            continue
        for index, expression in enumerate(actual):
            flow = _parse_flow(expression, args_var, aliases, functions)
            if flow is None or index >= len(helper.parameters):
                continue
            parameter = helper.parameters[index]
            helper_aliases = _local_aliases(helper.body)
            helper_display = _find_display_flow(
                helper.body, parameter, helper_aliases, functions
            )
            if helper_display is not None:
                return (flow, helper_display[1]), (_short_function_name(helper.name),)
            nested, chain = _find_helper_flow(
                helper.body,
                parameter,
                helper_aliases,
                functions,
                depth + 1,
                max_depth,
                visited | {helper.name},
            )
            if nested is not None:
                return (flow, nested[1]), (_short_function_name(helper.name),) + chain
    return None, ()


def _is_display_state_sink(callee: str, functions: list[_Function]) -> bool:
    method = _short_function_name(callee)
    folded = method.casefold()
    if (
        "text" not in folded
        or not any(term in folded for term in _DISPLAY_STATE_TERMS)
        or any(term in folded for term in _DISPLAY_CONFIG_TERMS)
    ):
        return False
    helper = _resolve_function(callee, functions)
    if helper is None or not helper.parameters:
        return False
    parameter = re.escape(helper.parameters[0])
    return bool(
        re.search(
            rf"this\.[A-Za-z_$][\w$]*(?:text|message|notice|info)[A-Za-z0-9_$]*\s*(?:=|\+=)[^;]*\b{parameter}\b",
            helper.body,
            re.I,
        )
    )


def _optional_numeric_tail_flow(
    branch: str,
    args_var: str,
    flow: _Flow,
) -> _Flow | None:
    escaped = re.escape(args_var)
    if flow.mode != "joined_remainder":
        return None
    required = (
        re.search(rf"\b{escaped}\.pop\s*\(\s*\)", branch),
        re.search(rf"\b{escaped}\.push\s*\(", branch),
        re.search(rf"\b{escaped}\s*\[\s*{escaped}\.length\s*-\s*1\s*\]", branch),
        re.search(r"\bisNaN\s*\(", branch),
        re.search(rf"\b{escaped}\.length\s*={{2,3}}\s*1\b", branch),
    )
    return _Flow("joined_optional_numeric_tail", (), 0) if all(required) else None


def _all_flows(body: str, args_var: str, aliases: dict[str, str]) -> Iterator[_Flow]:
    expressions = [body, *aliases.values()]
    seen: set[_Flow] = set()
    for expression in expressions:
        for candidate in re.findall(rf"(?:Number|parseInt)?\s*\(?'?{re.escape(args_var)}[^;,)]*\)?", expression):
            flow = _parse_flow(candidate, args_var, aliases)
            if flow is not None and flow not in seen:
                seen.add(flow); yield flow
    represented = {index for item in seen for index in item.indices}
    for index_text in re.findall(rf"\b{re.escape(args_var)}\s*\[\s*(\d+)\s*\]", body):
        index = int(index_text)
        if index in represented:
            continue
        flow = _Flow(
            "single_token" if index == 0 else "fixed_index",
            (index,),
            index,
        )
        if flow not in seen:
            seen.add(flow)
            represented.add(index)
            yield flow


def _parse_flow(
    expression: str,
    args_var: str,
    aliases: dict[str, str],
    functions: list[_Function] | None = None,
    depth: int = 0,
) -> _Flow | None:
    text = expression.strip()
    if depth < 3 and re.fullmatch(r"[A-Za-z_$][\w$]*", text) and text in aliases:
        return _parse_flow(aliases[text], args_var, aliases, functions, depth + 1)
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
    if functions is not None and depth < 3:
        reconstructed = _helper_reconstruction_flow(
            text, args_var, aliases, functions, depth
        )
        if reconstructed is not None:
            return reconstructed
    if re.search(rf"\b{escaped}\b", text):
        return _Flow("unknown", (), None)
    return None


def _helper_reconstruction_flow(
    expression: str,
    args_var: str,
    aliases: dict[str, str],
    functions: list[_Function],
    depth: int,
) -> _Flow | None:
    for callee, actual in _iter_named_calls(expression):
        helper = _resolve_function(callee, functions)
        if helper is None or not helper.parameters or not actual:
            continue
        source = _parse_flow(actual[0], args_var, aliases, None, depth + 1)
        if source is None or source.mode != "unknown":
            continue
        mode = _reconstruction_helper_mode(helper)
        if mode is None:
            continue
        helper_mode, default_start = mode
        if len(actual) >= 3:
            continue
        start = default_start
        if len(actual) == 2:
            if not re.fullmatch(r"\d+", actual[1].strip()):
                continue
            start = int(actual[1].strip())
        if start is None:
            continue
        if start == 0:
            return _Flow("joined_remainder", (), 0)
        return _Flow("joined_slice", (), start)
    return None


def _reconstruction_helper_mode(helper: _Function) -> tuple[str, int | None] | None:
    array = re.escape(helper.parameters[0])
    direct_join = re.search(
        rf"\breturn\s+{array}\.join\(\s*['\"] ['\"]\s*\)\s*;",
        helper.body,
    )
    if direct_join:
        return "joined_remainder", 0
    direct_slice = re.search(
        rf"\breturn\s+{array}\.slice\(\s*(\d+)\s*\)\.join\(\s*['\"] ['\"]\s*\)\s*;",
        helper.body,
    )
    if direct_slice:
        return "joined_slice", int(direct_slice.group(1))
    if len(helper.parameters) < 2:
        return None
    start_name = helper.parameters[1]
    index_match = re.search(
        rf"for\s*\(\s*var\s+([A-Za-z_$][\w$]*)\s*=\s*{re.escape(start_name)}\s*;",
        helper.body,
    )
    if index_match is None:
        return None
    index = re.escape(index_match.group(1))
    structural = (
        re.search(rf"\b{array}\s*\[\s*{index}\s*\]", helper.body),
        re.search(r"\+=\s*['\"] ['\"]\s*;", helper.body),
        re.search(r"\breturn\s+[A-Za-z_$][\w$]*\s*;", helper.body),
    )
    if not all(structural):
        return None
    default = re.search(
        rf"arguments\.length\s*<\s*2\s*\)\s*{re.escape(start_name)}\s*=\s*(\d+)",
        helper.body,
    )
    return "joined_slice", int(default.group(1)) if default else None


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


def _iter_named_calls(value: str) -> Iterator[tuple[str, list[str]]]:
    for _, _, name, arguments in _iter_named_calls_with_positions(value):
        yield name, arguments


def _iter_named_calls_with_positions(
    value: str,
) -> Iterator[tuple[int, int, str, list[str]]]:
    pattern = re.compile(
        r"(?<!function\s)(?P<name>(?:this\.|\$?[A-Za-z_$][\w$]*\.)*"
        r"[A-Za-z_$][\w$]*)\s*\("
    )
    for match in pattern.finditer(value):
        end = _balanced_end(value, match.end() - 1, "(", ")")
        if end is None:
            continue
        yield (
            match.start(),
            end,
            match.group("name"),
            _split_arguments(value[match.end():end - 1]),
        )


def _short_function_name(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def _resolve_function(name: str, functions: list[_Function]) -> _Function | None:
    if name.startswith("this."):
        exact = f"Game_Interpreter.prototype.{name[5:]}"
        candidates = [item for item in functions if item.name == exact]
    else:
        candidates = [item for item in functions if item.name == name]
        if not candidates and "." in name:
            short = _short_function_name(name)
            candidates = [item for item in functions if item.name.endswith(f".{short}")]
    return candidates[0] if len(candidates) == 1 else None


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
