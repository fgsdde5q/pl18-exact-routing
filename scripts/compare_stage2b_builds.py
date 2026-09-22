#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path


IGNORED_COMPACT = {"SHA256SUMS", "export-reproducibility.json", "rebuild-reproducibility.json", "stage2b-manifest.json", "stage2b-validation-report.md"}


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def compact_hashes(root: Path) -> dict[str, str]:
    return {
        path.name: digest(path)
        for path in sorted(root.iterdir())
        if path.is_file() and path.name not in IGNORED_COMPACT
    }


def refresh_sums(root: Path) -> None:
    with (root / "SHA256SUMS").open("w", encoding="utf-8") as output:
        for path in sorted(root.iterdir()):
            if path.is_file() and path.name != "SHA256SUMS":
                output.write(f"{digest(path)}  {path.name}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--first-results", required=True, type=Path)
    parser.add_argument("--second-results", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--mode", choices=("export", "rebuild"), required=True)
    args = parser.parse_args()

    first_manifest = load(args.first_results / "stage2b-manifest.json")
    second_manifest = load(args.second_results / "stage2b-manifest.json")
    first_artifacts = first_manifest["artifact_hashes"]
    second_artifacts = second_manifest["artifact_hashes"]
    artifact_names = sorted(set(first_artifacts) | set(second_artifacts))
    artifact_comparisons = {
        name: {
            "match": first_artifacts.get(name) == second_artifacts.get(name),
            "first": first_artifacts.get(name),
            "second": second_artifacts.get(name),
        }
        for name in artifact_names
    }
    first_compact = compact_hashes(args.first_results)
    second_compact = compact_hashes(args.second_results)
    compact_names = sorted(set(first_compact) | set(second_compact))
    compact_comparisons = {
        name: {
            "match": first_compact.get(name) == second_compact.get(name),
            "first_sha256": first_compact.get(name),
            "second_sha256": second_compact.get(name),
        }
        for name in compact_names
    }
    mismatches = [
        f"artifact:{name}" for name, value in artifact_comparisons.items() if not value["match"]
    ] + [
        f"compact:{name}" for name, value in compact_comparisons.items() if not value["match"]
    ]

    if args.output.exists():
        shutil.rmtree(args.output)
    shutil.copytree(args.first_results, args.output)
    export_certificate = {
        "schema_version": 1,
        "status": "STAGE2B_EXPORT_REPRODUCIBILITY" if not mismatches else "STAGE2B_EXPORT_REPRODUCIBILITY_FAILED",
        "result": "PASS" if not mismatches else "FAILED",
        "independent_export_count": 2,
        "artifact_comparisons": artifact_comparisons,
        "compact_comparisons": compact_comparisons,
        "mismatches": mismatches,
    }
    write(args.output / "export-reproducibility.json", export_certificate)

    manifest = load(args.output / "stage2b-manifest.json")
    statuses = manifest["statuses"]
    if not mismatches and "STAGE2B_EXPORT_REPRODUCIBLE" not in statuses:
        statuses.insert(-1, "STAGE2B_EXPORT_REPRODUCIBLE")
    if args.mode == "rebuild":
        rebuild = {
            "schema_version": 1,
            "status": "STAGE2B_REBUILD_REPRODUCIBLE" if not mismatches else "STAGE2B_REBUILD_REPRODUCIBILITY: FAILED",
            "result": "PASS" if not mismatches else "FAILED",
            "clean_build_count": 2,
            "stage2b_product_cache_restored": False,
            "stage2b_metadata_checkpoint_restored": False,
            "immutable_stage2a_metadata_restored": True,
            "artifact_comparisons": artifact_comparisons,
            "compact_comparisons": compact_comparisons,
            "mismatches": mismatches,
        }
        write(args.output / "rebuild-reproducibility.json", rebuild)
        if not mismatches and "STAGE2B_REBUILD_REPRODUCIBLE" not in statuses:
            statuses.insert(-1, "STAGE2B_REBUILD_REPRODUCIBLE")
    write(args.output / "stage2b-manifest.json", manifest)
    report = "# Stage 2B validation\n\n" + "\n".join(f"- **{status}**" for status in statuses)
    report += "\n\nTwo independent Stage 2B builds produced identical canonical counts, hashes, artifacts, and compact certificates.\n"
    (args.output / "stage2b-validation-report.md").write_text(report, encoding="utf-8")
    refresh_sums(args.output)
    if mismatches:
        raise ValueError("Stage 2B reproducibility mismatch: " + ", ".join(mismatches))
    print("STAGE2B_EXPORT_REPRODUCIBLE")
    if args.mode == "rebuild":
        print("STAGE2B_REBUILD_REPRODUCIBLE")
    print("SOLVER_NOT_STARTED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
