#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path

from stage3_upper_bounds import (
    FAILURE_STATUS,
    MODEL_ORDER,
    OUTCOME_CATEGORIES,
    SUCCESS_STATUSES,
)


def validate(root: Path) -> bool:
    manifest = json.loads((root / "stage3-manifest.json").read_text(encoding="utf-8"))
    statuses = set(manifest["statuses"])
    model_has_feasible: dict[str, bool] = {}
    for model in MODEL_ORDER:
        path = root / f"evaluation-outcomes-{model}.csv"
        with path.open(encoding="utf-8", newline="") as source:
            rows = list(csv.DictReader(source))
        if len(rows) != 197:
            raise ValueError(f"{path}: expected 197 outcomes, got {len(rows)}")
        categories = Counter(row["result_category"] for row in rows)
        unknown = set(categories) - set(OUTCOME_CATEGORIES)
        if unknown:
            raise ValueError(f"{path}: unknown outcome categories: {sorted(unknown)}")
        if sum(categories.values()) != 197:
            raise ValueError(f"{path}: non-exhaustive outcome classification")
        model_has_feasible[model] = categories["FEASIBLE"] > 0

        top = json.loads((root / "candidates" / f"{model}-top.json").read_text(encoding="utf-8"))
        if model_has_feasible[model]:
            best = top.get("BEST_FOUND_FEASIBLE")
            if not best or not isinstance(best.get("total_duration_s"), (int, float)):
                raise ValueError(f"{model}: feasible outcomes exist but numeric best candidate is absent")
            if not best.get("actual_first_hit_order") or not best["nominal_order"][-1]:
                raise ValueError(f"{model}: best candidate lacks first-hit order or terminal city")

        with (root / "finishes" / f"{model}-finishes.csv").open(encoding="utf-8", newline="") as source:
            finishes = list(csv.DictReader(source))
        if len(finishes) != 17 or any(not row["result_category"] for row in finishes):
            raise ValueError(f"{model}: all 17 terminal-city experiments need explicit outcomes")

        for route_id in ("A", "B", "C"):
            fixed = json.loads((root / "fixed-orders" / f"{model}-{route_id}.json").read_text(encoding="utf-8"))
            category = fixed["result_category"]
            if category == "FEASIBLE":
                if not isinstance(fixed.get("total_duration_s"), (int, float)) or not isinstance(fixed.get("total_distance_m"), (int, float)):
                    raise ValueError(f"{model}/{route_id}: feasible case lacks numeric duration/distance")
                if (fixed.get("fixed_order_check_delta_s") or 0) > 1.0:
                    raise ValueError(f"{model}/{route_id}: independent checker differs by more than 1s")
            elif category == "ORDER_INVALID_BY_FIRST_HIT":
                if not fixed.get("actual_first_hit_order"):
                    raise ValueError(f"{model}/{route_id}: invalid order lacks actual first-hit order")
            elif category == "UNREACHABLE":
                if not fixed.get("failure_reason") or not fixed.get("failing_transition_or_state"):
                    raise ValueError(f"{model}/{route_id}: unreachable case lacks graph-level proof")
            else:
                raise ValueError(f"{model}/{route_id}: unresolved fixed-order outcome {category}")

    success = all(model_has_feasible.values())
    forbidden_without_route = set(SUCCESS_STATUSES)
    if success:
        missing = forbidden_without_route - statuses
        if missing or FAILURE_STATUS in statuses:
            raise ValueError(f"successful Stage 3 has inconsistent statuses; missing={sorted(missing)}")
    else:
        forbidden = forbidden_without_route & statuses
        if forbidden or FAILURE_STATUS not in statuses:
            raise ValueError(f"failed Stage 3 has inconsistent statuses; forbidden={sorted(forbidden)}")
    return success


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", type=Path, default=Path("results/stage3"))
    args = parser.parse_args()
    success = validate(args.root)
    print("UPPER_BOUND_STAGE_OK" if success else FAILURE_STATUS)
    return 0 if success else 2


if __name__ == "__main__":
    raise SystemExit(main())
