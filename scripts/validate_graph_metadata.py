#!/usr/bin/env python3

import argparse
import json
from pathlib import Path

from certify_graph_stage import validate_edges, validate_turns


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metadata", required=True, type=Path)
    args = parser.parse_args()
    summary = json.loads(
        (args.metadata / "export-summary.json").read_text(encoding="utf-8")
    )
    edge_count, _ = validate_edges(args.metadata / "directed-base-edges.tsv")
    turn_count, _ = validate_turns(args.metadata / "edge-based-turn-states.tsv")
    if edge_count != summary["legal_directed_motorcar_edges"]:
        raise ValueError("directed edge count mismatch")
    if turn_count != summary["edge_based_turn_states"]:
        raise ValueError("turn state count mismatch")
    if summary["prohibited_turn_violations"] != 0:
        raise ValueError("prohibited turn appears in exported graph")
    restriction_certificate = json.loads(
        (args.metadata / "turn-restriction-certificate.json").read_text(
            encoding="utf-8"
        )
    )
    if (
        restriction_certificate["osrm_summary"]["accepted_restrictions"]
        != summary["enforced_turn_restrictions"]
    ):
        raise ValueError("restriction certificate disagrees with OSRM summary")
    expected = restriction_certificate["expected_prohibited_transitions"]
    if expected["count"] <= 0 or expected["observed_in_exported_turn_states"] != 0:
        raise ValueError("restriction transition proof is incomplete")
    candidates = restriction_certificate["source_candidate_transitions"]
    non_enforced = restriction_certificate[
        "osrm_non_enforced_candidate_transitions"
    ]
    way_aware_non_enforced = restriction_certificate[
        "way_aware_non_enforced_transitions"
    ]
    if candidates["count"] != expected["count"] + way_aware_non_enforced["count"]:
        raise ValueError("restriction candidate partition is inconsistent")
    if way_aware_non_enforced["count"] != 0:
        raise ValueError("way-aware restriction candidate remains allowed")
    if non_enforced["count"] and not all(
        item.get("relation_ids") for item in non_enforced["sample"]
    ):
        raise ValueError("non-enforced restriction candidates lack provenance")
    start_snap = json.loads(
        (args.metadata / "start-snap.json").read_text(encoding="utf-8")
    )
    available = sorted(
        item["stable_edge_id"]
        for item in start_snap["available_legal_initial_directions"]
    )
    certified = sorted(
        item["stable_edge_id"]
        for item in start_snap["geometrically_possible_initial_directions"]
        if item["legal_initial_direction"]
    )
    if available != certified:
        raise ValueError("start direction certificate disagrees with exported graph")
    if len(available) == 1:
        explanation = start_snap.get("single_direction_explanation", {})
        if explanation.get("status") != "START_DIRECTIONS_CERTIFIED":
            raise ValueError("single start direction is unexplained")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
