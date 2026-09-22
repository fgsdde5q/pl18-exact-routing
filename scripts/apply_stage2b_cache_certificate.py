#!/usr/bin/env python3

import argparse
import hashlib
import json
from pathlib import Path


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", required=True, type=Path)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--budget-bytes", required=True, type=int)
    args = parser.parse_args()
    certificate_path = args.results / "cache-budget-certificate.json"
    certificate = json.loads(certificate_path.read_text(encoding="utf-8"))
    archive_bytes = args.archive.stat().st_size
    certificate["canonical_archive_bytes"] = archive_bytes
    certificate["canonical_archive_sha256"] = digest(args.archive)
    certificate["budget_bytes"] = args.budget_bytes
    certificate["within_budget"] = archive_bytes <= args.budget_bytes
    certificate["status"] = "CACHE_BUDGET_OK" if certificate["within_budget"] else "CACHE_BUDGET_FAILED"
    certificate_path.write_text(
        json.dumps(certificate, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with (args.results / "SHA256SUMS").open("w", encoding="utf-8") as output:
        for path in sorted(args.results.iterdir()):
            if path.is_file() and path.name != "SHA256SUMS":
                output.write(f"{digest(path)}  {path.name}\n")
    if not certificate["within_budget"]:
        raise ValueError(f"Stage 2B archive exceeds cache budget: {archive_bytes}")
    print("CACHE_BUDGET_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
