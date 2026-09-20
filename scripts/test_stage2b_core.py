#!/usr/bin/env python3

import hashlib
import json
import unittest

from shapely.geometry import LineString, MultiPolygon, Polygon

from stage2b_core import (
    allocate_integer_total,
    canonical_fraction,
    canonical_geometry_sha256,
    classify_intersection,
    containing_interval,
    event_json,
    inherited_transition,
    internal_transitions,
    split_edge_id,
    split_point_id,
)


SQUARE = Polygon(((0, 0), (10, 0), (10, 10), (0, 10), (0, 0)))
RAW_HASH = "a" * 64


class GeometryTests(unittest.TestCase):
    def classify(self, coordinates, region=SQUARE):
        return classify_intersection(LineString(coordinates), region)

    def test_simple_crossing(self):
        result = self.classify(((-1, 5), (11, 5)))
        self.assertEqual(result.classification, "crossing")
        self.assertEqual(len(result.fractions), 2)

    def test_entering_and_exiting(self):
        self.assertEqual(self.classify(((-1, 5), (5, 5))).classification, "entering")
        self.assertEqual(self.classify(((5, 5), (11, 5))).classification, "exiting")

    def test_tangent_touch_does_not_visit(self):
        result = self.classify(((-1, -1), (1, 1)))
        self.assertEqual(result.classification, "entering")
        tangent = self.classify(((-1, 1), (1, -1)))
        self.assertEqual(tangent.classification, "tangential_touch")
        self.assertEqual(tangent.inside_intervals, ())

    def test_endpoint_on_boundary(self):
        result = self.classify(((0, 5), (-5, 5)))
        self.assertEqual(result.events[0].category, "endpoint_on_boundary")

    def test_boundary_overlap(self):
        result = self.classify(((0, 2), (0, 8)))
        self.assertEqual(result.classification, "overlap_along_boundary")
        self.assertEqual(result.inside_intervals, ())

    def test_polygon_hole(self):
        region = Polygon(
            ((0, 0), (10, 0), (10, 10), (0, 10), (0, 0)),
            holes=(((4, 4), (6, 4), (6, 6), (4, 6), (4, 4)),),
        )
        result = self.classify(((3, 5), (7, 5)), region)
        self.assertEqual(result.classification, "crossing_polygon_hole_boundary")
        self.assertTrue(all(event.hole_boundary for event in result.events))

    def test_multiple_crossings(self):
        disconnected = MultiPolygon(
            [
                Polygon(((0, 0), (2, 0), (2, 2), (0, 2), (0, 0))),
                Polygon(((4, 0), (6, 0), (6, 2), (4, 2), (4, 0))),
            ]
        )
        result = self.classify(((-1, 1), (7, 1)), disconnected)
        self.assertEqual(result.classification, "multiple_crossings")
        self.assertEqual(len(result.inside_intervals), 2)

    def test_disconnected_fully_inside_component(self):
        disconnected = MultiPolygon([SQUARE, Polygon(((20, 0), (30, 0), (30, 10), (20, 10), (20, 0)))])
        self.assertEqual(self.classify(((21, 5), (29, 5)), disconnected).classification, "fully_inside")

    def test_empty_negative_buffer(self):
        empty = Polygon(((0, 0), (1, 0), (1, 1), (0, 1), (0, 0))).buffer(-1, quad_segs=32)
        self.assertTrue(empty.is_empty)
        self.assertEqual(self.classify(((-1, 0.5), (2, 0.5)), empty).classification, "fully_outside")

    def test_fully_inside_and_outside(self):
        self.assertEqual(self.classify(((1, 1), (9, 9))).classification, "fully_inside")
        self.assertEqual(self.classify(((-2, -2), (-1, -1))).classification, "fully_outside")

    def test_opposite_directions(self):
        forward = self.classify(((-1, 5), (11, 5)))
        reverse = self.classify(((11, 5), (-1, 5)))
        self.assertEqual(forward.fractions, reverse.fractions)
        self.assertEqual(forward.events[0].category, "entering")
        self.assertEqual(reverse.events[0].category, "entering")


class CanonicalTests(unittest.TestCase):
    def test_duplicate_fraction_quantization(self):
        self.assertEqual(canonical_fraction(0.5), canonical_fraction(0.5000000000004))

    def test_stable_split_point_id(self):
        expected = hashlib.sha256(f"edge|0.500000000000|{RAW_HASH}|300.0".encode()).hexdigest()
        self.assertEqual(split_point_id("edge", "0.500000000000", RAW_HASH, "300.0"), expected)

    def test_multiple_cities_share_model_point_id(self):
        first = split_point_id("edge", "0.500000000000", RAW_HASH, "300.0")
        second = split_point_id("edge", "0.500000000000", RAW_HASH, "300.0")
        self.assertEqual(first, second)
        payload = event_json([{"city": "A", "bit_index": 0}, {"city": "B", "bit_index": 1}])
        self.assertEqual(json.loads(payload)[1]["city"], "B")

    def test_split_edge_id_and_sorting(self):
        self.assertEqual(split_edge_id("parent", 3), "parent@3")
        fractions = sorted({canonical_fraction(0.8), canonical_fraction(0.2), canonical_fraction(0.2)})
        self.assertEqual(fractions, ["0.200000000000", "0.800000000000"])

    def test_duration_residual_distribution(self):
        fractions = ["0.000000000000", "0.333333333333", "0.666666666667", "1.000000000000"]
        allocated = allocate_integer_total(10, fractions)
        self.assertEqual(allocated, [3, 4, 3])
        self.assertEqual(sum(allocated), 10)

    def test_start_edge_split(self):
        fractions = ["0.000000000000", "0.500000000000", "0.950000000000", "1.000000000000"]
        self.assertEqual(containing_interval(fractions, 0.922313075358), 1)

    def test_geometry_hash_is_orientation_independent(self):
        reverse = Polygon(tuple(reversed(SQUARE.exterior.coords)))
        self.assertEqual(canonical_geometry_sha256(SQUARE), canonical_geometry_sha256(reverse))

    def test_inherited_allowed_turn(self):
        transition = inherited_transition(["incoming", "outgoing", "1.5", "allowed"], {"incoming": 2})
        self.assertEqual(transition, ("incoming@2", "outgoing@0", "1.5", "inherited_stage2a_turn"))

    def test_inherited_prohibited_turn_is_rejected(self):
        with self.assertRaises(ValueError):
            inherited_transition(["incoming", "outgoing", "0.0", "prohibited"], {})

    def test_internal_split_continuation(self):
        transitions = internal_transitions("parent", 3)
        self.assertEqual(len(transitions), 2)
        self.assertTrue(all(item[2:] == ("0.0", "internal_split_continuation") for item in transitions))


if __name__ == "__main__":
    unittest.main()
