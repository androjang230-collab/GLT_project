from __future__ import annotations

import contextlib
import io
import json
import shutil
import struct
import tempfile
import unittest
from pathlib import Path

import glt
from engines.rpgmaker.fonts import RpgMakerFontService
from projects.manager import ProjectManager


def _cmap_table_from_groups(groups_data: list[tuple[int, int, int]]) -> bytes:
    groups = b"".join(
        struct.pack(">III", start, end, glyph_start)
        for start, end, glyph_start in groups_data
    )
    subtable = struct.pack(
        ">HHIII",
        12,
        0,
        16 + len(groups),
        0,
        len(groups_data),
    ) + groups
    return struct.pack(">HHHHI", 0, 1, 3, 10, 12) + subtable


def _cmap_table(codepoints: list[int]) -> bytes:
    return _cmap_table_from_groups(
        [
            (codepoint, codepoint, index + 1)
            for index, codepoint in enumerate(sorted(codepoints))
        ]
    )


def _sfnt_font(codepoints: list[int], *, otf: bool = False) -> bytes:
    cmap = _cmap_table(codepoints)
    return _sfnt_with_cmap(cmap, otf=otf)


def _sfnt_with_cmap(cmap: bytes, *, otf: bool = False) -> bytes:
    signature = b"OTTO" if otf else b"\x00\x01\x00\x00"
    header = signature + struct.pack(">HHHH", 1, 16, 0, 0)
    directory = struct.pack(">4sIII", b"cmap", 0, 28, len(cmap))
    return header + directory + cmap


def _woff_font(codepoints: list[int]) -> bytes:
    cmap = _cmap_table(codepoints)
    offset = 64
    length = offset + len(cmap)
    header = struct.pack(
        ">4sIIHHIHHIIIII",
        b"wOFF",
        0x00010000,
        length,
        1,
        0,
        28 + len(cmap),
        1,
        0,
        0,
        0,
        0,
        0,
        0,
    )
    directory = struct.pack(">4sIIII", b"cmap", offset, len(cmap), len(cmap), 0)
    return header + directory + cmap


