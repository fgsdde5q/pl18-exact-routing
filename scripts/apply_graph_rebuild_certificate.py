#!/usr/bin/env python3

import argparse
import hashlib
import json
from pathlib import Path


FINAL_STATUSES = [
    "ROAD_GRAPH_STAGE_OK",
    "GRAPH_REBUILD_REPRODUCIBLE",
    "CACHE_BUDGET_OK",
    "TURN_RESTRICTIONS_CERTIFIED",
    "START_DIRECTIONS_CERTIFIED",
    "PRG_PROVENANCE_LOCKED",
    "SOLVER_NOT_STARTED",
]


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--certificate", required=True, type=Path)
    parser.add_argument("--results", required=True, type=Path)
    args = parser.parse_args()

    certificate = json.loads(args.certificate.read_text(encoding="utf-8"))
    if certificate.get("status") != "GRAPH_REBUILD_REPRODUCIBLE":
        raise ValueError("cannot publish unsuccessful graph rebuild certificate")
    if certificate.get("full_graph_build_count") != 2:
        raise ValueError("graph rebuild certificate does not contain two builds")
    if certificate.get("ready_graph_cache_restored") is not False:
        raise ValueError("graph rebuild certificate used a ready graph cache")

    destination = args.results / "graph-rebuild-reproducibility.json"
    destination.write_bytes(args.certificate.read_bytes())
    manifest_path = args.results / "graph-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    first = certificate["comparisons"]
    canonical = manifest["canonical_exports"]
    cross_checks = {
        "directed base-edge canonical SHA-256": (
            first["directed_base_edges_sha256"]["first"],
            canonical["directed_base_edges_sha256"],
        ),
        "edge-based turn-state canonical SHA-256": (
            first["edge_based_turn_states_sha256"]["first"],
            canonical["edge_based_turn_states_sha256"],
        ),
        "counts": (first["counts"]["first"], manifest["counts"]),
        "start snap": (first["start_snap"]["first"], manifest["start_snap"]),
        "turn restriction certificate": (
            first["turn_restriction_certificate"]["first"],
            json.loads(
                (args.results / "turn-restriction-certificate.json").read_text(
                    encoding="utf-8"
                )
            ),
        ),
    }
    mismatches = [
        label for label, (rebuilt, canonical_value) in cross_checks.items()
        if rebuilt != canonical_value
    ]
    if mismatches:
        raise ValueError(
            "clean rebuild disagrees with canonical Stage 2A products: "
            + ", ".join(mismatches)
        )
    manifest["statuses"] = FINAL_STATUSES
    manifest["reproducibility"]["graph_rebuild"] = {
        "status": "GRAPH_REBUILD_REPRODUCIBILITY",
        "result": "PASS",
        "full_graph_build_count": 2,
        "ready_graph_cache_restored": False,
        "certificate": destination.name,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    report_path = args.results / "validation-report.md"
    report = report_path.read_text(encoding="utf-8")
    report = report.replace(
        "- `GRAPH_REBUILD_REPRODUCIBILITY`: `NOT_RUN`",
        "- `GRAPH_REBUILD_REPRODUCIBILITY`: `PASS`",
    )
    report = report.replace(
        "- Graph rebuild reproducibility is not asserted before the dispatch-only clean rebuild workflow succeeds.",
        "- Two independent clean OSRM graph rebuilds from frozen inputs produced matching canonical exports and certificates.",
    )
    status_anchor = "- `ROAD_GRAPH_STAGE_OK`\n"
    if "- `GRAPH_REBUILD_REPRODUCIBLE`\n" not in report:
        report = report.replace(
            status_anchor,
            status_anchor + "- `GRAPH_REBUILD_REPRODUCIBLE`\n",
        )
    report_path.write_text(report, encoding="utf-8")

    checksum_path = args.results / "SHA256SUMS"
    with checksum_path.open("w", encoding="utf-8") as target:
        for path in sorted(args.results.iterdir()):
            if path.is_file() and path.name != checksum_path.name:
                target.write(f"{digest(path)}  {path.name}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
