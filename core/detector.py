"""Engine-neutral detection coordinator."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from core.engine import EnginePlugin
from core.models import DetectionResult
from core.registry import EngineRegistry


class EngineDetector:
    """Runs registered engine plugins and selects the strongest result."""

    def __init__(self, engines: Iterable[EnginePlugin]) -> None:
        self._registry = EngineRegistry(engines)

    def detect(self, game_directory: Path) -> DetectionResult:
        """Compatibility wrapper for callers using the Phase 1 API."""

        return self._registry.detect(game_directory)
