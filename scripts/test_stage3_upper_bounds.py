#!/usr/bin/env python3

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import stage3_upper_bounds as stage3

ROOT = Path(__file__).resolve().parent.parent


class Stage3UpperBoundTests(unittest.TestCase):
    def setUp(self):
        self.city_to_bit = {"Warszawa": 0, "Łódź": 1, "Kielce": 2}
        self.bit_to_city = {value: key for key, value in self.city_to_bit.items()}
        self.edges = {
            "w": stage3.Edge("w", 1.0, 10.0, 1 << 0, "0", "0", "1", "0"),
            "l": stage3.Edge("l", 2.0, 20.0, 1 << 1, "1", "0", "2", "0"),
            "k": stage3.Edge("k", 3.0, 30.0, 1 << 2, "2", "0", "3", "0"),
            "lk": stage3.Edge("lk", 4.0, 40.0, (1 << 1) | (1 << 2), "1", "1", "2", "1"),
        }

    def test_fixed_order_uses_joint_state_sequence(self):
        adjacency = {
            "w": [stage3.Turn("l", 0.5, "allowed")],
            "l": [stage3.Turn("k", 0.5, "allowed")],
        }
        result = stage3.evaluate_fixed_order(
            edges=self.edges,
            adjacency=adjacency,
            model_id="M-boundary",
            route_id="fixture",
            nominal_order=["Warszawa", "Łódź", "Kielce"],
            city_to_bit=self.city_to_bit,
            bit_to_city=self.bit_to_city,
            start_edge_id="w",
            source_heuristic="fixture",
            seed=stage3.SEED,
        )
        self.assertEqual(result.feasibility, "FEASIBLE_FIXED_ORDER")
        self.assertEqual(result.total_duration_s, 7.0)
        self.assertEqual(result.total_distance_m, 60.0)
        self.assertEqual(result.actual_first_hit_order, ["Warszawa", "Łódź", "Kielce"])

    def test_first_hit_invalid_is_not_masked(self):
        adjacency = {"w": [stage3.Turn("lk", 0.5, "allowed")]}
        result = stage3.evaluate_fixed_order(
            edges=self.edges,
            adjacency=adjacency,
            model_id="M-boundary",
            route_id="fixture",
            nominal_order=["Warszawa", "Kielce", "Łódź"],
            city_to_bit=self.city_to_bit,
            bit_to_city=self.bit_to_city,
            start_edge_id="w",
            source_heuristic="fixture",
            seed=stage3.SEED,
        )
        self.assertEqual(result.feasibility, "ORDER_INVALID_BY_FIRST_HIT")
        self.assertEqual(result.actual_first_hit_order, ["Warszawa", "Łódź", "Kielce"])

    def test_historical_typo_is_normalised(self):
        self.assertEqual(stage3.normalise_city("Rzeszzów"), "Rzeszów")

    def test_sqlite_graph_loader_supports_fixed_order(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "split-edges.tsv").write_text(
                "\t".join([
                    "split_edge_id", "duration_s", "length_m", "M_boundary_mask",
                    "M_300_mask", "M_500_mask", "from_lon", "from_lat", "to_lon", "to_lat",
                ])
                + "\n"
                + "w\t1\t10\t1\t1\t1\t0\t0\t1\t0\n"
                + "l\t2\t20\t2\t2\t2\t1\t0\t2\t0\n"
                + "k\t3\t30\t4\t4\t4\t2\t0\t3\t0\n",
                encoding="utf-8",
            )
            (root / "internal-turn-states.tsv").write_text(
                "incoming_split_edge_id\toutgoing_split_edge_id\tturn_duration_s\ttransition_category\n"
                "w\tl\t0.5\tallowed\n",
                encoding="utf-8",
            )
            (root / "inherited-turn-states.tsv").write_text(
                "incoming_split_edge_id\toutgoing_split_edge_id\tturn_duration_s\ttransition_category\n"
                "l\tk\t0.5\tallowed\n",
                encoding="utf-8",
            )
            graph = stage3.build_sqlite_graph(root, "M-boundary", root / "graph.sqlite")
            try:
                result = stage3.evaluate_fixed_order(
                    edges=graph,
                    adjacency=graph,
                    model_id="M-boundary",
                    route_id="sqlite-fixture",
                    nominal_order=["Warszawa", "Łódź", "Kielce"],
                    city_to_bit=self.city_to_bit,
                    bit_to_city=self.bit_to_city,
                    start_edge_id="w",
                    source_heuristic="fixture",
                    seed=stage3.SEED,
                )
                self.assertEqual(result.feasibility, "FEASIBLE_FIXED_ORDER")
                self.assertEqual(stage3.find_city_start_edge(graph, "Łódź", self.city_to_bit), "l")
            finally:
                graph.close()

    def test_stage3_statuses_and_workflow_contract(self):
        workflow = (ROOT / ".github/workflows/stage3-upper-bounds.yml").read_text(encoding="utf-8")
        source = (ROOT / "scripts/stage3_upper_bounds.py").read_text(encoding="utf-8")
        for status in (
            "UPPER_BOUND_STAGE_OK",
            "FIXED_ORDER_SOLVER_CERTIFIED",
            "FIRST_HIT_ORDER_CERTIFIED",
            "MANDATORY_ROUTES_EVALUATED",
            "ALL_FINISHES_EVALUATED",
            "LOCAL_CHECKS_COMPLETE",
            "TOP_K_FEASIBLE_CANDIDATES_READY",
            "EXACT_GLOBAL_SOLVER_NOT_STARTED",
        ):
            self.assertIn(status, source)
        self.assertIn("hashFiles('results/graph-prg/stage2b-manifest.json'", workflow)
        self.assertIn("${{ hashFiles('scripts/stage3_upper_bounds.py') }}-${{ github.sha }}", workflow)
        for forbidden in ("OPTIMAL", "globally optimal", "GLOBAL_OPTIMUM", "GLOBAL_LOWER_BOUND_CERTIFIED"):
            self.assertNotIn(forbidden, source.replace("EXACT_GLOBAL_SOLVER_NOT_STARTED", ""))


if __name__ == "__main__":
    unittest.main()
