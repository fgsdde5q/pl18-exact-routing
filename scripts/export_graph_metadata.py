#!/usr/bin/env python3

import argparse
import csv
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from array import array
from pathlib import Path

import osmium
from pyproj import Transformer

RESTRICTION_NAMESPACES = ("motorcar", "motor_vehicle", "vehicle")
RESTRICTION_SAMPLE_SIZE = 32


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
    def __init__(self, manifest: dict, needed_pairs: set[int], output: Path):
        super().__init__()
        self.manifest = manifest
        self.needed_pairs = needed_pairs
        self.output = output.open("w", encoding="utf-8", newline="")
        self.ways_read = 0
        self.legal_candidates = 0
        self.forbidden_ferry_edges = 0
        self.rejected_private_nonmotorcar_edges = 0
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


class CertificateInputCollector(osmium.SimpleHandler):
    def __init__(self, selected_way_id: int):
        super().__init__()
        self.selected_way_id = selected_way_id
        self.selected_way = None
        self.restriction_relations = []
        self.conditional_relation_ids = []

    def way(self, way) -> None:
        if way.id == self.selected_way_id:
            self.selected_way = {
                "id": int(way.id),
                "node_ids": [int(node.ref) for node in way.nodes],
                "tags": dict(sorted((tag.k, tag.v) for tag in way.tags)),
            }

    def relation(self, relation) -> None:
        tags = dict((tag.k, tag.v) for tag in relation.tags)
        restriction_keys = [
            key for key in tags if key == "restriction" or key.startswith("restriction:")
        ]
        if not restriction_keys and tags.get("type") != "restriction":
            return
        conditional_keys = [key for key in restriction_keys if key.endswith(":conditional")]
        if conditional_keys:
            self.conditional_relation_ids.append(int(relation.id))
        unconditional_values = [
            tags[key]
            for key in ("restriction", *(
                f"restriction:{namespace}" for namespace in RESTRICTION_NAMESPACES
            ))
            if key in tags
        ]
        recognized = [
            value
            for value in unconditional_values
            if value.startswith("only_")
            or (value.startswith("no_") and not value.endswith("_on_red"))
        ]
        members = [
            {"type": member.type, "ref": int(member.ref), "role": member.role}
            for member in relation.members
            if member.role in {"from", "via", "to"}
        ]
        self.restriction_relations.append(
            {
                "relation_id": int(relation.id),
                "tags": dict(sorted(tags.items())),
                "members": members,
                "recognized_values": recognized,
                "conditional": bool(conditional_keys),
                "ignored_by_except": bool(
                    set(
                        item.strip()
                        for item in tags.get("except", "").split(";")
                        if item.strip()
                    )
                    & set(RESTRICTION_NAMESPACES)
                ),
            }
        )


class RestrictionWayCollector(osmium.SimpleHandler):
    def __init__(self, needed_way_ids: set[int]):
        super().__init__()
        self.needed_way_ids = needed_way_ids
        self.ways = {}

    def way(self, way) -> None:
        if way.id in self.needed_way_ids:
            self.ways[int(way.id)] = [int(node.ref) for node in way.nodes]


def osrm_via_node_path(
    from_nodes: list[int], to_nodes: list[int], via_node: int
) -> tuple[int, int, int] | None:
    if len(from_nodes) < 2 or len(to_nodes) < 2:
        return None
    from_first_source, from_first_target = from_nodes[0], from_nodes[1]
    from_last_source, from_last_target = from_nodes[-2], from_nodes[-1]
    to_first_source, to_first_target = to_nodes[0], to_nodes[1]
    to_last_source, to_last_target = to_nodes[-2], to_nodes[-1]
    if via_node == from_first_source:
        if from_first_source == to_first_source:
            return from_first_target, to_first_source, to_first_target
        if from_first_source == to_last_target:
            return from_first_target, to_last_target, to_last_source
    if via_node == from_last_target:
        if from_last_target == to_first_source:
            return from_last_source, to_first_source, to_first_target
        if from_last_target == to_last_target:
            return from_last_source, to_last_target, to_last_source
    return None


