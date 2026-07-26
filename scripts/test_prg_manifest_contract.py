#!/usr/bin/env python3

import csv
import json
import unittest
from decimal import Decimal
from pathlib import Path

from prg_stage import canonical_city_name, load_instance


class PrgManifestContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repository_root = Path(__file__).resolve().parent.parent
        cls.instance = load_instance(
            cls.repository_root
            / "instance/pl_18_capitals_static_instance_v1.yaml"
        )
        cls.inventory = json.loads(
            (
                cls.repository_root
                / "results/prg/prg-city-inventory.json"
            ).read_text(encoding="utf-8")
        )
        with (
            cls.repository_root / "results/prg/prg-city-inventory.csv"
        ).open(encoding="utf-8", newline="") as source:
            cls.csv_rows = list(csv.DictReader(source))

    def test_start_coordinate_matches_manifest_exactly(self) -> None:
        expected = self.instance["canonical_start"]
        actual = self.inventory["canonical_start_check"]
        self.assertEqual(actual["id"], expected["id"])
        self.assertEqual(actual["crs"], expected["crs"])
        self.assertEqual(
            Decimal(str(actual["longitude"])),
            Decimal(expected["longitude_text"]),
        )
        self.assertEqual(
            Decimal(str(actual["latitude"])),
            Decimal(expected["latitude_text"]),
        )
        self.assertEqual(actual["longitude_text"], expected["longitude_text"])
        self.assertEqual(actual["latitude_text"], expected["latitude_text"])

    def test_city_set_matches_manifest(self) -> None:
        expected = {
            (target["id"], target["name"], target["teryt"])
            for target in self.instance["targets"]
        }
        actual = {
            (city["city_id"], city["name"], city["teryt"])
            for city in self.inventory["cities"]
        }
        self.assertEqual(actual, expected)

    def test_city_order_and_bit_indexes_match_manifest(self) -> None:
        expected = [
            (
                target["bit_index"],
                target["id"],
                target["name"],
                target["teryt"],
            )
            for target in self.instance["targets"]
        ]
        actual_json = [
            (
                city["bit_index"],
                city["city_id"],
                city["name"],
                city["teryt"],
            )
            for city in self.inventory["cities"]
        ]
        actual_csv = [
            (
                int(row["bit_index"]),
                row["city_id"],
                row["name"],
                row["teryt"],
            )
            for row in self.csv_rows
        ]
        self.assertEqual(actual_json, expected)
        self.assertEqual(actual_csv, expected)
        self.assertEqual(
            [row[0] for row in expected],
            list(range(len(expected))),
        )

    def test_teryt_codes_are_unique(self) -> None:
        manifest_teryt = [target["teryt"] for target in self.instance["targets"]]
        inventory_teryt = [city["teryt"] for city in self.inventory["cities"]]
        self.assertEqual(len(manifest_teryt), len(set(manifest_teryt)))
        self.assertEqual(len(inventory_teryt), len(set(inventory_teryt)))

    def test_normalized_name_resolves_to_same_city(self) -> None:
        manifest_identity = {
            canonical_city_name(target["name"]): target["id"]
            for target in self.instance["targets"]
        }
        self.assertEqual(len(manifest_identity), len(self.instance["targets"]))
        for city in self.inventory["cities"]:
            self.assertEqual(
                manifest_identity.get(canonical_city_name(city["name"])),
                city["city_id"],
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
