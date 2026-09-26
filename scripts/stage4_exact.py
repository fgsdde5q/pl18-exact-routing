#!/usr/bin/env python3
"""Stage 4 exact product-graph solver and proof-gate utilities.

All proof-critical costs are integer microseconds.  The module deliberately
contains no conversion from an unverified scalar Stage 3 upper bound: an
incumbent can only be installed together with a replayed edge-path witness.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import heapq
import json
import os
import random
import resource
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_HALF_EVEN
from pathlib import Path
from typing import Iterable, Protocol

COST_SCALE = 1_000_000
COST_UNIT = "microsecond"
SCHEMA_VERSION = 1
SEED = 20260726
START_EDGE = "25857664/4/0/3069840595/611255856@0"
MODELS = ("M-boundary", "M-300", "M-500")
ALL_18 = (1 << 18) - 1
INCOMPLETE = "BEST_FOUND_SOLUTION_GLOBAL_OPTIMALITY_NOT_PROVEN"


def exact_units(value: str | int | Decimal) -> int:
    """Losslessly convert a canonical decimal-second value to microseconds."""
    decimal = Decimal(value)
    scaled = decimal * COST_SCALE
    integral = scaled.to_integral_value(rounding=ROUND_HALF_EVEN)
    if scaled != integral:
        raise ValueError(f"cost {value!r} is not representable in {COST_UNIT}s")
    result = int(integral)
    if result < 0:
        raise ValueError("negative costs are forbidden")
    return result


def seconds(units: int | None) -> float | None:
    return None if units is None else units / COST_SCALE


@dataclass(frozen=True, slots=True)
class Arc:
    target: str
    cost: int
    category: str = "explicit"


@dataclass(frozen=True, slots=True)
class RoadState:
    state_id: str
    city_mask: int
    edge_cost: int = 0
    distance_mm: int = 0


class Graph(Protocol):
    def state(self, state_id: str) -> RoadState: ...
    def outgoing(self, state_id: str) -> Iterable[Arc]: ...
    def state_ids(self) -> Iterable[str]: ...


class MemoryGraph:
    def __init__(self, states: Iterable[RoadState], adjacency: dict[str, list[Arc]]):
        self.states = {state.state_id: state for state in states}
        self.adjacency = {key: sorted(value, key=lambda arc: (arc.target, arc.cost, arc.category))
                          for key, value in adjacency.items()}

    def state(self, state_id: str) -> RoadState:
        return self.states[state_id]

    def outgoing(self, state_id: str) -> Iterable[Arc]:
        return self.adjacency.get(state_id, ())

    def state_ids(self) -> Iterable[str]:
        return sorted(self.states)


class LowerBound(Protocol):
    name: str
    def value(self, road_state: str, visited_mask: int) -> int: ...


class ZeroBound:
    name = "zero_relaxation"
    def value(self, road_state: str, visited_mask: int) -> int:
        return 0


class EntryCostBound:
    """Max of minimum transition costs that can first acquire each city.

    Every completion must acquire every unvisited bit on some transition.
    Its total remaining cost is at least the cost of that transition, which is
    at least the global minimum transition acquiring the bit.  Taking a maximum
    (not a sum) remains valid when one transition acquires multiple bits.
    """
    name = "maximum_minimum_city_entry_transition"

    def __init__(self, graph: Graph, all_mask: int):
        self.all_mask = all_mask
        self.minimum: dict[int, int] = {}
        for source in graph.state_ids():
            for arc in graph.outgoing(source):
                mask = graph.state(arc.target).city_mask & all_mask
                bit = 0
                while mask:
                    low = mask & -mask
                    candidate = arc.cost
                    self.minimum[bit] = min(candidate, self.minimum.get(bit, candidate))
                    mask ^= low
                    bit += 1
                    while mask and not (mask & (1 << bit)):
                        bit += 1

    def value(self, road_state: str, visited_mask: int) -> int:
        missing = self.all_mask & ~visited_mask
        answer = 0
        bit = 0
        while missing:
            low = missing & -missing
            if bit not in self.minimum:
                return 0
            answer = max(answer, self.minimum[bit])
            missing ^= low
            bit += 1
            while missing and not (missing & (1 << bit)):
                bit += 1
        return answer


class TransitionCountBound:
    """A cardinality relaxation using min arc cost and max bits per arc.

    A transition can acquire at most K city bits.  Thus at least ceil(r/K)
    transitions are needed for r missing bits; each costs at least the global
    minimum arc cost.  Zero-cost arcs correctly make this bound zero.
    """
    name = "minimum_transition_count_relaxation"

    def __init__(self, graph: Graph, all_mask: int):
        costs: list[int] = []
        max_bits = 1
        for source in graph.state_ids():
            for arc in graph.outgoing(source):
                costs.append(arc.cost)
                max_bits = max(max_bits, (graph.state(arc.target).city_mask & all_mask).bit_count())
        self.minimum_arc = min(costs, default=0)
        self.max_bits = max_bits
        self.all_mask = all_mask

    def value(self, road_state: str, visited_mask: int) -> int:
        count = (self.all_mask & ~visited_mask).bit_count()
        return ((count + self.max_bits - 1) // self.max_bits) * self.minimum_arc


class CompositeBound:
    def __init__(self, components: list[LowerBound]):
        if len(components) < 2:
            raise ValueError("Stage 4 requires at least two admissible lower-bound components")
        self.components = components
        self.name = "max(" + ",".join(item.name for item in components) + ")"

    def value(self, road_state: str, visited_mask: int) -> int:
        return max(item.value(road_state, visited_mask) for item in self.components)


@dataclass(slots=True)
class Counters:
    expanded: int = 0
    generated: int = 0
    dominated: int = 0
    ub_pruned: int = 0
    unreachable_pruned: int = 0
    stale: int = 0
    peak_frontier: int = 0


@dataclass(slots=True)
class SolveResult:
    status: str
    ub_units: int | None
    lb_units: int | None
    path: list[str]
    actual_first_hit_bits: list[int]
    terminal_state: str | None
    counters: Counters
    stop_reason: str
    wall_time_seconds: float
    incumbent_history: list[dict]


def first_hit_order(path: list[str], graph: Graph, initial_mask: int) -> list[int]:
    order = [bit for bit in range(initial_mask.bit_length()) if initial_mask & (1 << bit)]
    visited = initial_mask
    for state_id in path[1:]:
        new = graph.state(state_id).city_mask & ~visited
        for bit in range(new.bit_length()):
            if new & (1 << bit):
                order.append(bit)
        visited |= new
    return order


def _key(state_id: str, mask: int) -> str:
    return f"{state_id}\0{mask}"


def _unkey(value: str) -> tuple[str, int]:
    state, mask = value.rsplit("\0", 1)
    return state, int(mask)


def _atomic_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as target:
        json.dump(document, target, sort_keys=True, separators=(",", ":"))
        target.write("\n")
        temporary = Path(target.name)
    os.replace(temporary, path)


def solve(
    graph: Graph,
    start: str,
    initial_mask: int,
    all_mask: int,
    bound: LowerBound,
    *,
    incumbent_units: int | None = None,
    incumbent_path: list[str] | None = None,
    max_expansions: int | None = None,
    checkpoint: Path | None = None,
    resume: bool = False,
    checkpoint_identity: dict | None = None,
) -> SolveResult:
    """Deterministic exact A* with safe superset dominance and checkpointing."""
    started = time.monotonic()
    identity = checkpoint_identity or {}
    counters = Counters()
    history: list[dict] = []
    parent: dict[str, str] = {}
    best: dict[str, int] = {}
    masks_by_road: dict[str, dict[int, int]] = {}
    heap: list[tuple[int, int, str, int]] = []

    if resume:
        if checkpoint is None or not checkpoint.exists():
            raise ValueError("resume requested without a checkpoint")
        document = json.loads(checkpoint.read_text(encoding="utf-8"))
        if document["schema_version"] != SCHEMA_VERSION or document["identity"] != identity:
            raise ValueError("checkpoint schema/input/config identity mismatch")
        heap = [tuple(item) for item in document["frontier"]]
        heapq.heapify(heap)
        best = {key: int(value) for key, value in document["best"].items()}
        parent = dict(document["parent"])
        masks_by_road = {}
        for key, value in best.items():
            q, mask = _unkey(key)
            masks_by_road.setdefault(q, {})[mask] = value
        incumbent_units = document["incumbent_units"]
        incumbent_path = document["incumbent_path"]
        history = document["incumbent_history"]
        counters = Counters(**document["counters"])
    else:
        start_mask = initial_mask | graph.state(start).city_mask
        start_key = _key(start, start_mask)
        best[start_key] = 0
        masks_by_road[start] = {start_mask: 0}
        heapq.heappush(heap, (bound.value(start, start_mask), 0, start, start_mask))

    goal_key: str | None = _key(incumbent_path[-1], all_mask) if incumbent_path else None

    def write_checkpoint() -> None:
        if checkpoint is None:
            return
        _atomic_json(checkpoint, {
            "schema_version": SCHEMA_VERSION,
            "identity": identity,
            "frontier": heap,
            "best": best,
            "parent": parent,
            "incumbent_units": incumbent_units,
            "incumbent_path": incumbent_path,
            "incumbent_history": history,
            "counters": asdict(counters),
            "cost_scale": COST_SCALE,
        })

    stop_reason = "FRONTIER_EXHAUSTED"
    while heap:
        f_score, g_score, road, mask = heapq.heappop(heap)
        state_key = _key(road, mask)
        if best.get(state_key) != g_score:
            counters.stale += 1
            continue
        if incumbent_units is not None and f_score >= incumbent_units:
            heapq.heappush(heap, (f_score, g_score, road, mask))
            stop_reason = "BOUND_MET"
            break
        if max_expansions is not None and counters.expanded >= max_expansions:
            heapq.heappush(heap, (f_score, g_score, road, mask))
            stop_reason = "EXPANSION_LIMIT"
            break
        counters.expanded += 1
        if mask == all_mask:
            incumbent_units = g_score
            goal_key = state_key
            history.append({"cost_units": g_score, "expanded": counters.expanded, "state": road})
            continue
        for arc in graph.outgoing(road):
            counters.generated += 1
            target_state = graph.state(arc.target)
            next_mask = mask | (target_state.city_mask & all_mask)
            next_g = g_score + arc.cost
            if incumbent_units is not None and next_g + bound.value(arc.target, next_mask) >= incumbent_units:
                counters.ub_pruned += 1
                continue
            road_labels = masks_by_road.setdefault(arc.target, {})
            if any((old_mask | next_mask) == old_mask and old_g <= next_g
                   for old_mask, old_g in road_labels.items()):
                counters.dominated += 1
                continue
            for old_mask, old_g in list(road_labels.items()):
                if (next_mask | old_mask) == next_mask and next_g <= old_g:
                    old_key = _key(arc.target, old_mask)
                    if best.get(old_key) == old_g:
                        del best[old_key]
                    del road_labels[old_mask]
                    counters.dominated += 1
            next_key = _key(arc.target, next_mask)
            if next_g >= best.get(next_key, 1 << 62):
                counters.dominated += 1
                continue
            best[next_key] = next_g
            road_labels[next_mask] = next_g
            parent[next_key] = state_key
            next_f = next_g + bound.value(arc.target, next_mask)
            heapq.heappush(heap, (next_f, next_g, arc.target, next_mask))
        counters.peak_frontier = max(counters.peak_frontier, len(heap))

    current_lb = None
    while heap:
        f_score, g_score, road, mask = heap[0]
        if best.get(_key(road, mask)) == g_score:
            current_lb = min(f_score, incumbent_units) if incumbent_units is not None else f_score
            break
        heapq.heappop(heap)
    if not heap and incumbent_units is not None:
        current_lb = incumbent_units

    path = list(incumbent_path or [])
    if goal_key is not None and goal_key in best:
        path = []
        cursor = goal_key
        while True:
            q, _ = _unkey(cursor)
            path.append(q)
            if cursor not in parent:
                break
            cursor = parent[cursor]
        path.reverse()
    optimal = incumbent_units is not None and (not heap or (current_lb is not None and current_lb >= incumbent_units))
    status = "OPTIMAL" if optimal else ("INCOMPLETE" if heap else "INFEASIBLE")
    write_checkpoint()
    return SolveResult(
        status=status, ub_units=incumbent_units, lb_units=current_lb, path=path,
        actual_first_hit_bits=first_hit_order(path, graph, initial_mask) if path else [],
        terminal_state=path[-1] if path else None, counters=counters, stop_reason=stop_reason,
        wall_time_seconds=time.monotonic() - started, incumbent_history=history,
    )


def exhaustive_dijkstra(graph: Graph, start: str, initial_mask: int, all_mask: int) -> SolveResult:
    return solve(graph, start, initial_mask, all_mask, ZeroBound())


def replay_path(graph: Graph, path: list[str], start: str, initial_mask: int, all_mask: int) -> dict:
    if not path or path[0] != start:
        raise ValueError("path does not start at the frozen state")
    cost = 0
    distance = graph.state(start).distance_mm
    visited = initial_mask | graph.state(start).city_mask
    for source, target in zip(path, path[1:]):
        candidates = [arc for arc in graph.outgoing(source) if arc.target == target]
        if not candidates:
            raise ValueError(f"illegal transition {source}->{target}")
        arc = min(candidates, key=lambda item: (item.cost, item.category))
        cost += arc.cost
        distance += graph.state(target).distance_mm
        visited |= graph.state(target).city_mask
        if visited == all_mask:
            break
    if visited != all_mask:
        raise ValueError("path does not visit every required city")
    encoded = "\n".join(path).encode("utf-8")
    return {
        "status": "REPLAY_VALID",
        "cost_units": cost,
        "total_duration_seconds": seconds(cost),
        "distance_mm": distance,
        "visited_mask": visited,
        "road_path_sha256": hashlib.sha256(encoded).hexdigest(),
        "actual_first_hit_bits": first_hit_order(path, graph, initial_mask),
    }


def synthetic_instance(city_count: int, seed: int = SEED) -> tuple[MemoryGraph, str, int, int]:
    """Deterministic, cyclic road graph with transit first hits and detours."""
    if city_count < 2:
        raise ValueError("at least two cities required")
    rng = random.Random(seed + city_count)
    states = [RoadState("start", 1)]
    states += [RoadState(f"c{index}", 1 << index, distance_mm=1000 + index)
               for index in range(1, city_count)]
    states += [RoadState(f"x{index}", 0, distance_mm=500) for index in range(city_count)]
    adjacency: dict[str, list[Arc]] = {state.state_id: [] for state in states}
    ids = [state.state_id for state in states]
    for source in ids:
        for target in ids:
            if source != target and (rng.random() < 0.24 or target == "start"):
                adjacency[source].append(Arc(target, rng.randint(1, 30) * COST_SCALE))
    # Guaranteed city chain and return arcs keep every product state reachable.
    previous = "start"
    for index in range(1, city_count):
        adjacency[previous].append(Arc(f"c{index}", (2 + index) * COST_SCALE))
        previous = f"c{index}"
    adjacency[previous].append(Arc("start", 2 * COST_SCALE))
    return MemoryGraph(states, adjacency), "start", 1, (1 << city_count) - 1


def independent_subset_dp(graph: Graph, start: str, initial_mask: int, all_mask: int) -> int | None:
    """Independent Bellman-Ford relaxation over the full product graph."""
    states = list(graph.state_ids())
    inf = 1 << 62
    distances = {(start, initial_mask | graph.state(start).city_mask): 0}
    limit = len(states) * (all_mask + 1)
    for _ in range(limit):
        changed = False
        for (road, mask), value in list(distances.items()):
            for arc in graph.outgoing(road):
                next_mask = mask | (graph.state(arc.target).city_mask & all_mask)
                key = (arc.target, next_mask)
                candidate = value + arc.cost
                if candidate < distances.get(key, inf):
                    distances[key] = candidate
                    changed = True
        if not changed:
            break
    goals = [value for (road, mask), value in distances.items() if mask == all_mask]
    return min(goals, default=None)


def validate_small(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    cases = []
    for city_count in (4, 6, 8, 10):
        graph, start, initial, target = synthetic_instance(city_count)
        dijkstra = exhaustive_dijkstra(graph, start, initial, target)
        components = [EntryCostBound(graph, target), TransitionCountBound(graph, target)]
        astar = solve(graph, start, initial, target, CompositeBound(components))
        independent = independent_subset_dp(graph, start, initial, target)
        if not (dijkstra.status == astar.status == "OPTIMAL"
                and dijkstra.ub_units == astar.ub_units == independent):
            raise AssertionError(f"exact solvers disagree for {city_count} cities")
        # Validate both components against exact remaining cost at every reachable seed state.
        admissibility_checks = 0
        for road in graph.state_ids():
            mask = initial | graph.state(road).city_mask
            remaining = exhaustive_dijkstra(graph, road, mask, target).ub_units
            if remaining is not None:
                for component in components:
                    if component.value(road, mask) > remaining:
                        raise AssertionError(f"inadmissible {component.name}")
                    admissibility_checks += 1
        with tempfile.TemporaryDirectory() as temporary:
            checkpoint = Path(temporary) / "checkpoint.json"
            partial = solve(graph, start, initial, target, CompositeBound(components),
                            max_expansions=3, checkpoint=checkpoint,
                            checkpoint_identity={"case": city_count})
            resumed = solve(graph, start, initial, target, CompositeBound(components),
                            checkpoint=checkpoint, resume=True,
                            checkpoint_identity={"case": city_count})
            if partial.status != "INCOMPLETE" or resumed.ub_units != astar.ub_units:
                raise AssertionError("checkpoint resume changed the optimum")
        replay = replay_path(graph, astar.path, start, initial, target)
        cases.append({
            "cities": city_count, "optimum_units": astar.ub_units,
            "optimum_seconds": seconds(astar.ub_units), "path": astar.path,
            "first_hit_bits": astar.actual_first_hit_bits,
            "admissibility_checks": admissibility_checks,
            "checkpoint_resume": "PASS", "independent_checker": "PASS",
            "replay": replay,
        })
    document = {
        "schema_version": SCHEMA_VERSION, "seed": SEED,
        "cost_units": COST_UNIT, "cases": cases,
        "statuses": ["SMALL_INSTANCE_VALIDATION_PASS", "LOWER_BOUND_ADMISSIBILITY_PASS",
                     "CHECKPOINT_RESUME_PASS", "INDEPENDENT_CHECKER_PASS"],
    }
    _atomic_json(output / "summary.json", document)
    return document


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_lineage(repository: Path, output: Path) -> dict:
    stage3 = json.loads((repository / "results/stage3/stage3-manifest.json").read_text())
    stage2b_path = repository / "results/graph-prg/stage2b-manifest.json"
    failures = []
    if not all(stage3.get("per_model_feasible", {}).get(model) is True for model in MODELS):
        failures.append("not all Stage 3 models have a feasible route")
    for required in ("UPPER_BOUND_STAGE_OK", "EXACT_GLOBAL_SOLVER_NOT_STARTED"):
        if required not in stage3.get("statuses", []):
            failures.append(f"missing Stage 3 status {required}")
    actual = sha256(stage2b_path)
    if actual != stage3.get("stage2b_manifest_sha256"):
        failures.append("Stage 2B manifest hash mismatch")
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", "341c2230f48c1638abb55d93178efb1eaf5f8ab6", "HEAD"],
        cwd=repository, check=False,
    ).returncode == 0
    if not ancestor:
        failures.append("required Stage 3.1 commit is not an ancestor")
    document = {
        "status": "STAGE4_INPUT_LINEAGE_VALID" if not failures else "STAGE4_BLOCKED_INVALID_INPUT_LINEAGE",
        "required_ancestor": "341c2230f48c1638abb55d93178efb1eaf5f8ab6",
        "ancestor_ok": ancestor, "stage2b_manifest_sha256": actual,
        "artifact_hashes": stage3.get("stage2b_artifact_hashes", {}), "failures": failures,
    }
    _atomic_json(output, document)
    if failures:
        raise SystemExit("STAGE4_BLOCKED_INVALID_INPUT_LINEAGE")
    return document


def write_scaffold(repository: Path, output: Path) -> None:
    """Write truthful pre-run artifacts; never promotes scalar Stage 3 UBs."""
    input_dir = output / "input-validation"
    small_dir = output / "small-instance-validation"
    validate_lineage(repository, input_dir / "stage3-lineage.json")
    small = validate_small(small_dir)
    for model in MODELS:
        _atomic_json(input_dir / f"{model}-incumbent-replay.json", {
            "model": model, "status": "WITNESS_UNAVAILABLE", "incumbent_enabled": False,
            "initial_UB": "INFINITY",
            "reason": "full ordered Stage 3 split-edge witness is not committed; recover and replay on canonical Stage 2B graph",
        })
    independent = output / "independent-check"
    independent.mkdir(parents=True, exist_ok=True)
    # These are pre-run certificates generated from a worktree; a real model
    # run replaces this with GITHUB_SHA / the checked-out implementation commit.
    git_commit = "PRE_RUN_WORKTREE_NOT_A_SOLVER_CLAIM"
    city_order = [item["city"] for item in json.loads(subprocess.run(
        ["ruby", str(repository / "scripts/manifest_json.rb"),
         str(repository / "instance/pl_18_capitals_static_instance_v3.yaml")],
        check=True, capture_output=True, text=True).stdout)["cities"]["canonical_bit_order"]]
    for model in MODELS:
        model_dir = output / model
        model_dir.mkdir(parents=True, exist_ok=True)
        certificate = {
            "model": model, "solver_status": "NOT_STARTED", "UB_seconds": None, "LB_seconds": None,
            "absolute_gap_seconds": None, "relative_gap": None, "optimality_tolerance_seconds": 1.0,
            "start_state": START_EDGE, "terminal_state": None, "terminal_city": None,
            "actual_first_hit_order": [], "total_duration_seconds": None,
            "total_distance_metres": None, "road_path_sha256": None, "geometry_sha256": None,
            "expanded_labels": 0, "generated_labels": 0, "dominated_labels": 0,
            "UB_pruned_labels": 0, "unreachable_pruned_labels": 0, "peak_frontier": 0,
            "peak_RAM_bytes": None, "peak_disk_bytes": None, "wall_time_seconds": 0,
            "cpu_time_seconds": 0, "solver_git_commit": git_commit,
            "manifest_sha256": sha256(repository / "instance/pl_18_capitals_static_instance_v2.yaml"),
            "OSM_sha256": "f28f493c6cc280da1128b03be21ae2eb1973f443c14235dea36088bcbd3e83f3",
            "PRG_raw_sha256": "adf261aae31257dd24843b6f6c0e694efb40041aae3913b1c27aa4801866974c",
            "road_graph_hash": None,
            "split_graph_hash": sha256(repository / "results/graph-prg/stage2b-manifest.json"),
            "lower_bound_method": "max(minimum_city_entry_edge,minimum_transition_count)",
            "independent_checker_status": "NOT_STARTED",
            "certification_status": INCOMPLETE,
        }
        _atomic_json(model_dir / "certificate.json", certificate)
        _atomic_json(model_dir / "solution.json", {"model": model, "status": "NOT_STARTED", "path": None})
        _atomic_json(model_dir / "incumbent-history.json", {"model": model, "history": []})
        _atomic_json(model_dir / "performance.json", {"model": model, "status": "NOT_STARTED"})
        _atomic_json(model_dir / "checkpoint-manifest.json", {
            "model": model, "status": "NO_CHECKPOINT", "schema_version": SCHEMA_VERSION,
        })
        with (model_dir / "terminal-results.csv").open("w", encoding="utf-8", newline="") as target:
            writer = csv.writer(target, lineterminator="\n")
            writer.writerow(["terminal_city", "best_cost_seconds", "LB_seconds", "gap_seconds", "status"])
            for city in city_order[1:]:
                writer.writerow([city, "", "", "", "NOT_STARTED"])
        _atomic_json(independent / f"{model}.json", {
            "model": model, "status": "NOT_STARTED", "claim": INCOMPLETE,
        })
        model_files = sorted(path for path in model_dir.iterdir() if path.is_file() and path.name != "SHA256SUMS")
        (model_dir / "SHA256SUMS").write_text("".join(
            f"{sha256(path)}  {path.name}\n" for path in model_files), encoding="utf-8")
    with (output / "global-comparison.csv").open("w", encoding="utf-8", newline="") as target:
        writer = csv.writer(target, lineterminator="\n")
        writer.writerow(["model", "optimum_seconds", "A_seconds", "B_seconds", "C_seconds",
                         "A_minus_optimum", "B_minus_optimum", "C_minus_optimum", "status"])
        for model in MODELS:
            values = []
            for route in ("A", "B", "C"):
                fixed = json.loads((repository / f"results/stage3/fixed-orders/{model}-{route}.json").read_text())
                values.append(fixed.get("total_duration_s"))
            writer.writerow([model, "", *values, "", "", "", INCOMPLETE])
    manifest = {
        "schema_version": SCHEMA_VERSION, "stage": 4,
        "status": INCOMPLETE,
        "cost_scale": COST_SCALE, "cost_unit": COST_UNIT,
        "models": {model: {"solver_status": "NOT_STARTED", "incumbent_enabled": False,
                           "UB_seconds": None, "LB_seconds": None} for model in MODELS},
        "gates": small["statuses"] + ["STAGE4_INPUT_LINEAGE_VALID"],
        "full_graph_available_locally": False,
        "global_optimum_certified": False,
    }
    _atomic_json(output / "stage4-manifest.json", manifest)
    report = """# Stage 4 exact solve status

