#!/usr/bin/env python3

import argparse
import csv
import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
from array import array
from pathlib import Path

import osmium
from pyproj import Transformer


def pair_key(from_node: int, to_node: int) -> str:
    return f"{from_node:020d}/{to_node:020d}"


def stable_id(way: int, ordinal: int, direction: int, from_node: int, to_node: int) -> str:
    return f"{way}/{ordinal}/{direction}/{from_node}/{to_node}"


def is_duplicate_osm_node_segment(row: dict[str, str]) -> bool:
    if row["from_node_id"] != row["to_node_id"]:
        return False
    coordinates_match = (
        float(row["from_longitude"]) == float(row["to_longitude"])
        and float(row["from_latitude"]) == float(row["to_latitude"])
    )
    if not coordinates_match or float(row["length_m"]) > 0.001:
        raise ValueError(
            "OSRM self-loop segment is not a duplicate OSM-node representation: "
            f"{row['edge_based_node_id']}/{row['geometry_segment_ordinal']}"
        )
    return True


def collapse_duplicate_osm_node_segments(reader):
    current_edge_based_node = None
    rows = []
    for row in reader:
        edge_based_node = row["edge_based_node_id"]
        if current_edge_based_node is not None and edge_based_node != current_edge_based_node:
            yield from collapse_edge_based_node_segments(rows)
            rows = []
        current_edge_based_node = edge_based_node
        rows.append(row)
    if rows:
        yield from collapse_edge_based_node_segments(rows)


def collapse_edge_based_node_segments(rows):
    collapsed = []
    pending_duration_ds = 0
    duplicate_count = 0
    duplicate_duration_ds = 0
    for row in rows:
        if is_duplicate_osm_node_segment(row):
            duration_ds = int(row["duration_ds"])
            if duration_ds < 0:
                raise ValueError("negative duration on duplicate OSM-node segment")
            duplicate_count += 1
            duplicate_duration_ds += duration_ds
            if collapsed:
                collapsed[-1]["duration_ds"] = str(
                    int(collapsed[-1]["duration_ds"]) + duration_ds
                )
            else:
                pending_duration_ds += duration_ds
            continue
        if pending_duration_ds:
            row["duration_ds"] = str(int(row["duration_ds"]) + pending_duration_ds)
            pending_duration_ds = 0
        collapsed.append(row)
    if not collapsed:
        raise ValueError(
            "edge-based node contains no real OSM segment: "
            f"{rows[0]['edge_based_node_id']}"
        )
    for row in collapsed:
        yield row, duplicate_count, duplicate_duration_ds
        duplicate_count = 0
        duplicate_duration_ds = 0


