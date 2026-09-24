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
import time
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Iterator, Protocol

MODEL_ORDER = ("M-boundary", "M-300", "M-500")
MODEL_ALIASES = {"A": "M-boundary", "B": "M-300", "C": "M-500"}
MODEL_MASK_COLUMN = {"M-boundary": "M_boundary_mask", "M-300": "M_300_mask", "M-500": "M_500_mask"}
SEED = 20260726
STATUSES = [
    "UPPER_BOUND_STAGE_OK",
    "FIXED_ORDER_SOLVER_CERTIFIED",
    "FIRST_HIT_ORDER_CERTIFIED",
    "MANDATORY_ROUTES_EVALUATED",
    "ALL_FINISHES_EVALUATED",
    "LOCAL_CHECKS_COMPLETE",
    "TOP_K_FEASIBLE_CANDIDATES_READY",
    "EXACT_GLOBAL_SOLVER_NOT_STARTED",
]

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


class Graph(Protocol):
    def get_edge(self, edge_id: str) -> Edge | None:
        ...

    def outgoing_turns(self, edge_id: str) -> Iterable[Turn]:
        ...

    def first_edge_with_mask(self, mask: int) -> str | None:
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
            SELECT edge_id, duration_s, distance_m, city_mask, from_lon, from_lat, to_lon, to_lat
            FROM edges WHERE edge_id = ?
            """,
            (edge_id,),
        ).fetchone()
        if row is None:
            return None
        return Edge(row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7])

    def outgoing_turns(self, edge_id: str) -> Iterable[Turn]:
        cursor = self.connection.execute(
            "SELECT outgoing, duration_s, category FROM turns WHERE incoming = ?",
            (edge_id,),
        )
        for outgoing, duration_s, category in cursor:
            yield Turn(outgoing, duration_s, category)

    def first_edge_with_mask(self, mask: int) -> str | None:
        bit = mask.bit_length() - 1
        row = self.connection.execute("SELECT edge_id FROM city_start_edges WHERE bit = ?", (bit,)).fetchone()
        return None if row is None else row[0]

    def close(self) -> None:
        type(self).get_edge.cache_clear()
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
            to_lat TEXT NOT NULL
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
        """
    )
    mask_column = MODEL_MASK_COLUMN[model_id]
    edge_rows: list[tuple] = []
    city_starts: dict[int, str] = {}
    for count, row in enumerate(read_tsv(edges_path), start=1):
        edge_id = row["split_edge_id"]
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
            )
        )
        remaining_mask = city_mask
        while remaining_mask:
            bit_mask = remaining_mask & -remaining_mask
            bit = bit_mask.bit_length() - 1
            previous_edge_id = city_starts.get(bit)
            if previous_edge_id is None or edge_id < previous_edge_id:
                city_starts[bit] = edge_id
            remaining_mask ^= bit_mask
        if len(edge_rows) >= 100_000:
            batch_insert(connection, "INSERT INTO edges VALUES (?, ?, ?, ?, ?, ?, ?, ?)", edge_rows)
        if count % 1_000_000 == 0:
            print(f"stage3: indexed {count:,} split edges for {model_id}", flush=True)
    batch_insert(connection, "INSERT INTO edges VALUES (?, ?, ?, ?, ?, ?, ?, ?)", edge_rows)
    connection.executemany("INSERT INTO city_start_edges VALUES (?, ?)", sorted(city_starts.items()))
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
    connection.commit()
    edge_total = connection.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
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
) -> RouteResult:
    nominal_order = [normalise_city(city) for city in nominal_order]
    target_mask = 0
    for city in nominal_order:
        target_mask |= 1 << city_to_bit[city]
    start_edge = get_edge(edges, start_edge_id)
    if start_edge is None:
        raise ValueError(f"start edge missing from split graph: {start_edge_id}")
    start_order, start_mask = update_order((), 0, start_edge.city_mask, bit_to_city)
    start_city = nominal_order[0]
    if start_city not in start_order:
        start_order = (start_city, *start_order)
        start_mask |= 1 << city_to_bit[start_city]
    if not order_prefix_valid(start_order, nominal_order):
        return invalid_result(model_id, route_id, source_heuristic, seed, nominal_order, list(start_order), "ORDER_INVALID_BY_FIRST_HIT")
    distances: dict[tuple[str, int], float] = {(start_edge_id, start_mask): start_edge.duration_s}
    lengths: dict[tuple[str, int], float] = {(start_edge_id, start_mask): start_edge.distance_m}
    orders: dict[tuple[str, int], tuple[str, ...]] = {(start_edge_id, start_mask): start_order}
    previous: dict[tuple[str, int], tuple[tuple[str, int], Turn]] = {}
    heap = [(start_edge.duration_s, start_edge_id, start_mask)]
    expanded = 0
    best_goal: tuple[str, int] | None = None
    invalid_first_hit: tuple[str, ...] | None = None
    while heap:
        duration, edge_id, visited_mask = heapq.heappop(heap)
        state = (edge_id, visited_mask)
        if duration != distances.get(state):
            continue
        expanded += 1
        actual_order = orders[state]
        if visited_mask & target_mask == target_mask and list(actual_order) == nominal_order:
            best_goal = state
            break
        for turn in outgoing_turns(adjacency, edge_id):
            outgoing = get_edge(edges, turn.outgoing)
            if outgoing is None:
                raise ValueError(f"turn points to missing split edge {turn.outgoing}")
            next_order, next_mask = update_order(actual_order, visited_mask, outgoing.city_mask, bit_to_city)
            if next_mask & ~target_mask:
                continue
            if not order_prefix_valid(next_order, nominal_order):
                invalid_first_hit = next_order
                continue
            next_state = (outgoing.edge_id, next_mask)
            next_duration = duration + turn.duration_s + outgoing.duration_s
            if next_duration < distances.get(next_state, float("inf")):
                distances[next_state] = next_duration
                lengths[next_state] = lengths[state] + outgoing.distance_m
                orders[next_state] = next_order
                previous[next_state] = (state, turn)
                heapq.heappush(heap, (next_duration, outgoing.edge_id, next_mask))
    if best_goal is None:
        if invalid_first_hit is not None:
            return invalid_result(model_id, route_id, source_heuristic, seed, nominal_order, list(invalid_first_hit), "ORDER_INVALID_BY_FIRST_HIT")
        best_seen = max(orders.values(), key=len, default=start_order)
        return invalid_result(model_id, route_id, source_heuristic, seed, nominal_order, list(best_seen), "NO_FEASIBLE_FIXED_ORDER_PATH")
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
            "algorithm": "constrained_product_graph_shortest_path" if checker else "label_setting_fixed_order_dp",
            "expanded_labels": expanded,
            "stage": "Stage 3 upper-bound fixed-order evaluation",
        },
    )


