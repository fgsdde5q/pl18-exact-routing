#!/usr/bin/env python3

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


class ComparisonTests(unittest.TestCase):
    def make_build(self, root: Path, artifact_hash="a" * 64):
        root.mkdir()
        write_json(root / "stage2b-contract.json", {"schema_version": 1})
        write_json(root / "split-points-summary.json", {"count": 2})
        write_json(root / "export-reproducibility.json", {"result": "PENDING_SECOND_EXPORT"})
        write_json(root / "stage2b-manifest.json", {
            "statuses": ["ROAD_PRG_INTERSECTION_OK", "SOLVER_NOT_STARTED"],
            "artifact_hashes": {"split.tsv.zst": {"sha256": artifact_hash, "size_bytes": 10}},
        })
        (root / "stage2b-validation-report.md").write_text("pending\n", encoding="utf-8")
        (root / "SHA256SUMS").write_text("pending\n", encoding="utf-8")

    def test_matching_builds_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            first, second, output = temporary / "first", temporary / "second", temporary / "output"
            self.make_build(first)
            self.make_build(second)
            subprocess.run([
                "python3", str(ROOT / "scripts/compare_stage2b_builds.py"),
                "--first-results", str(first), "--second-results", str(second),
                "--output", str(output), "--mode", "rebuild",
            ], check=True, capture_output=True, text=True)
            certificate = json.loads((output / "rebuild-reproducibility.json").read_text())
            self.assertEqual(certificate["result"], "PASS")
            manifest = json.loads((output / "stage2b-manifest.json").read_text())
            self.assertIn("STAGE2B_REBUILD_REPRODUCIBLE", manifest["statuses"])

    def test_artifact_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            first, second, output = temporary / "first", temporary / "second", temporary / "output"
            self.make_build(first)
            self.make_build(second, "b" * 64)
            result = subprocess.run([
                "python3", str(ROOT / "scripts/compare_stage2b_builds.py"),
                "--first-results", str(first), "--second-results", str(second),
                "--output", str(output), "--mode", "rebuild",
            ], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            certificate = json.loads((output / "rebuild-reproducibility.json").read_text())
            self.assertEqual(certificate["result"], "FAILED")


if __name__ == "__main__":
    unittest.main()
