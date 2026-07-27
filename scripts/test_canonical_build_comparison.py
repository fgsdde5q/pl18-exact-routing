#!/usr/bin/env python3

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


class CanonicalBuildComparisonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        script = Path(__file__).resolve().parent / "compare_canonical_builds.py"
        spec = importlib.util.spec_from_file_location("compare_canonical_builds", script)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_equal_builds_remove_stale_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.json"
            second = root / "second.json"
            diagnostics = root / "diagnostics.json"
            payload = {"directed_base_edges_sha256": "same"}
            first.write_text(json.dumps(payload), encoding="utf-8")
            second.write_text(json.dumps(payload), encoding="utf-8")
            diagnostics.write_text("stale", encoding="utf-8")
            self.assertEqual(self.module.compare(first, second, diagnostics), {})
            self.assertFalse(diagnostics.exists())

    def test_mismatch_names_each_different_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.json"
            second = root / "second.json"
            diagnostics = root / "diagnostics.json"
            first.write_text(
                json.dumps(
                    {
                        "directed_base_edges_sha256": "first-edge",
                        "edge_based_turn_states_sha256": "same-turn",
                    }
                ),
                encoding="utf-8",
            )
            second.write_text(
                json.dumps(
                    {
                        "directed_base_edges_sha256": "second-edge",
                        "edge_based_turn_states_sha256": "same-turn",
                    }
                ),
                encoding="utf-8",
            )
            differences = self.module.compare(first, second, diagnostics)
            self.assertEqual(
                differences,
                {
                    "directed_base_edges_sha256": {
                        "first": "first-edge",
                        "second": "second-edge",
                    }
                },
            )
            self.assertEqual(
                json.loads(diagnostics.read_text(encoding="utf-8")),
                {"differences": differences},
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
