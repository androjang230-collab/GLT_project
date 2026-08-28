"""Safe location-based translation insertion for RPG Maker MV/MZ."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, TypeAlias

from core.errors import ApplySafetyError
from core.models import (
    ApplyIssue,
    ApplyReport,
    EngineId,
    TranslationEntry,
)
from core.version import SCHEMA_VERSION, TOOL_VERSION
from engines.rpgmaker.extractor import RpgMakerExtractor, find_control_codes
from engines.rpgmaker.plugin_rules import (
    VERIFIED,
    classify_mv_command,
    extract_runtime_payload,
    parse_editor_annotation,
    rebuild_editor_annotation,
)
from engines.rpgmaker.plugin_contracts import (
    ContractType,
    SemanticRole,
    resolve_delimited_block_spans,
    resolve_plugin_parameter_binding,
    resolve_tokenized_meta_spans,
)
from engines.rpgmaker.validator import (
    JapaneseAllowlist,
    JsonPathError,
    JsonPathToken,
    compare_control_codes,
    detect_japanese_scripts,
    find_unexpected_changes,
    get_json_value,
    parse_json_path,
    set_json_value,
)

@dataclass(frozen=True, slots=True)
class TranslationRecord:
    id: str
    engine: str
    file: str
    type: str
    json_path: str
    original: str
    translation: str
    control_codes: tuple[str, ...] | None
    event_id: int | None
    page_id: int | None
    command_index: int | None
    parameter_index: int | None
    line_number: int
    extra_metadata: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class _ValidatedRecord:
    record: TranslationRecord
    canonical: TranslationEntry
    path_tokens: tuple[JsonPathToken, ...]
    expected_storage_value: str
    replacement_value: str
    mirror_updates: tuple["_MirrorUpdate", ...] = ()


@dataclass(frozen=True, slots=True)
class _PluginValidatedRecord:
    record: TranslationRecord
    canonical: TranslationEntry
    contract_type: str
    storage_identity: str
    span_start: int
    span_end: int
    expected_segment: str
    replacement_segment: str
    expected_storage_value: str
    path_tokens: tuple[JsonPathToken, ...] = ()

    @property
    def overlap_unit(self) -> tuple[str, ...]:
        if self.contract_type == "scalar_parameter_text":
            return ("raw_file", self.canonical.file)
        return (
            "json_field",
            self.canonical.file,
            self.canonical.json_path or "",
        )


_ApplyPlan: TypeAlias = _ValidatedRecord | _PluginValidatedRecord


@dataclass(frozen=True, slots=True)
class _MirrorUpdate:
    json_path: str
    path_tokens: tuple[JsonPathToken, ...]
    expected: str
    replacement: str


@dataclass(slots=True)
class PreflightResult:
    """Shared read-only validation result for QA, dry-run, and apply."""

    report: ApplyReport
    records: list[TranslationRecord]
    validated_records: list[_ApplyPlan]


class RpgMakerInserter:
    """Copy a game and apply translations only to Phase 2-approved locations."""

    def __init__(self, engine: EngineId) -> None:
        if engine not in (EngineId.RPGMAKER_MV, EngineId.RPGMAKER_MZ):
            raise ValueError(f"unsupported engine: {engine}")
        self.engine = engine

    def apply(
        self,
        game_directory: Path,
        translation_file: Path,
        output_directory: Path,
        *,
        allowlist: JapaneseAllowlist | None = None,
        additional_issues: list[ApplyIssue] | None = None,
    ) -> ApplyReport:
        game_directory = game_directory.resolve()
        translation_file = translation_file.resolve()
        output_directory = output_directory.resolve()
        self._validate_input_paths(game_directory, translation_file)
        self._validate_output_path(game_directory, output_directory)
        preflight = self.preflight(
            game_directory,
            translation_file,
            output_directory=output_directory,
            allowlist=allowlist,
            additional_issues=additional_issues,
        )
        report = preflight.report
        validated = preflight.validated_records

        output_directory.parent.mkdir(parents=True, exist_ok=True)
        staging_directory = output_directory.parent / (
            f".{output_directory.name}.glt-{uuid.uuid4().hex}.tmp"
        )
        try:
            shutil.copytree(
                game_directory,
                staging_directory,
                symlinks=True,
                copy_function=shutil.copy2,
            )
            source_hashes = _tree_hashes(game_directory)
            copied_hashes = _tree_hashes(staging_directory)
            report.files_copied = len(source_hashes)
            if source_hashes != copied_hashes:
                raise ApplySafetyError(
                    "COPY_VERIFICATION_FAILED: copied output differs from source"
                )

            self._apply_validated_records(staging_directory, validated, report)
            self._verify_unchanged_files(
                game_directory,
                staging_directory,
                set(report.modified_files),
            )
            self._write_report(staging_directory, report)

            if output_directory.exists():
                raise FileExistsError(
                    f"output directory appeared during apply: {output_directory}"
                )
            staging_directory.rename(output_directory)
        except BaseException:
            if staging_directory.exists():
                shutil.rmtree(staging_directory)
            raise
        return report

    def preflight(
        self,
        game_directory: Path,
        translation_file: Path,
        *,
        output_directory: Path | None = None,
        allowlist: JapaneseAllowlist | None = None,
        additional_issues: list[ApplyIssue] | None = None,
    ) -> PreflightResult:
        """Run all source/translation validation without copying or writing."""

        game_directory = game_directory.resolve()
        translation_file = translation_file.resolve()
        self._validate_input_paths(game_directory, translation_file)
        if output_directory is not None:
            self._validate_output_path(game_directory, output_directory.resolve())

        report = ApplyReport(engine=self.engine)
        records = self._load_translation_jsonl(translation_file, report)
        duplicate_ids = self._find_duplicate_ids(records, report)
        validated = self._validate_records(
            game_directory,
            records,
            duplicate_ids,
            report,
            allowlist,
        )
        if additional_issues:
            report.issues.extend(additional_issues)
        report.applicable = len(validated)
        report.planned_ids = [item.record.id for item in validated]
        report.planned_files = sorted(
            {item.canonical.file for item in validated}
        )
        return PreflightResult(
            report=report,
            records=records,
            validated_records=validated,
        )

    @staticmethod
    def _validate_input_paths(
        game_directory: Path,
        translation_file: Path,
    ) -> None:
        if not game_directory.is_dir():
            raise FileNotFoundError(f"game directory does not exist: {game_directory}")
        if not translation_file.is_file():
            raise FileNotFoundError(
                f"translation JSONL does not exist: {translation_file}"
            )

    @staticmethod
    def _validate_output_path(
        game_directory: Path,
        output_directory: Path,
    ) -> None:
        if output_directory.exists():
            raise FileExistsError(
                f"output path already exists; refusing to overwrite: {output_directory}"
            )
        if output_directory == game_directory:
            raise ApplySafetyError("output directory must differ from the game directory")
        if output_directory.is_relative_to(game_directory):
            raise ApplySafetyError("output directory cannot be inside the game directory")
        if game_directory.is_relative_to(output_directory):
            raise ApplySafetyError("output directory cannot contain the game directory")

    @staticmethod
    def _load_translation_jsonl(
        translation_file: Path,
        report: ApplyReport,
    ) -> list[TranslationRecord]:
        records: list[TranslationRecord] = []
        try:
            stream = translation_file.open("r", encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            raise ApplySafetyError(f"cannot read translation JSONL: {exc}") from exc

        with stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                report.total_translation_entries += 1
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    report.issues.append(
                        ApplyIssue(
                            severity="error",
                            code="MALFORMED_TRANSLATION_JSONL",
                            reason=(
                                f"line {line_number}, column {exc.colno}: {exc.msg}"
                            ),
                        )
                    )
                    continue
                record = _parse_translation_record(payload, line_number, report)
                if record is not None:
                    records.append(record)

        report.translated_entries = sum(
            bool(record.translation.strip()) for record in records
        )
        report.untranslated_entries = sum(
            not record.translation.strip() for record in records
        )
        return records

    @staticmethod
    def _find_duplicate_ids(
        records: list[TranslationRecord],
        report: ApplyReport,
    ) -> set[str]:
        counts = Counter(record.id for record in records)
        duplicates = {entry_id for entry_id, count in counts.items() if count > 1}
        for entry_id in sorted(duplicates):
            matching = [record for record in records if record.id == entry_id]
            report.issues.append(
                _record_issue(
                    matching[0],
                    severity="error",
                    code="DUPLICATE_ID",
                    reason=(
                        f"translation ID occurs {len(matching)} times; all copies were blocked"
                    ),
                )
            )
        return duplicates

    def _validate_records(
        self,
        game_directory: Path,
        records: list[TranslationRecord],
        duplicate_ids: set[str],
        report: ApplyReport,
        allowlist: JapaneseAllowlist | None,
    ) -> list[_ApplyPlan]:
        extraction = RpgMakerExtractor(self.engine).extract(game_directory)
        for issue in extraction.issues:
            report.issues.append(
                ApplyIssue(
                    severity="error",
                    code="SOURCE_JSON_ERROR",
                    file=issue.file,
                    reason=issue.message,
                )
            )
        canonical_by_id = {entry.id: entry for entry in extraction.entries}
        documents: dict[str, Any] = {}

        validated: list[_ApplyPlan] = []
        for record in records:
            if record.id in duplicate_ids:
                continue
            if record.engine != self.engine.value:
                report.issues.append(
                    _record_issue(
                        record,
                        severity="error",
                        code="ENGINE_MISMATCH",
                        reason=(
                            f"entry engine {record.engine!r} does not match {self.engine.value!r}"
                        ),
                    )
                )
                continue
            is_plugin_claim = (
                record.id.startswith("PluginConsumed:")
                or record.extra_metadata.get("source_kind")
                == "plugin_consumed_text"
            )
            if not _is_safe_translation_path(record.file):
                report.issues.append(
                    _record_issue(
                        record,
                        severity="error",
                        code="INVALID_FILE_PATH",
                        reason=(
                            "file must be a relative data/*.json path or the exact "
                            "Phase-2B target js/plugins.js"
                        ),
                    )
                )
                continue
            path_tokens: tuple[JsonPathToken, ...] = ()
            if record.file != "js/plugins.js":
                try:
                    path_tokens = parse_json_path(record.json_path)
                except JsonPathError as exc:
                    report.issues.append(
                        _record_issue(
                            record,
                            severity="error",
                            code="INVALID_JSON_PATH",
                            reason=str(exc),
                        )
                    )
                    continue

            canonical = canonical_by_id.get(record.id)
            if canonical is None:
                target = game_directory.joinpath(*PurePosixPath(record.file).parts)
                if is_plugin_claim and target.is_file():
                    code = "PLUGIN_CONTRACT_NOT_RESOLVED"
                    reason = (
                        "plugin contract no longer resolves uniquely in the current source"
                    )
                else:
                    code = "UNKNOWN_ID" if target.is_file() else "TARGET_FILE_NOT_FOUND"
                    reason = (
                        "ID does not identify a Phase 2 translation target"
                        if target.is_file()
                        else "target source file does not exist"
                    )
                report.issues.append(
                    _record_issue(
                        record,
                        severity="error",
                        code=code,
                        reason=reason,
                    )
                )
                continue
            if canonical.file != record.file:
                report.issues.append(
                    _record_issue(
                        record,
                        severity="error",
                        code="FILE_MISMATCH",
                        reason=f"ID resolves to file {canonical.file}",
                    )
                )
                continue
            if canonical.json_path != record.json_path:
                report.issues.append(
                    _record_issue(
                        record,
                        severity="error",
                        code="JSON_PATH_MISMATCH",
                        reason=f"ID resolves to json_path {canonical.json_path}",
                    )
                )
                continue
            if canonical.type != record.type:
                report.issues.append(
                    _record_issue(
                        record,
                        severity="error",
                        code="TYPE_MISMATCH",
                        reason=f"ID resolves to translation type {canonical.type!r}",
                    )
                )
                continue
            location_mismatches = [
                field_name
                for field_name in (
                    "event_id",
                    "page_id",
                    "command_index",
                    "parameter_index",
                )
                if getattr(record, field_name) is not None
                and getattr(record, field_name) != getattr(canonical, field_name)
            ]
            if location_mismatches:
                report.issues.append(
                    _record_issue(
                        record,
                        severity="error",
                        code="LOCATION_MISMATCH",
                        reason=(
                            "location metadata differs from the canonical target: "
                            f"{location_mismatches!r}"
                        ),
                    )
                )
                continue
            if canonical.original != record.original:
                report.issues.append(
                    _record_issue(
                        record,
                        severity="conflict",
                        code="SOURCE_TEXT_MISMATCH",
                        reason="current game text differs from JSONL original",
                    )
                )
                continue

            actual_codes = find_control_codes(record.original)
            if record.control_codes is not None and Counter(record.control_codes) != Counter(actual_codes):
                report.issues.append(
                    _record_issue(
                        record,
                        severity="error",
                        code="CONTROL_CODE_METADATA_MISMATCH",
                        reason="JSONL control_codes does not match the original text",
                    )
                )
                continue

            if not record.translation.strip():
                report.skipped_untranslated += 1
                continue

            control_difference = compare_control_codes(
                record.original,
                record.translation,
            )
            if not control_difference.matches:
                report.issues.append(
                    _record_issue(
                        record,
                        severity="error",
                        code="CONTROL_CODE_MISMATCH",
                        reason=control_difference.describe(),
                    )
                )
                continue
            if (
                canonical.extra_metadata.get("space_policy") == "requires_protection"
                and re.search(r"[ \t\r\n]", record.translation)
            ):
                report.issues.append(
                    _record_issue(
                        record,
                        severity="error",
                        code="PLUGIN_ARGUMENT_SPACE_UNSAFE",
                        reason="single-token plugin argument cannot safely contain ordinary whitespace",
                    )
                )
                continue
            scripts = detect_japanese_scripts(record.translation)
            is_allowlisted = allowlist is not None and allowlist.allows(
                record.translation
            )
            if scripts.kana and not is_allowlisted:
                report.issues.append(
                    _record_issue(
                        record,
                        severity="warning",
                        code="JAPANESE_TEXT_REMAINS",
                        reason=(
                            "translated text contains "
                            + ", ".join(
                                label
                                for label in scripts.labels
                                if label in {"Hiragana", "Katakana"}
                            )
                        ),
                    )
                )
            if scripts.cjk_kanji and not scripts.kana and not is_allowlisted:
                report.issues.append(
                    _record_issue(
                        record,
                        severity="info",
                        code="CJK_KANJI_REMAINS",
                        reason=(
                            "translated text contains CJK Kanji without Hiragana/Katakana"
                        ),
                    )
                )
            if canonical.extra_metadata.get("source_kind") == "plugin_consumed_text":
                plan = _build_plugin_storage_plan(
                    game_directory,
                    record,
                    canonical,
                    path_tokens,
                    documents,
                    report,
                )
            else:
                plan = _build_storage_plan(
                    game_directory,
                    record,
                    canonical,
                    path_tokens,
                    documents,
                    report,
                )
            if plan is not None:
                if (
                    isinstance(plan, _PluginValidatedRecord)
                    and record.translation == record.original
                ):
                    report.issues.append(
                        _record_issue(
                            record,
                            severity="info",
                            code="NO_CHANGE",
                            reason=(
                                "plugin contract translation already equals the "
                                "validated current source value"
                            ),
                        )
                    )
                else:
                    validated.append(plan)
        validated = _block_plugin_edit_overlaps(validated, report)
        _set_plugin_contract_report_metadata(
            report,
            records,
            validated,
            canonical_by_id,
        )
        return validated

    @staticmethod
    def _apply_validated_records(
        staging_directory: Path,
        records: list[_ApplyPlan],
        report: ApplyReport,
    ) -> None:
        standard_records = [
            item for item in records if isinstance(item, _ValidatedRecord)
        ]
        plugin_records = [
            item for item in records if isinstance(item, _PluginValidatedRecord)
        ]
        grouped: dict[str, list[_ValidatedRecord]] = defaultdict(list)
        for validated in standard_records:
            grouped[validated.canonical.file].append(validated)

        for relative_file, file_records in grouped.items():
            target_file = staging_directory.joinpath(*PurePosixPath(relative_file).parts)
            try:
                raw_bytes = target_file.read_bytes()
                document = json.loads(raw_bytes.decode("utf-8-sig"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                for validated in file_records:
                    report.issues.append(
                        _record_issue(
                            validated.record,
                            severity="error",
                            code="MALFORMED_SOURCE_JSON",
                            reason=str(exc),
                        )
                    )
                continue

            before = copy.deepcopy(document)
            allowed_paths: set[str] = set()
            applied_records: list[_ValidatedRecord] = []
            for validated in file_records:
                try:
                    current_value = get_json_value(document, validated.path_tokens)
                except JsonPathError as exc:
                    report.issues.append(
                        _record_issue(
                            validated.record,
                            severity="error",
                            code="INVALID_JSON_PATH",
                            reason=str(exc),
                        )
                    )
                    continue
                if current_value != validated.expected_storage_value:
                    report.issues.append(
                        _record_issue(
                            validated.record,
                            severity="conflict",
                            code="SOURCE_TEXT_MISMATCH",
                            reason="copied game text differs from JSONL original",
                        )
                    )
                    continue
                if not isinstance(current_value, str):
                    report.issues.append(
                        _record_issue(
                            validated.record,
                            severity="error",
                            code="INVALID_TARGET_TYPE",
                            reason="target value is not a string",
                        )
                    )
                    continue
                set_json_value(
                    document,
                    validated.path_tokens,
                    validated.replacement_value,
                )
                allowed_paths.add(validated.record.json_path)
                mirror_failed = False
                for mirror in validated.mirror_updates:
                    try:
                        mirror_value = get_json_value(document, mirror.path_tokens)
                    except JsonPathError:
                        mirror_failed = True
                        break
                    if mirror_value != mirror.expected:
                        mirror_failed = True
                        break
                    set_json_value(document, mirror.path_tokens, mirror.replacement)
                    allowed_paths.add(mirror.json_path)
                if mirror_failed:
                    set_json_value(
                        document,
                        validated.path_tokens,
                        validated.expected_storage_value,
                    )
                    for mirror in validated.mirror_updates:
                        try:
                            if get_json_value(document, mirror.path_tokens) == mirror.replacement:
                                set_json_value(document, mirror.path_tokens, mirror.expected)
                        except JsonPathError:
                            pass
                    report.issues.append(
                        _record_issue(
                            validated.record,
                            severity="conflict",
                            code="PLUGIN_MIRROR_CHANGED",
                            reason="code 657 mirror changed after preflight; entry was blocked",
                        )
                    )
                    continue
                applied_records.append(validated)

            if not applied_records:
                continue
            unexpected = find_unexpected_changes(before, document, allowed_paths)
            if unexpected:
                for validated in applied_records:
                    report.issues.append(
                        _record_issue(
                            validated.record,
                            severity="error",
                            code="UNEXPECTED_DATA_CHANGE",
                            reason=f"unexpected changed paths: {unexpected!r}",
                        )
                    )
                continue

            write_error = _write_json_atomic_verified(
                target_file,
                raw_bytes,
                document,
                before,
                allowed_paths,
            )
            if write_error is not None:
                for validated in applied_records:
                    report.issues.append(
                        _record_issue(
                            validated.record,
                            severity="error",
                            code="UNEXPECTED_DATA_CHANGE",
                            reason=write_error,
                        )
                    )
                continue
            if relative_file not in report.modified_files:
                report.modified_files.append(relative_file)
            report.applied += len(applied_records)

        _apply_plugin_validated_records(staging_directory, plugin_records, report)

    @staticmethod
    def _verify_unchanged_files(
        game_directory: Path,
        staging_directory: Path,
        modified_files: set[str],
    ) -> None:
        source_hashes = _tree_hashes(game_directory)
        output_hashes = _tree_hashes(staging_directory)
        if source_hashes.keys() != output_hashes.keys():
            raise ApplySafetyError(
                "UNEXPECTED_DATA_CHANGE: copied file set changed during apply"
            )
        for relative_path, source_hash in source_hashes.items():
            portable_path = relative_path.as_posix()
            if portable_path in modified_files:
                continue
            if output_hashes[relative_path] != source_hash:
                raise ApplySafetyError(
                    f"UNEXPECTED_DATA_CHANGE: non-target file changed: {portable_path}"
                )

    @staticmethod
    def _write_report(staging_directory: Path, report: ApplyReport) -> None:
        report_file = staging_directory / "reports" / "apply_report.json"
        if report_file.exists():
            raise ApplySafetyError(
                "REPORT_PATH_CONFLICT: source game already contains "
                "reports/apply_report.json"
            )
        report_file.parent.mkdir(parents=True, exist_ok=True)
        _write_bytes_atomic(
            report_file,
            (
                json.dumps(
                    {
                        "tool_version": TOOL_VERSION,
                        "schema_version": SCHEMA_VERSION,
                        **report.to_json_dict(),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            ).encode("utf-8"),
        )


_MZ_COMMAND_PATH = re.compile(
    r"^(?P<list_path>\$[\s\S]*\.list)\[(?P<index>\d+)\]\.parameters\[3\][\s\S]*$"
)
_PLUGIN_PARAMETER_PATH = re.compile(r"^\$\[(?P<load_order>\d+)\]\.parameters$")
_SUPPORTED_PLUGIN_CONTRACT_TYPES = frozenset(
    {
        ContractType.SCALAR_PARAMETER_TEXT.value,
        ContractType.DELIMITED_BLOCK_TEXT.value,
        ContractType.TOKENIZED_VISIBLE_SEGMENT.value,
    }
)
_PLUGIN_METADATA_PRECONDITIONS = (
    "source_kind",
    "semantic_role",
    "contract_type",
    "contract_id",
    "contract_fingerprint",
    "storage_file",
    "storage_type",
    "storage_identity",
    "storage_key",
    "parser_evidence",
    "sink_evidence",
    "segment_ordinal",
    "segment_start",
    "segment_end",
    "source_fingerprint",
    "grammar_fingerprint",
    "whitespace_policy",
    "apply_supported",
)


def _build_plugin_storage_plan(
    game_directory: Path,
    record: TranslationRecord,
    canonical: TranslationEntry,
    path_tokens: tuple[JsonPathToken, ...],
    documents: dict[str, Any],
    report: ApplyReport,
) -> _PluginValidatedRecord | None:
    """Re-resolve a Phase-2B contract against authoritative current source."""

    metadata = canonical.extra_metadata
    contract_type = metadata.get("contract_type")
    if (
        contract_type not in _SUPPORTED_PLUGIN_CONTRACT_TYPES
        or metadata.get("apply_supported") is not True
    ):
        report.issues.append(
            _record_issue(
                record,
                severity="error",
                code="PLUGIN_CONTRACT_UNSUPPORTED",
                reason=(
                    f"contract type {contract_type!r} has no Phase-2B apply path"
                ),
            )
        )
        return None
    missing = [
        key for key in _PLUGIN_METADATA_PRECONDITIONS if key not in record.extra_metadata
    ]
    if contract_type == ContractType.SCALAR_PARAMETER_TEXT.value:
        missing.extend(
            key
            for key in ("source_token_start", "source_token_end")
            if key not in record.extra_metadata
        )
    if missing:
        report.issues.append(
            _record_issue(
                record,
                severity="error",
                code="PLUGIN_CONTRACT_METADATA_MISSING",
                reason=f"required contract metadata is missing: {sorted(set(missing))!r}",
            )
        )
        return None
    if record.extra_metadata.get("apply_supported") is not True:
        report.issues.append(
            _record_issue(
                record,
                severity="error",
                code="PLUGIN_CONTRACT_UNSUPPORTED",
                reason="translation artifact does not declare this contract apply-supported",
            )
        )
        return None
    for key in _PLUGIN_METADATA_PRECONDITIONS:
        if record.extra_metadata.get(key) == metadata.get(key):
            continue
        code = (
            "PLUGIN_SOURCE_FINGERPRINT_MISMATCH"
            if key == "source_fingerprint"
            else "PLUGIN_GRAMMAR_FINGERPRINT_MISMATCH"
            if key in {"contract_fingerprint", "grammar_fingerprint"}
            else "PLUGIN_STORAGE_IDENTITY_MISMATCH"
            if key.startswith("storage_") or key.startswith("segment_")
            else "PLUGIN_CONTRACT_METADATA_MISMATCH"
        )
        report.issues.append(
            _record_issue(
                record,
                severity="conflict",
                code=code,
                reason=f"current contract metadata differs for {key!r}",
            )
        )
        return None

    if contract_type == ContractType.SCALAR_PARAMETER_TEXT.value:
        return _build_scalar_parameter_plan(
            game_directory, record, canonical, report
        )
    if contract_type == ContractType.DELIMITED_BLOCK_TEXT.value:
        return _build_delimited_block_plan(
            game_directory,
            record,
            canonical,
            path_tokens,
            documents,
            report,
        )
    return _build_tokenized_meta_plan(
        game_directory,
        record,
        canonical,
        path_tokens,
        documents,
        report,
    )


def _build_scalar_parameter_plan(
    game_directory: Path,
    record: TranslationRecord,
    canonical: TranslationEntry,
    report: ApplyReport,
) -> _PluginValidatedRecord | None:
    metadata = canonical.extra_metadata
    path_match = _PLUGIN_PARAMETER_PATH.fullmatch(canonical.json_path or "")
    key = metadata.get("storage_key")
    if (
        canonical.file != "js/plugins.js"
        or metadata.get("storage_type") != "plugins_js_parameter_string"
        or path_match is None
        or not isinstance(key, str)
    ):
        report.issues.append(
            _record_issue(
                record,
                severity="error",
                code="PLUGIN_STORAGE_IDENTITY_MISMATCH",
                reason="scalar parameter is not bound to an exact plugins.js token",
            )
        )
        return None
    source = game_directory / "js/plugins.js"
    resolved = resolve_plugin_parameter_binding(
        source,
        int(path_match.group("load_order")),
        key,
    )
    if resolved is None:
        report.issues.append(
            _record_issue(
                record,
                severity="conflict",
                code="PLUGIN_CONTRACT_NOT_RESOLVED",
                reason="current plugins.js no longer resolves the verified plugin/key token",
            )
        )
        return None
    value, binding = resolved
    if (
        value != record.original
        or binding.storage_identity != metadata.get("storage_identity")
        or binding.source_value_sha256 != metadata.get("source_fingerprint")
        or record.extra_metadata.get("source_token_start")
        != metadata.get("source_token_start")
        or record.extra_metadata.get("source_token_end")
        != metadata.get("source_token_end")
        or binding.token_start != metadata.get("source_token_start")
        or binding.token_end != metadata.get("source_token_end")
    ):
        report.issues.append(
            _record_issue(
                record,
                severity="conflict",
                code="PLUGIN_SOURCE_PRECONDITION_FAILED",
                reason="current scalar token identity, fingerprint, span, or value changed",
            )
        )
        return None
    try:
        source_text = source.read_bytes().decode("utf-8-sig")
        token = source_text[binding.token_start : binding.token_end]
        decoded = json.loads(token)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
        report.issues.append(
            _record_issue(
                record,
                severity="error",
                code="PLUGIN_SOURCE_PRECONDITION_FAILED",
                reason=f"verified scalar token cannot be decoded: {exc}",
            )
        )
        return None
    if decoded != record.original:
        report.issues.append(
            _record_issue(
                record,
                severity="conflict",
                code="SOURCE_TEXT_MISMATCH",
                reason="current plugins.js token differs from JSONL original",
            )
        )
        return None
    return _PluginValidatedRecord(
        record=record,
        canonical=canonical,
        contract_type=ContractType.SCALAR_PARAMETER_TEXT.value,
        storage_identity=binding.storage_identity,
        span_start=binding.token_start,
        span_end=binding.token_end,
        expected_segment=token,
        replacement_segment=_encode_js_string(record.translation),
        expected_storage_value=source_text,
    )


def _build_delimited_block_plan(
    game_directory: Path,
    record: TranslationRecord,
    canonical: TranslationEntry,
    path_tokens: tuple[JsonPathToken, ...],
    documents: dict[str, Any],
    report: ApplyReport,
) -> _PluginValidatedRecord | None:
    metadata = canonical.extra_metadata
    if (
        not _is_safe_data_path(canonical.file)
        or metadata.get("storage_type") != "data_json_note_field"
        or not path_tokens
    ):
        report.issues.append(
            _record_issue(
                record,
                severity="error",
                code="PLUGIN_STORAGE_IDENTITY_MISMATCH",
                reason="delimited block is not bound to an exact data JSON note field",
            )
        )
        return None
    document = documents.get(record.file)
    if document is None:
        source = game_directory.joinpath(*PurePosixPath(record.file).parts)
        try:
            document = json.loads(source.read_bytes().decode("utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            report.issues.append(
                _record_issue(
                    record,
                    severity="error",
                    code="MALFORMED_SOURCE_JSON",
                    reason=str(exc),
                )
            )
            return None
        documents[record.file] = document
    try:
        note = get_json_value(document, path_tokens)
    except JsonPathError as exc:
        report.issues.append(
            _record_issue(
                record,
                severity="error",
                code="INVALID_JSON_PATH",
                reason=str(exc),
            )
        )
        return None
    if not isinstance(note, str):
        report.issues.append(
            _record_issue(
                record,
                severity="error",
                code="INVALID_TARGET_TYPE",
                reason="plugin note target is not a string",
            )
        )
        return None
    parser_rule = metadata.get("parser_evidence")
    ordinal = metadata.get("segment_ordinal")
    start = metadata.get("segment_start")
    end = metadata.get("segment_end")
    spans = (
        resolve_delimited_block_spans(note, parser_rule)
        if isinstance(parser_rule, str)
        else ()
    )
    if (
        not isinstance(ordinal, int)
        or isinstance(ordinal, bool)
        or not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or not 0 <= ordinal < len(spans)
        or spans[ordinal] != (start, end)
        or note[start:end] != record.original
        or _sha256_text(note) != metadata.get("source_fingerprint")
    ):
        report.issues.append(
            _record_issue(
                record,
                severity="conflict",
                code="PLUGIN_SOURCE_PRECONDITION_FAILED",
                reason=(
                    "current note grammar, block ordinal, source fingerprint, "
                    "span, or body changed"
                ),
            )
        )
        return None
    return _PluginValidatedRecord(
        record=record,
        canonical=canonical,
        contract_type=ContractType.DELIMITED_BLOCK_TEXT.value,
        storage_identity=str(metadata["storage_identity"]),
        span_start=start,
        span_end=end,
        expected_segment=record.original,
        replacement_segment=record.translation,
        expected_storage_value=note,
        path_tokens=path_tokens,
    )


def _build_tokenized_meta_plan(
    game_directory: Path,
    record: TranslationRecord,
    canonical: TranslationEntry,
    path_tokens: tuple[JsonPathToken, ...],
    documents: dict[str, Any],
    report: ApplyReport,
) -> _PluginValidatedRecord | None:
    metadata = canonical.extra_metadata
    if (
        not _is_safe_data_path(canonical.file)
        or metadata.get("storage_type") != "data_json_note_field"
        or not path_tokens
    ):
        report.issues.append(
            _record_issue(
                record,
                severity="error",
                code="PLUGIN_STORAGE_IDENTITY_MISMATCH",
                reason="tokenized meta text is not bound to an exact data JSON note field",
            )
        )
        return None
    document = documents.get(record.file)
    if document is None:
        source = game_directory.joinpath(*PurePosixPath(record.file).parts)
        try:
            document = json.loads(source.read_bytes().decode("utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            report.issues.append(
                _record_issue(
                    record,
                    severity="error",
                    code="MALFORMED_SOURCE_JSON",
                    reason=str(exc),
                )
            )
            return None
        documents[record.file] = document
    try:
        note = get_json_value(document, path_tokens)
    except JsonPathError as exc:
        report.issues.append(
            _record_issue(
                record,
                severity="error",
                code="INVALID_JSON_PATH",
                reason=str(exc),
            )
        )
        return None
    if not isinstance(note, str):
        report.issues.append(
            _record_issue(
                record,
                severity="error",
                code="INVALID_TARGET_TYPE",
                reason="plugin note target is not a string",
            )
        )
        return None
    parser_rule = metadata.get("parser_evidence")
    ordinal = metadata.get("segment_ordinal")
    start = metadata.get("segment_start")
    end = metadata.get("segment_end")
    spans = (
        resolve_tokenized_meta_spans(note, parser_rule)
        if isinstance(parser_rule, str)
        else ()
    )
    if (
        not isinstance(ordinal, int)
        or isinstance(ordinal, bool)
        or not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or not 0 <= ordinal < len(spans)
        or spans[ordinal] != (start, end)
        or note[start:end] != record.original
        or _sha256_text(note) != metadata.get("source_fingerprint")
    ):
        report.issues.append(
            _record_issue(
                record,
                severity="conflict",
                code="PLUGIN_SOURCE_PRECONDITION_FAILED",
                reason=(
                    "current note tag grammar, token ordinal, source fingerprint, "
                    "span, or visible segment changed"
                ),
            )
        )
        return None
    return _PluginValidatedRecord(
        record=record,
        canonical=canonical,
        contract_type=ContractType.TOKENIZED_VISIBLE_SEGMENT.value,
        storage_identity=str(metadata["storage_identity"]),
        span_start=start,
        span_end=end,
        expected_segment=record.original,
        replacement_segment=record.translation,
        expected_storage_value=note,
        path_tokens=path_tokens,
    )


def _block_plugin_edit_overlaps(
    plans: list[_ApplyPlan],
    report: ApplyReport,
) -> list[_ApplyPlan]:
    plugin_plans = [
        item for item in plans if isinstance(item, _PluginValidatedRecord)
    ]
    grouped: dict[tuple[str, ...], list[_PluginValidatedRecord]] = defaultdict(list)
    for item in plugin_plans:
        grouped[item.overlap_unit].append(item)
    blocked_ids: set[str] = set()
    for items in grouped.values():
        ordered = sorted(items, key=lambda item: (item.span_start, item.span_end))
        for index, left in enumerate(ordered):
            for right in ordered[index + 1 :]:
                if right.span_start >= left.span_end:
                    break
                if max(left.span_start, right.span_start) < min(
                    left.span_end, right.span_end
                ):
                    blocked_ids.update({left.record.id, right.record.id})
    for item in plugin_plans:
        if item.record.id not in blocked_ids:
            continue
        report.issues.append(
            _record_issue(
                item.record,
                severity="conflict",
                code="PLUGIN_EDIT_OVERLAP",
                reason="planned plugin edits overlap in the same storage unit",
            )
        )
    return [item for item in plans if item.record.id not in blocked_ids]


def _set_plugin_contract_report_metadata(
    report: ApplyReport,
    records: list[TranslationRecord],
    validated: list[_ApplyPlan],
    canonical_by_id: Mapping[str, TranslationEntry],
) -> None:
    plugin_records = [
        item
        for item in records
        if item.id.startswith("PluginConsumed:")
        or item.extra_metadata.get("source_kind") == "plugin_consumed_text"
    ]
    contract_totals = Counter(
        str(item.extra_metadata.get("contract_type", "UNKNOWN"))
        for item in plugin_records
    )
    supported = sum(
        item.extra_metadata.get("contract_type")
        in _SUPPORTED_PLUGIN_CONTRACT_TYPES
        for item in plugin_records
    )
    plugin_plans = [
        item for item in validated if isinstance(item, _PluginValidatedRecord)
    ]
    plugin_ids = {item.id for item in plugin_records}
    plugin_errors = [
        issue
        for issue in report.issues
        if issue.id in plugin_ids and issue.severity in {"error", "conflict"}
    ]
    unsupported = sum(
        issue.code == "PLUGIN_CONTRACT_UNSUPPORTED" for issue in plugin_errors
    )
    overlap_conflicts = sum(
        issue.code == "PLUGIN_EDIT_OVERLAP" for issue in plugin_errors
    )
    precondition_failures = sum(
        issue.code
        not in {"PLUGIN_CONTRACT_UNSUPPORTED", "PLUGIN_EDIT_OVERLAP"}
        for issue in plugin_errors
    )
    no_change = sum(
        issue.id in plugin_ids and issue.code == "NO_CHANGE"
        for issue in report.issues
    )
    report.extra_metadata["plugin_contracts"] = {
        "total_entries": len(plugin_records),
        "supported_entries": supported,
        "unsupported_entries": unsupported,
        "applicable_entries": len(plugin_plans),
        "no_change_entries": no_change,
        "precondition_failures": precondition_failures,
        "overlap_conflicts": overlap_conflicts,
        "contract_type_totals": dict(sorted(contract_totals.items())),
        "planned_files": sorted({item.canonical.file for item in plugin_plans}),
        "resolved_current_entries": sum(
            item.id in canonical_by_id for item in plugin_records
        ),
    }


def _encode_js_string(value: str) -> str:
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("\u2028", r"\u2028")
        .replace("\u2029", r"\u2029")
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _apply_plugin_validated_records(
    staging_directory: Path,
    records: list[_PluginValidatedRecord],
    report: ApplyReport,
) -> None:
    scalar_records = [
        item
        for item in records
        if item.contract_type == ContractType.SCALAR_PARAMETER_TEXT.value
    ]
    note_records = [
        item
        for item in records
        if item.contract_type
        in {
            ContractType.DELIMITED_BLOCK_TEXT.value,
            ContractType.TOKENIZED_VISIBLE_SEGMENT.value,
        }
    ]
    if scalar_records:
        _apply_scalar_parameter_records(
            staging_directory,
            scalar_records,
            report,
        )
    if note_records:
        _apply_delimited_block_records(
            staging_directory,
            note_records,
            report,
        )


def _apply_scalar_parameter_records(
    staging_directory: Path,
    records: list[_PluginValidatedRecord],
    report: ApplyReport,
) -> None:
    grouped: dict[str, list[_PluginValidatedRecord]] = defaultdict(list)
    for item in records:
        grouped[item.canonical.file].append(item)
    for relative_file, file_records in grouped.items():
        target = staging_directory.joinpath(*PurePosixPath(relative_file).parts)
        try:
            original_bytes = target.read_bytes()
            source_text = original_bytes.decode("utf-8-sig")
        except (OSError, UnicodeError) as exc:
            for item in file_records:
                report.issues.append(
                    _record_issue(
                        item.record,
                        severity="error",
                        code="PLUGIN_SOURCE_PRECONDITION_FAILED",
                        reason=f"copied plugins.js cannot be read: {exc}",
                    )
                )
            continue
        invalid = [
            item
            for item in file_records
            if source_text != item.expected_storage_value
            or source_text[item.span_start : item.span_end] != item.expected_segment
        ]
        if invalid:
            for item in file_records:
                report.issues.append(
                    _record_issue(
                        item.record,
                        severity="conflict",
                        code="PLUGIN_SOURCE_PRECONDITION_FAILED",
                        reason=(
                            "copied plugins.js changed after preflight; the file was not patched"
                        ),
                    )
                )
            continue
        patched = source_text
        for item in sorted(
            file_records,
            key=lambda value: (value.span_start, value.span_end),
            reverse=True,
        ):
            patched = (
                patched[: item.span_start]
                + item.replacement_segment
                + patched[item.span_end :]
            )
        encoding = "utf-8-sig" if original_bytes.startswith(b"\xef\xbb\xbf") else "utf-8"
        write_error = _write_plugins_js_atomic_verified(
            target,
            patched.encode(encoding),
            file_records,
        )
        if write_error is not None:
            for item in file_records:
                report.issues.append(
                    _record_issue(
                        item.record,
                        severity="error",
                        code="PLUGIN_WRITE_VALIDATION_FAILED",
                        reason=write_error,
                    )
                )
            continue
        if relative_file not in report.modified_files:
            report.modified_files.append(relative_file)
        report.applied += len(file_records)


def _apply_delimited_block_records(
    staging_directory: Path,
    records: list[_PluginValidatedRecord],
    report: ApplyReport,
) -> None:
    grouped: dict[str, list[_PluginValidatedRecord]] = defaultdict(list)
    for item in records:
        grouped[item.canonical.file].append(item)
    for relative_file, file_records in grouped.items():
        target = staging_directory.joinpath(*PurePosixPath(relative_file).parts)
        try:
            raw_bytes = target.read_bytes()
            document = json.loads(raw_bytes.decode("utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            for item in file_records:
                report.issues.append(
                    _record_issue(
                        item.record,
                        severity="error",
                        code="MALFORMED_SOURCE_JSON",
                        reason=str(exc),
                    )
                )
            continue

        before = copy.deepcopy(document)
        by_field: dict[str, list[_PluginValidatedRecord]] = defaultdict(list)
        for item in file_records:
            by_field[item.canonical.json_path or ""].append(item)
        allowed_paths: set[str] = set()
        applied_records: list[_PluginValidatedRecord] = []
        for json_path, field_records in by_field.items():
            first = field_records[0]
            try:
                current = get_json_value(document, first.path_tokens)
            except JsonPathError as exc:
                for item in field_records:
                    report.issues.append(
                        _record_issue(
                            item.record,
                            severity="error",
                            code="INVALID_JSON_PATH",
                            reason=str(exc),
                        )
                    )
                continue
            parser_rule = first.canonical.extra_metadata.get("parser_evidence")
            current_spans = (
                _resolve_plugin_note_spans(
                    current,
                    parser_rule,
                    first.contract_type,
                )
                if isinstance(current, str) and isinstance(parser_rule, str)
                else ()
            )
            invalid = not isinstance(current, str) or any(
                current != item.expected_storage_value
                or current[item.span_start : item.span_end] != item.expected_segment
                or (item.span_start, item.span_end) not in current_spans
                for item in field_records
            )
            if invalid:
                for item in field_records:
                    report.issues.append(
                        _record_issue(
                            item.record,
                            severity="conflict",
                            code="PLUGIN_SOURCE_PRECONDITION_FAILED",
                            reason=(
                                "copied note grammar or body changed after preflight; "
                                "the note field was not patched"
                            ),
                        )
                    )
                continue
            patched = current
            for item in sorted(
                field_records,
                key=lambda value: (value.span_start, value.span_end),
                reverse=True,
            ):
                patched = (
                    patched[: item.span_start]
                    + item.replacement_segment
                    + patched[item.span_end :]
                )
            set_json_value(document, first.path_tokens, patched)
            allowed_paths.add(json_path)
            applied_records.extend(field_records)

        if not applied_records:
            continue
        unexpected = find_unexpected_changes(before, document, allowed_paths)
        if unexpected:
            for item in applied_records:
                report.issues.append(
                    _record_issue(
                        item.record,
                        severity="error",
                        code="UNEXPECTED_DATA_CHANGE",
                        reason=f"unexpected changed paths: {unexpected!r}",
                    )
                )
            continue
        write_error = _write_json_atomic_verified(
            target,
            raw_bytes,
            document,
            before,
            allowed_paths,
        )
        if write_error is not None:
            for item in applied_records:
                report.issues.append(
                    _record_issue(
                        item.record,
                        severity="error",
                        code="UNEXPECTED_DATA_CHANGE",
                        reason=write_error,
                    )
                )
            continue
        if relative_file not in report.modified_files:
            report.modified_files.append(relative_file)
        report.applied += len(applied_records)


def _resolve_plugin_note_spans(
    value: str,
    parser_rule: str,
    contract_type: str,
) -> tuple[tuple[int, int], ...]:
    if contract_type == ContractType.DELIMITED_BLOCK_TEXT.value:
        return resolve_delimited_block_spans(value, parser_rule)
    if contract_type == ContractType.TOKENIZED_VISIBLE_SEGMENT.value:
        return resolve_tokenized_meta_spans(value, parser_rule)
    return ()


def _build_storage_plan(
    game_directory: Path,
    record: TranslationRecord,
    canonical: TranslationEntry,
    path_tokens: tuple[JsonPathToken, ...],
    documents: dict[str, Any],
    report: ApplyReport,
) -> _ValidatedRecord | None:
    """Resolve virtual plugin payloads and safe editor mirrors read-only."""

    document = documents.get(record.file)
    if document is None:
        source = game_directory.joinpath(*PurePosixPath(record.file).parts)
        try:
            document = json.loads(source.read_bytes().decode("utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            report.issues.append(
                _record_issue(
                    record,
                    severity="error",
                    code="MALFORMED_SOURCE_JSON",
                    reason=str(exc),
                )
            )
            return None
        documents[record.file] = document
    try:
        storage_value = get_json_value(document, path_tokens)
    except JsonPathError as exc:
        report.issues.append(
            _record_issue(
                record,
                severity="error",
                code="INVALID_JSON_PATH",
                reason=str(exc),
            )
        )
        return None
    if not isinstance(storage_value, str):
        report.issues.append(
            _record_issue(
                record,
                severity="error",
                code="INVALID_TARGET_TYPE",
                reason="target value is not a string",
            )
        )
        return None

    metadata = canonical.extra_metadata
    if metadata.get("source_kind") != "plugin_command":
        if storage_value != record.original:
            report.issues.append(
                _record_issue(
                    record,
                    severity="conflict",
                    code="SOURCE_TEXT_MISMATCH",
                    reason="current game text differs from JSONL original",
                )
            )
            return None
        return _ValidatedRecord(
            record, canonical, path_tokens, storage_value, record.translation
        )

    if metadata.get("classification") not in {"verified", "apply_verified"}:
        report.issues.append(
            _record_issue(
                record,
                severity="error",
                code="UNVERIFIED_PLUGIN_TEXT",
                reason="conditional plugin text cannot be applied automatically",
            )
        )
        return None

    if canonical.parameter_index == 0 and ":cmd356:" in canonical.id:
        if metadata.get("plugin_rule") == "mv_source_discovery_v1":
            argument_mode = metadata.get("argument_mode")
            payload_start = metadata.get("payload_start")
            if not isinstance(argument_mode, str) or (
                payload_start is not None and not isinstance(payload_start, int)
            ):
                report.issues.append(
                    _record_issue(record, severity="error", code="PLUGIN_RULE_INVALID", reason="discovered payload metadata is invalid")
                )
                return None
            payload = extract_runtime_payload(storage_value, argument_mode, payload_start)
            expected_command = metadata.get("plugin_command")
            normalization = metadata.get("command_normalization", "exact")
            command_matches = (
                isinstance(expected_command, str)
                and payload is not None
                and (
                    payload.command == expected_command
                    if normalization == "exact"
                    else payload.command.upper() == expected_command
                    if normalization == "upper"
                    else payload.command.lower() == expected_command
                    if normalization == "lower"
                    else False
                )
            )
            if (
                payload is None
                or not command_matches
                or payload.payload != record.original
            ):
                report.issues.append(
                    _record_issue(record, severity="conflict", code="PLUGIN_CONTEXT_MISMATCH", reason="discovered MV command or payload no longer matches")
                )
                return None
            return _ValidatedRecord(
                record, canonical, path_tokens, storage_value,
                payload.rebuild(storage_value, record.translation),
            )
        match = classify_mv_command(storage_value)
        if (
            match.classification != VERIFIED
            or match.payload != record.original
            or match.prefix != metadata.get("plugin_command")
        ):
            report.issues.append(
                _record_issue(
                    record,
                    severity="conflict",
                    code="PLUGIN_CONTEXT_MISMATCH",
                    reason="MV plugin prefix or payload no longer matches the verified rule",
                )
            )
            return None
        return _ValidatedRecord(
            record,
            canonical,
            path_tokens,
            storage_value,
            match.rebuild(record.translation),
        )

    if storage_value != record.original:
        report.issues.append(
            _record_issue(
                record,
                severity="conflict",
                code="SOURCE_TEXT_MISMATCH",
                reason="current plugin argument differs from JSONL original",
            )
        )
        return None
    mirrors = _find_mz_mirror_updates(
        document,
        canonical,
        record,
        report,
    )
    return _ValidatedRecord(
        record,
        canonical,
        path_tokens,
        storage_value,
        record.translation,
        mirrors,
    )


def _find_mz_mirror_updates(
    document: Any,
    canonical: TranslationEntry,
    record: TranslationRecord,
    report: ApplyReport,
) -> tuple[_MirrorUpdate, ...]:
    match = _MZ_COMMAND_PATH.match(canonical.json_path or "")
    if match is None:
        return ()
    list_path = match.group("list_path")
    source_index = int(match.group("index"))
    try:
        command_list = get_json_value(document, parse_json_path(list_path))
    except JsonPathError:
        return ()
    if not isinstance(command_list, list):
        return ()
    argument_path = canonical.extra_metadata.get("argument_path")
    if not isinstance(argument_path, str):
        return ()
    updates: list[_MirrorUpdate] = []
    for index in range(source_index + 1, len(command_list)):
        command = command_list[index]
        if not isinstance(command, dict) or command.get("code") != 657:
            break
        parameters = command.get("parameters")
        if not isinstance(parameters, list) or not parameters or not isinstance(parameters[0], str):
            continue
        raw = parameters[0]
        parsed = parse_editor_annotation(raw)
        if parsed is None or parsed[0] != argument_path:
            continue
        if parsed[1] != record.original:
            report.issues.append(
                _record_issue(
                    record,
                    severity="warning",
                    code="PLUGIN_MIRROR_MISMATCH",
                    reason=f"code 657 annotation at command {index} was preserved",
                )
            )
            continue
        json_path = f"{list_path}[{index}].parameters[0]"
        updates.append(
            _MirrorUpdate(
                json_path=json_path,
                path_tokens=parse_json_path(json_path),
                expected=raw,
                replacement=rebuild_editor_annotation(raw, record.translation),
            )
        )
    return tuple(updates)


def _parse_translation_record(
    payload: Any,
    line_number: int,
    report: ApplyReport,
) -> TranslationRecord | None:
    if not isinstance(payload, dict):
        report.issues.append(
            ApplyIssue(
                severity="error",
                code="INVALID_TRANSLATION_ENTRY",
                reason=f"line {line_number}: entry must be a JSON object",
            )
        )
        return None
    required_string_fields = (
        "id",
        "engine",
        "file",
        "type",
        "json_path",
        "original",
        "translation",
    )
    invalid = [key for key in required_string_fields if not isinstance(payload.get(key), str)]
    entry_id = payload.get("id") if isinstance(payload.get("id"), str) else None
    if invalid or not entry_id:
        report.issues.append(
            ApplyIssue(
                severity="error",
                code="INVALID_TRANSLATION_ENTRY",
                id=entry_id,
                file=payload.get("file") if isinstance(payload.get("file"), str) else None,
                reason=(
                    f"line {line_number}: missing or non-string fields: {invalid!r}"
                    if invalid
                    else f"line {line_number}: id must not be empty"
                ),
            )
        )
        return None
    raw_control_codes = payload.get("control_codes")
    control_codes: tuple[str, ...] | None = None
    if raw_control_codes is not None:
        if not isinstance(raw_control_codes, list) or not all(
            isinstance(code, str) for code in raw_control_codes
        ):
            report.issues.append(
                ApplyIssue(
                    severity="error",
                    code="INVALID_TRANSLATION_ENTRY",
                    id=entry_id,
                    file=payload["file"],
                    json_path=payload["json_path"],
                    reason=f"line {line_number}: control_codes must be an array of strings",
                )
            )
            return None
        control_codes = tuple(raw_control_codes)
    location_metadata: dict[str, int | None] = {}
    for field_name in (
        "event_id",
        "page_id",
        "command_index",
        "parameter_index",
    ):
        value = payload.get(field_name)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
            report.issues.append(
                ApplyIssue(
                    severity="error",
                    code="INVALID_TRANSLATION_ENTRY",
                    id=entry_id,
                    file=payload["file"],
                    json_path=payload["json_path"],
                    reason=f"line {line_number}: {field_name} must be an integer",
                )
            )
            return None
        location_metadata[field_name] = value
    standard_fields = {
        "id",
        "engine",
        "file",
        "type",
        "original",
        "translation",
        "speaker",
        "json_path",
        "event_id",
        "page_id",
        "command_index",
        "parameter_index",
        "map_id",
        "map_name",
        "control_codes",
    }
    return TranslationRecord(
        id=entry_id,
        engine=payload["engine"],
        file=payload["file"],
        type=payload["type"],
        json_path=payload["json_path"],
        original=payload["original"],
        translation=payload["translation"],
        control_codes=control_codes,
        event_id=location_metadata["event_id"],
        page_id=location_metadata["page_id"],
        command_index=location_metadata["command_index"],
        parameter_index=location_metadata["parameter_index"],
        line_number=line_number,
        extra_metadata={
            key: value for key, value in payload.items() if key not in standard_fields
        },
    )


def _record_issue(
    record: TranslationRecord,
    *,
    severity: str,
    code: str,
    reason: str,
) -> ApplyIssue:
    return ApplyIssue(
        severity=severity,
        code=code,
        id=record.id,
        file=record.file,
        json_path=record.json_path,
        type=record.type,
        original=record.original,
        translation=record.translation,
        reason=reason,
    )


def _is_safe_data_path(value: str) -> bool:
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and len(path.parts) >= 2
        and path.parts[0] == "data"
        and ".." not in path.parts
        and path.suffix.casefold() == ".json"
    )


def _is_safe_translation_path(value: str) -> bool:
    return value == "js/plugins.js" or _is_safe_data_path(value)


def _tree_hashes(root: Path) -> dict[Path, str]:
    hashes: dict[Path, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            hashes[path.relative_to(root)] = digest.hexdigest()
    return hashes


def _write_json_atomic_verified(
    target_file: Path,
    original_bytes: bytes,
    document: Any,
    before: Any,
    allowed_paths: set[str],
) -> str | None:
    had_bom = original_bytes.startswith(b"\xef\xbb\xbf")
    original_text = original_bytes.decode("utf-8-sig")
    pretty = "\n" in original_text or "\r" in original_text
    trailing_newline = original_text.endswith(("\n", "\r"))
    if pretty:
        serialized = json.dumps(document, ensure_ascii=False, indent=2)
    else:
        serialized = json.dumps(
            document,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    if trailing_newline:
        serialized += "\n"
    encoding = "utf-8-sig" if had_bom else "utf-8"
    payload = serialized.encode(encoding)

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            delete=False,
            dir=target_file.parent,
            prefix=f".{target_file.name}.",
            suffix=".tmp",
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(payload)
        reloaded = json.loads(temporary_path.read_text(encoding="utf-8-sig"))
        unexpected = find_unexpected_changes(before, reloaded, allowed_paths)
        if unexpected:
            return f"serialized JSON changed unapproved paths: {unexpected!r}"
        os.replace(temporary_path, target_file)
        temporary_path = None
        return None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return f"atomic JSON write validation failed: {exc}"
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _write_plugins_js_atomic_verified(
    target_file: Path,
    payload: bytes,
    records: list[_PluginValidatedRecord],
) -> str | None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            delete=False,
            dir=target_file.parent,
            prefix=f".{target_file.name}.",
            suffix=".tmp",
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(payload)
        for item in records:
            match = _PLUGIN_PARAMETER_PATH.fullmatch(item.canonical.json_path or "")
            key = item.canonical.extra_metadata.get("storage_key")
            if match is None or not isinstance(key, str):
                return "patched scalar target lost its plugin/key identity"
            resolved = resolve_plugin_parameter_binding(
                temporary_path,
                int(match.group("load_order")),
                key,
            )
            if resolved is None or resolved[0] != item.record.translation:
                return "patched plugins.js failed bounded registry re-resolution"
        os.replace(temporary_path, target_file)
        temporary_path = None
        return None
    except (OSError, UnicodeError, ValueError) as exc:
        return f"atomic plugins.js write validation failed: {exc}"
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _write_bytes_atomic(target_file: Path, payload: bytes) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            delete=False,
            dir=target_file.parent,
            prefix=f".{target_file.name}.",
            suffix=".tmp",
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(payload)
        os.replace(temporary_path, target_file)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
