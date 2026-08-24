"""Portable path helpers. No machine-specific path is persisted."""

from __future__ import annotations

from pathlib import Path


def resolve_input_directory(path: Path, *, base: Path | None = None) -> Path:
    """Resolve a user-supplied path at runtime and verify it is a directory."""

    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = (base or Path.cwd()) / candidate
    candidate = candidate.resolve()

    if not candidate.exists():
        raise FileNotFoundError(f"game directory does not exist: {path}")
    if not candidate.is_dir():
        raise NotADirectoryError(f"game path is not a directory: {path}")
    return candidate


def portable_relative_path(path: Path, root: Path) -> str:
    """Return a stable forward-slash relative path for reports and JSON data."""

    return path.relative_to(root).as_posix()


def resolve_output_file(path: Path, *, base: Path | None = None) -> Path:
    """Resolve an output path without storing a machine-specific location."""

    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = (base or Path.cwd()) / candidate
    candidate = candidate.resolve()
    if candidate.exists() and candidate.is_dir():
        raise IsADirectoryError(f"output path is a directory: {path}")
    return candidate


def resolve_input_file(path: Path, *, base: Path | None = None) -> Path:
    """Resolve a user-supplied file path and verify it exists."""

    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = (base or Path.cwd()) / candidate
    candidate = candidate.resolve()
    if not candidate.exists():
        raise FileNotFoundError(f"input file does not exist: {path}")
    if not candidate.is_file():
        raise IsADirectoryError(f"input path is not a file: {path}")
    return candidate


def resolve_new_directory(path: Path, *, base: Path | None = None) -> Path:
    """Resolve a new directory path without creating or persisting it."""

    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = (base or Path.cwd()) / candidate
    return candidate.resolve()
