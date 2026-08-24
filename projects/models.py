"""Models and exceptions for portable GLT project folders."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from core.models import ApplyIssue, EngineId


class ProjectError(RuntimeError):
    """A fatal project format, path, or filesystem problem."""


class ProjectValidationError(ProjectError):
    """Project/game conflicts that safely block project apply."""

    def __init__(self, issues: list[ApplyIssue]) -> None:
        self.issues = issues
        super().__init__("; ".join(f"{issue.code}: {issue.reason}" for issue in issues))


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    project_version: int
    schema_version: int
    tool_version: str
    engine: EngineId
    game_fingerprint: str
    source_file: str
    translation_file: str
    glossary_file: str
    translation_memory_file: str
    allowlist_file: str
    engine_metadata: dict[str, object] = field(default_factory=dict)

    def to_json_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "project_version": self.project_version,
            "schema_version": self.schema_version,
            "tool_version": self.tool_version,
            "engine": self.engine.value,
            "game_fingerprint": self.game_fingerprint,
            "source_file": self.source_file,
            "translation_file": self.translation_file,
            "glossary_file": self.glossary_file,
            "translation_memory_file": self.translation_memory_file,
            "allowlist_file": self.allowlist_file,
        }
        if self.engine_metadata:
            payload["engine_metadata"] = self.engine_metadata
        return payload

    @classmethod
    def from_json_dict(cls, payload: Any) -> "ProjectConfig":
        if not isinstance(payload, dict):
            raise ProjectError("project.json root must be an object")
        string_fields = (
            "tool_version",
            "engine",
            "game_fingerprint",
            "source_file",
            "translation_file",
            "glossary_file",
            "translation_memory_file",
            "allowlist_file",
        )
        invalid = [name for name in string_fields if not isinstance(payload.get(name), str)]
        if invalid:
            raise ProjectError(f"project.json has missing/non-string fields: {invalid!r}")
        for name in ("project_version", "schema_version"):
            value = payload.get(name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise ProjectError(f"project.json field {name!r} must be an integer")
        try:
            engine = EngineId(payload["engine"])
        except ValueError as exc:
            raise ProjectError(f"unsupported project engine: {payload['engine']!r}") from exc
        for name in (
            "source_file",
            "translation_file",
            "glossary_file",
            "translation_memory_file",
            "allowlist_file",
        ):
            _validate_relative_member(payload[name], name)
        engine_metadata = payload.get("engine_metadata", {})
        if not isinstance(engine_metadata, dict):
            raise ProjectError("project.json field 'engine_metadata' must be an object")
        _validate_portable_metadata(engine_metadata)
        return cls(
            project_version=payload["project_version"],
            schema_version=payload["schema_version"],
            tool_version=payload["tool_version"],
            engine=engine,
            game_fingerprint=payload["game_fingerprint"],
            source_file=payload["source_file"],
            translation_file=payload["translation_file"],
            glossary_file=payload["glossary_file"],
            translation_memory_file=payload["translation_memory_file"],
            allowlist_file=payload["allowlist_file"],
            engine_metadata=engine_metadata,
        )


@dataclass(frozen=True, slots=True)
class ProjectContext:
    root: Path
    config: ProjectConfig

    def member(self, relative_path: str) -> Path:
        path = self.root.joinpath(*PurePosixPath(relative_path).parts).resolve()
        if not path.is_relative_to(self.root):
            raise ProjectError("project member escapes project directory")
        return path

    @property
    def source_file(self) -> Path:
        return self.member(self.config.source_file)

    @property
    def translation_file(self) -> Path:
        return self.member(self.config.translation_file)

    @property
    def glossary_file(self) -> Path:
        return self.member(self.config.glossary_file)

    @property
    def translation_memory_file(self) -> Path:
        return self.member(self.config.translation_memory_file)

    @property
    def allowlist_file(self) -> Path:
        return self.member(self.config.allowlist_file)

    @property
    def reports_directory(self) -> Path:
        return self.root / "reports"


@dataclass(frozen=True, slots=True)
class TmOperationResult:
    matches: int = 0
    filled: int = 0
    skipped_existing: int = 0
    added: int = 0
    duplicates: int = 0
    issues: tuple[ApplyIssue, ...] = ()


def _validate_relative_member(value: str, field_name: str) -> None:
    path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if (
        path.is_absolute()
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or ".." in path.parts
        or ".." in windows_path.parts
        or not path.parts
    ):
        raise ProjectError(f"{field_name} must be a project-relative path")


def _validate_portable_metadata(value: object, *, key: str = "engine_metadata") -> None:
    """Reject obvious machine-specific absolute paths in extension metadata."""

    if isinstance(value, dict):
        for child_key, child in value.items():
            if not isinstance(child_key, str):
                raise ProjectError(f"{key} keys must be strings")
            _validate_portable_metadata(child, key=f"{key}.{child_key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _validate_portable_metadata(child, key=f"{key}[{index}]")
        return
    if value is None or isinstance(value, (bool, int, float)):
        return
    if not isinstance(value, str):
        raise ProjectError(f"{key} contains an unsupported value")
    windows_path = PureWindowsPath(value)
    if Path(value).is_absolute() or windows_path.is_absolute() or windows_path.drive:
        raise ProjectError(f"{key} must not contain an absolute path")
