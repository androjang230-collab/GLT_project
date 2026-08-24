"""Engine-neutral serialization for common GLT translation entries."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path

from core.models import TranslationEntry


def write_jsonl(
    entries: Iterable[TranslationEntry],
    output_file: Path,
    *,
    overwrite: bool = True,
) -> None:
    """Atomically write UTF-8 JSONL in the established common field order."""

    output_file.parent.mkdir(parents=True, exist_ok=True)
    if not overwrite and output_file.exists():
        raise FileExistsError(f"output JSONL already exists: {output_file}")
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            delete=False,
            dir=output_file.parent,
            prefix=f".{output_file.name}.",
            suffix=".tmp",
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            for entry in entries:
                json.dump(
                    entry.to_json_dict(),
                    temporary_file,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        if overwrite:
            os.replace(temporary_path, output_file)
        else:
            os.link(temporary_path, output_file)
            temporary_path.unlink()
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


__all__ = ["write_jsonl"]
