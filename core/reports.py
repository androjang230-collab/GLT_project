"""Engine-neutral report writers."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from core.models import ApplyReport
from core.version import SCHEMA_VERSION, TOOL_VERSION


def write_dry_run_report(reports_directory: Path, report: ApplyReport) -> Path:
    reports_directory.mkdir(parents=True, exist_ok=True)
    report_file = reports_directory / "dry_run_report.json"
    payload = {
        "tool_version": TOOL_VERSION,
        "schema_version": SCHEMA_VERSION,
        "dry_run": True,
        **report.to_json_dict(),
    }
    _write_json_atomic(report_file, payload)
    return report_file


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
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
            json.dump(payload, temporary_file, ensure_ascii=False, indent=2)
            temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
