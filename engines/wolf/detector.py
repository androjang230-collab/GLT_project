"""Conservative read-only WOLF RPG Editor detection."""

from __future__ import annotations

import os
from pathlib import Path

from core.models import DetectionResult, EngineId
from core.paths import portable_relative_path


DETECTION_THRESHOLD = 80
_EXECUTABLE_NAMES = {"game.exe", "gamepro.exe"}
_DATABASE_SUFFIX = "database.dat"
_MAX_DETECTION_ENTRIES = 2_000


class WolfDetector:
    """Score multiple official WOLF layout artifacts without reading payloads."""

    def detect(self, game_directory: Path) -> DetectionResult:
        if not game_directory.is_dir():
            return DetectionResult.unknown()

        evidence: list[Path] = []
        score = 0
        root_children = _children_by_name(game_directory)

        executable = _first_named(root_children, _EXECUTABLE_NAMES, file_only=True)
        if executable is not None:
            evidence.append(executable)
            score += 30

        editor = root_children.get("editor.exe")
        if editor is not None and editor.is_file():
            evidence.append(editor)
            score += 3
        config = root_children.get("config.exe")
        if config is not None and config.is_file():
            evidence.append(config)
            score += 2

        data_directory = root_children.get("data")
        basic_directory: Path | None = None
        if data_directory is not None and data_directory.is_dir():
            evidence.append(data_directory)
            score += 5
            basic_directory = _children_by_name(data_directory).get("basicdata")

        if basic_directory is not None and basic_directory.is_dir():
            basic_children = _children_by_name(basic_directory)
            game_data = basic_children.get("game.dat")
            if game_data is not None and game_data.is_file():
                evidence.append(game_data)
                score += 45
            common_events = basic_children.get("commonevent.dat")
            if common_events is not None and common_events.is_file():
                evidence.append(common_events)
                score += 8
            tile_data = basic_children.get("tilesetdata.dat")
            if tile_data is not None and tile_data.is_file():
                evidence.append(tile_data)
                score += 3
            databases = sorted(
                (
                    path
                    for name, path in basic_children.items()
                    if name.endswith(_DATABASE_SUFFIX) and path.is_file()
                ),
                key=lambda path: path.name.casefold(),
            )
            if databases:
                evidence.append(databases[0])
                score += 6

        map_file = (
            _find_first_suffix(data_directory, ".mps")
            if data_directory is not None and data_directory.is_dir()
            else None
        )
        if map_file is not None:
            evidence.append(map_file)
            score += 6

        archive_candidates = _direct_archive_candidates(game_directory, data_directory)
        data_archive = next(
            (path for path in archive_candidates if path.name.casefold() == "data.wolf"),
            None,
        )
        if data_archive is not None:
            evidence.append(data_archive)
            score += 55
        else:
            wolf_archives = [
                path for path in archive_candidates if path.suffix.casefold() == ".wolf"
            ]
            basic_archive = next(
                (
                    path
                    for path in wolf_archives
                    if path.name.casefold() == "basicdata.wolf"
                    and path.parent == data_directory
                ),
                None,
            )
            if basic_archive is not None:
                evidence.append(basic_archive)
                score += 45
            elif wolf_archives:
                evidence.extend(wolf_archives[:2])
                score += 25 + min(10, max(0, len(wolf_archives) - 1) * 5)

        individual_encrypted = next(
            (
                path
                for path in archive_candidates
                if path.suffix.casefold() == ".wolfx"
            ),
            None,
        )
        if individual_encrypted is not None:
            evidence.append(individual_encrypted)
            score += 5

        custom_asset = next(
            (
                path
                for path in archive_candidates
                if path.suffix.casefold() == ".assets"
            ),
            None,
        )
        if custom_asset is not None:
            evidence.append(custom_asset)
            score += 5

        confidence = min(99, score)
        portable_evidence = tuple(
            dict.fromkeys(portable_relative_path(path, game_directory) for path in evidence)
        )
        if confidence >= DETECTION_THRESHOLD:
            return DetectionResult(
                engine=EngineId.WOLF_RPG_EDITOR,
                confidence=confidence,
                evidence=portable_evidence,
                detected=True,
            )
        return DetectionResult.unknown(
            confidence=min(confidence, DETECTION_THRESHOLD - 1),
            evidence=portable_evidence,
        )


def _children_by_name(directory: Path) -> dict[str, Path]:
    try:
        return {path.name.casefold(): path for path in directory.iterdir()}
    except OSError:
        return {}


def _first_named(
    children: dict[str, Path],
    names: set[str],
    *,
    file_only: bool,
) -> Path | None:
    for name in sorted(names):
        candidate = children.get(name)
        if candidate is not None and (not file_only or candidate.is_file()):
            return candidate
    return None


def _direct_archive_candidates(
    game_directory: Path,
    data_directory: Path | None,
) -> list[Path]:
    directories = [game_directory]
    if data_directory is not None and data_directory.is_dir():
        directories.append(data_directory)
    candidates: list[Path] = []
    for directory in directories:
        for path in _children_by_name(directory).values():
            if path.is_file() and path.suffix.casefold() in {".wolf", ".wolfx", ".assets"}:
                candidates.append(path)
    return sorted(candidates, key=lambda path: path.as_posix().casefold())


def _find_first_suffix(directory: Path, suffix: str) -> Path | None:
    checked = 0
    stack = [directory]
    while stack and checked < _MAX_DETECTION_ENTRIES:
        current = stack.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda item: item.name.casefold())
        except OSError:
            continue
        for entry in entries:
            checked += 1
            if checked > _MAX_DETECTION_ENTRIES:
                break
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False) and entry.name.casefold().endswith(suffix):
                    return Path(entry.path)
            except OSError:
                continue
    return None