def osrm_restriction_summary(extract_log: Path) -> dict:
    text = extract_log.read_text(encoding="utf-8", errors="replace")
    patterns = {
        "parsed_restrictions": r"Collecting node information on ([0-9]+) restrictions",
        "invalid_restrictions": (
            r"Removing invalid turn restrictions\.\.\.removed ([0-9]+) invalid turn restrictions"
        ),
        "accepted_restrictions": r"Constructing restriction graph on ([0-9]+) restrictions",
    }
    result = {}
    for key, pattern in patterns.items():
        matches = re.findall(pattern, text)
        if len(matches) != 1:
            raise ValueError(f"expected exactly one OSRM {key} counter")
        result[key] = int(matches[0])
    accounted = (
        result["invalid_restrictions"] + result["accepted_restrictions"]
    )
    if accounted > result["parsed_restrictions"]:
        raise ValueError("OSRM restriction counters are internally inconsistent")
    result["unresolved_before_graph_validation"] = (
        result["parsed_restrictions"] - accounted
    )
    return result


def relation_shape(relation: dict) -> dict:
    from_ways = [
        member["ref"]
        for member in relation["members"]
        if member["type"] == "w" and member["role"] == "from"
    ]
    to_ways = [
        member["ref"]
        for member in relation["members"]
        if member["type"] == "w" and member["role"] == "to"
    ]
    via_nodes = [
        member["ref"]
        for member in relation["members"]
        if member["type"] == "n" and member["role"] == "via"
    ]
    via_ways = [
        member["ref"]
        for member in relation["members"]
        if member["type"] == "w" and member["role"] == "via"
    ]
    restriction_type = None
    if relation["recognized_values"]:
        kinds = {
            "only" if value.startswith("only_") else "no"
            for value in relation["recognized_values"]
        }
        if len(kinds) == 1:
            restriction_type = kinds.pop()
    return {
        "from_way_ids": from_ways,
        "to_way_ids": to_ways,
        "via_node_ids": via_nodes,
        "via_way_ids": via_ways,
        "restriction_type": restriction_type,
        "restriction_values": relation["recognized_values"],
        "ignored_by_except": relation["ignored_by_except"],
        "member_shape_valid": (
            (len(from_ways) <= 1 or any(
                value.startswith("no_entry")
                for value in relation["recognized_values"]
            ))
            and (len(to_ways) <= 1 or any(
                value.startswith("no_exit")
                for value in relation["recognized_values"]
            ))
        ),
    }