def invalid_result(model_id: str, route_id: str, source_heuristic: str, seed: int, nominal_order: list[str], actual_order: list[str], feasibility: str) -> RouteResult:
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
        provenance={"algorithm": "label_setting_fixed_order_dp", "stage": "Stage 3 upper-bound fixed-order evaluation"},
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
    total_duration = sum(edge.duration_s for edge in path_edges)
    total_distance = sum(edge.distance_m for edge in path_edges)
    return [
        {
            "from_city": nominal_order[0],
            "to_city": nominal_order[-1],
            "entry_stable_edge": path[0],
            "exit_stable_edge": path[-1],
            "internal_city_time_s": round(total_duration, 1),
            "internal_city_distance_m": round(total_distance, 3),
            "transition_time_s": round(distances[goal] - total_duration, 1) if goal in distances else 0.0,
            "transition_distance_m": round(lengths[goal] - total_distance, 3) if goal in lengths else 0.0,
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
    }


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


def evaluate_with_checker(**kwargs) -> RouteResult:
    primary = evaluate_fixed_order(**kwargs)
    if primary.feasibility != "FEASIBLE_FIXED_ORDER":
        return primary
    checker = evaluate_fixed_order(**{**kwargs, "checker": True})
    if checker.feasibility != "FEASIBLE_FIXED_ORDER" or checker.total_duration_s is None:
        primary.feasibility = "INDEPENDENT_CHECK_FAILED"
        return primary
    delta = abs((primary.total_duration_s or 0.0) - checker.total_duration_s)
    primary.checker_duration_s = checker.total_duration_s
    primary.fixed_order_check_delta_s = round(delta, 3)
    if delta > 1.0:
        primary.feasibility = "INDEPENDENT_CHECK_FAILED"
    return primary


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


def write_finishes(path: Path, finishes: dict[str, RouteResult | None]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(["terminal_city", "best_known_feasible_cost_s", "city_order", "entry_exit_state_sequence", "provenance"])
        for terminal, result in finishes.items():
            writer.writerow([
                terminal,
                "" if result is None else result.total_duration_s,
                "" if result is None else " -> ".join(result.nominal_order),
                "" if result is None else json.dumps(result.entry_exit_state_sequence, ensure_ascii=False, separators=(",", ":")),
                "" if result is None else json.dumps(result.provenance, ensure_ascii=False, separators=(",", ":")),
            ])


def write_report(path: Path, summaries: dict[str, dict], fixed: dict[str, dict[str, RouteResult]], performance: dict) -> None:
    lines = ["# Stage 3 validation", ""]
    for status in STATUSES:
        lines.append(f"- **{status}**")
    lines.append("")
    for model_id in MODEL_ORDER:
        top = summaries[model_id]
        rows = [
            ("A cost", fixed[model_id]["A"].total_duration_s),
            ("B cost", fixed[model_id]["B"].total_duration_s),
            ("C cost", fixed[model_id]["C"].total_duration_s),
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
    path.write_text("\n".join(lines), encoding="utf-8")


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
            fixed: dict[str, RouteResult] = {}
            for route_id, order in ROUTES.items():
                result = evaluate_with_checker(
                    edges=edges, adjacency=adjacency, model_id=model_id, route_id=route_id,
                    nominal_order=order, city_to_bit=city_to_bit, bit_to_city=bit_to_city,
                    start_edge_id=start_edge_id, source_heuristic=f"fixed-route-{route_id}", seed=SEED,
                )
                fixed[route_id] = result
                json_dump(args.output / "fixed-orders" / f"{model_id}-{route_id}.json", route_to_json(result))
                if result.feasibility == "INDEPENDENT_CHECK_FAILED":
                    raise ValueError(f"{model_id} route {route_id} independent check failed")
            fixed_by_model[model_id] = fixed

            evaluations = list(fixed.values())
            candidates = candidate_orders(cities)
            for source, order in candidates:
                evaluations.append(evaluate_fixed_order(
                    edges=edges, adjacency=adjacency, model_id=model_id, route_id=source,
                    nominal_order=order, city_to_bit=city_to_bit, bit_to_city=bit_to_city,
                    start_edge_id=start_edge_id, source_heuristic=source, seed=SEED,
                ))
            finishes: dict[str, RouteResult | None] = {}
            for terminal in cities[1:]:
                best: RouteResult | None = None
                for source, order in terminal_orders(candidates, terminal)[:8]:
                    result = evaluate_fixed_order(
                        edges=edges, adjacency=adjacency, model_id=model_id, route_id=f"{source}-finish",
                        nominal_order=order, city_to_bit=city_to_bit, bit_to_city=bit_to_city,
                        start_edge_id=start_edge_id, source_heuristic=source, seed=SEED,
                    )
                    evaluations.append(result)
                    if result.feasibility == "FEASIBLE_FIXED_ORDER" and (
                        best is None or (result.total_duration_s or float("inf")) < (best.total_duration_s or float("inf"))
                    ):
                        best = result
                finishes[terminal] = best
            write_finishes(args.output / "finishes" / f"{model_id}-finishes.csv", finishes)
            top_summaries[model_id] = write_top_candidates(args.output / "candidates" / f"{model_id}-top.json", evaluations, fixed)

            terminal_rank = sorted(
                (value for value in finishes.values() if value is not None),
                key=lambda value: value.total_duration_s or float("inf"),
            )
            performance[model_id] = {
                "wall_time_s": round(time.perf_counter() - model_started, 3),
                "cpu_time_s": round(time.process_time(), 3),
                "peak_rss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                "cache_hit_rates": {"fixed_order_solver": None},
                "best_terminal_city": terminal_rank[0].nominal_order[-1] if terminal_rank else None,
                "second_terminal_city": terminal_rank[1].nominal_order[-1] if len(terminal_rank) > 1 else None,
            }

            for check_id, order in LOCAL_CHECKS.items():
                local_start_edge_id = find_city_start_edge(edges, normalise_city(order[0]), city_to_bit)
                local = evaluate_fixed_order(
                    edges=edges, adjacency=adjacency, model_id=model_id, route_id=check_id,
                    nominal_order=order, city_to_bit=city_to_bit, bit_to_city=bit_to_city,
                    start_edge_id=local_start_edge_id, source_heuristic="mandatory-local-check", seed=SEED,
                )
                local_checks[f"{model_id}:{check_id}"] = route_to_json(local)
        finally:
            graph.close()
            graph_path.unlink(missing_ok=True)

    stage2b_manifest = args.stage2b_results / "stage2b-manifest.json"
    stage3_manifest = {
        "schema_version": 1,
        "statuses": STATUSES,
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
    }
    json_dump(args.output / "stage3-manifest.json", stage3_manifest)
    json_dump(args.output / "local-checks.json", local_checks)
    json_dump(args.output / "performance.json", performance)
    write_report(args.output / "validation-report.md", top_summaries, fixed_by_model, performance)
    sha256sums(args.output)
    print("UPPER_BOUND_STAGE_OK")
    print("EXACT_GLOBAL_SOLVER_NOT_STARTED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
