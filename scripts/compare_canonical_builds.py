#!/usr/bin/env python3

import argparse
import json
import sys
from pathlib import Path


def compare(first_path: Path, second_path: Path, diagnostics_path: Path) -> dict:
    first = json.loads(first_path.read_text(encoding="utf-8"))
    second = json.loads(second_path.read_text(encoding="utf-8"))
    differences = {
        key: {"first": first.get(key), "second": second.get(key)}
        for key in sorted(first.keys() | second.keys())
        if first.get(key) != second.get(key)
    }
    if differences:
        diagnostics_path.write_text(
            json.dumps({"differences": differences}, indent=2) + "\n",
            encoding="utf-8",
        )
    elif diagnostics_path.exists():
        diagnostics_path.unlink()
    return differences


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--first", required=True, type=Path)
    parser.add_argument("--second", required=True, type=Path)
    parser.add_argument("--diagnostics", required=True, type=Path)
    args = parser.parse_args()
    differences = compare(args.first, args.second, args.diagnostics)
    for key, values in differences.items():
        print(f"canonical determinism mismatch: {key}", file=sys.stderr)
        print(f"  first:  {values['first']}", file=sys.stderr)
        print(f"  second: {values['second']}", file=sys.stderr)
    return 1 if differences else 0


if __name__ == "__main__":
    raise SystemExit(main())
