#!/usr/bin/env python3

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path


EXPECTED_MANIFEST_SHA256 = "f47d9a805defdb7e28005048d7ad9a7a76f666e75d508422a7edd239ce60f7ce"
EXPECTED_PBF_SIZE = 2078786520
EXPECTED_PBF_MD5 = "eb188df5acafd002244ed84bb7b650ab"
EXPECTED_PBF_SHA256 = "2f49ae5a61fbd70de5a8696ffa1cd1ac177bcfc9fea1d69cad43fbf4e5af4f28"
EXPECTED_OSRM_COMMIT = "3c32a51bf58d12bf30efd0808d0b6ad51d334122"


def digest(path: Path, algorithm: str = "sha256") -> str:
    value = hashlib.new(algorithm)
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def parse_stable(identifier: str) -> tuple[int, int, int, int, int]:
    values = tuple(int(value) for value in identifier.split("/"))
    if len(values) != 5:
        raise ValueError(f"invalid stable edge ID: {identifier}")
    return values


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


def validate_edges(path: Path) -> tuple[int, str]:
    previous = None
    count = 0
    sha256 = hashlib.sha256()
    with path.open("rb") as binary:
        for block in iter(lambda: binary.read(1024 * 1024), b""):
            sha256.update(block)
    with path.open(encoding="utf-8") as source:
        for line in source:
            fields = line.rstrip("\n").split("\t")
            if len(fields) != 15:
                raise ValueError("directed base edge record has unexpected field count")
            identifier = fields[0]
            parse_stable(identifier)
            if previous is not None and identifier <= previous:
                raise ValueError("stable edge IDs are duplicated or not canonical sorted")
            previous = identifier
            if float(fields[9]) < 0 or float(fields[10]) < 0:
                raise ValueError("negative edge length or duration")
            if float(fields[12]) <= 0:
                raise ValueError("non-positive effective edge speed")
            if fields[13].startswith("forbidden:") or fields[14] != "not_ferry":
                raise ValueError("forbidden access or ferry edge is routable")
            count += 1
    return count, sha256.hexdigest()


def validate_turns(path: Path) -> tuple[int, str]:
    previous = None
    count = 0
    sha256 = hashlib.sha256()
    with path.open("rb") as binary:
        for block in iter(lambda: binary.read(1024 * 1024), b""):
            sha256.update(block)
    with path.open(encoding="utf-8") as source:
        for line in source:
            fields = line.rstrip("\n").split("\t")
            if len(fields) != 4:
                raise ValueError("turn state record has unexpected field count")
            incoming = parse_stable(fields[0])
            outgoing = parse_stable(fields[1])
            key = (fields[0], fields[1])
            if previous is not None and key <= previous:
                raise ValueError("turn states are duplicated or not canonical sorted")
            previous = key
            if incoming[4] != outgoing[3]:
                raise ValueError("directed edge endpoints are disconnected")
            if float(fields[2]) < 0:
                raise ValueError("negative turn duration")
            if fields[3] != "allowed":
                raise ValueError("unexpected turn restriction status")
            count += 1
    return count, sha256.hexdigest()


