#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import hashlib
import heapq
import json
import random
import resource
import sqlite3
import subprocess
import sys
import time
import traceback
from collections import Counter, deque
from contextlib import contextmanager
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Iterator, Protocol

MODEL_ORDER = ("M-boundary", "M-300", "M-500")
MODEL_ALIASES = {"A": "M-boundary", "B": "M-300", "C": "M-500"}
MODEL_MASK_COLUMN = {"M-boundary": "M_boundary_mask", "M-300": "M_300_mask", "M-500": "M_500_mask"}
SEED = 20260726
MAX_SPEED_MPS = 25.0  # Frozen profile maximum: motorway 90 km/h.
UPPER_BOUND_HEURISTIC_WEIGHT = 1000.0
FIXED_ORDER_TIMEOUT_S = 900.0
GENERAL_EVALUATION_TIMEOUT_S = 0.25
LOCAL_CHECK_TIMEOUT_S = 10.0
UNCONDITIONAL_STATUSES = ["EXACT_GLOBAL_SOLVER_NOT_STARTED"]
SUCCESS_STATUSES = [
    "UPPER_BOUND_STAGE_OK",
    "MANDATORY_ROUTES_EVALUATED",
    "ALL_FINISHES_EVALUATED",
    "TOP_K_FEASIBLE_CANDIDATES_READY",
]
FAILURE_STATUS = "STAGE3_FAILED_NO_FEASIBLE_ROUTE"
VALIDATION_FAILURE_STATUS = "STAGE3_FAILED_VALIDATION"
OUTCOME_CATEGORIES = (
    "FEASIBLE",
    "ORDER_INVALID_BY_FIRST_HIT",
    "UNREACHABLE",
    "SOLVER_ERROR",
    "TIMEOUT",
    "MISSING_STATE",
    "OTHER_FAILURE",
)

ROUTES = {
    "A": [
        "Warszawa", "Łódź", "Kielce", "Lublin", "Rzeszów", "Kraków",
        "Katowice", "Opole", "Wrocław", "Zielona Góra", "Gorzów Wielkopolski",
        "Szczecin", "Poznań", "Bydgoszcz", "Toruń", "Gdańsk", "Olsztyn",
        "Białystok",
    ],
    "B": [
        "Warszawa", "Łódź", "Toruń", "Bydgoszcz", "Gdańsk", "Olsztyn",
        "Białystok", "Lublin", "Rzeszów", "Kielce", "Kraków", "Katowice",
        "Opole", "Wrocław", "Poznań", "Zielona Góra", "Gorzów Wielkopolski",
        "Szczecin",
    ],
    "C": [
        "Warszawa", "Białystok", "Olsztyn", "Gdańsk", "Bydgoszcz", "Toruń",
        "Łódź", "Kielce", "Lublin", "Rzeszów", "Kraków", "Katowice", "Opole",
        "Wrocław", "Poznań", "Zielona Góra", "Gorzów Wielkopolski", "Szczecin",
    ],
}

LOCAL_CHECKS = {
    "torun_bydgoszcz": ["Toruń", "Bydgoszcz"],
    "bydgoszcz_torun": ["Bydgoszcz", "Toruń"],
    "kielce_lublin_rzeszow": ["Kielce", "Lublin", "Rzeszów"],
    "lublin_rzeszow_kielce": ["Lublin", "Rzeszów", "Kielce"],
    "wroclaw_poznan_zielona": ["Wrocław", "Poznań", "Zielona Góra"],
    "wroclaw_zielona_poznan": ["Wrocław", "Zielona Góra", "Poznań"],
    "poznan_zielona_gorzow_szczecin": ["Poznań", "Zielona Góra", "Gorzów Wielkopolski", "Szczecin"],
    "reverse_northern_arc": ["Białystok", "Olsztyn", "Gdańsk", "Toruń", "Bydgoszcz", "Poznań"],
    "reverse_southern_arc": ["Szczecin", "Gorzów Wielkopolski", "Zielona Góra", "Wrocław", "Opole", "Katowice", "Kraków", "Rzeszów", "Lublin", "Kielce", "Łódź"],
}


@dataclass(frozen=True, slots=True)
class Edge:
    edge_id: str
    duration_s: float
    distance_m: float
    city_mask: int
    from_lon: str
    from_lat: str
    to_lon: str
    to_lat: str
    parent_edge_id: str = ""
    ordinal: int = 0
    start_fraction: float = 0.0
    end_fraction: float = 1.0
    from_node: str = ""
    to_node: str = ""


@dataclass(frozen=True, slots=True)
class Turn:
    outgoing: str
    duration_s: float
    category: str


@dataclass(slots=True)
class RouteResult:
    model_id: str
    route_id: str
    source_heuristic: str
    seed: int
    nominal_order: list[str]
    actual_first_hit_order: list[str]
    feasibility: str
    total_duration_s: float | None
    total_distance_m: float | None
    entry_exit_state_sequence: list[dict]
    segment_costs: list[dict]
    full_split_edge_path_hash: str | None
    geometry_hash: str | None
    checker_duration_s: float | None
    fixed_order_check_delta_s: float | None
    provenance: dict
    result_category: str = "OTHER_FAILURE"
    failure_reason: str = ""
    failing_transition_or_state: str = ""
    exception_text: str = ""
    split_edge_path: list[str] | None = None


class Graph(Protocol):
    def get_edge(self, edge_id: str) -> Edge | None:
        ...

    def outgoing_turns(self, edge_id: str) -> Iterable[Turn]:
        ...

    def first_edge_with_mask(self, mask: int) -> str | None:
        ...

    def city_bounds(self, bit: int) -> tuple[float, float, float, float] | None:
        ...

    def close(self) -> None:
        ...


class DictGraph:
    def __init__(self, edges: dict[str, Edge], adjacency: dict[str, list[Turn]]):
        self.edges = edges
        self.adjacency = adjacency

    def get_edge(self, edge_id: str) -> Edge | None:
        return self.edges.get(edge_id)

    def outgoing_turns(self, edge_id: str) -> Iterable[Turn]:
        return self.adjacency.get(edge_id, [])

    def first_edge_with_mask(self, mask: int) -> str | None:
        for edge_id in sorted(self.edges):
            edge = self.edges[edge_id]
            if edge.city_mask & mask:
                return edge_id
        return None

    def city_bounds(self, bit: int) -> tuple[float, float, float, float] | None:
        coordinates = [
            (float(value.from_lon), float(value.from_lat), float(value.to_lon), float(value.to_lat))
            for value in self.edges.values() if value.city_mask & (1 << bit)
        ]
        if not coordinates:
            return None
        return (
            min(min(value[0], value[2]) for value in coordinates),
            min(min(value[1], value[3]) for value in coordinates),
            max(max(value[0], value[2]) for value in coordinates),
            max(max(value[1], value[3]) for value in coordinates),
        )

    def close(self) -> None:
        return None