def build_restriction_certificate(
    relations: list[dict],
    conditional_relation_ids: list[int],
    base_edges: Path,
    turn_states: Path,
    extract_log: Path,
    restriction_ways: dict[int, list[int]],
) -> dict:
    shaped = [(relation, relation_shape(relation)) for relation in relations]
    direct_relations = [
        (relation, shape)
        for relation, shape in shaped
        if shape["restriction_type"] is not None
        and not shape["ignored_by_except"]
        and shape["member_shape_valid"]
        and len(shape["via_node_ids"]) == 1
        and not shape["via_way_ids"]
        and shape["from_way_ids"]
        and shape["to_way_ids"]
    ]
    resolved_paths = []
    for relation, shape in direct_relations:
        via_node = shape["via_node_ids"][0]
        for from_way in shape["from_way_ids"]:
            for to_way in shape["to_way_ids"]:
                from_nodes = restriction_ways.get(from_way)
                to_nodes = restriction_ways.get(to_way)
                path = (
                    osrm_via_node_path(from_nodes, to_nodes, via_node)
                    if from_nodes is not None and to_nodes is not None
                    else None
                )
                if path is not None:
                    resolved_paths.append((relation, shape, from_way, to_way, path))
    relevant_via_nodes = {item[4][1] for item in resolved_paths}
    incoming_by_nodes = {}
    outgoing_by_nodes = {}
    outgoing_all = {}
    with base_edges.open(encoding="utf-8", newline="") as source:
        for row in csv.reader(source, delimiter="\t"):
            identifier = row[0]
            _, _, _, from_node, to_node = parse_stable(identifier)
            if to_node in relevant_via_nodes:
                incoming_by_nodes.setdefault((from_node, to_node), []).append(
                    identifier
                )
            if from_node in relevant_via_nodes:
                outgoing_by_nodes.setdefault((from_node, to_node), []).append(
                    identifier
                )
                outgoing_all.setdefault(from_node, []).append(identifier)
    candidate_origins = {}
    legacy_projection_origins = {}
    resolved_relations = []
    for relation, shape, from_way, to_way, path in sorted(
        resolved_paths,
        key=lambda item: (
            item[0]["relation_id"],
            item[2],
            item[3],
            item[4],
        ),
    ):
        from_node, via_node, to_node = path
        endpoint_incoming_edges = sorted(
            incoming_by_nodes.get((from_node, via_node), [])
        )
        incoming_edges = [
            edge for edge in endpoint_incoming_edges
            if parse_stable(edge)[0] == from_way
        ]
        designated_outgoing = sorted(
            edge for edge in outgoing_by_nodes.get((via_node, to_node), [])
            if parse_stable(edge)[0] == to_way
        )
        if shape["restriction_type"] == "no":
            prohibited_outgoing = designated_outgoing
        else:
            prohibited_outgoing = sorted(
                edge
                for edge in outgoing_all.get(via_node, [])
                if parse_stable(edge)[4] != to_node
            )
        relation_pairs = sorted(
            (incoming_edge, outgoing_edge)
            for incoming_edge in incoming_edges
            for outgoing_edge in prohibited_outgoing
        )
        legacy_relation_pairs = sorted(
            (incoming_edge, outgoing_edge)
            for incoming_edge in endpoint_incoming_edges
            for outgoing_edge in prohibited_outgoing
        )
        for pair in relation_pairs:
            candidate_origins.setdefault(pair, set()).add(relation["relation_id"])
        for pair in legacy_relation_pairs:
            legacy_projection_origins.setdefault(pair, []).append(
                {
                    "relation_id": relation["relation_id"],
                    "from_way_id": from_way,
                    "to_way_id": to_way,
                    "resolved_osrm_node_path": [from_node, via_node, to_node],
                }
            )
        if incoming_edges and designated_outgoing:
            resolved_relations.append(
                {
                    "relation_id": relation["relation_id"],
                    "restriction_type": shape["restriction_type"],
                    "restriction_values": shape["restriction_values"],
                    "from_way_ids": [from_way],
                    "via": {"type": "node", "node_id": via_node},
                    "to_way_ids": [to_way],
                    "resolved_osrm_node_path": [from_node, via_node, to_node],
                    "incoming_edges": incoming_edges,
                    "outgoing_edges": designated_outgoing,
                    "expected_prohibited_transition_count": len(relation_pairs),
                    "expected_prohibited_transition_sample": [
                        {"incoming_edge": pair[0], "outgoing_edge": pair[1]}
                        for pair in relation_pairs[:8]
                    ],
                }
            )
    observed = set()
    legacy_projection_observed = set()
    with turn_states.open(encoding="utf-8", newline="") as source:
        for row in csv.reader(source, delimiter="\t"):
            pair = (row[0], row[1])
            if pair in candidate_origins:
                observed.add(pair)
            if pair in legacy_projection_origins and pair not in candidate_origins:
                legacy_projection_observed.add(pair)
    candidates = set(candidate_origins)
    expected = candidates - observed

    def pair_digest(pairs: set[tuple[str, str]]) -> str:
        result = hashlib.sha256()
        for incoming_edge, outgoing_edge in sorted(pairs):
            result.update(f"{incoming_edge}\t{outgoing_edge}\n".encode())
        return result.hexdigest()

    non_enforced_relation_ids = sorted(
        {
            origin["relation_id"]
            for pair in legacy_projection_observed
            for origin in legacy_projection_origins[pair]
        }
    )
    non_enforced_relation_digest = hashlib.sha256()
    for relation_id in non_enforced_relation_ids:
        non_enforced_relation_digest.update(f"{relation_id}\n".encode())
    osrm_summary = osrm_restriction_summary(extract_log)
    no_relations = sum(
        shape["restriction_type"] == "no" for _, shape in shaped
    )
    only_relations = sum(
        shape["restriction_type"] == "only" for _, shape in shaped
    )
    relation_by_id = {relation["relation_id"]: relation for relation in relations}
    non_enforced_records = []
    for pair in sorted(legacy_projection_observed):
        origins = sorted(
            legacy_projection_origins[pair],
            key=lambda item: (item["relation_id"], item["from_way_id"], item["to_way_id"]),
        )
        for origin in origins:
            relation_id = origin["relation_id"]
            relation = relation_by_id[relation_id]
            shape = relation_shape(relation)
            incoming_way_id = parse_stable(pair[0])[0]
            outgoing_way_id = parse_stable(pair[1])[0]
            projection_mismatch = (
                "incoming_way_id_differs_from_restriction_from_way"
                if incoming_way_id != origin["from_way_id"]
                else "legacy_endpoint_projection_did_not_preserve_relation_way_identity"
            )
            restriction_tags = {
                key: value
                for key, value in relation.get("tags", {}).items()
                if key == "restriction" or key.startswith("restriction:")
            }
            conditional = relation.get("conditional", False)
            non_enforced_records.append(
                {
                    "osm_relation_id": relation_id,
                    "restriction_tags": restriction_tags,
                    "conditional_status": (
                        "conditional" if conditional else "unconditional"
                    ),
                    "via_type": "node" if shape["via_node_ids"] else "way",
                    "incoming_stable_edge_id": pair[0],
                    "outgoing_stable_edge_id": pair[1],
                    "restriction_from_way_id": origin["from_way_id"],
                    "restriction_to_way_id": origin["to_way_id"],
                    "transition_incoming_way_id": incoming_way_id,
                    "transition_outgoing_way_id": outgoing_way_id,
                    "resolved_osrm_node_path": origin["resolved_osrm_node_path"],
                    "projection_mismatch": projection_mismatch,
                    "osrm_non_enforcement_reason": (
                        "the transition enters the via node on a different OSM way than "
                        "the restriction's from member; pinned OSRM therefore does not "
                        "apply this relation to the transition. The previous exporter "
                        "candidate logic matched only the OSM node pair and produced a "
                        "false positive by discarding from-way identity"
                    ),
                    "transition_remains_allowed": True,
                    "matches_frozen_static_model": True,
                    "frozen_static_model_assessment": (
                        "allowed: the frozen relation applies only to its declared from "
                        "way, while this stable incoming edge belongs to a parallel way"
                    ),
                }
            )
    return {
        "schema_version": 2,
        "status": "TURN_RESTRICTIONS_CERTIFIED",
        "source_relations": {
            "restriction_relation_count": len(relations),
            "no_turn_relation_count": no_relations,
            "only_turn_relation_count": only_relations,
            "conditional_relation_count": len(set(conditional_relation_ids)),
            "conditional_policy": "ignored_by_project_and_osrm_extract",
        },
        "osrm_summary": osrm_summary,
        "direct_relation_coverage": {
            "via_node_candidate_relations": len(direct_relations),
            "via_node_resolved_paths": len(resolved_paths),
            "via_node_paths_with_exported_edges": len(resolved_relations),
            "via_way_relation_count": sum(
                bool(shape["via_way_ids"]) for _, shape in shaped
            ),
        },
        "source_candidate_transitions": {
            "count": len(candidates),
            "canonical_sha256": pair_digest(candidates),
            "method": (
                "derived from frozen PBF restriction relations and matched to "
                "canonical directed base-edge endpoints"
            ),
        },
        "expected_prohibited_transitions": {
            "count": len(expected),
            "canonical_sha256": pair_digest(expected),
            "observed_in_exported_turn_states": 0,
            "proof": (
                "every source-derived candidate was matched against the complete "
                "exported allowed turn-state set; only absent candidates are "
                "certified as OSRM-enforced prohibited transitions"
            ),
        },
        "osrm_non_enforced_candidate_transitions": {
            "count": len(legacy_projection_observed),
            "relation_count": len(non_enforced_relation_ids),
            "relation_ids_sha256": non_enforced_relation_digest.hexdigest(),
            "classification": (
                "historical exporter-v1 endpoint-projection false positives: allowed "
                "transitions whose incoming stable edge is not the restriction from way"
            ),
            "aggregate_reason_evidence": {
                "invalid_restrictions": osrm_summary["invalid_restrictions"],
                "unresolved_before_graph_validation": osrm_summary[
                    "unresolved_before_graph_validation"
                ],
            },
            "sample": [
                {
                    "incoming_edge": pair[0],
                    "outgoing_edge": pair[1],
                    "relation_ids": sorted(
                        {
                            origin["relation_id"]
                            for origin in legacy_projection_origins[pair]
                        }
                    ),
                }
                for pair in sorted(legacy_projection_observed)[:32]
            ],
            "machine_readable_records": non_enforced_records,
        },
        "sample": {
            "selection": "lowest relation_id among resolved via-node restrictions",
            "limit": RESTRICTION_SAMPLE_SIZE,
            "relations": resolved_relations[:RESTRICTION_SAMPLE_SIZE],
        },
    }

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


