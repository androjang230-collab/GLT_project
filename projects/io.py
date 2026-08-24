"""Atomic portable file helpers used by project artifacts."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from projects.models import ProjectError


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ProjectError(
            f"invalid JSON in {path.name} at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    except (OSError, UnicodeError) as exc:
        raise ProjectError(f"cannot read {path.name}: {exc}") from exc


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        stream = path.open("r", encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ProjectError(f"cannot read {path.name}: {exc}") from exc
    with stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ProjectError(
                    f"invalid JSONL in {path.name} line {line_number}: {exc.msg}"
                ) from exc
            if not isinstance(payload, dict):
                raise ProjectError(
                    f"invalid JSONL in {path.name} line {line_number}: object required"
                )
            records.append(payload)
    return records


def write_json(path: Path, payload: dict[str, object]) -> None:
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    text = "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        for record in records
    )
    atomic_write_text(path, text)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            delete=False,
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(text)
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
