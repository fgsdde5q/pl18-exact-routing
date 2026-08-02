#!/usr/bin/env python3

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path


OLD_COUNT = 15_654_639
OLD_SHA256 = "6a6a0c8e63380242569548bff93b74931fb5f2d54ca1320c33afceadb5364061"
NEW_COUNT = 15_654_641
NEW_SHA256 = "cb613a7ad6019181064b3f09ef36ccabd8a732ad50897f04da49468306e23bad"
DIRECTED_EDGE_SHA256 = "f27449dfa026193831458712bcb97c1c8b07fdf9fe4f9d33c6c2f2f613a4406a"


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def line_count(path: Path) -> int:
    with path.open("rb") as source:
        return sum(
            block.count(b"\n")
            for block in iter(lambda: source.read(1024 * 1024), b"")
        )


def parse_row(line: str) -> dict:
    incoming, outgoing, duration, status = line.rstrip("\n").split("\t")
    incoming_parts = incoming.split("/")
    outgoing_parts = outgoing.split("/")
    category = (
        "u_turn"
        if incoming_parts[3] == outgoing_parts[4]
        and incoming_parts[4] == outgoing_parts[3]
        else "ordinary_turn"
    )
    return {
        "incoming_stable_edge_id": incoming,
        "outgoing_stable_edge_id": outgoing,
        "turn_duration_s": float(duration),
        "restriction_status": status,
        "restriction_relation_ids": [],
        "transition_category": category,
    }


def record_key(record: dict) -> tuple[str, str]:
    return record["incoming_stable_edge_id"], record["outgoing_stable_edge_id"]


def load_relation_map(path: Path) -> dict[tuple[str, str], list[int]]:
    certificate = json.loads(path.read_text(encoding="utf-8"))
    records = certificate["osrm_non_enforced_candidate_transitions"]["sample"]
    return {
        (record["incoming_edge"], record["outgoing_edge"]): record["relation_ids"]
        for record in records
    }


