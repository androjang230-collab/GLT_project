"""WOLF adapter for read-only inspection and official text-export round trips."""

from __future__ import annotations

from pathlib import Path

from core.archive import ArchiveReport
from core.engine import EnginePlugin
from core.models import DetectionResult, EngineId
from core.structure import StructureReport
from engines.wolf.archive import WolfArchiveProbe
from engines.wolf.detector import WolfDetector
from engines.wolf.structure import WolfStructureInspector
from engines.wolf.text_inspector import WolfTextInspector
from engines.wolf.text_extractor import WolfExtractionResult, WolfTextExtractor
from engines.wolf.text_models import WolfTextReport
from engines.wolf.text_qa import WolfQaResult, WolfTextQa
from engines.wolf.text_writer import WolfTextWriter, WolfWriteReport
from core.translation_io import write_jsonl


class WolfRPGEngine(EnginePlugin):
    """Keep native/archive mutation unsupported; write only copied .Auto.txt trees."""

    adapter_id = "wolf"
    supported_engines = frozenset({EngineId.WOLF_RPG_EDITOR})
    archive_extensions = frozenset({".wolf", ".wolfx", ".assets"})

    def detect(self, game_directory: Path) -> DetectionResult:
        return WolfDetector().detect(game_directory)

    def inspect_structure(self, game_directory: Path) -> StructureReport:
        return WolfStructureInspector().inspect(game_directory)

    def inspect_archive(self, archive_file: Path) -> ArchiveReport:
        return WolfArchiveProbe().probe(archive_file)

    def inspect_text_export(self, export_directory: Path) -> WolfTextReport:
        return WolfTextInspector().inspect(export_directory)

    def extract_text_export(
        self, export_directory: Path, output_file: Path
    ) -> WolfExtractionResult:
        export_directory = export_directory.resolve()
        output_file = output_file.resolve()
        if output_file == export_directory or output_file.is_relative_to(
            export_directory
        ):
            raise ValueError("WOLF JSONL output cannot be inside the export directory")
        result = WolfTextExtractor().inspect_and_convert(export_directory)
        if not result.report.blocked:
            write_jsonl(result.entries, output_file, overwrite=False)
        return result

    def qa_text_export(
        self, export_directory: Path, translation_file: Path
    ) -> WolfQaResult:
        return WolfTextQa().validate(export_directory, translation_file)

    def apply_text_export(
        self,
        export_directory: Path,
        translation_file: Path,
        output_directory: Path,
        *,
        dry_run: bool = False,
    ) -> WolfWriteReport:
        return WolfTextWriter().apply(
            export_directory,
            translation_file,
            output_directory,
            dry_run=dry_run,
        )


__all__ = ["WolfRPGEngine"]
