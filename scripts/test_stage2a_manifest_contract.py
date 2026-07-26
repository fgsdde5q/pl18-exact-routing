#!/usr/bin/env python3

import hashlib
import json
import subprocess
import unittest
from pathlib import Path


EXPECTED_V1_SHA256 = "9b3508725f5e19a843235b931d8e37d7bd4fcba976bf6a71a905b6cb0630a3b4"
EXPECTED_V2_SHA256 = "f47d9a805defdb7e28005048d7ad9a7a76f666e75d508422a7edd239ce60f7ce"
EXPECTED_OSM_SHA256 = "2f49ae5a61fbd70de5a8696ffa1cd1ac177bcfc9fea1d69cad43fbf4e5af4f28"
EXPECTED_PRG_RAW_SHA256 = "adf261aae31257dd24843b6f6c0e694efb40041aae3913b1c27aa4801866974c"
EXPECTED_PRG_COMPRESSED_SHA256 = "91bdab09b9fe77ea02add8cfdf88a41c03ae84587ff8fdfc1808fb340fa41684"
EXPECTED_START = ("20.986450", "52.268850")
EXPECTED_CLASS_SPEEDS = {
    "motorway",
    "motorway_link",
    "trunk",
    "trunk_link",
    "primary",
    "primary_link",
    "secondary",
    "secondary_link",
    "tertiary",
    "tertiary_link",
    "unclassified",
    "residential",
    "living_street",
    "service",
    "winter_road",
    "ice_road",
}
EXPECTED_SURFACE_CAPS = {
    "asphalt",
    "concrete",
    "concrete:plates",
    "concrete:lanes",
    "paved",
    "cement",
    "compacted",
    "fine_gravel",
    "paving_stones",
    "metal",
    "bricks",
    "grass",
    "wood",
    "sett",
    "grass_paver",
    "gravel",
    "unpaved",
    "ground",
    "dirt",
    "pebblestone",
    "tartan",
    "cobblestone",
    "clay",
    "earth",
    "stone",
    "rocky",
    "sand",
    "mud",
    "ice",
    "snow",
}
EXPECTED_TRACKTYPE_CAPS = {"grade1", "grade2", "grade3", "grade4", "grade5"}
EXPECTED_SMOOTHNESS_CAPS = {
    "intermediate",
    "bad",
    "very_bad",
    "horrible",
    "very_horrible",
    "impassable",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def decimal_text(value: object) -> str:
    return f"{float(value):.6f}"


class Stage2AManifestContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repository_root = Path(__file__).resolve().parent.parent
        cls.v1_path = cls.repository_root / "instance/pl_18_capitals_static_instance_v1.yaml"
        cls.v2_path = cls.repository_root / "instance/pl_18_capitals_static_instance_v2.yaml"
        manifest_json = subprocess.run(
            [
                "ruby",
                str(cls.repository_root / "scripts/manifest_json.rb"),
                str(cls.v2_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        cls.v2 = json.loads(manifest_json)
        cls.v1_inventory = json.loads(
            (cls.repository_root / "results/prg/prg-city-inventory.json").read_text(
                encoding="utf-8"
            )
        )

    def test_pointer_and_exact_hashes(self) -> None:
        pointer = (
            self.repository_root / "instance/CURRENT"
        ).read_text(encoding="utf-8")
        self.assertEqual(pointer, "pl_18_capitals_static_instance_v2.yaml\n")
        self.assertEqual(sha256_file(self.v1_path), EXPECTED_V1_SHA256)
        self.assertEqual(sha256_file(self.v2_path), EXPECTED_V2_SHA256)
        self.assertEqual(
            self.v2["manifest"]["supersedes"]["sha256"], EXPECTED_V1_SHA256
        )

    def test_stage1_city_identity_and_order_are_unchanged(self) -> None:
        actual = [
            (
                city["bit_index"],
                city["city"],
                city["TERYT"],
                city["PRG_object_ID"],
            )
            for city in self.v2["cities"]["canonical_bit_order"]
        ]
        v1_order = [
            (
                city["bit_index"],
                city["name"],
                city["teryt"],
                city["prg_object_id"],
            )
            for city in self.v1_inventory["cities"]
        ]
        self.assertEqual(actual, v1_order)
        self.assertEqual([row[0] for row in actual], list(range(18)))

    def test_stage1_start_and_input_hashes_are_unchanged(self) -> None:
        start = self.v2["start"]["wgs84"]
        self.assertEqual(
            (decimal_text(start["longitude"]), decimal_text(start["latitude"])),
            EXPECTED_START,
        )
        self.assertEqual(self.v2["inputs"]["osm"]["sha256"], EXPECTED_OSM_SHA256)
        self.assertEqual(
            self.v2["inputs"]["prg"]["raw_gml_sha256"], EXPECTED_PRG_RAW_SHA256
        )
        self.assertEqual(
            self.v2["inputs"]["prg"]["compressed_gml_sha256"],
            EXPECTED_PRG_COMPRESSED_SHA256,
        )
        compressed = self.repository_root / self.v2["inputs"]["prg"]["pinned_file"]
        self.assertEqual(sha256_file(compressed), EXPECTED_PRG_COMPRESSED_SHA256)
        raw = subprocess.run(
            ["zstd", "--decompress", "--stdout", str(compressed)],
            check=True,
            capture_output=True,
        ).stdout
        self.assertEqual(hashlib.sha256(raw).hexdigest(), EXPECTED_PRG_RAW_SHA256)

    def test_all_speed_tables_and_caps_are_present(self) -> None:
        speed = self.v2["speed_model"]
        self.assertEqual(set(speed["class_speed_kmh"]), EXPECTED_CLASS_SPEEDS)
        self.assertEqual(set(speed["surface_cap_kmh"]), EXPECTED_SURFACE_CAPS)
        self.assertEqual(set(speed["tracktype_cap_kmh"]), EXPECTED_TRACKTYPE_CAPS)
        self.assertEqual(
            set(speed["smoothness_cap_kmh"]), EXPECTED_SMOOTHNESS_CAPS
        )
        for table_name in (
            "class_speed_kmh",
            "surface_cap_kmh",
            "tracktype_cap_kmh",
            "smoothness_cap_kmh",
            "bridge_cap_kmh",
            "symbolic_maxspeed_kmh",
            "way_duration_multipliers",
        ):
            for value in speed[table_name].values():
                self.assertTrue(value is None or isinstance(value, (int, float)))

    def test_all_turn_penalties_are_numeric(self) -> None:
        turn = self.v2["turn_cost_model"]
        required_numeric_paths = (
            ("angle_penalty", "maximum_s"),
            ("angle_penalty", "right_hand_traffic_bias"),
            ("u_turn_extra_s",),
            ("barrier_penalty_s", "gate"),
            ("barrier_penalty_s", "lift_gate"),
            ("other_node_obstacle_penalty_s",),
            ("traffic_signal_extra_s",),
            ("stop_sign_extra_s",),
            ("link_entry_extra_s",),
            ("link_exit_extra_s",),
            ("interchange_extra_s",),
            ("roundabout_entry_extra_s",),
            ("roundabout_exit_extra_s",),
            ("toll_extra_s",),
        )
        for path in required_numeric_paths:
            value = turn
            for key in path:
                value = value[key]
            self.assertIsInstance(value, (int, float), path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