def point_inside_warszawa(repository_root: Path, longitude: float, latitude: float) -> bool:
    script = """
from osgeo import ogr, osr
import sys
dataset = ogr.Open(sys.argv[1])
layer = dataset.GetLayer(0)
source = osr.SpatialReference()
source.ImportFromEPSG(4326)
target = layer.GetSpatialRef()
transform = osr.CoordinateTransformation(source, target)
point = ogr.Geometry(ogr.wkbPoint)
point.AddPoint(float(sys.argv[2]), float(sys.argv[3]))
point.Transform(transform)
for feature in layer:
    if str(feature.GetField("teryt")) == "1465011":
        raise SystemExit(0 if feature.GetGeometryRef().Contains(point) else 1)
raise SystemExit(2)
"""
    result = subprocess.run(
        [
            "python3",
            "-c",
            script,
            str(repository_root / "results/prg/prg-18-computational.gpkg"),
            str(longitude),
            str(latitude),
        ]
    )
    return result.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--pbf", required=True, type=Path)
    parser.add_argument("--metadata", required=True, type=Path)
    parser.add_argument("--first-hashes", required=True, type=Path)
    parser.add_argument("--second-hashes", required=True, type=Path)
    parser.add_argument("--toolchain", required=True, type=Path)
    parser.add_argument("--resource-usage", required=True, type=Path)
    parser.add_argument("--graph-binary-hashes-first", required=True, type=Path)
    parser.add_argument("--graph-binary-hashes-second", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    repository_root = Path(__file__).resolve().parent.parent
    args.output.mkdir(parents=True, exist_ok=True)

    manifest_sha256 = digest(args.manifest)
    if manifest_sha256 != EXPECTED_MANIFEST_SHA256:
        raise ValueError("frozen manifest v2 SHA-256 mismatch")
    if args.pbf.stat().st_size != EXPECTED_PBF_SIZE:
        raise ValueError("PBF size mismatch")
    if digest(args.pbf, "md5") != EXPECTED_PBF_MD5:
        raise ValueError("PBF MD5 mismatch")
    if digest(args.pbf) != EXPECTED_PBF_SHA256:
        raise ValueError("PBF SHA-256 mismatch")

    manifest = load_manifest(repository_root, args.manifest)
    profile_sha256 = digest(args.profile)
    export_summary = json.loads(
        (args.metadata / "export-summary.json").read_text(encoding="utf-8")
    )
    start_snap = json.loads(
        (args.metadata / "start-snap.json").read_text(encoding="utf-8")
    )
    first_hashes = json.loads(args.first_hashes.read_text(encoding="utf-8"))
    second_hashes = json.loads(args.second_hashes.read_text(encoding="utf-8"))
    if first_hashes != second_hashes:
        raise ValueError("second clean canonical metadata build differs")

    edge_count, edge_hash = validate_edges(args.metadata / "directed-base-edges.tsv")
    turn_count, turn_hash = validate_turns(args.metadata / "edge-based-turn-states.tsv")
    if edge_count != export_summary["legal_directed_motorcar_edges"]:
        raise ValueError("directed edge count mismatch")
    if turn_count != export_summary["edge_based_turn_states"]:
        raise ValueError("turn state count mismatch")
    if edge_hash != second_hashes["directed_base_edges_sha256"]:
        raise ValueError("directed edge canonical hash mismatch")
    if turn_hash != second_hashes["edge_based_turn_states_sha256"]:
        raise ValueError("turn state canonical hash mismatch")
    if export_summary["prohibited_turn_violations"] != 0:
        raise ValueError("prohibited turn appears in exported graph")
    if start_snap["distance_m"] > manifest["start"]["snap"]["max_distance_m"]:
        raise ValueError("start snap exceeds 150 metres")
    if not start_snap["available_legal_initial_directions"]:
        raise ValueError("start has no legal initial direction")
    start = manifest["start"]["wgs84"]
    if not point_inside_warszawa(
        repository_root, float(start["longitude"]), float(start["latitude"])
    ):
        raise ValueError("start is outside Warszawa PRG polygon")

    first_binary_hashes = args.graph_binary_hashes_first.read_text(encoding="utf-8")
    second_binary_hashes = args.graph_binary_hashes_second.read_text(encoding="utf-8")
    binary_deterministic = first_binary_hashes == second_binary_hashes
    graph_manifest = {
        "schema_version": 1,
        "statuses": [
            "ROAD_GRAPH_STAGE_OK",
            "PRG_PROVENANCE_LOCKED",
            "SOLVER_NOT_STARTED",
        ],
        "semantic_manifest": {
            "path": str(args.manifest.resolve().relative_to(repository_root)),
            "sha256": manifest_sha256,
        },
        "inputs": {
            "osm": {
                "file": manifest["inputs"]["osm"]["file"],
                "size_bytes": EXPECTED_PBF_SIZE,
                "md5": EXPECTED_PBF_MD5,
                "sha256": EXPECTED_PBF_SHA256,
            },
            "prg": manifest["inputs"]["prg"],
        },
        "osrm": {
            "version": manifest["routing_engine"]["version"],
            "commit": EXPECTED_OSRM_COMMIT,
            "pipeline": ["osrm-extract", "osrm-partition", "osrm-customize"],
            "binary_files_byte_deterministic": binary_deterministic,
        },
        "graph_exporter": {
            "source": "tools/osrm_graph_dump.cpp",
            "sha256": digest(repository_root / "tools/osrm_graph_dump.cpp"),
            "repository_commit": os.environ.get("GITHUB_SHA", "local"),
        },
        "canonical_exports": {
            "directed_base_edges_sha256": edge_hash,
            "edge_based_turn_states_sha256": turn_hash,
            "second_clean_build_match": True,
        },
        "counts": export_summary,
        "start_snap": start_snap,
        "resource_usage_file": "resource-usage.txt",
    }
    (args.output / "graph-manifest.json").write_text(
        json.dumps(graph_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    profile_manifest = {
        "schema_version": 1,
        "profile": "profiles/pl18-car.lua",
        "sha256": profile_sha256,
        "generated_from": {
            "manifest_sha256": manifest_sha256,
            "osrm_commit": EXPECTED_OSRM_COMMIT,
            "upstream_profile": "profiles/car.lua",
            "generator": "scripts/generate_pl18_profile.py",
            "generator_sha256": digest(
                repository_root / "scripts/generate_pl18_profile.py"
            ),
        },
        "inherited_from_OSRM_v26.5.0": [
            "directed oneway handling",
            "static vehicle dimension and weight parsing",
            "unconditional turn restriction expansion",
            "edge-based graph construction",
            "sigmoid angle penalty formula",
        ],
        "explicit_project_overrides": [
            "duration weight",
            "manifest v2 class speeds and static caps",
            "private and non-motorcar access forbidden",
            "ferries and shuttle trains forbidden",
            "conditional restrictions ignored",
            "toll allowed without extra penalty",
            "traffic, stop, ramp and interchange extras disabled",
            "gate and lift-gate penalties fixed at 60 seconds",
        ],
    }
    (args.output / "profile-manifest.json").write_text(
        json.dumps(profile_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    edge_counts = {
        "osm_ways_read": export_summary["osm_ways_read"],
        "legal_directed_motorcar_edges": edge_count,
        "edge_based_turn_states": turn_count,
    }
    (args.output / "edge-counts.json").write_text(
        json.dumps(edge_counts, indent=2) + "\n", encoding="utf-8"
    )
    restrictions_summary = {
        "enforced_turn_restrictions": export_summary["enforced_turn_restrictions"],
        "prohibited_turn_violations": 0,
        "enforcement_source": "OSRM v26.5.0 accepted restriction graph and edge-based transitions",
        "conditional_restrictions": "ignored",
    }
    (args.output / "restrictions-summary.json").write_text(
        json.dumps(restrictions_summary, indent=2) + "\n", encoding="utf-8"
    )
    access_summary = {
        "forbidden_ferry_edges": export_summary["forbidden_ferry_edges"],
        "rejected_private_nonmotorcar_edges": export_summary[
            "rejected_private_nonmotorcar_edges"
        ],
        "toll_policy": "allowed_without_extra_cost",
    }
    (args.output / "access-summary.json").write_text(
        json.dumps(access_summary, indent=2) + "\n", encoding="utf-8"
    )
    (args.output / "start-snap.json").write_bytes(
        (args.metadata / "start-snap.json").read_bytes()
    )
    (args.output / "toolchain.txt").write_bytes(args.toolchain.read_bytes())
    (args.output / "resource-usage.txt").write_bytes(args.resource_usage.read_bytes())

    binary_note = (
        "were byte-for-byte identical"
        if binary_deterministic
        else "were not byte-for-byte identical; canonical sorted exports were compared instead"
    )
    initial_directions = ", ".join(
        item["stable_edge_id"]
        for item in start_snap["available_legal_initial_directions"]
    )
    report = f"""# Stage 2A road graph validation report

## Status

- `ROAD_GRAPH_STAGE_OK`
- `PRG_PROVENANCE_LOCKED`
- `SOLVER_NOT_STARTED`

## Frozen inputs

- Manifest v2 SHA-256: `{manifest_sha256}`
- OSM size: `{EXPECTED_PBF_SIZE}` bytes
- OSM MD5: `{EXPECTED_PBF_MD5}`
- OSM SHA-256: `{EXPECTED_PBF_SHA256}`
- PRG raw GML SHA-256: `{manifest["inputs"]["prg"]["raw_gml_sha256"]}`
- PRG compressed GML SHA-256: `{manifest["inputs"]["prg"]["compressed_gml_sha256"]}`
- Profile SHA-256: `{profile_sha256}`
- OSRM source commit: `{EXPECTED_OSRM_COMMIT}`

## Graph counts

- OSM ways read: `{export_summary["osm_ways_read"]}`
- Legal directed motorcar edges: `{edge_count}`
- Edge-based turn states: `{turn_count}`
- Enforced turn restrictions: `{export_summary["enforced_turn_restrictions"]}`
- Restriction authority: OSRM's accepted restriction graph after invalid-restriction removal.
- Forbidden ferry edges: `{export_summary["forbidden_ferry_edges"]}`
- Rejected private/non-motorcar edges: `{export_summary["rejected_private_nonmotorcar_edges"]}`

## Start snap

- Input: `{start["longitude"]}, {start["latitude"]}`
- Snapped coordinate: `{start_snap["snapped_wgs84"]["longitude"]}, {start_snap["snapped_wgs84"]["latitude"]}`
- EPSG:2180 coordinate: `{start_snap["snapped_epsg2180"]["x"]}, {start_snap["snapped_epsg2180"]["y"]}`
- Distance: `{start_snap["distance_m"]}` m
- Selected OSM way: `{start_snap["osm_way_id"]}`
- Selected stable edge: `{start_snap["selected_stable_edge_id"]}`
- Available legal initial directions: `{initial_directions}`
- Exact start inside Warszawa frozen PRG polygon: `PASS`

## inherited_from_OSRM_v26.5.0

- Directed oneway handling and physical vehicle limits.
- Unconditional static turn restriction expansion.
- Edge-based graph construction and sigmoid angle penalty implementation.

## explicit_project_overrides

- Duration is the routing weight.
- Manifest v2 class speeds, maxspeed reduction, surface, tracktype, smoothness and bridge caps.
- Private/non-motorcar roads, ferries and shuttle trains are forbidden.
- Conditional time/day/date/season restrictions and all dynamic inputs are ignored.
- Toll roads have no extra cost; gate/lift-gate penalties are 60 seconds.
- Traffic-signal, stop-sign, ramp, interchange and roundabout extras are zero.

## Reproducibility

- Directed base-edge canonical SHA-256: `{edge_hash}`
- Edge-based turn-state canonical SHA-256: `{turn_hash}`
- Second clean metadata build: `PASS`
- OSRM binary files {binary_note}.
- Stable edge IDs unique, values non-negative, endpoints connected: `PASS`
- Export contains only OSRM-allowed turn transitions; prohibited-status records: `0`.
- Ferry/private rejection and 150 m snap bound: `PASS`
"""
    (args.output / "validation-report.md").write_text(report, encoding="utf-8")

    checksum_paths = [
        path
        for path in sorted(args.output.iterdir())
        if path.is_file() and path.name != "SHA256SUMS"
    ]
    with (args.output / "SHA256SUMS").open("w", encoding="utf-8") as target:
        for path in checksum_paths:
            target.write(f"{digest(path)}  {path.name}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
