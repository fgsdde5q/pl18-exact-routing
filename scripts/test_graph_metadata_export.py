#!/usr/bin/env python3

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from array import array
from pathlib import Path


OSM_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="stage2a-test">
  <node id="1" lat="52.268850" lon="20.985950"/>
  <node id="2" lat="52.268850" lon="20.986950"/>
  <node id="3" lat="52.269350" lon="20.986950"/>
  <node id="4" lat="52.269350" lon="20.985950"/>
  <way id="10">
    <nd ref="1"/><nd ref="2"/>
    <tag k="highway" v="residential"/>
    <tag k="maxspeed" v="30"/>
    <tag k="oneway" v="alternating"/>
  </way>
  <way id="11">
    <nd ref="2"/><nd ref="3"/>
    <tag k="highway" v="service"/>
    <tag k="access" v="private"/>
  </way>
  <way id="12">
    <nd ref="3"/><nd ref="4"/>
    <tag k="route" v="ferry"/>
  </way>
  <relation id="20">
    <member type="way" ref="10" role="from"/>
    <member type="node" ref="2" role="via"/>
    <member type="way" ref="10" role="to"/>
    <tag k="type" v="restriction"/>
    <tag k="restriction" v="no_u_turn"/>
  </relation>
</osm>
"""


class GraphMetadataExportTests(unittest.TestCase):
    def test_synthetic_graph_is_canonical_and_filtered(self) -> None:
        repository_root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            osm = root / "fixture.osm"
            osm.write_text(OSM_FIXTURE, encoding="utf-8")
            dump = root / "dump"
            dump.mkdir()
            (dump / "osrm-directed-segments.tsv").write_text(
                "edge_based_node_id\tgeometry_segment_ordinal\tfrom_node_id\tto_node_id"
                "\tfrom_longitude\tfrom_latitude\tto_longitude\tto_latitude"
                "\tlength_m\tduration_ds\n"
                "0\t0\t1\t2\t20.985950\t52.268850\t20.986950\t52.268850\t68.300\t102\n"
                "0\t1\t2\t2\t20.986950\t52.268850\t20.986950\t52.268850\t0.000\t1\n"
                "1\t0\t2\t1\t20.986950\t52.268850\t20.985950\t52.268850\t68.300\t102\n",
                encoding="utf-8",
            )
            (dump / "osrm-turn-states.tsv").write_text(
                "incoming_edge_based_node_id\toutgoing_edge_based_node_id\tfrom_node_id"
                "\tvia_node_id\tto_node_id\tturn_duration_ds\trestriction_status\n"
                "0\t1\t1\t2\t1\t200\tallowed\n"
                "1\t0\t2\t1\t2\t200\tallowed\n",
                encoding="utf-8",
            )
            extract_log = root / "osrm-extract.log"
            extract_log.write_text(
                "[info] Constructing restriction graph on 1 restrictions...ok\n",
                encoding="utf-8",
            )
            output = root / "output"
            temporary = root / "temp"
            subprocess.run(
                [
                    "python3",
                    str(repository_root / "scripts/export_graph_metadata.py"),
                    "--manifest",
                    str(repository_root / "instance/pl_18_capitals_static_instance_v2.yaml"),
                    "--pbf",
                    str(osm),
                    "--osrm-dump",
                    str(dump),
                    "--osrm-extract-log",
                    str(extract_log),
                    "--output",
                    str(output),
                    "--temp",
                    str(temporary),
                ],
                check=True,
                env=os.environ.copy(),
            )
            edges = (output / "directed-base-edges.tsv").read_text(
                encoding="utf-8"
            ).splitlines()
            self.assertEqual(len(edges), 2)
            self.assertTrue(edges[0].startswith("10/0/0/1/2\t"))
            self.assertTrue(edges[1].startswith("10/0/1/2/1\t"))
            self.assertEqual(edges[0].split("\t")[10], "10.3")
            turns = (output / "edge-based-turn-states.tsv").read_text(
                encoding="utf-8"
            ).splitlines()
            self.assertEqual(len(turns), 2)
            summary = json.loads(
                (output / "export-summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(summary["legal_directed_motorcar_edges"], 2)
            self.assertEqual(summary["edge_based_turn_states"], 2)
            self.assertEqual(summary["enforced_turn_restrictions"], 1)
            self.assertEqual(summary["prohibited_turn_violations"], 0)
            self.assertEqual(summary["collapsed_duplicate_osrm_node_segments"], 1)
            self.assertEqual(summary["collapsed_duplicate_osrm_node_duration_ds"], 1)
            self.assertEqual(summary["rejected_private_nonmotorcar_edges"], 2)
            self.assertEqual(summary["forbidden_ferry_edges"], 2)
            snap = json.loads(
                (output / "start-snap.json").read_text(encoding="utf-8")
            )
            self.assertLess(snap["distance_m"], 1)
            self.assertEqual(len(snap["available_legal_initial_directions"]), 2)

            subprocess.run(
                [
                    "python3",
                    str(repository_root / "scripts/validate_graph_metadata.py"),
                    "--metadata",
                    str(output),
                ],
                check=True,
                env=os.environ.copy(),
            )

    def test_all_unmatched_pairs_are_reported_together(self) -> None:
        repository_root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location(
            "export_graph_metadata",
            repository_root / "scripts/export_graph_metadata.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segments = root / "segments.tsv"
            segments.write_text(
                "00000000000000000001/00000000000000000002\t0\t0\t1\t2\t0\t0\t1\t1\t1\t1\n"
                "00000000000000000003/00000000000000000004\t1\t0\t3\t4\t0\t0\t1\t1\t1\t1\n",
                encoding="utf-8",
            )
            candidates = root / "candidates.tsv"
            candidates.write_text("", encoding="utf-8")
            diagnostics = root / "mismatches.tsv"
            with self.assertRaisesRegex(ValueError, "2 OSRM node pairs"):
                module.resolve_segments(
                    segments,
                    candidates,
                    root / "resolved.tsv",
                    diagnostics,
                )
            self.assertEqual(len(diagnostics.read_text(encoding="utf-8").splitlines()), 3)

    def test_dynamic_oneway_values_match_osrm_behavior(self) -> None:
        repository_root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location(
            "export_graph_metadata_oneway",
            repository_root / "scripts/export_graph_metadata.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        handler = module.CandidateExporter.__new__(module.CandidateExporter)
        for value in ("alternating", "reversible"):
            with self.subTest(oneway=value):
                self.assertEqual(
                    handler.direction_flags({"oneway": value}, "secondary"),
                    (True, True),
                )

    def test_accepted_restriction_count_requires_one_osrm_record(self) -> None:
        repository_root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location(
            "export_graph_metadata_restrictions",
            repository_root / "scripts/export_graph_metadata.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            extract_log = Path(directory) / "extract.log"
            extract_log.write_text(
                "[info] Constructing restriction graph on 42 restrictions...ok\n",
                encoding="utf-8",
            )
            self.assertEqual(module.accepted_turn_restriction_count(extract_log), 42)
            extract_log.write_text("missing count\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "exactly one"):
                module.accepted_turn_restriction_count(extract_log)

    def test_prohibited_osrm_turn_record_is_rejected(self) -> None:
        repository_root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location(
            "export_graph_metadata_prohibited",
            repository_root / "scripts/export_graph_metadata.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            turns = root / "turns.tsv"
            turns.write_text(
                "incoming_edge_based_node_id\toutgoing_edge_based_node_id\tfrom_node_id"
                "\tvia_node_id\tto_node_id\tturn_duration_ds\trestriction_status\n"
                "0\t0\t1\t2\t1\t0\tprohibited\n",
                encoding="utf-8",
            )
            columns = [
                array("Q", [10]),
                array("Q", [0]),
                array("Q", [0]),
                array("Q", [1]),
                array("Q", [2]),
            ]
            count, violations = module.resolve_turns(
                turns,
                root / "resolved.tsv",
                columns,
                columns,
            )
            self.assertEqual(count, 0)
            self.assertEqual(violations, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
