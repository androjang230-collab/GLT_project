"""Common result returned by engine QA adapters."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.fingerprint import GameFingerprint
from core.models import ApplyReport


@dataclass(frozen=True, slots=True)
class QaResult:
    report: ApplyReport
    fingerprint: GameFingerprint
    translation_percentage: float
    reports_directory: Path
