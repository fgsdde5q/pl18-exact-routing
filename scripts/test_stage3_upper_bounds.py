#!/usr/bin/env python3

import unittest
import csv
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
                    "split_edge_id", "parent_edge_id", "ordinal", "start_fraction", "end_fraction",
                    "duration_s", "length_m", "M_boundary_mask",
                    "M_300_mask", "M_500_mask", "from_lon", "from_lat", "to_lon", "to_lat",
                ])
                + "\n"
                + "w\t1/0/0/10/11\t0\t0\t1\t1\t10\t1\t1\t1\t0\t0\t1\t0\n"
                + "l\t2/0/0/11/12\t0\t0\t1\t2\t20\t2\t2\t2\t1\t0\t2\t0\n"
                + "k\t3/0/0/12/13\t0\t0\t1\t3\t30\t4\t4\t4\t2\t0\t3\t0\n",
                encoding="utf-8",
            )
            (root / "internal-turn-states.tsv").write_text(
                "incoming_split_edge_id\toutgoing_split_edge_id\tturn_duration_s\ttransition_category\n"
                "w\tl\t0.5\tallowed\n",
                encoding="utf-8",
            )
            (root / "gates.tsv").write_text(
                "model_id\tbit_index\tcity\tevent_category\tsplit_point_id\tparent_edge_id\t"
                "incoming_split_edge_id\toutgoing_split_edge_id\tosm_way_id\tfraction\tdirection_bit\t"
                "wgs84_x\twgs84_y\tepsg2180_x\tepsg2180_y\n"
                "M-boundary\t0\tWarszawa\texiting\tg0\tw-parent\tw\tl\t1\t1\t0\t0\t0\t0\t0\n"
                "M-boundary\t1\tŁódź\tentering\tg1\tl-parent\tw\tl\t1\t0\t0\t1\t0\t0\t0\n"
                "M-boundary\t2\tKielce\tentering\tg2\tk-parent\tl\tk\t1\t0\t0\t2\t0\t0\t0\n",
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
                self.assertEqual(graph.metadata()["split_edge_rows"], "3")
                self.assertEqual(graph.gate_edges(2), ["k", "l"])
            finally:
                graph.close()

    def test_goal_is_free_after_last_city_and_no_exit_is_required(self):
        adjacency = {"w": [stage3.Turn("l", 0.5, "allowed")]}
        result = stage3.evaluate_fixed_order(
            edges=self.edges,
            adjacency=adjacency,
            model_id="M-boundary",
            route_id="free-finish",
            nominal_order=["Warszawa", "Łódź"],
            city_to_bit=self.city_to_bit,
            bit_to_city=self.bit_to_city,
            start_edge_id="w",
            source_heuristic="fixture",
            seed=stage3.SEED,
        )
        self.assertEqual(result.result_category, "FEASIBLE")
        self.assertEqual(result.actual_first_hit_order, ["Warszawa", "Łódź"])

    def test_road_edges_outside_next_city_are_allowed(self):
        outside = stage3.Edge("outside", 1.0, 10.0, 0, "1", "0", "1.5", "0")
        edges = {**self.edges, "outside": outside}
        adjacency = {
            "w": [stage3.Turn("outside", 0.0, "allowed")],
            "outside": [stage3.Turn("l", 0.0, "allowed")],
        }
        result = stage3.evaluate_fixed_order(
            edges=edges,
            adjacency=adjacency,
            model_id="M-boundary",
            route_id="outside-next-city",
            nominal_order=["Warszawa", "Łódź"],
            city_to_bit=self.city_to_bit,
            bit_to_city=self.bit_to_city,
            start_edge_id="w",
            source_heuristic="fixture",
            seed=stage3.SEED,
        )
        self.assertEqual(result.result_category, "FEASIBLE")

    def test_unreachable_has_graph_level_reason(self):
        result = stage3.evaluate_fixed_order(
            edges=self.edges,
            adjacency={},
            model_id="M-boundary",
            route_id="unreachable",
            nominal_order=["Warszawa", "Łódź"],
            city_to_bit=self.city_to_bit,
            bit_to_city=self.bit_to_city,
            start_edge_id="w",
            source_heuristic="fixture",
            seed=stage3.SEED,
        )
        self.assertEqual(result.result_category, "UNREACHABLE")
        self.assertIn("zero outgoing", result.failure_reason)

    def test_start_snap_uses_frozen_zero_cost_initial_state(self):
        edge = stage3.Edge("w", 10.0, 100.0, 1, "0", "0", "1", "0", "p", 0, 0.0, 1.0)
        result = stage3.evaluate_fixed_order(
            edges={"w": edge}, adjacency={}, model_id="M-boundary", route_id="snap",
            nominal_order=["Warszawa"], city_to_bit=self.city_to_bit, bit_to_city=self.bit_to_city,
            start_edge_id="w", source_heuristic="fixture", seed=stage3.SEED,
            initial_visited_mask=1, start_snap_fraction=0.75,
        )
        self.assertEqual(result.total_duration_s, 0.0)
        self.assertEqual(result.total_distance_m, 0.0)

    def test_independent_checker_agrees_with_primary(self):
        adjacency = {
            "w": [stage3.Turn("l", 0.5, "allowed")],
            "l": [stage3.Turn("k", 0.5, "allowed")],
        }
        kwargs = dict(
            edges=self.edges, adjacency=adjacency, model_id="M-boundary", route_id="checked",
            nominal_order=["Warszawa", "Łódź", "Kielce"], city_to_bit=self.city_to_bit,
            bit_to_city=self.bit_to_city, start_edge_id="w", source_heuristic="fixture", seed=stage3.SEED,
        )
        result = stage3.evaluate_with_checker(**kwargs)
        self.assertEqual(result.result_category, "FEASIBLE")
        self.assertEqual(result.fixed_order_check_delta_s, 0.0)
        self.assertEqual(result.provenance["independent_checker_distance_delta_m"], 0.0)

    def test_outcome_csv_is_exhaustive(self):
        result = stage3.invalid_result(
            "M-boundary", "candidate", "fixture", stage3.SEED,
            ["Warszawa", "Łódź"], ["Warszawa"], "UNREACHABLE",
            failure_reason="exhausted", failing_transition_or_state="w",
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "outcomes.csv"
            counts = stage3.write_evaluation_outcomes(path, [result] * 197)
            with path.open(encoding="utf-8", newline="") as source:
                rows = list(csv.DictReader(source))
            self.assertEqual(len(rows), 197)
            self.assertEqual(sum(counts.values()), 197)
            self.assertEqual(counts["UNREACHABLE"], 197)

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
            "STAGE3_FAILED_NO_FEASIBLE_ROUTE",
        ):
            self.assertIn(status, source)
        self.assertIn("hashFiles('results/graph-prg/stage2b-manifest.json'", workflow)
        self.assertIn("${{ hashFiles('scripts/stage3_upper_bounds.py') }}-${{ github.sha }}", workflow)
        for forbidden in ("OPTIMAL", "globally optimal", "GLOBAL_OPTIMUM", "GLOBAL_LOWER_BOUND_CERTIFIED"):
            self.assertNotIn(forbidden, source.replace("EXACT_GLOBAL_SOLVER_NOT_STARTED", ""))


if __name__ == "__main__":
    unittest.main()
