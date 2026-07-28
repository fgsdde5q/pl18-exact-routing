#!/usr/bin/env python3

import re
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPOSITORY_ROOT / ".github/workflows/road-graph-stage.yml"
EXPORTER = REPOSITORY_ROOT / "tools/osrm_graph_dump.cpp"
GRAPH_STAGE = REPOSITORY_ROOT / "scripts/graph_stage.sh"
PBF_CACHE_ARCHIVE_BYTES = 2_078_836_070
DEFAULT_REPOSITORY_CACHE_BYTES = 10_000_000_000
CCACHE_MAX_BYTES = 750_000_000


class Stage2ACacheContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")

    def env_integer(self, name: str) -> int:
        match = re.search(rf"^\s+{re.escape(name)}:\s+(\d+)$", self.workflow, re.MULTILINE)
        self.assertIsNotNone(match, f"missing integer workflow environment value {name}")
        return int(match.group(1))

    def test_cache_budget_stays_below_default_repository_limit(self) -> None:
        budget = (
            PBF_CACHE_ARCHIVE_BYTES
            + self.env_integer("VCPKG_CACHE_MAX_BYTES")
            + CCACHE_MAX_BYTES
            + self.env_integer("OSRM_CHECKPOINT_MAX_BYTES")
            + self.env_integer("METADATA_CHECKPOINT_MAX_BYTES")
            + self.env_integer("CERTIFIED_CACHE_MAX_BYTES")
        )
        self.assertLess(budget, DEFAULT_REPOSITORY_CACHE_BYTES)
        self.assertGreaterEqual(DEFAULT_REPOSITORY_CACHE_BYTES - budget, 500_000_000)

    def cache_key_lines(self, prefix: str) -> list[str]:
        return [
            line.strip()
            for line in self.workflow.splitlines()
            if f"key: {prefix}" in line
        ]

    def test_osrm_checkpoint_key_uses_only_graph_inputs(self) -> None:
        key_lines = self.cache_key_lines("pl18-osrm-checkpoint-")
        self.assertEqual(len(key_lines), 2)
        self.assertEqual(key_lines[0], key_lines[1])
        key = key_lines[0]
        self.assertNotIn("github.sha", key)
        for semantic_input in (
            "PBF_SHA256",
            "profiles/pl18-car.lua",
            "OSRM_COMMIT",
            "VCPKG_COMMIT",
            "tools/osrm_graph_dump.cpp",
            "scripts/build_osrm_stage2a.sh",
        ):
            self.assertIn(semantic_input, key)
        for later_phase_input in (
            "pl_18_capitals_static_instance_v2.yaml",
            "scripts/export_graph_metadata.py",
            "scripts/certify_graph_stage.py",
            "scripts/graph_stage.sh",
        ):
            self.assertNotIn(later_phase_input, key)

    def test_metadata_checkpoint_survives_certifier_changes(self) -> None:
        key_lines = self.cache_key_lines("pl18-metadata-checkpoint-")
        self.assertEqual(len(key_lines), 2)
        self.assertEqual(key_lines[0], key_lines[1])
        key = key_lines[0]
        self.assertNotIn("github.sha", key)
        for semantic_input in (
            "PBF_SHA256",
            "pl_18_capitals_static_instance_v2.yaml",
            "profiles/pl18-car.lua",
            "OSRM_COMMIT",
            "VCPKG_COMMIT",
            "tools/osrm_graph_dump.cpp",
            "scripts/build_osrm_stage2a.sh",
            "scripts/export_graph_metadata.py",
            "scripts/compare_canonical_builds.py",
            "scripts/validate_graph_metadata.py",
        ):
            self.assertIn(semantic_input, key)
        self.assertNotIn("scripts/certify_graph_stage.py", key)
        self.assertNotIn("scripts/graph_stage.sh", key)

    def test_certified_cache_key_covers_all_certification_inputs(self) -> None:
        key_lines = self.cache_key_lines("pl18-certified-")
        self.assertEqual(len(key_lines), 2)
        self.assertEqual(key_lines[0], key_lines[1])
        key = key_lines[0]
        for semantic_input in (
            "PBF_SHA256",
            "pl_18_capitals_static_instance_v2.yaml",
            "prg-18-computational.gpkg",
            "profiles/pl18-car.lua",
            "OSRM_COMMIT",
            "VCPKG_COMMIT",
            "tools/osrm_graph_dump.cpp",
            "scripts/build_osrm_stage2a.sh",
            "scripts/export_graph_metadata.py",
            "scripts/compare_canonical_builds.py",
            "scripts/validate_graph_metadata.py",
            "scripts/certify_graph_stage.py",
            "scripts/graph_stage.sh",
        ):
            self.assertIn(semantic_input, key)

    def test_completed_checkpoints_are_saved_after_later_failures(self) -> None:
        self.assertIn(
            "if: always() && steps.osrm-checkpoint.outputs.cache-hit != 'true' "
            "&& steps.osrm-checkpoint-budget.outputs.eligible == 'true'",
            self.workflow,
        )
        self.assertIn(
            "if: always() && steps.metadata-checkpoint.outputs.cache-hit != 'true' "
            "&& steps.metadata-checkpoint-budget.outputs.eligible == 'true'",
            self.workflow,
        )
        self.assertGreaterEqual(self.workflow.count('test -f "${OSRM_CHECKPOINT_ROOT}/.complete"'), 1)
        self.assertGreaterEqual(
            self.workflow.count('! -f "${OSRM_CHECKPOINT_ROOT}/.complete"'), 1
        )
        self.assertGreaterEqual(
            self.workflow.count('test -f "${METADATA_CHECKPOINT_ROOT}/.complete"'), 1
        )
        self.assertGreaterEqual(
            self.workflow.count('! -f "${METADATA_CHECKPOINT_ROOT}/.complete"'), 1
        )

    def test_one_graph_build_feeds_two_clean_metadata_exports(self) -> None:
        source = GRAPH_STAGE.read_text(encoding="utf-8")
        self.assertEqual(source.count('"${OSRM_BUILD}/osrm-extract"'), 1)
        self.assertEqual(source.count('"${OSRM_BUILD}/osrm-partition"'), 1)
        self.assertEqual(source.count('"${OSRM_BUILD}/osrm-customize"'), 1)
        self.assertIn("build_metadata first", source)
        self.assertIn("build_metadata second", source)
        self.assertIn('--osrm-dump "${CHECKPOINT_ROOT}/dump"', source)
        self.assertIn('if [[ ! -f "${CHECKPOINT_ROOT}/.complete" ]]', source)
        self.assertIn('if [[ ! -f "${METADATA_ROOT}/.complete" ]]', source)
        self.assertIn('"${METADATA_ROOT}/graph-binary-hashes.txt"', source)
        self.assertIn(
            '"${CHECKPOINT_ROOT}/graph-binary-hashes.txt" \\\n'
            '  "${METADATA_ROOT}/graph-binary-hashes.txt"',
            source,
        )

    def test_dependency_caches_are_saved_after_failed_builds(self) -> None:
        self.assertIn(
            "if: always() && steps.vcpkg-cache.outputs.cache-hit != 'true'",
            self.workflow,
        )
        self.assertIn(
            "if: always() && steps.ccache.outputs.cache-hit != 'true'",
            self.workflow,
        )
        self.assertNotIn(
            "hashFiles('tools/osrm_graph_dump.cpp') }}\n          restore-keys: |\n"
            "            vcpkg-stage2a-",
            self.workflow,
        )

    def test_exporter_uses_pinned_osrm_alias_namespace(self) -> None:
        source = EXPORTER.read_text(encoding="utf-8")
        self.assertIn("osrm::from_alias<typename Alias::value_type>(input)", source)
        self.assertNotIn("util::from_alias<typename Alias::value_type>(input)", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
