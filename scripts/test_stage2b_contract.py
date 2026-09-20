#!/usr/bin/env python3

import json
import subprocess
import unittest
from pathlib import Path

from stage2b_core import MODEL_ALIASES, MODEL_DEPTHS


ROOT = Path(__file__).resolve().parent.parent


class Stage2BContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = subprocess.run(
            ["ruby", str(ROOT / "scripts/manifest_json.rb"), str(ROOT / "instance/pl_18_capitals_static_instance_v3.yaml")],
            check=True,
            capture_output=True,
            text=True,
        )
        cls.manifest = json.loads(result.stdout)

    def test_model_ids_and_presentation_aliases_are_frozen(self):
        self.assertEqual(MODEL_ALIASES, {"A": "M-boundary", "B": "M-300", "C": "M-500"})
        self.assertEqual(MODEL_DEPTHS, {"M-boundary": "0.0", "M-300": "300.0", "M-500": "500.0"})
        self.assertEqual(set(self.manifest["prg_geometry_model"]["visit_regions"]), set(MODEL_DEPTHS))

    def test_city_order_and_bits_are_manifest_only(self):
        cities = self.manifest["cities"]["canonical_bit_order"]
        self.assertEqual(len(cities), 18)
        self.assertEqual([city["bit_index"] for city in cities], list(range(18)))
        self.assertEqual(cities[0]["city"], "Warszawa")
        self.assertEqual(cities[-1]["city"], "Białystok")

    def test_solver_is_not_invoked(self):
        checked = [
            ROOT / "scripts/build_stage2b.py",
            ROOT / "scripts/run_stage2b_audit.sh",
            ROOT / ".github/workflows/stage2b-engine.yml",
        ]
        forbidden = ("A* solver", "label-setting", "MILP", "upper-bound search", "route optimization")
        combined = "\n".join(path.read_text(encoding="utf-8") for path in checked)
        for value in forbidden:
            self.assertNotIn(value, combined)

    def test_clean_workflow_has_no_stage2b_cache_restore(self):
        engine = (ROOT / ".github/workflows/stage2b-engine.yml").read_text(encoding="utf-8")
        self.assertNotIn("stage2b-cache", engine)
        self.assertIn("stage2b_product_cache_restored", (ROOT / "scripts/compare_stage2b_builds.py").read_text(encoding="utf-8"))

    def test_full_gate_inventory_uses_authoritative_count(self):
        source = (ROOT / "scripts/build_stage2b.py").read_text(encoding="utf-8")
        self.assertIn("EXPECTED_EDGE_COUNT = 33_473_569", source)
        self.assertIn('"GATE_INVENTORY_COMPLETE"', source)

    def test_start_states_are_separate(self):
        source = (ROOT / "scripts/build_stage2b.py").read_text(encoding="utf-8")
        self.assertIn('"ordinary_start_states": 0', source)
        self.assertIn('"ordinary_turn_state_count_contribution": 0', source)


if __name__ == "__main__":
    unittest.main()
