#!/usr/bin/env python3

import re
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPOSITORY_ROOT / ".github/workflows/road-graph-stage.yml"
EXPORTER = REPOSITORY_ROOT / "tools/osrm_graph_dump.cpp"
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
            + self.env_integer("GRAPH_CACHE_MAX_BYTES")
        )
        self.assertLess(budget, DEFAULT_REPOSITORY_CACHE_BYTES)
        self.assertGreaterEqual(DEFAULT_REPOSITORY_CACHE_BYTES - budget, 500_000_000)

    def test_graph_cache_key_uses_semantic_inputs_not_commit_sha(self) -> None:
        graph_key_lines = [
            line.strip() for line in self.workflow.splitlines() if "key: pl18-graph-" in line
        ]
        self.assertEqual(len(graph_key_lines), 2)
        self.assertEqual(graph_key_lines[0], graph_key_lines[1])
        key = graph_key_lines[0]
        self.assertNotIn("github.sha", key)
        for semantic_input in (
            "PBF_SHA256",
            "pl_18_capitals_static_instance_v2.yaml",
            "profiles/pl18-car.lua",
            "OSRM_COMMIT",
            "tools/osrm_graph_dump.cpp",
            "scripts/export_graph_metadata.py",
        ):
            self.assertIn(semantic_input, key)

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
