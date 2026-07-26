#!/usr/bin/env python3

import csv
import hashlib
import json
import subprocess
import sys
from pathlib import Path


PROVENANCE_STATUS = "PRG_PROVENANCE_LOCKED"
SOURCE_MANIFEST_PATH = Path("input/prg/source-manifest.json")
DERIVED_ARTIFACTS_PATH = Path("results/prg/derived-artifacts.json")
DERIVED_PRODUCT_PATHS = {
    "results/prg/prg-18-original.gpkg",
    "results/prg/prg-18-computational.gpkg",
    "results/prg/prg-18.geojson",
    "results/prg/prg-city-inventory.csv",
    "results/prg/prg-city-inventory.json",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run(repository_root: Path) -> None:
    source_manifest_path = repository_root / SOURCE_MANIFEST_PATH
    source_manifest_sha256 = sha256_file(source_manifest_path)
    source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    if source_manifest["provenance_status"] != PROVENANCE_STATUS:
        raise ValueError("source manifest provenance is not locked")

    selected = source_manifest["selected_raw_input"]
    compressed_path = repository_root / selected["compressed_path"]
    if sha256_file(compressed_path) != selected["compressed_sha256"]:
        raise ValueError("selected compressed raw SHA-256 changed")
    decompressed = subprocess.run(
        ["zstd", "--decompress", "--stdout", str(compressed_path)],
        check=True,
        capture_output=True,
    ).stdout
    if hashlib.sha256(decompressed).hexdigest() != selected["raw_sha256"]:
        raise ValueError("selected uncompressed raw SHA-256 changed")
    materialized_path = repository_root / selected["materialized_path"]
    if materialized_path.read_bytes() != decompressed:
        raise ValueError("materialized raw GML is not the selected pinned response")

    mapping = json.loads(
        (repository_root / DERIVED_ARTIFACTS_PATH).read_text(encoding="utf-8")
    )
    if mapping["provenance_status"] != PROVENANCE_STATUS:
        raise ValueError("derived artifact provenance is not locked")
    if mapping["selected_raw_gml_sha256"] != selected["raw_sha256"]:
        raise ValueError("derived artifact map selects a different raw GML")
    if mapping["source_manifest"]["sha256"] != source_manifest_sha256:
        raise ValueError("derived artifact map selects a different source manifest")
    if set(mapping["artifacts"]) != DERIVED_PRODUCT_PATHS:
        raise ValueError("derived artifact map must cover exactly five products")

    for relative_path, artifact in mapping["artifacts"].items():
        if artifact["raw_gml_sha256"] != selected["raw_sha256"]:
            raise ValueError(f"{relative_path} refers to a different raw GML")
        if artifact["manifest_sha256"] != source_manifest_sha256:
            raise ValueError(f"{relative_path} refers to a different manifest")
        if artifact["source_manifest_sha256"] != source_manifest_sha256:
            raise ValueError(f"{relative_path} source manifest SHA-256 differs")
        if artifact["artifact_sha256"] != sha256_file(
            repository_root / relative_path
        ):
            raise ValueError(f"{relative_path} content SHA-256 changed")
        generator = artifact["generator"]
        if generator["script_sha256"] != sha256_file(
            repository_root / generator["script"]
        ):
            raise ValueError(f"{relative_path} generator SHA-256 changed")

    inventory_path = repository_root / "results/prg/prg-city-inventory.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    if inventory["source"]["raw_gml_sha256"] != selected["raw_sha256"]:
        raise ValueError("JSON inventory refers to a different raw GML")
    if inventory["source"]["source_manifest_sha256"] != source_manifest_sha256:
        raise ValueError("JSON inventory refers to a different source manifest")
    if any(
        city["source_wfs_response_sha256"] != selected["raw_sha256"]
        for city in inventory["cities"]
    ):
        raise ValueError("a JSON inventory city refers to a different raw GML")

    csv_path = repository_root / "results/prg/prg-city-inventory.csv"
    with csv_path.open(encoding="utf-8", newline="") as source:
        rows = list(csv.DictReader(source))
    if any(
        row["source_wfs_response_sha256"] != selected["raw_sha256"] for row in rows
    ):
        raise ValueError("a CSV inventory city refers to a different raw GML")


def main() -> int:
    repository_root = Path(__file__).resolve().parent.parent
    try:
        run(repository_root)
    except Exception as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print("PRG provenance contract passed.")
    print(PROVENANCE_STATUS)
    print("SOLVER_NOT_STARTED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
