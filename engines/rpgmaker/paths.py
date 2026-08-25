"""Shared RPG Maker game/content-root path resolution."""

from __future__ import annotations

from pathlib import Path


def resolve_rpgmaker_content_root(game_directory: Path) -> Path:
    """Return an MV/MZ content root while preserving legacy fallback behavior."""

    if (game_directory / "data").is_dir() and (game_directory / "js").is_dir():
        return game_directory
    packaged = game_directory / "www"
    if (packaged / "data").is_dir() and (packaged / "js").is_dir():
        return packaged
    return game_directory


__all__ = ["resolve_rpgmaker_content_root"]