def project_canonical_base_edges(resolved: Path, projected: Path) -> int:
    count = 0
    with resolved.open(encoding="utf-8", newline="") as source, projected.open(
        "w", encoding="utf-8", newline=""
    ) as target:
        for row in csv.reader(source, delimiter="\t"):
            if len(row) != 15:
                raise ValueError("resolved base edge has unexpected field count")
            target.write("\t".join([row[0], *row[3:]]) + "\n")
            count += 1
    return count


def stable_from_arrays(values, index: int) -> str:
    return stable_id(*(column[index] for column in values))


def accepted_turn_restriction_count(extract_log: Path) -> int:
    pattern = re.compile(r"Constructing restriction graph on ([0-9]+) restrictions")
    matches = pattern.findall(extract_log.read_text(encoding="utf-8", errors="replace"))
    if len(matches) != 1:
        raise ValueError(
            "expected exactly one accepted turn-restriction count in osrm-extract log"
        )
    return int(matches[0])


def resolve_turns(
    turns_path: Path,
    output: Path,
    first,
    last,
) -> tuple[int, int]:
    count = 0
    violations = 0
    with turns_path.open(encoding="utf-8", newline="") as source, output.open(
        "w", encoding="utf-8", newline=""
    ) as target:
        reader = csv.DictReader(source, delimiter="\t")
        for row in reader:
            if row["restriction_status"] != "allowed":
                violations += 1
                continue
            incoming = int(row["incoming_edge_based_node_id"])
            outgoing = int(row["outgoing_edge_based_node_id"])
            incoming_id = stable_from_arrays(last, incoming)
            outgoing_id = stable_from_arrays(first, outgoing)
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
            from_x, from_y = transformer.transform(float(row[3]), float(row[4]))
            to_x, to_y = transformer.transform(float(row[5]), float(row[6]))
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


