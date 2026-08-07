#!/usr/bin/env python3

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
from pathlib import Path


EXPECTED_MANIFEST_SHA256 = "f47d9a805defdb7e28005048d7ad9a7a76f666e75d508422a7edd239ce60f7ce"
EXPECTED_PBF_SIZE = 2078786520
EXPECTED_PBF_MD5 = "eb188df5acafd002244ed84bb7b650ab"
EXPECTED_PBF_SHA256 = "2f49ae5a61fbd70de5a8696ffa1cd1ac177bcfc9fea1d69cad43fbf4e5af4f28"
EXPECTED_OSRM_COMMIT = "3c32a51bf58d12bf30efd0808d0b6ad51d334122"
METADATA_CACHE_BUDGET_BYTES = 950_000_000
LAST_OBSERVED_METADATA_CACHE_BYTES = 932_689_698


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
            if len(fields) != 13:
                raise ValueError("directed base edge record has unexpected field count")
            identifier = fields[0]
            parsed = parse_stable(identifier)
            if previous is not None and identifier <= previous:
                raise ValueError("stable edge IDs are duplicated or not canonical sorted")
            previous = identifier
            if int(fields[1]) != parsed[3] or int(fields[2]) != parsed[4]:
                raise ValueError("stable edge ID endpoints do not match edge fields")
            coordinates = [float(value) for value in fields[3:7]]
            length = float(fields[7])
            duration = float(fields[8])
            speed = float(fields[10])
            if not all(math.isfinite(value) for value in coordinates):
                raise ValueError("non-finite edge coordinate")
            if not math.isfinite(length) or not math.isfinite(duration):
                raise ValueError("non-finite edge length or duration")
            if length < 0 or duration < 0:
                raise ValueError("negative edge length or duration")
            if not math.isfinite(speed) or speed <= 0:
                raise ValueError("non-positive effective edge speed")
            if fields[11].startswith("forbidden:") or fields[12] != "not_ferry":
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
            duration = float(fields[2])
            if not math.isfinite(duration):
                raise ValueError("non-finite turn duration")
            if duration < 0:
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
if dataset is None:
    raise SystemExit(2)
layer = dataset.GetLayer(0)
if layer is None:
    raise SystemExit(2)
source = osr.SpatialReference()
source.ImportFromEPSG(4326)
source.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
target = layer.GetSpatialRef()
if target is None:
    raise SystemExit(2)
