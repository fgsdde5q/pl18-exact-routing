#!/usr/bin/env python3

import json
import random
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import stage4_exact as stage4
import stage4_disk_solver as disk


class Stage4ExactTests(unittest.TestCase):
    def test_exact_fixed_point_rejects_loss(self):
        self.assertEqual(stage4.exact_units("1.234567"), 1_234_567)
        with self.assertRaises(ValueError):
            stage4.exact_units("0.0000001")
        with self.assertRaises(ValueError):
            stage4.exact_units("-1")

    def test_free_finish_and_first_hit_semantics(self):
        states = [
            stage4.RoadState("w", 1),
            stage4.RoadState("transit", 4),
            stage4.RoadState("last", 2),
            stage4.RoadState("expensive-exit", 0),
        ]
        graph = stage4.MemoryGraph(states, {
            "w": [stage4.Arc("transit", 2)],
            "transit": [stage4.Arc("last", 3)],
            "last": [stage4.Arc("expensive-exit", 999)],
        })
        result = stage4.exhaustive_dijkstra(graph, "w", 1, 7)
        self.assertEqual(result.status, "OPTIMAL")
        self.assertEqual(result.ub_units, 5)
        self.assertEqual(result.path, ["w", "transit", "last"])
        self.assertEqual(result.actual_first_hit_bits, [0, 2, 1])

    def test_superset_dominance_is_safe_and_reverse_is_not_used(self):
        # At q, {0,1} at cost 2 dominates {0} at cost 3.  The cheaper subset
        # cannot dominate the superset because acquiring city 1 later costs 50.
        states = [stage4.RoadState("s", 1), stage4.RoadState("a", 2),
                  stage4.RoadState("q", 0), stage4.RoadState("b", 4)]
        graph = stage4.MemoryGraph(states, {
            "s": [stage4.Arc("a", 1), stage4.Arc("q", 3)],
            "a": [stage4.Arc("q", 1)],
            "q": [stage4.Arc("a", 50), stage4.Arc("b", 1)],
        })
        result = stage4.exhaustive_dijkstra(graph, "s", 1, 7)
        self.assertEqual(result.ub_units, 3)
        self.assertGreater(result.counters.dominated, 0)

    def test_two_bounds_are_admissible_on_random_instances(self):
        for city_count in (4, 6, 8):
            graph, start, initial, target = stage4.synthetic_instance(city_count, seed=918)
            exact = stage4.exhaustive_dijkstra(graph, start, initial, target)
            components = [stage4.EntryCostBound(graph, target),
                          stage4.TransitionCountBound(graph, target)]
            astar = stage4.solve(graph, start, initial, target, stage4.CompositeBound(components))
            independent = stage4.independent_subset_dp(graph, start, initial, target)
            self.assertEqual(exact.ub_units, astar.ub_units)
            self.assertEqual(exact.ub_units, independent)

    def test_interrupt_resume_is_identical(self):
        graph, start, initial, target = stage4.synthetic_instance(8)
        bound = stage4.CompositeBound([
            stage4.EntryCostBound(graph, target), stage4.TransitionCountBound(graph, target)
        ])
        uninterrupted = stage4.solve(graph, start, initial, target, bound)
        with TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "checkpoint.json"
            partial = stage4.solve(graph, start, initial, target, bound, max_expansions=2,
                                   checkpoint=checkpoint, checkpoint_identity={"hash": "fixture"})
            self.assertEqual(partial.status, "INCOMPLETE")
            resumed = stage4.solve(graph, start, initial, target, bound, checkpoint=checkpoint,
                                   resume=True, checkpoint_identity={"hash": "fixture"})
            self.assertEqual(resumed.ub_units, uninterrupted.ub_units)
            self.assertEqual(resumed.path, uninterrupted.path)
            with self.assertRaises(ValueError):
                stage4.solve(graph, start, initial, target, bound, checkpoint=checkpoint,
                             resume=True, checkpoint_identity={"hash": "other"})

    def test_unverified_stage3_scalar_is_never_an_incumbent(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            # This test checks the emitted contract without needing large graph files.
            source = (Path(__file__).parent / "stage4_exact.py").read_text(encoding="utf-8")
            self.assertIn('"initial_UB": "INFINITY"', source)
            self.assertNotIn("230495.6", source)
            self.assertNotIn("229843.4", source)
            self.assertNotIn("239029.1", source)

    def test_replay_rejects_teleport_and_incomplete_path(self):
        graph = stage4.MemoryGraph(
            [stage4.RoadState("s", 1), stage4.RoadState("a", 2), stage4.RoadState("b", 4)],
            {"s": [stage4.Arc("a", 1)], "a": [stage4.Arc("b", 2)]},
        )
        with self.assertRaises(ValueError):
            stage4.replay_path(graph, ["s", "b"], "s", 1, 7)
        with self.assertRaises(ValueError):
            stage4.replay_path(graph, ["s", "a"], "s", 1, 7)
        replay = stage4.replay_path(graph, ["s", "a", "b"], "s", 1, 7)
        self.assertEqual(replay["cost_units"], 3)

    def test_external_memory_chunk_proves_fixture(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            graph_path = root / "graph.sqlite"
            connection = sqlite3.connect(graph_path)
            connection.executescript("""
              CREATE TABLE edges(edge_id TEXT PRIMARY KEY,duration_units INTEGER,distance_m TEXT,
                city_mask INTEGER,from_lon TEXT,from_lat TEXT,to_lon TEXT,to_lat TEXT,
                parent_edge_id TEXT,ordinal INTEGER,from_node TEXT,to_node TEXT) WITHOUT ROWID;
              CREATE TABLE turns(incoming TEXT,outgoing TEXT,turn_units INTEGER,category TEXT);
              CREATE TABLE geometry_turns(incoming TEXT PRIMARY KEY,outgoing TEXT) WITHOUT ROWID;
              CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT) WITHOUT ROWID;
            """)
            connection.executemany("INSERT INTO edges VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", [
                (stage4.START_EDGE, 0, "0", 1, "0", "0", "1", "0", "p", 0, "0", "1"),
                ("goal", 7, "1", stage4.ALL_18 ^ 1, "1", "0", "2", "0", "g", 0, "1", "2"),
            ])
            connection.execute("INSERT INTO turns VALUES(?,?,?,?)",
                               (stage4.START_EDGE, "goal", 3, "explicit"))
            metadata = {
                "identity": {"fixture": True}, "minimum_edge_units": 0,
                "maximum_bits_per_edge": 17,
                "minimum_city_edge_units": {str(bit): 7 for bit in range(1, 18)},
            }
            connection.executemany("INSERT INTO metadata VALUES(?,?)",
                                   [(key, json.dumps(value)) for key, value in metadata.items()])
            connection.commit(); connection.close()
            progress = disk.run_chunk(graph_path, root / "checkpoint.sqlite", 10, None, None)
            self.assertEqual(progress["solver_status"], "OPTIMAL")
            self.assertEqual(progress["UB_units"], 10)
            self.assertEqual(progress["LB_units"], 10)


if __name__ == "__main__":
    unittest.main()
