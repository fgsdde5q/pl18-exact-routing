#!/usr/bin/env python3

import csv
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from osgeo import gdal, ogr, osr

from prg_stage import (
    EXPECTED_TARGET_COUNT,
    GEOJSON_EPSG,
    INVENTORY_COLUMNS,
    START_POINTS,
    authority_code,
    geometry_is_multipart,
    geometry_validity,
    load_targets,
    srs_for_epsg,
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_layer(path: Path) -> tuple[object, object]:
    dataset = gdal.OpenEx(str(path), gdal.OF_VECTOR)
    if dataset is None:
        raise ValueError(f"cannot open {path}")
    layer = dataset.GetLayer(0)
    if layer is None:
        raise ValueError(f"{path} has no layer")
    return dataset, layer


def features_by_teryt(layer: object) -> dict[str, object]:
    features: dict[str, object] = {}
    for feature in layer:
        teryt = feature.GetFieldAsString("teryt")
        if teryt in features:
            raise ValueError(f"duplicate TERYT {teryt}")
        features[teryt] = feature.Clone()
    return features


def verify_checksums(repository_root: Path) -> None:
    sums_path = repository_root / "results/prg/SHA256SUMS"
    for line in sums_path.read_text(encoding="utf-8").splitlines():
        expected, relative_path = line.split("  ", 1)
        actual = sha256_file(repository_root / relative_path)
        if actual != expected:
            raise ValueError(
                f"SHA-256 mismatch for {relative_path}: expected {expected}, got {actual}"
            )


def verify_inventory(
    repository_root: Path, targets: list[dict[str, str]]
) -> dict[str, object]:
    json_path = repository_root / "results/prg/prg-city-inventory.json"
    inventory = json.loads(json_path.read_text(encoding="utf-8"))
    if inventory["stage_status"] != "PRG_STAGE_OK":
        raise ValueError("stage status is not PRG_STAGE_OK")
    if inventory["solver_status"] != "SOLVER_NOT_STARTED":
        raise ValueError("solver status is not SOLVER_NOT_STARTED")
    cities = inventory["cities"]
    if len(cities) != EXPECTED_TARGET_COUNT:
        raise ValueError(f"inventory contains {len(cities)} cities")
    if len({city["teryt"] for city in cities}) != EXPECTED_TARGET_COUNT:
        raise ValueError("inventory does not contain 18 unique TERYT codes")
    if [city["city_id"] for city in cities] != [target["id"] for target in targets]:
        raise ValueError("inventory order does not follow the target instance")
    if [city["report_order"] for city in cities] != list(
        range(1, EXPECTED_TARGET_COUNT + 1)
    ):
        raise ValueError("inventory report_order is not deterministic")
    if not all(city["area_epsg2180_m2"] > 0 for city in cities):
        raise ValueError("inventory contains a non-positive area")
    for city in cities:
        if city["geos_is_valid"] and city["invalidity_reason"]:
            raise ValueError(f"{city['name']} is valid but has an invalidity reason")
        if not city["geos_is_valid"] and not city["invalidity_reason"]:
            raise ValueError(f"{city['name']} is invalid without a reason")
        if city["source_wfs_response_sha256"] != inventory["source"]["raw_gml_sha256"]:
            raise ValueError(f"{city['name']} has the wrong raw WFS SHA-256")

    csv_path = repository_root / "results/prg/prg-city-inventory.csv"
    with csv_path.open(encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames != INVENTORY_COLUMNS:
            raise ValueError(f"unexpected CSV columns: {reader.fieldnames}")
        rows = list(reader)
    if [row["teryt"] for row in rows] != [city["teryt"] for city in cities]:
        raise ValueError("CSV and JSON inventory orders differ")
    return inventory


def verify_raw_and_original(
    repository_root: Path, inventory: dict[str, object]
) -> None:
    raw_path = repository_root / "results/prg/prg-18-raw.gml"
    root = ET.parse(raw_path).getroot()
    if root.attrib.get("numberReturned") != str(EXPECTED_TARGET_COUNT):
        raise ValueError("raw GML numberReturned is not 18")
    if sha256_file(raw_path) != inventory["source"]["raw_gml_sha256"]:
        raise ValueError("raw GML SHA-256 does not match inventory")

    raw_dataset, raw_layer = read_layer(raw_path)
    original_dataset, original_layer = read_layer(
        repository_root / "results/prg/prg-18-original.gpkg"
    )
    if raw_layer.GetFeatureCount() != EXPECTED_TARGET_COUNT:
        raise ValueError("raw GML feature count is not 18")
    if original_layer.GetFeatureCount() != EXPECTED_TARGET_COUNT:
        raise ValueError("original GeoPackage feature count is not 18")

    raw_features: dict[str, object] = {}
    for feature in raw_layer:
        teryt = feature.GetFieldAsString("JPT_KOD_JE")
        raw_features[teryt] = feature.Clone()
    original_features = features_by_teryt(original_layer)
    if set(raw_features) != set(original_features):
        raise ValueError("raw GML and original GeoPackage TERYT sets differ")

    for teryt, raw_feature in raw_features.items():
        raw_geometry = raw_feature.GetGeometryRef()
        original_geometry = original_features[teryt].GetGeometryRef()
        if raw_geometry is None or raw_geometry.IsEmpty():
            raise ValueError(f"raw geometry {teryt} is empty")
        if original_geometry is None or original_geometry.IsEmpty():
            raise ValueError(f"original geometry {teryt} is empty")
        if raw_geometry.ExportToIsoWkb(ogr.wkbNDR) != original_geometry.ExportToIsoWkb(
            ogr.wkbNDR
        ):
            raise ValueError(f"original geometry {teryt} changed from raw GML")

    raw_dataset = None
    original_dataset = None
    raw_path.with_suffix(".gfs").unlink(missing_ok=True)


def verify_computational_and_start_points(
    repository_root: Path, inventory: dict[str, object]
) -> None:
    dataset, layer = read_layer(
        repository_root / "results/prg/prg-18-computational.gpkg"
    )
    spatial_reference = layer.GetSpatialRef()
    if spatial_reference is None or authority_code(spatial_reference) != "2180":
        raise ValueError("computational GeoPackage CRS is not EPSG:2180")
    if layer.GetFeatureCount() != EXPECTED_TARGET_COUNT:
        raise ValueError("computational GeoPackage feature count is not 18")
    features = features_by_teryt(layer)
    cities = {city["teryt"]: city for city in inventory["cities"]}

    for teryt, feature in features.items():
        geometry = feature.GetGeometryRef()
        if geometry is None or geometry.IsEmpty():
            raise ValueError(f"computational geometry {teryt} is empty")
        valid, reason = geometry_validity(geometry)
        if not valid:
            raise ValueError(f"computational geometry {teryt} is invalid: {reason}")
        if geometry.GetArea() <= 0:
            raise ValueError(f"computational geometry {teryt} has non-positive area")
        if geometry_is_multipart(geometry) != cities[teryt]["multipart"]:
            raise ValueError(f"multipart status mismatch for {teryt}")

    warsaw = features["1465011"].GetGeometryRef()
    source = srs_for_epsg(int(GEOJSON_EPSG))
    target = srs_for_epsg(2180)
    transform = osr.CoordinateTransformation(source, target)
    for start_point in START_POINTS:
        point = ogr.Geometry(ogr.wkbPoint)
        point.AddPoint_2D(start_point["longitude"], start_point["latitude"])
        point.AssignSpatialReference(source)
        point.Transform(transform)
        if not warsaw.Contains(point):
            raise ValueError(f"{start_point['name']} is not inside Warszawa")
    dataset = None


def coordinate_pairs(value: object):
    if (
        isinstance(value, list)
        and len(value) >= 2
        and isinstance(value[0], (int, float))
        and isinstance(value[1], (int, float))
    ):
        yield value[0], value[1]
        return
    if isinstance(value, list):
        for child in value:
            yield from coordinate_pairs(child)


def verify_geojson(repository_root: Path) -> None:
    path = repository_root / "results/prg/prg-18.geojson"
    document = json.loads(path.read_text(encoding="utf-8"))
    if "crs" in document:
        raise ValueError("RFC 7946 GeoJSON must not contain a crs member")
    if len(document.get("features", [])) != EXPECTED_TARGET_COUNT:
        raise ValueError("GeoJSON feature count is not 18")
    for feature in document["features"]:
        coordinates = list(coordinate_pairs(feature["geometry"]["coordinates"]))
        if not coordinates:
            raise ValueError("GeoJSON contains an empty geometry")
        if not all(-180 <= longitude <= 180 for longitude, _ in coordinates):
            raise ValueError("GeoJSON longitude is outside EPSG:4326 range")
        if not all(-90 <= latitude <= 90 for _, latitude in coordinates):
            raise ValueError("GeoJSON latitude is outside EPSG:4326 range")

    dataset, layer = read_layer(path)
    spatial_reference = layer.GetSpatialRef()
    if spatial_reference is None or authority_code(spatial_reference) != GEOJSON_EPSG:
        raise ValueError("GeoJSON is not interpreted as EPSG:4326")
    dataset = None


def verify_report_order(
    repository_root: Path, inventory: dict[str, object]
) -> None:
    report = (
        repository_root / "results/prg/validation-report.md"
    ).read_text(encoding="utf-8")
    rows = re.findall(r"^\| (\d+) \| ([^|]+?) \| `(\d{7})` \|", report, re.MULTILINE)
    expected = [
        (str(city["report_order"]), city["name"], city["teryt"])
        for city in inventory["cities"]
    ]
    if rows != expected:
        raise ValueError("validation report city order is not deterministic")
    if "PRG_STAGE_OK" not in report or "SOLVER_NOT_STARTED" not in report:
        raise ValueError("validation report is missing stage statuses")


def run(repository_root: Path) -> None:
    targets = load_targets(
        repository_root / "instance/pl_18_capitals_static_instance_v1.yaml"
    )
    inventory = verify_inventory(repository_root, targets)
    verify_raw_and_original(repository_root, inventory)
    verify_computational_and_start_points(repository_root, inventory)
    verify_geojson(repository_root)
    verify_report_order(repository_root, inventory)
    verify_checksums(repository_root)
    print("All PRG Stage 1 checks passed.")
    print("PRG_STAGE_OK")
    print("SOLVER_NOT_STARTED")


def main() -> int:
    repository_root = Path(__file__).resolve().parent.parent
    gdal.UseExceptions()
    try:
        run(repository_root)
    except Exception as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