Status: `BEST_FOUND_SOLUTION_GLOBAL_OPTIMALITY_NOT_PROVEN`

The frozen input lineage and all deterministic 4/6/8/10-city proof gates pass.
The canonical large Stage 2B tables are intentionally not stored in Git.  No
historical scalar Stage 3 value has been enabled for pruning: each model remains
at `UB=INFINITY` until its full path is recovered and independently replayed.

The GitHub workflow performs hash-checked graph restore/export, incumbent
recovery/replay, bounded checkpointed chunks, and final validation.  A full
Poland-18 optimum has not been claimed by these pre-run artifacts.
"""
    (output / "validation-report.md").write_text(report, encoding="utf-8")
    input_files = sorted(path for path in input_dir.iterdir() if path.is_file() and path.name != "SHA256SUMS")
    (input_dir / "SHA256SUMS").write_text("".join(
        f"{sha256(path)}  {path.name}\n" for path in input_files), encoding="utf-8")
    files = sorted(path for path in output.rglob("*") if path.is_file() and path.name != "SHA256SUMS")
    (output / "SHA256SUMS").write_text("".join(
        f"{sha256(path)}  {path.relative_to(output)}\n" for path in files), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    small = subparsers.add_parser("validate-small")
    small.add_argument("--output", type=Path, required=True)
    lineage = subparsers.add_parser("validate-lineage")
    lineage.add_argument("--repository", type=Path, default=Path("."))
    lineage.add_argument("--output", type=Path, required=True)
    scaffold = subparsers.add_parser("scaffold")
    scaffold.add_argument("--repository", type=Path, default=Path("."))
    scaffold.add_argument("--output", type=Path, default=Path("results/stage4"))
    smoke = subparsers.add_parser("smoke")
    smoke.add_argument("--cities", type=int, default=10)
    smoke.add_argument("--max-expansions", type=int, default=5)
    args = parser.parse_args()
    if args.command == "validate-small":
        validate_small(args.output)
    elif args.command == "validate-lineage":
        validate_lineage(args.repository.resolve(), args.output)
    elif args.command == "scaffold":
        write_scaffold(args.repository.resolve(), args.output)
    else:
        graph, start, initial, target = synthetic_instance(args.cities)
        result = solve(graph, start, initial, target,
                       CompositeBound([EntryCostBound(graph, target), TransitionCountBound(graph, target)]),
                       max_expansions=args.max_expansions)
        print(json.dumps({**asdict(result), "ub_seconds": seconds(result.ub_units),
                          "lb_seconds": seconds(result.lb_units)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
