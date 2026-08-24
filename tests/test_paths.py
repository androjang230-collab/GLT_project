from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.paths import portable_relative_path, resolve_input_directory


class PathTests(unittest.TestCase):
    def test_resolves_relative_input_at_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            game = root / "portable-game"
            game.mkdir()

            self.assertEqual(
                game.resolve(),
                resolve_input_directory(Path("portable-game"), base=root),
            )

    def test_report_path_uses_forward_slashes(self) -> None:
        root = Path("project")
        self.assertEqual(
            "data/System.json",
            portable_relative_path(root / "data" / "System.json", root),
        )

    def test_missing_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaises(FileNotFoundError):
                resolve_input_directory(
                    Path("missing"),
                    base=Path(temporary_directory),
                )


if __name__ == "__main__":
    unittest.main()
