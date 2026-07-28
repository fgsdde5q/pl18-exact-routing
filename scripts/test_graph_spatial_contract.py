#!/usr/bin/env python3

import unittest
from pathlib import Path

from certify_graph_stage import point_inside_warszawa


class GraphSpatialContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repository_root = Path(__file__).resolve().parent.parent

    def test_frozen_start_is_inside_warszawa(self) -> None:
        self.assertTrue(
            point_inside_warszawa(
                self.repository_root,
                longitude=20.986450,
                latitude=52.268850,
            )
        )

    def test_known_outside_point_is_rejected(self) -> None:
        self.assertFalse(
            point_inside_warszawa(
                self.repository_root,
                longitude=19.0,
                latitude=52.0,
            )
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
