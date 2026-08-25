from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.models import EngineId
from engines.rpgmaker.detector import RpgMakerEngine
from engines.rpgmaker.paths import resolve_rpgmaker_content_root


def _touch(root: Path, relative_path: str) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")


class RpgMakerDetectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.detector = RpgMakerEngine()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_detects_mz_from_multiple_artifacts(self) -> None:
        for relative_path in (
            "js/rmmz_core.js",
            "data/System.json",
            "data/Map001.json",
            "data/Actors.json",
            "js/plugins.js",
            "index.html",
        ):
            _touch(self.root, relative_path)

        result = self.detector.detect(self.root)

        self.assertTrue(result.detected)
        self.assertEqual(EngineId.RPGMAKER_MZ, result.engine)
        self.assertEqual(99, result.confidence)
        self.assertEqual("js/rmmz_core.js", result.evidence[0])
        self.assertIn("data/Map001.json", result.evidence)

    def test_detects_mv_from_required_artifacts(self) -> None:
        _touch(self.root, "js/rpg_core.js")
        _touch(self.root, "data/System.json")

        result = self.detector.detect(self.root)

        self.assertTrue(result.detected)
        self.assertEqual(EngineId.RPGMAKER_MV, result.engine)
        self.assertEqual(90, result.confidence)

    def test_packaged_root_and_www_detect_the_same_mv_content(self) -> None:
        packaged_root = self.root / "packaged"
        www = packaged_root / "www"
        _touch(www, "js/rpg_core.js")
        _touch(www, "data/System.json")
        _touch(www, "data/Map001.json")
        _touch(www, "index.html")
        _touch(packaged_root, "Game.exe")

        from_root = self.detector.detect(packaged_root)
        from_www = self.detector.detect(www)

        self.assertEqual(www, resolve_rpgmaker_content_root(packaged_root))
        self.assertEqual(www, resolve_rpgmaker_content_root(www))
        self.assertEqual(from_www, from_root)
        self.assertTrue(from_root.detected)
        self.assertEqual(EngineId.RPGMAKER_MV, from_root.engine)
        self.assertGreaterEqual(from_root.confidence, 95)

        (packaged_root / "data").mkdir()
        (packaged_root / "js").mkdir()
        self.assertEqual(packaged_root, resolve_rpgmaker_content_root(packaged_root))

    def test_content_root_resolver_preserves_unknown_directory(self) -> None:
        self.assertEqual(self.root, resolve_rpgmaker_content_root(self.root))

    def test_does_not_confirm_from_a_single_core_file(self) -> None:
        _touch(self.root, "js/rmmz_core.js")

        result = self.detector.detect(self.root)

        self.assertFalse(result.detected)
        self.assertIsNone(result.engine)
        self.assertLess(result.confidence, 75)

    def test_empty_directory_is_unknown(self) -> None:
        result = self.detector.detect(self.root)

        self.assertFalse(result.detected)
        self.assertEqual(0, result.confidence)
        self.assertEqual((), result.evidence)

    def test_conflicting_complete_signatures_are_unknown(self) -> None:
        _touch(self.root, "js/rmmz_core.js")
        _touch(self.root, "js/rpg_core.js")
        _touch(self.root, "data/System.json")

        result = self.detector.detect(self.root)

        self.assertFalse(result.detected)
        self.assertEqual("Unknown", result.display_name)
        self.assertIn("js/rmmz_core.js", result.evidence)
        self.assertIn("js/rpg_core.js", result.evidence)


if __name__ == "__main__":
    unittest.main()
