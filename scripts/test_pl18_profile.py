#!/usr/bin/env python3

import hashlib
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


EXPECTED_MANIFEST_SHA256 = "f47d9a805defdb7e28005048d7ad9a7a76f666e75d508422a7edd239ce60f7ce"
EXPECTED_OSRM_COMMIT = "3c32a51"


class Pl18ProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repository_root = Path(__file__).resolve().parent.parent
        cls.manifest_path = (
            cls.repository_root / "instance/pl_18_capitals_static_instance_v2.yaml"
        )
        cls.profile_path = cls.repository_root / "profiles/pl18-car.lua"
        cls.upstream_path = Path(os.environ["UPSTREAM_CAR_PROFILE"])
        cls.profile = cls.profile_path.read_text(encoding="utf-8")
        cls.upstream = cls.upstream_path.read_text(encoding="utf-8")
        cls.manifest = json.loads(
            subprocess.run(
                [
                    "ruby",
                    str(cls.repository_root / "scripts/manifest_json.rb"),
                    str(cls.manifest_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )

    def test_profile_is_exact_generator_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            generated = Path(directory) / "pl18-car.lua"
            subprocess.run(
                [
                    "python3",
                    str(self.repository_root / "scripts/generate_pl18_profile.py"),
                    "--manifest",
                    str(self.manifest_path),
                    "--upstream-car",
                    str(self.upstream_path),
                    "--output",
                    str(generated),
                ],
                check=True,
            )
            self.assertEqual(generated.read_bytes(), self.profile_path.read_bytes())

    def test_profile_is_bound_to_pinned_inputs(self) -> None:
        digest = hashlib.sha256(self.manifest_path.read_bytes()).hexdigest()
        self.assertEqual(digest, EXPECTED_MANIFEST_SHA256)
        self.assertIn(f"commit {EXPECTED_OSRM_COMMIT}.", self.profile)
        self.assertIn(f"Frozen manifest SHA-256: {digest}.", self.profile)

    def test_intentional_upstream_differences_are_present(self) -> None:
        expected_differences = {
            "duration objective": "weight_name                     = 'duration',",
            "project access": "local function project_access",
            "private forbidden": '"private",',
            "ferry forbidden": 'data.route == "ferry"',
            "shuttle forbidden": 'data.route == "shuttle_train"',
            "unknown highway forbidden": "not profile.speeds.highway[data.highway]",
            "directional maxspeed": 'get_value_by_key("maxspeed:forward")',
            "static maxspeed cap": "math.min(result.forward_speed, forward * profile.speed_reduction)",
            "nonpositive maxspeed removes edge": "result.forward_mode = mode.inaccessible",
            "no traffic or stop penalty": "Obstacles.process_node(profile, node)",
            "gate penalty": "gate = 60,",
            "lift gate penalty": "lift_gate = 60,",
            "u-turn penalty": "u_turn_penalty                 = 20,",
            "toll allowed": "-- 'toll',    -- uncomment this to avoid tolls",
            "no ferry speed": "route_speeds = {\n    },",
        }
        for label, text in expected_differences.items():
            if label == "no traffic or stop penalty":
                self.assertNotIn(text, self.profile, label)
            else:
                self.assertIn(text, self.profile, label)
        self.assertIn("weight_name                     = 'routability',", self.upstream)
        self.assertIn("route_speeds = {\n      ferry = 5,", self.upstream)
        self.assertIn("Obstacles.process_node(profile, node)", self.upstream)

    def test_manifest_tables_are_rendered(self) -> None:
        speed = self.manifest["speed_model"]
        for table in (
            "class_speed_kmh",
            "surface_cap_kmh",
            "tracktype_cap_kmh",
            "smoothness_cap_kmh",
            "bridge_cap_kmh",
            "symbolic_maxspeed_kmh",
        ):
            for key, value in speed[table].items():
                rendered_key = key if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) else f'["{key}"]'
                rendered_value = "nil" if value is None else f"{value:g}"
                self.assertIn(f"{rendered_key} = {rendered_value},", self.profile)


if __name__ == "__main__":
    unittest.main(verbosity=2)
