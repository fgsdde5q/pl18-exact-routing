#!/usr/bin/env python3

import json
import os
import subprocess
import tempfile
import unittest
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
            turns = (output / "edge-based-turn-states.tsv").read_text(
                encoding="utf-8"
            ).splitlines()
            self.assertEqual(len(turns), 2)
            summary = json.loads(
                (output / "export-summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(summary["legal_directed_motorcar_edges"], 2)
            self.assertEqual(summary["edge_based_turn_states"], 2)
            self.assertEqual(summary["rejected_private_nonmotorcar_edges"], 2)
            self.assertEqual(summary["forbidden_ferry_edges"], 2)
            snap = json.loads(
                (output / "start-snap.json").read_text(encoding="utf-8")
            )
            self.assertLess(snap["distance_m"], 1)
            self.assertEqual(len(snap["available_legal_initial_directions"]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
