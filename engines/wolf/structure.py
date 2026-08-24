"""Read-only WOLF RPG Editor layout reconnaissance."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from core.models import EngineId
from core.paths import portable_relative_path
from core.structure import StructureFile, StructureReport
from engines.wolf.detector import WolfDetector
from engines.wolf.archive import classify_wolf_archive_role


_MAX_INSPECTION_FILES = 50_000
_HEADER_BYTES = 16
_MAX_HASH_SIZE = 1024 * 1024
_ARCHIVE_SUFFIXES = {".wolf", ".wolfx", ".assets"}
_FONT_SUFFIXES = {".ttf", ".ttc", ".otf", ".woff", ".woff2"}
_STRUCTURAL_SUFFIXES = {
    ".dat",
    ".mps",
    ".project",
    ".txt",
    ".csv",
    ".wolf",
    ".wolfx",
    ".assets",
}
_HEADER_SUFFIXES = {".dat", ".mps", ".project", ".wolf", ".wolfx", ".assets"}
_MEDIA_DIRECTORY_NAMES = {
    "audio",
    "bgm",
    "bgs",
    "se",
    "picture",
    "pictures",
    "charachip",
    "mapchip",
    "chipset",
    "movie",
    "movies",
    "font",
    "fonts",
}


class WolfStructureInspector:
    """Inventory names, sizes, small hashes, and bounded headers only."""

    def inspect(self, game_directory: Path) -> StructureReport:
        detection = WolfDetector().detect(game_directory)
        if not detection.detected:
            raise ValueError("WOLF RPG Editor could not be detected with sufficient confidence")

        files, directories, notes, truncated = _walk_read_only(game_directory)
        executable_paths: list[Path] = []
        data_paths: list[Path] = []
        archive_paths: list[Path] = []
        source_paths: list[Path] = []
        map_paths: list[Path] = []
        common_event_paths: list[Path] = []
        database_paths: list[Path] = []
        font_paths: list[Path] = []
        unknown_binary_paths: list[Path] = []
        data_directories: list[str] = []
        media_directories: list[str] = []

        for directory in directories:
            relative = portable_relative_path(directory, game_directory)
            parts = tuple(part.casefold() for part in directory.relative_to(game_directory).parts)
            if parts and parts[0] == "data" and len(parts) <= 3:
                data_directories.append(relative)
            if directory.name.casefold() in _MEDIA_DIRECTORY_NAMES:
                media_directories.append(relative)

        for path in files:
            relative_parts = tuple(
                part.casefold() for part in path.relative_to(game_directory).parts
            )
            suffix = path.suffix.casefold()
            in_data = bool(relative_parts and relative_parts[0] == "data")
            if suffix == ".exe":
                executable_paths.append(path)
            if suffix in _ARCHIVE_SUFFIXES:
                archive_paths.append(path)
                _, role_confidence, text_likelihood = classify_wolf_archive_role(
                    path.name
                )
                if role_confidence == "probable" and text_likelihood in {
                    "high",
                    "medium",
                }:
                    source_paths.append(path)
            if _is_possible_text_source(path, relative_parts):
                source_paths.append(path)
            if suffix == ".mps" and in_data:
                map_paths.append(path)
            if path.name.casefold() == "commonevent.dat" and relative_parts[:2] == (
                "data",
                "basicdata",
            ):
                common_event_paths.append(path)
            if path.name.casefold().endswith("database.dat") and relative_parts[:2] == (
                "data",
                "basicdata",
            ):
                database_paths.append(path)
            if _is_font_candidate(path):
                font_paths.append(path)
            if in_data and (suffix in _STRUCTURAL_SUFFIXES or _is_font_candidate(path)):
                data_paths.append(path)
            if suffix in {".dat", ".project", ".bin"} and path not in source_paths:
                unknown_binary_paths.append(path)

        info_cache: dict[Path, StructureFile] = {}

        def info(path: Path) -> StructureFile:
            if path not in info_cache:
                info_cache[path] = _file_info(path, game_directory)
            return info_cache[path]

        known_wolf_archives = [
            path for path in archive_paths if path.suffix.casefold() == ".wolf"
        ]
        individual_encrypted = [
            path for path in archive_paths if path.suffix.casefold() == ".wolfx"
        ]
        custom_assets = [
            path for path in archive_paths if path.suffix.casefold() == ".assets"
        ]
        unpacked_core = any(
            tuple(part.casefold() for part in path.relative_to(game_directory).parts)
            == ("data", "basicdata", "game.dat")
            for path in files
        )
        if known_wolf_archives and unpacked_core:
            packaging_type = "mixed"
        elif known_wolf_archives:
            packaging_type = "packed"
        elif unpacked_core and individual_encrypted:
            packaging_type = "mixed"
        elif unpacked_core:
            packaging_type = "unpacked"
        else:
            packaging_type = "unknown"

        if known_wolf_archives or individual_encrypted:
            encryption_status = "probably_encrypted"
        elif custom_assets:
            encryption_status = "unknown"
            notes.append(
                ".assets files may be custom-extension WOLF archives, but were not classified as encrypted"
            )
        elif unpacked_core:
            encryption_status = "not_detected"
        else:
            encryption_status = "unknown"

        if truncated:
            notes.append(
                f"inspection stopped after {_MAX_INSPECTION_FILES} files"
            )
        notes.append(
            "WOLF generation/version was not inferred because no verified version marker was available"
        )

        relevant_paths = set(
            executable_paths
            + data_paths
            + archive_paths
            + source_paths
            + font_paths
            + unknown_binary_paths
        )
        return StructureReport(
            engine=EngineId.WOLF_RPG_EDITOR,
            confidence=detection.confidence,
            evidence=detection.evidence,
            executables=_infos(executable_paths, info),
            data_files=_infos(data_paths, info),
            data_directories=tuple(sorted(set(data_directories), key=str.casefold)),
            archive_files=_infos(archive_paths, info),
            possible_text_sources=_infos(source_paths, info),
            possible_map_files=_infos(map_paths, info),
            possible_common_event_files=_infos(common_event_paths, info),
            possible_database_files=_infos(database_paths, info),
            possible_font_files=_infos(font_paths, info),
            media_directories=tuple(sorted(set(media_directories), key=str.casefold)),
            unknown_binary_files=_infos(unknown_binary_paths, info),
            possible_version=None,
            version_confidence="unknown",
            packaging_type=packaging_type,
            encryption_status=encryption_status,
            relevant_files=_infos(list(relevant_paths), info),
            notes=tuple(notes),
            extra_metadata={
                "inspection_file_count": len(files),
                "inspection_directory_count": len(directories),
                "inspection_truncated": truncated,
                "header_read_limit_bytes": _HEADER_BYTES,
                "small_core_hash_limit_bytes": _MAX_HASH_SIZE,
                "wolf_archive_count": len(known_wolf_archives),
                "wolfx_file_count": len(individual_encrypted),
                "custom_assets_candidate_count": len(custom_assets),
            },
        )


def _walk_read_only(
    root: Path,
) -> tuple[list[Path], list[Path], list[str], bool]:
    files: list[Path] = []
    directories: list[Path] = []
    notes: list[str] = []
    truncated = False
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        safe_directories: list[str] = []
        for name in sorted(directory_names, key=str.casefold):
            path = current_path / name
            if path.is_symlink():
                notes.append(
                    f"skipped symbolic-link directory: {portable_relative_path(path, root)}"
                )
            else:
                safe_directories.append(name)
                directories.append(path)
        directory_names[:] = safe_directories
        for name in sorted(file_names, key=str.casefold):
            path = current_path / name
            if path.is_symlink():
                notes.append(
                    f"skipped symbolic-link file: {portable_relative_path(path, root)}"
                )
                continue
            files.append(path)
            if len(files) >= _MAX_INSPECTION_FILES:
                truncated = True
                directory_names[:] = []
                break
        if truncated:
            break
    return files, directories, notes, truncated


def _is_possible_text_source(path: Path, relative_parts: tuple[str, ...]) -> bool:
    suffix = path.suffix.casefold()
    if suffix in {".mps", ".txt", ".csv"} and relative_parts[:1] == ("data",):
        return True
    if relative_parts[:2] != ("data", "basicdata"):
        return False
    name = path.name.casefold()
    return name in {"game.dat", "commonevent.dat", "tilesetdata.dat"} or name.endswith(
        "database.dat"
    )


def _is_font_candidate(path: Path) -> bool:
    suffixes = tuple(suffix.casefold() for suffix in path.suffixes)
    return path.suffix.casefold() in _FONT_SUFFIXES or (
        len(suffixes) >= 2
        and suffixes[-1] == ".wolfx"
        and suffixes[-2] in _FONT_SUFFIXES
    )


def _file_info(path: Path, root: Path) -> StructureFile:
    try:
        size = path.stat().st_size
    except OSError:
        size = -1
    suffix = path.suffix.casefold()
    header_hex = None
    if suffix in _HEADER_SUFFIXES:
        try:
            with path.open("rb") as stream:
                header_hex = stream.read(_HEADER_BYTES).hex()
        except OSError:
            header_hex = None
    sha256 = None
    if 0 <= size <= _MAX_HASH_SIZE and _is_small_hash_candidate(path):
        try:
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(64 * 1024), b""):
                    digest.update(chunk)
            sha256 = digest.hexdigest()
        except OSError:
            sha256 = None
    metadata: dict[str, object] = {}
    if suffix in _ARCHIVE_SUFFIXES:
        role, role_confidence, text_likelihood = classify_wolf_archive_role(
            path.name
        )
        archive_type = {
            ".wolf": "wolf_archive",
            ".wolfx": "wolfx_individual_encrypted_file",
            ".assets": "wolf_custom_extension_candidate",
        }[suffix]
        metadata = {
            "archive_type": archive_type,
            "probable_role": role,
            "role_confidence": role_confidence,
            "role_basis": "filename_heuristic",
            "text_likelihood": text_likelihood,
        }
    return StructureFile(
        file=portable_relative_path(path, root),
        size=size,
        sha256=sha256,
        header_hex=header_hex,
        metadata=metadata,
    )


def _is_small_hash_candidate(path: Path) -> bool:
    name = path.name.casefold()
    return name in {"game.dat", "commonevent.dat", "tilesetdata.dat"} or name.endswith(
        "database.dat"
    )


def _infos(paths: list[Path], factory) -> tuple[StructureFile, ...]:
    return tuple(
        factory(path)
        for path in sorted(set(paths), key=lambda item: item.as_posix().casefold())
    )
