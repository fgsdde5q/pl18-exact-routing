#!/usr/bin/env python3
"""External-memory adapter for the Stage 4 exact product-graph solver.

The immutable road graph and mutable product labels live in separate SQLite
databases.  Every cost column is integer microseconds; no REAL value is used by
queue ordering, dominance, pruning, or the optimality test.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import sqlite3
import time
from pathlib import Path

import stage3_upper_bounds as stage3
from stage4_exact import (ALL_18, COST_SCALE, MODELS, SCHEMA_VERSION, START_EDGE,
                          exact_units, seconds, sha256)
from stage4_incumbent import verify_artifacts

MASK_COLUMN = stage3.MODEL_MASK_COLUMN
EXPECTED_EDGES = 33_640_280
EXPECTED_EXPLICIT = 15_752_983
EXPECTED_IMPLICIT = 24_936_692


def configure_build(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        PRAGMA temp_store=FILE;
        PRAGMA cache_size=-500000;
    """)


def artifact_path(root: Path, name: str) -> Path:
    result = root / name
    if result.exists():
        return result
    alternate = result.with_suffix("")
    if alternate.exists():
        return alternate
    raise FileNotFoundError(result)


def build_graph(repository: Path, artifacts: Path, model: str, destination: Path,
                enforce_canonical_counts: bool = True) -> dict:
    hashes = verify_artifacts(repository, artifacts)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    db = sqlite3.connect(destination)
    configure_build(db)
    db.executescript("""
        CREATE TABLE edges(
          edge_id TEXT PRIMARY KEY, duration_units INTEGER NOT NULL,
          distance_m TEXT NOT NULL, city_mask INTEGER NOT NULL,
          from_lon TEXT NOT NULL, from_lat TEXT NOT NULL,
          to_lon TEXT NOT NULL, to_lat TEXT NOT NULL,
          parent_edge_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
          from_node TEXT NOT NULL, to_node TEXT NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE turns(
          incoming TEXT NOT NULL, outgoing TEXT NOT NULL,
          turn_units INTEGER NOT NULL, category TEXT NOT NULL
        );
        CREATE TABLE geometry_turns(
          incoming TEXT PRIMARY KEY, outgoing TEXT NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
    """)
    rows = []
    edge_count = 0
    column = MASK_COLUMN[model]
    for edge_count, row in enumerate(stage3.read_tsv(artifact_path(artifacts, "split-edges.tsv.zst")), 1):
        parent = row["parent_edge_id"]
        parts = parent.split("/")
        rows.append((row["split_edge_id"], exact_units(row["duration_s"]), row["length_m"],
                     int(row[column]), row["from_lon"], row["from_lat"], row["to_lon"], row["to_lat"],
                     parent, int(row["ordinal"]), parts[3], parts[4]))
        if len(rows) == 100_000:
            db.executemany("INSERT INTO edges VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            rows.clear()
            db.commit()
    db.executemany("INSERT INTO edges VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    db.commit()
    turn_rows = []
    explicit_count = 0
    for name in ("internal-turn-states.tsv.zst", "inherited-turn-states.tsv.zst"):
        for row in stage3.read_tsv(artifact_path(artifacts, name)):
            explicit_count += 1
            turn_rows.append((row["incoming_split_edge_id"], row["outgoing_split_edge_id"],
                              exact_units(row["turn_duration_s"]), row["transition_category"]))
            if len(turn_rows) == 100_000:
                db.executemany("INSERT INTO turns VALUES(?,?,?,?)", turn_rows)
                turn_rows.clear()
                db.commit()
    db.executemany("INSERT INTO turns VALUES(?,?,?,?)", turn_rows)
    db.commit()
    db.executescript("""
        CREATE INDEX turns_incoming ON turns(incoming);
        CREATE INDEX edges_from_node_ordinal ON edges(from_node,ordinal);
        CREATE INDEX edges_parent_ordinal ON edges(parent_edge_id,ordinal);
        INSERT INTO geometry_turns(incoming,outgoing)
        SELECT current.edge_id, MIN(successor.edge_id)
        FROM edges current
        JOIN edges successor ON successor.from_node=current.to_node
          AND successor.ordinal=0 AND successor.to_node!=current.from_node
        WHERE NOT EXISTS(
          SELECT 1 FROM edges sibling
          WHERE sibling.parent_edge_id=current.parent_edge_id
            AND sibling.ordinal=current.ordinal+1)
          AND NOT EXISTS(SELECT 1 FROM turns WHERE incoming=current.edge_id)
          AND NOT EXISTS(SELECT 1 FROM turns WHERE outgoing=successor.edge_id)
        GROUP BY current.edge_id HAVING COUNT(*)=1;
        CREATE INDEX geometry_turns_outgoing ON geometry_turns(outgoing);
    """)
    implicit_count = db.execute("SELECT COUNT(*) FROM geometry_turns").fetchone()[0]
    start_degree = db.execute("""
      SELECT (SELECT COUNT(*) FROM turns WHERE incoming=?)
           + (SELECT COUNT(*) FROM geometry_turns WHERE incoming=?)
    """, (START_EDGE, START_EDGE)).fetchone()[0]
    bits = [bit for bit in range(18) if db.execute(
        "SELECT 1 FROM edges WHERE (city_mask & ?) != 0 LIMIT 1", (1 << bit,)
    ).fetchone()]
    if enforce_canonical_counts:
        actual = (edge_count, explicit_count, implicit_count, start_degree, bits)
        expected = (EXPECTED_EDGES, EXPECTED_EXPLICIT, EXPECTED_IMPLICIT, 1, list(range(18)))
        if actual != expected:
            raise ValueError(f"Stage 4 loader audit mismatch: actual={actual}, expected={expected}")
    min_edge = db.execute("SELECT MIN(duration_units) FROM edges").fetchone()[0] or 0
    # MAX(mask) is not MAX(popcount); calculate the latter over only 18 possible one-city
    # masks in canonical data, while remaining correct for synthetic multi-bit fixtures.
    max_gain = max((int(row[0]).bit_count() for row in db.execute("SELECT DISTINCT city_mask FROM edges")), default=1)
    entry_min = {}
    for bit in range(18):
        value = db.execute("SELECT MIN(duration_units) FROM edges WHERE (city_mask & ?) != 0", (1 << bit,)).fetchone()[0]
        if value is not None:
            entry_min[str(bit)] = value
    identity = {
        "schema_version": SCHEMA_VERSION, "model": model, "cost_scale": COST_SCALE,
        "stage2b_manifest_sha256": sha256(repository / "results/graph-prg/stage2b-manifest.json"),
        "stage2b_artifact_hashes": hashes,
    }
    metadata = {
        "identity": identity, "edge_count": edge_count, "explicit_turn_count": explicit_count,
        "implicit_turn_count": implicit_count, "start_out_degree": start_degree, "city_bits": bits,
        "minimum_edge_units": min_edge, "maximum_bits_per_edge": max_gain,
        "minimum_city_edge_units": entry_min,
    }
    db.executemany("INSERT INTO metadata VALUES(?,?)",
                   [(key, json.dumps(value, sort_keys=True)) for key, value in metadata.items()])
    db.commit()
    db.execute("PRAGMA optimize")
    db.close()
    return metadata


class ExactRoadGraph:
    def __init__(self, path: Path):
        self.path = path
        self.db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self.metadata = {key: json.loads(value) for key, value in self.db.execute("SELECT key,value FROM metadata")}

    def mask(self, road: str) -> int:
        row = self.db.execute("SELECT city_mask FROM edges WHERE edge_id=?", (road,)).fetchone()
        if row is None:
            raise KeyError(road)
        return row[0]

    def outgoing(self, road: str):
        return self.db.execute("""
          SELECT t.outgoing,t.turn_units+e.duration_units,t.category
          FROM turns t JOIN edges e ON e.edge_id=t.outgoing WHERE t.incoming=?
          UNION ALL
          SELECT g.outgoing,e.duration_units,'implicit_edge_based_geometry_continuation'
          FROM geometry_turns g JOIN edges e ON e.edge_id=g.outgoing WHERE g.incoming=?
          ORDER BY 1,2,3
        """, (road, road))

    def close(self):
        self.db.close()


def heuristic(metadata: dict, mask: int) -> int:
    missing = ALL_18 & ~mask
    entry = 0
    minima = metadata["minimum_city_edge_units"]
    for bit in range(18):
        if missing & (1 << bit):
            entry = max(entry, int(minima.get(str(bit), 0)))
    count = missing.bit_count()
    maximum = max(1, int(metadata["maximum_bits_per_edge"]))
    steps = ((count + maximum - 1) // maximum) * int(metadata["minimum_edge_units"])
    return max(entry, steps)


def initialise_store(path: Path, identity: dict, incumbent: dict | None) -> sqlite3.Connection:
    new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("PRAGMA cache_size=-300000")
    if new:
        db.executescript("""
          CREATE TABLE labels(road TEXT NOT NULL,mask INTEGER NOT NULL,g INTEGER NOT NULL,
            active INTEGER NOT NULL,parent_road TEXT,parent_mask INTEGER,
            PRIMARY KEY(road,mask)) WITHOUT ROWID;
          CREATE TABLE frontier(road TEXT NOT NULL,mask INTEGER NOT NULL,f INTEGER NOT NULL,g INTEGER NOT NULL,
            PRIMARY KEY(road,mask)) WITHOUT ROWID;
          CREATE INDEX frontier_order ON frontier(f,g,road,mask);
          CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
        """)
        initial_mask = 1
        db.execute("INSERT INTO labels VALUES(?,?,?,?,?,?)", (START_EDGE, initial_mask, 0, 1, None, None))
        db.execute("INSERT INTO frontier VALUES(?,?,?,?)", (START_EDGE, initial_mask, 0, 0))
        ub = None
        path_value = None
        if incumbent is not None:
            if incumbent.get("status") != "INCUMBENT_REPLAY_VALID" or not incumbent.get("incumbent_enabled"):
                raise ValueError("refusing an incumbent without successful replay")
            ub = int(incumbent["initial_UB_units"])
        values = {"schema_version": SCHEMA_VERSION, "identity": identity, "UB": ub,
                  "incumbent_path": path_value, "history": [], "counters": {
                      "expanded": 0, "generated": 0, "dominated": 0, "UB_pruned": 0,
                      "unreachable_pruned": 0, "peak_frontier": 1}}
        db.executemany("INSERT INTO meta VALUES(?,?)", [(k, json.dumps(v, sort_keys=True)) for k, v in values.items()])
        db.commit()
    else:
        stored = json.loads(db.execute("SELECT value FROM meta WHERE key='identity'").fetchone()[0])
        if stored != identity:
            raise ValueError("checkpoint graph/config identity mismatch")
    return db


def meta_get(db: sqlite3.Connection, key: str):
    return json.loads(db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()[0])


def meta_set(db: sqlite3.Connection, key: str, value) -> None:
    db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, json.dumps(value, sort_keys=True)))


def reconstruct(db: sqlite3.Connection, road: str, mask: int) -> list[str]:
    result = []
    while True:
        result.append(road)
        row = db.execute("SELECT parent_road,parent_mask FROM labels WHERE road=? AND mask=?", (road, mask)).fetchone()
        if row is None:
            raise ValueError("broken label parent chain")
        if row[0] is None:
            return list(reversed(result))
        road, mask = row[0], row[1]


def run_chunk(graph_path: Path, store_path: Path, seconds_limit: float,
              max_expansions: int | None, incumbent_path: Path | None) -> dict:
    graph = ExactRoadGraph(graph_path)
    incumbent = json.loads(incumbent_path.read_text()) if incumbent_path else None
    identity = {**graph.metadata["identity"], "algorithm": "external_sqlite_astar_v1",
                "heuristic": "max(city_edge_entry,transition_count)"}
    db = initialise_store(store_path, identity, incumbent)
    started = time.monotonic()
    counters = meta_get(db, "counters")
    ub = meta_get(db, "UB")
    history = meta_get(db, "history")
    initial_expanded = counters["expanded"]
    reason = "FRONTIER_EXHAUSTED"
    while True:
        if time.monotonic() - started >= seconds_limit:
            reason = "CHUNK_TIME_LIMIT"
            break
        if max_expansions is not None and counters["expanded"] - initial_expanded >= max_expansions:
            reason = "CHUNK_EXPANSION_LIMIT"
            break
        row = db.execute("SELECT road,mask,f,g FROM frontier ORDER BY f,g,road,mask LIMIT 1").fetchone()
        if row is None:
            break
        road, mask, f_value, g_value = row
        if ub is not None and f_value >= ub:
            reason = "BOUND_MET"
            break
        db.execute("DELETE FROM frontier WHERE road=? AND mask=?", (road, mask))
        active = db.execute("SELECT g,active FROM labels WHERE road=? AND mask=?", (road, mask)).fetchone()
        if active != (g_value, 1):
            continue
        counters["expanded"] += 1
        if mask == ALL_18:
            ub = g_value
            route = reconstruct(db, road, mask)
            meta_set(db, "incumbent_path", route)
            history.append({"UB_units": ub, "expanded": counters["expanded"], "terminal_state": road})
            continue
        for target, arc_cost, category in graph.outgoing(road):
            counters["generated"] += 1
            next_mask = mask | graph.mask(target)
            next_g = g_value + arc_cost
            next_f = next_g + heuristic(graph.metadata, next_mask)
            if ub is not None and next_f >= ub:
                counters["UB_pruned"] += 1
                continue
            dominated = db.execute("""
              SELECT 1 FROM labels WHERE road=? AND active=1 AND (mask | ?)=mask AND g<=? LIMIT 1
            """, (target, next_mask, next_g)).fetchone()
            if dominated:
                counters["dominated"] += 1
                continue
            removed = db.execute("""
              SELECT mask FROM labels WHERE road=? AND active=1 AND (? | mask)=? AND ?<=g
            """, (target, next_mask, next_mask, next_g)).fetchall()
            for (old_mask,) in removed:
                db.execute("UPDATE labels SET active=0 WHERE road=? AND mask=?", (target, old_mask))
                db.execute("DELETE FROM frontier WHERE road=? AND mask=?", (target, old_mask))
                counters["dominated"] += 1
            existing = db.execute("SELECT g FROM labels WHERE road=? AND mask=?", (target, next_mask)).fetchone()
            if existing is not None and existing[0] <= next_g:
                counters["dominated"] += 1
                continue
            db.execute("INSERT OR REPLACE INTO labels VALUES(?,?,?,?,?,?)",
                       (target, next_mask, next_g, 1, road, mask))
            db.execute("INSERT OR REPLACE INTO frontier VALUES(?,?,?,?)",
                       (target, next_mask, next_f, next_g))
        if counters["expanded"] % 1000 == 0:
            size = db.execute("SELECT COUNT(*) FROM frontier").fetchone()[0]
            counters["peak_frontier"] = max(counters["peak_frontier"], size)
            meta_set(db, "UB", ub); meta_set(db, "history", history); meta_set(db, "counters", counters)
            db.commit()
    frontier = db.execute("SELECT MIN(f),COUNT(*) FROM frontier").fetchone()
    lb = frontier[0]
    if frontier[1] == 0 and ub is not None:
        lb = ub
    optimal = ub is not None and (frontier[1] == 0 or lb >= ub)
    status = "OPTIMAL" if optimal else "INCOMPLETE"
    meta_set(db, "UB", ub); meta_set(db, "history", history); meta_set(db, "counters", counters)
    db.commit()
    db.close(); graph.close()
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    document = {
        "schema_version": SCHEMA_VERSION, "solver_status": status, "stop_reason": reason,
        "UB_units": ub, "UB_seconds": seconds(ub), "LB_units": lb, "LB_seconds": seconds(lb),
        "absolute_gap_seconds": None if ub is None or lb is None else seconds(ub - lb),
        "relative_gap": None if ub is None or lb is None or ub == 0 else (ub - lb) / ub,
        "counters": counters, "wall_time_seconds": time.monotonic() - started,
        "peak_RSS_platform_units": peak, "checkpoint_bytes": store_path.stat().st_size,
        "identity": identity,
    }
    return document


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build-graph")
    build.add_argument("--repository", type=Path, default=Path("."))
    build.add_argument("--stage2b-artifacts", type=Path, required=True)
    build.add_argument("--model", choices=MODELS, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--fixture", action="store_true")
    run = commands.add_parser("run-chunk")
    run.add_argument("--graph", type=Path, required=True)
    run.add_argument("--checkpoint", type=Path, required=True)
    run.add_argument("--incumbent-replay", type=Path)
    run.add_argument("--seconds", type=float, default=18_000)
    run.add_argument("--max-expansions", type=int)
    run.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "build-graph":
        result = build_graph(args.repository.resolve(), args.stage2b_artifacts, args.model,
                             args.output, enforce_canonical_counts=not args.fixture)
    else:
        result = run_chunk(args.graph, args.checkpoint, args.seconds, args.max_expansions,
                           args.incumbent_replay)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.command == "run-chunk":
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    else:
        print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
