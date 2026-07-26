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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
