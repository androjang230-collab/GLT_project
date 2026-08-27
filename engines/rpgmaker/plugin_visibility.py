"""Conservative static evidence for plugin-consumed player-visible values.

This Phase-1 analyzer is intentionally lexical and bounded.  It never executes
JavaScript and it does not create translation entries.  Findings describe an
explainable source-to-sink path that a later contract layer may consume.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from typing import Iterator

from engines.rpgmaker.mv_plugin_discovery import (
    APPLY_VERIFIED,
    DISCOVERED_VERIFIED,
    INTERNAL as COMMAND_INTERNAL,
    UNKNOWN as COMMAND_UNKNOWN,
    UNSAFE as COMMAND_UNSAFE,
    MvPluginDiscoveryReport,
    _extract_functions,
    _iter_named_calls,
    _resolve_function,
    _short_function_name,
    _strip_comments,
)
from engines.rpgmaker.plugin_inventory import PluginInventory, PluginRecord


MAX_HELPER_DEPTH = 2


class VisibilityClassification(str, Enum):
    VERIFIED_VISIBLE = "VERIFIED_VISIBLE"
    CONDITIONAL_VISIBLE = "CONDITIONAL_VISIBLE"
    INTERNAL = "INTERNAL"
    UNSAFE = "UNSAFE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class PluginVisibilityIssue:
    severity: str
    code: str
    reason: str
    plugin_name: str | None = None
    source_file: str | None = None


@dataclass(frozen=True, slots=True)
class PluginVisibilityFinding:
    plugin_name: str
    plugin_file: str
    source_kind: str
    source_access: str
    transform_evidence: tuple[str, ...]
    helper_chain: tuple[str, ...]
    helper_depth: int
    sink: str | None
    classification: str
    reason: str
    source_identity: str = field(repr=False)
    mixed_use: bool = False
    ambiguity: tuple[str, ...] = ()

    def to_json_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload.pop("source_identity", None)
        return payload


@dataclass(slots=True)
class PluginVisibilityReport:
    active_plugin_count: int
    analyzed_plugin_count: int = 0
    findings: list[PluginVisibilityFinding] = field(default_factory=list)
    issues: list[PluginVisibilityIssue] = field(default_factory=list)

    def to_json_dict(self) -> dict[str, object]:
        classes = Counter(item.classification for item in self.findings)
        source_types = Counter(item.source_kind for item in self.findings)
        return {
            "active_plugin_count": self.active_plugin_count,
            "analyzed_plugin_count": self.analyzed_plugin_count,
            "source_types_discovered": dict(sorted(source_types.items())),
            "classification_counts": {
                name.value: classes[name.value] for name in VisibilityClassification
            },
            "findings": [item.to_json_dict() for item in self.findings],
            "issues": [asdict(item) for item in self.issues],
        }


@dataclass(frozen=True, slots=True)
class _Binding:
    identity: str
    kind: str
    access: str
    transforms: tuple[str, ...] = ()
    uncertain: bool = False


@dataclass(slots=True)
class _Usage:
    bindings: list[_Binding] = field(default_factory=list)
    visible: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    internal: list[str] = field(default_factory=list)
    unsafe: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)


_ASSIGNMENT = re.compile(
    r"\b(?:var|let|const)\s+([A-Za-z_$][\w$]*)\s*=\s*([^;]+);",
    re.M,
)
_PARAMETERS_CALL = re.compile(
    r"PluginManager\.parameters\s*\(\s*(['\"])(?P<name>.*?)\1\s*\)"
)
_LITERAL_PROPERTY = re.compile(
    r"^(?P<base>[A-Za-z_$][\w$]*)\s*(?:\.\s*(?P<dot>[A-Za-z_$][\w$]*)|"
    r"\[\s*(['\"])(?P<bracket>.*?)\3\s*\])$"
)
_DYNAMIC_PROPERTY = re.compile(
    r"^(?P<base>[A-Za-z_$][\w$]*)\s*\[\s*(?!['\"])(?P<key>[^\]]+)\]$"
)
_NOTE_ACCESS = re.compile(r"\b(?P<base>[A-Za-z_$][\w$]*)\.note\b")
_META_ACCESS = re.compile(
    r"\b(?:(?P<object>[A-Za-z_$][\w$]*)\.)?meta(?:\.\s*(?P<dot>[A-Za-z_$][\w$]*)|"
    r"\[\s*(['\"])(?P<bracket>.*?)\3\s*\])"
)
_DYNAMIC_META_ACCESS = re.compile(
    r"\b(?:(?P<object>[A-Za-z_$][\w$]*)\.)?meta\s*\[\s*(?!['\"])([^\]]+)\]"
)
_TRANSFORMS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\.match\s*\(\s*/[^/\n]+/[gimuy]*\s*\)"), "literal_regex_match"),
    (re.compile(r"\.split\s*\(\s*(['\"])[\s\S]*?\1\s*\)"), "literal_split"),
    (re.compile(r"\.slice\s*\(\s*\d+(?:\s*,\s*\d+)?\s*\)"), "literal_slice"),
    (re.compile(r"\.substr\s*\(\s*\d+(?:\s*,\s*\d+)?\s*\)"), "literal_substr"),
    (re.compile(r"\.substring\s*\(\s*\d+(?:\s*,\s*\d+)?\s*\)"), "literal_substring"),
    (re.compile(r"\.replace\s*\(\s*(?:/[^/\n]+/[gimuy]*|(['\"])[\s\S]*?\1)"), "literal_replace"),
)
_CAPTURE = re.compile(r"\[\s*(\d+)\s*\]")
_INTERNAL_CALLS = {"Number", "parseInt", "parseFloat", "Boolean"}
_CALLBACK_PATTERN = re.compile(r"\.(?:forEach|map|filter|reduce|some|every)\s*\(")
_DISPLAY_CONFIG = re.compile(r"(?:color|font|align|position|size|style|config)", re.I)


def analyze_plugin_visibility(
    inventory: PluginInventory,
    *,
    command_discovery: MvPluginDiscoveryReport | None = None,
    max_helper_depth: int = MAX_HELPER_DEPTH,
) -> PluginVisibilityReport:
    """Analyze explicitly active plugins and return hash-free behavioral evidence."""

    report = PluginVisibilityReport(len(inventory.active_plugins))
    for issue in inventory.issues:
        if issue.plugin_name is not None and any(
            item.name == issue.plugin_name and item.enabled is True
            for item in inventory.plugins
        ):
            report.issues.append(
                PluginVisibilityIssue(
                    issue.severity,
                    issue.code,
                    issue.reason,
                    issue.plugin_name,
                    issue.source_file,
                )
            )
    for plugin in inventory.active_plugins:
        if not plugin.source_available or plugin.source_path is None:
            continue
        try:
            source = plugin.source_path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            report.issues.append(
                PluginVisibilityIssue(
                    "warning",
                    "PLUGIN_SOURCE_READ_ERROR",
                    str(exc),
                    plugin.name,
                    plugin.source_file,
                )
            )
            continue
        report.analyzed_plugin_count += 1
        report.findings.extend(
            _analyze_source(plugin, _strip_comments(source), max_helper_depth)
        )

    if command_discovery is not None:
        report.findings.extend(_command_findings(command_discovery))
    report.findings = _resolve_cross_plugin_conflicts(report.findings)
    report.findings.sort(
        key=lambda item: (
            item.plugin_name.casefold(),
            item.source_kind,
            item.source_access.casefold(),
            item.classification,
        )
    )
    return report


def _analyze_source(
    plugin: PluginRecord,
    source: str,
    max_helper_depth: int,
) -> list[PluginVisibilityFinding]:
    functions = _extract_functions(source)
    parameter_containers: dict[str, str] = {}
    meta_containers: dict[str, str] = {}
    env: dict[str, _Binding] = {}

    for name, expression in _assignments(source):
        parameter = _PARAMETERS_CALL.fullmatch(expression.strip())
        if parameter is not None:
            parameter_containers[name] = parameter.group("name")
            continue
        meta_root = re.fullmatch(r"([A-Za-z_$][\w$]*)\.meta", expression.strip())
        if meta_root is not None:
            meta_containers[name] = meta_root.group(1)

    # Resolve aliases and simple transforms with a small fixed-point bound.
    assignments = list(_assignments(source))
    for _ in range(4):
        changed = False
        for name, expression in assignments:
            binding = _binding_from_expression(
                expression, env, parameter_containers, meta_containers
            )
            if binding is not None and env.get(name) != binding:
                env[name] = binding
                changed = True
            if expression.strip() in parameter_containers:
                parameter_containers[name] = parameter_containers[expression.strip()]
            if expression.strip() in meta_containers:
                meta_containers[name] = meta_containers[expression.strip()]
        if not changed:
            break

    bindings = _collect_bindings(source, env, parameter_containers, meta_containers)
    usages: dict[str, _Usage] = defaultdict(_Usage)
    for binding in bindings:
        usages[binding.identity].bindings.append(binding)

    helper_summaries = {
        function.name: _helper_sink_summary(
            function, functions, max_helper_depth, set()
        )
        for function in functions
    }
    for callee, arguments in _iter_named_calls(source):
        short = _short_function_name(callee)
        direct = _direct_sink(callee, include_heuristic=False)
        if direct is not None:
            argument_index, sink = direct
            if argument_index < len(arguments):
                binding = _binding_from_expression(
                    arguments[argument_index], env, parameter_containers, meta_containers
                )
                if binding is not None:
                    usages[binding.identity].visible.append((sink, ()))
            continue
        helper = _resolve_function(callee, functions)
        summaries = helper_summaries.get(helper.name, ()) if helper else ()
        for parameter_index, sink, chain in summaries:
            if parameter_index >= len(arguments):
                continue
            binding = _binding_from_expression(
                arguments[parameter_index], env, parameter_containers, meta_containers
            )
            if binding is not None:
                usages[binding.identity].visible.append((sink, chain))
        if helper is None:
            heuristic = _direct_sink(callee)
            if heuristic is not None:
                argument_index, sink = heuristic
                if argument_index < len(arguments):
                    binding = _binding_from_expression(
                        arguments[argument_index], env, parameter_containers, meta_containers
                    )
                    if binding is not None:
                        usages[binding.identity].visible.append((sink, ()))
        if short in _INTERNAL_CALLS and arguments:
            binding = _binding_from_expression(
                arguments[0], env, parameter_containers, meta_containers
            )
            if binding is not None:
                usages[binding.identity].internal.append(short)
        if short == "eval" and arguments:
            binding = _binding_from_expression(
                arguments[0], env, parameter_containers, meta_containers
            )
            if binding is not None:
                usages[binding.identity].unsafe.append("eval")

    for binding in bindings:
        probes = [binding.access]
        probes.extend(name for name, value in env.items() if value.identity == binding.identity)
        for probe in probes:
            escaped = re.escape(probe)
            if re.search(rf"(?:{escaped})\s*(?:===|!==|==|!=|\.indexOf\s*\(|\.includes\s*\()", source):
                usages[binding.identity].internal.append("comparison/control use")
            if re.search(rf"\b(?:Number|parseInt|parseFloat|Boolean)\s*\(\s*{escaped}\b", source):
                usages[binding.identity].internal.append("numeric/config conversion")
            if re.search(rf"\beval\s*\([^)]*\b{escaped}\b", source):
                usages[binding.identity].unsafe.append("eval")
            if _CALLBACK_PATTERN.search(source) and re.search(rf"\b{escaped}\b", source):
                usages[binding.identity].unresolved.append("callback ownership is unresolved")
        if binding.uncertain:
            usages[binding.identity].unresolved.append("dynamic source access")

    findings: list[PluginVisibilityFinding] = []
    for identity, usage in usages.items():
        representative = sorted(
            usage.bindings,
            key=lambda item: (item.uncertain, len(item.transforms), item.access),
        )[0]
        transforms = tuple(
            dict.fromkeys(
                transform
                for item in usage.bindings
                for transform in item.transforms
            )
        )
        visible = usage.visible
        mixed = bool(visible and (usage.internal or usage.unsafe))
        if usage.unsafe:
            classification = VisibilityClassification.UNSAFE
            reason = "source reaches evaluated or syntax-unsafe use"
        elif visible and not mixed and not usage.unresolved:
            classification = VisibilityClassification.VERIFIED_VISIBLE
            reason = "deterministic bounded source-to-display flow"
        elif visible:
            classification = VisibilityClassification.CONDITIONAL_VISIBLE
            reason = (
                "display flow exists but source also has mixed or unresolved usage"
            )
        elif usage.internal and not usage.unresolved:
            classification = VisibilityClassification.INTERNAL
            reason = "source is consumed only by control/config/identifier logic"
        elif usage.unresolved:
            classification = VisibilityClassification.UNKNOWN
            reason = "; ".join(dict.fromkeys(usage.unresolved))
        else:
            classification = VisibilityClassification.UNKNOWN
            reason = "source consumption does not reach a recognized sink"
        sink, chain = visible[0] if visible else (None, ())
        findings.append(
            PluginVisibilityFinding(
                plugin_name=plugin.name,
                plugin_file=plugin.source_file or f"js/plugins/{plugin.name}.js",
                source_kind=representative.kind,
                source_access=representative.access,
                transform_evidence=transforms,
                helper_chain=chain,
                helper_depth=len(chain),
                sink=sink,
                classification=classification.value,
                reason=reason,
                source_identity=identity,
                mixed_use=mixed,
            )
        )
    return findings


def _assignments(source: str) -> Iterator[tuple[str, str]]:
    for match in _ASSIGNMENT.finditer(source):
        yield match.group(1), match.group(2).strip()


def _collect_bindings(
    source: str,
    env: dict[str, _Binding],
    parameter_containers: dict[str, str],
    meta_containers: dict[str, str],
) -> list[_Binding]:
    found: dict[tuple[str, str, tuple[str, ...], bool], _Binding] = {}
    for binding in env.values():
        found[
            (binding.identity, binding.access, binding.transforms, binding.uncertain)
        ] = binding
    for expression in _source_expressions(source, parameter_containers, meta_containers):
        binding = _binding_from_expression(
            expression, env, parameter_containers, meta_containers
        )
        if binding is not None:
            found[
                (binding.identity, binding.access, binding.transforms, binding.uncertain)
            ] = binding
    return list(found.values())


def _source_expressions(
    source: str,
    parameter_containers: dict[str, str],
    meta_containers: dict[str, str],
) -> Iterator[str]:
    for container in parameter_containers:
        pattern = re.compile(
            rf"\b{re.escape(container)}\s*(?:\.\s*[A-Za-z_$][\w$]*|\[\s*(?:['\"][^'\"]*['\"]|[^\]]+)\s*\])"
        )
        yield from (match.group(0) for match in pattern.finditer(source))
    yield from (match.group(0) for match in _NOTE_ACCESS.finditer(source))
    yield from (match.group(0) for match in _META_ACCESS.finditer(source))
    yield from (match.group(0) for match in _DYNAMIC_META_ACCESS.finditer(source))
    for container in meta_containers:
        pattern = re.compile(
            rf"\b{re.escape(container)}\s*(?:\.\s*[A-Za-z_$][\w$]*|\[\s*(?:['\"][^'\"]*['\"]|[^\]]+)\s*\])"
        )
        yield from (match.group(0) for match in pattern.finditer(source))


def _binding_from_expression(
    expression: str,
    env: dict[str, _Binding],
    parameter_containers: dict[str, str],
    meta_containers: dict[str, str],
) -> _Binding | None:
    text = _strip_wrapping_parentheses(expression.strip())
    if text in env:
        return env[text]

    direct_parameter = re.fullmatch(
        r"PluginManager\.parameters\s*\(\s*(['\"])(?P<plugin>.*?)\1\s*\)\s*"
        r"(?:\.\s*(?P<dot>[A-Za-z_$][\w$]*)|\[\s*(['\"])(?P<bracket>.*?)\4\s*\])",
        text,
    )
    if direct_parameter is not None:
        key = direct_parameter.group("dot") or direct_parameter.group("bracket")
        return _Binding(
            f"plugin_parameter:{direct_parameter.group('plugin')}:{key}",
            "plugin_parameter",
            f"PluginManager.parameters(<literal>)[{key!r}]",
        )

    property_match = _LITERAL_PROPERTY.fullmatch(text)
    if property_match is not None:
        base = property_match.group("base")
        key = property_match.group("dot") or property_match.group("bracket")
        if base in parameter_containers:
            namespace = parameter_containers[base]
            return _Binding(
                f"plugin_parameter:{namespace}:{key}",
                "plugin_parameter",
                f"{base}[{key!r}]",
            )
        if base in meta_containers:
            owner = meta_containers[base]
            return _Binding(
                f"meta:{owner}:{key}", "meta", f"{base}[{key!r}]"
            )
    dynamic = _DYNAMIC_PROPERTY.fullmatch(text)
    if dynamic is not None and dynamic.group("base") in parameter_containers:
        base = dynamic.group("base")
        return _Binding(
            f"plugin_parameter_dynamic:{parameter_containers[base]}",
            "plugin_parameter",
            f"{base}[<dynamic>]",
            ("dynamic_property",),
            True,
        )

    note = _NOTE_ACCESS.fullmatch(text)
    if note is not None:
        base = note.group("base")
        return _Binding(f"note:{base}", "note", f"{base}.note")
    meta = _META_ACCESS.fullmatch(text)
    if meta is not None:
        owner = meta.group("object") or "meta"
        key = meta.group("dot") or meta.group("bracket")
        return _Binding(f"meta:{owner}:{key}", "meta", text)
    dynamic_meta = _DYNAMIC_META_ACCESS.fullmatch(text)
    if dynamic_meta is not None:
        owner = dynamic_meta.group("object") or "meta"
        return _Binding(
            f"meta_dynamic:{owner}",
            "meta",
            f"{owner}.meta[<dynamic>]",
            ("dynamic_property",),
            True,
        )

    embedded: list[_Binding] = []
    for candidate in _source_expressions(text, parameter_containers, meta_containers):
        if candidate == text:
            continue
        binding = _binding_from_expression(
            candidate, {}, parameter_containers, meta_containers
        )
        if binding is not None:
            embedded.append(binding)
    referenced = embedded + [
        binding
        for name, binding in env.items()
        if re.search(rf"\b{re.escape(name)}\b", text)
    ]
    identities = {item.identity for item in referenced}
    if len(identities) != 1:
        return None
    base = referenced[0]
    transforms = list(base.transforms)
    for pattern, label in _TRANSFORMS:
        if pattern.search(text):
            transforms.append(label)
    capture = _CAPTURE.search(text)
    if capture and "literal_regex_match" in transforms:
        transforms.append(f"capture[{capture.group(1)}]")
    uncertain = base.uncertain or bool(_CALLBACK_PATTERN.search(text))
    return replace(
        base,
        transforms=tuple(dict.fromkeys(transforms)),
        uncertain=uncertain,
    )


def _strip_wrapping_parentheses(value: str) -> str:
    text = value
    while text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    return text


def _direct_sink(
    callee: str,
    *,
    include_heuristic: bool = True,
) -> tuple[int, str] | None:
    compact = re.sub(r"\s+", "", callee)
    short = _short_function_name(compact)
    folded = compact.casefold()
    if short in {"drawText", "drawTextEx", "addCommand"}:
        return 0, short
    if compact == "$gameMessage.add":
        return 0, compact
    if re.search(r"(?:Window_Help|helpWindow).*\.setText$", compact, re.I):
        return 0, compact
    if re.search(r"(?:PIXI\.Text|Sprite_Text)$", compact):
        return 0, compact
    if not include_heuristic:
        return None
    if _DISPLAY_CONFIG.search(short):
        return None
    if (
        re.search(r"(?:draw|render|show|create)", short, re.I)
        and re.search(r"(?:text|message|label|caption|help|popup|notice)", short, re.I)
    ):
        return 0, compact
    return None


def _helper_sink_summary(
    function: object,
    functions: list[object],
    max_depth: int,
    visited: set[str],
) -> tuple[tuple[int, str, tuple[str, ...]], ...]:
    name = getattr(function, "name")
    if name in visited or max_depth < 0:
        return ()
    parameters = tuple(getattr(function, "parameters"))
    body = getattr(function, "body")
    aliases = _parameter_aliases(body, parameters)
    result: list[tuple[int, str, tuple[str, ...]]] = []
    for callee, arguments in _iter_named_calls(body):
        direct = _direct_sink(callee, include_heuristic=False)
        if direct is not None:
            argument_index, sink = direct
            if argument_index < len(arguments):
                for index in _parameter_indices(arguments[argument_index], parameters, aliases):
                    result.append((index, sink, (_short_function_name(name),)))
            continue
        helper = _resolve_function(callee, functions)
        if helper is not None and max_depth > 0:
            nested = _helper_sink_summary(
                helper, functions, max_depth - 1, visited | {name}
            )
            for nested_index, sink, chain in nested:
                if nested_index >= len(arguments):
                    continue
                for index in _parameter_indices(arguments[nested_index], parameters, aliases):
                    result.append((index, sink, (_short_function_name(name),) + chain))
            continue
        heuristic = _direct_sink(callee)
        if heuristic is not None:
            argument_index, sink = heuristic
            if argument_index >= len(arguments):
                continue
            for index in _parameter_indices(arguments[argument_index], parameters, aliases):
                result.append((index, sink, (_short_function_name(name),)))
    return tuple(dict.fromkeys(result))


def _parameter_aliases(body: str, parameters: tuple[str, ...]) -> dict[str, int]:
    aliases = {name: index for index, name in enumerate(parameters)}
    changed = True
    while changed:
        changed = False
        for name, expression in _assignments(body):
            matches = _parameter_indices(expression, parameters, aliases)
            if len(matches) == 1 and aliases.get(name) != matches[0]:
                aliases[name] = matches[0]
                changed = True
    return aliases


def _parameter_indices(
    expression: str,
    parameters: tuple[str, ...],
    aliases: dict[str, int],
) -> tuple[int, ...]:
    result = {
        index
        for name, index in aliases.items()
        if re.search(rf"\b{re.escape(name)}\b", expression)
    }
    return tuple(sorted(result))


def _command_findings(
    discovery: MvPluginDiscoveryReport,
) -> list[PluginVisibilityFinding]:
    mapping = {
        APPLY_VERIFIED: VisibilityClassification.VERIFIED_VISIBLE,
        DISCOVERED_VERIFIED: VisibilityClassification.CONDITIONAL_VISIBLE,
        COMMAND_INTERNAL: VisibilityClassification.INTERNAL,
        COMMAND_UNSAFE: VisibilityClassification.UNSAFE,
        COMMAND_UNKNOWN: VisibilityClassification.UNKNOWN,
    }
    result: list[PluginVisibilityFinding] = []
    for item in discovery.observations:
        classification = mapping.get(
            item.classification, VisibilityClassification.UNKNOWN
        )
        result.append(
            PluginVisibilityFinding(
                plugin_name=item.plugin_name,
                plugin_file=f"js/plugins/{item.plugin_file}",
                source_kind="plugin_command",
                source_access=f"pluginCommand({item.command!r})",
                transform_evidence=(item.argument_mode,),
                helper_chain=item.helper_chain,
                helper_depth=len(item.helper_chain),
                sink=item.sink,
                classification=classification.value,
                reason=item.unresolved_reason
                or "existing bounded MV plugin-command flow evidence",
                source_identity=f"plugin_command:{item.command_normalization}:{item.command}",
                mixed_use=False,
            )
        )
    return result


def _resolve_cross_plugin_conflicts(
    findings: list[PluginVisibilityFinding],
) -> list[PluginVisibilityFinding]:
    grouped: dict[str, list[PluginVisibilityFinding]] = defaultdict(list)
    for item in findings:
        grouped[item.source_identity].append(item)
    result: list[PluginVisibilityFinding] = []
    for items in grouped.values():
        plugins = {item.plugin_name for item in items}
        classes = {item.classification for item in items}
        visible = bool(
            classes
            & {
                VisibilityClassification.VERIFIED_VISIBLE.value,
                VisibilityClassification.CONDITIONAL_VISIBLE.value,
            }
        )
        nonvisible = bool(
            classes
            & {
                VisibilityClassification.INTERNAL.value,
                VisibilityClassification.UNSAFE.value,
                VisibilityClassification.UNKNOWN.value,
            }
        )
        if len(plugins) > 1 and visible and nonvisible:
            names = tuple(sorted(plugins, key=str.casefold))
            for item in items:
                classification = (
                    VisibilityClassification.UNSAFE.value
                    if item.classification == VisibilityClassification.UNSAFE.value
                    else VisibilityClassification.CONDITIONAL_VISIBLE.value
                )
                result.append(
                    replace(
                        item,
                        classification=classification,
                        mixed_use=True,
                        ambiguity=names,
                        reason="multiple active plugins consume the same source with conflicting semantics",
                    )
                )
        else:
            result.extend(items)
    return result


__all__ = [
    "PluginVisibilityFinding",
    "PluginVisibilityIssue",
    "PluginVisibilityReport",
    "VisibilityClassification",
    "analyze_plugin_visibility",
]
