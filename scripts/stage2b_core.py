#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable

from shapely import get_parts, normalize, to_wkb
from shapely.geometry import LineString, Point, Polygon


FRACTION_PLACES = 12
FRACTION_QUANTUM = Decimal("0.000000000001")
MODEL_DEPTHS = {"M-boundary": "0.0", "M-300": "300.0", "M-500": "500.0"}
MODEL_ALIASES = {"A": "M-boundary", "B": "M-300", "C": "M-500"}


def canonical_fraction(value: float) -> str:
    bounded = min(1.0, max(0.0, value))
    return format(
        Decimal(str(bounded)).quantize(FRACTION_QUANTUM, rounding=ROUND_HALF_UP),
        ".12f",
    )


def split_point_id(parent_edge_id: str, fraction: str, raw_prg_sha256: str, depth: str) -> str:
    payload = f"{parent_edge_id}|{fraction}|{raw_prg_sha256}|{depth}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def split_edge_id(parent_edge_id: str, ordinal: int) -> str:
    return f"{parent_edge_id}@{ordinal}"


def containing_interval(fractions: list[str], fraction: float) -> int:
    if not fractions or fractions[0] != "0.000000000000" or fractions[-1] != "1.000000000000":
        raise ValueError("fractions must cover [0, 1]")
    canonical = Decimal(canonical_fraction(fraction))
    for ordinal, (start, end) in enumerate(zip(fractions, fractions[1:])):
        if Decimal(start) <= canonical < Decimal(end):
            return ordinal
    if canonical == Decimal("1.000000000000"):
        return len(fractions) - 2
    raise ValueError("fraction is outside [0, 1]")


def inherited_transition(row: list[str], split_last: dict[str, int]) -> tuple[str, str, str, str]:
    if len(row) != 4 or row[3] != "allowed":
        raise ValueError("only authoritative allowed Stage 2A turns may be inherited")
    return (
        split_edge_id(row[0], split_last.get(row[0], 0)),
        split_edge_id(row[1], 0),
        row[2],
        "inherited_stage2a_turn",
    )


def internal_transitions(parent_edge_id: str, child_count: int):
    if child_count < 1:
        raise ValueError("a parent must have at least one child")
    return [
        (split_edge_id(parent_edge_id, ordinal - 1), split_edge_id(parent_edge_id, ordinal), "0.0", "internal_split_continuation")
        for ordinal in range(1, child_count)
    ]


def canonical_geometry_sha256(geometry) -> str:
    canonical = normalize(geometry)
    return hashlib.sha256(to_wkb(canonical, byte_order=1, include_srid=False)).hexdigest()


def geometry_components(geometry) -> int:
    if geometry.is_empty:
        return 0
    return sum(1 for _ in get_parts(geometry))


def iter_atomic(geometry) -> Iterable:
    if geometry.is_empty:
        return
    if geometry.geom_type.startswith("Multi") or geometry.geom_type == "GeometryCollection":
        for part in geometry.geoms:
            yield from iter_atomic(part)
    else:
        yield geometry


def boundary_fractions(line: LineString, region, tolerance: float = 1e-9) -> tuple[dict[str, str], bool]:
    intersection = line.intersection(region.boundary)
    fractions: dict[str, str] = {}
    has_overlap = False
    length = line.length
    if length <= tolerance:
        return {}, False
    def add_fraction(value: float) -> None:
        canonical = canonical_fraction(value)
        raw = format(min(1.0, max(0.0, value)), ".17g")
        fractions.setdefault(canonical, raw)

    for part in iter_atomic(intersection):
        if part.geom_type == "Point":
            add_fraction(line.project(part) / length)
        elif part.geom_type == "LineString":
            has_overlap = has_overlap or part.length > tolerance
            coordinates = list(part.coords)
            if coordinates:
                add_fraction(line.project(Point(coordinates[0])) / length)
                add_fraction(line.project(Point(coordinates[-1])) / length)
        else:
            for coordinate in getattr(part, "coords", []):
                add_fraction(line.project(Point(coordinate)) / length)
    return dict(sorted(fractions.items(), key=lambda item: Decimal(item[0]))), has_overlap


