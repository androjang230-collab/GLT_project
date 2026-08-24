"""Canonical RPG Maker adapter import.

The legacy ``engines.rpgmaker.detector.RpgMakerEngine`` path remains valid for
existing integrations and tests.
"""

from engines.rpgmaker.detector import RpgMakerEngine

__all__ = ["RpgMakerEngine"]
