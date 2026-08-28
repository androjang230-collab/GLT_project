"""Phase-2A semantic contracts for conservatively extractable plugin text.

This module never executes JavaScript and never writes game files.  It turns
Phase-1 visibility evidence into semantic roles, bounded grammar contracts,
and translation entries tied to exact storage locations.  Only contract types
with a Phase-2B reconstruction path declare ``apply_supported = True``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from core.models import EngineId, ExtractionIssue, TranslationEntry
from engines.rpgmaker.mv_plugin_discovery import (
    MvPluginDiscoveryReport,
    _strip_comments,
    discover_mv_plugin_commands,
)
from engines.rpgmaker.plugin_inventory import (
    PluginInventory,
    PluginRecord,
    load_plugin_inventory,
)
from engines.rpgmaker.plugin_visibility import (
    PluginVisibilityFinding,
    VisibilityClassification,
    analyze_plugin_visibility,
)


class SemanticRole(str, Enum):
    TRANSLATABLE_TEXT = "TRANSLATABLE_TEXT"
    VISIBLE_FORMATTING = "VISIBLE_FORMATTING"
    INTERNAL_CONTROL = "INTERNAL_CONTROL"
    MIXED_USE = "MIXED_USE"
    UNSAFE_TEXT = "UNSAFE_TEXT"
    UNKNOWN = "UNKNOWN"


class ContractType(str, Enum):
    SCALAR_PARAMETER_TEXT = "scalar_parameter_text"
    REGEX_CAPTURE_TEXT = "regex_capture_text"
    DELIMITED_BLOCK_TEXT = "delimited_block_text"
    META_VALUE_TEXT = "meta_value_text"
    TOKENIZED_VISIBLE_SEGMENT = "tokenized_visible_segment"


@dataclass(frozen=True, slots=True)
class StorageBinding:
    file: str
    json_path: str
    storage_type: str
    storage_identity: str
    source_value_sha256: str
    segment_start: int
    segment_end: int
    source_key: str | None = None
    token_start: int | None = None
    token_end: int | None = None

    @property
    def span_identity(self) -> tuple[str, str, str, int, int]:
        return (
            self.file,
            self.json_path,
            self.storage_identity,
            self.segment_start,
            self.segment_end,
        )


@dataclass(frozen=True, slots=True)
class BehaviorContract:
    contract_type: str
    source_kind: str
    parser_rule: str
    semantic_role: str
    source_access: str
    transform_evidence: tuple[str, ...]
    sink_evidence: str
    grammar_fingerprint: str
    contract_id: str
    whitespace_policy: str
    capture_ordinal: int | None = None
    open_delimiter: str | None = None
    close_delimiter: str | None = None


@dataclass(frozen=True, slots=True)
class SemanticFinding:
    plugin_name: str
    plugin_file: str
    source_kind: str
    source_access: str
    visibility_classification: str
    semantic_role: str
    reason: str
    sink: str | None
    contract_types: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ContractSuppression:
    code: str
    reason: str
    file: str | None = None
    json_path: str | None = None
    plugin_names: tuple[str, ...] = ()


@dataclass(slots=True)
class PluginContractReport:
    active_plugin_count: int = 0
    analyzed_plugin_count: int = 0
    semantic_findings: list[SemanticFinding] = field(default_factory=list)
    contracts: list[BehaviorContract] = field(default_factory=list)
    entries: list[TranslationEntry] = field(default_factory=list)
    suppressions: list[ContractSuppression] = field(default_factory=list)
    issues: list[ExtractionIssue] = field(default_factory=list)

    def summary(self) -> dict[str, object]:
        return {
            "active_plugin_count": self.active_plugin_count,
            "analyzed_plugin_count": self.analyzed_plugin_count,
            "semantic_role_totals": dict(
                sorted(Counter(x.semantic_role for x in self.semantic_findings).items())
            ),
            "contract_type_totals": dict(
                sorted(Counter(x.contract_type for x in self.contracts).items())
            ),
            "source_kind_totals": dict(
                sorted(Counter(x.source_kind for x in self.contracts).items())
            ),
            "extracted_entries": len(self.entries),
            "suppression_totals": dict(
                sorted(Counter(x.code for x in self.suppressions).items())
            ),
        }


@dataclass(frozen=True, slots=True)
class _ContractTemplate:
    contract_type: ContractType
    source_kind: str
    source_access: str
    parser_rule: str
    sink: str
    transforms: tuple[str, ...]
    whitespace_policy: str
    owner: str | None = None
    regex_pattern: str | None = None
    regex_flags: int = 0
    capture_ordinal: int | None = None
    open_delimiter: str | None = None
    close_delimiter: str | None = None
    meta_key: str | None = None


@dataclass(frozen=True, slots=True)
class _Claim:
    text: str
    binding: StorageBinding
    contract: BehaviorContract
    finding: PluginVisibilityFinding
    segment_ordinal: int = 0


@dataclass(frozen=True, slots=True)
class _NoteStorage:
    owner: str
    file: str
    json_path: str
    value: str


_PARAMETER_CONFIG = re.compile(
    r"(?:font|size|width|height|opacity|color|position|offset|coordinate|"
    r"timing|wait|delay|spacing|padding|margin|lines?|rows?|volume|pitch)",
    re.I,
)
_INTERNAL_CONFIG = re.compile(
    r"(?:switch|variable|common\s*event|file|path|image|picture|audio|sound|"
    r"script|formula|expression|storage\s*key|plugin\s*name|command\s*id|\bid\b)",
    re.I,
)
_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)")
_BOOLEAN = re.compile(r"(?:true|false|on|off|yes|no)", re.I)
_PATH = re.compile(
    r"(?:[A-Za-z]:[\\/]|[\\/]|\.(?:png|jpe?g|webp|ogg|m4a|wav|json|js)$)",
    re.I,
)
_CLEAR_TEXT_SINK = re.compile(
    r"(?:drawText(?:Ex)?|addCommand|setText|\$gameMessage\.add|"
    r"showInfo|setDTextPicture|PIXI\.Text|Sprite_Text)",
    re.I,
)
_CONTROL_CODE_PATTERN = re.compile(
    r"\\(?:[VNCIP]\[\d+\]|G|[{}.$|!><^])", re.IGNORECASE
)
_REGISTRY_ASSIGNMENT = re.compile(
    r"(?:var\s+)?\$plugins\s*=\s*(\[[\s\S]*\])\s*;?\s*$"
)
_NOTE_MATCH = re.compile(
    r"\b(?P<owner>[A-Za-z_$][\w$]*)\.note\s*\.match\s*\(\s*/"
    r"(?P<pattern>(?:\\.|[^/\n])*)/(?P<flags>[gimsuy]*)\s*\)"
)
_NOTE_SPLIT = re.compile(
    r"\b(?P<owner>[A-Za-z_$][\w$]*)\.note\s*\.split\s*\(\s*"
    r"(['\"])\\n\2\s*\)"
)
_MATCH_LITERAL = re.compile(
    r"\.match\s*\(\s*/(?P<pattern>(?:\\.|[^/\n])*)/"
    r"(?P<flags>[gimsuy]*)\s*\)"
)
_META_LITERAL = re.compile(
    r"^(?P<owner>[A-Za-z_$][\w$]*)\.meta(?:\.\s*(?P<dot>[A-Za-z_$][\w$]*)|"
    r"\[\s*(['\"])(?P<bracket>.*?)\3\s*\])$"
)

_OWNER_FILES: Mapping[str, tuple[str, ...]] = {
    "actor": ("Actors.json",),
    "class": ("Classes.json",),
    "item": ("Items.json",),
    "weapon": ("Weapons.json",),
    "armor": ("Armors.json",),
    "skill": ("Skills.json",),
    "state": ("States.json",),
    "enemy": ("Enemies.json",),
}

_APPLY_SUPPORTED_CONTRACTS = frozenset(
    {
        ContractType.SCALAR_PARAMETER_TEXT.value,
        ContractType.DELIMITED_BLOCK_TEXT.value,
    }
)


def extract_plugin_consumed_text(
    game_directory: Path,
    engine: EngineId,
    *,
    existing_entries: Sequence[TranslationEntry] = (),
    inventory: PluginInventory | None = None,
    command_discovery: MvPluginDiscoveryReport | None = None,
) -> PluginContractReport:
    """Return extraction-only plugin text entries and explainable evidence."""

    root = game_directory.resolve()
    config = root / "js/plugins.js"
    sources = root / "js/plugins"
    report = PluginContractReport()
    if not config.is_file() or not sources.is_dir():
        return report
    try:
        inventory = inventory or load_plugin_inventory(config, sources)
        report.issues.extend(
            ExtractionIssue(
                item.source_file or "js/plugins.js",
                f"{item.code}: {item.reason}",
            )
            for item in inventory.issues
            if item.severity == "error"
        )
        if engine == EngineId.RPGMAKER_MV and command_discovery is None:
            command_discovery = discover_mv_plugin_commands(
                config, sources, inventory=inventory
            )
        visibility = analyze_plugin_visibility(
            inventory,
            command_discovery=command_discovery,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        report.issues.append(ExtractionIssue("js/plugins.js", str(exc)))
        return report

    report.active_plugin_count = visibility.active_plugin_count
    report.analyzed_plugin_count = visibility.analyzed_plugin_count
    records = {item.name: item for item in inventory.active_plugins}
    note_storages = tuple(_iter_note_storages(root))
    claims: list[_Claim] = []

    for finding in visibility.findings:
        record = records.get(finding.plugin_name)
        templates = _contract_templates(finding, record)
        prepared_note_contracts: list[
            tuple[_ContractTemplate, BehaviorContract, list[_Claim]]
        ] = []
        for template in templates:
            provisional = _materialize_contract(
                template, SemanticRole.TRANSLATABLE_TEXT
            )
            prepared_note_contracts.append(
                (
                    template,
                    provisional,
                    _claims_for_note_contract(
                        template,
                        provisional,
                        finding,
                        note_storages,
                    ),
                )
            )
        parameter_binding: StorageBinding | None = None
        parameter_value: str | None = None
        parameter_key: str | None = None
        if record is not None and finding.source_kind == "plugin_parameter":
            parameter_key = _parameter_key(finding, record)
            raw_value = record.parameters.get(parameter_key) if parameter_key else None
            parameter_value = raw_value if isinstance(raw_value, str) else None
            if parameter_key is not None and parameter_value is not None:
                parameter_binding = _bind_parameter_token(
                    config, record, parameter_key, parameter_value
                )

        has_safe_boundary = parameter_binding is not None or any(
            template_claims
            for _, _, template_claims in prepared_note_contracts
        )
        role, role_reason = classify_semantic_role(
            finding,
            source_key=parameter_key,
            source_value=parameter_value,
            has_safe_boundary=has_safe_boundary,
        )
        contract_types = tuple(
            sorted({template.contract_type.value for template in templates})
        )
        if parameter_binding is not None and role == SemanticRole.TRANSLATABLE_TEXT:
            contract_types = (ContractType.SCALAR_PARAMETER_TEXT.value,)
        report.semantic_findings.append(
            SemanticFinding(
                plugin_name=finding.plugin_name,
                plugin_file=finding.plugin_file,
                source_kind=finding.source_kind,
                source_access=finding.source_access,
                visibility_classification=finding.classification,
                semantic_role=role.value,
                reason=role_reason,
                sink=finding.sink,
                contract_types=contract_types,
            )
        )

        if role != SemanticRole.TRANSLATABLE_TEXT:
            continue
        if parameter_binding is not None and parameter_value is not None:
            template = _ContractTemplate(
                ContractType.SCALAR_PARAMETER_TEXT,
                "plugin_parameter",
                finding.source_access,
                "literal scalar plugin parameter value",
                finding.sink or "",
                finding.transform_evidence,
                "preserve_exact",
            )
            contract = _materialize_contract(template, role)
            report.contracts.append(contract)
            claims.append(_Claim(parameter_value, parameter_binding, contract, finding))
            continue
        for _, contract, template_claims in prepared_note_contracts:
            report.contracts.append(contract)
            claims.extend(template_claims)

    report.entries, report.suppressions = _resolve_claims(
        claims,
        existing_entries,
        engine,
    )
    return report


def classify_semantic_role(
    finding: PluginVisibilityFinding,
    *,
    source_key: str | None = None,
    source_value: str | None = None,
    has_safe_boundary: bool = False,
) -> tuple[SemanticRole, str]:
    """Classify meaning from use evidence without inspecting text language."""

    key = source_key or finding.source_access
    value = source_value.strip() if isinstance(source_value, str) else None
    visible = bool(finding.sink and _CLEAR_TEXT_SINK.search(finding.sink))
    formatting = bool(_PARAMETER_CONFIG.search(_humanized_key(key)))
    numeric = bool(value is not None and _NUMBER.fullmatch(value))

    if finding.classification == VisibilityClassification.UNSAFE.value:
        return SemanticRole.UNSAFE_TEXT, "visibility evidence reaches unsafe syntax use"
    if value is not None and not value:
        return SemanticRole.UNKNOWN, "blank scalar parameter is not a translation unit"
    if visible and (formatting or numeric):
        return (
            SemanticRole.VISIBLE_FORMATTING,
            "visible sink receives numeric/style/layout formatting data",
        )
    if finding.mixed_use or finding.ambiguity:
        return SemanticRole.MIXED_USE, "source has mixed or conflicting semantics"
    if finding.classification == VisibilityClassification.INTERNAL.value:
        return SemanticRole.INTERNAL_CONTROL, "source is used only by internal control logic"
    if value is not None and (
        _BOOLEAN.fullmatch(value)
        or _PATH.search(value)
        or _INTERNAL_CONFIG.search(_humanized_key(key))
    ):
        return SemanticRole.INTERNAL_CONTROL, "scalar value is control/configuration data"
    if value is not None and _is_json_container_string(value):
        return SemanticRole.UNKNOWN, "JSON-in-string parameter is outside Phase 2A"
    if (
        finding.classification
        in {
            VisibilityClassification.VERIFIED_VISIBLE.value,
            VisibilityClassification.CONDITIONAL_VISIBLE.value,
        }
        and visible
        and has_safe_boundary
        and not finding.ambiguity
    ):
        return (
            SemanticRole.TRANSLATABLE_TEXT,
            "textual display use has a deterministic grammar and storage boundary",
        )
    if finding.classification == VisibilityClassification.INTERNAL.value:
        return SemanticRole.INTERNAL_CONTROL, "source is internal"
    return SemanticRole.UNKNOWN, "safe textual semantics or storage boundary is unproven"


def _contract_templates(
    finding: PluginVisibilityFinding,
    record: PluginRecord | None,
) -> tuple[_ContractTemplate, ...]:
    if record is None or not record.source_available or record.source_path is None:
        return ()
    if finding.source_kind not in {"note", "meta"}:
        return ()
    try:
        source = _strip_comments(record.source_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError):
        return ()

    templates: list[_ContractTemplate] = []
    if finding.source_kind == "note":
        owner = _note_owner(finding.source_access)
        if owner is None:
            return ()
        templates.extend(_regex_capture_templates(finding, source, owner))
        templates.extend(_delimited_block_templates(finding, source, owner))
    elif finding.source_kind == "meta":
        template = _meta_value_template(finding)
        if template is not None:
            templates.append(template)
    unique: dict[tuple[object, ...], _ContractTemplate] = {}
    for item in templates:
        key = (
            item.contract_type,
            item.source_access,
            item.parser_rule,
            item.capture_ordinal,
            item.open_delimiter,
            item.close_delimiter,
        )
        unique[key] = item
    return tuple(unique.values())


def _regex_capture_templates(
    finding: PluginVisibilityFinding,
    source: str,
    owner: str,
) -> list[_ContractTemplate]:
    capture_ordinals = {
        int(match.group(1))
        for transform in finding.transform_evidence
        if (match := re.fullmatch(r"capture\[(\d+)\]", transform))
    }
    if capture_ordinals != {1}:
        return []
    result: list[_ContractTemplate] = []
    for match in _NOTE_MATCH.finditer(source):
        if match.group("owner") != owner:
            continue
        compiled = _compile_js_regex(match.group("pattern"), match.group("flags"))
        if compiled is None or compiled.groups != 1:
            continue
        result.append(
            _ContractTemplate(
                ContractType.REGEX_CAPTURE_TEXT,
                "note",
                finding.source_access,
                f"literal_regex:{match.group('pattern')}/{match.group('flags')}",
                finding.sink or "",
                finding.transform_evidence,
                "preserve_capture_exact",
                owner=owner,
                regex_pattern=match.group("pattern"),
                regex_flags=compiled.flags,
                capture_ordinal=1,
            )
        )
    return result


def _delimited_block_templates(
    finding: PluginVisibilityFinding,
    source: str,
    owner: str,
) -> list[_ContractTemplate]:
    if "literal_split" not in finding.transform_evidence:
        return []
    if not any(match.group("owner") == owner for match in _NOTE_SPLIT.finditer(source)):
        return []
    opens: dict[str, str] = {}
    closes: dict[str, str] = {}
    for match in _MATCH_LITERAL.finditer(source):
        literal = match.group("pattern").replace(r"\/", "/")
        parsed = _literal_delimiter(literal)
        if parsed is None:
            continue
        tag, closing = parsed
        if closing:
            closes[tag] = f"</{tag}>"
        else:
            opens[tag] = f"<{tag}>"
    return [
        _ContractTemplate(
            ContractType.DELIMITED_BLOCK_TEXT,
            "note",
            finding.source_access,
            f"literal_delimited_lines:{opens[tag]}...{closes[tag]}",
            finding.sink or "",
            finding.transform_evidence,
            "preserve_body_lines_without_delimiter_newlines",
            owner=owner,
            open_delimiter=opens[tag],
            close_delimiter=closes[tag],
        )
        for tag in sorted(set(opens) & set(closes), key=str.casefold)
    ]


def _meta_value_template(
    finding: PluginVisibilityFinding,
) -> _ContractTemplate | None:
    match = _META_LITERAL.fullmatch(finding.source_access)
    if match is None:
        return None
    owner = match.group("owner")
    key = match.group("dot") or match.group("bracket")
    if not key or _owner_files(owner) is None:
        return None
    return _ContractTemplate(
        ContractType.META_VALUE_TEXT,
        "meta",
        finding.source_access,
        f"rpgmaker_literal_meta_value:{key}",
        finding.sink or "",
        finding.transform_evidence,
        "preserve_meta_value_exact",
        owner=owner,
        meta_key=key,
    )


def _materialize_contract(
    template: _ContractTemplate,
    role: SemanticRole,
) -> BehaviorContract:
    grammar = {
        "contract_type": template.contract_type.value,
        "source_kind": template.source_kind,
        "parser_rule": template.parser_rule,
        "capture_ordinal": template.capture_ordinal,
        "open_delimiter": template.open_delimiter,
        "close_delimiter": template.close_delimiter,
        "whitespace_policy": template.whitespace_policy,
    }
    fingerprint = _fingerprint(grammar)
    return BehaviorContract(
        contract_type=template.contract_type.value,
        source_kind=template.source_kind,
        parser_rule=template.parser_rule,
        semantic_role=role.value,
        source_access=template.source_access,
        transform_evidence=template.transforms,
        sink_evidence=template.sink,
        grammar_fingerprint=fingerprint,
        contract_id=f"{template.contract_type.value}:{fingerprint[:16]}",
        whitespace_policy=template.whitespace_policy,
        capture_ordinal=template.capture_ordinal,
        open_delimiter=template.open_delimiter,
        close_delimiter=template.close_delimiter,
    )


def _claims_for_note_contract(
    template: _ContractTemplate,
    contract: BehaviorContract,
    finding: PluginVisibilityFinding,
    storages: Sequence[_NoteStorage],
) -> list[_Claim]:
    if template.owner is None:
        return []
    result: list[_Claim] = []
    for storage in storages:
        if storage.owner != template.owner.casefold():
            continue
        if template.contract_type == ContractType.REGEX_CAPTURE_TEXT:
            compiled = _compile_js_regex(
                template.regex_pattern or "", "", explicit_flags=template.regex_flags
            )
            if compiled is None or template.capture_ordinal is None:
                continue
            for ordinal, match in enumerate(compiled.finditer(storage.value)):
                try:
                    start, end = match.span(template.capture_ordinal)
                except IndexError:
                    continue
                if start < 0 or end <= start or not storage.value[start:end].strip():
                    continue
                result.append(
                    _note_claim(storage, start, end, contract, finding, ordinal)
                )
        elif template.contract_type == ContractType.DELIMITED_BLOCK_TEXT:
            for ordinal, (start, end) in enumerate(
                resolve_delimited_block_spans(
                    storage.value,
                    contract.parser_rule,
                )
            ):
                if end > start and storage.value[start:end].strip():
                    result.append(
                        _note_claim(storage, start, end, contract, finding, ordinal)
                    )
        elif template.contract_type == ContractType.META_VALUE_TEXT:
            pattern = re.compile(
                rf"<{re.escape(template.meta_key or '')}:(?P<value>[^<>]*)>"
            )
            for ordinal, match in enumerate(pattern.finditer(storage.value)):
                start, end = match.span("value")
                if end > start and storage.value[start:end].strip():
                    result.append(
                        _note_claim(storage, start, end, contract, finding, ordinal)
                    )
    return result


def _note_claim(
    storage: _NoteStorage,
    start: int,
    end: int,
    contract: BehaviorContract,
    finding: PluginVisibilityFinding,
    ordinal: int,
) -> _Claim:
    binding = StorageBinding(
        file=storage.file,
        json_path=storage.json_path,
        storage_type="data_json_note_field",
        storage_identity=f"{storage.file}:{storage.json_path}",
        source_value_sha256=_sha256_text(storage.value),
        segment_start=start,
        segment_end=end,
    )
    return _Claim(storage.value[start:end], binding, contract, finding, ordinal)


def _resolve_claims(
    claims: Sequence[_Claim],
    existing_entries: Sequence[TranslationEntry],
    engine: EngineId,
) -> tuple[list[TranslationEntry], list[ContractSuppression]]:
    suppressions: list[ContractSuppression] = []
    blocked: set[int] = set()
    existing_locations = {
        (entry.file, entry.json_path)
        for entry in existing_entries
        if entry.json_path is not None
    }
    for index, claim in enumerate(claims):
        if (claim.binding.file, claim.binding.json_path) in existing_locations:
            blocked.add(index)
            suppressions.append(
                ContractSuppression(
                    "EXISTING_ENTRY_OVERLAP",
                    "an existing standard entry already owns this storage field",
                    claim.binding.file,
                    claim.binding.json_path,
                    (claim.finding.plugin_name,),
                )
            )

    by_storage: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    for index, claim in enumerate(claims):
        by_storage[
            (
                claim.binding.file,
                claim.binding.json_path,
                claim.binding.storage_identity,
            )
        ].append(index)
    for indices in by_storage.values():
        for left_offset, left_index in enumerate(indices):
            left = claims[left_index]
            for right_index in indices[left_offset + 1 :]:
                right = claims[right_index]
                if not _spans_overlap(left.binding, right.binding):
                    continue
                if (
                    left.binding.segment_start == right.binding.segment_start
                    and left.binding.segment_end == right.binding.segment_end
                    and left.text == right.text
                ):
                    continue
                blocked.update({left_index, right_index})
                suppressions.append(
                    ContractSuppression(
                        "PLUGIN_CONTRACT_OVERLAP",
                        "plugin contracts claim overlapping non-identical source spans",
                        left.binding.file,
                        left.binding.json_path,
                        tuple(
                            sorted(
                                {
                                    left.finding.plugin_name,
                                    right.finding.plugin_name,
                                },
                                key=str.casefold,
                            )
                        ),
                    )
                )

    exact_groups: dict[tuple[str, str, str, int, int], list[int]] = defaultdict(list)
    for index, claim in enumerate(claims):
        if index not in blocked:
            exact_groups[claim.binding.span_identity].append(index)

    entries: list[TranslationEntry] = []
    for identity in sorted(exact_groups):
        indices = exact_groups[identity]
        selected = sorted(
            (claims[index] for index in indices),
            key=lambda item: (
                item.contract.contract_id,
                item.finding.plugin_name.casefold(),
            ),
        )
        first = selected[0]
        plugin_evidence = [
            {
                "plugin": item.finding.plugin_name,
                "plugin_file": item.finding.plugin_file,
                "source_access": item.finding.source_access,
                "sink": item.finding.sink,
            }
            for item in selected
        ]
        contracts = sorted({item.contract.contract_id for item in selected})
        entry_id = _plugin_entry_id(first.binding, first.contract.contract_type)
        metadata: dict[str, object] = {
            "source_kind": "plugin_consumed_text",
            "semantic_role": SemanticRole.TRANSLATABLE_TEXT.value,
            "contract_type": first.contract.contract_type,
            "contract_id": first.contract.contract_id,
            "contract_fingerprint": first.contract.grammar_fingerprint,
            "compatible_contract_ids": contracts,
            "plugin_evidence": plugin_evidence,
            "storage_file": first.binding.file,
            "storage_type": first.binding.storage_type,
            "storage_identity": first.binding.storage_identity,
            "storage_key": first.binding.source_key,
            "parser_evidence": first.contract.parser_rule,
            "sink_evidence": first.contract.sink_evidence,
            "segment_ordinal": first.segment_ordinal,
            "segment_start": first.binding.segment_start,
            "segment_end": first.binding.segment_end,
            "source_fingerprint": first.binding.source_value_sha256,
            "grammar_fingerprint": first.contract.grammar_fingerprint,
            "whitespace_policy": first.contract.whitespace_policy,
            "apply_supported": (
                first.contract.contract_type in _APPLY_SUPPORTED_CONTRACTS
            ),
        }
        if first.binding.token_start is not None:
            metadata["source_token_start"] = first.binding.token_start
            metadata["source_token_end"] = first.binding.token_end
        entries.append(
            TranslationEntry(
                id=entry_id,
                engine=engine,
                file=first.binding.file,
                type="plugin_text",
                original=first.text,
                json_path=first.binding.json_path,
                control_codes=tuple(
                    match.group(0) for match in _CONTROL_CODE_PATTERN.finditer(first.text)
                ),
                extra_metadata=metadata,
            )
        )
    return entries, _deduplicate_suppressions(suppressions)


def _bind_parameter_token(
    config: Path,
    record: PluginRecord,
    key: str,
    value: str,
) -> StorageBinding | None:
    if record.load_order is None:
        return None
    resolved = resolve_plugin_parameter_binding(config, record.load_order, key)
    if resolved is None or resolved[0] != value:
        return None
    return resolved[1]


def resolve_plugin_parameter_binding(
    config: Path,
    load_order: int,
    key: str,
) -> tuple[str, StorageBinding] | None:
    """Resolve one scalar registry token without executing or rewriting JS."""

    try:
        # ``Path.read_text`` performs universal-newline conversion.  Token
        # offsets must instead describe the exact decoded source so CRLF files
        # can be patched without shifting spans or changing line endings.
        text = config.read_bytes().decode("utf-8-sig")
        match = _REGISTRY_ASSIGNMENT.search(text)
        if match is None:
            return None
        payload = match.group(1)
        object_spans = _top_level_object_spans(payload)
        if not 0 <= load_order < len(object_spans):
            return None
        record_start, record_end = object_spans[load_order]
        located = _parameter_string_tokens(payload, record_start, record_end).get(key)
        if located is None:
            return None
        value = located[0]
        token_start = match.start(1) + located[1]
        token_end = match.start(1) + located[2]
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return None
    return (
        value,
        StorageBinding(
            file="js/plugins.js",
            json_path=f"$[{load_order}].parameters",
            storage_type="plugins_js_parameter_string",
            storage_identity=(
                f"js/plugins.js:$plugins[{load_order}].parameters[{key!r}]"
            ),
            source_value_sha256=_sha256_text(value),
            segment_start=0,
            segment_end=len(value),
            source_key=key,
            token_start=token_start,
            token_end=token_end,
        ),
    )


def _top_level_object_spans(payload: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    in_string = False
    escaped = False
    array_depth = 0
    object_depth = 0
    start: int | None = None
    for index, character in enumerate(payload):
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "[":
            array_depth += 1
        elif character == "]":
            array_depth -= 1
        elif character == "{":
            object_depth += 1
            if array_depth == 1 and object_depth == 1:
                start = index
        elif character == "}":
            if array_depth == 1 and object_depth == 1 and start is not None:
                spans.append((start, index + 1))
                start = None
            object_depth -= 1
        if array_depth < 0 or object_depth < 0:
            raise ValueError("unbalanced plugin registry")
    if in_string or array_depth != 0 or object_depth != 0:
        raise ValueError("unbalanced plugin registry")
    return spans


def _parameter_string_tokens(
    payload: str,
    record_start: int,
    record_end: int,
) -> dict[str, tuple[str, int, int]]:
    properties = _object_properties(payload, record_start, record_end)
    parameters = properties.get("parameters")
    if parameters is None or parameters[0] != "object":
        return {}
    parameter_properties = _object_properties(payload, parameters[1], parameters[2])
    result: dict[str, tuple[str, int, int]] = {}
    for key, (kind, start, end) in parameter_properties.items():
        if kind != "string" or key in result:
            continue
        result[key] = (json.loads(payload[start:end]), start, end)
    return result


def _object_properties(
    text: str,
    start: int,
    end: int,
) -> dict[str, tuple[str, int, int]]:
    if start >= end or text[start] != "{" or text[end - 1] != "}":
        raise ValueError("expected bounded JSON object")
    result: dict[str, tuple[str, int, int]] = {}
    index = start + 1
    while True:
        index = _skip_space(text, index, end - 1)
        if index >= end - 1:
            return result
        if text[index] == ",":
            index = _skip_space(text, index + 1, end - 1)
        if text[index] != '"':
            raise ValueError("object key is not a JSON string")
        key, _, index = _scan_json_string(text, index)
        index = _skip_space(text, index, end - 1)
        if index >= end - 1 or text[index] != ":":
            raise ValueError("object key has no colon")
        value_start = _skip_space(text, index + 1, end - 1)
        kind, value_end = _skip_json_value(text, value_start, end - 1)
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = (kind, value_start, value_end)
        index = _skip_space(text, value_end, end - 1)
        if index < end - 1 and text[index] not in {",", "}"}:
            raise ValueError("unsupported syntax after JSON value")


def _skip_json_value(text: str, start: int, limit: int) -> tuple[str, int]:
    if start >= limit:
        raise ValueError("missing JSON value")
    if text[start] == '"':
        _, _, end = _scan_json_string(text, start)
        return "string", end
    if text[start] in "[{":
        closing = "]" if text[start] == "[" else "}"
        end = _scan_balanced(text, start, text[start], closing)
        return ("array" if text[start] == "[" else "object"), end
    end = start
    while end < limit and text[end] not in ",}":
        end += 1
    json.loads(text[start:end].strip())
    return "primitive", end


def _scan_balanced(text: str, start: int, opening: str, closing: str) -> int:
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        character = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == opening:
            depth += 1
        elif character == closing:
            depth -= 1
            if depth == 0:
                return index + 1
    raise ValueError("unterminated JSON container")


def _scan_json_string(text: str, start: int) -> tuple[str, int, int]:
    escaped = False
    for index in range(start + 1, len(text)):
        character = text[index]
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == '"':
            end = index + 1
            return json.loads(text[start:end]), start, end
    raise ValueError("unterminated JSON string")


def _iter_note_storages(root: Path) -> Iterator[_NoteStorage]:
    data = root / "data"
    if not data.is_dir():
        return
    for owner, names in _OWNER_FILES.items():
        for name in names:
            path = data / name
            if not path.is_file():
                continue
            try:
                document = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if not isinstance(document, list):
                continue
            for index, record in enumerate(document):
                if not isinstance(record, dict) or not isinstance(record.get("note"), str):
                    continue
                yield _NoteStorage(
                    owner,
                    f"data/{name}",
                    f"$[{index}].note",
                    record["note"],
                )
    for path in sorted(data.glob("Map*.json"), key=lambda item: item.name.casefold()):
        try:
            document = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(document, dict) or not isinstance(document.get("events"), list):
            continue
        for index, event in enumerate(document["events"]):
            if not isinstance(event, dict) or not isinstance(event.get("note"), str):
                continue
            yield _NoteStorage(
                "event",
                f"data/{path.name}",
                f"$.events[{index}].note",
                event["note"],
            )


def _parameter_key(
    finding: PluginVisibilityFinding,
    record: PluginRecord,
) -> str | None:
    matches = [
        key
        for key in record.parameters
        if isinstance(key, str)
        and finding.source_access.endswith(f"[{key!r}]")
    ]
    return matches[0] if len(matches) == 1 else None


def _note_owner(source_access: str) -> str | None:
    match = re.fullmatch(r"([A-Za-z_$][\w$]*)\.note", source_access)
    if match is None or _owner_files(match.group(1)) is None:
        return None
    return match.group(1)


def _owner_files(owner: str) -> tuple[str, ...] | None:
    if owner.casefold() == "event":
        return ("Map*.json",)
    return _OWNER_FILES.get(owner.casefold())


def _compile_js_regex(
    pattern: str,
    flags: str,
    *,
    explicit_flags: int | None = None,
) -> re.Pattern[str] | None:
    if explicit_flags is None:
        if any(flag in flags for flag in "guy"):
            return None
        python_flags = 0
        if "i" in flags:
            python_flags |= re.I
        if "m" in flags:
            python_flags |= re.M
        if "s" in flags:
            python_flags |= re.S
    else:
        python_flags = explicit_flags
    if "(?<" in pattern or re.search(r"\\[1-9]", pattern):
        return None
    try:
        return re.compile(pattern.replace(r"\/", "/"), python_flags)
    except re.error:
        return None


def _literal_delimiter(pattern: str) -> tuple[str, bool] | None:
    value = pattern
    if value.startswith("^"):
        value = value[1:]
    else:
        return None
    if value.endswith("$"):
        value = value[:-1]
    closing = value.startswith("</")
    if closing:
        value = value[2:]
    elif value.startswith("<"):
        value = value[1:]
    else:
        return None
    if not value.endswith(">"):
        return None
    tag = value[:-1]
    if not tag or re.search(r"[\\.^$*+?()\[\]{}|<>]", tag):
        return None
    return tag, closing


def _delimited_body_spans(
    text: str,
    open_delimiter: str,
    close_delimiter: str,
) -> Iterator[tuple[int, int]]:
    if not open_delimiter or not close_delimiter:
        return
    line_pattern = re.compile(
        rf"(?m)^(?:{re.escape(open_delimiter)})\r?$|"
        rf"^(?:{re.escape(close_delimiter)})\r?$"
    )
    open_end: int | None = None
    for match in line_pattern.finditer(text):
        line = match.group(0).rstrip("\r")
        if line == open_delimiter:
            if open_end is not None:
                open_end = None
                continue
            newline_end = match.end()
            if newline_end < len(text) and text[newline_end] == "\n":
                newline_end += 1
            open_end = newline_end
        elif line == close_delimiter and open_end is not None:
            body_end = match.start()
            if body_end > open_end and text[body_end - 1] == "\n":
                body_end -= 1
                if body_end > open_end and text[body_end - 1] == "\r":
                    body_end -= 1
            yield open_end, body_end
            open_end = None


def resolve_delimited_block_spans(
    text: str,
    parser_rule: str,
) -> tuple[tuple[int, int], ...]:
    """Re-resolve Phase-2A delimited bodies from their grammar rule."""

    prefix = "literal_delimited_lines:"
    if not parser_rule.startswith(prefix):
        return ()
    grammar = parser_rule[len(prefix) :]
    parts = grammar.split("...", 1)
    if len(parts) != 2 or not all(parts):
        return ()
    return tuple(_delimited_body_spans(text, parts[0], parts[1]))


def _spans_overlap(left: StorageBinding, right: StorageBinding) -> bool:
    return max(left.segment_start, right.segment_start) < min(
        left.segment_end, right.segment_end
    )


def _deduplicate_suppressions(
    values: Iterable[ContractSuppression],
) -> list[ContractSuppression]:
    result: list[ContractSuppression] = []
    seen: set[tuple[object, ...]] = set()
    for value in values:
        key = (
            value.code,
            value.reason,
            value.file,
            value.json_path,
            value.plugin_names,
        )
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _plugin_entry_id(binding: StorageBinding, contract_type: str) -> str:
    if binding.storage_type == "plugins_js_parameter_string":
        location = binding.storage_identity.removeprefix("js/plugins.js:")
    else:
        location = f"{binding.file}:{binding.json_path}"
    return (
        "PluginConsumed:"
        + _id_token(location)
        + f":{contract_type}:segment{binding.segment_start}"
    )


def _id_token(value: str) -> str:
    return value.replace("~", "~0").replace(":", "~1")


def _humanized_key(value: str) -> str:
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    return re.sub(r"[_\-\[\]'\".]+", " ", value)


def _is_json_container_string(value: str) -> bool:
    stripped = value.strip()
    if not stripped.startswith(("{", "[")):
        return False
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, (dict, list))


def _skip_space(text: str, index: int, limit: int) -> int:
    while index < limit and text[index].isspace():
        index += 1
    return index


def _fingerprint(value: Mapping[str, object]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _sha256_text(payload)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "BehaviorContract",
    "ContractSuppression",
    "ContractType",
    "PluginContractReport",
    "SemanticFinding",
    "SemanticRole",
    "StorageBinding",
    "classify_semantic_role",
    "extract_plugin_consumed_text",
    "resolve_delimited_block_spans",
    "resolve_plugin_parameter_binding",
]