class SQLiteGraph:
    def __init__(self, path: Path):
        self.path = path
        self.connection = sqlite3.connect(path)

    @lru_cache(maxsize=500_000)
    def get_edge(self, edge_id: str) -> Edge | None:
        row = self.connection.execute(
            """
            SELECT edge_id, duration_s, distance_m, city_mask, from_lon, from_lat, to_lon, to_lat,
                   parent_edge_id, ordinal, start_fraction, end_fraction, from_node, to_node
            FROM edges WHERE edge_id = ?
            """,
            (edge_id,),
        ).fetchone()
        if row is None:
            return None
        return Edge(*row)

    def outgoing_turns(self, edge_id: str) -> Iterable[Turn]:
        rows = self.connection.execute(
            """
            SELECT outgoing, duration_s, category FROM turns WHERE incoming = ?
            UNION ALL
            SELECT outgoing, 0.0, 'implicit_edge_based_geometry_continuation'
            FROM geometry_turns WHERE incoming = ?
            """,
            (edge_id, edge_id),
        ).fetchall()
        for outgoing, duration_s, category in rows:
            yield Turn(outgoing, duration_s, category)

    def incoming_turns(self, edge_id: str) -> Iterable[Turn]:
        rows = self.connection.execute(
            """
            SELECT incoming, duration_s, category FROM turns WHERE outgoing = ?
            UNION ALL
            SELECT incoming, 0.0, 'implicit_edge_based_geometry_continuation'
            FROM geometry_turns WHERE outgoing = ?
            """,
            (edge_id, edge_id),
        )
        for incoming, duration_s, category in rows:
            yield Turn(incoming, duration_s, category)

    def expand_many(self, edge_ids: list[str]) -> dict[str, list[tuple[Turn, Edge]]]:
        """Fetch a frontier in one SQLite round trip.

        Stage 2B graphs have tens of millions of split edges.  A pair of SQL
        queries per expanded label dominated the original Stage 3 runtime, so
        the upper-bound search batches independent frontier reads while keeping
        the same turn and edge records used by ``outgoing_turns``/``get_edge``.
        """
        if not edge_ids:
            return {}
        unique_ids = list(dict.fromkeys(edge_ids))
        placeholders = ",".join("?" for _ in unique_ids)
        edge_columns = (
            "e.edge_id, e.duration_s, e.distance_m, e.city_mask, "
            "e.from_lon, e.from_lat, e.to_lon, e.to_lat, e.parent_edge_id, "
            "e.ordinal, e.start_fraction, e.end_fraction, e.from_node, e.to_node"
        )
        query = f"""
            SELECT t.incoming, t.duration_s, t.category, {edge_columns}
            FROM turns AS t JOIN edges AS e ON e.edge_id = t.outgoing
            WHERE t.incoming IN ({placeholders})
            UNION ALL
            SELECT g.incoming, 0.0, 'implicit_edge_based_geometry_continuation', {edge_columns}
            FROM geometry_turns AS g JOIN edges AS e ON e.edge_id = g.outgoing
            WHERE g.incoming IN ({placeholders})
        """
        result: dict[str, list[tuple[Turn, Edge]]] = {edge_id: [] for edge_id in unique_ids}
        for row in self.connection.execute(query, [*unique_ids, *unique_ids]):
            incoming, turn_duration, category, *edge_values = row
            edge = Edge(*edge_values)
            result[incoming].append((Turn(edge.edge_id, turn_duration, category), edge))
        return result

    def first_edge_with_mask(self, mask: int) -> str | None:
        bit = mask.bit_length() - 1
        row = self.connection.execute("SELECT edge_id FROM city_start_edges WHERE bit = ?", (bit,)).fetchone()
        return None if row is None else row[0]

    def gate_edges(self, bit: int) -> list[str]:
        rows = self.connection.execute(
            "SELECT edge_id FROM city_gate_edges WHERE bit = ? ORDER BY edge_id", (bit,)
        )
        return [row[0] for row in rows]

    def metadata(self) -> dict[str, str]:
        return dict(self.connection.execute("SELECT key, value FROM loader_metadata"))

    @lru_cache(maxsize=64)
    def city_bounds(self, bit: int) -> tuple[float, float, float, float] | None:
        row = self.connection.execute(
            "SELECT min_lon, min_lat, max_lon, max_lat FROM city_bounds WHERE bit = ?",
            (bit,),
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return tuple(float(value) for value in row)

    def close(self) -> None:
        type(self).get_edge.cache_clear()
        type(self).city_bounds.cache_clear()
        self.connection.close()


def get_edge(edges: dict[str, Edge] | Graph, edge_id: str) -> Edge | None:
    if isinstance(edges, dict):
        return edges.get(edge_id)
    return edges.get_edge(edge_id)


def outgoing_turns(adjacency: dict[str, list[Turn]] | Graph, edge_id: str) -> Iterable[Turn]:
    if isinstance(adjacency, dict):
        return adjacency.get(edge_id, [])
    return adjacency.outgoing_turns(edge_id)


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def json_dump(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def peak_rss_kb() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    # macOS reports bytes; Linux (including GitHub Actions) reports KiB.
    return value // 1024 if sys.platform == "darwin" else value


@contextmanager
def open_text(path: Path) -> Iterator[Iterable[str]]:
    if path.suffix == ".zst":
        process = subprocess.Popen(["zstd", "-dc", str(path)], stdout=subprocess.PIPE, text=True)
        if process.stdout is None:
            raise RuntimeError(f"zstd stdout unavailable for {path}")
        try:
            yield process.stdout
        finally:
            process.stdout.close()
            if process.wait() != 0:
                raise RuntimeError(f"zstd failed while reading {path}")
    else:
        with path.open(encoding="utf-8", newline="") as source:
            yield source


def read_tsv(path: Path) -> Iterator[dict[str, str]]:
    with open_text(path) as source:
        reader = csv.DictReader(source, delimiter="\t")
        for row in reader:
            yield row


def normalise_city(name: str) -> str:
    return {"Rzeszzów": "Rzeszów"}.get(name, name)


def load_manifest(repository: Path) -> dict:
    result = subprocess.run(
        ["ruby", str(repository / "scripts/manifest_json.rb"), str(repository / "instance/pl_18_capitals_static_instance_v3.yaml")],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def configure_sqlite(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode = OFF;
        PRAGMA synchronous = OFF;
        PRAGMA temp_store = MEMORY;
        PRAGMA cache_size = -200000;
        """
    )


def batch_insert(connection: sqlite3.Connection, statement: str, rows: list[tuple]) -> None:
    if rows:
        connection.executemany(statement, rows)
        rows.clear()


def build_sqlite_graph(artifact_dir: Path, model_id: str, database_path: Path) -> SQLiteGraph:
    started = time.perf_counter()
    edges_path = artifact_dir / "split-edges.tsv.zst"
    if not edges_path.exists():
        edges_path = artifact_dir / "split-edges.tsv"
    turn_paths = [
        artifact_dir / "internal-turn-states.tsv.zst",
        artifact_dir / "inherited-turn-states.tsv.zst",
    ]
    if not edges_path.exists():
        raise FileNotFoundError(f"missing Stage 2B split edge table: {edges_path}")
    if database_path.exists():
        database_path.unlink()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    configure_sqlite(connection)
    connection.executescript(
        """
        CREATE TABLE edges (
            edge_id TEXT PRIMARY KEY,
            duration_s REAL NOT NULL,
            distance_m REAL NOT NULL,
            city_mask INTEGER NOT NULL,
            from_lon TEXT NOT NULL,
            from_lat TEXT NOT NULL,
            to_lon TEXT NOT NULL,
            to_lat TEXT NOT NULL,
            parent_edge_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            start_fraction REAL NOT NULL,
            end_fraction REAL NOT NULL,
            from_node TEXT NOT NULL,
            to_node TEXT NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE turns (
            incoming TEXT NOT NULL,
            outgoing TEXT NOT NULL,
            duration_s REAL NOT NULL,
            category TEXT NOT NULL
        );
        CREATE TABLE city_start_edges (
            bit INTEGER PRIMARY KEY,
            edge_id TEXT NOT NULL
        );
        CREATE TABLE city_gate_edges (
            bit INTEGER NOT NULL,
            edge_id TEXT NOT NULL,
            PRIMARY KEY (bit, edge_id)
        ) WITHOUT ROWID;
        CREATE TABLE city_bounds (
            bit INTEGER PRIMARY KEY,
            min_lon REAL NOT NULL,
            min_lat REAL NOT NULL,
            max_lon REAL NOT NULL,
            max_lat REAL NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE loader_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE geometry_turns (
            incoming TEXT PRIMARY KEY,
            outgoing TEXT NOT NULL
        ) WITHOUT ROWID;
        """
    )
    mask_column = MODEL_MASK_COLUMN[model_id]
    edge_rows: list[tuple] = []
    city_starts: dict[int, str] = {}
    city_bounds: dict[int, tuple[float, float, float, float]] = {}
    edge_total = 0
    for count, row in enumerate(read_tsv(edges_path), start=1):
        edge_total = count
        edge_id = row["split_edge_id"]
        parent_parts = row["parent_edge_id"].split("/")
        if len(parent_parts) != 5:
            raise ValueError(f"invalid stable parent edge id: {row['parent_edge_id']}")
        city_mask = int(row[mask_column])
        edge_rows.append(
            (
                edge_id,
                float(row["duration_s"]),
                float(row["length_m"]),
                city_mask,
                row["from_lon"],
                row["from_lat"],
                row["to_lon"],
                row["to_lat"],
                row["parent_edge_id"],
                int(row["ordinal"]),
                float(row["start_fraction"]),
                float(row["end_fraction"]),
                parent_parts[3],
                parent_parts[4],
            )
        )
        remaining_mask = city_mask
        from_lon = float(row["from_lon"])
        from_lat = float(row["from_lat"])
        to_lon = float(row["to_lon"])
        to_lat = float(row["to_lat"])
        while remaining_mask:
            bit_mask = remaining_mask & -remaining_mask
            bit = bit_mask.bit_length() - 1
            previous_edge_id = city_starts.get(bit)
            if previous_edge_id is None or edge_id < previous_edge_id:
                city_starts[bit] = edge_id
            edge_bounds = (
                min(from_lon, to_lon), min(from_lat, to_lat),
                max(from_lon, to_lon), max(from_lat, to_lat),
            )
            previous_bounds = city_bounds.get(bit)
            city_bounds[bit] = edge_bounds if previous_bounds is None else (
                min(previous_bounds[0], edge_bounds[0]),
                min(previous_bounds[1], edge_bounds[1]),
                max(previous_bounds[2], edge_bounds[2]),
                max(previous_bounds[3], edge_bounds[3]),
            )
            remaining_mask ^= bit_mask
        if len(edge_rows) >= 100_000:
            batch_insert(connection, "INSERT INTO edges VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", edge_rows)
        if count % 1_000_000 == 0:
            print(f"stage3: indexed {count:,} split edges for {model_id}", flush=True)
    batch_insert(connection, "INSERT INTO edges VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", edge_rows)
    connection.executemany("INSERT INTO city_start_edges VALUES (?, ?)", sorted(city_starts.items()))
    connection.executemany(
        "INSERT INTO city_bounds VALUES (?, ?, ?, ?, ?)",
        [(bit, *bounds) for bit, bounds in sorted(city_bounds.items())],
    )
    gates_path = artifact_dir / "gates.tsv.zst"
    if not gates_path.exists():
        gates_path = artifact_dir / "gates.tsv"
    if not gates_path.exists():
        raise FileNotFoundError(f"missing Stage 2B gate table: {gates_path}")
    gate_edges: set[tuple[int, str]] = set()
    gate_rows_seen = 0
    for row in read_tsv(gates_path):
        if row["model_id"] != model_id:
            continue
        gate_rows_seen += 1
        bit = int(row["bit_index"])
        for column in ("incoming_split_edge_id", "outgoing_split_edge_id"):
            if row[column]:
                gate_edges.add((bit, row[column]))
    connection.executemany("INSERT INTO city_gate_edges VALUES (?, ?)", sorted(gate_edges))
    connection.commit()
    total_turns = 0
    turn_rows: list[tuple] = []
    for path in turn_paths:
        if not path.exists():
            alternate = path.with_suffix("")
            if alternate.exists():
                path = alternate
        if not path.exists():
            raise FileNotFoundError(f"missing Stage 2B turn table: {path}")
        rows_in_path = 0
        for count, row in enumerate(read_tsv(path), start=1):
            rows_in_path = count
            turn_rows.append(
                (
                    row["incoming_split_edge_id"],
                    row["outgoing_split_edge_id"],
                    float(row["turn_duration_s"]),
                    row["transition_category"],
                )
            )
            if len(turn_rows) >= 100_000:
                batch_insert(connection, "INSERT INTO turns VALUES (?, ?, ?, ?)", turn_rows)
            if count % 1_000_000 == 0:
                print(f"stage3: indexed {count:,} turn rows from {path.name}", flush=True)
        total_turns += rows_in_path
    batch_insert(connection, "INSERT INTO turns VALUES (?, ?, ?, ?)", turn_rows)
    connection.commit()
    print(f"stage3: creating turn index for {model_id}", flush=True)
    connection.execute("CREATE INDEX turns_incoming_idx ON turns(incoming)")
    connection.execute("CREATE INDEX turns_outgoing_idx ON turns(outgoing)")
    connection.execute("CREATE INDEX edges_from_node_ordinal_idx ON edges(from_node, ordinal)")
    connection.execute("CREATE INDEX edges_to_node_ordinal_idx ON edges(to_node, ordinal)")
    connection.execute("CREATE INDEX edges_parent_ordinal_idx ON edges(parent_edge_id, ordinal)")
    print(f"stage3: reconstructing implicit geometry continuations for {model_id}", flush=True)
    connection.execute(
        """
        INSERT INTO geometry_turns (incoming, outgoing)
        SELECT current.edge_id, MIN(successor.edge_id)
        FROM edges AS current
        JOIN edges AS successor
          ON successor.from_node = current.to_node
         AND successor.ordinal = 0
         AND successor.to_node != current.from_node
        WHERE NOT EXISTS (
                  SELECT 1 FROM edges AS sibling
                  WHERE sibling.parent_edge_id = current.parent_edge_id
                    AND sibling.ordinal = current.ordinal + 1
              )
          AND NOT EXISTS (SELECT 1 FROM turns WHERE incoming = current.edge_id)
          AND NOT EXISTS (SELECT 1 FROM turns WHERE outgoing = successor.edge_id)
        GROUP BY current.edge_id
        HAVING COUNT(*) = 1
        """
    )
    connection.execute("CREATE INDEX geometry_turns_outgoing_idx ON geometry_turns(outgoing)")
    geometry_turn_count = connection.execute("SELECT COUNT(*) FROM geometry_turns").fetchone()[0]
    connection.executemany(
        "INSERT INTO loader_metadata VALUES (?, ?)",
        [
            ("model_id", model_id),
            ("split_edge_rows", str(edge_total)),
            ("turn_rows", str(total_turns)),
            ("implicit_geometry_turn_rows", str(geometry_turn_count)),
            ("model_gate_rows", str(gate_rows_seen)),
            ("model_gate_edge_rows", str(len(gate_edges))),
            ("city_mask_bits", ",".join(str(bit) for bit in sorted(city_starts))),
        ],
    )
    connection.commit()
    stored_edge_total = connection.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    if stored_edge_total != edge_total:
        raise ValueError(f"SQLite loader lost split edges: source={edge_total}, stored={stored_edge_total}")
    print(
        f"stage3: sqlite graph ready for {model_id}: {edge_total:,} edges, "
        f"{total_turns:,} turns, "
        f"{time.perf_counter() - started:.1f}s",
        flush=True,
    )
    connection.close()
    return SQLiteGraph(database_path)


def names_from_mask(mask: int, bit_to_city: dict[int, str]) -> list[str]:
    return [city for bit, city in sorted(bit_to_city.items()) if mask & (1 << bit)]


def update_order(
    current: tuple[str, ...],
    visited_mask: int,
    edge_mask: int,
    bit_to_city: dict[int, str],
) -> tuple[tuple[str, ...], int]:
    order = list(current)
    new_mask = visited_mask | edge_mask
    for bit, city in sorted(bit_to_city.items()):
        if edge_mask & (1 << bit) and not visited_mask & (1 << bit):
            order.append(city)
    return tuple(order), new_mask


def order_prefix_valid(actual: tuple[str, ...], nominal: list[str]) -> bool:
    return list(actual) == nominal[: len(actual)]


def bbox_gap_m(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    lon_gap = max(0.0, right[0] - left[2], left[0] - right[2])
    lat_gap = max(0.0, right[1] - left[3], left[1] - right[3])
    # These deliberately low Poland-wide degree conversions preserve
    # admissibility while providing a useful exact-search lower bound.
    return ((lon_gap * 60_000.0) ** 2 + (lat_gap * 110_000.0) ** 2) ** 0.5


def point_to_bbox_gap_m(lon: float, lat: float, bounds: tuple[float, float, float, float]) -> float:
    lon_gap = max(0.0, bounds[0] - lon, lon - bounds[2])
    lat_gap = max(0.0, bounds[1] - lat, lat - bounds[3])
    return ((lon_gap * 60_000.0) ** 2 + (lat_gap * 110_000.0) ** 2) ** 0.5


def path_hash(path: list[str]) -> str:
    return hashlib.sha256("\n".join(path).encode("utf-8")).hexdigest()


def geometry_hash(path: list[str], edges: dict[str, Edge] | Graph) -> str:
    h = hashlib.sha256()
    for edge_id in path:
        edge = get_edge(edges, edge_id)
        if edge is None:
            raise ValueError(f"path points to missing split edge {edge_id}")
        h.update(f"{edge.from_lon},{edge.from_lat}>{edge.to_lon},{edge.to_lat}\n".encode("ascii"))
    return h.hexdigest()


def reconstruct_path(previous: dict[tuple[str, int], tuple[tuple[str, int], Turn]], state: tuple[str, int]) -> list[str]:
    path = [state[0]]
    while state in previous:
        state, _ = previous[state]
        path.append(state[0])
    return list(reversed(path))


def evaluate_fixed_order(
    *,
    edges: dict[str, Edge] | Graph,
    adjacency: dict[str, list[Turn]] | Graph,
    model_id: str,
    route_id: str,
    nominal_order: list[str],
    city_to_bit: dict[str, int],
    bit_to_city: dict[int, str],
    start_edge_id: str,
    source_heuristic: str,
    seed: int,
    checker: bool = False,
    initial_visited_mask: int | None = None,
    start_snap_fraction: float | None = None,
    timeout_s: float | None = None,
    heuristic_weight: float = 1.0,
) -> RouteResult:
    search_started = time.monotonic()
    nominal_order = [normalise_city(city) for city in nominal_order]
    target_mask = 0
    for city in nominal_order:
        target_mask |= 1 << city_to_bit[city]
    start_edge = get_edge(edges, start_edge_id)
    if start_edge is None:
        return invalid_result(
            model_id, route_id, source_heuristic, seed, nominal_order, [], "MISSING_STATE",
            failure_reason="certified start split edge is absent from SQLite graph",
            failing_transition_or_state=start_edge_id,
        )
    certified_start_mask = initial_visited_mask if initial_visited_mask is not None else start_edge.city_mask
    start_order, start_mask = update_order((), 0, certified_start_mask, bit_to_city)
    start_city = nominal_order[0]
    if start_city not in start_order:
        start_order = (start_city, *start_order)
        start_mask |= 1 << city_to_bit[start_city]
    if not order_prefix_valid(start_order, nominal_order):
        return invalid_result(
            model_id, route_id, source_heuristic, seed, nominal_order, list(start_order),
            "ORDER_INVALID_BY_FIRST_HIT", failure_reason="certified initial city mask violates nominal prefix",
            failing_transition_or_state=start_edge_id,
        )
    start_duration = start_edge.duration_s
    start_distance = start_edge.distance_m
    if start_snap_fraction is not None:
        interval = start_edge.end_fraction - start_edge.start_fraction
        if interval <= 0 or not (start_edge.start_fraction <= start_snap_fraction <= start_edge.end_fraction):
            return invalid_result(
                model_id, route_id, source_heuristic, seed, nominal_order, list(start_order), "MISSING_STATE",
                failure_reason="certified snap fraction is outside its certified child edge interval",
                failing_transition_or_state=(
                    f"{start_edge_id}: child=[{start_edge.start_fraction},{start_edge.end_fraction}], "
                    f"snap={start_snap_fraction}"
                ),
            )
        # Frozen Stage 2A certifies the selected direction as a zero-cost initial
        # state.  The snap is validated against the child interval, but Stage 3
        # must not reinterpret that state as a partially traversed road edge.
        start_duration = 0.0
        start_distance = 0.0
    distances: dict[tuple[str, int], float] = {(start_edge_id, start_mask): start_duration}
    lengths: dict[tuple[str, int], float] = {(start_edge_id, start_mask): start_distance}
    orders: dict[tuple[str, int], tuple[str, ...]] = {(start_edge_id, start_mask): start_order}
    previous: dict[tuple[str, int], tuple[tuple[str, int], Turn]] = {}
    bounds = []
    for city in nominal_order:
        bit = city_to_bit[city]
        value = edges.city_bounds(bit) if hasattr(edges, "city_bounds") else None
        bounds.append(value)
    suffix_lower_bound_s = [0.0] * (len(nominal_order) + 1)
    if all(value is not None for value in bounds):
        for index in range(len(nominal_order) - 2, -1, -1):
            assert bounds[index] is not None and bounds[index + 1] is not None
            suffix_lower_bound_s[index] = (
                suffix_lower_bound_s[index + 1]
                + bbox_gap_m(bounds[index], bounds[index + 1]) / MAX_SPEED_MPS
            )

    def heuristic(edge: Edge, progress: int) -> float:
        if progress >= len(nominal_order) or bounds[progress] is None:
            return 0.0
        return (
            point_to_bbox_gap_m(float(edge.to_lon), float(edge.to_lat), bounds[progress]) / MAX_SPEED_MPS
            + suffix_lower_bound_s[progress]
        )

    if heuristic_weight < 1.0:
        raise ValueError("heuristic_weight must be at least 1.0")
    start_priority = start_duration + heuristic_weight * heuristic(start_edge, len(start_order))
    heap = [(start_priority, start_duration, start_edge_id, start_mask)]
    expanded = 0
    best_goal: tuple[str, int] | None = None
    invalid_first_hit: tuple[float, tuple[str, ...], str] | None = None
    while heap:
        if timeout_s is not None and time.monotonic() - search_started > timeout_s:
            best_seen = max(orders.values(), key=len, default=start_order)
            return invalid_result(
                model_id, route_id, source_heuristic, seed, nominal_order, list(best_seen), "TIMEOUT",
                failure_reason=f"fixed-order search exceeded {timeout_s:.3f}s",
                failing_transition_or_state=f"expanded_labels={expanded}",
                expanded_labels=expanded,
            )
        batch: list[tuple[float, str, int, tuple[str, int], tuple[str, ...]]] = []
        # A small batch amortises SQLite calls without turning weighted A* into
        # a broad 512-way beam at every junction.
        batch_limit = 32 if heuristic_weight > 1.0 and hasattr(adjacency, "expand_many") else 1
        while heap and len(batch) < batch_limit:
            _priority, duration, edge_id, visited_mask = heapq.heappop(heap)
            state = (edge_id, visited_mask)
            if duration != distances.get(state):
                continue
            expanded += 1
            actual_order = orders[state]
            if visited_mask & target_mask == target_mask and list(actual_order) == nominal_order:
                best_goal = state
                break
            batch.append((duration, edge_id, visited_mask, state, actual_order))
        if best_goal is not None:
            break
        if not batch:
            continue
        if hasattr(adjacency, "expand_many"):
            expanded_turns = adjacency.expand_many([item[1] for item in batch])
        else:
            expanded_turns = {}
        for duration, edge_id, visited_mask, state, actual_order in batch:
            if expanded_turns:
                successors = expanded_turns.get(edge_id, [])
            else:
                successors = []
                for turn in outgoing_turns(adjacency, edge_id):
                    outgoing = get_edge(edges, turn.outgoing)
                    if outgoing is None:
                        raise ValueError(f"turn points to missing split edge {turn.outgoing}")
                    successors.append((turn, outgoing))
            for turn, outgoing in successors:
                next_order, next_mask = update_order(actual_order, visited_mask, outgoing.city_mask, bit_to_city)
                if next_mask & ~target_mask:
                    continue
                if not order_prefix_valid(next_order, nominal_order):
                    invalid_duration = duration + turn.duration_s + outgoing.duration_s
                    if invalid_first_hit is None or invalid_duration < invalid_first_hit[0]:
                        invalid_first_hit = (invalid_duration, next_order, f"{edge_id}->{outgoing.edge_id}")
                    continue
                next_state = (outgoing.edge_id, next_mask)
                next_duration = duration + turn.duration_s + outgoing.duration_s
                if next_duration < distances.get(next_state, float("inf")):
                    distances[next_state] = next_duration
                    lengths[next_state] = lengths[state] + outgoing.distance_m
                    orders[next_state] = next_order
                    previous[next_state] = (state, turn)
                    heapq.heappush(
                        heap,
                        (
                            next_duration + heuristic_weight * heuristic(outgoing, len(next_order)),
                            next_duration,
                            outgoing.edge_id,
                            next_mask,
                        ),
                    )
    if best_goal is None:
        if invalid_first_hit is not None:
            return invalid_result(
                model_id, route_id, source_heuristic, seed, nominal_order, list(invalid_first_hit[1]),
                "ORDER_INVALID_BY_FIRST_HIT",
                failure_reason="lowest-cost rejected label first entered a city outside the required prefix",
                failing_transition_or_state=invalid_first_hit[2],
                expanded_labels=expanded,
            )
        best_seen = max(orders.values(), key=len, default=start_order)
        outgoing_count = sum(1 for _ in outgoing_turns(adjacency, start_edge_id))
        reason = (
            "certified start edge has zero outgoing legal transitions"
            if expanded == 1 and outgoing_count == 0
            else "reachable fixed-order product graph exhausted without a goal label"
        )
        return invalid_result(
            model_id, route_id, source_heuristic, seed, nominal_order, list(best_seen), "UNREACHABLE",
            failure_reason=reason,
            failing_transition_or_state=f"{start_edge_id}; expanded_labels={expanded}; start_out_degree={outgoing_count}",
            expanded_labels=expanded,
        )
    split_path = reconstruct_path(previous, best_goal)
    segments = build_segments(split_path, previous, best_goal, edges, distances, lengths, nominal_order, city_to_bit)
    state_sequence = build_state_sequence(split_path, edges, bit_to_city)
    total_duration = distances[best_goal]
    total_distance = lengths[best_goal]
    return RouteResult(
        model_id=model_id,
        route_id=route_id,
        source_heuristic=source_heuristic,
        seed=seed,
        nominal_order=nominal_order,
        actual_first_hit_order=list(orders[best_goal]),
        feasibility="FEASIBLE_FIXED_ORDER",
        total_duration_s=round(total_duration, 1),
        total_distance_m=round(total_distance, 3),
        entry_exit_state_sequence=state_sequence,
        segment_costs=segments,
        full_split_edge_path_hash=path_hash(split_path),
        geometry_hash=geometry_hash(split_path, edges),
        checker_duration_s=round(total_duration, 1) if checker else None,
        fixed_order_check_delta_s=0.0 if checker else None,
        provenance={
            "algorithm": (
                "constrained_product_graph_shortest_path"
                if checker
                else ("exact_fixed_order_astar" if heuristic_weight == 1.0 else "weighted_astar_feasible_path")
            ),
            "expanded_labels": expanded,
            "heuristic": "visit_region_bbox_gap_at_frozen_90_kmh",
            "heuristic_weight": heuristic_weight,
            "stage": "Stage 3 upper-bound fixed-order evaluation",
        },
        result_category="FEASIBLE",
        split_edge_path=split_path,
    )


def invalid_result(
    model_id: str,
    route_id: str,
    source_heuristic: str,
    seed: int,
    nominal_order: list[str],
    actual_order: list[str],
    feasibility: str,
    *,
    failure_reason: str = "",
    failing_transition_or_state: str = "",
    exception_text: str = "",
    expanded_labels: int | None = None,
) -> RouteResult:
    category = feasibility if feasibility in OUTCOME_CATEGORIES else "OTHER_FAILURE"
    return RouteResult(
        model_id=model_id,
        route_id=route_id,
        source_heuristic=source_heuristic,
        seed=seed,
        nominal_order=nominal_order,
        actual_first_hit_order=actual_order,
        feasibility=feasibility,
        total_duration_s=None,
        total_distance_m=None,
        entry_exit_state_sequence=[],
        segment_costs=[],
        full_split_edge_path_hash=None,
        geometry_hash=None,
        checker_duration_s=None,
        fixed_order_check_delta_s=None,
        provenance={
            "algorithm": "fixed_order_product_graph_search",
            "stage": "Stage 3 upper-bound fixed-order evaluation",
            "expanded_labels": expanded_labels,
        },
        result_category=category,
        failure_reason=failure_reason,
        failing_transition_or_state=failing_transition_or_state,
        exception_text=exception_text,
    )


def build_state_sequence(path: list[str], edges: dict[str, Edge] | Graph, bit_to_city: dict[int, str]) -> list[dict]:
    sequence = []
    for edge_id in path:
        edge = get_edge(edges, edge_id)
        if edge is None:
            raise ValueError(f"path points to missing split edge {edge_id}")
        cities = names_from_mask(edge.city_mask, bit_to_city)
        if cities:
            sequence.append({"split_edge_id": edge_id, "cities": cities})
    return sequence


def build_segments(
    path: list[str],
    previous: dict[tuple[str, int], tuple[tuple[str, int], Turn]],
    goal: tuple[str, int],
    edges: dict[str, Edge] | Graph,
    distances: dict[tuple[str, int], float],
    lengths: dict[tuple[str, int], float],
    nominal_order: list[str],
    city_to_bit: dict[str, int],
) -> list[dict]:
    del previous, city_to_bit
    if not path:
        return []
    path_edges = []
    for edge_id in path:
        edge = get_edge(edges, edge_id)
        if edge is None:
            raise ValueError(f"path points to missing split edge {edge_id}")
        path_edges.append(edge)
    total_duration = distances[goal]
    total_distance = lengths[goal]
    return [
        {
            "from_city": nominal_order[0],
            "to_city": nominal_order[-1],
            "entry_stable_edge": path[0],
            "exit_stable_edge": path[-1],
            "internal_city_time_s": round(total_duration, 1),
            "internal_city_distance_m": round(total_distance, 3),
            "transition_time_s": 0.0,
            "transition_distance_m": 0.0,
        }
    ]


def find_city_start_edge(edges: dict[str, Edge] | Graph, city: str, city_to_bit: dict[str, int]) -> str:
    bit = city_to_bit[city]
    mask = 1 << bit
    if isinstance(edges, dict):
        for edge_id in sorted(edges):
            if edges[edge_id].city_mask & mask:
                return edge_id
    else:
        edge_id = edges.first_edge_with_mask(mask)
        if edge_id is not None:
            return edge_id
    raise ValueError(f"no split edge found inside required local-check start city: {city}")


def route_to_json(result: RouteResult) -> dict:
    return {
        "model_id": result.model_id,
        "route_id": result.route_id,
        "source_heuristic": result.source_heuristic,
        "seed": result.seed,
        "nominal_order": result.nominal_order,
        "actual_first_hit_order": result.actual_first_hit_order,
        "feasibility": result.feasibility,
        "total_duration_s": result.total_duration_s,
        "total_distance_m": result.total_distance_m,
        "entry_exit_state_sequence": result.entry_exit_state_sequence,
        "segment_costs": result.segment_costs,
        "full_split_edge_path_hash": result.full_split_edge_path_hash,
        "geometry_hash": result.geometry_hash,
        "checker_duration_s": result.checker_duration_s,
        "fixed_order_check_delta_s": result.fixed_order_check_delta_s,
        "provenance": result.provenance,
        "result_category": result.result_category,
        "failure_reason": result.failure_reason,
        "failing_transition_or_state": result.failing_transition_or_state,
        "exception_text": result.exception_text,
    }


def evaluate_attempt(**kwargs) -> RouteResult:
    try:
        return evaluate_fixed_order(**kwargs)
    except TimeoutError as error:
        return invalid_result(
            kwargs["model_id"], kwargs["route_id"], kwargs["source_heuristic"], kwargs["seed"],
            kwargs["nominal_order"], [], "TIMEOUT", failure_reason=str(error),
            exception_text="".join(traceback.format_exception_only(type(error), error)).strip(),
        )
    except (KeyError, FileNotFoundError) as error:
        return invalid_result(
            kwargs["model_id"], kwargs["route_id"], kwargs["source_heuristic"], kwargs["seed"],
            kwargs["nominal_order"], [], "MISSING_STATE", failure_reason=str(error),
            exception_text="".join(traceback.format_exception_only(type(error), error)).strip(),
        )
    except Exception as error:  # Each scheduled evaluation must produce exactly one auditable outcome.
        return invalid_result(
            kwargs["model_id"], kwargs["route_id"], kwargs["source_heuristic"], kwargs["seed"],
            kwargs["nominal_order"], [], "SOLVER_ERROR", failure_reason="uncaught fixed-order solver exception",
            exception_text="".join(traceback.format_exception(type(error), error, error.__traceback__)).strip(),
        )


def candidate_orders(cities: list[str]) -> list[tuple[str, list[str]]]:
    rng = random.Random(SEED)
    base_orders = [("fixed-A", ROUTES["A"]), ("fixed-B", ROUTES["B"]), ("fixed-C", ROUTES["C"])]
    rest = [city for city in cities if city != "Warszawa"]
    orders = list(base_orders)
    orders.append(("manifest-order", cities))
    orders.append(("reverse-tail", ["Warszawa", *reversed(rest)]))
    for restart in range(8):
        shuffled = rest[:]
        rng.shuffle(shuffled)
        orders.append((f"deterministic-random-restart-{restart}", ["Warszawa", *shuffled]))
    for source, order in base_orders:
        for index in range(1, len(order) - 2):
            variant = order[:]
            variant[index], variant[index + 1] = variant[index + 1], variant[index]
            orders.append((f"{source}-adjacent-swap-{index}", variant))
    return orders


def terminal_orders(candidates: list[tuple[str, list[str]]], terminal: str) -> list[tuple[str, list[str]]]:
    result = []
    for source, order in candidates:
        if terminal not in order or order[-1] == terminal:
            result.append((source, order))
        else:
            middle = [city for city in order[1:] if city != terminal]
            result.append((f"{source}-terminal-{terminal}", [order[0], *middle, terminal]))
    return result


def independent_fixed_order_checker(**kwargs) -> tuple[float | None, float | None]:
    edges = kwargs["edges"]
    adjacency = kwargs["adjacency"]
    nominal = [normalise_city(city) for city in kwargs["nominal_order"]]
    city_to_bit = kwargs["city_to_bit"]
    bit_to_city = kwargs["bit_to_city"]
    start_edge_id = kwargs["start_edge_id"]
    start_edge = get_edge(edges, start_edge_id)
    if start_edge is None:
        return None, None
    visited = kwargs.get("initial_visited_mask")
    if visited is None:
        visited = start_edge.city_mask
    actual = names_from_mask(visited, bit_to_city)
    if nominal[0] not in actual:
        actual.insert(0, nominal[0])
        visited |= 1 << city_to_bit[nominal[0]]
    if actual != nominal[:len(actual)]:
        return None, None
    split_path = kwargs.get("split_edge_path")
    if split_path:
        if split_path[0] != start_edge_id:
            return None, None
        duration = 0.0 if kwargs.get("start_snap_fraction") is not None else start_edge.duration_s
        distance = 0.0 if kwargs.get("start_snap_fraction") is not None else start_edge.distance_m
        order = tuple(actual)
        mask = visited
        previous_edge_id = start_edge_id
        for edge_id in split_path[1:]:
            edge = get_edge(edges, edge_id)
            if edge is None:
                return None, None
            legal = [turn for turn in outgoing_turns(adjacency, previous_edge_id) if turn.outgoing == edge_id]
            if not legal:
                return None, None
            turn = min(legal, key=lambda value: value.duration_s)
            if turn.duration_s < 0 or edge.duration_s < 0 or edge.distance_m < 0:
                return None, None
            duration += turn.duration_s + edge.duration_s
            distance += edge.distance_m
            order, mask = update_order(order, mask, edge.city_mask, bit_to_city)
            if not order_prefix_valid(order, nominal):
                return None, None
            previous_edge_id = edge_id
        if list(order) != nominal:
            return None, None
        return round(duration, 1), round(distance, 3)
    start_cost = start_edge.duration_s
    start_length = start_edge.distance_m
    snap = kwargs.get("start_snap_fraction")
    if snap is not None:
        interval = start_edge.end_fraction - start_edge.start_fraction
        if interval <= 0 or not start_edge.start_fraction <= snap <= start_edge.end_fraction:
            return None, None
        start_cost = 0.0
        start_length = 0.0
    start_state = (start_edge_id, len(actual), visited)
    costs = {start_state: start_cost}
    lengths = {start_state: start_length}
    heap = [(start_cost, start_edge_id, len(actual), visited)]
    target_mask = sum(1 << city_to_bit[city] for city in nominal)
    while heap:
        cost, edge_id, progress, mask = heapq.heappop(heap)
        state = (edge_id, progress, mask)
        if cost != costs.get(state):
            continue
        if progress == len(nominal) and mask & target_mask == target_mask:
            return round(cost, 1), round(lengths[state], 3)
        for turn in outgoing_turns(adjacency, edge_id):
            outgoing = get_edge(edges, turn.outgoing)
            if outgoing is None:
                raise ValueError(f"checker turn points to missing split edge {turn.outgoing}")
            new_bits = outgoing.city_mask & ~mask
            names = names_from_mask(new_bits, bit_to_city)
            if names != nominal[progress:progress + len(names)]:
                continue
            next_mask = mask | outgoing.city_mask
            if next_mask & ~target_mask:
                continue
            next_state = (outgoing.edge_id, progress + len(names), next_mask)
            next_cost = cost + turn.duration_s + outgoing.duration_s
            if next_cost < costs.get(next_state, float("inf")):
                costs[next_state] = next_cost
                lengths[next_state] = lengths[state] + outgoing.distance_m
                heapq.heappush(heap, (next_cost, outgoing.edge_id, progress + len(names), next_mask))
    return None, None


def evaluate_with_checker(**kwargs) -> RouteResult:
    primary = evaluate_attempt(**kwargs)
    if primary.feasibility != "FEASIBLE_FIXED_ORDER":
        return primary
    checker_started = time.monotonic()
    checker_duration, checker_distance = independent_fixed_order_checker(
        **kwargs, split_edge_path=primary.split_edge_path
    )
    checker_wall_s = time.monotonic() - checker_started
    if checker_duration is None or checker_distance is None:
        primary.feasibility = "INDEPENDENT_CHECK_FAILED"
        primary.result_category = "OTHER_FAILURE"
        primary.failure_reason = "independent fixed-order checker found no matching feasible path"
        return primary
    delta = abs((primary.total_duration_s or 0.0) - checker_duration)
    distance_delta = abs((primary.total_distance_m or 0.0) - checker_distance)
    primary.checker_duration_s = checker_duration
    primary.provenance["independent_checker_wall_s"] = round(checker_wall_s, 6)
    primary.provenance["independent_checker_distance_delta_m"] = round(distance_delta, 6)
    if primary.provenance.get("algorithm") == "weighted_astar_feasible_path":
        # Weighted A* is used only to discover an upper-bound witness.  Because
        # labels may reopen, its mutable predecessor tree can encode a cheaper
        # final path than the tentative goal label.  The independent full-path
        # replay is the authoritative exact cost certificate.
        primary.provenance["tentative_search_duration_s"] = primary.total_duration_s
        primary.provenance["tentative_search_distance_m"] = primary.total_distance_m
        primary.total_duration_s = checker_duration
        primary.total_distance_m = checker_distance
        if primary.segment_costs:
            primary.segment_costs[0]["internal_city_time_s"] = checker_duration
            primary.segment_costs[0]["internal_city_distance_m"] = checker_distance
        primary.fixed_order_check_delta_s = 0.0
        return primary
    primary.fixed_order_check_delta_s = round(delta, 3)
    if delta > 1.0 or distance_delta > 0.001:
        primary.feasibility = "INDEPENDENT_CHECK_FAILED"
        primary.result_category = "OTHER_FAILURE"
        primary.failure_reason = (
            f"independent fixed-order checker differs by {delta:.3f}s/{distance_delta:.6f}m"
        )
    return primary


def replay_certified_path(template: RouteResult, **kwargs) -> RouteResult | None:
    """Reuse a fully checked split-edge path against another model's masks.

    Edge costs and legal turns are model-independent; visit masks are not.  The
    independent checker therefore replays every edge before a path discovered
    for one model may certify another model.
    """
    if not template.split_edge_path:
        return None
    started = time.monotonic()
    duration, distance = independent_fixed_order_checker(
        **kwargs, split_edge_path=template.split_edge_path
    )
    wall_s = time.monotonic() - started
    if duration is None or distance is None:
        return None
    delta = abs(duration - (template.total_duration_s or 0.0))
    distance_delta = abs(distance - (template.total_distance_m or 0.0))
    if delta > 1.0 or distance_delta > 0.001:
        return None
    provenance = dict(template.provenance)
    provenance.update({
        "algorithm": "cross_model_full_path_replay",
        "source_model": template.model_id,
        "independent_checker_wall_s": round(wall_s, 6),
        "independent_checker_distance_delta_m": round(distance_delta, 6),
    })
    return replace(
        template,
        model_id=kwargs["model_id"],
        route_id=kwargs["route_id"],
        source_heuristic=kwargs["source_heuristic"],
        seed=kwargs["seed"],
        nominal_order=[normalise_city(city) for city in kwargs["nominal_order"]],
        actual_first_hit_order=[normalise_city(city) for city in kwargs["nominal_order"]],
        checker_duration_s=duration,
        fixed_order_check_delta_s=round(delta, 3),
        provenance=provenance,
    )


def write_evaluation_outcomes(path: Path, results: list[RouteResult]) -> dict[str, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    counts = Counter(result.result_category for result in results)
    unexpected = sorted(set(counts) - set(OUTCOME_CATEGORIES))
    if unexpected:
        raise ValueError(f"unknown Stage 3 outcome categories: {unexpected}")
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow([
            "candidate_order_id", "nominal_order", "model", "terminal_city", "result_category",
            "exact_duration_s", "exact_distance_m", "failure_reason", "failing_city_transition_or_state",
            "first_hit_order", "exception_error_text",
        ])
        for result in results:
            writer.writerow([
                result.route_id,
                " -> ".join(result.nominal_order),
                result.model_id,
                result.nominal_order[-1] if result.nominal_order else "",
                result.result_category,
                "" if result.total_duration_s is None else result.total_duration_s,
                "" if result.total_distance_m is None else result.total_distance_m,
                result.failure_reason,
                result.failing_transition_or_state,
                " -> ".join(result.actual_first_hit_order),
                result.exception_text,
            ])
    if sum(counts.values()) != len(results):
        raise AssertionError("outcome classification is not exhaustive")
    return {category: counts.get(category, 0) for category in OUTCOME_CATEGORIES}


def write_top_candidates(path: Path, results: list[RouteResult], fixed: dict[str, RouteResult]) -> dict:
    unique: dict[tuple[str, ...], RouteResult] = {}
    invalid = 0
    for result in results:
        key = tuple(result.actual_first_hit_order)
        if result.feasibility == "FEASIBLE_FIXED_ORDER":
            if key not in unique or (result.total_duration_s or float("inf")) < (unique[key].total_duration_s or float("inf")):
                unique[key] = result
        elif result.feasibility == "ORDER_INVALID_BY_FIRST_HIT":
            invalid += 1
    ranked = sorted(unique.values(), key=lambda value: value.total_duration_s or float("inf"))
    payload = {
        "BEST_FOUND_FEASIBLE": route_to_json(ranked[0]) if ranked else None,
        "SECOND_BEST_FOUND": route_to_json(ranked[1]) if len(ranked) > 1 else None,
        "THIRD_BEST_FOUND": route_to_json(ranked[2]) if len(ranked) > 2 else None,
        "best_second_gap_s": (
            round((ranked[1].total_duration_s or 0) - (ranked[0].total_duration_s or 0), 1)
            if len(ranked) > 1 else None
        ),
        "fixed_orders": {name: route_to_json(value) for name, value in fixed.items()},
        "unique_feasible_orders": len(unique),
        "invalid_first_hit_orders": invalid,
        "exact_fixed_order_evaluations": len(results),
        "stage4_exact_claim": "forbidden",
    }
    json_dump(path, payload)
    return payload


def write_finishes(path: Path, finishes: dict[str, RouteResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow([
            "terminal_city", "result_category", "best_known_feasible_cost_s", "distance_m", "city_order",
            "actual_first_hit_order", "failure_reason", "failing_transition_or_state", "provenance",
        ])
        for terminal, result in finishes.items():
            writer.writerow([
                terminal,
                result.result_category,
                "" if result.total_duration_s is None else result.total_duration_s,
                "" if result.total_distance_m is None else result.total_distance_m,
                " -> ".join(result.nominal_order),
                " -> ".join(result.actual_first_hit_order),
                result.failure_reason,
                result.failing_transition_or_state,
                json.dumps(result.provenance, ensure_ascii=False, separators=(",", ":")),
            ])


def write_report(
    path: Path,
    summaries: dict[str, dict],
    fixed: dict[str, dict[str, RouteResult]],
    performance: dict,
    statuses: list[str],
    outcome_counts: dict[str, dict[str, int]],
) -> None:
    lines = ["# Stage 3 validation", ""]
    for status in statuses:
        lines.append(f"- **{status}**")
    lines.append("")
    for model_id in MODEL_ORDER:
        top = summaries[model_id]
        rows = [
            ("A outcome", diagnostic_value(fixed[model_id]["A"])),
            ("B outcome", diagnostic_value(fixed[model_id]["B"])),
            ("C outcome", diagnostic_value(fixed[model_id]["C"])),
            ("best found cost", value_at(top, "BEST_FOUND_FEASIBLE", "total_duration_s")),
            ("best found nominal order", join_order(value_at(top, "BEST_FOUND_FEASIBLE", "nominal_order"))),
            ("actual first-hit order", join_order(value_at(top, "BEST_FOUND_FEASIBLE", "actual_first_hit_order"))),
            ("second best found", value_at(top, "SECOND_BEST_FOUND", "total_duration_s")),
            ("third best found", value_at(top, "THIRD_BEST_FOUND", "total_duration_s")),
            ("best terminal city found", performance[model_id].get("best_terminal_city")),
            ("second terminal city found", performance[model_id].get("second_terminal_city")),
            ("number of exact fixed-order evaluations", top["exact_fixed_order_evaluations"]),
            ("number of unique feasible orders", top["unique_feasible_orders"]),
            ("number of invalid first-hit orders", top["invalid_first_hit_orders"]),
            ("runtime", performance[model_id]["wall_time_s"]),
            ("peak RAM", performance[model_id]["peak_rss_kb"]),
        ]
        lines.extend([f"## {model_id}", "", "| metric | value |", "| --- | --- |"])
        for key, value in rows:
            lines.append(f"| {key} | {value if value is not None else 'n/a'} |")
        lines.append("")
        lines.extend(["### Evaluation outcomes", "", "| category | count |", "| --- | ---: |"])
        for category in OUTCOME_CATEGORIES:
            lines.append(f"| {category} | {outcome_counts[model_id][category]} |")
        lines.append(f"| **total** | **{sum(outcome_counts[model_id].values())}** |")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def diagnostic_value(result: RouteResult) -> str:
    if result.result_category == "FEASIBLE":
        return f"FEASIBLE; duration_s={result.total_duration_s}; distance_m={result.total_distance_m}"
    if result.result_category == "ORDER_INVALID_BY_FIRST_HIT":
        return "ORDER_INVALID_BY_FIRST_HIT; actual=" + " -> ".join(result.actual_first_hit_order)
    return f"{result.result_category}; {result.failure_reason}; proof={result.failing_transition_or_state}"


def value_at(document: dict, key: str, field: str):
    value = document.get(key)
    return None if value is None else value.get(field)


def join_order(value) -> str | None:
    return None if value is None else " -> ".join(value)


def sha256sums(root: Path) -> None:
    with (root / "SHA256SUMS").open("w", encoding="utf-8") as output:
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.name != "SHA256SUMS":
                output.write(f"{digest(path)}  {path.relative_to(root).as_posix()}\n")


def validate_loader(
    graph: SQLiteGraph,
    *,
    model_id: str,
    start_edge_id: str,
    expected_edges: int,
    expected_turns: int,
    city_count: int,
) -> dict:
    connection = graph.connection
    edge_count = connection.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    turn_count = connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    dangling_outgoing = connection.execute(
        "SELECT COUNT(*) FROM turns t LEFT JOIN edges e ON e.edge_id=t.outgoing WHERE e.edge_id IS NULL"
    ).fetchone()[0]
    dangling_incoming = connection.execute(
        "SELECT COUNT(*) FROM turns t LEFT JOIN edges e ON e.edge_id=t.incoming WHERE e.edge_id IS NULL"
    ).fetchone()[0]
    invalid_cost_edges = connection.execute(
        "SELECT COUNT(*) FROM edges WHERE duration_s < 0 OR distance_m < 0 OR duration_s != duration_s OR distance_m != distance_m"
    ).fetchone()[0]
    invalid_cost_turns = connection.execute(
        "SELECT COUNT(*) FROM turns WHERE duration_s < 0 OR duration_s != duration_s"
    ).fetchone()[0]
    geometry_turn_count = connection.execute("SELECT COUNT(*) FROM geometry_turns").fetchone()[0]
    start_out_degree = sum(1 for _ in graph.outgoing_turns(start_edge_id))
    mask_bits = {
        bit for bit in range(city_count)
        if connection.execute("SELECT 1 FROM edges WHERE (city_mask & ?) != 0 LIMIT 1", (1 << bit,)).fetchone()
    }
    gate_bits = {row[0] for row in connection.execute("SELECT DISTINCT bit FROM city_gate_edges")}
    start_present = graph.get_edge(start_edge_id) is not None
    checks = {
        "model": model_id,
        "split_edges": {"expected": expected_edges, "actual": edge_count, "ok": edge_count == expected_edges},
        "turn_transitions": {"expected": expected_turns, "actual": turn_count, "ok": turn_count == expected_turns},
        "implicit_geometry_transitions": {
            "actual": geometry_turn_count,
            "start_out_degree": start_out_degree,
            "ok": geometry_turn_count > 0 and start_out_degree > 0,
        },
        "gate_states": {"bits": sorted(gate_bits), "ok": gate_bits == set(range(city_count))},
        "city_bitmasks": {"bits": sorted(mask_bits), "ok": mask_bits == set(range(city_count))},
        "start_state": {"edge_id": start_edge_id, "ok": start_present},
        "terminal_labels": {"count": city_count, "free_road_state_after_all_visits": True, "ok": True},
        "model_depth_membership": {"loaded_model": graph.metadata().get("model_id"), "ok": graph.metadata().get("model_id") == model_id},
        "dangling_turn_endpoints": {"incoming": dangling_incoming, "outgoing": dangling_outgoing, "ok": not dangling_incoming and not dangling_outgoing},
        "finite_nonnegative_costs": {"edges_invalid": invalid_cost_edges, "turns_invalid": invalid_cost_turns, "ok": not invalid_cost_edges and not invalid_cost_turns},
    }
    failed = [name for name, value in checks.items() if isinstance(value, dict) and not value.get("ok", False)]
    checks["status"] = "SQLITE_LOADER_CERTIFIED" if not failed else "SQLITE_LOADER_FAILED"
    checks["failed_checks"] = failed
    return checks


def certify_split_path(
    graph: Graph,
    path: list[str],
    *,
    initial_mask: int,
    bit_to_city: dict[int, str],
) -> dict:
    if not path:
        raise ValueError("cannot certify an empty split-edge path")
    total_duration = 0.0
    total_distance = 0.0
    accumulated_mask = initial_mask
    transitions = []
    for index, edge_id in enumerate(path):
        edge = graph.get_edge(edge_id)
        if edge is None:
            raise ValueError(f"path edge is missing: {edge_id}")
        if edge.duration_s < 0 or edge.distance_m < 0:
            raise ValueError(f"negative edge cost: {edge_id}")
        total_duration += edge.duration_s
        total_distance += edge.distance_m
        accumulated_mask |= edge.city_mask
        if index:
            previous = graph.get_edge(path[index - 1])
            assert previous is not None
            legal = [turn for turn in graph.outgoing_turns(previous.edge_id) if turn.outgoing == edge_id]
            if not legal:
                raise ValueError(f"illegal transition {previous.edge_id}->{edge_id}")
            if (previous.to_lon, previous.to_lat) != (edge.from_lon, edge.from_lat):
                raise ValueError(f"discontinuous topology {previous.edge_id}->{edge_id}")
            turn = min(legal, key=lambda value: value.duration_s)
            if turn.duration_s < 0:
                raise ValueError(f"negative turn cost {previous.edge_id}->{edge_id}")
            total_duration += turn.duration_s
            transitions.append({"incoming": previous.edge_id, "outgoing": edge_id, "category": turn.category})
    return {
        "status": "KNOWN_PATH_CERTIFIED",
        "path": path,
        "continuous_topology": True,
        "legal_turns": True,
        "finite_nonnegative_cost": True,
        "duration_s": round(total_duration, 3),
        "distance_m": round(total_distance, 6),
        "initial_mask": initial_mask,
        "accumulated_mask": accumulated_mask,
        "accumulated_cities": names_from_mask(accumulated_mask, bit_to_city),
        "transitions": transitions,
    }


def connectivity_sanity(
    graph: SQLiteGraph,
    *,
    start_edge_id: str,
    city_to_bit: dict[str, int],
    max_states: int = 40_000_000,
) -> dict:
    edge_gate_bits: dict[str, set[int]] = {}
    for bit, edge_id in graph.connection.execute("SELECT bit, edge_id FROM city_gate_edges"):
        edge_gate_bits.setdefault(edge_id, set()).add(bit)

    def search(reverse: bool) -> tuple[dict[int, str], int]:
        witnesses: dict[int, str] = {}
        visited = {start_edge_id}
        frontier = deque([start_edge_id])
        column = "incoming" if not reverse else "outgoing"
        selected = "outgoing" if not reverse else "incoming"
        while frontier and len(witnesses) < len(city_to_bit):
            batch = []
            while frontier and len(batch) < 500:
                batch.append(frontier.popleft())
            for edge_id in batch:
                for bit in edge_gate_bits.get(edge_id, ()):
                    witnesses.setdefault(bit, edge_id)
            placeholders = ",".join("?" for _ in batch)
            geometry_column = "incoming" if not reverse else "outgoing"
            geometry_selected = "outgoing" if not reverse else "incoming"
            query = (
                f"SELECT {selected} FROM turns WHERE {column} IN ({placeholders}) "
                f"UNION ALL SELECT {geometry_selected} FROM geometry_turns "
                f"WHERE {geometry_column} IN ({placeholders})"
            )
            for (edge_id,) in graph.connection.execute(query, [*batch, *batch]):
                if edge_id not in visited:
                    visited.add(edge_id)
                    frontier.append(edge_id)
            if len(visited) > max_states:
                raise TimeoutError(f"connectivity sanity exceeded {max_states} road states")
        return witnesses, len(visited)

    try:
        forward, forward_states = search(False)
        reverse, reverse_states = search(True)
    except TimeoutError as error:
        return {"status": "CONNECTIVITY_SANITY_TIMEOUT", "reason": str(error)}
    expected_bits = set(city_to_bit.values())
    missing_forward = sorted(expected_bits - set(forward))
    missing_reverse = sorted(expected_bits - set(reverse))
    if missing_forward or missing_reverse:
        return {
            "status": "DIRECTED_GATE_CONNECTIVITY_FAILED",
            "missing_start_to_city_bits": missing_forward,
            "missing_city_to_start_bits": missing_reverse,
            "forward_states_examined": forward_states,
            "reverse_states_examined": reverse_states,
        }
    pair_witnesses = []
    cities = sorted(city_to_bit, key=city_to_bit.get)
    for source in cities:
        for target in cities:
            if source == target:
                continue
            source_bit = city_to_bit[source]
            target_bit = city_to_bit[target]
            pair_witnesses.append({
                "source": source,
                "target": target,
                "source_gate_to_start_witness": reverse[source_bit],
                "start_to_target_gate_witness": forward[target_bit],
            })
    return {
        "status": "DIRECTED_GATE_CONNECTIVITY_CERTIFIED",
        "proof": (
            "For every city cluster, reverse search found a gate that reaches the certified start and "
            "forward search found a gate reachable from start; concatenation proves an allowed directed "
            "path between every ordered pair of city gate clusters."
        ),
        "start_to_each_other_city": {city: forward[bit] for city, bit in city_to_bit.items() if bit != 0},
        "each_city_to_start": {city: reverse[bit] for city, bit in city_to_bit.items() if bit != 0},
        "ordered_city_pairs": pair_witnesses,
        "forward_states_examined": forward_states,
        "reverse_states_examined": reverse_states,
        "A_first_transition": [ROUTES["A"][0], ROUTES["A"][1]],
        "B_first_transition": [ROUTES["B"][0], ROUTES["B"][1]],
        "C_first_transition": [ROUTES["C"][0], ROUTES["C"][1]],
        "A_last_transition": ROUTES["A"][-2:],
        "B_last_transition": ROUTES["B"][-2:],
        "C_last_transition": ROUTES["C"][-2:],
    }


def compare_sqlite_to_in_memory_reference(
    graph: SQLiteGraph,
    *,
    path: list[str],
    model_id: str,
    city_to_bit: dict[str, int],
    bit_to_city: dict[int, str],
    initial_mask: int,
) -> dict:
    selected = set(path)
    edges = {edge_id: graph.get_edge(edge_id) for edge_id in path}
    if any(edge is None for edge in edges.values()):
        raise ValueError("reference subgraph includes a missing edge")
    typed_edges = {edge_id: edge for edge_id, edge in edges.items() if edge is not None}
    adjacency = {
        edge_id: [turn for turn in graph.outgoing_turns(edge_id) if turn.outgoing in selected]
        for edge_id in path
    }
    reference = DictGraph(typed_edges, adjacency)

    class FilteredSQLiteGraph:
        def get_edge(self, edge_id: str) -> Edge | None:
            return graph.get_edge(edge_id) if edge_id in selected else None

        def outgoing_turns(self, edge_id: str) -> Iterable[Turn]:
            return (turn for turn in graph.outgoing_turns(edge_id) if turn.outgoing in selected)

        def first_edge_with_mask(self, mask: int) -> str | None:
            return next((edge_id for edge_id in path if typed_edges[edge_id].city_mask & mask), None)

        def close(self) -> None:
            return None

    order, _ = update_order((), 0, initial_mask, bit_to_city)
    visited = initial_mask
    for edge_id in path:
        order, visited = update_order(order, visited, typed_edges[edge_id].city_mask, bit_to_city)
    nominal_order = list(order) or [bit_to_city[0]]
    kwargs = dict(
        model_id=model_id,
        route_id="deterministic-reference-subgraph",
        nominal_order=nominal_order,
        city_to_bit=city_to_bit,
        bit_to_city=bit_to_city,
        start_edge_id=path[0],
        source_heuristic="stage3.1-reference",
        seed=SEED,
        initial_visited_mask=initial_mask,
    )
    memory_result = evaluate_fixed_order(edges=reference, adjacency=reference, **kwargs)
    sqlite_view = FilteredSQLiteGraph()
    sqlite_result = evaluate_fixed_order(edges=sqlite_view, adjacency=sqlite_view, **kwargs)
    same = (
        memory_result.result_category == sqlite_result.result_category
        and memory_result.total_duration_s == sqlite_result.total_duration_s
        and memory_result.total_distance_m == sqlite_result.total_distance_m
    )
    if not same:
        raise ValueError("SQLite-backed and in-memory reference results differ")
    return {
        "status": "SQLITE_IN_MEMORY_REFERENCE_AGREES",
        "path": path,
        "nominal_order": nominal_order,
        "sqlite": route_to_json(sqlite_result),
        "in_memory": route_to_json(memory_result),
        "same_feasibility_and_cost": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage2b-artifacts", required=True, type=Path)
    parser.add_argument("--stage2b-results", default=Path("results/graph-prg"), type=Path)
    parser.add_argument("--output", default=Path("results/stage3"), type=Path)
    args = parser.parse_args()
    repository = Path(__file__).resolve().parent.parent
    manifest = load_manifest(repository)
    cities = [normalise_city(city["city"]) for city in manifest["cities"]["canonical_bit_order"]]
    city_to_bit = {normalise_city(city["city"]): int(city["bit_index"]) for city in manifest["cities"]["canonical_bit_order"]}
    bit_to_city = {bit: city for city, bit in city_to_bit.items()}
    start_certificate = json.loads((args.stage2b_results / "start-state-certificate.json").read_text(encoding="utf-8"))
    fixed_by_model: dict[str, dict[str, RouteResult]] = {}
    top_summaries: dict[str, dict] = {}
    performance: dict[str, dict] = {}
    local_checks: dict[str, dict] = {}
    stage_started = time.perf_counter()
    args.output.mkdir(parents=True, exist_ok=True)
    historical_report = args.output / "validation-report.md"
    if historical_report.exists():
        historical_dir = args.output / "historical"
        historical_dir.mkdir(parents=True, exist_ok=True)
        historical_copy = historical_dir / "validation-report-pre-stage3.1.md"
        if not historical_copy.exists():
            historical_copy.write_text(
                "<!-- INVALID_CERTIFICATE_ZERO_FEASIBLE_ROUTES -->\n\n"
                + historical_report.read_text(encoding="utf-8"),
                encoding="utf-8",
            )

    split_summary = json.loads((args.stage2b_results / "split-edges-summary.json").read_text(encoding="utf-8"))
    turn_summary = json.loads((args.stage2b_results / "turn-states-summary.json").read_text(encoding="utf-8"))
    expected_edges = int(split_summary["split_edges"])
    expected_turns = int(turn_summary["inherited_stage2a_turns"]) + int(turn_summary["internal_split_continuations"])
    loader_audits: dict[str, dict] = {}
    path_certificates: dict[str, dict] = {}
    connectivity_certificates: dict[str, dict] = {}
    reference_comparisons: dict[str, dict] = {}
    outcome_counts: dict[str, dict[str, int]] = {}

    for model_id in MODEL_ORDER:
        model_started = time.perf_counter()
        graph_path = args.output / "work" / f"{model_id}.sqlite"
        graph = build_sqlite_graph(args.stage2b_artifacts, model_id, graph_path)
        edges: dict[str, Edge] | Graph = graph
        adjacency: dict[str, list[Turn]] | Graph = graph
        start_edge_id = start_certificate["child_edge_id"]
        if get_edge(edges, start_edge_id) is None:
            raise ValueError(f"Stage 2B start state differs from split graph: {start_edge_id}")
        try:
            loader_audits[model_id] = validate_loader(
                graph,
                model_id=model_id,
                start_edge_id=start_edge_id,
                expected_edges=expected_edges,
                expected_turns=expected_turns,
                city_count=len(cities),
            )
            print(f"stage3: running directed connectivity sanity for {model_id}", flush=True)
            connectivity_certificates[model_id] = connectivity_sanity(
                graph, start_edge_id=start_edge_id, city_to_bit=city_to_bit
            )
            json_dump(args.output / "connectivity-sanity.partial.json", connectivity_certificates)
            json_dump(args.output / "sqlite-loader-audit.partial.json", loader_audits)
            print(
                f"stage3: connectivity outcome for {model_id}: "
                f"{connectivity_certificates[model_id]['status']}",
                flush=True,
            )
            initial_mask = int(start_certificate["model_initial_visited_masks"][model_id])
            snap_fraction = float(start_certificate["snap_fraction"])
            fixed: dict[str, RouteResult] = {}
            for route_id, order in ROUTES.items():
                print(f"stage3: evaluating mandatory {model_id}/{route_id}", flush=True)
                fixed_kwargs = dict(
                    edges=edges, adjacency=adjacency, model_id=model_id, route_id=route_id,
                    nominal_order=order, city_to_bit=city_to_bit, bit_to_city=bit_to_city,
                    start_edge_id=start_edge_id, source_heuristic=f"fixed-route-{route_id}", seed=SEED,
                    initial_visited_mask=initial_mask, start_snap_fraction=snap_fraction,
                    timeout_s=FIXED_ORDER_TIMEOUT_S,
                    heuristic_weight=UPPER_BOUND_HEURISTIC_WEIGHT,
                )
                template = fixed_by_model.get("M-boundary", {}).get(route_id)
                result = replay_certified_path(template, **fixed_kwargs) if template is not None else None
                if result is None:
                    result = evaluate_with_checker(**fixed_kwargs)
                fixed[route_id] = result
                print(
                    f"stage3: mandatory {model_id}/{route_id}: {result.result_category}; "
                    f"duration_s={result.total_duration_s}",
                    flush=True,
                )
                json_dump(args.output / "fixed-orders" / f"{model_id}-{route_id}.json", route_to_json(result))
            fixed_by_model[model_id] = fixed

            evaluations = list(fixed.values())
            order_cache: dict[tuple[str, ...], RouteResult] = {
                tuple(result.nominal_order): result for result in fixed.values()
            }

            def scheduled_evaluation(route_id: str, source: str, order: list[str]) -> RouteResult:
                key = tuple(order)
                cached = order_cache.get(key)
                if cached is not None:
                    provenance = dict(cached.provenance)
                    provenance["scheduled_result_reused_from"] = cached.route_id
                    return replace(
                        cached,
                        route_id=route_id,
                        source_heuristic=source,
                        provenance=provenance,
                    )
                result = evaluate_attempt(
                    edges=edges, adjacency=adjacency, model_id=model_id, route_id=route_id,
                    nominal_order=order, city_to_bit=city_to_bit, bit_to_city=bit_to_city,
                    start_edge_id=start_edge_id, source_heuristic=source, seed=SEED,
                    initial_visited_mask=initial_mask, start_snap_fraction=snap_fraction,
                    timeout_s=GENERAL_EVALUATION_TIMEOUT_S,
                    heuristic_weight=UPPER_BOUND_HEURISTIC_WEIGHT,
                )
                order_cache[key] = result
                return result

            candidates = candidate_orders(cities)
            for source, order in candidates:
                evaluations.append(scheduled_evaluation(source, source, order))
            finishes: dict[str, RouteResult] = {}
            for terminal in cities[1:]:
                best: RouteResult | None = None
                terminal_attempts: list[RouteResult] = []
                for source, order in terminal_orders(candidates, terminal)[:8]:
                    result = scheduled_evaluation(f"{source}-finish", source, order)
                    evaluations.append(result)
                    terminal_attempts.append(result)
                    if result.feasibility == "FEASIBLE_FIXED_ORDER" and (
                        best is None or (result.total_duration_s or float("inf")) < (best.total_duration_s or float("inf"))
                    ):
                        best = result
                if best is None:
                    best = min(
                        terminal_attempts,
                        key=lambda value: (
                            OUTCOME_CATEGORIES.index(value.result_category),
                            -len(value.actual_first_hit_order),
                            value.route_id,
                        ),
                    )
                finishes[terminal] = best
            if len(evaluations) != 197:
                raise AssertionError(f"expected exactly 197 evaluations for {model_id}, got {len(evaluations)}")
            outcome_counts[model_id] = write_evaluation_outcomes(
                args.output / f"evaluation-outcomes-{model_id}.csv", evaluations
            )
            write_finishes(args.output / "finishes" / f"{model_id}-finishes.csv", finishes)
            top_summaries[model_id] = write_top_candidates(args.output / "candidates" / f"{model_id}-top.json", evaluations, fixed)

            terminal_rank = sorted(
                (value for value in finishes.values() if value.result_category == "FEASIBLE"),
                key=lambda value: value.total_duration_s or float("inf"),
            )
            performance[model_id] = {
                "wall_time_s": round(time.perf_counter() - model_started, 3),
                "cpu_time_s": round(time.process_time(), 3),
                "peak_rss_kb": peak_rss_kb(),
                "cache_hit_rates": {"fixed_order_solver": None},
                "best_terminal_city": terminal_rank[0].nominal_order[-1] if terminal_rank else None,
                "second_terminal_city": terminal_rank[1].nominal_order[-1] if len(terminal_rank) > 1 else None,
            }

            for check_id, order in LOCAL_CHECKS.items():
                local_start_edge_id = find_city_start_edge(edges, normalise_city(order[0]), city_to_bit)
                local = evaluate_attempt(
                    edges=edges, adjacency=adjacency, model_id=model_id, route_id=check_id,
                    nominal_order=order, city_to_bit=city_to_bit, bit_to_city=bit_to_city,
                    start_edge_id=local_start_edge_id, source_heuristic="mandatory-local-check", seed=SEED,
                    timeout_s=LOCAL_CHECK_TIMEOUT_S,
                    heuristic_weight=UPPER_BOUND_HEURISTIC_WEIGHT,
                )
                local_checks[f"{model_id}:{check_id}"] = route_to_json(local)
            representative_path = next(
                (result.split_edge_path for result in evaluations if result.split_edge_path), None
            )
            if representative_path:
                short_path = representative_path[: min(8, len(representative_path))]
            else:
                first_turn = next(iter(graph.outgoing_turns(start_edge_id)), None)
                short_path = [start_edge_id] + ([first_turn.outgoing] if first_turn else [])
            path_certificates[model_id] = certify_split_path(
                graph, short_path, initial_mask=initial_mask, bit_to_city=bit_to_city
            )
            reference_comparisons[model_id] = compare_sqlite_to_in_memory_reference(
                graph,
                path=short_path,
                model_id=model_id,
                city_to_bit=city_to_bit,
                bit_to_city=bit_to_city,
                initial_mask=initial_mask,
            )
        finally:
            graph.close()
            graph_path.unlink(missing_ok=True)

    stage2b_manifest = args.stage2b_results / "stage2b-manifest.json"
    per_model_feasible = {
        model_id: outcome_counts[model_id]["FEASIBLE"] > 0 for model_id in MODEL_ORDER
    }
    fixed_resolved = all(
        result.result_category in {"FEASIBLE", "ORDER_INVALID_BY_FIRST_HIT", "UNREACHABLE"}
        for model_results in fixed_by_model.values()
        for result in model_results.values()
    )
    audits_certified = all(
        loader_audits[model_id]["status"] == "SQLITE_LOADER_CERTIFIED"
        and connectivity_certificates[model_id]["status"] == "DIRECTED_GATE_CONNECTIVITY_CERTIFIED"
        and path_certificates[model_id]["status"] == "KNOWN_PATH_CERTIFIED"
        and reference_comparisons[model_id]["status"] == "SQLITE_IN_MEMORY_REFERENCE_AGREES"
        for model_id in MODEL_ORDER
    )
    local_checks_complete = len(local_checks) == len(MODEL_ORDER) * len(LOCAL_CHECKS)
    stage_success = all(per_model_feasible.values()) and fixed_resolved and audits_certified and local_checks_complete
    statuses = [*UNCONDITIONAL_STATUSES]
    if fixed_resolved:
        statuses = ["FIXED_ORDER_SOLVER_CERTIFIED", "FIRST_HIT_ORDER_CERTIFIED", *statuses]
    if local_checks_complete:
        statuses = ["LOCAL_CHECKS_COMPLETE", *statuses]
    if stage_success:
        statuses = [*SUCCESS_STATUSES, *statuses]
    else:
        statuses = [
            FAILURE_STATUS if not all(per_model_feasible.values()) else VALIDATION_FAILURE_STATUS,
            *statuses,
        ]
    stage3_manifest = {
        "schema_version": 1,
        "statuses": statuses,
        "seed": SEED,
        "models": list(MODEL_ORDER),
        "model_aliases": MODEL_ALIASES,
        "stage2b_manifest_sha256": digest(stage2b_manifest) if stage2b_manifest.exists() else None,
        "stage2b_artifact_hashes": {
            path.name: {"sha256": digest(path), "size_bytes": path.stat().st_size}
            for path in sorted(args.stage2b_artifacts.iterdir()) if path.is_file()
        },
        "stage4_exact_claim": "forbidden",
        "wall_time_s": round(time.perf_counter() - stage_started, 3),
        "per_model_feasible": per_model_feasible,
        "evaluation_outcome_counts": outcome_counts,
    }
    json_dump(args.output / "stage3-manifest.json", stage3_manifest)
    json_dump(args.output / "local-checks.json", local_checks)
    json_dump(args.output / "sqlite-loader-audit.json", loader_audits)
    json_dump(args.output / "connectivity-sanity.json", connectivity_certificates)
    json_dump(args.output / "known-path-certificates.json", path_certificates)
    json_dump(args.output / "sqlite-in-memory-reference.json", reference_comparisons)
    json_dump(args.output / "performance.json", performance)
    write_report(
        args.output / "validation-report.md", top_summaries, fixed_by_model, performance,
        statuses, outcome_counts,
    )
    sha256sums(args.output)
    failure = FAILURE_STATUS if not all(per_model_feasible.values()) else VALIDATION_FAILURE_STATUS
    print("UPPER_BOUND_STAGE_OK" if stage_success else failure)
    print("EXACT_GLOBAL_SOLVER_NOT_STARTED")
    return 0 if stage_success else 2


if __name__ == "__main__":
    raise SystemExit(main())
