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
                "1\t0\t2\t1\t2\t200\tallowed\n",
                encoding="utf-8",
            )
            extract_log = root / "osrm-extract.log"
            extract_log.write_text(
                "[info] Collecting node information on 1 restrictions...ok\n"
                "[info] Removing invalid turn restrictions...removed 0 invalid turn restrictions, after 0s\n"
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
            self.assertEqual(len(edges[0].split("\t")), 13)
            self.assertEqual(edges[0].split("\t")[8], "10.3")
            turns = (output / "edge-based-turn-states.tsv").read_text(
                encoding="utf-8"
            ).splitlines()
            self.assertEqual(len(turns), 1)
            summary = json.loads(
                (output / "export-summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(summary["legal_directed_motorcar_edges"], 2)
            self.assertEqual(summary["edge_based_turn_states"], 1)
            self.assertEqual(summary["enforced_turn_restrictions"], 1)
            self.assertEqual(summary["invalid_turn_restrictions"], 0)
            self.assertEqual(summary["expected_prohibited_turn_transitions"], 1)
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
            self.assertEqual(
                len(snap["geometrically_possible_initial_directions"]), 2
            )
            restriction_certificate = json.loads(
                (output / "turn-restriction-certificate.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                restriction_certificate["expected_prohibited_transitions"]["count"],
                1,
            )
            self.assertEqual(
                restriction_certificate["expected_prohibited_transitions"][
                    "observed_in_exported_turn_states"
                ],
                0,
            )
            self.assertEqual(
                restriction_certificate[
                    "osrm_non_enforced_candidate_transitions"
                ]["count"],
                0,
            )

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
            certificate_path = output / "turn-restriction-certificate.json"
            inconsistent = json.loads(
                certificate_path.read_text(encoding="utf-8")
            )
            inconsistent["source_candidate_transitions"]["count"] += 1
            certificate_path.write_text(
                json.dumps(inconsistent, indent=2) + "\n",
                encoding="utf-8",
            )
            validation = subprocess.run(
                [
                    "python3",
                    str(repository_root / "scripts/validate_graph_metadata.py"),
                    "--metadata",
                    str(output),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=os.environ.copy(),
            )
            self.assertNotEqual(validation.returncode, 0)
            self.assertIn(
                "restriction candidate partition is inconsistent",
                validation.stderr,
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
                "[info] Collecting node information on 45 restrictions...ok\n"
                "[info] Removing invalid turn restrictions...removed 3 invalid turn restrictions, after 0s\n"
                "[info] Constructing restriction graph on 42 restrictions...ok\n",
                encoding="utf-8",
            )
            self.assertEqual(module.accepted_turn_restriction_count(extract_log), 42)
            self.assertEqual(
                module.osrm_restriction_summary(extract_log),
                {
                    "parsed_restrictions": 45,
                    "invalid_restrictions": 3,
                    "accepted_restrictions": 42,
                    "unresolved_before_graph_validation": 0,
                },
            )
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

    def test_allowed_source_candidate_is_not_claimed_prohibited(self) -> None:
        repository_root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location(
            "export_graph_metadata_candidate_partition",
            repository_root / "scripts/export_graph_metadata.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base_edges = root / "edges.tsv"
            base_edges.write_text(
                "10/0/0/1/2\t1\t2\t0\t0\t0\t0\t1\t1\tresidential\t30\tpublic_default\tnot_ferry\n"
                "10/0/1/2/1\t2\t1\t0\t0\t0\t0\t1\t1\tresidential\t30\tpublic_default\tnot_ferry\n",
                encoding="utf-8",
            )
            turn_states = root / "turns.tsv"
            turn_states.write_text(
                "10/0/0/1/2\t10/0/1/2/1\t0\n",
                encoding="utf-8",
            )
            extract_log = root / "extract.log"
            extract_log.write_text(
                "[info] Collecting node information on 1 restrictions...ok\n"
                "[info] Removing invalid turn restrictions...removed 1 invalid turn restrictions, after 0s\n"
                "[info] Constructing restriction graph on 0 restrictions...ok\n",
                encoding="utf-8",
            )
            relation = {
                "relation_id": 20,
                "recognized_values": ["no_u_turn"],
                "ignored_by_except": False,
                "members": [
                    {"type": "w", "ref": 10, "role": "from"},
                    {"type": "n", "ref": 2, "role": "via"},
                    {"type": "w", "ref": 10, "role": "to"},
                ],
            }
            certificate = module.build_restriction_certificate(
                [relation],
                [],
                base_edges,
                turn_states,
                extract_log,
                {10: [1, 2]},
            )
            self.assertEqual(
                certificate["source_candidate_transitions"]["count"], 1
            )
            self.assertEqual(
                certificate["expected_prohibited_transitions"]["count"], 0
            )
            non_enforced = certificate[
                "osrm_non_enforced_candidate_transitions"
            ]
            self.assertEqual(non_enforced["count"], 0)
            self.assertEqual(non_enforced["sample"], [])

    def test_restriction_candidates_preserve_from_way_identity(self) -> None:
        repository_root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location(
            "export_graph_metadata_restriction_way_identity",
            repository_root / "scripts/export_graph_metadata.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base_edges = root / "edges.tsv"
            base_edges.write_text(
                "10/0/0/1/2\t1\t2\t0\t0\t0\t0\t1\t1\tresidential\t30\tpublic_default\tnot_ferry\n"
                "11/0/0/1/2\t1\t2\t0\t0\t0\t0\t1\t1\tresidential\t30\tpublic_default\tnot_ferry\n"
                "12/0/0/2/3\t2\t3\t0\t0\t0\t0\t1\t1\tresidential\t30\tpublic_default\tnot_ferry\n"
                "13/0/0/2/4\t2\t4\t0\t0\t0\t0\t1\t1\tresidential\t30\tpublic_default\tnot_ferry\n",
                encoding="utf-8",
            )
            turn_states = root / "turns.tsv"
            turn_states.write_text(
                "11/0/0/1/2\t13/0/0/2/4\t0.0\tallowed\n",
                encoding="utf-8",
            )
            extract_log = root / "extract.log"
            extract_log.write_text(
                "[info] Collecting node information on 1 restrictions...ok\n"
                "[info] Removing invalid turn restrictions...removed 0 invalid turn restrictions, after 0s\n"
                "[info] Constructing restriction graph on 1 restrictions...ok\n",
                encoding="utf-8",
            )
            relation = {
                "relation_id": 20,
                "recognized_values": ["only_straight_on"],
                "ignored_by_except": False,
                "conditional": False,
                "tags": {"restriction": "only_straight_on"},
                "members": [
                    {"type": "w", "ref": 10, "role": "from"},
                    {"type": "n", "ref": 2, "role": "via"},
                    {"type": "w", "ref": 12, "role": "to"},
                ],
            }
            certificate = module.build_restriction_certificate(
                [relation], [], base_edges, turn_states, extract_log,
                {10: [1, 2], 12: [2, 3]},
            )
            non_enforced = certificate["osrm_non_enforced_candidate_transitions"]
            self.assertEqual(non_enforced["count"], 1)
            record = non_enforced["machine_readable_records"][0]
            self.assertEqual(record["restriction_from_way_id"], 10)
            self.assertEqual(record["transition_incoming_way_id"], 11)
            self.assertTrue(record["matches_frozen_static_model"])
            self.assertEqual(
                record["projection_mismatch"],
                "incoming_way_id_differs_from_restriction_from_way",
            )

    def test_canonical_edges_exclude_internal_osrm_identifiers(self) -> None:
        repository_root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location(
            "export_graph_metadata_canonical",
            repository_root / "scripts/export_graph_metadata.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        semantic_fields = [
            "10/0/0/1/2",
            "1",
            "2",
            "20.0",
            "52.0",
            "20.1",
            "52.1",
            "10.0",
            "1.0",
            "residential",
            "30.0",
            "public_default",
            "not_ferry",
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projected_outputs = []
            for index, internal_fields in enumerate((["17", "0"], ["991", "8"])):
                resolved = root / f"resolved-{index}.tsv"
                resolved.write_text(
                    "\t".join(
                        [
                            semantic_fields[0],
                            *internal_fields,
                            *semantic_fields[1:],
                        ]
                    )
                    + "\n",
                    encoding="utf-8",
                )
                projected = root / f"projected-{index}.tsv"
                module.project_canonical_base_edges(resolved, projected)
                projected_outputs.append(projected.read_bytes())
            self.assertEqual(projected_outputs[0], projected_outputs[1])
            self.assertEqual(
                projected_outputs[0].decode("utf-8").rstrip("\n").split("\t"),
                semantic_fields,
            )

    def test_single_start_direction_has_tag_rule_explanation(self) -> None:
        repository_root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location(
            "export_graph_metadata_start",
            repository_root / "scripts/export_graph_metadata.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        handler = module.CandidateExporter.__new__(module.CandidateExporter)
        handler.access_hierarchy = ["motorcar", "motor_vehicle", "vehicle", "access"]
        handler.allowed_access = {"yes", "permissive", "destination"}
        handler.forbidden_access = {"no", "private"}
        handler.class_speeds = {"residential": 50}
        handler.surface_caps = {}
        handler.tracktype_caps = {}
        handler.smoothness_caps = {}
        handler.bridge_caps = {}
        handler.symbolic_speeds = {}
        handler.speed_reduction = 0.9
        handler.vehicle = {
            "height_m": 2.0,
            "width_m": 1.9,
            "length_m": 4.8,
            "weight_kg": 2000,
        }
        start_snap = {
            "segment_ordinal": 0,
            "available_legal_initial_directions": [
                {"stable_edge_id": "10/0/0/1/2", "direction_bit": 0}
            ],
        }
        selected_way = {
            "id": 10,
            "node_ids": [1, 2],
            "tags": {"highway": "residential", "oneway": "yes"},
        }
        result = module.enrich_start_direction_certificate(
            start_snap, selected_way, handler
        )
        self.assertEqual(
            result["single_direction_explanation"]["status"],
            "START_DIRECTIONS_CERTIFIED",
        )
        backward = result["geometrically_possible_initial_directions"][1]
        self.assertIn("forbidden_by_oneway_tag", backward["rejection_reasons"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
