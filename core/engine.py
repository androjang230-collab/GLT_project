"""Stable interface implemented by engine-specific modules."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from core.errors import UnsupportedEngineOperationError
from core.models import ApplyReport, DetectionResult, ExtractionResult

if TYPE_CHECKING:
    from core.archive import ArchiveReport
    from core.fingerprint import GameFingerprint
    from core.models import ApplyIssue, EngineId
    from core.qa import QaResult
    from core.structure import StructureReport


class TranslationRecordView(Protocol):
    """Engine-neutral fields consumed by common Project language checks.

    Engine adapters may expose additional location metadata.  In particular,
    core code does not require every engine to use an RPG Maker JSON path.
    """

    id: str
    file: str
    type: str
    original: str
    translation: str
    json_path: str | None


IssueProvider = Callable[[Sequence[TranslationRecordView]], list["ApplyIssue"]]


class EnginePlugin(ABC):
    """Common engine contract.

    One adapter may cover multiple closely related engine IDs, such as RPG
    Maker MV and MZ.  Default capability methods fail explicitly so a future
    detector-only adapter can still be registered safely.
    """

    adapter_id = ""
    supported_engines: frozenset["EngineId"] = frozenset()
    archive_extensions: frozenset[str] = frozenset()

    @abstractmethod
    def detect(self, game_directory: Path) -> DetectionResult:
        """Inspect a game directory without modifying it."""

    def detect_project_source(self, source_directory: Path) -> DetectionResult:
        """Identify a source accepted by the portable Project workflow.

        Most engines use the ordinary game directory.  An adapter may also
        accept an official, portable interchange directory such as WOLF
        ``Data_AutoTXT`` without changing the common Project format.
        """

        return self.detect(source_directory)

    def project_source_mode(self, source_directory: Path) -> str:
        """Return a portable adapter-defined source-mode identifier."""

        return "game_directory"

    def project_metadata(
        self,
        source_directory: Path,
        extraction: ExtractionResult,
    ) -> Mapping[str, object]:
        """Return optional engine metadata stored in ``project.json``."""

        return {}

    def project_extraction_errors(
        self,
        extraction: ExtractionResult,
    ) -> Sequence[object]:
        """Return extraction issues that must block Project creation."""

        return extraction.issues

    def extract_entries(self, game_directory: Path) -> ExtractionResult:
        """Extract entries in memory without assuming a storage format in core."""

        raise self._unsupported("text extraction")

    def extract(self, game_directory: Path, output_file: Path) -> ExtractionResult:
        raise self._unsupported("text extraction")

    def validate(self, project_directory: Path) -> None:
        raise self._unsupported("project validation")

    def apply(
        self,
        game_directory: Path,
        translation_file: Path,
        output_directory: Path,
        *,
        dry_run: bool = False,
        allowlist_path: Path | None = None,
        issue_provider: IssueProvider | None = None,
    ) -> ApplyReport:
        raise self._unsupported("translation apply")

    def qa(
        self,
        game_directory: Path,
        translation_file: Path,
        reports_directory: Path,
        *,
        allowlist_path: Path | None = None,
        issue_provider: IssueProvider | None = None,
    ) -> "QaResult":
        raise self._unsupported("translation QA")

    def fingerprint(
        self,
        game_directory: Path,
        engine: "EngineId",
    ) -> "GameFingerprint":
        """Build a portable engine-specific game fingerprint."""

        raise self._unsupported("game fingerprinting")

    def accepts_legacy_fingerprint(
        self,
        game_directory: Path,
        engine: "EngineId",
        expected: str,
    ) -> bool:
        """Return whether an older adapter-specific source set matches."""

        return False

    def inspect_structure(self, game_directory: Path) -> "StructureReport":
        """Inspect engine-owned files without modifying the game directory."""

        raise self._unsupported("structure inspection")

    def inspect_archive(self, archive_file: Path) -> "ArchiveReport":
        """Probe one archive candidate without modifying or extracting it."""

        raise self._unsupported("archive inspection")

    def inspect_text_export(self, export_directory: Path) -> object:
        """Inspect an engine-owned text export without modifying it."""

        raise self._unsupported("text export inspection")

    def extract_text_export(
        self, export_directory: Path, output_file: Path
    ) -> ExtractionResult:
        """Convert an official engine text export to common GLT entries."""

        raise self._unsupported("text export extraction")

    def validate_entry(self, entry: object) -> list["ApplyIssue"]:
        """Return adapter-specific entry issues without mutating the entry."""

        return []

    def font_check(self, game_directory: Path, reports_directory: Path) -> object:
        raise self._unsupported("font inspection")

    def font_patch(
        self,
        game_directory: Path,
        font_file: Path,
        output_directory: Path,
        *,
        dry_run: bool = False,
        reports_directory: Path | None = None,
    ) -> object:
        raise self._unsupported("font patching")

    def _unsupported(self, capability: str) -> UnsupportedEngineOperationError:
        adapter = self.adapter_id or type(self).__name__
        return UnsupportedEngineOperationError(
            f"{capability} is not supported by the {adapter} adapter"
        )
