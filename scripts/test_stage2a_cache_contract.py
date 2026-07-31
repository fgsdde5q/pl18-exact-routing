#!/usr/bin/env python3

import re
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPOSITORY_ROOT / ".github/workflows/road-graph-stage.yml"
EXPORTER = REPOSITORY_ROOT / "tools/osrm_graph_dump.cpp"
GRAPH_STAGE = REPOSITORY_ROOT / "scripts/graph_stage.sh"
REBUILD_WORKFLOW = (
    REPOSITORY_ROOT / ".github/workflows/road-graph-rebuild-check.yml"
)
REBUILD_STAGE = REPOSITORY_ROOT / "scripts/run_graph_rebuild_audit.sh"
PBF_CACHE_ARCHIVE_BYTES = 2_078_836_070
DEFAULT_REPOSITORY_CACHE_BYTES = 10_000_000_000
CCACHE_MAX_BYTES = 750_000_000


class Stage2ACacheContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")
        cls.rebuild_workflow = REBUILD_WORKFLOW.read_text(encoding="utf-8")

    def env_integer(self, name: str) -> int:
        match = re.search(rf"^\s+{re.escape(name)}:\s+(\d+)$", self.workflow, re.MULTILINE)
        self.assertIsNotNone(match, f"missing integer workflow environment value {name}")
        return int(match.group(1))

    def test_cache_budget_stays_below_default_repository_limit(self) -> None:
        self.assertEqual(
            self.env_integer("METADATA_CHECKPOINT_MAX_BYTES"), 950_000_000
        )
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

    def test_metadata_budget_covers_observed_archive(self) -> None:
        observed_archive_bytes = 932_689_698
        self.assertLessEqual(
            observed_archive_bytes,
            self.env_integer("METADATA_CHECKPOINT_MAX_BYTES"),
        )

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
        self.assertIn("jobs${{ env.JOBS }}", key)
        self.assertEqual(self.env_integer("JOBS"), 1)
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

    def test_graph_product_cache_identity_pins_single_thread_build(self) -> None:
        self.assertEqual(self.env_integer("JOBS"), 1)
        for prefix in (
            "pl18-osrm-checkpoint-",
            "pl18-metadata-checkpoint-",
            "pl18-certified-",
        ):
            key_lines = self.cache_key_lines(prefix)
            self.assertEqual(len(key_lines), 2)
            self.assertTrue(
                all("jobs${{ env.JOBS }}" in key for key in key_lines),
                prefix,
            )

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

    def test_rebuild_workflow_cannot_restore_ready_graph_products(self) -> None:
        self.assertIn("workflow_dispatch:", self.rebuild_workflow)
        for forbidden_cache in (
            "pl18-osrm-checkpoint",
            "pl18-metadata-checkpoint",
            "pl18-certified",
            "OSRM_CHECKPOINT_ROOT",
            "METADATA_CHECKPOINT_ROOT",
        ):
            self.assertNotIn(forbidden_cache, self.rebuild_workflow)
        for allowed_cache in ("pl18-pbf-", "vcpkg-stage2a-", "ccache-stage2a-"):
            self.assertIn(allowed_cache, self.rebuild_workflow)

    def test_rebuild_script_creates_two_distinct_graph_roots(self) -> None:
        source = REBUILD_STAGE.read_text(encoding="utf-8")
        self.assertIn("build_graph first-clean-build", source)
        self.assertIn("build_graph second-clean-build", source)
        self.assertIn('"ready_graph_cache_restored": False', source)
        self.assertEqual(source.count('"${OSRM_BUILD}/osrm-extract"'), 1)

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
