"""Read-only WOLF native-format research primitives.

This package deliberately exposes reconnaissance and logical models only.  It
does not decrypt, unpack, patch, or serialize WOLF native data.
"""

from engines.wolf.native.models import (
    EvidenceGrade,
    NativeDocument,
    NativeLocation,
    NativeRecord,
    NativeTextField,
    WolfNativeResearchReport,
)
from engines.wolf.native.probe import WolfNativeProbe, write_native_research_report

__all__ = [
    "EvidenceGrade",
    "NativeDocument",
    "NativeLocation",
    "NativeRecord",
    "NativeTextField",
    "WolfNativeProbe",
    "WolfNativeResearchReport",
    "write_native_research_report",
]