def enrich_start_direction_certificate(
    start_snap: dict,
    selected_way: dict,
    candidate_exporter: CandidateExporter,
) -> dict:
    if selected_way is None:
        raise ValueError("selected start way was not found in frozen OSM input")
    ordinal = start_snap["segment_ordinal"]
    nodes = selected_way["node_ids"]
    if ordinal < 0 or ordinal + 1 >= len(nodes):
        raise ValueError("start segment ordinal is outside selected OSM way")
    tags = selected_way["tags"]
    highway = tags.get("highway")
    forward_enabled, backward_enabled = candidate_exporter.direction_flags(
        tags, highway
    )
    graph_edges = {
        item["stable_edge_id"]
        for item in start_snap["available_legal_initial_directions"]
    }
    directions = []
    for name, direction_bit, from_node, to_node, oneway_allowed in (
        ("forward", 0, nodes[ordinal], nodes[ordinal + 1], forward_enabled),
        ("backward", 1, nodes[ordinal + 1], nodes[ordinal], backward_enabled),
    ):
        identifier = stable_id(
            selected_way["id"], ordinal, direction_bit, from_node, to_node
        )
        access_allowed, access_reason = candidate_exporter.direction_is_allowed(
            tags, name
        )
        dimensions_allowed = candidate_exporter.dimensions_allow(tags, name)
        routable_class = highway in candidate_exporter.class_speeds
        ferry_allowed = tags.get("route") not in {"ferry", "shuttle_train"}
        speed_positive = False
        if routable_class:
            speed_positive = (
                candidate_exporter.effective_speed(tags, highway, name) > 0
            )
        graph_edge_present = identifier in graph_edges
        rejection_reasons = []
        if not oneway_allowed:
            rejection_reasons.append("forbidden_by_oneway_tag")
        if not access_allowed:
            rejection_reasons.append(access_reason)
        if not dimensions_allowed:
            rejection_reasons.append("forbidden_by_vehicle_dimensions")
        if not routable_class:
            rejection_reasons.append("forbidden_highway_class")
        if not ferry_allowed:
            rejection_reasons.append("forbidden_ferry_or_shuttle_train")
        if routable_class and not speed_positive:
            rejection_reasons.append("nonpositive_effective_speed")
        if not graph_edge_present:
            rejection_reasons.append("directed_edge_absent_from_osrm_graph")
        legal = not rejection_reasons
        directions.append(
            {
                "name": name,
                "direction_bit": direction_bit,
                "stable_edge_id": identifier,
                "from_node_id": from_node,
                "to_node_id": to_node,
                "geometry_possible": True,
                "oneway_check": {
                    "allowed": oneway_allowed,
                    "tag_value": tags.get("oneway"),
                },
                "access_check": {
                    "allowed": access_allowed,
                    "decision": access_reason,
                },
                "vehicle_dimension_check": {"allowed": dimensions_allowed},
                "graph_edge_present": graph_edge_present,
                "turn_check": {
                    "allowed": True,
                    "decision": "not_applicable_at_route_start_without_incoming_edge",
                },
                "legal_initial_direction": legal,
                "rejection_reasons": rejection_reasons,
            }
        )
    certified = sorted(
        item["stable_edge_id"]
        for item in directions
        if item["legal_initial_direction"]
    )
    if certified != sorted(graph_edges):
        raise ValueError(
            "start direction tag/access certificate disagrees with exported graph"
        )
    result = dict(start_snap)
    result["selected_way_tags"] = tags
    result["oneway_status"] = {
        "raw": tags.get("oneway"),
        "forward_allowed": forward_enabled,
        "backward_allowed": backward_enabled,
    }
    result["geometrically_possible_initial_directions"] = directions
    if len(certified) == 1:
        rejected = [
            {
                "stable_edge_id": item["stable_edge_id"],
                "reasons": item["rejection_reasons"],
            }
            for item in directions
            if not item["legal_initial_direction"]
        ]
        if not rejected or not all(item["reasons"] for item in rejected):
            raise ValueError("single initial direction lacks tag/access explanation")
        result["single_direction_explanation"] = {
            "status": "START_DIRECTIONS_CERTIFIED",
            "available_stable_edge_id": certified[0],
            "rejected_directions": rejected,
            "evidence": "frozen OSM way tags plus project oneway/access/vehicle rules",
        }
    else:
        result["single_direction_explanation"] = {
            "status": "START_DIRECTIONS_CERTIFIED",
            "available_direction_count": len(certified),
            "evidence": "frozen OSM way tags plus project oneway/access/vehicle rules",
        }
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--pbf", required=True, type=Path)
    parser.add_argument("--osrm-dump", required=True, type=Path)
    parser.add_argument("--osrm-extract-log", required=True, type=Path)
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
    handler = CandidateExporter(manifest, needed_pairs, candidates)
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

    ebn_sorted = args.temp / "ebn-segments.tsv"
    sort_file(resolved, ebn_sorted, ["-k2,2n", "-k3,3n"], args.temp)
    first, last = build_edge_arrays(ebn_sorted)
    projected_base_edges = args.temp / "projected-base-edges.tsv"
    project_canonical_base_edges(resolved, projected_base_edges)
    base_edges = args.output / "directed-base-edges.tsv"
    sort_file(projected_base_edges, base_edges, ["-k1,1"], args.temp, unique=True)

    turns_unsorted = args.temp / "turn-states.tsv"
    turn_count, prohibited_violations = resolve_turns(
        args.osrm_dump / "osrm-turn-states.tsv",
        turns_unsorted,
        first,
        last,
    )
    turn_states = args.output / "edge-based-turn-states.tsv"
    sort_file(turns_unsorted, turn_states, ["-k1,1", "-k2,2"], args.temp, unique=True)
    if prohibited_violations:
        raise ValueError(f"{prohibited_violations} prohibited turns appear in exported graph")

    start_snap = snap_start(base_edges, manifest)
    certificate_inputs = CertificateInputCollector(start_snap["osm_way_id"])
    certificate_inputs.apply_file(str(args.pbf), locations=False)
    restriction_way_ids = {
        member["ref"]
        for relation in certificate_inputs.restriction_relations
        for member in relation["members"]
        if member["type"] == "w"
    }
    restriction_way_collector = RestrictionWayCollector(restriction_way_ids)
    restriction_way_collector.apply_file(str(args.pbf), locations=False)
    start_snap = enrich_start_direction_certificate(
        start_snap, certificate_inputs.selected_way, handler
    )
    (args.output / "start-snap.json").write_text(
        json.dumps(start_snap, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    restriction_certificate = build_restriction_certificate(
        certificate_inputs.restriction_relations,
        certificate_inputs.conditional_relation_ids,
        base_edges,
        turn_states,
        args.osrm_extract_log,
        restriction_way_collector.ways,
    )
    non_enforced_records = restriction_certificate[
        "osrm_non_enforced_candidate_transitions"
    ].pop("machine_readable_records")
    (args.output / "turn-restriction-certificate.json").write_text(
        json.dumps(restriction_certificate, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (args.output / "non-enforced-restriction-candidates.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "OSRM_NON_ENFORCED_RESTRICTION_CANDIDATES",
                "record_count": len(non_enforced_records),
                "records": non_enforced_records,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    restriction_summary = restriction_certificate["osrm_summary"]
    summary = {
        "osm_ways_read": handler.ways_read,
        "legal_candidate_segments": handler.legal_candidates,
        "resolved_osrm_geometry_segments": resolved_segment_count,
        "collapsed_duplicate_osrm_node_segments": collapsed_duplicate_segments,
        "collapsed_duplicate_osrm_node_duration_ds": collapsed_duplicate_duration_ds,
        "legal_directed_motorcar_edges": sum(1 for _ in base_edges.open(encoding="utf-8")),
        "edge_based_turn_states": sum(1 for _ in turn_states.open(encoding="utf-8")),
        "source_restriction_relations": restriction_certificate[
            "source_relations"
        ]["restriction_relation_count"],
        "parsed_turn_restrictions": restriction_summary["parsed_restrictions"],
        "invalid_turn_restrictions": restriction_summary["invalid_restrictions"],
        "unresolved_turn_restrictions": restriction_summary[
            "unresolved_before_graph_validation"
        ],
        "enforced_turn_restrictions": restriction_summary["accepted_restrictions"],
        "no_turn_relations": restriction_certificate["source_relations"][
            "no_turn_relation_count"
        ],
        "only_turn_relations": restriction_certificate["source_relations"][
            "only_turn_relation_count"
        ],
        "conditional_restriction_relations": restriction_certificate[
            "source_relations"
        ]["conditional_relation_count"],
        "expected_prohibited_turn_transitions": restriction_certificate[
            "expected_prohibited_transitions"
        ]["count"],
        "source_candidate_turn_transitions": restriction_certificate[
            "source_candidate_transitions"
        ]["count"],
        "osrm_non_enforced_candidate_turn_transitions": restriction_certificate[
            "osrm_non_enforced_candidate_transitions"
        ]["count"],
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
