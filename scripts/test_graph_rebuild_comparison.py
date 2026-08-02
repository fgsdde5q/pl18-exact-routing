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
        (metadata / "non-enforced-restriction-candidates.json").write_text(
            '{"record_count": 0}\n', encoding="utf-8"
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
            comparisons = {
                "directed_base_edges_sha256": {"first": "edge-hash"},
                "edge_based_turn_states_sha256": {"first": "turn-hash"},
                "counts": {"first": {"count": 1}},
                "start_snap": {"first": {"distance_m": 1}},
                "turn_restriction_certificate": {
                    "first": {"status": "TURN_RESTRICTIONS_CERTIFIED"}
                },
                "non_enforced_restriction_candidates": {
                    "first": {"record_count": 0}
                },
            }
            certificate.write_text(
                json.dumps(
                    {
                        "status": "GRAPH_REBUILD_REPRODUCIBLE",
                        "full_graph_build_count": 2,
                        "ready_graph_cache_restored": False,
                        "comparisons": comparisons,
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
                        "canonical_exports": {
                            "directed_base_edges_sha256": "edge-hash",
                            "edge_based_turn_states_sha256": "turn-hash",
                        },
                        "counts": {"count": 1},
                        "start_snap": {"distance_m": 1},
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
            (results / "turn-restriction-certificate.json").write_text(
                '{"status": "TURN_RESTRICTIONS_CERTIFIED"}\n',
                encoding="utf-8",
            )
            (results / "non-enforced-restriction-candidates.json").write_text(
                '{"record_count": 0}\n', encoding="utf-8"
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

    def test_certificate_must_match_canonical_stage_products(self) -> None:
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
                        "comparisons": {
                            "directed_base_edges_sha256": {"first": "rebuilt"},
                            "edge_based_turn_states_sha256": {"first": "turn"},
                            "counts": {"first": {}},
                            "start_snap": {"first": {}},
                            "turn_restriction_certificate": {
                                "first": {"status": "TURN_RESTRICTIONS_CERTIFIED"}
                            },
                            "non_enforced_restriction_candidates": {
                                "first": {"record_count": 0}
                            },
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (results / "graph-manifest.json").write_text(
                json.dumps(
                    {
                        "canonical_exports": {
                            "directed_base_edges_sha256": "canonical",
                            "edge_based_turn_states_sha256": "turn",
                        },
                        "counts": {},
                        "start_snap": {},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (results / "turn-restriction-certificate.json").write_text(
                '{"status": "TURN_RESTRICTIONS_CERTIFIED"}\n',
                encoding="utf-8",
            )
            (results / "non-enforced-restriction-candidates.json").write_text(
                '{"record_count": 0}\n', encoding="utf-8"
            )
            (results / "validation-report.md").write_text("", encoding="utf-8")
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
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                "disagrees with canonical Stage 2A products", result.stderr
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
