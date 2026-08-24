"""Read-only font diagnostics and safe font patching for RPG Maker MV/MZ."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import tempfile
import uuid
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit

from core.models import ENGINE_DISPLAY_NAMES, EngineId
from core.version import TOOL_VERSION
from engines.rpgmaker.detector import RpgMakerEngine
from engines.rpgmaker.inserter import ApplySafetyError
from engines.rpgmaker.validator import find_unexpected_changes


_FONT_SUFFIXES = {".ttf", ".otf", ".woff", ".woff2"}
_PATCH_FONT_SUFFIXES = {".ttf", ".otf"}
_HANGUL_SYLLABLES_TOTAL = 0xD7A3 - 0xAC00 + 1
_MIN_PATCH_HANGUL_COVERAGE_PERCENT = 95.0
_KOREAN_RANGES = (
    ("hangul_syllables", 0xAC00, 0xD7A3),
    ("hangul_jamo", 0x1100, 0x11FF),
    ("hangul_compatibility_jamo", 0x3130, 0x318F),
)
_GENERIC_FONT_NAMES = {
    "serif",
    "sans-serif",
    "monospace",
    "cursive",
    "fantasy",
    "system-ui",
    "inherit",
    "initial",
    "unset",
}
_FONT_FACE_RE = re.compile(r"@font-face\s*\{(?P<body>.*?)\}", re.IGNORECASE | re.DOTALL)
_DECLARATION_RE = re.compile(
    r"(?P<name>font-family|src)\s*:\s*(?P<value>[^;}]*)",
    re.IGNORECASE,
)
_URL_RE = re.compile(
    r"url\(\s*(?P<quote>['\"]?)(?P<url>.*?)(?P=quote)\s*\)",
    re.IGNORECASE,
)
_JS_FONT_RE = re.compile(
    r"\b(?P<kind>fontFace|fontFamily)\s*(?:=|:)\s*"
    r"(?P<quote>['\"`])(?P<name>.*?)(?P=quote)",
)
_HTML_LINK_RE = re.compile(r"<link\b(?P<attributes>[^>]*)>", re.IGNORECASE)
_HTML_ATTRIBUTE_RE = re.compile(
    r"(?P<name>[\w:-]+)\s*=\s*(?P<quote>['\"])(?P<value>.*?)(?P=quote)",
    re.IGNORECASE,
)


class FontOperationError(RuntimeError):
    """A fatal path, engine, or font operation problem."""


@dataclass(frozen=True, slots=True)
class GlyphSupport:
    hangul_syllables: bool
    hangul_jamo: bool
    hangul_compatibility_jamo: bool
    hangul_syllables_count: int
    hangul_jamo_count: int
    hangul_compatibility_jamo_count: int

    @property
    def hangul_coverage_percent(self) -> float:
        return round(
            self.hangul_syllables_count / _HANGUL_SYLLABLES_TOTAL * 100,
            4,
        )

    @property
    def hangul_coverage_status(self) -> str:
        if self.hangul_syllables_count == 0:
            return "NONE"
        if self.hangul_syllables_count >= _HANGUL_SYLLABLES_TOTAL:
            return "FULL"
        return "PARTIAL"

    @property
    def patch_coverage_acceptable(self) -> bool:
        return (
            self.hangul_coverage_percent
            >= _MIN_PATCH_HANGUL_COVERAGE_PERCENT
        )

    def to_json_dict(self) -> dict[str, object]:
        return {
            "hangul_coverage_status": self.hangul_coverage_status,
            "hangul_coverage_percent": self.hangul_coverage_percent,
            "hangul_syllables": self.hangul_syllables,
            "hangul_jamo": self.hangul_jamo,
            "hangul_compatibility_jamo": self.hangul_compatibility_jamo,
            "hangul_syllables_count": self.hangul_syllables_count,
            "hangul_syllables_total": _HANGUL_SYLLABLES_TOTAL,
            "hangul_jamo_count": self.hangul_jamo_count,
            "hangul_compatibility_jamo_count": (
                self.hangul_compatibility_jamo_count
            ),
        }


@dataclass(frozen=True, slots=True)
class FontFileInfo:
    file: str
    format: str
    glyph_support: GlyphSupport | None = None
    parse_error: str | None = None

    def to_json_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "file": self.file,
            "format": self.format,
            "hangul_coverage_status": (
                "UNKNOWN"
                if self.glyph_support is None
                else self.glyph_support.hangul_coverage_status
            ),
            "hangul_coverage_percent": (
                None
                if self.glyph_support is None
                else self.glyph_support.hangul_coverage_percent
            ),
        }
        if self.glyph_support is not None:
            payload["glyph_ranges"] = self.glyph_support.to_json_dict()
        if self.parse_error is not None:
            payload["parse_error"] = self.parse_error
        return payload


@dataclass(frozen=True, slots=True)
class FontReference:
    file: str
    line: int | None
    kind: str
    font_name: str | None = None
    target: str | None = None
    external: bool = False
    exists: bool | None = None
    plugin: bool = False

    def to_json_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "file": self.file,
            "kind": self.kind,
            "external": self.external,
            "plugin": self.plugin,
        }
        for name in ("line", "font_name", "target", "exists"):
            value = getattr(self, name)
            if value is not None:
                payload[name] = value
        return payload


@dataclass(frozen=True, slots=True)
class FontIssue:
    severity: str
    code: str
    reason: str
    file: str | None = None
    line: int | None = None

    def to_json_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "severity": self.severity,
            "issue_code": self.code,
            "reason": self.reason,
        }
        if self.file is not None:
            payload["file"] = self.file
        if self.line is not None:
            payload["line"] = self.line
        return payload


@dataclass(slots=True)
class FontReport:
    engine: EngineId
    action: str
    default_font: str | None = None
    font_files: list[FontFileInfo] = field(default_factory=list)
    references: list[FontReference] = field(default_factory=list)
    issues: list[FontIssue] = field(default_factory=list)
    provided_font: FontFileInfo | None = None
    dry_run: bool = False
    planned_files: list[str] = field(default_factory=list)
    planned_reference_changes: list[str] = field(default_factory=list)
    patched_files: list[str] = field(default_factory=list)
    copied_font: str | None = None

    @property
    def warnings(self) -> int:
        return sum(issue.severity == "warning" for issue in self.issues)

    @property
    def errors(self) -> int:
        return sum(issue.severity == "error" for issue in self.issues)

    @property
    def default_font_info(self) -> FontFileInfo | None:
        return next(
            (item for item in self.font_files if item.file == self.default_font),
            None,
        )

    def to_json_dict(self) -> dict[str, object]:
        default_info = self.default_font_info
        payload: dict[str, object] = {
            "tool_version": TOOL_VERSION,
            "engine": self.engine.value,
            "action": self.action,
            "dry_run": self.dry_run,
            "default_font": self.default_font,
            "default_font_hangul_coverage_status": (
                "UNKNOWN"
                if default_info is None or default_info.glyph_support is None
                else default_info.glyph_support.hangul_coverage_status
            ),
            "default_font_hangul_coverage_percent": (
                None
                if default_info is None or default_info.glyph_support is None
                else default_info.glyph_support.hangul_coverage_percent
            ),
            "minimum_patch_hangul_coverage_percent": (
                _MIN_PATCH_HANGUL_COVERAGE_PERCENT
            ),
            "font_files": [item.to_json_dict() for item in self.font_files],
            "font_references": [item.to_json_dict() for item in self.references],
            "warnings": self.warnings,
            "errors": self.errors,
            "issues": [issue.to_json_dict() for issue in self.issues],
            "planned_files": self.planned_files,
            "planned_reference_changes": self.planned_reference_changes,
            "patched_files": self.patched_files,
            "copied_font": self.copied_font,
        }
        if self.provided_font is not None:
            payload["provided_font"] = self.provided_font.to_json_dict()
        return payload


@dataclass(frozen=True, slots=True)
class _CssUrl:
    value: str
    value_start: int
    value_end: int
    line: int


@dataclass(frozen=True, slots=True)
class _CssFace:
    family: str
    urls: tuple[_CssUrl, ...]


@dataclass(frozen=True, slots=True)
class _PatchPlan:
    reference_kind: str
    reference_file: str
    destination_font: str
    old_reference: str
    new_reference: str


class RpgMakerFontService:
    """Analyze and patch font references without touching the source game."""

    def check(self, game_directory: Path, reports_directory: Path) -> FontReport:
        game_directory = game_directory.resolve()
        reports_directory = reports_directory.resolve()
        engine = _detect_engine(game_directory)
        _validate_reports_path(game_directory, reports_directory)
        report = self._analyze(game_directory, engine, action="check")
        _write_report(reports_directory / "font_report.json", report)
        return report

    def patch(
        self,
        game_directory: Path,
        font_file: Path,
        output_directory: Path,
        *,
        dry_run: bool = False,
        reports_directory: Path | None = None,
    ) -> FontReport:
        game_directory = game_directory.resolve()
        font_file = font_file.resolve()
        output_directory = output_directory.resolve()
        engine = _detect_engine(game_directory)
        _validate_output_path(game_directory, output_directory)
        if not font_file.is_file():
            raise FileNotFoundError(f"font file does not exist: {font_file}")
        if font_file.suffix.casefold() not in _PATCH_FONT_SUFFIXES:
            raise FontOperationError("font patch accepts only .ttf or .otf files")
        if font_file.is_relative_to(game_directory):
            raise FontOperationError("patch font must be provided outside the game directory")
        if reports_directory is not None:
            reports_directory = reports_directory.resolve()
            _validate_reports_path(game_directory, reports_directory)
            if reports_directory == output_directory or reports_directory.is_relative_to(
                output_directory
            ):
                raise ApplySafetyError(
                    "external font reports directory cannot be inside output directory"
                )

        report = self._analyze(game_directory, engine, action="patch")
        report.dry_run = dry_run
        report.provided_font = _inspect_font(font_file, font_file.name)
        if report.provided_font.parse_error is not None:
            report.issues.append(
                FontIssue(
                    "error",
                    "PATCH_FONT_PARSE_FAILED",
                    report.provided_font.parse_error,
                    font_file.name,
                )
            )
        elif report.provided_font.glyph_support is None:
            report.issues.append(
                FontIssue(
                    "error",
                    "PATCH_FONT_COVERAGE_UNKNOWN",
                    "provided font Hangul coverage could not be calculated",
                    font_file.name,
                )
            )
        else:
            provided_support = report.provided_font.glyph_support
            if provided_support.hangul_coverage_status == "NONE":
                report.issues.append(
                    FontIssue(
                        "error",
                        "PATCH_FONT_NO_HANGUL_COVERAGE",
                        "provided font covers 0 of 11,172 Hangul Syllables",
                        font_file.name,
                    )
                )
            elif not provided_support.patch_coverage_acceptable:
                report.issues.append(
                    FontIssue(
                        "error",
                        "PATCH_FONT_INSUFFICIENT_HANGUL_COVERAGE",
                        (
                            "provided font Hangul Syllables coverage is "
                            f"{provided_support.hangul_coverage_percent:.4f}% "
                            f"({provided_support.hangul_syllables_count}/"
                            f"{_HANGUL_SYLLABLES_TOTAL}); minimum patch coverage is "
                            f"{_MIN_PATCH_HANGUL_COVERAGE_PERCENT:.2f}%"
                        ),
                        font_file.name,
                    )
                )
            elif provided_support.hangul_coverage_status == "PARTIAL":
                report.issues.append(
                    FontIssue(
                        "warning",
                        "PATCH_FONT_PARTIAL_HANGUL_COVERAGE",
                        (
                            "provided font is usable but not FULL: "
                            f"{provided_support.hangul_coverage_percent:.4f}% "
                            f"({provided_support.hangul_syllables_count}/"
                            f"{_HANGUL_SYLLABLES_TOTAL})"
                        ),
                        font_file.name,
                    )
                )

        plan = self._build_patch_plan(game_directory, engine, font_file, report)
        if plan is not None:
            report.planned_files = [plan.destination_font, plan.reference_file]
            report.planned_reference_changes = [
                f"{plan.reference_file}: {plan.old_reference} -> {plan.new_reference}"
            ]

        if report.errors or plan is None or dry_run:
            if reports_directory is not None:
                _write_report(reports_directory / "font_patch_report.json", report)
            return report

        source_hashes = _tree_hashes(game_directory)
        output_directory.parent.mkdir(parents=True, exist_ok=True)
        staging_directory = output_directory.parent / (
            f".{output_directory.name}.glt-font-{uuid.uuid4().hex}.tmp"
        )
        try:
            shutil.copytree(
                game_directory,
                staging_directory,
                symlinks=True,
                copy_function=shutil.copy2,
            )
            if _tree_hashes(staging_directory) != source_hashes:
                raise FontOperationError(
                    "COPY_VERIFICATION_FAILED: copied output differs from source"
                )

            destination = _from_portable(staging_directory, plan.destination_font)
            destination.parent.mkdir(parents=True, exist_ok=True)
            _write_bytes_atomic(destination, font_file.read_bytes())
            reference = _from_portable(staging_directory, plan.reference_file)
            if plan.reference_kind == "css_gamefont":
                _patch_gamefont_css(reference, plan.new_reference)
            elif plan.reference_kind == "mz_system":
                _patch_mz_system_json(reference, plan.new_reference)
            else:  # pragma: no cover - protected by plan construction
                raise FontOperationError(f"unsupported patch plan: {plan.reference_kind}")

            _verify_expected_tree_changes(
                source_hashes,
                staging_directory,
                changed={Path(*Path(plan.reference_file).parts)},
                added={Path(*Path(plan.destination_font).parts)},
            )
            post = self._analyze(staging_directory, engine, action="post_patch_check")
            if post.default_font != plan.destination_font:
                raise FontOperationError(
                    "PATCH_REFERENCE_VERIFICATION_FAILED: default font did not change"
                )
            post_default = post.default_font_info
            if (
                post_default is None
                or post_default.glyph_support is None
                or not post_default.glyph_support.patch_coverage_acceptable
            ):
                raise FontOperationError(
                    "PATCH_GLYPH_VERIFICATION_FAILED: patched font coverage is below threshold"
                )
            if any(
                issue.code == "MISSING_FONT_REFERENCE" and issue.severity == "error"
                for issue in post.issues
            ):
                raise FontOperationError(
                    "PATCH_REFERENCE_VERIFICATION_FAILED: a font reference is missing"
                )

            report.patched_files = [plan.reference_file]
            report.copied_font = plan.destination_font
            _write_report(staging_directory / "reports/font_patch_report.json", report)
            if _tree_hashes(game_directory) != source_hashes:
                raise FontOperationError("SOURCE_GAME_CHANGED_DURING_FONT_PATCH")
            if output_directory.exists():
                raise FileExistsError(
                    f"output directory appeared during patch: {output_directory}"
                )
            staging_directory.rename(output_directory)
        except BaseException:
            if staging_directory.exists():
                shutil.rmtree(staging_directory)
            raise

        if reports_directory is not None:
            _write_report(reports_directory / "font_patch_report.json", report)
        return report

    def _analyze(
        self,
        game_directory: Path,
        engine: EngineId,
        *,
        action: str,
    ) -> FontReport:
        report = FontReport(engine=engine, action=action)
        font_paths = sorted(
            (
                path
                for path in game_directory.rglob("*")
                if path.is_file() and path.suffix.casefold() in _FONT_SUFFIXES
            ),
            key=lambda path: _portable(path, game_directory).casefold(),
        )
        report.font_files = [
            _inspect_font(path, _portable(path, game_directory)) for path in font_paths
        ]
        for info in report.font_files:
            if info.parse_error is not None:
                report.issues.append(
                    FontIssue(
                        "error",
                        "FONT_PARSE_FAILED",
                        info.parse_error,
                        info.file,
                    )
                )

        css_files = _css_files(game_directory)
        css_faces: list[tuple[Path, _CssFace]] = []
        declared_names: set[str] = set()
        for css_file in css_files:
            relative = _portable(css_file, game_directory)
            try:
                text = css_file.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeError) as exc:
                report.issues.append(
                    FontIssue(
                        "warning",
                        "FONT_TEXT_FILE_READ_FAILED",
                        str(exc),
                        relative,
                    )
                )
                continue
            faces = _parse_css_faces(text)
            css_faces.extend((css_file, face) for face in faces)
            for name, line in _css_family_declarations(text):
                declared_names.add(name)
                report.references.append(
                    FontReference(
                        file=relative,
                        line=line,
                        kind="css_font_family",
                        font_name=name,
                    )
                )
            for face in faces:
                declared_names.add(face.family)
                for url in face.urls:
                    reference = _font_url_reference(
                        game_directory,
                        css_file,
                        url,
                        face.family,
                    )
                    report.references.append(reference)
                    if reference.external:
                        report.issues.append(
                            FontIssue(
                                "warning",
                                "EXTERNAL_FONT_REFERENCE",
                                "font is loaded from an external URL",
                                relative,
                                url.line,
                            )
                        )
                    elif reference.exists is False:
                        report.issues.append(
                            FontIssue(
                                "error",
                                "MISSING_FONT_REFERENCE",
                                f"referenced font file does not exist: {reference.target}",
                                relative,
                                url.line,
                            )
                        )

        index_file = game_directory / "index.html"
        if index_file.is_file():
            try:
                index_text = index_file.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeError) as exc:
                report.issues.append(
                    FontIssue(
                        "warning",
                        "FONT_TEXT_FILE_READ_FAILED",
                        str(exc),
                        "index.html",
                    )
                )
            else:
                embedded_faces = _parse_css_faces(index_text)
                css_faces.extend((index_file, face) for face in embedded_faces)
                for name, line in _css_family_declarations(index_text):
                    declared_names.add(name)
                    report.references.append(
                        FontReference(
                            file="index.html",
                            line=line,
                            kind="html_font_family",
                            font_name=name,
                        )
                    )
                for face in embedded_faces:
                    declared_names.add(face.family)
                    for url in face.urls:
                        reference = _font_url_reference(
                            game_directory,
                            index_file,
                            url,
                            face.family,
                        )
                        report.references.append(reference)
                        if reference.external:
                            report.issues.append(
                                FontIssue(
                                    "warning",
                                    "EXTERNAL_FONT_REFERENCE",
                                    "font is loaded from an external URL",
                                    "index.html",
                                    url.line,
                                )
                            )
                        elif reference.exists is False:
                            report.issues.append(
                                FontIssue(
                                    "error",
                                    "MISSING_FONT_REFERENCE",
                                    (
                                        "referenced font file does not exist: "
                                        f"{reference.target}"
                                    ),
                                    "index.html",
                                    url.line,
                                )
                            )
                for reference in _html_stylesheet_references(
                    game_directory,
                    index_text,
                ):
                    report.references.append(reference)
                    if reference.external:
                        report.issues.append(
                            FontIssue(
                                "warning",
                                "EXTERNAL_STYLESHEET_REFERENCE",
                                "index.html loads an external stylesheet",
                                "index.html",
                                reference.line,
                            )
                        )
                    elif reference.exists is False:
                        report.issues.append(
                            FontIssue(
                                "warning",
                                "MISSING_STYLESHEET_REFERENCE",
                                f"stylesheet does not exist: {reference.target}",
                                "index.html",
                                reference.line,
                            )
                        )

        system_default = _mz_system_font_reference(game_directory, engine)
        if system_default is not None:
            report.references.append(system_default)
            report.default_font = system_default.target
            if system_default.exists is False:
                report.issues.append(
                    FontIssue(
                        "error",
                        "MISSING_FONT_REFERENCE",
                        f"System.json font file does not exist: {system_default.target}",
                        system_default.file,
                    )
                )
        if report.default_font is None:
            css_default = _css_default_font(game_directory, engine, css_faces)
            if css_default is not None:
                report.default_font = css_default.target

        for js_file in _javascript_files(game_directory):
            relative = _portable(js_file, game_directory)
            plugin = relative == "js/plugins.js" or relative.startswith("js/plugins/")
            try:
                text = js_file.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeError) as exc:
                report.issues.append(
                    FontIssue(
                        "warning",
                        "FONT_TEXT_FILE_READ_FAILED",
                        str(exc),
                        relative,
                    )
                )
                continue
            for match in _JS_FONT_RE.finditer(text):
                name = match.group("name").strip()
                if name:
                    declared_names.add(name)
                line = _line_number(text, match.start())
                report.references.append(
                    FontReference(
                        file=relative,
                        line=line,
                        kind=f"javascript_{match.group('kind')}",
                        font_name=name,
                        plugin=plugin,
                    )
                )
                if plugin:
                    report.issues.append(
                        FontIssue(
                            "warning",
                            "PLUGIN_FONT_REFERENCE",
                            f"plugin contains {match.group('kind')} reference {name!r}",
                            relative,
                            line,
                        )
                    )

        custom_names = {
            name.casefold()
            for name in declared_names
            if name and name.casefold() not in _GENERIC_FONT_NAMES
        }
        if len(custom_names) > 1:
            report.issues.append(
                FontIssue(
                    "warning",
                    "MULTIPLE_FONTS_USED",
                    f"multiple font families are referenced: {sorted(custom_names)!r}",
                )
            )

        default_info = report.default_font_info
        if report.default_font is None:
            report.issues.append(
                FontIssue(
                    "warning",
                    "DEFAULT_FONT_NOT_IDENTIFIED",
                    "a unique default font reference could not be identified",
                )
            )
        elif default_info is not None and default_info.glyph_support is not None:
            support = default_info.glyph_support
            if support.hangul_coverage_status == "FULL":
                report.issues.append(
                    FontIssue(
                        "info",
                        "DEFAULT_FONT_FULL_HANGUL_COVERAGE",
                        "default font covers all 11,172 Hangul Syllables",
                        default_info.file,
                    )
                )
            elif support.hangul_coverage_status == "PARTIAL":
                report.issues.append(
                    FontIssue(
                        "warning",
                        "DEFAULT_FONT_PARTIAL_HANGUL_COVERAGE",
                        (
                            "default font Hangul Syllables coverage is "
                            f"{support.hangul_coverage_percent:.4f}% "
                            f"({support.hangul_syllables_count}/"
                            f"{_HANGUL_SYLLABLES_TOTAL})"
                        ),
                        default_info.file,
                    )
                )
            else:
                report.issues.append(
                    FontIssue(
                        "warning",
                        "DEFAULT_FONT_NO_HANGUL_COVERAGE",
                        "default font covers 0 of 11,172 Hangul Syllables",
                        default_info.file,
                    )
                )
        fallback = next(
            (
                info
                for info in report.font_files
                if info.file != report.default_font
                and info.glyph_support is not None
                and info.glyph_support.hangul_coverage_status != "NONE"
            ),
            None,
        )
        if fallback is not None:
            report.issues.append(
                FontIssue(
                    "info",
                    "HANGUL_COVERAGE_FALLBACK_FONT_FOUND",
                    (
                        "a non-default font has Hangul Syllables coverage: "
                        f"{fallback.glyph_support.hangul_coverage_status} "
                        f"{fallback.glyph_support.hangul_coverage_percent:.4f}%"
                    ),
                    fallback.file,
                )
            )
        return report

    @staticmethod
    def _build_patch_plan(
        game_directory: Path,
        engine: EngineId,
        font_file: Path,
        report: FontReport,
    ) -> _PatchPlan | None:
        fonts_directory = game_directory / "fonts"
        destination_name = _available_font_name(fonts_directory, font_file.name)
        destination_relative = f"fonts/{destination_name}"
        if engine == EngineId.RPGMAKER_MV:
            css_file = game_directory / "fonts/gamefont.css"
            plan = _css_patch_plan(
                game_directory,
                css_file,
                destination_relative,
                destination_name,
            )
        else:
            system_file = game_directory / "data/System.json"
            plan = _mz_system_patch_plan(
                game_directory,
                system_file,
                destination_relative,
                destination_name,
            )
            if plan is None:
                css_candidates = [
                    game_directory / "fonts/gamefont.css",
                    game_directory / "css/gamefont.css",
                ]
                plans = [
                    candidate_plan
                    for candidate in css_candidates
                    if (
                        candidate_plan := _css_patch_plan(
                            game_directory,
                            candidate,
                            destination_relative,
                            _relative_css_url(candidate.parent, fonts_directory / destination_name),
                        )
                    )
                    is not None
                ]
                plan = plans[0] if len(plans) == 1 else None
        if plan is None:
            report.issues.append(
                FontIssue(
                    "error",
                    "MANUAL_FONT_REFERENCE_REVIEW_REQUIRED",
                    "a unique safe default font reference could not be selected",
                )
            )
        return plan


def _detect_engine(game_directory: Path) -> EngineId:
    if not game_directory.is_dir():
        raise FileNotFoundError(f"game directory does not exist: {game_directory}")
    detection = RpgMakerEngine().detect(game_directory)
    if not detection.detected or detection.engine is None:
        raise FontOperationError("RPG Maker MV/MZ could not be detected")
    return detection.engine


def _validate_output_path(game_directory: Path, output_directory: Path) -> None:
    if output_directory.exists():
        raise FileExistsError(
            f"output path already exists; refusing to overwrite: {output_directory}"
        )
    if output_directory == game_directory:
        raise ApplySafetyError("output directory must differ from the game directory")
    if output_directory.is_relative_to(game_directory):
        raise ApplySafetyError("output directory cannot be inside the game directory")
    if game_directory.is_relative_to(output_directory):
        raise ApplySafetyError("output directory cannot contain the game directory")


def _validate_reports_path(game_directory: Path, reports_directory: Path) -> None:
    if reports_directory == game_directory or reports_directory.is_relative_to(
        game_directory
    ):
        raise ApplySafetyError("font reports directory cannot be inside the game directory")


def _css_files(game_directory: Path) -> list[Path]:
    candidates: set[Path] = set()
    for relative_directory in ("fonts", "css"):
        directory = game_directory / relative_directory
        if directory.is_dir():
            candidates.update(path for path in directory.rglob("*.css") if path.is_file())
    return sorted(candidates, key=lambda path: _portable(path, game_directory).casefold())


def _javascript_files(game_directory: Path) -> list[Path]:
    js_directory = game_directory / "js"
    if not js_directory.is_dir():
        return []
    files = {path for path in js_directory.glob("*.js") if path.is_file()}
    plugins_directory = js_directory / "plugins"
    if plugins_directory.is_dir():
        files.update(path for path in plugins_directory.rglob("*.js") if path.is_file())
    plugins_config = js_directory / "plugins.js"
    if plugins_config.is_file():
        files.add(plugins_config)
    return sorted(files, key=lambda path: _portable(path, game_directory).casefold())


def _html_stylesheet_references(
    game_directory: Path,
    text: str,
) -> list[FontReference]:
    references: list[FontReference] = []
    for tag in _HTML_LINK_RE.finditer(text):
        attributes = {
            match.group("name").casefold(): match.group("value")
            for match in _HTML_ATTRIBUTE_RE.finditer(tag.group("attributes"))
        }
        rel = attributes.get("rel", "").casefold().split()
        href = attributes.get("href")
        if "stylesheet" not in rel or not href:
            continue
        line = _line_number(text, tag.start())
        if _is_external_url(href):
            references.append(
                FontReference(
                    file="index.html",
                    line=line,
                    kind="html_stylesheet",
                    target=href,
                    external=True,
                )
            )
            continue
        clean = unquote(urlsplit(href).path)
        target = (game_directory / clean).resolve()
        references.append(
            FontReference(
                file="index.html",
                line=line,
                kind="html_stylesheet",
                target=(
                    _portable(target, game_directory)
                    if target.is_relative_to(game_directory)
                    else clean
                ),
                exists=(
                    target.is_file() if target.is_relative_to(game_directory) else False
                ),
            )
        )
    return references


def _parse_css_faces(text: str) -> list[_CssFace]:
    faces: list[_CssFace] = []
    for face_match in _FONT_FACE_RE.finditer(text):
        body = face_match.group("body")
        family = ""
        urls: list[_CssUrl] = []
        for declaration in _DECLARATION_RE.finditer(body):
            name = declaration.group("name").casefold()
            value = declaration.group("value")
            if name == "font-family":
                family = _primary_font_name(value)
            elif name == "src":
                for url_match in _URL_RE.finditer(value):
                    value_start = (
                        face_match.start("body")
                        + declaration.start("value")
                        + url_match.start("url")
                    )
                    value_end = (
                        face_match.start("body")
                        + declaration.start("value")
                        + url_match.end("url")
                    )
                    urls.append(
                        _CssUrl(
                            value=url_match.group("url").strip(),
                            value_start=value_start,
                            value_end=value_end,
                            line=_line_number(text, value_start),
                        )
                    )
        if family:
            faces.append(_CssFace(family=family, urls=tuple(urls)))
    return faces


def _css_family_declarations(text: str) -> list[tuple[str, int]]:
    declarations: list[tuple[str, int]] = []
    for match in _DECLARATION_RE.finditer(text):
        if match.group("name").casefold() != "font-family":
            continue
        name = _primary_font_name(match.group("value"))
        if name:
            declarations.append((name, _line_number(text, match.start())))
    return declarations


def _primary_font_name(value: str) -> str:
    return value.split(",", 1)[0].strip().strip("'\"")


def _font_url_reference(
    game_directory: Path,
    css_file: Path,
    url: _CssUrl,
    family: str,
) -> FontReference:
    if _is_external_url(url.value):
        return FontReference(
            file=_portable(css_file, game_directory),
            line=url.line,
            kind="css_font_source",
            font_name=family,
            target=url.value,
            external=True,
        )
    clean = unquote(urlsplit(url.value).path)
    target_path = (css_file.parent / clean).resolve()
    if not target_path.is_relative_to(game_directory):
        return FontReference(
            file=_portable(css_file, game_directory),
            line=url.line,
            kind="css_font_source",
            font_name=family,
            target=clean,
            exists=False,
        )
    return FontReference(
        file=_portable(css_file, game_directory),
        line=url.line,
        kind="css_font_source",
        font_name=family,
        target=_portable(target_path, game_directory),
        exists=target_path.is_file(),
    )


def _css_default_font(
    game_directory: Path,
    engine: EngineId,
    css_faces: list[tuple[Path, _CssFace]],
) -> FontReference | None:
    candidates: list[FontReference] = []
    for css_file, face in css_faces:
        if face.family.casefold() != "gamefont":
            continue
        for url in face.urls:
            reference = _font_url_reference(game_directory, css_file, url, face.family)
            if not reference.external and reference.target is not None:
                candidates.append(reference)
    if engine == EngineId.RPGMAKER_MV:
        candidates = [
            reference
            for reference in candidates
            if reference.file.casefold() == "fonts/gamefont.css"
        ]
    unique = {reference.target: reference for reference in candidates}
    return next(iter(unique.values())) if len(unique) == 1 else None


def _mz_system_font_reference(
    game_directory: Path,
    engine: EngineId,
) -> FontReference | None:
    if engine != EngineId.RPGMAKER_MZ:
        return None
    system_file = game_directory / "data/System.json"
    if not system_file.is_file():
        return None
    try:
        payload = json.loads(system_file.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    advanced = payload.get("advanced") if isinstance(payload, dict) else None
    filename = advanced.get("mainFontFilename") if isinstance(advanced, dict) else None
    if not isinstance(filename, str) or not filename.strip():
        return None
    if Path(filename).name != filename or Path(filename).suffix.casefold() not in _FONT_SUFFIXES:
        return FontReference(
            file="data/System.json",
            line=None,
            kind="rpgmaker_mz_main_font",
            font_name="rmmz-mainfont",
            target=filename,
            exists=False,
        )
    target = game_directory / "fonts" / filename
    return FontReference(
        file="data/System.json",
        line=None,
        kind="rpgmaker_mz_main_font",
        font_name="rmmz-mainfont",
        target=_portable(target, game_directory),
        exists=target.is_file(),
    )


def _css_patch_plan(
    game_directory: Path,
    css_file: Path,
    destination_relative: str,
    new_url: str,
) -> _PatchPlan | None:
    if not css_file.is_file():
        return None
    try:
        text = css_file.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return None
    faces = [
        face for face in _parse_css_faces(text) if face.family.casefold() == "gamefont"
    ]
    if len(faces) != 1 or len(faces[0].urls) != 1:
        return None
    old_url = faces[0].urls[0].value
    old_path = unquote(urlsplit(old_url).path)
    if _is_external_url(old_url) or Path(old_path).suffix.casefold() not in _FONT_SUFFIXES:
        return None
    return _PatchPlan(
        reference_kind="css_gamefont",
        reference_file=_portable(css_file, game_directory),
        destination_font=destination_relative,
        old_reference=old_url,
        new_reference=new_url,
    )


def _mz_system_patch_plan(
    game_directory: Path,
    system_file: Path,
    destination_relative: str,
    destination_name: str,
) -> _PatchPlan | None:
    if not system_file.is_file():
        return None
    try:
        payload = json.loads(system_file.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    advanced = payload.get("advanced") if isinstance(payload, dict) else None
    current = advanced.get("mainFontFilename") if isinstance(advanced, dict) else None
    if not isinstance(current, str) or not current.strip():
        return None
    if Path(current).name != current or Path(current).suffix.casefold() not in _FONT_SUFFIXES:
        return None
    return _PatchPlan(
        reference_kind="mz_system",
        reference_file="data/System.json",
        destination_font=destination_relative,
        old_reference=current,
        new_reference=destination_name,
    )


def _patch_gamefont_css(path: Path, new_url: str) -> None:
    original = path.read_bytes()
    had_bom = original.startswith(b"\xef\xbb\xbf")
    text = original.decode("utf-8-sig")
    faces = [
        face for face in _parse_css_faces(text) if face.family.casefold() == "gamefont"
    ]
    if len(faces) != 1 or len(faces[0].urls) != 1:
        raise FontOperationError("MANUAL_FONT_REFERENCE_REVIEW_REQUIRED")
    url = faces[0].urls[0]
    patched = text[: url.value_start] + new_url + text[url.value_end :]
    prefix = b"\xef\xbb\xbf" if had_bom else b""
    _write_bytes_atomic(path, prefix + patched.encode("utf-8"))
    reloaded = path.read_text(encoding="utf-8-sig")
    if new_url not in [
        item.value
        for face in _parse_css_faces(reloaded)
        if face.family.casefold() == "gamefont"
        for item in face.urls
    ]:
        raise FontOperationError("patched CSS reference could not be re-parsed")


def _patch_mz_system_json(path: Path, destination_name: str) -> None:
    original_bytes = path.read_bytes()
    before = json.loads(original_bytes.decode("utf-8-sig"))
    after = json.loads(original_bytes.decode("utf-8-sig"))
    advanced = after.get("advanced") if isinstance(after, dict) else None
    if not isinstance(advanced, dict) or not isinstance(
        advanced.get("mainFontFilename"), str
    ):
        raise FontOperationError("MANUAL_FONT_REFERENCE_REVIEW_REQUIRED")
    advanced["mainFontFilename"] = destination_name
    unexpected = find_unexpected_changes(
        before,
        after,
        {"$.advanced.mainFontFilename"},
    )
    if unexpected:
        raise FontOperationError(f"UNEXPECTED_DATA_CHANGE: {unexpected!r}")
    pretty = b"\n" in original_bytes or b"\r" in original_bytes
    trailing_newline = original_bytes.endswith((b"\n", b"\r"))
    serialized = (
        json.dumps(after, ensure_ascii=False, indent=2)
        if pretty
        else json.dumps(after, ensure_ascii=False, separators=(",", ":"))
    )
    if trailing_newline:
        serialized += "\n"
    encoding = "utf-8-sig" if original_bytes.startswith(b"\xef\xbb\xbf") else "utf-8"
    _write_bytes_atomic(path, serialized.encode(encoding))
    reloaded = json.loads(path.read_text(encoding="utf-8-sig"))
    if find_unexpected_changes(before, reloaded, {"$.advanced.mainFontFilename"}):
        raise FontOperationError("UNEXPECTED_DATA_CHANGE after System.json write")


def _available_font_name(font_directory: Path, requested: str) -> str:
    candidate = font_directory / requested
    if not candidate.exists():
        return requested
    stem = Path(requested).stem
    suffix = Path(requested).suffix
    index = 1
    while True:
        marker = "-glt" if index == 1 else f"-glt-{index}"
        name = f"{stem}{marker}{suffix}"
        if not (font_directory / name).exists():
            return name
        index += 1


def _relative_css_url(css_directory: Path, font_path: Path) -> str:
    return os.path.relpath(font_path, css_directory).replace("\\", "/")


def _verify_expected_tree_changes(
    source_hashes: dict[Path, str],
    staging_directory: Path,
    *,
    changed: set[Path],
    added: set[Path],
) -> None:
    staged_hashes = _tree_hashes(staging_directory)
    removed = set(source_hashes) - set(staged_hashes)
    actual_added = set(staged_hashes) - set(source_hashes)
    actual_changed = {
        path
        for path in set(source_hashes) & set(staged_hashes)
        if source_hashes[path] != staged_hashes[path]
    }
    if removed or actual_added != added or actual_changed != changed:
        raise FontOperationError(
            "UNEXPECTED_DATA_CHANGE: "
            f"removed={sorted(map(str, removed))!r}, "
            f"added={sorted(map(str, actual_added))!r}, "
            f"changed={sorted(map(str, actual_changed))!r}"
        )


def _inspect_font(path: Path, display_name: str) -> FontFileInfo:
    try:
        codepoints = _font_codepoints(path)
        counts = {
            name: sum(start <= codepoint <= end for codepoint in codepoints)
            for name, start, end in _KOREAN_RANGES
        }
        support = GlyphSupport(
            hangul_syllables=counts["hangul_syllables"] > 0,
            hangul_jamo=counts["hangul_jamo"] > 0,
            hangul_compatibility_jamo=(
                counts["hangul_compatibility_jamo"] > 0
            ),
            hangul_syllables_count=counts["hangul_syllables"],
            hangul_jamo_count=counts["hangul_jamo"],
            hangul_compatibility_jamo_count=(
                counts["hangul_compatibility_jamo"]
            ),
        )
        return FontFileInfo(
            file=display_name,
            format=path.suffix.casefold().lstrip("."),
            glyph_support=support,
        )
    except (OSError, ValueError, struct.error, zlib.error) as exc:
        return FontFileInfo(
            file=display_name,
            format=path.suffix.casefold().lstrip("."),
            parse_error=str(exc),
        )


def _font_codepoints(path: Path) -> set[int]:
    data = path.read_bytes()
    if len(data) < 4:
        raise ValueError("font file is too short")
    signature = data[:4]
    if signature == b"wOFF":
        cmap = _woff_table(data, b"cmap")
    elif signature == b"wOF2":
        return _fonttools_codepoints(path)
    elif signature in {b"\x00\x01\x00\x00", b"OTTO", b"true", b"typ1"}:
        cmap = _sfnt_table(data, b"cmap")
    else:
        raise ValueError("unsupported or invalid font signature")
    return _parse_cmap(cmap)


def _fonttools_codepoints(path: Path) -> set[int]:
    try:
        from fontTools.ttLib import TTFont  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ValueError(
            "WOFF2 cmap inspection requires optional fontTools dependency"
        ) from exc
    try:
        with TTFont(path, lazy=True) as font:
            best = font.getBestCmap()
            if not best:
                raise ValueError("font contains no usable Unicode cmap")
            return set(best)
    except Exception as exc:  # fontTools exposes several parser exception types
        raise ValueError(f"fontTools could not parse font: {exc}") from exc


def _sfnt_table(data: bytes, tag: bytes) -> bytes:
    if len(data) < 12:
        raise ValueError("invalid SFNT header")
    table_count = struct.unpack_from(">H", data, 4)[0]
    directory_end = 12 + table_count * 16
    if directory_end > len(data):
        raise ValueError("truncated SFNT table directory")
    for index in range(table_count):
        offset = 12 + index * 16
        table_tag, _, table_offset, length = struct.unpack_from(">4sIII", data, offset)
        if table_tag != tag:
            continue
        if table_offset + length > len(data):
            raise ValueError(f"truncated {tag.decode('ascii')} table")
        return data[table_offset : table_offset + length]
    raise ValueError(f"font has no {tag.decode('ascii')} table")


def _woff_table(data: bytes, tag: bytes) -> bytes:
    if len(data) < 44:
        raise ValueError("invalid WOFF header")
    table_count = struct.unpack_from(">H", data, 12)[0]
    directory_end = 44 + table_count * 20
    if directory_end > len(data):
        raise ValueError("truncated WOFF table directory")
    for index in range(table_count):
        offset = 44 + index * 20
        table_tag, table_offset, compressed_length, original_length, _ = struct.unpack_from(
            ">4sIIII", data, offset
        )
        if table_tag != tag:
            continue
        if table_offset + compressed_length > len(data):
            raise ValueError(f"truncated WOFF {tag.decode('ascii')} table")
        payload = data[table_offset : table_offset + compressed_length]
        if compressed_length < original_length:
            payload = zlib.decompress(payload)
        if len(payload) != original_length:
            raise ValueError(f"invalid WOFF {tag.decode('ascii')} table length")
        return payload
    raise ValueError(f"font has no {tag.decode('ascii')} table")


def _parse_cmap(cmap: bytes) -> set[int]:
    if len(cmap) < 4:
        raise ValueError("truncated cmap header")
    _, table_count = struct.unpack_from(">HH", cmap, 0)
    if 4 + table_count * 8 > len(cmap):
        raise ValueError("truncated cmap encoding records")
    offsets: set[int] = set()
    for index in range(table_count):
        _, _, offset = struct.unpack_from(">HHI", cmap, 4 + index * 8)
        offsets.add(offset)
    codepoints: set[int] = set()
    supported = False
    for offset in sorted(offsets):
        if offset + 2 > len(cmap):
            raise ValueError("invalid cmap subtable offset")
        fmt = struct.unpack_from(">H", cmap, offset)[0]
        if fmt == 4:
            codepoints.update(_cmap_format_4(cmap, offset))
            supported = True
        elif fmt == 6:
            codepoints.update(_cmap_format_6(cmap, offset))
            supported = True
        elif fmt in {12, 13}:
            codepoints.update(_cmap_format_12_or_13(cmap, offset, fmt))
            supported = True
    if not supported:
        raise ValueError("font contains no supported Unicode cmap format")
    return codepoints


def _cmap_format_4(data: bytes, offset: int) -> set[int]:
    if offset + 16 > len(data):
        raise ValueError("truncated cmap format 4")
    length, seg_count_x2 = struct.unpack_from(">HH", data, offset + 2)[0], struct.unpack_from(">H", data, offset + 6)[0]
    end = offset + length
    seg_count = seg_count_x2 // 2
    if not seg_count or end > len(data):
        raise ValueError("invalid cmap format 4 length")
    end_codes = offset + 14
    start_codes = end_codes + seg_count * 2 + 2
    deltas = start_codes + seg_count * 2
    range_offsets = deltas + seg_count * 2
    if range_offsets + seg_count * 2 > end:
        raise ValueError("truncated cmap format 4 arrays")
    found: set[int] = set()
    for index in range(seg_count):
        segment_end = struct.unpack_from(">H", data, end_codes + index * 2)[0]
        segment_start = struct.unpack_from(">H", data, start_codes + index * 2)[0]
        delta = struct.unpack_from(">h", data, deltas + index * 2)[0]
        range_offset_position = range_offsets + index * 2
        range_offset = struct.unpack_from(">H", data, range_offset_position)[0]
        for range_start, range_end in _relevant_intersections(segment_start, segment_end):
            for codepoint in range(range_start, range_end + 1):
                if range_offset == 0:
                    glyph = (codepoint + delta) & 0xFFFF
                else:
                    glyph_position = (
                        range_offset_position + range_offset + 2 * (codepoint - segment_start)
                    )
                    if glyph_position + 2 > end:
                        raise ValueError("invalid cmap format 4 glyph offset")
                    glyph = struct.unpack_from(">H", data, glyph_position)[0]
                    if glyph:
                        glyph = (glyph + delta) & 0xFFFF
                if glyph:
                    found.add(codepoint)
    return found


def _cmap_format_6(data: bytes, offset: int) -> set[int]:
    if offset + 10 > len(data):
        raise ValueError("truncated cmap format 6")
    length, first, count = struct.unpack_from(">HHH", data, offset + 2)
    end = offset + length
    if offset + 10 + count * 2 > end or end > len(data):
        raise ValueError("invalid cmap format 6 length")
    found: set[int] = set()
    for index in range(count):
        codepoint = first + index
        glyph = struct.unpack_from(">H", data, offset + 10 + index * 2)[0]
        if glyph and _is_korean_codepoint(codepoint):
            found.add(codepoint)
    return found


def _cmap_format_12_or_13(data: bytes, offset: int, fmt: int) -> set[int]:
    if offset + 16 > len(data):
        raise ValueError(f"truncated cmap format {fmt}")
    length, group_count = struct.unpack_from(">II", data, offset + 4)[0], struct.unpack_from(">I", data, offset + 12)[0]
    end = offset + length
    if offset + 16 + group_count * 12 > end or end > len(data):
        raise ValueError(f"invalid cmap format {fmt} length")
    found: set[int] = set()
    for index in range(group_count):
        start, finish, glyph_start = struct.unpack_from(
            ">III", data, offset + 16 + index * 12
        )
        for range_start, range_end in _relevant_intersections(start, finish):
            for codepoint in range(range_start, range_end + 1):
                glyph = glyph_start if fmt == 13 else glyph_start + codepoint - start
                if glyph:
                    found.add(codepoint)
    return found


def _relevant_intersections(start: int, end: int) -> Iterable[tuple[int, int]]:
    if start > end:
        return ()
    return tuple(
        (max(start, range_start), min(end, range_end))
        for _, range_start, range_end in _KOREAN_RANGES
        if max(start, range_start) <= min(end, range_end)
    )


def _is_korean_codepoint(codepoint: int) -> bool:
    return any(start <= codepoint <= end for _, start, end in _KOREAN_RANGES)


def _is_external_url(value: str) -> bool:
    lowered = value.strip().casefold()
    return lowered.startswith(("http://", "https://", "//", "data:"))


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _portable(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _from_portable(root: Path, value: str) -> Path:
    return root.joinpath(*value.split("/"))


def _tree_hashes(root: Path) -> dict[Path, str]:
    hashes: dict[Path, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        hashes[path.relative_to(root)] = digest.hexdigest()
    return hashes


def _write_report(path: Path, report: FontReport) -> None:
    _write_bytes_atomic(
        path,
        (json.dumps(report.to_json_dict(), ensure_ascii=False, indent=2) + "\n").encode(
            "utf-8"
        ),
    )


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            delete=False,
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(payload)
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
