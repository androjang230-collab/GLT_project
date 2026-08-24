"""Portable content fingerprint for an RPG Maker translation source."""

from __future__ import annotations

from pathlib import Path

from core.fingerprint import (
    FingerprintFile,
    GameFingerprint,
    build_content_fingerprint,
)
from core.models import EngineId
from engines.rpgmaker.extractor import RpgMakerExtractor


def calculate_game_fingerprint(
    game_directory: Path,
    engine: EngineId,
) -> GameFingerprint:
    """Hash engine plus selected relative paths/content; never hash absolute paths."""

    selected = set(RpgMakerExtractor.source_files(game_directory))
    map_infos = game_directory / "data/MapInfos.json"
    if map_infos.is_file():
        selected.add(map_infos)

    return build_content_fingerprint(
        game_directory,
        engine,
        selected,
        format_tag=b"glt-game-fingerprint-v1\0",
    )


__all__ = [
    "FingerprintFile",
    "GameFingerprint",
    "calculate_game_fingerprint",
]