def _write_font(path: Path, *, korean: bool, otf: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if korean:
        cmap = _cmap_table_from_groups(
            [
                (0xAC00, 0xD7A3, 1),
                (0x1100, 0x11FF, 0x3000),
                (0x3130, 0x318F, 0x3100),
            ]
        )
        path.write_bytes(_sfnt_with_cmap(cmap, otf=otf))
    else:
        path.write_bytes(_sfnt_font([0x41], otf=otf))


def _write_partial_font(path: Path, syllable_count: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cmap = _cmap_table_from_groups(
        [(0xAC00, 0xAC00 + syllable_count - 1, 1)]
    )
    path.write_bytes(_sfnt_with_cmap(cmap))


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _tree_bytes(root: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _mv_game(root: Path) -> Path:
    (root / "js/plugins").mkdir(parents=True)
    (root / "fonts").mkdir()
    (root / "js/rpg_core.js").write_text(
        'Window_Base.prototype.fontFamily = "GameFont";',
        encoding="utf-8",
    )
    (root / "js/plugins.js").write_text("var $plugins = [];", encoding="utf-8")
    (root / "js/plugins/MessageCore.js").write_text(
        "// keep this plugin byte-identical\nthis.fontFace = \"PluginFont\";\n",
        encoding="utf-8",
    )
    _write_json(root / "data/System.json", {})
    (root / "fonts/gamefont.css").write_text(
        "/* preserve this comment */\n"
        "@font-face {\n"
        "  font-family: GameFont;\n"
        "  src: url(\"latin.ttf\");\n"
        "}\n"
        "body { color: #123456; font-family: GameFont, sans-serif; }\n",
        encoding="utf-8",
    )
    _write_font(root / "fonts/latin.ttf", korean=False)
    (root / "index.html").write_text(
        '<link rel="stylesheet" href="fonts/gamefont.css">',
        encoding="utf-8",
    )
    return root


def _mz_game(root: Path) -> Path:
    (root / "js").mkdir(parents=True)
    (root / "fonts").mkdir()
    (root / "js/rmmz_core.js").write_text("// MZ core", encoding="utf-8")
    _write_json(
        root / "data/System.json",
        {
            "advanced": {
                "mainFontFilename": "latin.ttf",
                "numberFontFilename": "number.ttf",
                "unchanged": 7,
            },
            "gameTitle": "Test",
        },
    )
    _write_font(root / "fonts/latin.ttf", korean=False)
    _write_font(root / "fonts/number.ttf", korean=False)
    return root


class FontServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary_directory.name)
        self.game = _mv_game(self.workspace / "game")
        self.reports = self.workspace / "reports"
        self.service = RpgMakerFontService()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_mv_font_check_detects_css_fonts_js_and_plugin_without_writes(self) -> None:
        before = _tree_bytes(self.game)

        report = self.service.check(self.game, self.reports)

        self.assertEqual(before, _tree_bytes(self.game))
        self.assertEqual("fonts/latin.ttf", report.default_font)
        self.assertEqual(
            "NONE",
            report.to_json_dict()["default_font_hangul_coverage_status"],
        )
        self.assertIn("fonts/latin.ttf", {item.file for item in report.font_files})
        self.assertIn(
            "DEFAULT_FONT_NO_HANGUL_COVERAGE",
            {issue.code for issue in report.issues},
        )
        plugin = next(reference for reference in report.references if reference.plugin)
        self.assertEqual("js/plugins/MessageCore.js", plugin.file)
        self.assertEqual(2, plugin.line)
        self.assertEqual("PluginFont", plugin.font_name)
        self.assertTrue((self.reports / "font_report.json").is_file())

    def test_actual_cmap_distinguishes_korean_and_non_korean_fonts(self) -> None:
        _write_font(self.game / "fonts/korean-name-does-not-matter.otf", korean=True, otf=True)

        report = self.service.check(self.game, self.reports)
        fonts = {item.file: item for item in report.font_files}

        korean = fonts["fonts/korean-name-does-not-matter.otf"].glyph_support
        latin = fonts["fonts/latin.ttf"].glyph_support
        self.assertIsNotNone(korean)
        self.assertEqual("FULL", korean.hangul_coverage_status)
        self.assertEqual(100.0, korean.hangul_coverage_percent)
        self.assertEqual(11172, korean.hangul_syllables_count)
        self.assertTrue(korean.hangul_syllables)
        self.assertTrue(korean.hangul_jamo)
        self.assertTrue(korean.hangul_compatibility_jamo)
        self.assertIsNotNone(latin)
        self.assertEqual("NONE", latin.hangul_coverage_status)
        self.assertEqual(0.0, latin.hangul_coverage_percent)

    def test_woff_cmap_is_inspected_without_filename_guessing(self) -> None:
        (self.game / "fonts/webfont.woff").write_bytes(_woff_font([0xAC00]))

        report = self.service.check(self.game, self.reports)
        webfont = next(
            item for item in report.font_files if item.file == "fonts/webfont.woff"
        )

        self.assertIsNotNone(webfont.glyph_support)
        self.assertEqual("PARTIAL", webfont.glyph_support.hangul_coverage_status)
        self.assertEqual(1, webfont.glyph_support.hangul_syllables_count)

    def test_jamo_only_font_is_not_treated_as_korean_body_font(self) -> None:
        (self.game / "fonts/jamo-only.ttf").write_bytes(_sfnt_font([0x1100, 0x3131]))

        report = self.service.check(self.game, self.reports)
        jamo = next(
            item for item in report.font_files if item.file == "fonts/jamo-only.ttf"
        ).glyph_support

        self.assertIsNotNone(jamo)
        self.assertTrue(jamo.hangul_jamo)
        self.assertTrue(jamo.hangul_compatibility_jamo)
        self.assertEqual("NONE", jamo.hangul_coverage_status)
        self.assertEqual(0.0, jamo.hangul_coverage_percent)

    def test_missing_css_font_reference_is_error(self) -> None:
        (self.game / "fonts/latin.ttf").unlink()

        report = self.service.check(self.game, self.reports)

        self.assertIn("MISSING_FONT_REFERENCE", {issue.code for issue in report.issues})
        self.assertGreater(report.errors, 0)

    def test_font_patch_copies_font_and_changes_only_mv_src(self) -> None:
        font = self.workspace / "KoreanFont.ttf"
        output = self.workspace / "patched"
        _write_font(font, korean=True)
        before = _tree_bytes(self.game)
        original_css = (self.game / "fonts/gamefont.css").read_text(encoding="utf-8")
        original_plugin = (self.game / "js/plugins/MessageCore.js").read_bytes()

        report = self.service.patch(self.game, font, output)

        self.assertEqual(before, _tree_bytes(self.game))
        self.assertTrue((output / "fonts/KoreanFont.ttf").is_file())
        patched_css = (output / "fonts/gamefont.css").read_text(encoding="utf-8")
        self.assertEqual(
            original_css.replace("latin.ttf", "KoreanFont.ttf"),
            patched_css,
        )
        self.assertIn("color: #123456", patched_css)
        self.assertEqual(
            original_plugin,
            (output / "js/plugins/MessageCore.js").read_bytes(),
        )
        self.assertEqual(["fonts/gamefont.css"], report.patched_files)
        self.assertTrue((output / "reports/font_patch_report.json").is_file())

    def test_plugin_font_reference_is_never_automatically_modified(self) -> None:
        font = self.workspace / "KoreanFont.ttf"
        output = self.workspace / "patched"
        _write_font(font, korean=True)
        before = (self.game / "js/plugins/MessageCore.js").read_bytes()

        self.service.patch(self.game, font, output)

        self.assertEqual(before, (output / "js/plugins/MessageCore.js").read_bytes())

    def test_font_patch_dry_run_does_not_create_output(self) -> None:
        font = self.workspace / "KoreanFont.ttf"
        output = self.workspace / "dry-output"
        _write_font(font, korean=True)

        report = self.service.patch(
            self.game,
            font,
            output,
            dry_run=True,
            reports_directory=self.reports,
        )

        self.assertFalse(output.exists())
        self.assertTrue(report.dry_run)
        self.assertEqual(
            ["fonts/KoreanFont.ttf", "fonts/gamefont.css"],
            report.planned_files,
        )
        self.assertTrue((self.reports / "font_patch_report.json").is_file())

    def test_ambiguous_gamefont_src_requires_manual_review(self) -> None:
        css = self.game / "fonts/gamefont.css"
        css.write_text(
            "@font-face { font-family: GameFont; "
            'src: url("latin.ttf"), url("fallback.otf"); }',
            encoding="utf-8",
        )
        _write_font(self.game / "fonts/fallback.otf", korean=False, otf=True)
        font = self.workspace / "KoreanFont.ttf"
        output = self.workspace / "ambiguous-output"
        _write_font(font, korean=True)

        report = self.service.patch(
            self.game,
            font,
            output,
            reports_directory=self.reports,
        )

        self.assertFalse(output.exists())
        self.assertIn(
            "MANUAL_FONT_REFERENCE_REVIEW_REQUIRED",
            {issue.code for issue in report.issues},
        )

    def test_invalid_or_non_korean_patch_font_is_blocked(self) -> None:
        invalid = self.workspace / "invalid.ttf"
        invalid.write_bytes(b"not-a-font")
        invalid_output = self.workspace / "invalid-output"
        invalid_report = self.service.patch(self.game, invalid, invalid_output)

        non_korean = self.workspace / "latin.otf"
        _write_font(non_korean, korean=False, otf=True)
        non_korean_output = self.workspace / "non-korean-output"
        non_korean_report = self.service.patch(
            self.game,
            non_korean,
            non_korean_output,
        )

        self.assertFalse(invalid_output.exists())
        self.assertIn(
            "PATCH_FONT_PARSE_FAILED",
            {issue.code for issue in invalid_report.issues},
        )
        self.assertFalse(non_korean_output.exists())
        self.assertIn(
            "PATCH_FONT_NO_HANGUL_COVERAGE",
            {issue.code for issue in non_korean_report.issues},
        )

    def test_low_partial_coverage_is_blocked(self) -> None:
        font = self.workspace / "low-partial.ttf"
        output = self.workspace / "low-partial-output"
        _write_partial_font(font, 10613)

        report = self.service.patch(self.game, font, output)

        support = report.provided_font.glyph_support
        self.assertEqual("PARTIAL", support.hangul_coverage_status)
        self.assertLess(support.hangul_coverage_percent, 95.0)
        self.assertIn(
            "PATCH_FONT_INSUFFICIENT_HANGUL_COVERAGE",
            {issue.code for issue in report.issues},
        )
        self.assertFalse(output.exists())

    def test_high_partial_coverage_warns_but_can_patch(self) -> None:
        font = self.workspace / "high-partial.ttf"
        output = self.workspace / "high-partial-output"
        _write_partial_font(font, 10614)

        report = self.service.patch(self.game, font, output)

        support = report.provided_font.glyph_support
        self.assertEqual("PARTIAL", support.hangul_coverage_status)
        self.assertGreaterEqual(support.hangul_coverage_percent, 95.0)
        self.assertIn(
            "PATCH_FONT_PARTIAL_HANGUL_COVERAGE",
            {issue.code for issue in report.issues},
        )
        self.assertTrue(output.is_dir())

    def test_existing_output_is_protected(self) -> None:
        font = self.workspace / "KoreanFont.ttf"
        output = self.workspace / "existing"
        _write_font(font, korean=True)
        output.mkdir()

        with self.assertRaises(FileExistsError):
            self.service.patch(self.game, font, output)

    def test_filename_collision_uses_safe_new_name(self) -> None:
        font = self.workspace / "latin.ttf"
        output = self.workspace / "patched"
        _write_font(font, korean=True)

        report = self.service.patch(self.game, font, output)

        self.assertEqual("fonts/latin-glt.ttf", report.copied_font)
        self.assertTrue((output / "fonts/latin-glt.ttf").is_file())
        css = (output / "fonts/gamefont.css").read_text(encoding="utf-8")
        self.assertIn('url("latin-glt.ttf")', css)

    def test_mz_system_main_font_is_detected_and_safely_patched(self) -> None:
        game = _mz_game(self.workspace / "mz-game")
        font = self.workspace / "KoreanFont.ttf"
        output = self.workspace / "mz-output"
        _write_font(font, korean=True)

        checked = self.service.check(game, self.workspace / "mz-reports")
        patched = self.service.patch(game, font, output)
        system = json.loads((output / "data/System.json").read_text(encoding="utf-8"))

        self.assertEqual("fonts/latin.ttf", checked.default_font)
        self.assertEqual("KoreanFont.ttf", system["advanced"]["mainFontFilename"])
        self.assertEqual("number.ttf", system["advanced"]["numberFontFilename"])
        self.assertEqual(7, system["advanced"]["unchanged"])
        self.assertEqual(["data/System.json"], patched.patched_files)

    def test_project_font_check_and_patch_are_portable(self) -> None:
        manager = ProjectManager()
        project = self.workspace / "project"
        moved_project = self.workspace / "other-pc/project"
        font = self.workspace / "KoreanFont.ttf"
        output = self.workspace / "project-output"
        manager.create(self.game, project)
        moved_project.parent.mkdir()
        shutil.copytree(project, moved_project)
        # Font operations remain usable on a translated/apply output whose data
        # fingerprint intentionally differs from the original Project source.
        _write_json(self.game / "data/System.json", {"gameTitle": "번역판"})
        _write_font(font, korean=True)

        checked = manager.font_check(moved_project, self.game)
        patched = manager.font_patch(
            moved_project,
            self.game,
            font,
            output,
        )
        project_text = (moved_project / "project.json").read_text(encoding="utf-8")
        report_text = (moved_project / "reports/font_patch_report.json").read_text(
            encoding="utf-8"
        )

        self.assertEqual("fonts/latin.ttf", checked.default_font)
        self.assertEqual("fonts/KoreanFont.ttf", patched.copied_font)
        self.assertNotIn(str(font), project_text)
        self.assertNotIn(str(font), report_text)

    def test_standalone_and_project_font_cli(self) -> None:
        project = self.workspace / "project"
        font = self.workspace / "KoreanFont.ttf"
        output = self.workspace / "cli-output"
        _write_font(font, korean=True)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                0,
                glt.main(
                    [
                        "project",
                        "create",
                        str(self.game),
                        "--output",
                        str(project),
                    ]
                ),
            )

        font_check_output = io.StringIO()
        with contextlib.redirect_stdout(font_check_output):
            check_code = glt.main(
                [
                    "font-check",
                    str(self.game),
                    "--reports",
                    str(self.reports),
                ]
            )
            project_check_code = glt.main(
                ["project", "font-check", str(project), str(self.game)]
            )
            patch_code = glt.main(
                [
                    "project",
                    "font-patch",
                    str(project),
                    str(self.game),
                    "--font",
                    str(font),
                    "--output",
                    str(output),
                    "--dry-run",
                ]
            )

        self.assertEqual(0, check_code)
        self.assertEqual(0, project_check_code)
        self.assertEqual(0, patch_code)
        self.assertFalse(output.exists())
        self.assertIn("Detected Engine: RPG Maker MV", font_check_output.getvalue())


if __name__ == "__main__":
    unittest.main()
