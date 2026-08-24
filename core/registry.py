"""Registration and selection of engine adapters."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from core.engine import EnginePlugin
from core.models import DetectionResult, EngineId


@dataclass(frozen=True, slots=True)
class EngineSelection:
    """The adapter and concrete engine selected for a game directory."""

    adapter: EnginePlugin | None
    detection: DetectionResult

    @property
    def detected(self) -> bool:
        return self.adapter is not None and self.detection.detected


class EngineRegistry:
    """Ordered collection of adapters used by CLI and Project orchestration."""

    def __init__(self, engines: Iterable[EnginePlugin] = ()) -> None:
        self._engines: list[EnginePlugin] = []
        self._by_engine: dict[EngineId, EnginePlugin] = {}
        self._adapter_ids: set[str] = set()
        for engine in engines:
            self.register(engine)

    @property
    def engines(self) -> tuple[EnginePlugin, ...]:
        return tuple(self._engines)

    def register(self, adapter: EnginePlugin) -> None:
        adapter_id = adapter.adapter_id.strip() or (
            f"{type(adapter).__module__}.{type(adapter).__qualname__}"
        )
        if adapter_id in self._adapter_ids:
            raise ValueError(f"engine adapter is already registered: {adapter_id}")
        duplicates = set(adapter.supported_engines) & set(self._by_engine)
        if duplicates:
            values = sorted(engine.value for engine in duplicates)
            raise ValueError(f"engine IDs already have adapters: {values!r}")
        self._adapter_ids.add(adapter_id)
        self._engines.append(adapter)
        for engine in adapter.supported_engines:
            self._by_engine[engine] = adapter

    def adapter_for(self, engine: EngineId) -> EnginePlugin | None:
        return self._by_engine.get(engine)

    def adapter_for_archive(self, archive_file: Path) -> EnginePlugin | None:
        """Select the sole adapter declaring support for an archive suffix."""

        suffix = archive_file.suffix.casefold()
        matches = [
            adapter
            for adapter in self._engines
            if suffix in {item.casefold() for item in adapter.archive_extensions}
        ]
        if len(matches) > 1:
            raise ValueError(
                f"multiple engine adapters claim archive extension: {suffix}"
            )
        return matches[0] if matches else None

    def identify(self, game_directory: Path) -> EngineSelection:
        results = [(adapter, adapter.detect(game_directory)) for adapter in self._engines]
        if not results:
            return EngineSelection(None, DetectionResult.unknown())

        detected = [item for item in results if item[1].detected]
        if detected:
            adapter, result = max(detected, key=lambda item: item[1].confidence)
            return EngineSelection(adapter, result)

        _, strongest = max(results, key=lambda item: item[1].confidence)
        return EngineSelection(
            None,
            DetectionResult.unknown(
                confidence=strongest.confidence,
                evidence=strongest.evidence,
            ),
        )

    def identify_project_source(self, source_directory: Path) -> EngineSelection:
        """Select an adapter for a common Project source directory."""

        results = [
            (adapter, adapter.detect_project_source(source_directory))
            for adapter in self._engines
        ]
        if not results:
            return EngineSelection(None, DetectionResult.unknown())
        detected = [item for item in results if item[1].detected]
        if detected:
            adapter, result = max(detected, key=lambda item: item[1].confidence)
            return EngineSelection(adapter, result)
        _, strongest = max(results, key=lambda item: item[1].confidence)
        return EngineSelection(
            None,
            DetectionResult.unknown(
                confidence=strongest.confidence,
                evidence=strongest.evidence,
            ),
        )

    def detect(self, game_directory: Path) -> DetectionResult:
        return self.identify(game_directory).detection
