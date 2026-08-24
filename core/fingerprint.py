"""Engine-neutral portable fingerprint result models and hashing helper."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from core.models import EngineId


@dataclass(frozen=True, slots=True)
class FingerprintFile:
    file: str
    sha256: str

    def to_json_dict(self) -> dict[str, str]:
        return {"file": self.file, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class GameFingerprint:
    value: str
    files: tuple[FingerprintFile, ...]


def build_content_fingerprint(
    game_directory: Path,
    engine: EngineId,
    selected_files: set[Path],
    *,
    format_tag: bytes,
) -> GameFingerprint:
    """Hash engine and selected relative file contents without absolute paths."""

    files: list[FingerprintFile] = []
    for path in sorted(
        selected_files,
        key=lambda item: item.relative_to(game_directory).as_posix(),
    ):
        relative = path.relative_to(game_directory).as_posix()
        files.append(FingerprintFile(file=relative, sha256=sha256_file(path)))

    digest = hashlib.sha256()
    digest.update(format_tag)
    digest.update(engine.value.encode("utf-8"))
    digest.update(b"\0")
    for item in files:
        digest.update(item.file.encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.sha256.encode("ascii"))
        digest.update(b"\0")
    return GameFingerprint(value=digest.hexdigest(), files=tuple(files))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
