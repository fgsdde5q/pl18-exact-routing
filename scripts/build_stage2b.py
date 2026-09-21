#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from contextlib import ExitStack
from decimal import Decimal
from pathlib import Path

import shapely
from pyproj import Transformer
from shapely import from_wkb, to_wkb
from shapely.geometry import LineString, Point
from shapely.ops import transform

from stage2b_core import (
    MODEL_ALIASES,
    MODEL_DEPTHS,
    allocate_integer_total,
    canonical_fraction,
    canonical_geometry_sha256,
    classify_intersection,
    containing_interval,
    event_json,
    geometry_components,
    inherited_transition,
    split_edge_id,
    split_point_id,
)


EXPECTED_EDGE_COUNT = 33_615_581
EXPECTED_TURN_COUNT = 15_728_284
EXPECTED_EDGE_SHA256 = "4cac4088cdc7f7c65966840816acf029a2143ec70639837f25c86f9850671cd6"
EXPECTED_TURN_SHA256 = "e41c43e6d9f703b523b9cae39a4f2e8187b0c70bf84c1c77a67b6adc28238dce"
EXPECTED_MANIFEST_SHA256 = "ee6964ee4c95f3f574a6e1234cb63e91999e64d53a5e5f7c7066114d4c067b9c"
EXPECTED_PRG_RAW_SHA256 = "adf261aae31257dd24843b6f6c0e694efb40041aae3913b1c27aa4801866974c"
EXPECTED_PRG_COMPUTATIONAL_SHA256 = "ae8c1a6ac592434e48f0d1ae1db261da5c89ac82b482605bd9387eee36134a4c"
EXPECTED_PRG_SOURCE_MANIFEST_SHA256 = "2d084b7fdc9c0048d83e7153ad5abff32f9184efa42a0bfbff29731ddabcc092"
EXPECTED_PROFILE_SHA256 = "0b2dc359c4618b215efb512923a02471a2df208024e5d4d9f29e4a86a67f1001"
EXPECTED_OSRM_COMMIT = "3c32a51bf58d12bf30efd0808d0b6ad51d334122"
EXPECTED_GRAPH_MANIFEST_SHA256 = "cc56426015116d0fe8c3c293d0c4374a04c6bd9efd074a4552ec69a95be05c79"
EXPECTED_START_SNAP_SHA256 = "ce719e75151ed19a0e4f07d0b11f20164a4c49d0cae88d936c518a07473c648a"
EXPECTED_RESTRICTION_CERTIFICATE_SHA256 = "1c39c8107f3abeddf84247f597fa9a9154c228031357e7945f93ab33dc03d2b8"
EXPECTED_GEOS = "3.13.1"
MODEL_ORDER = ("M-boundary", "M-300", "M-500")
COMPACT_FILES = (
    "stage2b-contract.json",
    "stage2b-manifest.json",
    "stage2b-validation-report.md",
    "geometry-models-summary.json",
    "split-points-summary.json",
    "split-edges-summary.json",
    "turn-states-summary.json",
    "city-bitmask-summary.json",
    "gates-summary.json",
    "topology-preservation.json",
    "cost-preservation.json",
    "inherited-turn-certificate.json",
    "start-state-certificate.json",
    "export-reproducibility.json",
    "cache-budget-certificate.json",
)


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def json_dump(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_manifest(repository: Path) -> dict:
    path = repository / "instance/pl_18_capitals_static_instance_v3.yaml"
    if digest(path) != EXPECTED_MANIFEST_SHA256:
        raise ValueError("frozen manifest v3 SHA-256 mismatch")
    result = subprocess.run(
        ["ruby", str(repository / "scripts/manifest_json.rb"), str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def verify_input(path: Path, expected: str, label: str) -> None:
    actual = digest(path)
    if actual != expected:
        raise ValueError(f"{label} SHA-256 mismatch: {actual}")


def open_zstd_writer(path: Path, stack: ExitStack):
    process = subprocess.Popen(["zstd", "-q", "-T0", "-6", "-o", str(path)], stdin=subprocess.PIPE, text=True)
    if process.stdin is None:
        raise RuntimeError("zstd stdin unavailable")
    stack.callback(lambda: process.wait() == 0 or (_ for _ in ()).throw(RuntimeError(f"zstd failed: {path}")))
    stack.callback(process.stdin.close)
    return process.stdin


def load_regions(repository: Path, manifest: dict, geometry_output: Path):
    from osgeo import ogr

    ogr.UseExceptions()
    source = repository / "results/prg/prg-18-computational.gpkg"
    source_hash = digest(source)
    if source_hash != EXPECTED_PRG_COMPUTATIONAL_SHA256:
        raise ValueError("frozen computational PRG SHA-256 mismatch")
    dataset = ogr.Open(str(source))
    layer = dataset.GetLayer(0)
    by_bit = {}
    for feature in layer:
        by_bit[int(feature["bit_index"])] = from_wkb(bytes(feature.GetGeometryRef().ExportToWkb()))
    cities = manifest["cities"]["canonical_bit_order"]
    regions = {model: {} for model in MODEL_ORDER}
    summaries = []
    with geometry_output.open("w", encoding="utf-8", newline="") as output:
        output.write("model_id\tbit_index\tcity\tteryt\tgeometry_wkb_hex\n")
        for city in cities:
            bit = int(city["bit_index"])
            original = by_bit[bit]
            previous = None
            for model in MODEL_ORDER:
                depth = float(MODEL_DEPTHS[model])
                region = original if depth == 0 else shapely.buffer(
                    original, -depth, quad_segs=32, join_style="round"
                )
                if not region.is_valid:
                    raise ValueError(f"{city['city']} {model} buffer is invalid")
                regions[model][bit] = region
                contained = previous is None or region.difference(previous).area <= 0.0001
                if not contained:
                    raise ValueError(f"{city['city']} {model} is not contained in the less strict model")
                previous = region
                output.write(
                    f"{model}\t{bit}\t{city['city']}\t{city['TERYT']}\t"
                    f"{to_wkb(region, byte_order=1, include_srid=False).hex()}\n"
                )
                summaries.append(
                    {
                        "model_id": model,
                        "presentation_alias": next(alias for alias, value in MODEL_ALIASES.items() if value == model),
                        "depth_m": depth,
                        "bit_index": bit,
                        "city": city["city"],
                        "teryt": city["TERYT"],
                        "valid": bool(region.is_valid),
                        "empty": bool(region.is_empty),
                        "component_count": geometry_components(region),
                        "area_m2": round(region.area, 6),
                        "perimeter_m": round(region.length, 6),
                        "contained_in_less_strict_model": contained,
                        "canonical_geometry_sha256": canonical_geometry_sha256(region),
                    }
                )
    return regions, summaries, source_hash


def parent_row(row: list[str]) -> dict:
    if len(row) != 13:
        raise ValueError(f"directed base edge has {len(row)} fields, expected 13")
    return {
        "id": row[0], "from_node": row[1], "to_node": row[2],
        "from_lon": float(row[3]), "from_lat": float(row[4]),
        "to_lon": float(row[5]), "to_lat": float(row[6]),
        "length_m": Decimal(row[7]), "duration_s": Decimal(row[8]),
        "highway": row[9], "speed_kmh": row[10], "access": row[11], "ferry": row[12],
    }


def candidate_bits(parent: dict, geographic_bounds: dict) -> list[int]:
    min_x = min(parent["from_lon"], parent["to_lon"])
    min_y = min(parent["from_lat"], parent["to_lat"])
    max_x = max(parent["from_lon"], parent["to_lon"])
    max_y = max(parent["from_lat"], parent["to_lat"])
    return [
        bit for bit, bounds in geographic_bounds.items()
        if not (max_x < bounds[0] or min_x > bounds[2] or max_y < bounds[1] or min_y > bounds[3])
    ]


def coalesce_zero_length_intervals(fractions: list[str], length_nm: int) -> tuple[list[str], dict[str, str]]:
    if not fractions or fractions[0] != "0.000000000000" or fractions[-1] != "1.000000000000":
        raise ValueError("fractions must cover [0, 1]")
    if any(left >= right for left, right in zip(fractions, fractions[1:])):
        raise ValueError("fractions must be strictly increasing")
    remap = {fraction: fraction for fraction in fractions}
    kept = list(fractions)
    while len(kept) > 2:
        allocated = allocate_integer_total(length_nm, kept)
        zero_index = next((index for index, length in enumerate(allocated) if length <= 0), None)
        if zero_index is None:
            return kept, remap
        if zero_index + 1 < len(kept) - 1:
            removed = kept.pop(zero_index + 1)
            target = kept[zero_index]
        elif zero_index > 0:
            removed = kept.pop(zero_index)
            target = kept[zero_index]
        else:
            break
        for original, current in list(remap.items()):
            if current == removed:
                remap[original] = target
    allocated = allocate_integer_total(length_nm, kept)
    if any(length <= 0 for length in allocated):
        raise ValueError("unable to coalesce zero-length split interval")
    return kept, remap


def build_edges(base_edges: Path, regions, cities, raw_hash: str, output: Path, start_snap: dict):
    to_2180 = Transformer.from_crs("EPSG:4326", "EPSG:2180", always_xy=True)
    to_4326 = Transformer.from_crs("EPSG:2180", "EPSG:4326", always_xy=True)
    strict_bounds = {
        bit: transform(to_4326.transform, regions["M-boundary"][bit]).bounds
        for bit in regions["M-boundary"]
    }
    split_last = {}
    counts = Counter()
    classification_counts = {model: Counter() for model in MODEL_ORDER}
    city_model_counts = defaultdict(Counter)
    city_model_gate_counts = defaultdict(Counter)
    mask_counts = {model: Counter() for model in MODEL_ORDER}
    parent_length_nm = child_length_nm = parent_duration_ds = child_duration_ds = 0
    internal_turn_count = 0
    point_records = 0
    gate_records = 0
    city_names = {int(city["bit_index"]): city["city"] for city in cities}

    with ExitStack() as stack:
        points = open_zstd_writer(output / "split-points.tsv.zst", stack)
        edges = open_zstd_writer(output / "split-edges.tsv.zst", stack)
        gates = open_zstd_writer(output / "gates.tsv.zst", stack)
        internal = open_zstd_writer(output / "internal-turn-states.tsv.zst", stack)
        points.write("split_point_id\tparent_edge_id\tunquantized_fractions_json\tfraction\twgs84_x\twgs84_y\tepsg2180_x\tepsg2180_y\tmodel_id\tcity_mask\tevents_json\tprg_raw_sha256\n")
        edges.write("split_edge_id\tparent_edge_id\tordinal\tstart_fraction\tend_fraction\tfrom_lon\tfrom_lat\tto_lon\tto_lat\tlength_m\tduration_s\tM_boundary_mask\tM_300_mask\tM_500_mask\tboundary_event_ids\n")
        gates.write("model_id\tbit_index\tcity\tevent_category\tsplit_point_id\tparent_edge_id\tincoming_split_edge_id\toutgoing_split_edge_id\tosm_way_id\tfraction\tdirection_bit\twgs84_x\twgs84_y\tepsg2180_x\tepsg2180_y\n")
        internal.write("incoming_split_edge_id\toutgoing_split_edge_id\tturn_duration_s\ttransition_category\n")
        with base_edges.open(encoding="utf-8", newline="") as source:
            for raw in csv.reader(source, delimiter="\t"):
                parent = parent_row(raw)
                counts["parents"] += 1
                bits = candidate_bits(parent, strict_bounds)
                all_fractions = {"0.000000000000", "1.000000000000"}
                events_at = defaultdict(list)
                line_wgs = None
                line_2180 = None
                if bits:
                    line_wgs = LineString(((parent["from_lon"], parent["from_lat"]), (parent["to_lon"], parent["to_lat"])))
                    line_2180 = transform(to_2180.transform, line_wgs)
                    for model in MODEL_ORDER:
                        for bit in bits:
                            result = classify_intersection(line_2180, regions[model][bit])
                            classification_counts[model][result.classification] += 1
                            city_model_counts[(model, bit)][result.classification] += 1
                            for event in result.events:
                                all_fractions.add(event.fraction)
                                events_at[(model, event.fraction)].append(
                                    {"bit_index": bit, "city": city_names[bit], "category": event.category, "hole_boundary": event.hole_boundary, "unquantized_fraction": event.unquantized_fraction}
                                )
                fractions = sorted(all_fractions, key=Decimal)
                if any(left == right for left, right in zip(fractions, fractions[1:])):
                    raise ValueError("zero-length split interval")
                length_nm = int((parent["length_m"] * 1_000_000_000).to_integral_value())
                duration_ds = int((parent["duration_s"] * 10).to_integral_value())
                fractions, fraction_remap = coalesce_zero_length_intervals(fractions, length_nm)
                if fraction_remap:
                    remapped_events_at = defaultdict(list)
                    for (model, fraction), event_list in events_at.items():
                        remapped_events_at[(model, fraction_remap[fraction])].extend(event_list)
                    events_at = remapped_events_at
                point_ids_by_fraction = defaultdict(list)
                wgs_by_fraction = {
                    "0.000000000000": (parent["from_lon"], parent["from_lat"]),
                    "1.000000000000": (parent["to_lon"], parent["to_lat"]),
                }
                if line_2180 is not None:
                    for (model, fraction), event_list in sorted(events_at.items(), key=lambda item: (Decimal(item[0][1]), MODEL_ORDER.index(item[0][0]))):
                        point = line_2180.interpolate(float(fraction), normalized=True)
                        wgs = transform(to_4326.transform, point)
                        wgs_by_fraction[fraction] = (wgs.x, wgs.y)
                        identifier = split_point_id(parent["id"], fraction, raw_hash, MODEL_DEPTHS[model])
                        city_mask = sum(1 << int(event["bit_index"]) for event in event_list)
                        raw_fractions = json.dumps(sorted({event['unquantized_fraction'] for event in event_list}), separators=(",", ":"))
                        points.write(f"{identifier}\t{parent['id']}\t{raw_fractions}\t{fraction}\t{wgs.x:.9f}\t{wgs.y:.9f}\t{point.x:.3f}\t{point.y:.3f}\t{model}\t{city_mask}\t{event_json(event_list)}\t{raw_hash}\n")
                        point_ids_by_fraction[fraction].append(identifier)
                        point_records += 1
                        fraction_index = fractions.index(fraction)
                        incoming_child = split_edge_id(parent["id"], fraction_index - 1) if fraction_index > 0 else ""
                        outgoing_child = split_edge_id(parent["id"], fraction_index) if fraction_index + 1 < len(fractions) else ""
                        for event in event_list:
                            way, _, direction, _, _ = parent["id"].split("/")
                            gates.write(f"{model}\t{event['bit_index']}\t{event['city']}\t{event['category']}\t{identifier}\t{parent['id']}\t{incoming_child}\t{outgoing_child}\t{way}\t{fraction}\t{direction}\t{wgs.x:.9f}\t{wgs.y:.9f}\t{point.x:.3f}\t{point.y:.3f}\n")
                            city_model_gate_counts[(model, int(event["bit_index"]))][event["category"]] += 1
                            gate_records += 1
                allocated_lengths = allocate_integer_total(length_nm, fractions)
                allocated_durations = allocate_integer_total(duration_ds, fractions)
                if any(length <= 0 for length in allocated_lengths):
                    raise ValueError(f"non-positive child length on {parent['id']}")
                if sum(allocated_lengths) != length_nm or sum(allocated_durations) != duration_ds:
                    raise ValueError(f"per-parent cost preservation failed on {parent['id']}")
                parent_length_nm += length_nm
                parent_duration_ds += duration_ds
                for ordinal, (start, end) in enumerate(zip(fractions, fractions[1:])):
                    midpoint = (float(start) + float(end)) / 2
                    masks = {model: 0 for model in MODEL_ORDER}
                    if line_2180 is not None:
                        middle = line_2180.interpolate(midpoint, normalized=True)
                        for model in MODEL_ORDER:
                            for bit in bits:
                                if regions[model][bit].contains(middle):
                                    masks[model] |= 1 << bit
                    for model in MODEL_ORDER:
                        mask_counts[model][str(masks[model])] += 1
                    if masks["M-500"] & ~masks["M-300"] or masks["M-300"] & ~masks["M-boundary"]:
                        raise ValueError(f"visit-model bitmask nesting failed on {parent['id']}")
                    start_lon, start_lat = wgs_by_fraction[start]
                    end_lon, end_lat = wgs_by_fraction[end]
                    identifier = split_edge_id(parent["id"], ordinal)
                    references = sorted(set(point_ids_by_fraction[start] + point_ids_by_fraction[end]))
                    edges.write(
                        f"{identifier}\t{parent['id']}\t{ordinal}\t{start}\t{end}\t"
                        f"{start_lon:.9f}\t{start_lat:.9f}\t{end_lon:.9f}\t{end_lat:.9f}\t"
                        f"{allocated_lengths[ordinal] / 1_000_000_000:.9f}\t{allocated_durations[ordinal] / 10:.1f}\t"
                        f"{masks['M-boundary']}\t{masks['M-300']}\t{masks['M-500']}\t{','.join(references)}\n"
                    )
                    child_length_nm += allocated_lengths[ordinal]
                    child_duration_ds += allocated_durations[ordinal]
                    counts["split_edges"] += 1
                    if ordinal:
                        internal.write(f"{split_edge_id(parent['id'], ordinal - 1)}\t{identifier}\t0.0\tinternal_split_continuation\n")
                        internal_turn_count += 1
                if len(fractions) > 2:
                    split_last[parent["id"]] = len(fractions) - 2
                    counts["split_parents"] += 1
                if parent["id"] == start_snap["selected_stable_edge_id"]:
                    counts["start_child_ordinal"] = containing_interval(fractions, float(start_snap["fraction"]))
    if counts["parents"] != EXPECTED_EDGE_COUNT:
        raise ValueError(f"authoritative edge count mismatch: {counts['parents']}")
    if parent_length_nm != child_length_nm or parent_duration_ds != child_duration_ds:
        raise ValueError("child length or duration total differs from parents")
    if internal_turn_count != counts["split_edges"] - counts["parents"]:
        raise ValueError("internal split continuation count mismatch")
    for model in MODEL_ORDER:
        evaluated_total = 0
        for bit in city_names:
            key = (model, bit)
            evaluated = sum(city_model_counts[key].values())
            evaluated_total += evaluated
            city_model_counts[key]["fully_outside"] += EXPECTED_EDGE_COUNT - evaluated
        classification_counts[model]["fully_outside"] += EXPECTED_EDGE_COUNT * len(city_names) - evaluated_total
    if counts.get("start_child_ordinal") is None:
        raise ValueError("authoritative start edge was not processed")
    return {
        "counts": dict(counts), "split_last": split_last, "point_records": point_records,
        "gate_records": gate_records, "classification_counts": {model: dict(values) for model, values in classification_counts.items()},
        "city_model_counts": {f"{model}:{bit}": dict(values) for (model, bit), values in city_model_counts.items()},
        "city_model_gate_counts": {f"{model}:{bit}": dict(city_model_gate_counts[(model, bit)]) for model in MODEL_ORDER for bit in city_names},
        "mask_counts": {model: dict(values) for model, values in mask_counts.items()},
        "parent_length_nm": parent_length_nm, "child_length_nm": child_length_nm,
        "parent_duration_ds": parent_duration_ds, "child_duration_ds": child_duration_ds,
        "internal_turn_count": internal_turn_count,
    }


def build_turns(turns_path: Path, split_last: dict, output: Path) -> int:
    count = 0
    with ExitStack() as stack:
        target = open_zstd_writer(output / "inherited-turn-states.tsv.zst", stack)
        target.write("incoming_split_edge_id\toutgoing_split_edge_id\tturn_duration_s\ttransition_category\n")
        with turns_path.open(encoding="utf-8", newline="") as source:
            for row in csv.reader(source, delimiter="\t"):
                incoming, outgoing, duration, category = inherited_transition(row, split_last)
                target.write(f"{incoming}\t{outgoing}\t{duration}\t{category}\n")
                count += 1
    if count != EXPECTED_TURN_COUNT:
        raise ValueError(f"authoritative turn count mismatch: {count}")
    return count


def write_contract(repository: Path, output: Path, manifest: dict, prg_hash: str) -> None:
    contract = {
        "schema_version": 1,
        "contract_id": "pl18-stage2b-prg-road-graph-v1",
        "solver_execution": "forbidden",
        "frozen_inputs": {
            "manifest_v3": {"path": "instance/pl_18_capitals_static_instance_v3.yaml", "sha256": EXPECTED_MANIFEST_SHA256},
            "prg_raw": {"path": "results/prg/prg-18-raw.gml", "sha256": EXPECTED_PRG_RAW_SHA256},
            "prg_computational": {"path": "results/prg/prg-18-computational.gpkg", "sha256": prg_hash},
            "prg_source_manifest": {"path": "input/prg/source-manifest.json", "sha256": EXPECTED_PRG_SOURCE_MANIFEST_SHA256},
            "profile": {"path": "profiles/pl18-car.lua", "sha256": EXPECTED_PROFILE_SHA256},
            "osrm_commit": EXPECTED_OSRM_COMMIT,
            "stage2a_graph_manifest": {"path": "results/graph/graph-manifest.json", "sha256": EXPECTED_GRAPH_MANIFEST_SHA256},
            "stage2a_start_snap": {"path": "results/graph/start-snap.json", "sha256": EXPECTED_START_SNAP_SHA256},
            "stage2a_turn_restriction_certificate": {"path": "results/graph/turn-restriction-certificate.json", "sha256": EXPECTED_RESTRICTION_CERTIFICATE_SHA256},
        },
        "stage2a_authority": {
            "legal_directed_motorcar_edges": EXPECTED_EDGE_COUNT,
            "edge_based_turn_states": EXPECTED_TURN_COUNT,
            "directed_base_edges_sha256": EXPECTED_EDGE_SHA256,
            "turn_state_canonical_sha256": EXPECTED_TURN_SHA256,
        },
        "geometry": {"crs": "EPSG:2180", "engine": "GEOS", "version": EXPECTED_GEOS, "binding": "Shapely 2.1.2", "tolerance_m": 0.01, "buffer_join_style": "round", "quad_segs": 32, "holes_are_boundaries": True, "source_repair": "forbidden; use approved Stage 1 computational lineage unchanged"},
        "models": [{"model_id": model, "presentation_alias": alias, "depth_m": float(MODEL_DEPTHS[model])} for alias, model in MODEL_ALIASES.items()],
        "city_order": manifest["cities"]["canonical_bit_order"],
        "fraction": {"directed": True, "unquantized_representation": "IEEE-754 binary64 shortest round-trip decimal", "precision_decimal_places": 12, "rounding": "ROUND_HALF_UP"},
        "boundary_event_categories": ["fully_outside", "fully_inside", "entering", "exiting", "crossing", "multiple_crossings", "endpoint_on_boundary", "tangential_touch", "overlap_along_boundary", "crossing_polygon_hole_boundary", "degenerate_numerical_case"],
        "visit_semantics": {"bit_rule": "positive_length_inside_open_polygon_interior", "tangential_contact_counts": False, "boundary_overlap_counts": False},
        "stable_ids": {"split_point": "SHA256(base_directed_edge|fraction_1e-12|PRG_raw_SHA256|model_depth)", "split_edge": "base_directed_edge@ordered_split_interval", "turn_state": "incoming_split_edge->outgoing_split_edge"},
        "subdivision": {"scope": "union_of_all_model_boundary_events", "bitmasks": ["M-boundary", "M-300", "M-500"], "split_edge_geometry": "directed two-point WGS84 LineString represented by from/to coordinate columns", "length_precision_m": 0.000000001},
        "turn_semantics": {"internal_split_continuation_cost_s": 0.0, "inherited_stage2a_turn_cost_occurrences": 1, "start_states_in_ordinary_turns": False},
        "canonical_sorting": {"split_points": ["parent_edge_id", "fraction", "model_id"], "split_edges": ["parent_edge_id", "ordinal"], "turn_states": ["incoming_split_edge_id", "outgoing_split_edge_id"]},
        "cache_policy": {"immutable_stage2a_metadata_allowed": True, "stage2b_products_in_clean_rebuild": "forbidden"},
        "rebuild_policy": {"independent_builds": 2, "all_counts_hashes_certificates_must_match": True},
    }
    json_dump(output / "stage2b-contract.json", contract)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-edges", required=True, type=Path)
    parser.add_argument("--turn-states", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--artifact-output", required=True, type=Path)
    args = parser.parse_args()
    repository = Path(__file__).resolve().parent.parent
    args.output.mkdir(parents=True, exist_ok=True)
    args.artifact_output.mkdir(parents=True, exist_ok=True)
    if shapely.geos_version_string != EXPECTED_GEOS:
        raise ValueError(f"GEOS version must be {EXPECTED_GEOS}, got {shapely.geos_version_string}")
    verify_input(args.base_edges, EXPECTED_EDGE_SHA256, "directed base edges")
    verify_input(args.turn_states, EXPECTED_TURN_SHA256, "turn states")
    verify_input(repository / "profiles/pl18-car.lua", EXPECTED_PROFILE_SHA256, "profile")
    verify_input(repository / "results/prg/prg-18-raw.gml", EXPECTED_PRG_RAW_SHA256, "raw PRG")
    verify_input(repository / "input/prg/source-manifest.json", EXPECTED_PRG_SOURCE_MANIFEST_SHA256, "PRG source manifest")
    verify_input(repository / "results/graph/graph-manifest.json", EXPECTED_GRAPH_MANIFEST_SHA256, "Stage 2A graph manifest")
    verify_input(repository / "results/graph/start-snap.json", EXPECTED_START_SNAP_SHA256, "Stage 2A start snap")
    verify_input(repository / "results/graph/turn-restriction-certificate.json", EXPECTED_RESTRICTION_CERTIFICATE_SHA256, "Stage 2A restriction certificate")
    manifest = load_manifest(repository)
    shutil.copy2(repository / "schemas/stage2b-artifact-layout.json", args.output / "artifact-schemas.json")
    geometry_path = args.artifact_output / "geometry-models.tsv"
    regions, geometry_summary, prg_hash = load_regions(repository, manifest, geometry_path)
    write_contract(repository, args.output, manifest, prg_hash)
    start = json.loads((repository / "results/graph/start-snap.json").read_text(encoding="utf-8"))
    edge_result = build_edges(args.base_edges, regions, manifest["cities"]["canonical_bit_order"], EXPECTED_PRG_RAW_SHA256, args.artifact_output, start)
    inherited_count = build_turns(args.turn_states, edge_result["split_last"], args.artifact_output)
    artifact_hashes = {path.name: {"sha256": digest(path), "size_bytes": path.stat().st_size} for path in sorted(args.artifact_output.iterdir()) if path.is_file()}
    json_dump(args.output / "geometry-models-summary.json", {"status": "VISIT_MODELS_CERTIFIED", "regions": geometry_summary, "artifact": artifact_hashes[geometry_path.name]})
    json_dump(args.output / "split-points-summary.json", {"status": "ROAD_PRG_INTERSECTION_OK", "count": edge_result["point_records"], "classification_counts": edge_result["classification_counts"], "artifact": artifact_hashes["split-points.tsv.zst"]})
    json_dump(args.output / "split-edges-summary.json", {"status": "SPLIT_GRAPH_CERTIFIED", **edge_result["counts"], "artifact": artifact_hashes["split-edges.tsv.zst"]})
    json_dump(args.output / "turn-states-summary.json", {"status": "TURN_SEMANTICS_PRESERVED", "inherited_stage2a_turns": inherited_count, "internal_split_continuations": edge_result["internal_turn_count"], "artifacts": {name: artifact_hashes[name] for name in ("internal-turn-states.tsv.zst", "inherited-turn-states.tsv.zst")}})
    json_dump(args.output / "city-bitmask-summary.json", {"status": "CITY_BITMASK_CERTIFIED", "model_mask_counts": edge_result["mask_counts"], "rule": "positive_length_inside_open_polygon_interior", "membership_evaluation": "open-interior midpoint of every canonical interval", "tangential_or_boundary_only_bits_set": 0})
    json_dump(args.output / "gates-summary.json", {"status": "GATE_INVENTORY_COMPLETE", "source_parent_count": edge_result["counts"]["parents"], "city_count": 18, "model_count": 3, "edge_city_model_pair_count": EXPECTED_EDGE_COUNT * 18 * 3, "spatial_index_role": "bbox rejection only; rejected pairs are certified fully_outside", "gate_event_count": edge_result["gate_records"], "city_model_classification_counts": edge_result["city_model_counts"], "city_model_gate_counts": edge_result["city_model_gate_counts"], "artifact": artifact_hashes["gates.tsv.zst"]})
    json_dump(args.output / "topology-preservation.json", {"status": "TOPOLOGY_PRESERVED", "proof": "each parent directed edge is replaced by one directed child chain with identical endpoints; every adjacent child pair has one zero-cost internal continuation; every authoritative turn maps last incoming child to first outgoing child", "all_parents_represented": True, "parent_count": edge_result["counts"]["parents"], "split_edge_count": edge_result["counts"]["split_edges"], "split_parent_count": edge_result["counts"].get("split_parents", 0), "internal_continuation_count": edge_result["internal_turn_count"], "expected_internal_continuation_count": edge_result["counts"]["split_edges"] - edge_result["counts"]["parents"], "parent_endpoint_incidence_preserved": True, "weak_connected_components_preserved_by_constructive_bijection": True, "gaps": 0, "overlaps": 0, "zero_length_children": 0, "direction_preserved": True})
    json_dump(args.output / "cost-preservation.json", {"status": "COSTS_PRESERVED", "proof": "child edge deciseconds sum exactly to each parent and inherited turn duration occurs exactly once; internal continuations cost zero", "parent_length_nm": edge_result["parent_length_nm"], "child_length_nm": edge_result["child_length_nm"], "parent_duration_ds": edge_result["parent_duration_ds"], "child_duration_ds": edge_result["child_duration_ds"], "internal_turn_cost_s": 0.0, "residual_distribution": "largest_remainder_then_interval_ordinal", "maximum_parent_duration_error_ds": 0, "maximum_parent_length_error_nm": 0, "declared_length_tolerance_m": 0.000000001})
    json_dump(args.output / "inherited-turn-certificate.json", {"status": "TURN_SEMANTICS_PRESERVED", "authoritative_legal_turns": EXPECTED_TURN_COUNT, "inherited_legal_turns": inherited_count, "turn_duration_applied_exactly_once": True, "prohibited_turns_added": 0, "ordinary_start_states": 0})
    start_parent = start["selected_stable_edge_id"]
    ordinal = edge_result["counts"]["start_child_ordinal"]
    start_point = Point(start["snapped_epsg2180"]["x"], start["snapped_epsg2180"]["y"])
    start_masks = {
        model: sum(1 << bit for bit, region in regions[model].items() if region.contains(start_point))
        for model in MODEL_ORDER
    }
    if any((start_masks[model] & 1) == 0 for model in MODEL_ORDER):
        raise ValueError("authoritative start does not satisfy Warszawa visit condition in every model")
    json_dump(args.output / "start-state-certificate.json", {"status": "START_STATE_PRESERVED", "distance_m": start["distance_m"], "snap_fraction": start["fraction"], "snap_coordinate": start["snapped_wgs84"], "parent_edge_id": start_parent, "child_edge_id": split_edge_id(start_parent, ordinal), "initial_directions": start["available_legal_initial_directions"], "ordinary_turn_state_count_contribution": 0, "start_city_bit_index": 0, "model_initial_visited_masks": start_masks, "start_visit_condition_satisfied": {model: bool(mask & 1) for model, mask in start_masks.items()}})
    json_dump(args.output / "export-reproducibility.json", {"status": "STAGE2B_EXPORT_REPRODUCIBILITY", "result": "PENDING_SECOND_EXPORT", "artifacts": artifact_hashes})
    total_bytes = sum(value["size_bytes"] for value in artifact_hashes.values())
    json_dump(args.output / "cache-budget-certificate.json", {"status": "CACHE_BUDGET_OK", "canonical_artifact_bytes": total_bytes, "budget_bytes": 7_000_000_000, "within_budget": total_bytes <= 7_000_000_000})
    statuses = ["ROAD_PRG_INTERSECTION_OK", "VISIT_MODELS_CERTIFIED", "SPLIT_GRAPH_CERTIFIED", "GATE_INVENTORY_COMPLETE", "CITY_BITMASK_CERTIFIED", "TOPOLOGY_PRESERVED", "COSTS_PRESERVED", "TURN_SEMANTICS_PRESERVED", "START_STATE_PRESERVED", "SOLVER_NOT_STARTED"]
    stage_manifest = {"schema_version": 1, "statuses": statuses, "contract_sha256": digest(args.output / "stage2b-contract.json"), "artifact_hashes": artifact_hashes}
    json_dump(args.output / "stage2b-manifest.json", stage_manifest)
    report = "# Stage 2B validation\n\n" + "\n".join(f"- **{status}**" for status in statuses) + f"\n\nProcessed all {EXPECTED_EDGE_COUNT:,} authoritative directed edges and {EXPECTED_TURN_COUNT:,} authoritative legal turns. Rebuild reproducibility is not asserted until the dispatch-only clean audit passes.\n"
    (args.output / "stage2b-validation-report.md").write_text(report, encoding="utf-8")
    with (args.output / "SHA256SUMS").open("w", encoding="utf-8") as sums:
        for name in sorted(path.name for path in args.output.iterdir() if path.is_file() and path.name != "SHA256SUMS"):
            sums.write(f"{digest(args.output / name)}  {name}\n")
    print("ROAD_PRG_INTERSECTION_OK")
    print("SOLVER_NOT_STARTED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
