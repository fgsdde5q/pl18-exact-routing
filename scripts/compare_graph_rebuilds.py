#!/usr/bin/env python3

import argparse
import hashlib
import json
from pathlib import Path


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def build_record(metadata: Path, provenance: Path) -> dict:
    return {
        "provenance": load_json(provenance),
        "directed_base_edges_sha256": digest(metadata / "directed-base-edges.tsv"),
        "edge_based_turn_states_sha256": digest(
            metadata / "edge-based-turn-states.tsv"
        ),
        "counts": load_json(metadata / "export-summary.json"),
        "start_snap": load_json(metadata / "start-snap.json"),
        "turn_restriction_certificate": load_json(
            metadata / "turn-restriction-certificate.json"
        ),
        "non_enforced_restriction_candidates": load_json(
            metadata / "non-enforced-restriction-candidates.json"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--first-metadata", required=True, type=Path)
    parser.add_argument("--second-metadata", required=True, type=Path)
    parser.add_argument("--first-provenance", required=True, type=Path)
    parser.add_argument("--second-provenance", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    first = build_record(args.first_metadata, args.first_provenance)
    second = build_record(args.second_metadata, args.second_provenance)
    first_provenance = first["provenance"]
    second_provenance = second["provenance"]
    if first_provenance["build_id"] == second_provenance["build_id"]:
        raise ValueError("clean graph rebuilds use the same build identity")
    if (
        Path(first_provenance["clean_graph_root"]).resolve()
        == Path(second_provenance["clean_graph_root"]).resolve()
    ):
        raise ValueError("clean graph rebuilds use the same graph directory")
    if (
        first_provenance["ready_graph_cache_restored"]
        or second_provenance["ready_graph_cache_restored"]
    ):
        raise ValueError("ready OSRM graph cache was used by clean rebuild audit")

    compared_fields = [
        "directed_base_edges_sha256",
        "edge_based_turn_states_sha256",
        "counts",
        "start_snap",
        "turn_restriction_certificate",
        "non_enforced_restriction_candidates",
    ]
    mismatches = [
        field for field in compared_fields if first[field] != second[field]
    ]
    result = {
        "schema_version": 1,
        "status": (
            "GRAPH_REBUILD_REPRODUCIBLE" if not mismatches else "MISMATCH"
        ),
        "full_graph_build_count": 2,
        "ready_graph_cache_restored": False,
        "builds": [first_provenance, second_provenance],
        "comparisons": {
            field: {
                "match": first[field] == second[field],
                "first": first[field],
                "second": second[field],
            }
            for field in compared_fields
        },
        "mismatches": mismatches,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if mismatches:
        raise ValueError(
            "clean graph rebuild mismatch: " + ", ".join(mismatches)
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