def load_manifest(repository_root: Path, manifest_path: Path) -> dict:
    result = subprocess.run(
        [
            "ruby",
            str(repository_root / "scripts/manifest_json.rb"),
            str(manifest_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def numeric_speed(value: str | None) -> float | None:
    if not value:
        return None
    normalized = value.strip().lower()
    if ";" in normalized:
        values = [numeric_speed(item) for item in normalized.split(";")]
        parsed = [item for item in values if item is not None]
        return min(parsed) if parsed else None
    try:
        if normalized.endswith("mph"):
            return float(normalized[:-3].strip()) * 1.609344
        return float(normalized)
    except ValueError:
        return None


def dimensional_limit(value: str | None, kind: str) -> float | None:
    if not value:
        return None
    normalized = value.strip().lower().replace(",", ".")
    try:
        if kind == "weight":
            if normalized.endswith("kg"):
                return float(normalized[:-2].strip())
            if normalized.endswith("t"):
                return float(normalized[:-1].strip()) * 1000
            return float(normalized) * 1000
        if normalized.endswith("ft"):
            return float(normalized[:-2].strip()) * 0.3048
        if normalized.endswith("m"):
            return float(normalized[:-1].strip())
        return float(normalized)
    except ValueError:
        return None


class CandidateExporter(osmium.SimpleHandler):
    def __init__(self, manifest: dict, needed_pairs: set[int], output: Path, restrictions: Path):
        super().__init__()
        self.manifest = manifest
        self.needed_pairs = needed_pairs
        self.output = output.open("w", encoding="utf-8", newline="")
        self.restrictions = restrictions.open("w", encoding="utf-8", newline="")
        self.restrictions.write(
            "relation_id\trestriction_type\tfrom_way_id\tvia_node_id\tto_way_id\n"
        )
        self.ways_read = 0
        self.legal_candidates = 0
        self.forbidden_ferry_edges = 0
        self.rejected_private_nonmotorcar_edges = 0
        self.enforced_restrictions = 0
        self.no_restrictions: set[tuple[int, int, int]] = set()
        self.only_restrictions: set[tuple[int, int, int]] = set()
        access = manifest["access_model"]
        self.allowed_access = set(access["allowed_values"])
        self.forbidden_access = set(access["forbidden_values"])
        self.access_hierarchy = access["hierarchy"]
        speed = manifest["speed_model"]
        self.class_speeds = speed["class_speed_kmh"]
        self.surface_caps = speed["surface_cap_kmh"]
        self.tracktype_caps = speed["tracktype_cap_kmh"]
        self.smoothness_caps = speed["smoothness_cap_kmh"]
        self.bridge_caps = speed["bridge_cap_kmh"]
        self.symbolic_speeds = {key.lower(): value for key, value in speed["symbolic_maxspeed_kmh"].items()}
        self.speed_reduction = speed["maxspeed_reduction_factor"]
        self.vehicle = manifest["vehicle"]

    def close(self) -> None:
        self.output.close()
        self.restrictions.close()

    def access_value(self, tags, direction: str) -> str | None:
        for key in self.access_hierarchy:
            directional = tags.get(f"{key}:{direction}")
            if directional:
                return directional
            value = tags.get(key)
            if value:
                return value
        return None

    def direction_is_allowed(self, tags, direction: str) -> tuple[bool, str]:
        value = self.access_value(tags, direction)
        if value is None:
            return True, "public_default"
        if value in self.allowed_access:
            return True, f"allowed:{value}"
        return False, f"forbidden:{value}"

    def dimensions_allow(self, tags, direction: str) -> bool:
        checks = (
            ("maxheight", "height_m", "length"),
            ("maxwidth", "width_m", "length"),
            ("maxlength", "length_m", "length"),
            ("maxweight", "weight_kg", "weight"),
        )
        for tag, vehicle_key, kind in checks:
            value = tags.get(f"{tag}:{direction}") or tags.get(tag)
            limit = dimensional_limit(value, kind)
            if limit is not None and limit < self.vehicle[vehicle_key]:
                return False
        return True

    def direction_flags(self, tags, highway: str) -> tuple[bool, bool]:
        oneway = None
        for namespace in ("motorcar", "motor_vehicle", "vehicle"):
            oneway = tags.get(f"{namespace}:oneway")
            if oneway:
                break
        oneway = oneway or tags.get("oneway")
        if oneway == "-1":
            return False, True
        if oneway in {"yes", "1", "true"}:
            return True, False
        if oneway != "no" and (
            highway == "motorway" or tags.get("junction") in {"roundabout", "circular"}
        ):
            return True, False
        if oneway in {"reversible", "alternating"}:
            return True, True
        return True, True

    def effective_speed(self, tags, highway: str, direction: str) -> float:
        caps = [float(self.class_speeds[highway])]
        maxspeed = tags.get(f"maxspeed:{direction}") or tags.get("maxspeed")
        parsed = numeric_speed(maxspeed)
        if parsed is None:
            symbolic = (tags.get("source:maxspeed") or "").lower()
            parsed = self.symbolic_speeds.get(symbolic)
        if parsed is not None:
            caps.append(float(parsed) * self.speed_reduction)
        for tag, table in (
            ("surface", self.surface_caps),
            ("tracktype", self.tracktype_caps),
            ("smoothness", self.smoothness_caps),
            ("bridge", self.bridge_caps),
        ):
            value = tags.get(tag)
            cap = table.get(value) if value else None
            if cap is not None:
                caps.append(float(cap))
        return min(caps)

    def way(self, way) -> None:
        self.ways_read += 1
        tags = way.tags
        route = tags.get("route")
        segment_count = max(0, len(way.nodes) - 1)
        if route in {"ferry", "shuttle_train"}:
            self.forbidden_ferry_edges += segment_count * 2
            return
        highway = tags.get("highway")
        if highway not in self.class_speeds:
            return
        if tags.get("area") == "yes" or tags.get("impassable") == "yes" or tags.get("status") == "impassable":
            return
        if tags.get("construction") not in {None, "", "no", "widening", "minor"}:
            return

        forward, backward = self.direction_flags(tags, highway)
        for direction, enabled, direction_bit in (
            ("forward", forward, 0),
            ("backward", backward, 1),
        ):
            if not enabled:
                continue
            access_allowed, access_decision = self.direction_is_allowed(tags, direction)
            dimensions_allowed = self.dimensions_allow(tags, direction)
            if not access_allowed or not dimensions_allowed:
                self.rejected_private_nonmotorcar_edges += segment_count
                continue
            speed = self.effective_speed(tags, highway, direction)
            if speed <= 0:
                self.rejected_private_nonmotorcar_edges += segment_count
                continue
            for ordinal in range(segment_count):
                if direction == "forward":
                    from_node = way.nodes[ordinal].ref
                    to_node = way.nodes[ordinal + 1].ref
                else:
                    from_node = way.nodes[ordinal + 1].ref
                    to_node = way.nodes[ordinal].ref
                encoded = (int(from_node) << 64) | int(to_node)
                if encoded not in self.needed_pairs:
                    continue
                self.output.write(
                    f"{pair_key(from_node, to_node)}\t{way.id}\t{ordinal}\t{direction_bit}"
                    f"\t{from_node}\t{to_node}\t{highway}\t{speed:.6f}"
                    f"\t{access_decision}\tnot_ferry\n"
                )
                self.legal_candidates += 1

    def relation(self, relation) -> None:
        tags = relation.tags
        if tags.get("type") != "restriction":
            return
        restriction = None
        for key in (
            "restriction:motorcar",
            "restriction:motor_vehicle",
            "restriction:vehicle",
            "restriction",
        ):
            restriction = tags.get(key)
            if restriction:
                break
        if not restriction or "conditional" in restriction:
            return
        from_ways = [member.ref for member in relation.members if member.role == "from" and member.type == "w"]
        via_nodes = [member.ref for member in relation.members if member.role == "via" and member.type == "n"]
        to_ways = [member.ref for member in relation.members if member.role == "to" and member.type == "w"]
        if len(from_ways) != 1 or len(via_nodes) != 1 or len(to_ways) != 1:
            return
        record = (int(from_ways[0]), int(via_nodes[0]), int(to_ways[0]))
        if restriction.startswith("no_"):
            self.no_restrictions.add(record)
        elif restriction.startswith("only_"):
            self.only_restrictions.add(record)
        else:
            return
        self.enforced_restrictions += 1
        self.restrictions.write(
            f"{relation.id}\t{restriction}\t{record[0]}\t{record[1]}\t{record[2]}\n"
        )


def sort_file(source: Path, destination: Path, keys: list[str], temporary: Path, unique: bool = False) -> None:
    command = ["sort", "-T", str(temporary), "-t", "\t", *keys]
    if unique:
        command.append("-u")
    command.append(str(source))
    with destination.open("wb") as output:
        subprocess.run(command, check=True, stdout=output, env={**os.environ, "LC_ALL": "C"})


def group_rows(reader):
    current_key = None
    rows = []
    for row in reader:
        key = row[0]
        if current_key is not None and key != current_key:
            yield current_key, rows
            rows = []
        current_key = key
        rows.append(row)
    if current_key is not None:
        yield current_key, rows


def parse_stable(identifier: str) -> tuple[int, int, int, int, int]:
    return tuple(int(value) for value in identifier.split("/"))


def resolve_segments(
    segment_sorted: Path,
    candidate_sorted: Path,
    resolved: Path,
    unmatched: Path,
) -> int:
    count = 0
    unmatched_keys = 0
    unmatched_segments = 0
    with segment_sorted.open(encoding="utf-8", newline="") as segment_source, candidate_sorted.open(
        encoding="utf-8", newline=""
    ) as candidate_source, resolved.open("w", encoding="utf-8", newline="") as output, unmatched.open(
        "w", encoding="utf-8", newline=""
    ) as diagnostics:
        diagnostics.write(
            "pair_key\tedge_based_node_id\tgeometry_segment_ordinal\tfrom_node_id"
            "\tto_node_id\tlength_m\tduration_ds\n"
        )
        segment_groups = group_rows(csv.reader(segment_source, delimiter="\t"))
        candidate_groups = group_rows(csv.reader(candidate_source, delimiter="\t"))
        candidate_key, candidates = next(candidate_groups, (None, None))
        for key, segments in segment_groups:
            while candidate_key is not None and candidate_key < key:
                candidate_key, candidates = next(candidate_groups, (None, None))
            if candidate_key != key:
                unmatched_keys += 1
                unmatched_segments += len(segments)
                for segment in segments:
                    diagnostics.write(
                        "\t".join(
                            [
                                key,
                                segment[1],
                                segment[2],
                                segment[3],
                                segment[4],
                                segment[9],
                                segment[10],
                            ]
                        )
                        + "\n"
                    )
                continue
            for segment in segments:
                (
                    _,
                    edge_based_node,
                    geometry_ordinal,
                    from_node,
                    to_node,
                    from_lon,
                    from_lat,
                    to_lon,
                    to_lat,
                    length_m,
                    duration_ds,
                ) = segment
                length = float(length_m)
                duration = int(duration_ds)

                def rank(candidate):
                    speed = float(candidate[7])
                    expected = round(length / (speed / 3.6) * 10)
                    return (
                        abs(expected - duration),
                        int(candidate[1]),
                        int(candidate[2]),
                        int(candidate[3]),
                    )

                candidate = min(candidates, key=rank)
                identifier = stable_id(
                    int(candidate[1]),
                    int(candidate[2]),
                    int(candidate[3]),
                    int(from_node),
                    int(to_node),
                )
                output.write(
                    "\t".join(
                        [
                            identifier,
                            edge_based_node,
                            geometry_ordinal,
                            from_node,
                            to_node,
                            from_lon,
                            from_lat,
                            to_lon,
                            to_lat,
                            length_m,
                            f"{duration / 10:.1f}",
                            candidate[6],
                            candidate[7],
                            candidate[8],
                            candidate[9],
                        ]
                    )
                    + "\n"
                )
                count += 1
    if unmatched_keys:
        raise ValueError(
            f"{unmatched_keys} OSRM node pairs ({unmatched_segments} segments) have no legal "
            f"OSM candidate; see {unmatched}"
        )
    unmatched.unlink()
    return count


def build_edge_arrays(ebn_sorted: Path):
    first = [array("Q") for _ in range(5)]
    last = [array("Q") for _ in range(5)]
    current_ebn = -1
    first_stable = None
    last_stable = None
    with ebn_sorted.open(encoding="utf-8", newline="") as source:
        for row in csv.reader(source, delimiter="\t"):
            ebn = int(row[1])
            parsed = parse_stable(row[0])
            if ebn != current_ebn:
                if current_ebn >= 0:
                    for values, value in zip(first, first_stable):
                        values.append(value)
                    for values, value in zip(last, last_stable):
                        values.append(value)
                if ebn != current_ebn + 1:
                    raise ValueError("edge-based node IDs are not contiguous")
                current_ebn = ebn
                first_stable = parsed
            last_stable = parsed
    if current_ebn >= 0:
        for values, value in zip(first, first_stable):
            values.append(value)
        for values, value in zip(last, last_stable):
            values.append(value)
    return first, last


def stable_from_arrays(values, index: int) -> str:
    return stable_id(*(column[index] for column in values))


def resolve_turns(
    turns_path: Path,
    output: Path,
    first,
    last,
    no_restrictions: set[tuple[int, int, int]],
    only_restrictions: set[tuple[int, int, int]],
) -> tuple[int, int]:
    count = 0
    violations = 0
    with turns_path.open(encoding="utf-8", newline="") as source, output.open(
        "w", encoding="utf-8", newline=""
    ) as target:
        reader = csv.DictReader(source, delimiter="\t")
        for row in reader:
            incoming = int(row["incoming_edge_based_node_id"])
            outgoing = int(row["outgoing_edge_based_node_id"])
            incoming_id = stable_from_arrays(last, incoming)
            outgoing_id = stable_from_arrays(first, outgoing)
            incoming_way = last[0][incoming]
            outgoing_way = first[0][outgoing]
            via = int(row["via_node_id"])
            if (incoming_way, via, outgoing_way) in no_restrictions:
                violations += 1
            allowed_only = {
                to_way
                for from_way, restriction_via, to_way in only_restrictions
                if from_way == incoming_way and restriction_via == via
            }
            if allowed_only and outgoing_way not in allowed_only:
                violations += 1
            target.write(
                f"{incoming_id}\t{outgoing_id}\t{int(row['turn_duration_ds']) / 10:.1f}"
                "\tallowed\n"
            )
            count += 1
    return count, violations


def snap_start(base_edges: Path, manifest: dict) -> dict:
    start = manifest["start"]["wgs84"]
    transformer = Transformer.from_crs("EPSG:4326", "EPSG:2180", always_xy=True)
    start_x, start_y = transformer.transform(start["longitude"], start["latitude"])
    best = None
    selected_physical = None
    available = []
    with base_edges.open(encoding="utf-8", newline="") as source:
        for row in csv.reader(source, delimiter="\t"):
            identifier = row[0]
            way, ordinal, direction, from_node, to_node = parse_stable(identifier)
            from_x, from_y = transformer.transform(float(row[5]), float(row[6]))
            to_x, to_y = transformer.transform(float(row[7]), float(row[8]))
            delta_x = to_x - from_x
            delta_y = to_y - from_y
            denominator = delta_x * delta_x + delta_y * delta_y
            fraction = 0.0 if denominator == 0 else (
                (start_x - from_x) * delta_x + (start_y - from_y) * delta_y
            ) / denominator
            fraction = max(0.0, min(1.0, fraction))
            snap_x = from_x + fraction * delta_x
            snap_y = from_y + fraction * delta_y
            distance = math.hypot(start_x - snap_x, start_y - snap_y)
            rank = (round(distance * 1000), way, ordinal, direction)
            if best is None or rank < best[0]:
                longitude, latitude = Transformer.from_crs(
                    "EPSG:2180", "EPSG:4326", always_xy=True
                ).transform(snap_x, snap_y)
                best = (
                    rank,
                    {
                        "distance_m": round(distance, 3),
                        "snapped_epsg2180": {"x": round(snap_x, 3), "y": round(snap_y, 3)},
                        "snapped_wgs84": {
                            "longitude": round(longitude, 9),
                            "latitude": round(latitude, 9),
                        },
                        "osm_way_id": way,
                        "segment_ordinal": ordinal,
                        "selected_stable_edge_id": identifier,
                        "fraction": round(fraction, 12),
                    },
                )
                selected_physical = (way, ordinal, frozenset((from_node, to_node)))
    if best is None:
        raise ValueError("no eligible segment for start snap")
    with base_edges.open(encoding="utf-8", newline="") as source:
        for row in csv.reader(source, delimiter="\t"):
            parsed = parse_stable(row[0])
            physical = (parsed[0], parsed[1], frozenset((parsed[3], parsed[4])))
            if physical == selected_physical:
                available.append({"stable_edge_id": row[0], "direction_bit": parsed[2]})
    result = best[1]
    result["input_wgs84"] = {
        "longitude": float(start["longitude"]),
        "latitude": float(start["latitude"]),
    }
    result["snap_crs"] = "EPSG:2180"
    result["max_distance_m"] = manifest["start"]["snap"]["max_distance_m"]
    result["available_legal_initial_directions"] = sorted(
        available, key=lambda item: (item["direction_bit"], item["stable_edge_id"])
    )
    result["zero_cost_initial_states"] = [
        {
            "stable_edge_id": item["stable_edge_id"],
            "direction_bit": item["direction_bit"],
            "initial_cost_s": 0.0,
        }
        for item in result["available_legal_initial_directions"]
    ]
    if result["distance_m"] > result["max_distance_m"]:
        raise ValueError("start snap exceeds manifest maximum")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--pbf", required=True, type=Path)
    parser.add_argument("--osrm-dump", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--temp", required=True, type=Path)
    args = parser.parse_args()
    repository_root = Path(__file__).resolve().parent.parent
    manifest = load_manifest(repository_root, args.manifest)
    args.output.mkdir(parents=True, exist_ok=True)
    args.temp.mkdir(parents=True, exist_ok=True)

    raw_segments = args.osrm_dump / "osrm-directed-segments.tsv"
    segment_keyed = args.temp / "segments-keyed.tsv"
    needed_pairs: set[int] = set()
    with raw_segments.open(encoding="utf-8", newline="") as source, segment_keyed.open(
        "w", encoding="utf-8", newline=""
    ) as target:
        reader = csv.DictReader(source, delimiter="\t")
        collapsed_duplicate_segments = 0
        collapsed_duplicate_duration_ds = 0
        for row, duplicate_count, duplicate_duration_ds in collapse_duplicate_osm_node_segments(
            reader
        ):
            collapsed_duplicate_segments += duplicate_count
            collapsed_duplicate_duration_ds += duplicate_duration_ds
            from_node = int(row["from_node_id"])
            to_node = int(row["to_node_id"])
            needed_pairs.add((from_node << 64) | to_node)
            target.write(
                "\t".join(
                    [
                        pair_key(from_node, to_node),
                        row["edge_based_node_id"],
                        row["geometry_segment_ordinal"],
                        row["from_node_id"],
                        row["to_node_id"],
                        row["from_longitude"],
                        row["from_latitude"],
                        row["to_longitude"],
                        row["to_latitude"],
                        row["length_m"],
                        row["duration_ds"],
                    ]
                )
                + "\n"
            )

    candidates = args.temp / "candidates.tsv"
    restrictions = args.output / "restrictions.tsv"
    handler = CandidateExporter(manifest, needed_pairs, candidates, restrictions)
    handler.apply_file(str(args.pbf), locations=False)
    handler.close()
    del needed_pairs

    segment_sorted = args.temp / "segments-sorted.tsv"
    candidate_sorted = args.temp / "candidates-sorted.tsv"
    sort_file(segment_keyed, segment_sorted, ["-k1,1"], args.temp)
    sort_file(candidates, candidate_sorted, ["-k1,1", "-k2,2n", "-k3,3n", "-k4,4n"], args.temp)
    resolved = args.temp / "resolved-segments.tsv"
    resolved_segment_count = resolve_segments(
        segment_sorted,
        candidate_sorted,
        resolved,
        args.output / "export-mismatches.tsv",
    )

    base_edges = args.output / "directed-base-edges.tsv"
    sort_file(resolved, base_edges, ["-k1,1"], args.temp, unique=True)
    ebn_sorted = args.temp / "ebn-segments.tsv"
    sort_file(resolved, ebn_sorted, ["-k2,2n", "-k3,3n"], args.temp)
    first, last = build_edge_arrays(ebn_sorted)

    turns_unsorted = args.temp / "turn-states.tsv"
    turn_count, prohibited_violations = resolve_turns(
        args.osrm_dump / "osrm-turn-states.tsv",
        turns_unsorted,
        first,
        last,
        handler.no_restrictions,
        handler.only_restrictions,
    )
    turn_states = args.output / "edge-based-turn-states.tsv"
    sort_file(turns_unsorted, turn_states, ["-k1,1", "-k2,2"], args.temp, unique=True)
    if prohibited_violations:
        raise ValueError(f"{prohibited_violations} prohibited turns appear in exported graph")

    start_snap = snap_start(base_edges, manifest)
    (args.output / "start-snap.json").write_text(
        json.dumps(start_snap, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    summary = {
        "osm_ways_read": handler.ways_read,
        "legal_candidate_segments": handler.legal_candidates,
        "resolved_osrm_geometry_segments": resolved_segment_count,
        "collapsed_duplicate_osrm_node_segments": collapsed_duplicate_segments,
        "collapsed_duplicate_osrm_node_duration_ds": collapsed_duplicate_duration_ds,
        "legal_directed_motorcar_edges": sum(1 for _ in base_edges.open(encoding="utf-8")),
        "edge_based_turn_states": sum(1 for _ in turn_states.open(encoding="utf-8")),
        "enforced_turn_restrictions": handler.enforced_restrictions,
        "forbidden_ferry_edges": handler.forbidden_ferry_edges,
        "rejected_private_nonmotorcar_edges": handler.rejected_private_nonmotorcar_edges,
        "prohibited_turn_violations": prohibited_violations,
        "osrm_turn_records_before_deduplication": turn_count,
    }
    (args.output / "export-summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