def point_on_hole_boundary(point: Point, region, tolerance: float) -> bool:
    for polygon in iter_atomic(region):
        if not isinstance(polygon, Polygon):
            continue
        for ring in polygon.interiors:
            if ring.distance(point) <= tolerance:
                return True
    return False


def interpolate(line: LineString, fraction: str) -> Point:
    return line.interpolate(float(fraction), normalized=True)


@dataclass(frozen=True)
class BoundaryEvent:
    fraction: str
    unquantized_fraction: str
    category: str
    hole_boundary: bool


@dataclass(frozen=True)
class IntersectionResult:
    classification: str
    fractions: tuple[str, ...]
    events: tuple[BoundaryEvent, ...]
    inside_intervals: tuple[tuple[str, str], ...]
    boundary_overlap: bool


def classify_intersection(line: LineString, region, tolerance: float = 0.01) -> IntersectionResult:
    if line.length <= 1e-9 or not line.is_valid:
        return IntersectionResult("degenerate_numerical_case", (), (), (), False)
    fraction_map, overlap = boundary_fractions(line, region)
    fractions = list(fraction_map)
    cuts = sorted({"0.000000000000", "1.000000000000", *fractions}, key=Decimal)
    interval_inside: list[bool] = []
    inside_intervals: list[tuple[str, str]] = []
    for start, end in zip(cuts, cuts[1:]):
        if start == end:
            continue
        midpoint = (float(start) + float(end)) / 2.0
        inside = region.contains(line.interpolate(midpoint, normalized=True))
        interval_inside.append(inside)
        if inside:
            inside_intervals.append((start, end))

    events: list[BoundaryEvent] = []
    for fraction in fractions:
        index = cuts.index(fraction)
        before = interval_inside[index - 1] if index > 0 else False
        after = interval_inside[index] if index < len(interval_inside) else False
        point = interpolate(line, fraction)
        endpoint = fraction in {"0.000000000000", "1.000000000000"}
        if endpoint:
            category = "endpoint_on_boundary"
        elif overlap:
            category = "overlap_along_boundary"
        elif not before and after:
            category = "entering"
        elif before and not after:
            category = "exiting"
        else:
            category = "tangential_touch"
        hole = point_on_hole_boundary(point, region, tolerance)
        if hole and category in {"entering", "exiting"}:
            category = "crossing_polygon_hole_boundary"
        events.append(BoundaryEvent(fraction, fraction_map[fraction], category, hole))

    transitions = sum(event.category in {"entering", "exiting", "crossing_polygon_hole_boundary"} for event in events)
    if overlap:
        classification = "overlap_along_boundary"
    elif transitions > 2:
        classification = "multiple_crossings"
    elif any(event.hole_boundary for event in events):
        classification = "crossing_polygon_hole_boundary"
    elif any(event.category == "tangential_touch" for event in events) and not inside_intervals:
        classification = "tangential_touch"
    elif not events:
        classification = "fully_inside" if inside_intervals else "fully_outside"
    elif inside_intervals and cuts[0] == inside_intervals[0][0] and transitions == 1:
        classification = "exiting"
    elif inside_intervals and cuts[-1] == inside_intervals[-1][1] and transitions == 1:
        classification = "entering"
    elif inside_intervals and transitions >= 2:
        classification = "crossing"
    elif all(event.category == "endpoint_on_boundary" for event in events):
        classification = "endpoint_on_boundary"
    else:
        classification = "fully_outside"
    return IntersectionResult(
        classification,
        tuple(fractions),
        tuple(events),
        tuple(inside_intervals),
        overlap,
    )


def allocate_integer_total(total: int, fractions: list[str]) -> list[int]:
    if total < 0:
        raise ValueError("total must be non-negative")
    widths = [float(end) - float(start) for start, end in zip(fractions, fractions[1:])]
    if any(width <= 0 for width in widths):
        raise ValueError("split fractions must be strictly increasing")
    exact = [total * width / sum(widths) for width in widths]
    allocated = [math.floor(value) for value in exact]
    residual = total - sum(allocated)
    order = sorted(range(len(exact)), key=lambda index: (-(exact[index] - allocated[index]), index))
    for index in order[:residual]:
        allocated[index] += 1
    return allocated


def event_json(events: list[dict]) -> str:
    return json.dumps(events, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