def decorate(record: dict, relation_map: dict, change: str) -> dict:
    record["restriction_relation_ids"] = relation_map.get(record_key(record), [])
    record["reason"] = (
        "Pinned OSRM graph built with one extraction thread contains this allowed "
        "transition; the earlier unpinned parallel extraction omitted it."
        if change == "added"
        else "Record differs after pinning the OSRM graph build to one extraction thread."
    )
    record["first_changed_graph_build_commit"] = (
        "5049c28cc511550be5cbd20c7f49870b989a13d8"
    )
    record["first_changed_certificate_commit"] = "69d3746"
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--old", required=True, type=Path)
    parser.add_argument("--new", required=True, type=Path)
    parser.add_argument("--restriction-certificate", required=True, type=Path)
    parser.add_argument("--json", required=True, type=Path)
    parser.add_argument("--markdown", required=True, type=Path)
    args = parser.parse_args()

    observed = {
        "old": {"count": line_count(args.old), "sha256": digest(args.old)},
        "new": {"count": line_count(args.new), "sha256": digest(args.new)},
    }
    expected = {
        "old": {"count": OLD_COUNT, "sha256": OLD_SHA256},
        "new": {"count": NEW_COUNT, "sha256": NEW_SHA256},
    }
    if observed != expected:
        raise ValueError(
            f"turn-state inputs do not match the two certified versions: {observed}"
        )

    relation_map = load_relation_map(args.restriction_certificate)
    with tempfile.TemporaryDirectory() as directory:
        temporary = Path(directory)
        old_sorted = temporary / "old.tsv"
        new_sorted = temporary / "new.tsv"
        comm_path = temporary / "comm.tsv"
        environment = {**os.environ, "LC_ALL": "C"}
        for source, destination in ((args.old, old_sorted), (args.new, new_sorted)):
            with destination.open("wb") as output:
                subprocess.run(
                    ["sort", "-u", "-T", directory, str(source)],
                    check=True,
                    stdout=output,
                    env=environment,
                )
        with comm_path.open("wb") as output:
            subprocess.run(
                ["comm", "-3", str(old_sorted), str(new_sorted)],
                check=True,
                stdout=output,
                env=environment,
            )
        removed_lines = []
        added_lines = []
        for line in comm_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("\t"):
                added_lines.append(line[1:])
            else:
                removed_lines.append(line)

    removed = [parse_row(line) for line in removed_lines]
    added = [parse_row(line) for line in added_lines]
    removed_by_key = {record_key(record): record for record in removed}
    added_by_key = {record_key(record): record for record in added}
    changed_keys = sorted(removed_by_key.keys() & added_by_key.keys())
    changed = [
        {
            "incoming_stable_edge_id": pair[0],
            "outgoing_stable_edge_id": pair[1],
            "old": removed_by_key[pair],
            "new": added_by_key[pair],
            "restriction_relation_ids": relation_map.get(pair, []),
            "transition_category": added_by_key[pair]["transition_category"],
            "reason": "Record payload changed after pinning the OSRM build to one thread.",
            "first_changed_graph_build_commit": (
                "5049c28cc511550be5cbd20c7f49870b989a13d8"
            ),
            "first_changed_certificate_commit": "69d3746",
        }
        for pair in changed_keys
    ]
    removed = [
        decorate(record, relation_map, "removed")
        for record in removed
        if record_key(record) not in changed_keys
    ]
    added = [
        decorate(record, relation_map, "added")
        for record in added
        if record_key(record) not in changed_keys
    ]

    result = {
        "schema_version": 1,
        "canonical_schema": {
            "version": "turn-state-canonical-v1",
            "fields": [
                "incoming_stable_edge_id",
                "outgoing_stable_edge_id",
                "turn_duration_s",
                "restriction_status",
            ],
            "sorting": ["incoming_stable_edge_id", "outgoing_stable_edge_id"],
            "initial_start_states_included": False,
        },
        "versions": {
            "previous": expected["old"],
            "current_authoritative": expected["new"],
            "directed_base_edges_sha256_both": DIRECTED_EDGE_SHA256,
        },
        "set_comparison": {
            "added_count": len(added),
            "removed_count": len(removed),
            "changed_count": len(changed),
            "added": added,
            "removed": removed,
            "changed": changed,
        },
        "cause": {
            "real_osrm_graph_changed": True,
            "exporter_fix": False,
            "canonical_schema_or_sorting_changed": False,
            "initial_start_states_added": False,
            "restriction_certificate_logic_changed": False,
            "non_enforced_restriction_candidates_changed": False,
            "exact_cause": (
                "The earlier graph was extracted with an unpinned parallel thread count. "
                "The current graph pins extract, partition, and customize to one thread. "
                "Two independent clean single-thread builds agree and contain two "
                "additional allowed edge-based transitions."
            ),
        },
        "authority": {
            "count": NEW_COUNT,
            "sha256": NEW_SHA256,
            "basis": (
                "frozen manifest v2, frozen profile, frozen PBF, pinned OSRM commit, JOBS=1"
            ),
            "history_retained": [expected["old"]],
        },
    }
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    lines = [
        "# Stage 2A turn-state version diff",
        "",
        "## Decision",
        "",
        f"- Authoritative count: `{NEW_COUNT}`.",
        f"- Authoritative SHA-256: `{NEW_SHA256}`.",
        f"- Previous history: `{OLD_COUNT}`, `{OLD_SHA256}`.",
        "- Canonical schema and sorting are unchanged (`turn-state-canonical-v1`).",
        "- Initial/start states remain separate and are not ordinary turn transitions.",
        "",
        "## Exact cause",
        "",
        "The old graph used an unpinned parallel OSRM build. The authoritative graph pins all OSRM graph phases to `JOBS=1`. Directed base edges are identical, but the single-thread edge-based graph contains two additional allowed transitions. This is a graph-product difference, not an exporter, schema, sorting, start-state, restriction-certificate, or 25-candidate logic change.",
        "",
        "## Set comparison",
        "",
        f"- Added: `{len(added)}`.",
        f"- Removed: `{len(removed)}`.",
        f"- Changed: `{len(changed)}`.",
        "",
    ]
    for label, records in (("Added", added), ("Removed", removed)):
        lines.extend([f"### {label}", ""])
        if not records:
            lines.append("None.")
        for record in records:
            relation_ids = record["restriction_relation_ids"] or "not applicable"
            lines.append(
                f"- `{record['incoming_stable_edge_id']}` → "
                f"`{record['outgoing_stable_edge_id']}`; duration "
                f"`{record['turn_duration_s']:.1f}` s; category "
                f"`{record['transition_category']}`; restriction relation "
                f"`{relation_ids}`; first graph-build change `5049c28`; "
                "first certificate change `69d3746`."
            )
        lines.append("")
    lines.extend(["### Changed", ""])
    lines.append("None." if not changed else json.dumps(changed, ensure_ascii=False))
    args.markdown.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
