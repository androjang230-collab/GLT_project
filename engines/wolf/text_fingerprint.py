"""Portable fingerprints for WOLF official text-export directories."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from core.paths import portable_relative_path


@dataclass(frozen=True, slots=True)
class WolfSourceFileFingerprint:
    path: str
    size: int
    sha256: str

    def to_json_dict(self) -> dict[str, object]:
        return {"path": self.path, "size": self.size, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class WolfSourceFingerprint:
    algorithm: str
    value: str
    files: tuple[WolfSourceFileFingerprint, ...]

    def to_json_dict(self) -> dict[str, object]:
        return {
            "algorithm": self.algorithm,
            "value": self.value,
            "file_count": len(self.files),
            "files": [item.to_json_dict() for item in self.files],
        }

    def file_hash(self, portable_path: str) -> str | None:
        key = portable_path.casefold()
        return next(
            (item.sha256 for item in self.files if item.path.casefold() == key),
            None,
        )


def calculate_wolf_source_fingerprint(root: Path) -> WolfSourceFingerprint:
    """Hash every regular file without including its machine-specific root path."""

    root = root.resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"WOLF text source directory does not exist: {root}")
    paths: list[Path] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"symbolic links are not supported in WOLF text source: {path}")
        if path.is_file():
            paths.append(path)
    paths.sort(key=lambda item: portable_relative_path(item, root).casefold())

    aggregate = hashlib.sha256()
    files: list[WolfSourceFileFingerprint] = []
    for path in paths:
        relative = portable_relative_path(path, root)
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        files.append(WolfSourceFileFingerprint(relative, len(data), digest))
        encoded_path = relative.encode("utf-8")
        aggregate.update(len(encoded_path).to_bytes(8, "big"))
        aggregate.update(encoded_path)
        aggregate.update(len(data).to_bytes(8, "big"))
        aggregate.update(bytes.fromhex(digest))
    return WolfSourceFingerprint("sha256", aggregate.hexdigest(), tuple(files))


__all__ = [
    "WolfSourceFileFingerprint",
    "WolfSourceFingerprint",
    "calculate_wolf_source_fingerprint",
]
