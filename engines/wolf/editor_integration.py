"""Opt-in integration validation for the official WOLF Editor Text I/O CLI."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from core.translation_io import write_jsonl
from core.version import TOOL_VERSION
from engines.wolf.text_extractor import WolfTextExtractor
from engines.wolf.text_fingerprint import calculate_wolf_source_fingerprint
from engines.wolf.text_inspector import WolfTextInspector, decode_auto_text
from engines.wolf.text_writer import WolfTextWriter


WOLF_EDITOR_ENV = "GLT_WOLF_EDITOR"
WOLF_EDITOR_TARGETS = frozenset({"ALL", "BASIC", "MAP"})
WOLF_EDITOR_FIXTURE_KIND = "self_generated_official_export"
DEFAULT_EDITOR_TIMEOUT_SECONDS = 120
MAX_EDITOR_TIMEOUT_SECONDS = 3600
MAX_KOREAN_TRIAL_ENTRIES = 3
KOREAN_ROUNDTRIP_TEXT = "GLT 0.7.6 한국어 왕복 테스트입니다."


@dataclass(frozen=True, slots=True)
class WolfEditorIssue:
    severity: str
    code: str
    reason: str

    def to_json_dict(self) -> dict[str, str]:
        return {"severity": self.severity, "code": self.code, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class WolfEditorDetection:
    detected: bool
    editor_file: str
    editor_sha256: str
    editor_version: str | None
    version_source: str
    provenance: str
    evidence: tuple[str, ...]
    issues: tuple[WolfEditorIssue, ...] = ()

    def to_json_dict(self) -> dict[str, object]:
        return {
            "editor_detected": self.detected,
            "editor_file": self.editor_file,
            "editor_sha256": self.editor_sha256,
            "editor_version": self.editor_version,
            "version_source": self.version_source,
            "provenance": self.provenance,
            "evidence": list(self.evidence),
            "issues": [item.to_json_dict() for item in self.issues],
        }


@dataclass(frozen=True, slots=True)
class WolfEditorInvocation:
    mode: str
    target: str
    command: tuple[str, ...]
    working_directory: str
    exit_code: int | None
    timed_out: bool
    duration_ms: int
    stdout_bytes: int
    stderr_bytes: int
    stdout_sha256: str
    stderr_sha256: str
    output_exists: bool
    success: bool
    reason: str

    def to_json_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "target": self.target,
            "command": list(self.command),
            "working_directory": self.working_directory,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "duration_ms": self.duration_ms,
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
            "stdout_sha256": self.stdout_sha256,
            "stderr_sha256": self.stderr_sha256,
            "output_exists": self.output_exists,
            "success": self.success,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class WolfEncodingTrial:
    name: str
    requested_encoding: str
    editor_attempted: bool
    status: str
    txtinput_success: bool | None
    reexport_success: bool | None
    semantic_equal: bool | None
    byte_equal: bool | None
    korean_preserved: bool | None
    replacement_character_found: bool | None
    comma_preserved: bool | None
    reason: str
    translated_entry_count: int = 0
    translated_entry_types: tuple[str, ...] = ()
    control_codes_preserved: bool | None = None
    mojibake_found: bool | None = None
    choice_preserved: bool | None = None

    def to_json_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "requested_encoding": self.requested_encoding,
            "editor_attempted": self.editor_attempted,
            "status": self.status,
            "txtinput_success": self.txtinput_success,
            "reexport_success": self.reexport_success,
            "semantic_equal": self.semantic_equal,
            "byte_equal": self.byte_equal,
            "korean_preserved": self.korean_preserved,
            "replacement_character_found": self.replacement_character_found,
            "comma_preserved": self.comma_preserved,
            "reason": self.reason,
            "translated_entry_count": self.translated_entry_count,
            "translated_entry_types": list(self.translated_entry_types),
            "control_codes_preserved": self.control_codes_preserved,
            "mojibake_found": self.mojibake_found,
            "choice_preserved": self.choice_preserved,
        }


@dataclass(frozen=True, slots=True)
class WolfEditorIntegrationReport:
    tool_version: str
    fixture_kind: str
    official_verification: str
    editor_detected: bool
    editor_file: str
    editor_version: str | None
    editor_version_source: str
    editor_sha256: str
    target: str
    allow_editor_import: bool
    txtoutput_success: bool
    txtinput_success: bool | None
    reexport_success: bool | None
    source_fingerprint: str
    imported_fingerprint: str
    reexport_fingerprint: str
    baseline_export: dict[str, object]
    editor_noop_roundtrip: dict[str, object]
    glt_noop_roundtrip: dict[str, object]
    encoding_trials: tuple[WolfEncodingTrial, ...]
    korean_roundtrip: str
    comma_roundtrip: str
    choice_validation: dict[str, object]
    database_validation: dict[str, object]
    canonical_id: dict[str, object]
    invocations: tuple[WolfEditorInvocation, ...]
    warnings: int
    errors: int
    blockers: int
    issues: tuple[WolfEditorIssue, ...]

    @property
    def passed(self) -> bool:
        return self.errors == 0 and self.blockers == 0

    def to_json_dict(self) -> dict[str, object]:
        return {
            "tool_version": self.tool_version,
            "fixture_kind": self.fixture_kind,
            "official_verification": self.official_verification,
            "editor_detected": self.editor_detected,
            "editor_file": self.editor_file,
            "editor_version": self.editor_version,
            "editor_version_source": self.editor_version_source,
            "editor_sha256": self.editor_sha256,
            "target": self.target,
            "allow_editor_import": self.allow_editor_import,
            "txtoutput_success": self.txtoutput_success,
            "txtinput_success": self.txtinput_success,
            "reexport_success": self.reexport_success,
            "source_fingerprint": self.source_fingerprint,
            "imported_fingerprint": self.imported_fingerprint,
            "reexport_fingerprint": self.reexport_fingerprint,
            "baseline_export": self.baseline_export,
            "editor_noop_roundtrip": self.editor_noop_roundtrip,
            "glt_noop_roundtrip": self.glt_noop_roundtrip,
            "encoding_trials": [item.to_json_dict() for item in self.encoding_trials],
            "korean_roundtrip": self.korean_roundtrip,
            "comma_roundtrip": self.comma_roundtrip,
            "choice_validation": self.choice_validation,
            "database_validation": self.database_validation,
            "canonical_id": self.canonical_id,
            "invocations": [item.to_json_dict() for item in self.invocations],
            "warnings": self.warnings,
            "errors": self.errors,
            "blockers": self.blockers,
            "issues": [item.to_json_dict() for item in self.issues],
        }


@dataclass(frozen=True, slots=True)
class WolfEditorIntegrationResult:
    report: WolfEditorIntegrationReport
    workspace: Path | None
    workspace_preserved: bool


class WolfEditorInvokerProtocol(Protocol):
    provenance: str

    def invoke(
        self,
        editor: Path,
        working_directory: Path,
        *,
        mode: str,
        text_folder: str,
        target: str,
        timeout_seconds: int,
    ) -> WolfEditorInvocation: ...


class WolfEditorLocator:
    """Resolve only explicit, configured, or project-adjacent candidates."""

    def resolve(self, project: Path | None, explicit: Path | None) -> Path | None:
        if explicit is not None:
            return explicit.expanduser().resolve()
        configured = os.environ.get(WOLF_EDITOR_ENV)
        if configured:
            return Path(configured).expanduser().resolve()
        if project is None:
            return None
        candidates = [
            project / "Editor.exe",
            project / "EditorPro.exe",
        ]
        existing = [item.resolve() for item in candidates if item.is_file()]
        if len(existing) > 1:
            raise ValueError("multiple project-adjacent WOLF Editor candidates were found")
        return existing[0] if existing else None

    def check(
        self,
        editor: Path | None,
        *,
        project: Path | None = None,
        provenance: str = "explicit_path",
    ) -> WolfEditorDetection:
        if editor is None:
            return WolfEditorDetection(
                False,
                "",
                "",
                None,
                "not_available",
                "not_available",
                (),
                (WolfEditorIssue("blocker", "EDITOR_NOT_FOUND", "no explicit, configured, or project-adjacent Editor was found"),),
            )
        editor = editor.resolve()
        issues: list[WolfEditorIssue] = []
        evidence: list[str] = []
        if not editor.is_file() or editor.is_symlink():
            issues.append(WolfEditorIssue("blocker", "EDITOR_FILE_INVALID", "Editor path is not a regular file"))
            return WolfEditorDetection(False, editor.name, "", None, "unknown", provenance, tuple(evidence), tuple(issues))
        data = editor.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if editor.suffix.casefold() != ".exe":
            issues.append(WolfEditorIssue("blocker", "EDITOR_EXTENSION_INVALID", "Editor candidate must be a Windows .exe"))
        elif not data.startswith(b"MZ"):
            issues.append(WolfEditorIssue("blocker", "EDITOR_PE_SIGNATURE_MISSING", "Editor candidate has no Windows PE MZ signature"))
        else:
            evidence.append("Windows PE MZ signature")
        known_name = editor.name.casefold() in {"editor.exe", "editorpro.exe"}
        if known_name:
            evidence.append("recognized official editor filename")
        adjacent = _wolf_project_evidence(project or editor.parent)
        evidence.extend(adjacent)
        if not known_name and not adjacent:
            issues.append(WolfEditorIssue("blocker", "EDITOR_IDENTITY_UNCONFIRMED", "renamed candidate needs adjacent WOLF project evidence"))
        version = _read_file_version(editor)
        version_source = "pe_fixed_file_info" if version else "not_reliably_available"
        if version:
            evidence.append("PE fixed file version was read")
        return WolfEditorDetection(
            not any(item.severity == "blocker" for item in issues),
            editor.name,
            digest,
            version,
            version_source,
            provenance,
            tuple(evidence),
            tuple(issues),
        )


class SubprocessWolfEditorInvoker:
    """Invoke the documented Editor CLI without a shell or automatic retries."""

    provenance = WOLF_EDITOR_FIXTURE_KIND

    def invoke(
        self,
        editor: Path,
        working_directory: Path,
        *,
        mode: str,
        text_folder: str,
        target: str,
        timeout_seconds: int,
    ) -> WolfEditorInvocation:
        if mode not in {"txtoutput", "txtinput"}:
            raise ValueError(f"unsupported Editor text mode: {mode}")
        target = target.upper()
        if target not in WOLF_EDITOR_TARGETS:
            raise ValueError(f"unsupported Editor target: {target}")
        if not _safe_relative_name(text_folder):
            raise ValueError("Editor text folder must be one portable relative directory name")
        command = [
            str(editor),
            f"-{mode}",
            "-txt_folder",
            text_folder,
            "-target",
            target,
        ]
        portable_command = (editor.name, *command[1:])
        started = time.monotonic()
        stdout = b""
        stderr = b""
        exit_code: int | None = None
        timed_out = False
        reason = ""
        try:
            # Real Editor 3.682 BASIC/ALL runs can fail to terminate while
            # Python is waiting on anonymous PIPEs. File-backed capture keeps
            # the no-console/no-content-reporting policy without that deadlock.
            with (
                tempfile.TemporaryFile() as stdout_file,
                tempfile.TemporaryFile() as stderr_file,
            ):
                try:
                    completed = subprocess.run(
                        command,
                        cwd=working_directory,
                        shell=False,
                        stdout=stdout_file,
                        stderr=stderr_file,
                        timeout=timeout_seconds,
                        check=False,
                        creationflags=(
                            subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                        ),
                    )
                    exit_code = completed.returncode
                    if exit_code != 0:
                        reason = f"Editor exited with code {exit_code}"
                except subprocess.TimeoutExpired:
                    timed_out = True
                    reason = f"Editor exceeded timeout of {timeout_seconds} seconds"
                stdout_file.seek(0)
                stdout = stdout_file.read()
                stderr_file.seek(0)
                stderr = stderr_file.read()
        except OSError as exc:
            reason = f"Editor launch failed: {exc.__class__.__name__}"
        duration_ms = round((time.monotonic() - started) * 1000)
        output_path = working_directory / text_folder
        output_exists = output_path.is_dir()
        success = not timed_out and exit_code == 0 and output_exists
        if not reason and not output_exists:
            reason = "expected Editor text folder was not created"
        return WolfEditorInvocation(
            mode,
            target,
            portable_command,
            ".",
            exit_code,
            timed_out,
            duration_ms,
            len(stdout),
            len(stderr),
            hashlib.sha256(stdout).hexdigest(),
            hashlib.sha256(stderr).hexdigest(),
            output_exists,
            success,
            reason or "completed",
        )


class WolfEditorIntegrationValidator:
    """Run official Text I/O only in an isolated copy and record evidence."""

    def __init__(self, invoker: WolfEditorInvokerProtocol | None = None) -> None:
        self.invoker = invoker or SubprocessWolfEditorInvoker()

    def validate(
        self,
        project: Path,
        *,
        editor: Path | None = None,
        target: str = "ALL",
        allow_editor_import: bool = False,
        workspace: Path | None = None,
        keep_workspace: bool = False,
        timeout_seconds: int = DEFAULT_EDITOR_TIMEOUT_SECONDS,
    ) -> WolfEditorIntegrationResult:
        project = project.resolve()
        if not project.is_dir():
            raise NotADirectoryError(f"WOLF test project does not exist: {project}")
        target = target.upper()
        if target not in WOLF_EDITOR_TARGETS:
            raise ValueError(f"target must be one of: {', '.join(sorted(WOLF_EDITOR_TARGETS))}")
        if timeout_seconds < 1 or timeout_seconds > MAX_EDITOR_TIMEOUT_SECONDS:
            raise ValueError(f"timeout must be between 1 and {MAX_EDITOR_TIMEOUT_SECONDS} seconds")

        source_before = calculate_wolf_source_fingerprint(project)
        locator = WolfEditorLocator()
        explicit = editor is not None
        resolved_editor = locator.resolve(project, editor)
        provenance = (
            "explicit_path"
            if explicit
            else "configured_environment"
            if os.environ.get(WOLF_EDITOR_ENV)
            else "project_adjacent"
            if resolved_editor is not None
            else "not_available"
        )
        detection = locator.check(resolved_editor, project=project, provenance=provenance)
        if not detection.detected or resolved_editor is None:
            report = _empty_integration_report(
                detection,
                target,
                allow_editor_import,
                source_before.value,
                tuple(detection.issues),
            )
            return WolfEditorIntegrationResult(report, None, False)
        if not _wolf_project_evidence(project):
            project_issue = WolfEditorIssue(
                "blocker",
                "WOLF_PROJECT_EVIDENCE_MISSING",
                "project root has no Data, BasicData, or WOLF game executable evidence",
            )
            report = _empty_integration_report(
                detection,
                target,
                allow_editor_import,
                source_before.value,
                (project_issue,),
            )
            return WolfEditorIntegrationResult(report, None, False)

        workspace_path, auto_workspace = _create_workspace(workspace, project, resolved_editor)
        issues: list[WolfEditorIssue] = []
        invocations: list[WolfEditorInvocation] = []
        preserved = True
        report: WolfEditorIntegrationReport | None = None
        try:
            runtime = workspace_path / "runtime_template"
            runtime_editor = _prepare_runtime(project, resolved_editor, runtime)
            baseline_name = "glt_baseline_auto"
            baseline_invocation = self.invoker.invoke(
                runtime_editor,
                runtime,
                mode="txtoutput",
                text_folder=baseline_name,
                target=target,
                timeout_seconds=timeout_seconds,
            )
            invocations.append(baseline_invocation)
            baseline = runtime / baseline_name
            if not baseline_invocation.success:
                issues.append(WolfEditorIssue("blocker", "EDITOR_TXTOUTPUT_FAILED", baseline_invocation.reason))
                report = _integration_report(
                    detection=detection,
                    target=target,
                    allow_editor_import=allow_editor_import,
                    source_fingerprint=source_before.value,
                    fixture_kind=self.invoker.provenance,
                    invocations=invocations,
                    issues=issues,
                )
                return WolfEditorIntegrationResult(report, workspace_path, True)

            baseline_summary = _export_summary(baseline, self.invoker.provenance)
            baseline_errors = [
                item
                for item in baseline_summary.get("issues", [])
                if item.get("severity") == "error"
            ]
            if not baseline_summary.get("file_count") or baseline_errors:
                issues.append(
                    WolfEditorIssue(
                        "blocker",
                        "EDITOR_TXTOUTPUT_INVALID",
                        "Editor output was empty, partial, undecodable, or structurally invalid",
                    )
                )
                report = _integration_report(
                    detection=detection,
                    target=target,
                    allow_editor_import=allow_editor_import,
                    source_fingerprint=source_before.value,
                    fixture_kind=self.invoker.provenance,
                    invocations=invocations,
                    issues=issues,
                    txtoutput_success=False,
                    baseline_export=baseline_summary,
                )
                return WolfEditorIntegrationResult(report, workspace_path, True)
            extraction = WolfTextExtractor().inspect_and_convert(baseline)
            baseline_jsonl = workspace_path / "baseline.jsonl"
            write_jsonl(extraction.entries, baseline_jsonl, overwrite=False)
            glt_noop = workspace_path / "glt_noop_auto"
            glt_writer_report = WolfTextWriter().apply(
                baseline, baseline_jsonl, glt_noop
            )
            glt_noop_summary: dict[str, object] = {
                "writer_blocked": glt_writer_report.blocked,
                "byte_equal_to_baseline": (
                    glt_writer_report.source_fingerprint
                    == glt_writer_report.output_fingerprint
                ),
                "source_fingerprint": glt_writer_report.source_fingerprint,
                "output_fingerprint": glt_writer_report.output_fingerprint,
                "editor_import_attempted": False,
                "comparison": {},
            }
            if glt_writer_report.blocked:
                issues.append(WolfEditorIssue("blocker", "GLT_NOOP_WRITER_BLOCKED", "GLT no-op writer did not produce an Editor input candidate"))

            editor_noop: dict[str, object] = {
                "attempted": False,
                "txtinput_success": None,
                "reexport_success": None,
                "comparison": {},
            }
            imported_fingerprint = ""
            reexport_fingerprint = ""
            txtinput_success: bool | None = None
            reexport_success: bool | None = None
            trials: list[WolfEncodingTrial] = []

            if allow_editor_import and not glt_writer_report.blocked:
                direct = _run_editor_roundtrip(
                    self.invoker,
                    runtime,
                    runtime_editor.name,
                    baseline,
                    workspace_path / "editor_noop_runtime",
                    "direct_noop_input",
                    "direct_noop_reexport",
                    target,
                    timeout_seconds,
                )
                invocations.extend(direct["invocations"])
                editor_noop = {
                    "attempted": True,
                    "txtinput_success": direct["txtinput_success"],
                    "reexport_success": direct["reexport_success"],
                    "comparison": direct["comparison"],
                }
                glt_roundtrip = _run_editor_roundtrip(
                    self.invoker,
                    runtime,
                    runtime_editor.name,
                    glt_noop,
                    workspace_path / "glt_noop_runtime",
                    "glt_noop_input",
                    "glt_noop_reexport",
                    target,
                    timeout_seconds,
                )
                invocations.extend(glt_roundtrip["invocations"])
                glt_noop_summary["editor_import_attempted"] = True
                glt_noop_summary["comparison"] = glt_roundtrip["comparison"]
                glt_noop_summary["txtinput_success"] = glt_roundtrip["txtinput_success"]
                glt_noop_summary["reexport_success"] = glt_roundtrip["reexport_success"]
                txtinput_success = bool(direct["txtinput_success"] and glt_roundtrip["txtinput_success"])
                reexport_success = bool(direct["reexport_success"] and glt_roundtrip["reexport_success"])
                imported_fingerprint = str(glt_roundtrip["imported_fingerprint"])
                reexport_fingerprint = str(glt_roundtrip["reexport_fingerprint"])
                if not txtinput_success or not reexport_success:
                    issues.append(WolfEditorIssue("error", "EDITOR_NOOP_ROUNDTRIP_FAILED", "direct or GLT no-op Editor round-trip failed"))
                elif not bool(glt_roundtrip["comparison"].get("semantic_equal")):
                    issues.append(WolfEditorIssue("error", "EDITOR_NOOP_SEMANTIC_MISMATCH", "GLT no-op import/re-export changed semantic records"))

                for trial_name, encoding in (
                    ("source_encoding", "source"),
                    ("utf8_bom", "utf-8-sig"),
                    ("utf8_no_bom", "utf-8"),
                ):
                    trial, trial_invocations = _run_encoding_trial(
                        self.invoker,
                        baseline,
                        runtime,
                        runtime_editor.name,
                        workspace_path,
                        trial_name,
                        encoding,
                        target,
                        timeout_seconds,
                    )
                    trials.append(trial)
                    invocations.extend(trial_invocations)
            else:
                reason = "editor import requires --allow-editor-import"
                trials.extend(
                    WolfEncodingTrial(name, encoding, False, "unknown", None, None, None, None, None, None, None, reason)
                    for name, encoding in (
                        ("source_encoding", "source"),
                        ("utf8_bom", "utf-8-sig"),
                        ("utf8_no_bom", "utf-8"),
                    )
                )

            source_after = calculate_wolf_source_fingerprint(project)
            if source_after.value != source_before.value:
                issues.append(WolfEditorIssue("blocker", "ORIGINAL_PROJECT_CHANGED", "original project fingerprint changed during integration validation"))

            korean_status = _aggregate_korean_status(
                trials, self.invoker.provenance
            )
            comma_status = _aggregate_comma_status(
                trials, baseline_summary, self.invoker.provenance
            )
            choice_validation = _choice_validation(
                baseline, self.invoker.provenance, editor_noop, trials
            )
            database_validation = _database_validation(baseline, self.invoker.provenance, comma_status)
            official = _official_verification_status(
                self.invoker.provenance,
                allow_editor_import,
                editor_noop,
                glt_noop_summary,
                korean_status,
                comma_status,
            )
            if self.invoker.provenance != WOLF_EDITOR_FIXTURE_KIND:
                issues.append(WolfEditorIssue("warning", "NON_OFFICIAL_INVOCATION", "synthetic/emulated invocation cannot establish official Editor verification"))
            report = _integration_report(
                detection=detection,
                target=target,
                allow_editor_import=allow_editor_import,
                source_fingerprint=source_before.value,
                fixture_kind=self.invoker.provenance,
                invocations=invocations,
                issues=issues,
                official_verification=official,
                txtoutput_success=True,
                txtinput_success=txtinput_success,
                reexport_success=reexport_success,
                imported_fingerprint=imported_fingerprint,
                reexport_fingerprint=reexport_fingerprint,
                baseline_export=baseline_summary,
                editor_noop_roundtrip=editor_noop,
                glt_noop_roundtrip=glt_noop_summary,
                encoding_trials=tuple(trials),
                korean_roundtrip=korean_status,
                comma_roundtrip=comma_status,
                choice_validation=choice_validation,
                database_validation=database_validation,
            )
            failed = report.errors > 0 or report.blockers > 0
            preserved = keep_workspace or failed
            return WolfEditorIntegrationResult(
                report,
                workspace_path if preserved else None,
                preserved,
            )
        finally:
            if report is not None and not preserved and workspace_path.exists():
                shutil.rmtree(workspace_path)
            elif auto_workspace and report is None and workspace_path.exists():
                # Unexpected Python failures retain explicit workspaces but clean
                # automatically allocated scratch space.
                shutil.rmtree(workspace_path)


def _create_workspace(
    requested: Path | None, project: Path, editor: Path
) -> tuple[Path, bool]:
    if requested is None:
        return Path(tempfile.mkdtemp(prefix="glt-wolf-editor-")), True
    candidate = requested.expanduser().resolve()
    if candidate.exists():
        raise FileExistsError(f"integration workspace already exists: {candidate}")
    if candidate == project or candidate.is_relative_to(project):
        raise ValueError("integration workspace cannot be inside the original project")
    if candidate == editor.parent or candidate.is_relative_to(editor.parent):
        raise ValueError("integration workspace cannot be inside the original Editor directory")
    candidate.mkdir(parents=True)
    return candidate, False


def _prepare_runtime(project: Path, editor: Path, runtime: Path) -> Path:
    if editor.is_relative_to(project) and editor.parent != project:
        raise ValueError("Editor inside the project must be located at the project root")
    _copytree_no_links(project, runtime)
    try:
        relative_editor = editor.relative_to(project)
    except ValueError:
        relative_editor = Path(editor.name)
        for source in sorted(editor.parent.iterdir(), key=lambda item: item.name.casefold()):
            if source.is_symlink():
                raise ValueError(f"Editor runtime contains a symbolic link: {source.name}")
            if not source.is_file():
                continue
            if not (
                source == editor
                or source.suffix.casefold() == ".dll"
                or source.name.casefold().startswith("editor")
            ):
                continue
            destination = runtime / source.name
            if destination.exists():
                if destination.read_bytes() != source.read_bytes():
                    raise ValueError(f"project and Editor runtime file collide: {source.name}")
                continue
            shutil.copy2(source, destination)
    runtime_editor = runtime / relative_editor
    if not runtime_editor.is_file():
        raise FileNotFoundError("isolated runtime does not contain the Editor executable")
    if hashlib.sha256(runtime_editor.read_bytes()).digest() != hashlib.sha256(editor.read_bytes()).digest():
        raise RuntimeError("isolated Editor copy hash mismatch")
    return runtime_editor


def _copytree_no_links(source: Path, destination: Path) -> None:
    for item in source.rglob("*"):
        if item.is_symlink():
            raise ValueError(f"symbolic links are not supported in integration input: {item}")
    shutil.copytree(source, destination, copy_function=shutil.copy2)


def _export_summary(export: Path, fixture_kind: str) -> dict[str, object]:
    inspection = WolfTextInspector().inspect(export, fixture_kind=fixture_kind)
    extraction = WolfTextExtractor().inspect_and_convert(export)
    fingerprint = calculate_wolf_source_fingerprint(export)
    command_101 = [
        record
        for record in inspection.records
        if record.metadata.get("command_code") == "101"
    ]
    command_102 = [
        record
        for record in inspection.records
        if record.metadata.get("command_code") == "102"
    ]
    return {
        "fingerprint": fingerprint.value,
        "file_count": fingerprint.files.__len__(),
        "files": [item.to_json_dict() for item in fingerprint.files],
        "encoding": inspection.detected_encoding,
        "encoding_confidence": inspection.encoding_confidence,
        "bom": inspection.bom,
        "newline": inspection.newline_style,
        "final_newline": inspection.final_newline,
        "record_count": inspection.record_count,
        "translation_entry_count": len(extraction.entries),
        "translation_entry_ids": [entry.id for entry in extraction.entries],
        "choice_record_count": inspection.count_type("choice"),
        "database_record_count": sum(record.location.domain == "database" for record in inspection.records),
        "database_name_count": inspection.count_type("database_name"),
        "command_101_record_count": len(command_101),
        "command_101_verified_count": sum(
            record.classification.value == "verified_translatable"
            for record in command_101
        ),
        "command_102_option_count": len(command_102),
        "command_102_nested_option_count": sum(
            isinstance(record.metadata.get("command_indent"), int)
            and record.metadata.get("command_indent", 0) > 0
            for record in command_102
        ),
        "control_code_record_count": sum(
            bool(record.control_codes) for record in inspection.records
        ),
        "issues": [item.to_json_dict() for item in inspection.issues],
    }


def _run_editor_roundtrip(
    invoker: WolfEditorInvokerProtocol,
    runtime_template: Path,
    editor_name: str,
    input_export: Path,
    roundtrip_runtime: Path,
    input_name: str,
    reexport_name: str,
    target: str,
    timeout_seconds: int,
) -> dict[str, object]:
    _copytree_no_links(runtime_template, roundtrip_runtime)
    runtime_editor = roundtrip_runtime / editor_name
    input_destination = roundtrip_runtime / input_name
    _copytree_no_links(input_export, input_destination)
    txtinput = invoker.invoke(
        runtime_editor,
        roundtrip_runtime,
        mode="txtinput",
        text_folder=input_name,
        target=target,
        timeout_seconds=timeout_seconds,
    )
    imported_fingerprint = (
        calculate_wolf_source_fingerprint(roundtrip_runtime).value
        if txtinput.success
        else ""
    )
    if not txtinput.success:
        return {
            "invocations": (txtinput,),
            "txtinput_success": False,
            "reexport_success": False,
            "comparison": {},
            "imported_fingerprint": imported_fingerprint,
            "reexport_fingerprint": "",
            "reexport_path": None,
        }
    txtoutput = invoker.invoke(
        runtime_editor,
        roundtrip_runtime,
        mode="txtoutput",
        text_folder=reexport_name,
        target=target,
        timeout_seconds=timeout_seconds,
    )
    reexport = roundtrip_runtime / reexport_name
    comparison = (
        _compare_exports(input_export, reexport)
        if txtoutput.success
        else {}
    )
    reexport_fingerprint = (
        calculate_wolf_source_fingerprint(reexport).value
        if txtoutput.success
        else ""
    )
    return {
        "invocations": (txtinput, txtoutput),
        "txtinput_success": txtinput.success,
        "reexport_success": txtoutput.success,
        "comparison": comparison,
        "imported_fingerprint": imported_fingerprint,
        "reexport_fingerprint": reexport_fingerprint,
        "reexport_path": reexport if txtoutput.success else None,
    }


def _compare_exports(first: Path, second: Path) -> dict[str, object]:
    first_fingerprint = calculate_wolf_source_fingerprint(first)
    second_fingerprint = calculate_wolf_source_fingerprint(second)
    first_report = WolfTextInspector().inspect(first)
    second_report = WolfTextInspector().inspect(second)
    first_records = tuple(
        (
            item.id,
            item.type,
            item.normalized_view if item.normalized_view is not None else item.original,
            _semantic_control_codes(item),
            item.classification.value,
        )
        for item in first_report.records
    )
    second_records = tuple(
        (
            item.id,
            item.type,
            item.normalized_view if item.normalized_view is not None else item.original,
            _semantic_control_codes(item),
            item.classification.value,
        )
        for item in second_report.records
    )
    first_unknown = tuple(
        (item.source_file, item.kind, item.raw) for item in first_report.unknown_records
    )
    second_unknown = tuple(
        (item.source_file, item.kind, item.raw) for item in second_report.unknown_records
    )
    first_transport = tuple(
        (item.source_file, item.encoding, item.bom, item.newline_style, item.final_newline)
        for item in first_report.files
    )
    second_transport = tuple(
        (item.source_file, item.encoding, item.bom, item.newline_style, item.final_newline)
        for item in second_report.files
    )
    file_set_equal = tuple(item.path for item in first_fingerprint.files) == tuple(
        item.path for item in second_fingerprint.files
    )
    semantic_equal = (
        file_set_equal
        and first_records == second_records
        and first_unknown == second_unknown
        and first_report.sections == second_report.sections
    )
    return {
        "file_set_equal": file_set_equal,
        "record_order_equal": first_records == second_records,
        "unknown_records_equal": first_unknown == second_unknown,
        "sections_equal": first_report.sections == second_report.sections,
        "semantic_equal": semantic_equal,
        "transport_equal": first_transport == second_transport,
        "byte_equal": first_fingerprint.value == second_fingerprint.value,
        "first_fingerprint": first_fingerprint.value,
        "second_fingerprint": second_fingerprint.value,
    }


def _semantic_control_codes(record: object) -> tuple[str, ...]:
    return tuple(
        token
        for token in record.control_codes
        if not (record.type == "database_name" and token == "<<COMMA>>")
    )


def _run_encoding_trial(
    invoker: WolfEditorInvokerProtocol,
    baseline: Path,
    runtime_template: Path,
    editor_name: str,
    workspace: Path,
    trial_name: str,
    encoding: str,
    target: str,
    timeout_seconds: int,
) -> tuple[WolfEncodingTrial, tuple[WolfEditorInvocation, ...]]:
    trial_source = workspace / f"{trial_name}_source"
    _copytree_no_links(baseline, trial_source)
    try:
        if encoding != "source":
            _reencode_auto_text_tree(trial_source, encoding)
        extraction = WolfTextExtractor().inspect_and_convert(trial_source)
        if extraction.report.blocked or not extraction.entries:
            return (
                WolfEncodingTrial(trial_name, encoding, False, "unknown", None, None, None, None, None, None, None, "no verified entries were available after strict decoding"),
                (),
            )
        selected_entries = _select_korean_trial_entries(extraction.entries)
        expected_by_id = {
            entry.id: _korean_test_translation(entry) for entry in selected_entries
        }
        translated_entries = [
            replace(entry, translation=expected_by_id.get(entry.id, ""))
            for entry in extraction.entries
        ]
        translated_jsonl = workspace / f"{trial_name}.jsonl"
        write_jsonl(translated_entries, translated_jsonl, overwrite=False)
        translated_auto = workspace / f"{trial_name}_translated"
        writer_report = WolfTextWriter().apply(
            trial_source,
            translated_jsonl,
            translated_auto,
        )
        if writer_report.blocked:
            codes = ", ".join(sorted({item.issue_code for item in writer_report.issues if item.severity in {"error", "blocker"}}))
            return (
                WolfEncodingTrial(trial_name, encoding, False, "rejected", None, None, None, None, None, None, None, f"GLT writer blocked the encoding trial: {codes}"),
                (),
            )
        roundtrip = _run_editor_roundtrip(
            invoker,
            runtime_template,
            editor_name,
            translated_auto,
            workspace / f"{trial_name}_runtime",
            f"{trial_name}_input",
            f"{trial_name}_reexport",
            target,
            timeout_seconds,
        )
        if not roundtrip["txtinput_success"]:
            return (
                WolfEncodingTrial(trial_name, encoding, True, "rejected", False, False, None, None, None, None, None, "Editor txtinput failed"),
                tuple(roundtrip["invocations"]),
            )
        if not roundtrip["reexport_success"]:
            return (
                WolfEncodingTrial(trial_name, encoding, True, "rejected", True, False, None, None, None, None, None, "Editor re-export failed"),
                tuple(roundtrip["invocations"]),
            )
        reexport_path = roundtrip["reexport_path"]
        comparison = roundtrip["comparison"]
        output_report = WolfTextInspector().inspect(reexport_path)
        output_by_id = {record.id: record for record in output_report.records}
        selected_output = [
            output_by_id[entry.id]
            for entry in selected_entries
            if entry.id in output_by_id
        ]
        exact = len(selected_output) == len(selected_entries) and all(
            _semantic_record_text(output_by_id[entry.id]) == expected_by_id[entry.id]
            for entry in selected_entries
        )
        output_text = "\n".join(
            _semantic_record_text(record) for record in selected_output
        )
        korean = exact and ("한국어" in output_text or "검" in output_text)
        replacement = "\ufffd" in output_text
        question_replacement = "???" in output_text
        control_preserved = len(selected_output) == len(selected_entries) and all(
            tuple(
                token
                for token in output_by_id[entry.id].control_codes
                if token != "<<COMMA>>"
            )
            == tuple(token for token in entry.control_codes if token != "<<COMMA>>")
            for entry in selected_entries
        )
        mojibake = replacement or question_replacement or not exact
        comma_expected = any(
            entry.type == "database_name" for entry in translated_entries
        )
        comma_preserved = (
            any(
                record.type == "database_name"
                and (
                    record.normalized_view == "검, 대형"
                    or record.original == "검<<COMMA>> 대형"
                )
                for record in output_report.records
            )
            if comma_expected
            else None
        )
        choice_selected = [
            entry for entry in selected_entries if entry.type == "choice"
        ]
        choice_preserved = (
            all(
                entry.id in output_by_id
                and _semantic_record_text(output_by_id[entry.id])
                == expected_by_id[entry.id]
                for entry in choice_selected
            )
            if choice_selected
            else None
        )
        semantic = bool(comparison.get("semantic_equal"))
        byte_equal = bool(comparison.get("byte_equal"))
        if semantic and korean and control_preserved and not mojibake:
            status = "accepted" if byte_equal else "normalized"
            reason = "Korean semantic content survived Editor import/re-export"
        else:
            status = "corrupted"
            reason = "Editor re-export did not preserve the translated semantic records"
        return (
            WolfEncodingTrial(
                trial_name,
                encoding,
                True,
                status,
                True,
                True,
                semantic,
                byte_equal,
                korean,
                replacement,
                comma_preserved,
                reason,
                len(selected_entries),
                tuple(entry.type for entry in selected_entries),
                control_preserved,
                mojibake,
                choice_preserved,
            ),
            tuple(roundtrip["invocations"]),
        )
    except (OSError, RuntimeError, ValueError, UnicodeError) as exc:
        return (
            WolfEncodingTrial(trial_name, encoding, False, "rejected", None, None, None, None, None, None, None, f"trial preparation failed: {exc.__class__.__name__}"),
            (),
        )


def _reencode_auto_text_tree(root: Path, encoding: str) -> None:
    for path in sorted(root.rglob("*")):
        if not path.is_file() or not path.name.casefold().endswith(".auto.txt"):
            continue
        decoded = decode_auto_text(path.read_bytes())
        if decoded.text is None:
            raise UnicodeError(f"cannot strictly decode {path.name}")
        if encoding == "utf-8-sig":
            path.write_bytes(b"\xef\xbb\xbf" + decoded.text.encode("utf-8", errors="strict"))
        elif encoding == "utf-8":
            path.write_bytes(decoded.text.encode("utf-8", errors="strict"))
        else:
            raise ValueError(f"unsupported encoding trial: {encoding}")


def _korean_test_translation(entry: object) -> str:
    control_codes = "".join(
        token for token in entry.control_codes if token != "<<COMMA>>"
    )
    if entry.type == "database_name":
        return "검, 대형" + control_codes
    return KOREAN_ROUNDTRIP_TEXT + control_codes


def _semantic_record_text(record: object) -> str:
    return (
        record.normalized_view
        if record.normalized_view is not None
        else record.original
    )


def _select_korean_trial_entries(entries: list[object]) -> list[object]:
    """Choose a few deterministic visible records instead of rewriting a fixture."""

    selected: list[object] = []
    priorities = (
        lambda entry: entry.type == "dialogue" and bool(entry.control_codes),
        lambda entry: entry.type == "choice",
        lambda entry: entry.type == "database_name",
        lambda entry: entry.type in {"dialogue", "system"},
    )
    for predicate in priorities:
        match = next(
            (
                entry
                for entry in entries
                if entry not in selected and predicate(entry)
            ),
            None,
        )
        if match is not None:
            selected.append(match)
        if len(selected) >= MAX_KOREAN_TRIAL_ENTRIES:
            break
    return selected


def _aggregate_korean_status(
    trials: list[WolfEncodingTrial], fixture_kind: str
) -> str:
    if fixture_kind != WOLF_EDITOR_FIXTURE_KIND:
        return "NOT VERIFIED"
    return (
        "VERIFIED"
        if any(item.status in {"accepted", "normalized"} and item.korean_preserved for item in trials)
        else "NOT VERIFIED"
    )


def _aggregate_comma_status(
    trials: list[WolfEncodingTrial],
    baseline: dict[str, object],
    fixture_kind: str,
) -> str:
    if not baseline.get("database_name_count"):
        return "NOT AVAILABLE"
    if fixture_kind != WOLF_EDITOR_FIXTURE_KIND:
        return "NOT VERIFIED"
    return (
        "VERIFIED"
        if any(item.comma_preserved is True for item in trials)
        else "NOT VERIFIED"
    )


def _choice_validation(
    baseline: Path,
    fixture_kind: str,
    editor_noop: dict[str, object],
    trials: list[WolfEncodingTrial],
) -> dict[str, object]:
    report = WolfTextInspector().inspect(baseline, fixture_kind=fixture_kind)
    count = report.count_type("choice")
    comparison = editor_noop.get("comparison", {})
    official_roundtrip = (
        fixture_kind == WOLF_EDITOR_FIXTURE_KIND
        and bool(comparison.get("semantic_equal"))
        and any(item.choice_preserved is True for item in trials)
    )
    nested = any(
        record.type == "choice"
        and isinstance(record.metadata.get("command_indent"), int)
        and record.metadata.get("command_indent", 0) > 0
        for record in report.records
    )
    return {
        "status": "PARTIALLY VERIFIED" if official_roundtrip else "NOT VERIFIED",
        "record_count": count,
        "fixture_kind": fixture_kind,
        "noop_semantic_equal": comparison.get("semantic_equal"),
        "command_code": "102" if count else None,
        "displayed_option_location_verified": official_roundtrip,
        "option_order_verified": official_roundtrip,
        "branch_control_separation_verified": official_roundtrip,
        "nested_structure_observed": nested,
        "nested_cancel_default_verified": False,
        "classification_changed": bool(count),
        "reason": (
            "command 102 option literals survived verified-only GLT write and official Editor re-export; cancel/default semantics remain unverified"
            if official_roundtrip
            else "choice translation/write round-trip is not proven by the current official fixture"
        ),
    }


def _database_validation(
    baseline: Path,
    fixture_kind: str,
    comma_status: str,
) -> dict[str, object]:
    report = WolfTextInspector().inspect(baseline, fixture_kind=fixture_kind)
    return {
        "status": (
            "PARTIAL"
            if fixture_kind == WOLF_EDITOR_FIXTURE_KIND
            and report.count_type("database_name")
            else "NOT VERIFIED"
            if report.count_type("database_name")
            else "NOT AVAILABLE"
        ),
        "fixture_kind": fixture_kind,
        "dataname_records": report.count_type("database_name"),
        "experimental_database_text_records": report.count_type("database_text"),
        "comma_roundtrip": comma_status,
        "verified_database_text_fields": ["dataname"],
        "description_help_verified": False,
        "stable_identifier_experiment": "NOT RUN",
        "allowlist_changed": False,
    }


def _official_verification_status(
    fixture_kind: str,
    allow_editor_import: bool,
    editor_noop: dict[str, object],
    glt_noop: dict[str, object],
    korean_status: str,
    comma_status: str,
) -> str:
    if fixture_kind != WOLF_EDITOR_FIXTURE_KIND or not allow_editor_import:
        return "NOT VERIFIED"
    editor_semantic = bool(editor_noop.get("comparison", {}).get("semantic_equal"))
    glt_semantic = bool(glt_noop.get("comparison", {}).get("semantic_equal"))
    return (
        "VERIFIED"
        if editor_semantic
        and glt_semantic
        and korean_status == "VERIFIED"
        and comma_status in {"VERIFIED", "NOT AVAILABLE"}
        else "NOT VERIFIED"
    )


def _empty_integration_report(
    detection: WolfEditorDetection,
    target: str,
    allow_editor_import: bool,
    source_fingerprint: str,
    issues: tuple[WolfEditorIssue, ...],
) -> WolfEditorIntegrationReport:
    return _integration_report(
        detection=detection,
        target=target,
        allow_editor_import=allow_editor_import,
        source_fingerprint=source_fingerprint,
        fixture_kind="not_available",
        invocations=(),
        issues=list(issues),
    )


def _integration_report(
    *,
    detection: WolfEditorDetection,
    target: str,
    allow_editor_import: bool,
    source_fingerprint: str,
    fixture_kind: str,
    invocations: list[WolfEditorInvocation] | tuple[WolfEditorInvocation, ...],
    issues: list[WolfEditorIssue],
    official_verification: str = "NOT VERIFIED",
    txtoutput_success: bool = False,
    txtinput_success: bool | None = None,
    reexport_success: bool | None = None,
    imported_fingerprint: str = "",
    reexport_fingerprint: str = "",
    baseline_export: dict[str, object] | None = None,
    editor_noop_roundtrip: dict[str, object] | None = None,
    glt_noop_roundtrip: dict[str, object] | None = None,
    encoding_trials: tuple[WolfEncodingTrial, ...] = (),
    korean_roundtrip: str = "NOT VERIFIED",
    comma_roundtrip: str = "NOT VERIFIED",
    choice_validation: dict[str, object] | None = None,
    database_validation: dict[str, object] | None = None,
) -> WolfEditorIntegrationReport:
    return WolfEditorIntegrationReport(
        TOOL_VERSION,
        fixture_kind,
        official_verification,
        detection.detected,
        detection.editor_file,
        detection.editor_version,
        detection.version_source,
        detection.editor_sha256,
        target,
        allow_editor_import,
        txtoutput_success,
        txtinput_success,
        reexport_success,
        source_fingerprint,
        imported_fingerprint,
        reexport_fingerprint,
        baseline_export or {},
        editor_noop_roundtrip or {},
        glt_noop_roundtrip or {},
        encoding_trials,
        korean_roundtrip,
        comma_roundtrip,
        choice_validation or {"status": "NOT VERIFIED"},
        database_validation or {"status": "NOT VERIFIED"},
        {
            "schema_version": 1,
            "schema_status": "provisional",
            "decision": "keep v1",
            "v2_proposal_required": False,
            "reason": "native .dat/.mps cross-route identity is not verified",
        },
        tuple(invocations),
        sum(item.severity == "warning" for item in issues),
        sum(item.severity == "error" for item in issues),
        sum(item.severity == "blocker" for item in issues),
        tuple(issues),
    )


def write_wolf_editor_report(path: Path, report: WolfEditorIntegrationReport | WolfEditorDetection) -> Path:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"WOLF Editor report already exists: {path}")
    payload = report.to_json_dict()
    data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", delete=False, dir=path.parent, prefix=f".{path.name}.", suffix=".tmp") as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        temporary.unlink()
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


def _wolf_project_evidence(project: Path) -> list[str]:
    evidence: list[str] = []
    if (project / "Data").is_dir():
        evidence.append("adjacent Data directory")
    if (project / "BasicData").is_dir():
        evidence.append("adjacent BasicData directory")
    if (project / "Game.exe").is_file() or (project / "GamePro.exe").is_file():
        evidence.append("adjacent WOLF game executable candidate")
    return evidence


def _safe_relative_name(value: str) -> bool:
    return bool(value) and Path(value).name == value and value not in {".", ".."}


def _read_file_version(path: Path) -> str | None:
    if os.name != "nt":
        return None
    try:
        size = ctypes.windll.version.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return None
        buffer = ctypes.create_string_buffer(size)
        if not ctypes.windll.version.GetFileVersionInfoW(str(path), 0, size, buffer):
            return None
        pointer = ctypes.c_void_p()
        length = ctypes.c_uint()
        if not ctypes.windll.version.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)):
            return None

        class VS_FIXEDFILEINFO(ctypes.Structure):
            _fields_ = [
                ("dwSignature", ctypes.c_uint32), ("dwStrucVersion", ctypes.c_uint32),
                ("dwFileVersionMS", ctypes.c_uint32), ("dwFileVersionLS", ctypes.c_uint32),
                ("dwProductVersionMS", ctypes.c_uint32), ("dwProductVersionLS", ctypes.c_uint32),
                ("dwFileFlagsMask", ctypes.c_uint32), ("dwFileFlags", ctypes.c_uint32),
                ("dwFileOS", ctypes.c_uint32), ("dwFileType", ctypes.c_uint32),
                ("dwFileSubtype", ctypes.c_uint32), ("dwFileDateMS", ctypes.c_uint32),
                ("dwFileDateLS", ctypes.c_uint32),
            ]

        info = ctypes.cast(pointer, ctypes.POINTER(VS_FIXEDFILEINFO)).contents
        if info.dwSignature != 0xFEEF04BD:
            return None
        return ".".join(
            str(value)
            for value in (
                info.dwFileVersionMS >> 16,
                info.dwFileVersionMS & 0xFFFF,
                info.dwFileVersionLS >> 16,
                info.dwFileVersionLS & 0xFFFF,
            )
        )
    except (AttributeError, OSError, ValueError):
        return None


__all__ = [
    "DEFAULT_EDITOR_TIMEOUT_SECONDS",
    "MAX_EDITOR_TIMEOUT_SECONDS",
    "SubprocessWolfEditorInvoker",
    "WOLF_EDITOR_ENV",
    "WOLF_EDITOR_FIXTURE_KIND",
    "WOLF_EDITOR_TARGETS",
    "WolfEditorDetection",
    "WolfEditorIntegrationReport",
    "WolfEditorIntegrationResult",
    "WolfEditorIntegrationValidator",
    "WolfEditorInvocation",
    "WolfEditorInvokerProtocol",
    "WolfEditorIssue",
    "WolfEditorLocator",
    "WolfEncodingTrial",
    "write_wolf_editor_report",
]
