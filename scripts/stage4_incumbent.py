#!/usr/bin/env python3
"""Recover and independently replay one Stage 3.1 incumbent witness."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from decimal import Decimal
from pathlib import Path

import stage3_upper_bounds as stage3
from stage4_exact import COST_SCALE, MODELS, START_EDGE, exact_units, sha256


def verify_artifacts(repository: Path, artifacts: Path) -> dict:
    manifest = json.loads((repository / "results/stage3/stage3-manifest.json").read_text())
    actual = {}
    for name, expected in manifest["stage2b_artifact_hashes"].items():
        path = artifacts / name
        if not path.is_file():
            raise ValueError(f"missing canonical Stage 2B artifact {name}")
        digest = sha256(path)
        size = path.stat().st_size
        if digest != expected["sha256"] or size != expected["size_bytes"]:
            raise ValueError(f"frozen artifact mismatch for {name}")
        actual[name] = {"sha256": digest, "size_bytes": size}
    return actual


def city_maps(repository: Path) -> tuple[dict[str, int], dict[int, str]]:
    document = stage3.load_manifest(repository)
    cities = document.get("cities", {}).get("canonical_bit_order")
    if not isinstance(cities, list):
        raise ValueError("manifest v2/v3 has no city list")
    mapping = {stage3.normalise_city(item["city"]): int(item["bit_index"]) for item in cities}
    return mapping, {value: key for key, value in mapping.items()}


def collect_exact_records(artifacts: Path, path: list[str], model: str) -> tuple[dict, dict]:
    wanted_edges = set(path)
    wanted_pairs = set(zip(path, path[1:]))
    edges = {}
    edge_path = artifacts / "split-edges.tsv.zst"
    if not edge_path.exists():
        edge_path = edge_path.with_suffix("")
    mask_column = stage3.MODEL_MASK_COLUMN[model]
    for row in stage3.read_tsv(edge_path):
        edge_id = row["split_edge_id"]
        if edge_id in wanted_edges:
            edges[edge_id] = {
                "duration_units": exact_units(row["duration_s"]),
                "distance": Decimal(row["length_m"]),
                "mask": int(row[mask_column]),
                "from": (row["from_lon"], row["from_lat"]),
                "to": (row["to_lon"], row["to_lat"]),
            }
    if set(edges) != wanted_edges:
        raise ValueError(f"witness contains {len(wanted_edges - set(edges))} missing split edges")
    turns = {}
    for filename in ("internal-turn-states.tsv.zst", "inherited-turn-states.tsv.zst"):
        turn_path = artifacts / filename
        if not turn_path.exists():
            turn_path = turn_path.with_suffix("")
        for row in stage3.read_tsv(turn_path):
            pair = (row["incoming_split_edge_id"], row["outgoing_split_edge_id"])
            if pair in wanted_pairs:
                value = (exact_units(row["turn_duration_s"]), row["transition_category"])
                previous = turns.get(pair)
                if previous is not None and previous != value:
                    raise ValueError(f"ambiguous explicit transition {pair}")
                turns[pair] = value
    return edges, turns


def recover(repository: Path, artifacts: Path, model: str, output: Path, timeout: float) -> dict:
    hashes = verify_artifacts(repository, artifacts)
    candidate = json.loads((repository / f"results/stage3/candidates/{model}-top.json").read_text())["BEST_FOUND_FEASIBLE"]
    if not candidate:
        raise ValueError(f"Stage 3.1 has no feasible candidate for {model}")
    city_to_bit, bit_to_city = city_maps(repository)
    database = output / "work" / f"{model}-recovery.sqlite"
    graph = stage3.build_sqlite_graph(artifacts, model, database)
    try:
        result = stage3.evaluate_fixed_order(
            edges=graph, adjacency=graph, model_id=model, route_id="stage3.1-incumbent-recovery",
            nominal_order=candidate["actual_first_hit_order"], city_to_bit=city_to_bit,
            bit_to_city=bit_to_city, start_edge_id=START_EDGE,
            source_heuristic="historical_best_first_hit_order", seed=stage3.SEED,
            initial_visited_mask=1,
            start_snap_fraction=json.loads((repository / "results/graph-prg/start-state-certificate.json").read_text())["snap_fraction"],
            timeout_s=timeout, heuristic_weight=1.0,
        )
        if result.result_category != "FEASIBLE" or not result.split_edge_path:
            raise ValueError(f"incumbent recovery failed: {result.result_category}: {result.failure_reason}")
        path = result.split_edge_path
        exact_edges, explicit_turns = collect_exact_records(artifacts, path, model)
        duration = 0  # frozen start state has zero cost
        distance = Decimal(0)
        visited = 1
        actual_order = [bit_to_city[0]]
        categories = {"explicit": 0, "implicit_edge_based_geometry_continuation": 0}
        for index, edge_id in enumerate(path):
            edge = exact_edges[edge_id]
            if index:
                pair = (path[index - 1], edge_id)
                explicit = explicit_turns.get(pair)
                if explicit is None:
                    legal = any(turn.outgoing == edge_id and
                                turn.category == "implicit_edge_based_geometry_continuation"
                                for turn in graph.outgoing_turns(path[index - 1]))
                    if not legal:
                        raise ValueError(f"illegal witness transition {pair}")
                    turn_units, category = 0, "implicit_edge_based_geometry_continuation"
                else:
                    turn_units, category = explicit
                categories["explicit" if category != "implicit_edge_based_geometry_continuation" else category] += 1
                duration += turn_units + edge["duration_units"]
                distance += edge["distance"]
            new = edge["mask"] & ~visited
            for bit in range(18):
                if new & (1 << bit):
                    actual_order.append(bit_to_city[bit])
            visited |= edge["mask"]
            if visited == (1 << 18) - 1 and index != len(path) - 1:
                raise ValueError("recovered path continues after the first all-city goal")
        road_hash = hashlib.sha256("\n".join(path).encode("utf-8")).hexdigest()
        geometry = hashlib.sha256()
        for edge_id in path:
            edge = exact_edges[edge_id]
            geometry.update(f"{edge['from'][0]},{edge['from'][1]}>{edge['to'][0]},{edge['to'][1]}\n".encode("ascii"))
        geometry_hash = geometry.hexdigest()
        if visited != (1 << 18) - 1 or actual_order != candidate["actual_first_hit_order"]:
            raise ValueError("recovered witness fails all-city/first-hit replay")
        if round(duration / COST_SCALE, 1) != candidate["total_duration_s"]:
            raise ValueError("recovered witness objective differs from Stage 3.1 certificate")
        if road_hash != candidate["full_split_edge_path_hash"] or geometry_hash != candidate["geometry_hash"]:
            raise ValueError("recovered witness hashes differ from Stage 3.1 certificate")
        output.mkdir(parents=True, exist_ok=True)
        plain = output / f"{model}-incumbent-route-edges.txt"
        plain.write_text("\n".join(path) + "\n", encoding="utf-8")
        compressed = output / f"{model}-incumbent-route-edges.zst"
        subprocess.run(["zstd", "-q", "-f", "-19", str(plain), "-o", str(compressed)], check=True)
        plain.unlink()
        document = {
            "model": model, "status": "INCUMBENT_REPLAY_VALID", "incumbent_enabled": True,
            "initial_UB_units": duration, "initial_UB_seconds": duration / COST_SCALE,
            "historical_duration_seconds": candidate["total_duration_s"],
            "road_path_sha256": road_hash, "geometry_sha256": geometry_hash,
            "historical_path_hash_match": True, "actual_first_hit_order": actual_order,
            "total_distance_metres": float(distance), "visited_mask": visited,
            "start_state": START_EDGE, "transition_categories": categories,
            "split_edge_count": len(path), "stage2b_artifact_hashes": hashes,
        }
        destination = output / f"{model}-incumbent-replay.json"
        destination.write_text(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        return document
    finally:
        graph.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, default=Path("."))
    parser.add_argument("--stage2b-artifacts", type=Path, required=True)
    parser.add_argument("--model", choices=MODELS, required=True)
    parser.add_argument("--output", type=Path, default=Path("results/stage4/input-validation"))
    parser.add_argument("--timeout-seconds", type=float, default=18000)
    args = parser.parse_args()
    recover(args.repository.resolve(), args.stage2b_artifacts, args.model, args.output, args.timeout_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
