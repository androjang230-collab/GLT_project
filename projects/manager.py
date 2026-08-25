"""High-level portable Project creation, QA, apply, Glossary, and TM flows."""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from core.engine import EnginePlugin, TranslationRecordView
from core.errors import ApplySafetyError
from core.fingerprint import GameFingerprint
from core.models import ApplyIssue, ApplyReport, EngineId
from core.qa import QaResult
from core.registry import EngineRegistry
from core.reports import write_apply_report, write_dry_run_report
from core.version import PROJECT_VERSION, SCHEMA_VERSION, TOOL_VERSION
from engines.registry import create_engine_registry
from projects.glossary import (
    GLOSSARY_HEADER,
    glossary_issues,
    inconsistent_translation_issues,
    load_glossary,
)
from projects.io import atomic_write_text, read_json, write_json, write_jsonl
from projects.models import (
    ProjectConfig,
    ProjectContext,
    ProjectError,
    ProjectValidationError,
    TmOperationResult,
)
from projects.tm import tm_fill, tm_update


@dataclass(frozen=True, slots=True)
class ProjectCreateResult:
    project_directory: Path
    config: ProjectConfig
    fingerprint: GameFingerprint
    translation_entries: int


class ProjectManager:
    """Manage one game translation without persisting machine-specific paths."""

    def __init__(self, registry: EngineRegistry | None = None) -> None:
        self._registry = registry or create_engine_registry()

    def create(
        self,
        game_directory: Path,
        project_directory: Path,
        *,
        engine: EngineId | None = None,
    ) -> ProjectCreateResult:
        game_directory = game_directory.resolve()
        project_directory = project_directory.resolve()
        self._validate_new_project_path(game_directory, project_directory)
        if engine is not None:
            adapter = self._registry.adapter_for(engine)
            if adapter is None:
                raise ProjectError(f"unsupported project engine: {engine.value}")
            detection = adapter.detect_project_source(game_directory)
            if not detection.detected or detection.engine != engine:
                raise ProjectError(
                    f"source is not a supported {engine.value} project source"
                )
        else:
            selection = self._registry.identify_project_source(game_directory)
            adapter = selection.adapter
            detection = selection.detection
        if adapter is None or not detection.detected or detection.engine is None:
            raise ProjectError("a supported game or translation source could not be detected")

        extraction = adapter.extract_entries(game_directory)
        extraction_errors = adapter.project_extraction_errors(extraction)
        if extraction_errors:
            details = "; ".join(
                _extraction_issue_text(issue) for issue in extraction_errors
            )
            raise ProjectError(f"project extraction failed: {details}")
        fingerprint = adapter.fingerprint(game_directory, detection.engine)
        engine_metadata = {
            "engine_id": detection.engine.value,
            "source_mode": adapter.project_source_mode(game_directory),
            **dict(adapter.project_metadata(game_directory, extraction)),
        }
        config = ProjectConfig(
            project_version=PROJECT_VERSION,
            schema_version=SCHEMA_VERSION,
            tool_version=TOOL_VERSION,
            engine=detection.engine,
            game_fingerprint=fingerprint.value,
            source_file="source.jsonl",
            translation_file="translated.jsonl",
            glossary_file="glossary.csv",
            translation_memory_file="translation_memory.jsonl",
            allowlist_file="config/japanese_allowlist.txt",
            engine_metadata=engine_metadata,
        )

        project_directory.parent.mkdir(parents=True, exist_ok=True)
        staging = project_directory.parent / (
            f".{project_directory.name}.glt-project-{uuid.uuid4().hex}.tmp"
        )
        try:
            staging.mkdir()
            serialized_entries = [entry.to_json_dict() for entry in extraction.entries]
            write_jsonl(staging / config.source_file, serialized_entries)
            write_jsonl(staging / config.translation_file, serialized_entries)
            write_json(staging / "project.json", config.to_json_dict())
            atomic_write_text(
                staging / config.glossary_file,
                ",".join(GLOSSARY_HEADER) + "\n",
            )
            atomic_write_text(staging / config.translation_memory_file, "")
            atomic_write_text(staging / config.allowlist_file, "")
            (staging / "reports").mkdir()
            if project_directory.exists():
                raise FileExistsError(
                    f"project directory appeared during creation: {project_directory}"
                )
            staging.rename(project_directory)
        except BaseException:
            if staging.exists():
                shutil.rmtree(staging)
            raise
        return ProjectCreateResult(
            project_directory=project_directory,
            config=config,
            fingerprint=fingerprint,
            translation_entries=len(extraction.entries),
        )

    def load(self, project_directory: Path) -> ProjectContext:
        root = project_directory.resolve()
        if not root.is_dir():
            raise ProjectError(f"project directory does not exist: {project_directory}")
        project_file = root / "project.json"
        if not project_file.is_file():
            raise ProjectError("project.json does not exist")
        config = ProjectConfig.from_json_dict(read_json(project_file))
        context = ProjectContext(root=root, config=config)
        self._require_project_assets(context)
        return context

    def qa(
        self,
        project_directory: Path,
        game_directory: Path,
    ) -> QaResult:
        context = self.load(project_directory)
        game_directory = game_directory.resolve()
        adapter, _, project_issues = self._validate_game(context, game_directory)

        def project_issue_provider(
            records: Sequence[TranslationRecordView],
        ) -> list[ApplyIssue]:
            return [
                *project_issues,
                *self._project_language_issues(context, records),
            ]

        return adapter.qa(
            game_directory,
            context.translation_file,
            context.reports_directory,
            allowlist_path=context.allowlist_file,
            issue_provider=project_issue_provider,
        )

    def apply(
        self,
        project_directory: Path,
        game_directory: Path,
        output_directory: Path,
        *,
        dry_run: bool = False,
    ) -> ApplyReport:
        context = self.load(project_directory)
        game_directory = game_directory.resolve()
        output_directory = output_directory.resolve()
        adapter, _, project_issues = self._validate_game(context, game_directory)
        blockers = [
            issue
            for issue in project_issues
            if issue.severity in {"error", "conflict"}
        ]
        if blockers:
            raise ProjectValidationError(blockers)

        def project_issue_provider(
            records: Sequence[TranslationRecordView],
        ) -> list[ApplyIssue]:
            return [
                *project_issues,
                *self._project_language_issues(context, records),
            ]

        report = adapter.apply(
            game_directory,
            context.translation_file,
            output_directory,
            dry_run=dry_run,
            allowlist_path=context.allowlist_file,
            issue_provider=project_issue_provider,
        )
        if dry_run:
            write_dry_run_report(context.reports_directory, report)
        else:
            write_apply_report(context.reports_directory, report)
        return report

    def tm_fill(self, project_directory: Path) -> TmOperationResult:
        context = self.load(project_directory)
        return tm_fill(context.translation_file, context.translation_memory_file)

    def tm_update(self, project_directory: Path) -> TmOperationResult:
        context = self.load(project_directory)
        return tm_update(context.translation_file, context.translation_memory_file)

    def font_check(
        self,
        project_directory: Path,
        game_directory: Path,
    ) -> object:
        context = self.load(project_directory)
        game_directory = game_directory.resolve()
        adapter, _, issues = self._validate_game(context, game_directory)
        self._raise_project_blockers(
            [issue for issue in issues if issue.code != "GAME_FINGERPRINT_MISMATCH"]
        )
        return adapter.font_check(
            game_directory,
            context.reports_directory,
        )

    def font_patch(
        self,
        project_directory: Path,
        game_directory: Path,
        font_file: Path,
        output_directory: Path,
        *,
        dry_run: bool = False,
    ) -> object:
        context = self.load(project_directory)
        game_directory = game_directory.resolve()
        adapter, _, issues = self._validate_game(context, game_directory)
        self._raise_project_blockers(
            [issue for issue in issues if issue.code != "GAME_FINGERPRINT_MISMATCH"]
        )
        return adapter.font_patch(
            game_directory,
            font_file,
            output_directory,
            dry_run=dry_run,
            reports_directory=context.reports_directory,
        )

    @staticmethod
    def _validate_new_project_path(
        game_directory: Path,
        project_directory: Path,
    ) -> None:
        if not game_directory.is_dir():
            raise FileNotFoundError(f"game directory does not exist: {game_directory}")
        if project_directory.exists():
            raise FileExistsError(
                f"project output already exists; refusing to overwrite: {project_directory}"
            )
        if project_directory == game_directory or project_directory.is_relative_to(
            game_directory
        ):
            raise ApplySafetyError(
                "project directory cannot be the game directory or inside it"
            )
        if game_directory.is_relative_to(project_directory):
            raise ApplySafetyError("project directory cannot contain the game directory")

    @staticmethod
    def _require_project_assets(context: ProjectContext) -> None:
        required = (
            context.source_file,
            context.translation_file,
            context.glossary_file,
            context.translation_memory_file,
            context.allowlist_file,
        )
        missing = [path.relative_to(context.root).as_posix() for path in required if not path.is_file()]
        if missing:
            raise ProjectError(f"project files are missing: {missing!r}")

    def _validate_game(
        self,
        context: ProjectContext,
        game_directory: Path,
    ) -> tuple[EnginePlugin, EngineId, list[ApplyIssue]]:
        adapter = self._registry.adapter_for(context.config.engine)
        if adapter is None:
            raise ProjectError(
                f"no adapter is registered for {context.config.engine.value}"
            )
        detection = adapter.detect_project_source(game_directory)
        if not detection.detected or detection.engine is None:
            raise ProjectError(
                f"source is not valid for project engine {context.config.engine.value}"
            )
        engine = detection.engine
        issues: list[ApplyIssue] = []
        if context.config.project_version != PROJECT_VERSION:
            issues.append(
                ApplyIssue(
                    severity="error",
                    code="PROJECT_VERSION_MISMATCH",
                    reason=(
                        f"project version {context.config.project_version} is not supported"
                    ),
                )
            )
        if context.config.schema_version != SCHEMA_VERSION:
            issues.append(
                ApplyIssue(
                    severity="error",
                    code="SCHEMA_VERSION_MISMATCH",
                    reason=(
                        f"schema version {context.config.schema_version} is not supported"
                    ),
                )
            )
        if context.config.engine != engine:
            issues.append(
                ApplyIssue(
                    severity="conflict",
                    code="PROJECT_ENGINE_MISMATCH",
                    reason=(
                        f"project engine {context.config.engine.value} differs from "
                        f"detected engine {engine.value}"
                    ),
                )
            )
        expected_source_mode = context.config.engine_metadata.get(
            "source_mode", "game_directory"
        )
        current_source_mode = adapter.project_source_mode(game_directory)
        if expected_source_mode != current_source_mode:
            issues.append(
                ApplyIssue(
                    severity="conflict",
                    code="PROJECT_SOURCE_MODE_MISMATCH",
                    reason=(
                        f"project source mode {expected_source_mode!r} differs from "
                        f"current source mode {current_source_mode!r}"
                    ),
                )
            )
        current = adapter.fingerprint(game_directory, engine)
        if (
            context.config.game_fingerprint != current.value
            and not adapter.accepts_legacy_fingerprint(
                game_directory,
                engine,
                context.config.game_fingerprint,
            )
        ):
            issues.append(
                ApplyIssue(
                    severity="conflict",
                    code="GAME_FINGERPRINT_MISMATCH",
                    reason="current game fingerprint differs from project.json",
                )
            )
        return adapter, engine, issues

    @staticmethod
    def _project_language_issues(
        context: ProjectContext,
        records: Sequence[TranslationRecordView],
    ) -> list[ApplyIssue]:
        glossary = load_glossary(context.glossary_file)
        return [
            *glossary_issues(records, glossary),
            *inconsistent_translation_issues(records),
        ]

    @staticmethod
    def _raise_project_blockers(issues: list[ApplyIssue]) -> None:
        blockers = [
            issue for issue in issues if issue.severity in {"error", "conflict"}
        ]
        if blockers:
            raise ProjectValidationError(blockers)


def _extraction_issue_text(issue: object) -> str:
    file_name = getattr(issue, "file", None) or getattr(issue, "source_file", None) or "."
    message = getattr(issue, "message", None) or getattr(issue, "reason", None) or str(issue)
    return f"{file_name}: {message}"
