"""Built-in engine adapter registration."""

from __future__ import annotations

from core.registry import EngineRegistry
from engines.rpgmaker.engine import RpgMakerEngine
from engines.wolf.engine import WolfRPGEngine


def create_engine_registry() -> EngineRegistry:
    """Return a fresh registry so callers cannot mutate global adapter state."""

    return EngineRegistry((RpgMakerEngine(), WolfRPGEngine()))


__all__ = ["create_engine_registry"]