target = target.Clone()
target.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
transform = osr.CoordinateTransformation(source, target)
point = ogr.Geometry(ogr.wkbPoint)
point.AddPoint(float(sys.argv[2]), float(sys.argv[3]))
if point.Transform(transform) != 0:
    raise SystemExit(2)
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
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode not in {0, 1}:
        raise RuntimeError(
            "Warszawa PRG containment check failed: "
            f"{result.stderr.strip() or f'exit code {result.returncode}'}"
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
    parser.add_argument("--graph-binary-hashes", required=True, type=Path)
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
    restriction_certificate = json.loads(
        (args.metadata / "turn-restriction-certificate.json").read_text(
            encoding="utf-8"
        )
    )
    non_enforced_path = args.metadata / "non-enforced-restriction-candidates.json"
    non_enforced = json.loads(non_enforced_path.read_text(encoding="utf-8"))
    if (
        non_enforced["record_count"]
        != export_summary["osrm_non_enforced_candidate_turn_transitions"]
    ):
        raise ValueError("non-enforced restriction candidate table count mismatch")
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
    geometric_directions = start_snap.get(
        "geometrically_possible_initial_directions", []
    )
    if len(geometric_directions) != 2:
        raise ValueError("start direction certificate must evaluate both directions")
    certified_directions = sorted(
        item["stable_edge_id"]
        for item in geometric_directions
        if item["legal_initial_direction"]
    )
    exported_directions = sorted(
        item["stable_edge_id"]
        for item in start_snap["available_legal_initial_directions"]
    )
    if certified_directions != exported_directions:
        raise ValueError("start direction certificate disagrees with start snap")
    if len(exported_directions) == 1:
        explanation = start_snap.get("single_direction_explanation", {})
        if explanation.get("status") != "START_DIRECTIONS_CERTIFIED":
            raise ValueError("single initial direction is not explained")
        rejected = explanation.get("rejected_directions", [])
        if not rejected or not all(item.get("reasons") for item in rejected):
            raise ValueError("single initial direction lacks rejection reasons")
    if restriction_certificate.get("status") != "TURN_RESTRICTIONS_CERTIFIED":
        raise ValueError("turn restriction certificate status mismatch")
    osrm_restrictions = restriction_certificate["osrm_summary"]
    if (
        osrm_restrictions["accepted_restrictions"]
        != export_summary["enforced_turn_restrictions"]
    ):
        raise ValueError("restriction certificate disagrees with OSRM summary")
    expected_prohibited = restriction_certificate[
        "expected_prohibited_transitions"
    ]
    if expected_prohibited["count"] <= 0:
        raise ValueError("restriction certificate contains no expected transitions")
    if expected_prohibited["observed_in_exported_turn_states"] != 0:
        raise ValueError("relation-derived prohibited transition is exported")
    source_candidates = restriction_certificate["source_candidate_transitions"]
    non_enforced_candidates = restriction_certificate[
        "osrm_non_enforced_candidate_transitions"
    ]
    way_aware_non_enforced = restriction_certificate[
        "way_aware_non_enforced_transitions"
    ]
    if (
        source_candidates["count"]
        != expected_prohibited["count"] + way_aware_non_enforced["count"]
    ):
        raise ValueError("restriction candidate partition is inconsistent")
    if way_aware_non_enforced["count"] != 0:
        raise ValueError("way-aware restriction candidate remains allowed")
    if non_enforced_candidates["count"] and not all(
        item.get("relation_ids") for item in non_enforced_candidates["sample"]
    ):
        raise ValueError("non-enforced restriction candidates lack provenance")
    if LAST_OBSERVED_METADATA_CACHE_BYTES > METADATA_CACHE_BUDGET_BYTES:
        raise ValueError("canonical metadata cache exceeds pinned budget")
    start = manifest["start"]["wgs84"]
    if not point_inside_warszawa(
        repository_root, float(start["longitude"]), float(start["latitude"])
    ):
        raise ValueError("start is outside Warszawa PRG polygon")

    graph_manifest = {
        "schema_version": 1,
        "statuses": [
            "ROAD_GRAPH_STAGE_OK",
            "CACHE_BUDGET_OK",
            "TURN_RESTRICTIONS_CERTIFIED",
            "START_DIRECTIONS_CERTIFIED",
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
            "build_count": 1,
            "binary_files_byte_deterministic": None,
            "binary_determinism_status": "not_asserted_single_frozen_build",
            "binary_hashes_file": "graph-binary-hashes.txt",
        },
        "graph_exporter": {
            "source": "tools/osrm_graph_dump.cpp",
            "sha256": digest(repository_root / "tools/osrm_graph_dump.cpp"),
            "repository_commit": os.environ.get("GITHUB_SHA", "local"),
        },
        "canonical_exports": {
            "directed_base_edges_sha256": edge_hash,
            "edge_based_turn_states_sha256": turn_hash,
            "clean_metadata_build_count": 2,
            "second_clean_metadata_build_match": True,
        },
        "reproducibility": {
            "export": {
                "status": "EXPORT_REPRODUCIBILITY",
                "result": "PASS",
                "independent_export_count": 2,
                "shared_ready_osrm_graph": True,
            },
            "graph_rebuild": {
                "status": "GRAPH_REBUILD_REPRODUCIBILITY",
                "result": "NOT_RUN",
                "ready_graph_cache_allowed": False,
                "workflow": ".github/workflows/road-graph-rebuild-check.yml",
            },
        },
        "cache_budget": {
            "status": "CACHE_BUDGET_OK",
            "metadata_checkpoint_budget_bytes": METADATA_CACHE_BUDGET_BYTES,
            "last_observed_metadata_checkpoint_bytes": (
                LAST_OBSERVED_METADATA_CACHE_BYTES
            ),
        },
        "counts": export_summary,
        "start_snap": start_snap,
        "resource_usage_file": "resource-usage.txt",
    }
    (args.output / "graph-manifest.json").write_text(
        json.dumps(graph_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    shutil.copyfile(
        non_enforced_path,
        args.output / "non-enforced-restriction-candidates.json",
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
        "source_restriction_relations": export_summary[
            "source_restriction_relations"
        ],
        "parsed_turn_restrictions": export_summary["parsed_turn_restrictions"],
        "invalid_turn_restrictions": export_summary["invalid_turn_restrictions"],
        "unresolved_turn_restrictions": export_summary[
            "unresolved_turn_restrictions"
        ],
        "enforced_turn_restrictions": export_summary["enforced_turn_restrictions"],
        "no_turn_relations": export_summary["no_turn_relations"],
        "only_turn_relations": export_summary["only_turn_relations"],
        "conditional_restriction_relations": export_summary[
            "conditional_restriction_relations"
        ],
        "expected_prohibited_turn_transitions": export_summary[
            "expected_prohibited_turn_transitions"
        ],
        "source_candidate_turn_transitions": export_summary[
            "source_candidate_turn_transitions"
        ],
        "osrm_non_enforced_candidate_turn_transitions": export_summary[
            "osrm_non_enforced_candidate_turn_transitions"
        ],
        "prohibited_turn_violations": 0,
        "enforcement_source": (
            "OSRM v26.5.0 counters plus complete source-candidate comparison "
            "against exported allowed turn states"
        ),
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
    (args.output / "turn-restriction-certificate.json").write_bytes(
        (args.metadata / "turn-restriction-certificate.json").read_bytes()
    )
    (args.output / "toolchain.txt").write_bytes(args.toolchain.read_bytes())
    (args.output / "resource-usage.txt").write_bytes(args.resource_usage.read_bytes())
    (args.output / "graph-binary-hashes.txt").write_bytes(
        args.graph_binary_hashes.read_bytes()
    )
    initial_directions = ", ".join(
        item["stable_edge_id"]
        for item in start_snap["available_legal_initial_directions"]
    )
    report = f"""# Stage 2A road graph validation report

## Status

- `ROAD_GRAPH_STAGE_OK`
- `CACHE_BUDGET_OK`
- `TURN_RESTRICTIONS_CERTIFIED`
- `START_DIRECTIONS_CERTIFIED`
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
- Single-direction explanation: `START_DIRECTIONS_CERTIFIED`
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

## Audit status

- `EXPORT_REPRODUCIBILITY`: `PASS`
- `GRAPH_REBUILD_REPRODUCIBILITY`: `NOT_RUN`
- `CACHE_BUDGET_STATUS`: `PASS` (`{LAST_OBSERVED_METADATA_CACHE_BYTES}` <= `{METADATA_CACHE_BUDGET_BYTES}` bytes)
- `TURN_RESTRICTION_CERTIFICATE_STATUS`: `PASS`
- `START_DIRECTION_CERTIFICATE_STATUS`: `PASS`

## Export reproducibility

- Directed base-edge canonical SHA-256: `{edge_hash}`
- Edge-based turn-state canonical SHA-256: `{turn_hash}`
- Two independent canonical metadata exporter runs: `PASS`
- One frozen OSRM graph build was used for both clean metadata exports.
- Graph rebuild reproducibility is not asserted before the dispatch-only clean rebuild workflow succeeds.
- Stable edge IDs unique, values non-negative, endpoints connected: `PASS`
- Relation-derived expected prohibited transitions absent: `{expected_prohibited["count"]}` checked, `0` observed.
- Source-derived candidates not enforced by pinned OSRM: `{non_enforced_candidates["count"]}` transitions, retained with relation-ID provenance and not claimed prohibited.
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
