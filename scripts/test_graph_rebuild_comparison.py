#!/usr/bin/env python3

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


class GraphRebuildComparisonTests(unittest.TestCase):
    def make_build(
        self,
        root: Path,
        name: str,
        graph_root: Path,
        restored: bool = False,
    ) -> tuple[Path, Path]:
        metadata = root / name / "metadata"
        metadata.mkdir(parents=True)
        (metadata / "directed-base-edges.tsv").write_text("edge\n", encoding="utf-8")
        (metadata / "edge-based-turn-states.tsv").write_text("turn\n", encoding="utf-8")
        (metadata / "export-summary.json").write_text('{"count": 1}\n', encoding="utf-8")
        (metadata / "start-snap.json").write_text('{"distance_m": 1}\n', encoding="utf-8")
        (metadata / "turn-restriction-certificate.json").write_text(
            '{"status": "TURN_RESTRICTIONS_CERTIFIED"}\n',
            encoding="utf-8",
        )
        provenance = root / name / "provenance.json"
        provenance.write_text(
            json.dumps(
                {
                    "build_id": name,
                    "clean_graph_root": str(graph_root),
                    "ready_graph_cache_restored": restored,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return metadata, provenance

    def run_compare(
        self,
        first: tuple[Path, Path],
        second: tuple[Path, Path],
        output: Path,
    ) -> subprocess.CompletedProcess:
        script = Path(__file__).with_name("compare_graph_rebuilds.py")
        return subprocess.run(
            [
                "python3",
                str(script),
                "--first-metadata",
                str(first[0]),
                "--second-metadata",
                str(second[0]),
                "--first-provenance",
                str(first[1]),
                "--second-provenance",
                str(second[1]),
                "--output",
                str(output),
            ],
            capture_output=True,
            text=True,
        )

    def test_distinct_clean_builds_are_certified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self.make_build(root, "first", root / "graph-first")
            second = self.make_build(root, "second", root / "graph-second")
            output = root / "result.json"
            result = self.run_compare(first, second, output)
            self.assertEqual(result.returncode, 0, result.stderr)
            certificate = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(certificate["status"], "GRAPH_REBUILD_REPRODUCIBLE")

    def test_same_ready_graph_cannot_be_claimed_as_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph = root / "same-graph"
            first = self.make_build(root, "first", graph)
            second = self.make_build(root, "second", graph)
            result = self.run_compare(first, second, root / "result.json")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("same graph directory", result.stderr)

    def test_restored_ready_graph_cannot_be_claimed_as_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self.make_build(root, "first", root / "graph-first", restored=True)
            second = self.make_build(root, "second", root / "graph-second")
            result = self.run_compare(first, second, root / "result.json")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("ready OSRM graph cache", result.stderr)

    def test_successful_certificate_updates_stage_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = root / "results"
            results.mkdir()
            certificate = root / "certificate.json"
            certificate.write_text(
                json.dumps(
                    {
                        "status": "GRAPH_REBUILD_REPRODUCIBLE",
                        "full_graph_build_count": 2,
                        "ready_graph_cache_restored": False,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (results / "graph-manifest.json").write_text(
                json.dumps(
                    {
                        "statuses": ["ROAD_GRAPH_STAGE_OK"],
                        "reproducibility": {"graph_rebuild": {"result": "NOT_RUN"}},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (results / "validation-report.md").write_text(
                "- `ROAD_GRAPH_STAGE_OK`\n"
                "- `GRAPH_REBUILD_REPRODUCIBILITY`: `NOT_RUN`\n"
                "- Graph rebuild reproducibility is not asserted before the dispatch-only clean rebuild workflow succeeds.\n",
                encoding="utf-8",
            )
            script = Path(__file__).with_name(
                "apply_graph_rebuild_certificate.py"
            )
            result = subprocess.run(
                [
                    "python3",
                    str(script),
                    "--certificate",
                    str(certificate),
                    "--results",
                    str(results),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads(
                (results / "graph-manifest.json").read_text(encoding="utf-8")
            )
            self.assertIn("GRAPH_REBUILD_REPRODUCIBLE", manifest["statuses"])
            report = (results / "validation-report.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("`GRAPH_REBUILD_REPRODUCIBILITY`: `PASS`", report)
            self.assertNotIn("not asserted", report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
