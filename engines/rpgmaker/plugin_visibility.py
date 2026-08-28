"""Conservative static evidence for plugin-consumed player-visible values.

This Phase-1 analyzer is intentionally lexical and bounded.  It never executes
JavaScript and it does not create translation entries.  Findings describe an
explainable source-to-sink path that a later contract layer may consume.
"""

from __future__ import annotations

import json
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
MAX_PROPERTY_HOPS = 2


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
    resolved_key: str | None = None
    resolution_path: tuple[str, ...] = ()
    state_path: tuple[str, ...] = ()
    property_hops: int = 0

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


@dataclass(frozen=True, slots=True)
class _FlowEdge:
    target: str
    transforms: tuple[str, ...] = ()
    helper: str | None = None
    property_hop: int = 0
    evidence: str | None = None
    uncertain: bool = False


@dataclass(frozen=True, slots=True)
class _FlowSink:
    kind: str
    name: str


@dataclass(frozen=True, slots=True)
class _DynamicMetaSource:
    node: str
    identity: str
    owner: str
    key: str | None
    access: str
    resolution_path: tuple[str, ...]
    scope: str
    raw_access: str
    unresolved_reason: str | None = None


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
_BOUNDED_DYNAMIC_META_ACCESS = re.compile(
    r"(?P<receiver>(?:[A-Za-z_$][\w$]*\.)*[A-Za-z_$][\w$]*(?:\(\))?)"
    r"\.meta\s*\[\s*(?!['\"])(?P<key>[^\]]+)\]"
)
_FLOW_ASSIGNMENT = re.compile(
    r"(?<![\w$.])(?:(?:var|let|const)\s+)?"
    r"(?P<lhs>(?:this|[A-Za-z_$][\w$]*)(?:\.[A-Za-z_$][\w$]*)*)"
    r"\s*(?<![=!<>])=(?!=)\s*(?P<rhs>[^;]+);",
    re.M,
)
_RETURN_EXPRESSION = re.compile(r"\breturn\s+(?P<value>[^;]+);")
_PROPERTY_EXPRESSION = re.compile(
    r"\b(?P<value>(?:this|[A-Za-z_$][\w$]*)(?:\.[A-Za-z_$][\w$]*)+)"
)
_STRING_LITERAL = re.compile(r"^(['\"])(?P<value>(?:\\.|(?!\1).)*)\1$")
_SEGMENT_ACCESS = re.compile(r"\[\s*(?P<index>\d+)\s*\]")
_KNOWN_META_OWNERS = frozenset(
    {"actor", "class", "item", "weapon", "armor", "skill", "state", "enemy", "event"}
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
    dynamic_findings = _bounded_dynamic_meta_findings(
        plugin,
        source,
        functions,
        max_helper_depth=max_helper_depth,
    )
    if dynamic_findings:
        findings = [
            item
            for item in findings
            if not item.source_identity.startswith("meta_dynamic:")
        ]
        findings.extend(dynamic_findings)
    return findings


def _bounded_dynamic_meta_findings(
    plugin: PluginRecord,
    source: str,
    functions: list[object],
    *,
    max_helper_depth: int,
) -> list[PluginVisibilityFinding]:
    """Trace only resolved dynamic-meta values through a small lexical graph.

    The graph is deliberately narrower than JavaScript semantics: local variables,
    direct calls, unique method names, and at most two direct object-property writes.
    It does not model callbacks, containers, prototype mutation, or runtime keys.
    """

    contexts = _flow_contexts(source, functions)
    literal_helpers = _literal_helper_returns(functions)
    constants = {
        scope: _literal_constants(body, parameters, plugin, literal_helpers)
        for scope, _, body, parameters in contexts
    }
    dynamic_sources = _dynamic_meta_sources(
        contexts,
        functions,
        constants,
        literal_helpers,
    )
    if not dynamic_sources:
        return []

    edges: dict[str, list[_FlowEdge]] = defaultdict(list)
    sinks: dict[str, list[_FlowSink]] = defaultdict(list)
    property_writers: dict[str, set[str]] = defaultdict(set)
    property_reads: dict[str, tuple[str, str]] = {}
    local_names: dict[str, set[str]] = {}

    sources_by_scope: dict[str, list[_DynamicMetaSource]] = defaultdict(list)
    for item in dynamic_sources:
        sources_by_scope[item.scope].append(item)

    for scope, owner, body, parameters in contexts:
        names = set(parameters)
        assignments = list(_flow_assignments(body))
        for lhs, _ in assignments:
            if "." not in lhs:
                names.add(lhs)
        local_names[scope] = names

        for lhs, rhs in assignments:
            target = _flow_target_node(lhs, scope, owner)
            if target is None:
                continue
            transforms = _flow_transforms(rhs)
            dependencies = _flow_expression_nodes(
                rhs,
                scope,
                owner,
                names,
                functions,
                sources_by_scope.get(scope, ()),
                property_reads,
            )
            property_hop = 1 if target.startswith("prop:") else 0
            if target.startswith("prop:"):
                property_writers[target].add(_normalized_expression(rhs))
            for dependency in dependencies:
                edges[dependency].append(
                    _FlowEdge(
                        target,
                        transforms,
                        property_hop=property_hop,
                        evidence=(
                            f"property_write:{target.removeprefix('prop:')}"
                            if property_hop
                            else None
                        ),
                    )
                )

        return_node = f"return:{scope}"
        for match in _RETURN_EXPRESSION.finditer(body):
            value = match.group("value")
            for dependency in _flow_expression_nodes(
                value,
                scope,
                owner,
                names,
                functions,
                sources_by_scope.get(scope, ()),
                property_reads,
            ):
                edges[dependency].append(
                    _FlowEdge(return_node, _flow_transforms(value))
                )

        for callee, arguments in _iter_named_calls(body):
            direct = _direct_sink(callee, include_heuristic=False)
            if direct is None:
                direct = _direct_sink(callee)
            if direct is not None:
                argument_index, sink = direct
                if argument_index < len(arguments):
                    for dependency in _flow_expression_nodes(
                        arguments[argument_index],
                        scope,
                        owner,
                        names,
                        functions,
                        sources_by_scope.get(scope, ()),
                        property_reads,
                    ):
                        sinks[dependency].append(_FlowSink("visible", sink))
                continue

            short = _short_function_name(callee)
            if short in _INTERNAL_CALLS | {"eval"} and arguments:
                kind = "unsafe" if short == "eval" else "internal"
                for dependency in _flow_expression_nodes(
                    arguments[0],
                    scope,
                    owner,
                    names,
                    functions,
                    sources_by_scope.get(scope, ()),
                    property_reads,
                ):
                    sinks[dependency].append(_FlowSink(kind, short))

            helper = _resolve_flow_function(callee, functions)
            if helper is None:
                continue
            helper_scope = getattr(helper, "name")
            helper_parameters = tuple(getattr(helper, "parameters"))
            for index, argument in enumerate(arguments[: len(helper_parameters)]):
                target = f"var:{helper_scope}:{helper_parameters[index]}"
                for dependency in _flow_expression_nodes(
                    argument,
                    scope,
                    owner,
                    names,
                    functions,
                    sources_by_scope.get(scope, ()),
                    property_reads,
                ):
                    edges[dependency].append(
                        _FlowEdge(
                            target,
                            _flow_transforms(argument),
                            helper=_short_function_name(helper_scope),
                            evidence=f"direct_call:{_short_function_name(helper_scope)}",
                        )
                    )

        _add_literal_comparison_sinks(body, scope, owner, names, sinks)

    conflicted_properties = {
        node for node, writers in property_writers.items() if len(writers) > 1
    }
    properties_by_leaf: dict[str, list[str]] = defaultdict(list)
    for node in property_writers:
        property_path = node.rsplit(":", 1)[-1]
        properties_by_leaf[property_path.rsplit(".", 1)[-1]].append(node)
    for read_node, (reader_owner, path) in property_reads.items():
        if read_node in property_writers:
            continue
        leaf = path.rsplit(".", 1)[-1]
        candidates = list(properties_by_leaf.get(leaf, ()))
        if "." in path:
            candidates = [
                item
                for item in candidates
                if not item.startswith(f"prop:{reader_owner}:")
            ]
        uncertain = len(candidates) != 1
        for candidate in candidates:
            edges[candidate].append(
                _FlowEdge(
                    read_node,
                    evidence=(
                        "unique_property_receiver"
                        if not uncertain
                        else "ambiguous_property_receiver"
                    ),
                    uncertain=uncertain,
                )
            )

    results: list[PluginVisibilityFinding] = []
    for dynamic_source in dynamic_sources:
        if dynamic_source.key is None:
            results.append(
                PluginVisibilityFinding(
                    plugin_name=plugin.name,
                    plugin_file=plugin.source_file
                    or f"js/plugins/{plugin.name}.js",
                    source_kind="meta",
                    source_access=dynamic_source.access,
                    transform_evidence=("dynamic_property",),
                    helper_chain=(),
                    helper_depth=0,
                    sink=None,
                    classification=VisibilityClassification.UNKNOWN.value,
                    reason=dynamic_source.unresolved_reason
                    or "dynamic meta key is not deterministically resolved",
                    source_identity=dynamic_source.identity,
                    resolution_path=dynamic_source.resolution_path,
                )
            )
            continue
        results.extend(
            _trace_dynamic_source(
                plugin,
                dynamic_source,
                edges,
                sinks,
                conflicted_properties,
                max_helper_depth=max_helper_depth,
            )
        )

    unique: dict[tuple[object, ...], PluginVisibilityFinding] = {}
    for item in results:
        key = (
            item.source_identity,
            item.classification,
            item.sink,
            item.transform_evidence,
        )
        unique.setdefault(key, item)
    return list(unique.values())


def _flow_contexts(
    source: str,
    functions: list[object],
) -> list[tuple[str, str, str, tuple[str, ...]]]:
    contexts: list[tuple[str, str, str, tuple[str, ...]]] = [
        ("<global>", "<global>", _outside_function_bodies(source, functions), ())
    ]
    for function in functions:
        name = str(getattr(function, "name"))
        owner_match = re.match(r"(?P<owner>.+?)\.prototype\.[^.]+$", name)
        owner = owner_match.group("owner") if owner_match else name
        contexts.append(
            (
                name,
                owner,
                str(getattr(function, "body")),
                tuple(getattr(function, "parameters")),
            )
        )
    return contexts


def _outside_function_bodies(source: str, functions: list[object]) -> str:
    characters = list(source)
    search_start = 0
    for function in functions:
        body = str(getattr(function, "body"))
        start = source.find(body, search_start)
        if start < 0:
            start = source.find(body)
        if start < 0:
            continue
        characters[start : start + len(body)] = " " * len(body)
        search_start = start + len(body)
    return "".join(characters)


def _literal_helper_returns(functions: list[object]) -> dict[str, str]:
    by_short: dict[str, list[str]] = defaultdict(list)
    for function in functions:
        returns = [
            _decode_js_string_literal(match.group("value").strip())
            for match in _RETURN_EXPRESSION.finditer(str(getattr(function, "body")))
        ]
        resolved = {item for item in returns if item is not None}
        if len(returns) == 1 and len(resolved) == 1:
            by_short[_short_function_name(str(getattr(function, "name")))].append(
                next(iter(resolved))
            )
    return {
        name: values[0]
        for name, values in by_short.items()
        if len(set(values)) == 1
    }


def _literal_constants(
    body: str,
    parameters: tuple[str, ...],
    plugin: PluginRecord,
    literal_helpers: dict[str, str],
) -> dict[str, str]:
    del parameters
    assignments = list(_flow_assignments(body))
    parameter_containers: dict[str, str] = {}
    for lhs, rhs in assignments:
        if "." in lhs:
            continue
        match = _PARAMETERS_CALL.fullmatch(rhs.strip())
        if match is not None:
            parameter_containers[lhs] = match.group("name")

    constants: dict[str, str] = {}
    for _ in range(4):
        changed = False
        for lhs, rhs in assignments:
            if "." in lhs:
                continue
            value = _resolve_literal_expression(
                rhs,
                constants,
                literal_helpers,
                parameter_containers,
                plugin,
            )
            if value is not None and constants.get(lhs) != value:
                constants[lhs] = value
                changed = True
        if not changed:
            break
    return constants


def _resolve_literal_expression(
    expression: str,
    constants: dict[str, str],
    literal_helpers: dict[str, str],
    parameter_containers: dict[str, str] | None = None,
    plugin: PluginRecord | None = None,
) -> str | None:
    text = _strip_wrapping_parentheses(expression.strip())
    literal = _decode_js_string_literal(text)
    if literal is not None:
        return literal
    if text in constants:
        return constants[text]
    call = re.fullmatch(
        r"(?:this\.)?(?P<name>[A-Za-z_$][\w$]*)\s*\(\s*\)", text
    )
    if call is not None:
        return literal_helpers.get(call.group("name"))
    if parameter_containers and plugin is not None:
        match = _LITERAL_PROPERTY.fullmatch(text)
        if match is not None and match.group("base") in parameter_containers:
            key = match.group("dot") or match.group("bracket")
            value = plugin.parameters.get(key)
            return value if isinstance(value, str) else None
    return None


def _decode_js_string_literal(value: str) -> str | None:
    match = _STRING_LITERAL.fullmatch(value)
    if match is None:
        return None
    quote = value[0]
    payload = value[1:-1]
    output: list[str] = []
    index = 0
    escapes = {
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "b": "\b",
        "f": "\f",
        "v": "\v",
        "0": "\0",
        "\\": "\\",
        "/": "/",
        quote: quote,
    }
    while index < len(payload):
        character = payload[index]
        if character != "\\":
            output.append(character)
            index += 1
            continue
        if index + 1 >= len(payload):
            return None
        escaped = payload[index + 1]
        if escaped in escapes:
            output.append(escapes[escaped])
            index += 2
            continue
        if escaped in {"u", "x"}:
            width = 4 if escaped == "u" else 2
            digits = payload[index + 2 : index + 2 + width]
            if len(digits) != width or not re.fullmatch(r"[0-9A-Fa-f]+", digits):
                return None
            output.append(chr(int(digits, 16)))
            index += 2 + width
            continue
        return None
    return "".join(output)


def _dynamic_meta_sources(
    contexts: list[tuple[str, str, str, tuple[str, ...]]],
    functions: list[object],
    constants: dict[str, dict[str, str]],
    literal_helpers: dict[str, str],
) -> list[_DynamicMetaSource]:
    result: list[_DynamicMetaSource] = []
    context_by_scope = {scope: (owner, body, parameters) for scope, owner, body, parameters in contexts}
    for scope, _, body, parameters in contexts:
        meta_aliases: dict[str, tuple[str, str]] = {}
        for lhs, rhs in _flow_assignments(body):
            if "." in lhs:
                continue
            match = re.fullmatch(
                r"(?P<receiver>(?:[A-Za-z_$][\w$]*\.)*[A-Za-z_$][\w$]*(?:\(\))?)\.meta",
                rhs.strip(),
            )
            if match is not None:
                meta_aliases[lhs] = (
                    _dynamic_meta_owner(match.group("receiver")),
                    match.group("receiver"),
                )

        sites: list[tuple[int, str, str, str]] = []
        for match in _BOUNDED_DYNAMIC_META_ACCESS.finditer(body):
            sites.append(
                (
                    match.start(),
                    match.group(0),
                    _dynamic_meta_owner(match.group("receiver")),
                    match.group("key").strip(),
                )
            )
        for alias, (owner, receiver) in meta_aliases.items():
            pattern = re.compile(
                rf"\b{re.escape(alias)}\s*\[\s*(?!['\"])(?P<key>[^\]]+)\]"
            )
            for match in pattern.finditer(body):
                sites.append((match.start(), match.group(0), owner, match.group("key").strip()))

        for ordinal, (_, raw_access, owner, key_expression) in enumerate(sorted(sites)):
            key = _resolve_literal_expression(
                key_expression,
                constants.get(scope, {}),
                literal_helpers,
            )
            resolution_path: tuple[str, ...] = ()
            unresolved_reason: str | None = None
            if key is not None:
                resolution_path = ("dynamic_meta_key", "fixed_literal_or_alias")
            elif key_expression in parameters:
                parameter_index = parameters.index(key_expression)
                call_values: list[str] = []
                for caller_scope, _, caller_body, _ in contexts:
                    for callee, arguments in _iter_named_calls(caller_body):
                        resolved = _resolve_flow_function(callee, functions)
                        if resolved is None or getattr(resolved, "name") != scope:
                            continue
                        if parameter_index >= len(arguments):
                            continue
                        value = _resolve_literal_expression(
                            arguments[parameter_index],
                            constants.get(caller_scope, {}),
                            literal_helpers,
                        )
                        if value is not None:
                            call_values.append(value)
                unique_values = sorted(set(call_values))
                if len(unique_values) == 1:
                    key = unique_values[0]
                    resolution_path = (
                        "dynamic_meta_key",
                        f"function_parameter:{key_expression}",
                        "fixed_call_argument",
                    )
                else:
                    unresolved_reason = (
                        "dynamic meta function parameter has no unique fixed call argument"
                    )
            else:
                unresolved_reason = (
                    "dynamic meta key is not a bounded literal, alias, plugin parameter, or literal helper result"
                )

            known_owner = owner.casefold() if owner.casefold() in _KNOWN_META_OWNERS else owner
            identity = (
                f"meta:{known_owner}:{key}"
                if key is not None
                else f"meta_dynamic:{known_owner}:{scope}:{ordinal}"
            )
            access = (
                f"{known_owner}.meta[{key!r}]"
                if key is not None
                else f"{known_owner}.meta[<dynamic:{key_expression}>]"
            )
            result.append(
                _DynamicMetaSource(
                    node=f"source:{scope}:{ordinal}:{key or '<dynamic>'}",
                    identity=identity,
                    owner=known_owner,
                    key=key,
                    access=access,
                    resolution_path=resolution_path,
                    scope=scope,
                    raw_access=raw_access,
                    unresolved_reason=unresolved_reason,
                )
            )
    return result


def _dynamic_meta_owner(receiver: str) -> str:
    compact = re.sub(r"\s+", "", receiver)
    if compact.endswith(".event()") or compact == "event()":
        return "event"
    value = compact.rsplit(".", 1)[-1].removesuffix("()")
    return value


def _flow_assignments(body: str) -> Iterator[tuple[str, str]]:
    for match in _FLOW_ASSIGNMENT.finditer(body):
        yield match.group("lhs"), match.group("rhs").strip()


def _flow_target_node(lhs: str, scope: str, owner: str) -> str | None:
    if "." not in lhs:
        return f"var:{scope}:{lhs}"
    return _property_node(lhs, scope, owner)


def _property_node(value: str, scope: str, owner: str) -> str | None:
    parts = value.split(".")
    if len(parts) < 2 or any(not re.fullmatch(r"[A-Za-z_$][\w$]*", part) for part in parts):
        return None
    if parts[0] == "this":
        return f"prop:{owner}:{'.'.join(parts[1:])}"
    return f"prop:{scope}:{value}"


def _flow_expression_nodes(
    expression: str,
    scope: str,
    owner: str,
    local_names: set[str],
    functions: list[object],
    dynamic_sources: tuple[_DynamicMetaSource, ...] | list[_DynamicMetaSource],
    property_reads: dict[str, tuple[str, str]],
) -> set[str]:
    nodes: set[str] = set()
    for source in dynamic_sources:
        if source.raw_access in expression:
            nodes.add(source.node)

    masked = _mask_js_strings(expression)
    property_spans: list[tuple[int, int]] = []
    for match in _PROPERTY_EXPRESSION.finditer(masked):
        if match.end("value") < len(masked) and masked[match.end("value")] == "(":
            continue
        value = expression[match.start("value") : match.end("value")]
        node = _property_node(value, scope, owner)
        if node is None:
            continue
        nodes.add(node)
        property_spans.append((match.start("value"), match.end("value")))
        path = value.removeprefix("this.") if value.startswith("this.") else value
        property_reads.setdefault(node, (owner, path))

    variables_mask = list(masked)
    for start, end in property_spans:
        variables_mask[start:end] = " " * (end - start)
    remaining = "".join(variables_mask)
    for name in local_names:
        if re.search(rf"\b{re.escape(name)}\b", remaining):
            nodes.add(f"var:{scope}:{name}")

    for callee, _ in _iter_named_calls(expression):
        helper = _resolve_flow_function(callee, functions)
        if helper is not None:
            nodes.add(f"return:{getattr(helper, 'name')}")
    return nodes


def _mask_js_strings(value: str) -> str:
    output = list(value)
    index = 0
    quote: str | None = None
    while index < len(value):
        character = value[index]
        if quote is not None:
            output[index] = " "
            if character == "\\" and index + 1 < len(value):
                output[index + 1] = " "
                index += 2
                continue
            if character == quote:
                quote = None
            index += 1
            continue
        if character in {"'", '"', "`"}:
            quote = character
            output[index] = " "
        index += 1
    return "".join(output)


def _resolve_flow_function(callee: str, functions: list[object]) -> object | None:
    short = _short_function_name(callee)
    exact = [item for item in functions if getattr(item, "name") == callee]
    if len(exact) == 1:
        return exact[0]
    candidates = [
        item
        for item in functions
        if _short_function_name(str(getattr(item, "name"))) == short
    ]
    return candidates[0] if len(candidates) == 1 else None


def _flow_transforms(expression: str) -> tuple[str, ...]:
    transforms: list[str] = []
    for pattern, label in _TRANSFORMS:
        if pattern.search(expression):
            transforms.append(label)
    split = re.search(r"\.split\s*\(\s*(?P<literal>['\"](?:\\.|[^'\"])*['\"])\s*\)", expression)
    if split is not None:
        delimiter = _decode_js_string_literal(split.group("literal"))
        if delimiter is not None and delimiter:
            transforms.append("split_delimiter=" + json.dumps(delimiter, ensure_ascii=False))
    segment = _SEGMENT_ACCESS.search(expression)
    if segment is not None:
        transforms.append(f"segment[{segment.group('index')}]")
    return tuple(dict.fromkeys(transforms))


def _normalized_expression(value: str) -> str:
    return re.sub(r"\s+", "", value)


def _add_literal_comparison_sinks(
    body: str,
    scope: str,
    owner: str,
    local_names: set[str],
    sinks: dict[str, list[_FlowSink]],
) -> None:
    literal = r"(?:'(?:\\.|[^'])*'|\"(?:\\.|[^\"])*\")"
    for name in local_names:
        if re.search(
            rf"(?:\b{re.escape(name)}\b\s*(?:===|!==|==|!=)\s*{literal}|"
            rf"{literal}\s*(?:===|!==|==|!=)\s*\b{re.escape(name)}\b)",
            body,
        ):
            sinks[f"var:{scope}:{name}"].append(
                _FlowSink("internal", "literal comparison/control use")
            )
    for match in _PROPERTY_EXPRESSION.finditer(_mask_js_strings(body)):
        value = match.group("value")
        if not re.search(
            rf"{re.escape(value)}\s*(?:===|!==|==|!=)\s*{literal}", body
        ):
            continue
        node = _property_node(value, scope, owner)
        if node is not None:
            sinks[node].append(_FlowSink("internal", "literal comparison/control use"))


def _trace_dynamic_source(
    plugin: PluginRecord,
    source: _DynamicMetaSource,
    edges: dict[str, list[_FlowEdge]],
    sinks: dict[str, list[_FlowSink]],
    conflicted_properties: set[str],
    *,
    max_helper_depth: int,
) -> list[PluginVisibilityFinding]:
    queue: list[
        tuple[str, tuple[str, ...], tuple[str, ...], int, tuple[str, ...], bool]
    ] = [(source.node, (), (), 0, (), False)]
    visited: set[tuple[str, tuple[str, ...], tuple[str, ...], int, bool]] = set()
    reached: list[
        tuple[_FlowSink, tuple[str, ...], tuple[str, ...], int, tuple[str, ...], bool]
    ] = []
    while queue and len(visited) < 2048:
        node, transforms, helpers, hops, evidence, uncertain = queue.pop(0)
        state = (node, transforms, helpers, hops, uncertain)
        if state in visited:
            continue
        visited.add(state)
        for sink in sinks.get(node, ()):
            reached.append((sink, transforms, helpers, hops, evidence, uncertain))
        for edge in edges.get(node, ()):
            next_hops = hops + edge.property_hop
            next_helpers = helpers + ((edge.helper,) if edge.helper else ())
            if next_hops > MAX_PROPERTY_HOPS or len(next_helpers) > max_helper_depth:
                continue
            next_transforms = tuple(dict.fromkeys(transforms + edge.transforms))
            next_evidence = evidence + ((edge.evidence,) if edge.evidence else ())
            queue.append(
                (
                    edge.target,
                    next_transforms,
                    next_helpers,
                    next_hops,
                    next_evidence,
                    uncertain or edge.uncertain or edge.target in conflicted_properties,
                )
            )

    visible = [item for item in reached if item[0].kind == "visible"]
    internal = [item for item in reached if item[0].kind == "internal"]
    unsafe = [item for item in reached if item[0].kind == "unsafe"]
    if not visible:
        if unsafe:
            classification = VisibilityClassification.UNSAFE
            reason = "resolved dynamic meta reaches an unsafe syntax sink"
            selected = unsafe[0]
        elif internal:
            classification = VisibilityClassification.INTERNAL
            reason = "resolved dynamic meta is consumed only by control/config logic"
            selected = internal[0]
        else:
            classification = VisibilityClassification.UNKNOWN
            reason = "resolved dynamic meta does not reach a bounded display sink"
            selected = (_FlowSink("unknown", ""), (), (), 0, (), False)
        return [
            _dynamic_finding(
                plugin,
                source,
                selected,
                classification,
                reason,
                mixed=False,
            )
        ]

    grouped: dict[int | None, list[tuple[object, ...]]] = defaultdict(list)
    for item in visible:
        grouped[_segment_index(item[1])].append(item)
    results: list[PluginVisibilityFinding] = []
    for segment, candidates in grouped.items():
        selected = sorted(
            candidates,
            key=lambda item: (item[5], item[3], len(item[2]), len(item[1])),
        )[0]
        same_internal = [
            item for item in internal if _segment_index(item[1]) in {None, segment}
        ]
        same_unsafe = [
            item for item in unsafe if _segment_index(item[1]) in {None, segment}
        ]
        mixed = bool(same_internal or same_unsafe)
        if same_unsafe:
            classification = VisibilityClassification.UNSAFE
            reason = "visible dynamic-meta segment also reaches unsafe syntax use"
        elif mixed or selected[5]:
            classification = VisibilityClassification.CONDITIONAL_VISIBLE
            reason = (
                "display flow exists but the same segment has mixed or ambiguous state propagation"
            )
        else:
            classification = VisibilityClassification.VERIFIED_VISIBLE
            reason = (
                "deterministic dynamic key and bounded property-to-display flow"
            )
        results.append(
            _dynamic_finding(
                plugin,
                source,
                selected,
                classification,
                reason,
                mixed=mixed,
                segment=segment,
            )
        )
    return results


def _segment_index(transforms: tuple[str, ...]) -> int | None:
    values = [
        int(match.group(1))
        for item in transforms
        if (match := re.fullmatch(r"segment\[(\d+)\]", item))
    ]
    return values[-1] if values else None


def _dynamic_finding(
    plugin: PluginRecord,
    source: _DynamicMetaSource,
    reached: tuple[_FlowSink, tuple[str, ...], tuple[str, ...], int, tuple[str, ...], bool],
    classification: VisibilityClassification,
    reason: str,
    *,
    mixed: bool,
    segment: int | None = None,
) -> PluginVisibilityFinding:
    sink, transforms, helpers, hops, state_path, _ = reached
    evidence = tuple(
        dict.fromkeys(
            ("dynamic_property", f"resolved_key[{source.key}]") + transforms
        )
    )
    identity = source.identity + (f":segment{segment}" if segment is not None else "")
    return PluginVisibilityFinding(
        plugin_name=plugin.name,
        plugin_file=plugin.source_file or f"js/plugins/{plugin.name}.js",
        source_kind="meta",
        source_access=source.access,
        transform_evidence=evidence,
        helper_chain=helpers,
        helper_depth=len(helpers),
        sink=sink.name or None,
        classification=classification.value,
        reason=reason,
        source_identity=identity,
        mixed_use=mixed,
        resolved_key=source.key,
        resolution_path=source.resolution_path,
        state_path=state_path,
        property_hops=hops,
    )


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
