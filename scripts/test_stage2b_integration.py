#!/usr/bin/env python3

import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pyproj import Transformer
from shapely.geometry import Polygon

import build_stage2b


def projected_box(longitude: float, latitude: float, half_size: float):
    x, y = Transformer.from_crs("EPSG:4326", "EPSG:2180", always_xy=True).transform(longitude, latitude)
    return Polygon(
        (
            (x - half_size, y - half_size),
            (x + half_size, y - half_size),
            (x + half_size, y + half_size),
            (x - half_size, y + half_size),
            (x - half_size, y - half_size),
        )
    )


class Stage2BIntegrationTests(unittest.TestCase):
    def test_streaming_edge_and_turn_exports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            edges_path = root / "edges.tsv"
            turns_path = root / "turns.tsv"
            artifacts = root / "artifacts"
            artifacts.mkdir()
            edge_rows = [
                ["1/0/0/10/11", "10", "11", "19.98", "52.0", "20.02", "52.0", "2750.000", "120.0", "primary", "65.000000", "public_default", "not_ferry"],
                ["2/0/0/20/21", "20", "21", "18.0", "51.0", "18.01", "51.0", "700.000", "40.0", "secondary", "55.000000", "public_default", "not_ferry"],
            ]
            with edges_path.open("w", encoding="utf-8", newline="") as output:
                csv.writer(output, delimiter="\t", lineterminator="\n").writerows(edge_rows)
            turns_path.write_text("1/0/0/10/11\t2/0/0/20/21\t1.5\tallowed\n", encoding="utf-8")
            regions = {
                "M-boundary": {0: projected_box(20.0, 52.0, 500)},
                "M-300": {0: projected_box(20.0, 52.0, 300)},
                "M-500": {0: projected_box(20.0, 52.0, 100)},
            }
            cities = [{"bit_index": 0, "city": "Fixture City"}]
            start = {"selected_stable_edge_id": "1/0/0/10/11", "fraction": 0.5}
            with patch.object(build_stage2b, "EXPECTED_EDGE_COUNT", 2):
                result = build_stage2b.build_edges(edges_path, regions, cities, "a" * 64, artifacts, start)
            self.assertEqual(result["counts"]["parents"], 2)
            self.assertGreater(result["counts"]["split_edges"], 2)
            self.assertGreater(result["point_records"], 0)
            self.assertEqual(result["parent_duration_ds"], result["child_duration_ds"])
            self.assertEqual(result["parent_length_nm"], result["child_length_nm"])
            with patch.object(build_stage2b, "EXPECTED_TURN_COUNT", 1):
                inherited = build_stage2b.build_turns(turns_path, result["split_last"], artifacts)
            self.assertEqual(inherited, 1)
            self.assertTrue((artifacts / "split-edges.tsv.zst").stat().st_size > 0)
            self.assertTrue((artifacts / "gates.tsv.zst").stat().st_size > 0)


if __name__ == "__main__":
    unittest.main()
