"""Read-only RPG Maker MV/MZ detection."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.engine import EnginePlugin, IssueProvider
from core.fingerprint import GameFingerprint
from core.models import ApplyReport, DetectionResult, EngineId, ExtractionResult
from core.qa import QaResult
from core.paths import portable_relative_path
from engines.rpgmaker.extractor import RpgMakerExtractor, write_jsonl
from engines.rpgmaker.fingerprint import (
    calculate_game_fingerprint,
    calculate_legacy_game_fingerprint_0_8_1,
)
from engines.rpgmaker.inserter import RpgMakerInserter
from engines.rpgmaker.paths import resolve_rpgmaker_content_root
from engines.rpgmaker.qa import QaResult, RpgMakerQa
from engines.rpgmaker.validator import JapaneseAllowlist


@dataclass(frozen=True, slots=True)
class _Signature:
    engine: EngineId
    core_file: str


_SIGNATURES = (
    _Signature(EngineId.RPGMAKER_MZ, "js/rmmz_core.js"),
    _Signature(EngineId.RPGMAKER_MV, "js/rpg_core.js"),
)

_SUPPORTING_FILES = (
    ("data/Actors.json", 2),
    ("js/plugins.js", 1),
    ("index.html", 1),
)


class RpgMakerEngine(EnginePlugin):
    """Adapter preserving all RPG Maker MV/MZ behavior behind the core API."""

    adapter_id = "rpgmaker"
    supported_engines = frozenset(
        {EngineId.RPGMAKER_MV, EngineId.RPGMAKER_MZ}
    )

    def detect(self, game_directory: Path) -> DetectionResult:
        content_root = resolve_rpgmaker_content_root(game_directory)
        candidates = [
            self._evaluate_signature(content_root, signature)
            for signature in _SIGNATURES
        ]
        best = max(candidates, key=lambda result: result.confidence)

        tied_detected = [
            result
            for result in candidates
            if result.detected and result.confidence == best.confidence
        ]
        if len(tied_detected) > 1:
            evidence = tuple(
                dict.fromkeys(
                    item
                    for result in tied_detected
                    for item in result.evidence
                )
            )
            return DetectionResult.unknown(
                confidence=best.confidence,
                evidence=evidence,
            )
        return best

    def extract(self, game_directory: Path, output_file: Path) -> ExtractionResult:
        result = self.extract_entries(game_directory)
        write_jsonl(result.entries, output_file)
        return result

    def extract_entries(self, game_directory: Path) -> ExtractionResult:
        content_root = resolve_rpgmaker_content_root(game_directory)
        detection = self.detect(content_root)
        if not detection.detected or detection.engine is None:
            raise ValueError("RPG Maker MV/MZ could not be detected")
        return RpgMakerExtractor(detection.engine).extract(content_root)

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
        detection = self.detect(game_directory)
        if not detection.detected or detection.engine is None:
            raise ValueError("RPG Maker MV/MZ could not be detected")
        allowlist = (
            JapaneseAllowlist.from_file(allowlist_path)
            if allowlist_path is not None
            else None
        )
        inserter = RpgMakerInserter(detection.engine)
        additional_issues = None
        prepared = None
        if issue_provider is not None:
            prepared = inserter.preflight(
                game_directory,
                translation_file,
                output_directory=output_directory,
                allowlist=allowlist,
            )
            additional_issues = issue_provider(prepared.records)
        if dry_run:
            if prepared is None:
                prepared = inserter.preflight(
                    game_directory,
                    translation_file,
                    output_directory=output_directory,
                    allowlist=allowlist,
                )
            if additional_issues:
                prepared.report.issues.extend(additional_issues)
            return prepared.report
        return inserter.apply(
            game_directory,
            translation_file,
            output_directory,
            allowlist=allowlist,
            additional_issues=additional_issues,
        )

    def qa(
        self,
        game_directory: Path,
        translation_file: Path,
        reports_directory: Path,
        *,
        allowlist_path: Path | None = None,
        issue_provider: IssueProvider | None = None,
    ) -> QaResult:
        detection = self.detect(game_directory)
        if not detection.detected or detection.engine is None:
            raise ValueError("RPG Maker MV/MZ could not be detected")
        allowlist = (
            JapaneseAllowlist.from_file(allowlist_path)
            if allowlist_path is not None
            else None
        )
        qa = RpgMakerQa(detection.engine)
        if issue_provider is None:
            return qa.run(
                game_directory,
                translation_file,
                reports_directory,
                allowlist=allowlist,
            )
        preflight = RpgMakerInserter(detection.engine).preflight(
            game_directory,
            translation_file,
            allowlist=allowlist,
        )
        preflight.report.issues.extend(issue_provider(preflight.records))
        return qa.write_preflight_reports(
            game_directory,
            reports_directory,
            preflight,
            allowlist=allowlist,
        )

    def fingerprint(
        self,
        game_directory: Path,
        engine: EngineId,
    ) -> GameFingerprint:
        if engine not in self.supported_engines:
            raise ValueError(f"unsupported RPG Maker engine: {engine}")
        return calculate_game_fingerprint(
            resolve_rpgmaker_content_root(game_directory),
            engine,
        )

    def accepts_legacy_fingerprint(
        self,
        game_directory: Path,
        engine: EngineId,
        expected: str,
    ) -> bool:
        legacy = calculate_legacy_game_fingerprint_0_8_1(
            resolve_rpgmaker_content_root(game_directory),
            engine,
        )
        return legacy.value == expected

    def font_check(self, game_directory: Path, reports_directory: Path) -> object:
        from engines.rpgmaker.fonts import RpgMakerFontService

        return RpgMakerFontService().check(game_directory, reports_directory)

    def font_patch(
        self,
        game_directory: Path,
        font_file: Path,
        output_directory: Path,
        *,
        dry_run: bool = False,
        reports_directory: Path | None = None,
    ) -> object:
        from engines.rpgmaker.fonts import RpgMakerFontService

        return RpgMakerFontService().patch(
            game_directory,
            font_file,
            output_directory,
            dry_run=dry_run,
            reports_directory=reports_directory,
        )

    @staticmethod
    def _evaluate_signature(
        game_directory: Path,
        signature: _Signature,
    ) -> DetectionResult:
        core_path = game_directory / signature.core_file
        system_path = game_directory / "data/System.json"
        core_exists = core_path.is_file()
        system_exists = system_path.is_file()

        evidence_paths: list[Path] = []
        if core_exists:
            evidence_paths.append(core_path)
        if system_exists:
            evidence_paths.append(system_path)

        map_files = sorted((game_directory / "data").glob("Map*.json"))
        map_files = [path for path in map_files if path.is_file()]
        if map_files:
            evidence_paths.append(map_files[0])

        supporting_score = 0
        for relative_path, score in _SUPPORTING_FILES:
            candidate = game_directory / relative_path
            if candidate.is_file():
                evidence_paths.append(candidate)
                supporting_score += score

        evidence = tuple(
            portable_relative_path(path, game_directory)
            for path in evidence_paths
        )

        # A core script alone is suggestive, but confirmation always requires
        # System.json as a second independent artifact.
        if core_exists and system_exists:
            confidence = min(99, 90 + (5 if map_files else 0) + supporting_score)
            return DetectionResult(
                engine=signature.engine,
                confidence=confidence,
                evidence=evidence,
                detected=True,
            )

        confidence = 0
        if core_exists:
            confidence += 45
        if system_exists:
            confidence += 25
        if map_files:
            confidence += 5
        confidence = min(74, confidence + supporting_score)
        return DetectionResult.unknown(
            confidence=confidence,
            evidence=evidence,
        )
