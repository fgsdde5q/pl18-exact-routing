#!/usr/bin/env python3

import argparse
import csv
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation
from pathlib import Path

from osgeo import gdal, ogr, osr


PRG_WFS_URL = (
    "https://mapy.geoportal.gov.pl/wss/service/PZGIK/PRG/WFS/"
    "AdministrativeBoundaries"
)
STAGE0_RUN_URL = (
    "https://github.com/fgsdde5q/pl18-exact-routing/actions/runs/30208746751"
)
WFS_VERSION = "2.0.0"
EXPECTED_TARGET_COUNT = 18
EXPECTED_PRG_UNIT_TYPE = "GMI"
EXPECTED_TERYT_TYPE = "1"
EXPECTED_SOURCE_EPSG = "2180"
COMPUTATIONAL_EPSG = "2180"
GEOJSON_EPSG = "4326"
PROVENANCE_STATUS = "PRG_PROVENANCE_LOCKED"
SOURCE_MANIFEST_PATH = Path("input/prg/source-manifest.json")
DERIVED_ARTIFACTS_PATH = Path("results/prg/derived-artifacts.json")
DERIVED_PRODUCT_PATHS = [
    Path("results/prg/prg-18-original.gpkg"),
    Path("results/prg/prg-18-computational.gpkg"),
    Path("results/prg/prg-18.geojson"),
    Path("results/prg/prg-city-inventory.csv"),
    Path("results/prg/prg-city-inventory.json"),
]
SCHEMA_FIELDS = {
    "geometry": "msGeometry",
    "teryt": "JPT_KOD_JE",
    "unit_type": "JPT_SJR_KO",
    "name": "JPT_NAZWA_",
    "object_id": "JPT_ID",
}
INVENTORY_COLUMNS = [
    "bit_index",
    "city_id",
    "name",
    "teryt",
    "prg_object_id",
    "prg_unit_type",
    "teryt_type",
    "unit_type",
    "extracted_at_utc",
    "source_crs",
    "area_epsg2180_m2",
    "geometry_type",
    "multipart",
    "geos_is_valid",
    "invalidity_reason",
    "source_wfs_response_sha256",
    "make_valid_applied",
]
TEXT_FIELD_WIDTHS = {
    "city_id": 40,
    "name": 80,
    "teryt": 7,
    "prg_object_id": 40,
    "prg_unit_type": 8,
    "teryt_type": 4,
    "unit_type": 40,
    "extracted_at_utc": 32,
    "source_crs": 32,
    "geometry_type": 32,
    "invalidity_reason": 512,
    "source_wfs_response_sha256": 64,
}


def local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1]


def scalar(value: str) -> str:
    return value.strip().strip('"').strip("'")


def canonical_city_name(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def load_instance(path: Path) -> dict[str, object]:
    targets: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    canonical_start: dict[str, str] = {}
    target_count: int | None = None
    in_targets = False
    in_voivodeship = False
    in_canonical_start = False

    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("target_count:"):
            target_count = int(scalar(line.split(":", 1)[1]))
            continue
        if line.startswith("    canonical_start:"):
            in_canonical_start = True
            continue
        if in_canonical_start:
            if line.startswith("      ") and ":" in line:
                key, value = line.strip().split(":", 1)
                canonical_start[key] = scalar(value)
                continue
            in_canonical_start = False
        if line == "targets:":
            in_targets = True
            continue
        if not in_targets:
            continue
        if line and not line.startswith(" "):
            break
        if line.startswith("  - id:"):
            if current is not None:
                targets.append(current)
            current = {"id": scalar(line.split(":", 1)[1])}
            in_voivodeship = False
            continue
        if current is None:
            continue
        if line.startswith("    bit_index:"):
            current["bit_index"] = scalar(line.split(":", 1)[1])
            continue
        if line.startswith("    name_pl:"):
            current["name"] = scalar(line.split(":", 1)[1])
            continue
        if line.startswith("    municipality_teryt:"):
            current["teryt"] = scalar(line.split(":", 1)[1])
            continue
        if line.startswith("    voivodeship:"):
            in_voivodeship = True
            continue
        if in_voivodeship and line.startswith("      teryt:"):
            current["voivodeship_teryt"] = scalar(line.split(":", 1)[1])

    if current is not None:
        targets.append(current)

    required = {"id", "bit_index", "name", "teryt", "voivodeship_teryt"}
    for target in targets:
        missing = sorted(required - target.keys())
        if missing:
            raise ValueError(f"target is missing fields {missing}: {target}")
        target["bit_index"] = int(target["bit_index"])
    if target_count != EXPECTED_TARGET_COUNT or len(targets) != target_count:
        raise ValueError(
            f"manifest target_count={target_count}, parsed targets={len(targets)}"
        )
    if len({target["id"] for target in targets}) != len(targets):
        raise ValueError("target IDs are not unique")
    if [target["bit_index"] for target in targets] != list(
        range(EXPECTED_TARGET_COUNT)
    ):
        raise ValueError("manifest order and bit_index values must be exactly 0..17")
    if len({target["teryt"] for target in targets}) != EXPECTED_TARGET_COUNT:
        raise ValueError("manifest municipality TERYT codes are not unique")
    normalized_names = [canonical_city_name(target["name"]) for target in targets]
    if len(set(normalized_names)) != EXPECTED_TARGET_COUNT:
        raise ValueError("manifest city names collide after Unicode normalization")
    for target in targets:
        if not re.fullmatch(r"\d{7}", target["teryt"]):
            raise ValueError(f"invalid municipality TERYT for {target['name']}")
        if not target["teryt"].startswith(target["voivodeship_teryt"]):
            raise ValueError(f"TERYT voivodeship mismatch for {target['name']}")
        if target["teryt"][-1] != EXPECTED_TERYT_TYPE:
            raise ValueError(f"{target['name']} is not a gmina miejska TERYT")

    start_required = {"id", "name_pl", "crs", "longitude", "latitude"}
    start_missing = sorted(start_required - canonical_start.keys())
    if start_missing:
        raise ValueError(f"canonical_start is missing fields: {start_missing}")
    if canonical_start["crs"] != "EPSG:4326":
        raise ValueError("canonical_start CRS must be EPSG:4326")
    try:
        longitude_decimal = Decimal(canonical_start["longitude"])
        latitude_decimal = Decimal(canonical_start["latitude"])
    except InvalidOperation as error:
        raise ValueError("canonical_start coordinates are not decimal numbers") from error
    if not Decimal("-180") <= longitude_decimal <= Decimal("180"):
        raise ValueError("canonical_start longitude is outside EPSG:4326")
    if not Decimal("-90") <= latitude_decimal <= Decimal("90"):
        raise ValueError("canonical_start latitude is outside EPSG:4326")

    return {
        "target_count": target_count,
        "targets": targets,
        "canonical_start": {
            "id": canonical_start["id"],
            "name": canonical_start["name_pl"],
            "crs": canonical_start["crs"],
            "longitude": float(longitude_decimal),
            "latitude": float(latitude_decimal),
            "longitude_text": canonical_start["longitude"],
            "latitude_text": canonical_start["latitude"],
        },
        "manifest_sha256": sha256_file(path),
    }


def normalized_layer_label(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_value = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "_", ascii_value.casefold()).strip("_")


def select_gmina_feature_type(capabilities_path: Path) -> dict[str, str]:
    root = ET.parse(capabilities_path).getroot()
    candidates: list[dict[str, str]] = []

    for feature_type in root.iter():
        if local_name(feature_type.tag) != "FeatureType":
            continue
        values: dict[str, str] = {}
        for child in feature_type:
            if child.text:
                values[local_name(child.tag)] = child.text.strip()
        name = values.get("Name", "")
        title = values.get("Title", "")
        labels = {
            normalized_layer_label(name.rsplit(":", 1)[-1]),
            normalized_layer_label(title),
        }
        if any(
            label == "granice_gmin" or label.endswith("_granice_gmin")
            for label in labels
        ):
            candidates.append(
                {
                    "name": name,
                    "title": title,
                    "default_crs": values.get(
                        "DefaultCRS", values.get("DefaultSRS", "")
                    ),
                }
            )

    if len(candidates) != 1:
        names = [candidate["name"] for candidate in candidates]
        raise ValueError(
            "saved GetCapabilities must identify exactly one gmina boundary "
            f"FeatureType, found {names}"
        )
    selected = candidates[0]
    if not selected["name"] or not selected["default_crs"]:
        raise ValueError(f"incomplete FeatureType metadata: {selected}")
    return selected


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_source_manifest(repository_root: Path) -> dict[str, object]:
    path = repository_root / SOURCE_MANIFEST_PATH
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("provenance_status") != PROVENANCE_STATUS:
        raise ValueError("PRG source manifest is not provenance-locked")
    selected = manifest.get("selected_raw_input", {})
    if selected.get("wfs_url") != PRG_WFS_URL:
        raise ValueError("source manifest WFS URL differs from the PRG endpoint")
    if selected.get("feature_type") != "ms:A03_Granice_gmin":
        raise ValueError("source manifest FeatureType differs from the saved response")
    if selected.get("crs") != "urn:ogc:def:crs:EPSG::2180":
        raise ValueError("source manifest CRS differs from the saved response")
    return manifest


def materialize_pinned_raw(
    repository_root: Path, source_manifest: dict[str, object]
) -> Path:
    selected = source_manifest["selected_raw_input"]
    compressed_path = repository_root / str(selected["compressed_path"])
    if compressed_path.stat().st_size != int(selected["compressed_byte_size"]):
        raise ValueError("pinned PRG compressed byte size changed")
    if sha256_file(compressed_path) != selected["compressed_sha256"]:
        raise ValueError("pinned PRG compressed SHA-256 changed")

    raw_path = repository_root / str(selected["materialized_path"])
    temporary_path = raw_path.with_suffix(raw_path.suffix + ".part")
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    with temporary_path.open("wb") as output:
        subprocess.run(
            ["zstd", "--decompress", "--stdout", str(compressed_path)],
            check=True,
            stdout=output,
        )
    if temporary_path.stat().st_size != int(selected["byte_size"]):
        temporary_path.unlink(missing_ok=True)
        raise ValueError("pinned PRG uncompressed byte size changed")
    if sha256_file(temporary_path) != selected["raw_sha256"]:
        temporary_path.unlink(missing_ok=True)
        raise ValueError("pinned PRG uncompressed SHA-256 changed")
    temporary_path.replace(raw_path)
    return raw_path


def ensure_wfs_document(data: bytes, request_name: str) -> None:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        raise ValueError(f"{request_name} returned malformed XML: {error}") from error
    root_name = local_name(root.tag)
    if root_name in {"ExceptionReport", "ServiceExceptionReport"}:
        messages = [
            (element.text or "").strip()
            for element in root.iter()
            if local_name(element.tag) in {"ExceptionText", "ServiceException"}
            and (element.text or "").strip()
        ]
        raise ValueError(f"{request_name} failed: {'; '.join(messages)}")


def fetch_wfs(
    params: dict[str, str],
    output_path: Path,
    request_name: str,
    attempts: int = 5,
) -> dict[str, str]:
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{PRG_WFS_URL}?{query}",
        headers={
            "Accept": "application/gml+xml; version=3.2, application/xml",
            "User-Agent": "pl18-exact-routing-prg-stage/1",
        },
    )
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                data = response.read()
                headers = {key.casefold(): value for key, value in response.headers.items()}
            ensure_wfs_document(data, request_name)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = output_path.with_suffix(output_path.suffix + ".part")
            temporary_path.write_bytes(data)
            temporary_path.replace(output_path)
            return headers
        except (OSError, ValueError, urllib.error.URLError) as error:
            last_error = error
            if attempt == attempts:
                break
            time.sleep(5)

    raise RuntimeError(f"{request_name} failed after {attempts} attempts: {last_error}")


def parse_describe_feature_type(
    describe_path: Path, feature_type_local_name: str
) -> dict[str, str]:
    root = ET.parse(describe_path).getroot()
    declared_feature_types = {
        element.attrib.get("name", "")
        for element in root.iter()
        if local_name(element.tag) == "element"
        and element.attrib.get("substitutionGroup", "").endswith("AbstractFeature")
    }
    if feature_type_local_name not in declared_feature_types:
        raise ValueError(
            f"DescribeFeatureType does not declare {feature_type_local_name}"
        )

    schema: dict[str, str] = {}
    for element in root.iter():
        if local_name(element.tag) != "element":
            continue
        name = element.attrib.get("name", "")
        type_name = element.attrib.get("type", "")
        if name:
            schema[name] = type_name

    missing = [
        field_name
        for field_name in SCHEMA_FIELDS.values()
        if field_name not in schema
    ]
    if missing:
        raise ValueError(f"DescribeFeatureType is missing required fields: {missing}")
    if "GeometryPropertyType" not in schema[SCHEMA_FIELDS["geometry"]]:
        raise ValueError(
            f"{SCHEMA_FIELDS['geometry']} is not declared as a geometry property"
        )
    return schema


def build_or_filter(field_name: str, values: list[str]) -> str:
    fes_namespace = "http://www.opengis.net/fes/2.0"
    ET.register_namespace("fes", fes_namespace)
    root = ET.Element(f"{{{fes_namespace}}}Filter")
    parent = ET.SubElement(root, f"{{{fes_namespace}}}Or")
    for value in values:
        equals = ET.SubElement(parent, f"{{{fes_namespace}}}PropertyIsEqualTo")
        ET.SubElement(equals, f"{{{fes_namespace}}}ValueReference").text = field_name
        ET.SubElement(equals, f"{{{fes_namespace}}}Literal").text = value
    return ET.tostring(root, encoding="unicode")


def feature_rows_from_gml(
    path: Path, feature_type_local_name: str
) -> tuple[ET.Element, list[dict[str, str]]]:
    root = ET.parse(path).getroot()
    rows: list[dict[str, str]] = []
    wanted_fields = set(SCHEMA_FIELDS.values())

    for feature in root.iter():
        if local_name(feature.tag) != feature_type_local_name:
            continue
        row: dict[str, str] = {}
        for child in feature:
            child_name = local_name(child.tag)
            if child_name in wanted_fields and child_name != SCHEMA_FIELDS["geometry"]:
                row[child_name] = (child.text or "").strip()
        rows.append(row)
    return root, rows


def discover_targets(
    discovery_path: Path,
    feature_type_local_name: str,
    targets: list[dict[str, str]],
) -> list[dict[str, str]]:
    root, rows = feature_rows_from_gml(discovery_path, feature_type_local_name)
    returned = root.attrib.get("numberReturned")
    if returned is not None and returned.isdigit() and int(returned) != len(rows):
        raise ValueError(
            f"discovery numberReturned={returned}, parsed feature count={len(rows)}"
        )

    selected: list[dict[str, str]] = []
    for target in targets:
        candidates: list[dict[str, str]] = []
        for row in rows:
            name = row.get(SCHEMA_FIELDS["name"], "")
            teryt = row.get(SCHEMA_FIELDS["teryt"], "")
            unit_type = row.get(SCHEMA_FIELDS["unit_type"], "")
            if name != target["name"]:
                continue
            if canonical_city_name(name) != canonical_city_name(target["name"]):
                continue
            if teryt != target["teryt"]:
                continue
            if unit_type != EXPECTED_PRG_UNIT_TYPE:
                continue
            candidates.append(row)

        if len(candidates) != 1:
            summary = [
                {
                    "name": row.get(SCHEMA_FIELDS["name"], ""),
                    "teryt": row.get(SCHEMA_FIELDS["teryt"], ""),
                    "unit_type": row.get(SCHEMA_FIELDS["unit_type"], ""),
                }
                for row in candidates
            ]
            raise ValueError(
                f"{target['name']} did not resolve to one official urban gmina: "
                f"{summary}"
            )

        candidate = candidates[0]
        object_id = candidate.get(SCHEMA_FIELDS["object_id"], "")
        if not object_id:
            raise ValueError(f"{target['name']} has an empty PRG object ID")
        selected.append(
            {
                **target,
                "prg_object_id": object_id,
                "prg_unit_type": candidate[SCHEMA_FIELDS["unit_type"]],
            }
        )

    teryt_codes = [target["teryt"] for target in selected]
    if len(set(teryt_codes)) != EXPECTED_TARGET_COUNT:
        raise ValueError("discovery did not produce 18 unique TERYT codes")
    return selected


def select_targets_from_raw(
    raw_path: Path,
    feature_type_local_name: str,
    targets: list[dict[str, str]],
) -> list[dict[str, str]]:
    _, rows = feature_rows_from_gml(raw_path, feature_type_local_name)
    rows_by_teryt: dict[str, dict[str, str]] = {}
    for row in rows:
        teryt = row.get(SCHEMA_FIELDS["teryt"], "")
        if teryt in rows_by_teryt:
            raise ValueError(f"saved raw GML contains duplicate TERYT {teryt}")
        rows_by_teryt[teryt] = row
    expected_teryt = {target["teryt"] for target in targets}
    if set(rows_by_teryt) != expected_teryt:
        raise ValueError("saved raw GML city set differs from the frozen manifest")

    selected: list[dict[str, str]] = []
    for target in targets:
        row = rows_by_teryt[target["teryt"]]
        name = row.get(SCHEMA_FIELDS["name"], "")
        if name != target["name"]:
            raise ValueError(
                f"TERYT {target['teryt']} name differs from manifest: "
                f"{name!r} != {target['name']!r}"
            )
        if canonical_city_name(name) != canonical_city_name(target["name"]):
            raise ValueError(
                f"TERYT {target['teryt']} normalizes to a different city"
            )
        unit_type = row.get(SCHEMA_FIELDS["unit_type"], "")
        if unit_type != EXPECTED_PRG_UNIT_TYPE:
            raise ValueError(
                f"TERYT {target['teryt']} has unexpected unit type {unit_type}"
            )
        object_id = row.get(SCHEMA_FIELDS["object_id"], "")
        if not object_id:
            raise ValueError(f"{target['name']} has an empty PRG object ID")
        selected.append(
            {
                **target,
                "prg_object_id": object_id,
                "prg_unit_type": unit_type,
            }
        )
    return selected


def srs_for_epsg(code: int) -> osr.SpatialReference:
    spatial_reference = osr.SpatialReference()
    spatial_reference.ImportFromEPSG(code)
    spatial_reference.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return spatial_reference


def authority_code(spatial_reference: osr.SpatialReference) -> str:
    spatial_reference.AutoIdentifyEPSG()
    return (
        spatial_reference.GetAuthorityCode(None)
        or spatial_reference.GetAuthorityCode("PROJCS")
        or spatial_reference.GetAuthorityCode("GEOGCS")
        or ""
    )


def geometry_validity(geometry: ogr.Geometry) -> tuple[bool, str]:
    messages: list[str] = []

    def error_handler(error_class: int, error_number: int, message: str) -> None:
        del error_class, error_number
        if message:
            messages.append(message.strip())

    gdal.PushErrorHandler(error_handler)
    try:
        valid = bool(geometry.IsValid())
    finally:
        gdal.PopErrorHandler()
    reason = "" if valid else "; ".join(dict.fromkeys(messages))
    if not valid and not reason:
        reason = "GEOS reported an invalid geometry without a diagnostic"
    return valid, reason


def geometry_is_multipart(geometry: ogr.Geometry) -> bool:
    geometry_name = geometry.GetGeometryName().upper()
    return geometry_name.startswith("MULTI") or geometry_name == "GEOMETRYCOLLECTION"


def read_raw_geometries(
    raw_path: Path,
    feature_type_local_name: str,
    selected_targets: list[dict[str, str]],
) -> tuple[osr.SpatialReference, dict[str, ogr.Geometry]]:
    sidecar = raw_path.with_suffix(".gfs")
    sidecar.unlink(missing_ok=True)
    dataset = gdal.OpenEx(str(raw_path), gdal.OF_VECTOR)
    if dataset is None:
        raise ValueError(f"GDAL cannot open {raw_path}")
    layer = dataset.GetLayerByName(feature_type_local_name) or dataset.GetLayer(0)
    if layer is None:
        raise ValueError("raw GML contains no readable feature layer")
    source_srs = layer.GetSpatialRef()
    if source_srs is None:
        raise ValueError("raw GML has no source CRS")
    source_srs = source_srs.Clone()
    source_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

    expected_by_teryt = {target["teryt"]: target for target in selected_targets}
    geometries: dict[str, ogr.Geometry] = {}
    for feature in layer:
        teryt = feature.GetFieldAsString(SCHEMA_FIELDS["teryt"])
        if teryt not in expected_by_teryt:
            raise ValueError(f"raw GML contains unexpected TERYT {teryt}")
        if teryt in geometries:
            raise ValueError(f"raw GML contains duplicate TERYT {teryt}")
        target = expected_by_teryt[teryt]
        name = feature.GetFieldAsString(SCHEMA_FIELDS["name"])
        object_id = feature.GetFieldAsString(SCHEMA_FIELDS["object_id"])
        unit_type = feature.GetFieldAsString(SCHEMA_FIELDS["unit_type"])
        if name != target["name"]:
            raise ValueError(
                f"TERYT {teryt} name mismatch: expected {target['name']}, got {name}"
            )
        if object_id != target["prg_object_id"]:
            raise ValueError(
                f"TERYT {teryt} object ID changed between discovery and final response"
            )
        if unit_type != EXPECTED_PRG_UNIT_TYPE:
            raise ValueError(f"TERYT {teryt} has unexpected unit type {unit_type}")
        geometry = feature.GetGeometryRef()
        if geometry is None or geometry.IsEmpty():
            raise ValueError(f"TERYT {teryt} has an empty geometry")
        geometries[teryt] = geometry.Clone()

    dataset = None
    sidecar.unlink(missing_ok=True)
    if set(geometries) != set(expected_by_teryt):
        missing = sorted(set(expected_by_teryt) - set(geometries))
        raise ValueError(f"raw GML is missing TERYT codes: {missing}")
    return source_srs, geometries


def create_vector_output(
    path: Path,
    driver_name: str,
    layer_name: str,
    spatial_reference: osr.SpatialReference,
    records: list[dict[str, object]],
    geometries: list[ogr.Geometry],
    layer_options: list[str] | None = None,
) -> None:
    driver = ogr.GetDriverByName(driver_name)
    if driver is None:
        raise RuntimeError(f"OGR driver {driver_name} is unavailable")
    if path.exists():
        driver.DeleteDataSource(str(path))
    dataset = driver.CreateDataSource(str(path))
    if dataset is None:
        raise RuntimeError(f"cannot create {path}")
    layer = dataset.CreateLayer(
        layer_name,
        spatial_reference,
        geom_type=ogr.wkbUnknown,
        options=layer_options or [],
    )
    if layer is None:
        raise RuntimeError(f"cannot create layer {layer_name} in {path}")

    for column in INVENTORY_COLUMNS:
        if column in {"bit_index", "multipart", "geos_is_valid", "make_valid_applied"}:
            field = ogr.FieldDefn(column, ogr.OFTInteger)
            field.SetSubType(ogr.OFSTBoolean if column != "bit_index" else ogr.OFSTNone)
        elif column == "area_epsg2180_m2":
            field = ogr.FieldDefn(column, ogr.OFTReal)
            field.SetWidth(20)
            field.SetPrecision(3)
        else:
            field = ogr.FieldDefn(column, ogr.OFTString)
            field.SetWidth(TEXT_FIELD_WIDTHS.get(column, 80))
        if layer.CreateField(field) != ogr.OGRERR_NONE:
            raise RuntimeError(f"cannot create field {column} in {path}")

    definition = layer.GetLayerDefn()
    for record, geometry in zip(records, geometries, strict=True):
        feature = ogr.Feature(definition)
        for column in INVENTORY_COLUMNS:
            value = record[column]
            if isinstance(value, bool):
                value = int(value)
            feature.SetField(column, value)
        feature.SetGeometry(geometry)
        if layer.CreateFeature(feature) != ogr.OGRERR_NONE:
            raise RuntimeError(f"cannot write {record['name']} to {path}")
        feature = None

    dataset = None


def pin_geopackage_timestamp(path: Path, retrieval_timestamp: str) -> None:
    timestamp = retrieval_timestamp.removesuffix("Z")
    if not timestamp.endswith(".000"):
        timestamp = f"{timestamp}.000"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE gpkg_contents SET last_change = ?",
            (f"{timestamp}Z",),
        )


def validate_start_point(
    computational_geometries: dict[str, ogr.Geometry],
    canonical_start: dict[str, object],
    warsaw_teryt: str,
) -> dict[str, object]:
    warsaw = computational_geometries.get(warsaw_teryt)
    if warsaw is None:
        raise ValueError("Warszawa geometry from the frozen manifest is missing")
    source = srs_for_epsg(int(GEOJSON_EPSG))
    target = srs_for_epsg(int(COMPUTATIONAL_EPSG))
    transform = osr.CoordinateTransformation(source, target)
    point = ogr.Geometry(ogr.wkbPoint)
    point.AddPoint_2D(
        float(canonical_start["longitude"]),
        float(canonical_start["latitude"]),
    )
    point.AssignSpatialReference(source)
    point.Transform(transform)
    inside = bool(warsaw.Contains(point))
    if not inside:
        raise ValueError(
            f"{canonical_start['name']} ({canonical_start['longitude_text']}, "
            f"{canonical_start['latitude_text']}) is not inside Warszawa"
        )
    return {
        "id": canonical_start["id"],
        "name": canonical_start["name"],
        "longitude": canonical_start["longitude"],
        "latitude": canonical_start["latitude"],
        "longitude_text": canonical_start["longitude_text"],
        "latitude_text": canonical_start["latitude_text"],
        "crs": canonical_start["crs"],
        "inside_warszawa": inside,
    }


def build_outputs(
    repository_root: Path,
    feature_type: dict[str, str],
    describe_sha256: str,
    selected_targets: list[dict[str, str]],
    extracted_at_utc: str,
    canonical_start: dict[str, object],
    manifest_sha256: str,
    official_response_mode: str,
    source_manifest: dict[str, object],
    source_manifest_sha256: str,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    results_dir = repository_root / "results/prg"
    raw_path = results_dir / "prg-18-raw.gml"
    raw_sha256 = sha256_file(raw_path)
    selected_raw = source_manifest["selected_raw_input"]
    if raw_sha256 != selected_raw["raw_sha256"]:
        raise ValueError("materialized raw GML differs from the source manifest")
    raw_root, raw_rows = feature_rows_from_gml(
        raw_path, feature_type["name"].rsplit(":", 1)[-1]
    )
    if len(raw_rows) != EXPECTED_TARGET_COUNT:
        raise ValueError(f"final raw GML contains {len(raw_rows)} features")
    for attribute in ("numberMatched", "numberReturned"):
        value = raw_root.attrib.get(attribute)
        if value is not None and value.isdigit() and int(value) != EXPECTED_TARGET_COUNT:
            raise ValueError(f"final raw GML {attribute}={value}")

    source_srs, source_geometries = read_raw_geometries(
        raw_path,
        feature_type["name"].rsplit(":", 1)[-1],
        selected_targets,
    )
    source_epsg = authority_code(source_srs)
    if source_epsg != EXPECTED_SOURCE_EPSG:
        raise ValueError(
            f"expected source CRS EPSG:{EXPECTED_SOURCE_EPSG}, got EPSG:{source_epsg}"
        )
    computational_srs = srs_for_epsg(int(COMPUTATIONAL_EPSG))
    to_computational = osr.CoordinateTransformation(source_srs, computational_srs)
    to_geojson = osr.CoordinateTransformation(
        computational_srs, srs_for_epsg(int(GEOJSON_EPSG))
    )

    records: list[dict[str, object]] = []
    original_geometries: list[ogr.Geometry] = []
    computational_geometries_list: list[ogr.Geometry] = []
    geojson_geometries: list[ogr.Geometry] = []
    computational_by_teryt: dict[str, ogr.Geometry] = {}

    for target in selected_targets:
        original = source_geometries[target["teryt"]].Clone()
        valid, invalidity_reason = geometry_validity(original)
        computational = original.Clone()
        if source_epsg != COMPUTATIONAL_EPSG:
            computational.Transform(to_computational)
        make_valid_applied = False
        if not valid:
            computational = computational.MakeValid()
            make_valid_applied = True
            if computational is None or computational.IsEmpty():
                raise ValueError(f"MakeValid failed for {target['name']}")
        computational.AssignSpatialReference(computational_srs)
        computational_valid, computational_reason = geometry_validity(computational)
        if not computational_valid:
            raise ValueError(
                f"computational geometry remains invalid for {target['name']}: "
                f"{computational_reason}"
            )
        area = computational.GetArea()
        if area <= 0:
            raise ValueError(f"{target['name']} has non-positive area {area}")

        record: dict[str, object] = {
            "bit_index": target["bit_index"],
            "city_id": target["id"],
            "name": target["name"],
            "teryt": target["teryt"],
            "prg_object_id": target["prg_object_id"],
            "prg_unit_type": target["prg_unit_type"],
            "teryt_type": target["teryt"][-1],
            "unit_type": "gmina miejska",
            "extracted_at_utc": extracted_at_utc,
            "source_crs": f"EPSG:{source_epsg}",
            "area_epsg2180_m2": round(area, 3),
            "geometry_type": original.GetGeometryName(),
            "multipart": geometry_is_multipart(original),
            "geos_is_valid": valid,
            "invalidity_reason": invalidity_reason,
            "source_wfs_response_sha256": raw_sha256,
            "make_valid_applied": make_valid_applied,
        }
        records.append(record)
        original_geometries.append(original)
        computational_geometries_list.append(computational)
        computational_by_teryt[target["teryt"]] = computational.Clone()
        geojson_geometry = computational.Clone()
        geojson_geometry.Transform(to_geojson)
        geojson_geometries.append(geojson_geometry)

    warsaw_targets = [
        target for target in selected_targets if target["id"] == "warszawa"
    ]
    if len(warsaw_targets) != 1:
        raise ValueError("frozen manifest must contain exactly one Warszawa target")
    start_point_result = validate_start_point(
        computational_by_teryt,
        canonical_start,
        warsaw_targets[0]["teryt"],
    )

    create_vector_output(
        results_dir / "prg-18-original.gpkg",
        "GPKG",
        "prg_18_original",
        source_srs,
        records,
        original_geometries,
        ["SPATIAL_INDEX=YES"],
    )
    create_vector_output(
        results_dir / "prg-18-computational.gpkg",
        "GPKG",
        "prg_18_computational",
        computational_srs,
        records,
        computational_geometries_list,
        ["SPATIAL_INDEX=YES"],
    )
    pin_geopackage_timestamp(
        results_dir / "prg-18-original.gpkg",
        str(selected_raw["retrieval_timestamp"]),
    )
    pin_geopackage_timestamp(
        results_dir / "prg-18-computational.gpkg",
        str(selected_raw["retrieval_timestamp"]),
    )
    create_vector_output(
        results_dir / "prg-18.geojson",
        "GeoJSON",
        "prg_18",
        srs_for_epsg(int(GEOJSON_EPSG)),
        records,
        geojson_geometries,
        ["RFC7946=YES", "WRITE_BBOX=YES", "COORDINATE_PRECISION=7"],
    )

    metadata: dict[str, object] = {
        "stage_status": "PRG_STAGE_OK",
        "provenance_status": PROVENANCE_STATUS,
        "solver_status": "SOLVER_NOT_STARTED",
        "source": {
            "provider": "Główny Urząd Geodezji i Kartografii",
            "registry": "Państwowy Rejestr Granic",
            "wfs_endpoint": PRG_WFS_URL,
            "wfs_version": WFS_VERSION,
            "feature_type": feature_type["name"],
            "feature_type_title": feature_type["title"],
            "schema_fields": SCHEMA_FIELDS,
            "get_capabilities_stage0_run": STAGE0_RUN_URL,
            "get_capabilities_sha256": sha256_file(
                repository_root / "input/prg/GetCapabilities.xml"
            ),
            "describe_feature_type_sha256": describe_sha256,
            "raw_gml_sha256": raw_sha256,
            "raw_gml_compressed_sha256": selected_raw["compressed_sha256"],
            "source_manifest_path": SOURCE_MANIFEST_PATH.as_posix(),
            "source_manifest_sha256": source_manifest_sha256,
            "wfs_collection_timestamp": raw_root.attrib.get("timeStamp", ""),
            "extracted_at_utc": extracted_at_utc,
            "source_crs": f"EPSG:{source_epsg}",
        },
        "processing": {
            "computational_crs": f"EPSG:{COMPUTATIONAL_EPSG}",
            "geojson_crs": f"EPSG:{GEOJSON_EPSG}",
            "geos_version": (
                f"{ogr.GetGEOSVersionMajor()}."
                f"{ogr.GetGEOSVersionMinor()}."
                f"{ogr.GetGEOSVersionMicro()}"
            ),
            "gdal_version": gdal.VersionInfo("RELEASE_NAME"),
            "original_geometry_policy": "copied without MakeValid",
            "computational_geometry_policy": "MakeValid only when original is invalid",
            "official_response_mode": official_response_mode,
        },
        "manifest": {
            "path": "instance/pl_18_capitals_static_instance_v1.yaml",
            "sha256": manifest_sha256,
            "source_of_truth": True,
        },
        "canonical_start_check": start_point_result,
        "cities": records,
    }
    return records, metadata


def write_inventory(
    results_dir: Path,
    records: list[dict[str, object]],
    metadata: dict[str, object],
) -> None:
    csv_path = results_dir / "prg-city-inventory.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(
            output, fieldnames=INVENTORY_COLUMNS, lineterminator="\n"
        )
        writer.writeheader()
        for record in records:
            row = {
                key: (
                    str(value).lower()
                    if isinstance(value, bool)
                    else value
                )
                for key, value in record.items()
            }
            writer.writerow(row)

    json_path = results_dir / "prg-city-inventory.json"
    json_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_derived_artifacts(
    repository_root: Path, metadata: dict[str, object]
) -> dict[str, object]:
    source_manifest_sha256 = str(metadata["source"]["source_manifest_sha256"])
    raw_gml_sha256 = str(metadata["source"]["raw_gml_sha256"])
    generator_path = Path("scripts/prg_stage.py")
    artifacts = {
        path.as_posix(): {
            "artifact_sha256": sha256_file(repository_root / path),
            "raw_gml_sha256": raw_gml_sha256,
            "manifest_sha256": source_manifest_sha256,
            "source_manifest_sha256": source_manifest_sha256,
            "instance_manifest_sha256": metadata["manifest"]["sha256"],
            "gdal_version": metadata["processing"]["gdal_version"],
            "geos_version": metadata["processing"]["geos_version"],
            "generator": {
                "script": generator_path.as_posix(),
                "script_sha256": sha256_file(repository_root / generator_path),
            },
        }
        for path in DERIVED_PRODUCT_PATHS
    }
    document = {
        "schema_version": 1,
        "provenance_status": PROVENANCE_STATUS,
        "solver_status": "SOLVER_NOT_STARTED",
        "selected_raw_gml_sha256": raw_gml_sha256,
        "source_manifest": {
            "path": SOURCE_MANIFEST_PATH.as_posix(),
            "sha256": source_manifest_sha256,
        },
        "artifacts": artifacts,
    }
    output_path = repository_root / DERIVED_ARTIFACTS_PATH
    output_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return document


def write_validation_report(
    results_dir: Path,
    feature_type: dict[str, str],
    metadata: dict[str, object],
    records: list[dict[str, object]],
    source_manifest: dict[str, object],
    derived_artifacts: dict[str, object],
) -> None:
    source = metadata["source"]
    start_point = metadata["canonical_start_check"]
    selected_raw = source_manifest["selected_raw_input"]
    historical_raw = source_manifest["historical_raw_inputs"][0]
    resolution = source_manifest["hash_difference_resolution"]
    valid_count = sum(bool(record["geos_is_valid"]) for record in records)
    repaired_count = sum(bool(record["make_valid_applied"]) for record in records)
    manifest_city_set = {
        (str(record["city_id"]), str(record["name"]), str(record["teryt"]))
        for record in records
    }
    checks = [
        ("Exactly 18 PRG objects", len(records) == EXPECTED_TARGET_COUNT),
        ("City set matches the frozen manifest", len(manifest_city_set) == 18),
        (
            "18 unique TERYT codes",
            len({str(record["teryt"]) for record in records}) == EXPECTED_TARGET_COUNT,
        ),
        ("No empty geometries", True),
        (
            "Exact manifest start coordinate is inside Warszawa",
            bool(start_point["inside_warszawa"]),
        ),
        ("Computational CRS is EPSG:2180", True),
        ("GeoJSON is published in EPSG:4326", True),
        (
            "All EPSG:2180 areas are positive",
            all(float(record["area_epsg2180_m2"]) > 0 for record in records),
        ),
        (
            "Canonical order and bit indexes follow the frozen manifest",
            [int(record["bit_index"]) for record in records]
            == list(range(EXPECTED_TARGET_COUNT)),
        ),
        (
            "Materialized raw GML matches the pinned source manifest",
            source["raw_gml_sha256"] == selected_raw["raw_sha256"],
        ),
        (
            "Every mapped derived product uses the selected raw GML",
            all(
                artifact["raw_gml_sha256"] == selected_raw["raw_sha256"]
                for artifact in derived_artifacts["artifacts"].values()
            ),
        ),
        (
            "The two saved responses contain identical canonical PRG objects",
            bool(resolution["canonical_object_comparison"]["identical"])
            and int(resolution["canonical_object_comparison"]["feature_count"])
            == EXPECTED_TARGET_COUNT,
        ),
    ]
    if not all(passed for _, passed in checks):
        raise ValueError("one or more validation report checks failed")

    lines = [
        "# PRG Stage 1 validation report",
        "",
        "- Stage status: `PRG_STAGE_OK`",
        f"- Provenance status: `{PROVENANCE_STATUS}`",
        "- Solver status: `SOLVER_NOT_STARTED`",
        f"- Official WFS FeatureType: `{feature_type['name']}`",
        f"- TERYT field: `{SCHEMA_FIELDS['teryt']}`",
        f"- PRG object ID field: `{SCHEMA_FIELDS['object_id']}`",
        f"- City name field: `{SCHEMA_FIELDS['name']}`",
        f"- Unit type field: `{SCHEMA_FIELDS['unit_type']}`",
        f"- Geometry field: `{SCHEMA_FIELDS['geometry']}`",
        (
            "- Frozen manifest SHA-256: "
            f"`{metadata['manifest']['sha256']}`"
        ),
        f"- Source CRS: `{source['source_crs']}`",
        f"- Extraction time: `{source['extracted_at_utc']}`",
        (
            "- DescribeFeatureType SHA-256: "
            f"`{source['describe_feature_type_sha256']}`"
        ),
        f"- Selected raw GML SHA-256: `{source['raw_gml_sha256']}`",
        (
            "- Selected compressed GML SHA-256: "
            f"`{selected_raw['compressed_sha256']}`"
        ),
        (
            "- Source manifest SHA-256: "
            f"`{source['source_manifest_sha256']}`"
        ),
        (
            "- Official response mode: "
            f"`{metadata['processing']['official_response_mode']}`"
        ),
        f"- Original geometries valid in GEOS: `{valid_count}/18`",
        f"- Computational MakeValid copies created: `{repaired_count}`",
        "",
        "## Automated checks",
        "",
        "| Check | Result |",
        "|---|---|",
    ]
    lines.extend(
        f"| {name} | `{'PASS' if passed else 'FAIL'}` |"
        for name, passed in checks
    )
    lines.extend(
        [
            "",
            "## Canonical start coordinate",
            "",
            "| Point | EPSG:4326 longitude | EPSG:4326 latitude | Inside Warszawa |",
            "|---|---:|---:|---|",
            (
                f"| {start_point['name']} | {start_point['longitude_text']} | "
                f"{start_point['latitude_text']} | "
                f"`{'PASS' if start_point['inside_warszawa'] else 'FAIL'}` |"
            ),
            "",
            "## Raw input provenance",
            "",
            "| Role | WFS timestamp | Bytes | Raw SHA-256 | Compressed SHA-256 |",
            "|---|---|---:|---|---|",
            (
                f"| Selected pinned input | `{selected_raw['retrieval_timestamp']}` | "
                f"{selected_raw['byte_size']} | `{selected_raw['raw_sha256']}` | "
                f"`{selected_raw['compressed_sha256']}` |"
            ),
            (
                f"| Old saved report response | "
                f"`{historical_raw['wfs_collection_timestamp']}` | "
                f"{historical_raw['byte_size']} | `{historical_raw['raw_sha256']}` | "
                f"`{historical_raw['compressed_sha256']}` |"
            ),
            "",
            (
                "The old and selected raw GML files are both retained. Their only "
                "byte-level difference is "
                f"`{resolution['only_byte_difference']}`: "
                f"`{resolution['historical_timestamp']}` in the old response and "
                f"`{resolution['selected_timestamp']}` in the selected response. "
                "After replacing only that attribute with a fixed marker, both "
                "responses have SHA-256 "
                f"`{resolution['timestamp_normalized_sha256']}`."
            ),
            "",
            (
                "The earlier report mode "
                f"`{resolution['prior_report_mode']}` meant only that no new "
                "response was downloaded; it did not identify the reused file. "
                f"{resolution['resolution']}"
            ),
            "",
            (
                "The 18 canonical objects were joined by "
                f"`{resolution['canonical_object_comparison']['join_key']}` and "
                "compared using `JPT_NAZWA_`, `JPT_ID`, and ISO WKB. Result: "
                f"`{resolution['canonical_object_comparison']['feature_count']}/18 "
                "identical`. No geometry or PRG identity difference was found."
            ),
            "",
            (
                "The selected input is permanently pinned at "
                f"`{selected_raw['compressed_path']}`. The old response is retained at "
                f"`{historical_raw['compressed_path']}`. Neither depends on a "
                "temporary Actions artifact."
            ),
            "",
            "## Derived artifact lineage",
            "",
            "| Derived file | Exact raw GML SHA-256 | Artifact SHA-256 |",
            "|---|---|---|",
        ]
    )
    for path in DERIVED_PRODUCT_PATHS:
        artifact = derived_artifacts["artifacts"][path.as_posix()]
        lines.append(
            f"| `{path.as_posix()}` | `{artifact['raw_gml_sha256']}` | "
            f"`{artifact['artifact_sha256']}` |"
        )
    lines.extend(
        [
            "",
            (
                "All five products above were generated from selected raw GML "
                f"`{selected_raw['raw_sha256']}`. Their machine-readable mapping is "
                f"`{DERIVED_ARTIFACTS_PATH.as_posix()}`."
            ),
            "",
            "## Canonical city and bitmask order",
            "",
            (
                "| Bit index | City | TERYT | PRG object ID | Unit | Geometry | "
                "Multipart | GEOS valid | Area EPSG:2180 (m²) |"
            ),
            "|---:|---|---|---|---|---|---|---|---:|",
        ]
    )
    for record in records:
        lines.append(
            f"| {record['bit_index']} | {record['name']} | "
            f"`{record['teryt']}` | `{record['prg_object_id']}` | "
            f"`{record['prg_unit_type']}/{record['teryt_type']}` | "
            f"`{record['geometry_type']}` | "
            f"`{str(record['multipart']).lower()}` | "
            f"`{str(record['geos_is_valid']).lower()}` | "
            f"{float(record['area_epsg2180_m2']):.3f} |"
        )
    lines.extend(
        [
            "",
            "Original geometries are retained without MakeValid. If an original "
            "geometry is invalid, only the separate computational copy is repaired.",
            "",
            "No route optimizer or solver was started in this stage.",
            "",
            f"`{PROVENANCE_STATUS}`",
            "",
            "`SOLVER_NOT_STARTED`",
            "",
        ]
    )
    (results_dir / "validation-report.md").write_text(
        "\n".join(lines), encoding="utf-8"
    )


def write_sha256sums(repository_root: Path) -> None:
    paths = [
        Path("input/prg/GetCapabilities.xml"),
        Path("input/prg/DescribeFeatureType.xml"),
        Path("input/prg/source-manifest.json"),
        Path("input/prg/prg-18-raw.gml.zst"),
        Path(
            "input/prg/history/"
            "prg-18-raw-0e3f113673620334867151f8845853680d31ff4b511fdbf2124eb2d1d64f4838"
            ".gml.zst"
        ),
        Path("results/prg/prg-18-raw.gml"),
        Path("results/prg/prg-18-original.gpkg"),
        Path("results/prg/prg-18-computational.gpkg"),
        Path("results/prg/prg-18.geojson"),
        Path("results/prg/prg-city-inventory.csv"),
        Path("results/prg/prg-city-inventory.json"),
        Path("results/prg/derived-artifacts.json"),
        Path("results/prg/validation-report.md"),
    ]
    lines = [
        f"{sha256_file(repository_root / path)}  {path.as_posix()}" for path in paths
    ]
    (repository_root / "results/prg/SHA256SUMS").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def preserved_extraction_time(
    inventory_path: Path,
    raw_sha256: str,
    raw_path: Path,
) -> str:
    if inventory_path.exists():
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        source = inventory.get("source", {})
        if source.get("raw_gml_sha256") == raw_sha256:
            value = source.get("extracted_at_utc", "")
            if value:
                return str(value)
    raw_root = ET.parse(raw_path).getroot()
    timestamp = raw_root.attrib.get("timeStamp", "")
    if timestamp:
        return f"{timestamp}+00:00"
    raise ValueError("cannot preserve extraction time for saved raw GML")


def run(repository_root: Path, refresh_official_responses: bool) -> None:
    if refresh_official_responses:
        raise ValueError(
            "the provenance lock forbids overwriting the selected PRG responses; "
            "capture any future response under a new immutable path and audit it "
            "before changing input/prg/source-manifest.json"
        )

    capabilities_path = repository_root / "input/prg/GetCapabilities.xml"
    describe_path = repository_root / "input/prg/DescribeFeatureType.xml"
    instance_path = (
        repository_root / "instance/pl_18_capitals_static_instance_v1.yaml"
    )
    results_dir = repository_root / "results/prg"
    results_dir.mkdir(parents=True, exist_ok=True)

    source_manifest = load_source_manifest(repository_root)
    source_manifest_sha256 = sha256_file(repository_root / SOURCE_MANIFEST_PATH)
    selected_raw = source_manifest["selected_raw_input"]
    raw_path = materialize_pinned_raw(repository_root, source_manifest)
    instance = load_instance(instance_path)
    targets = instance["targets"]
    feature_type = select_gmina_feature_type(capabilities_path)
    feature_type_local = feature_type["name"].rsplit(":", 1)[-1]
    print(f"Selected PRG WFS FeatureType: {feature_type['name']}")
    if feature_type["name"] != selected_raw["feature_type"]:
        raise ValueError("GetCapabilities FeatureType differs from source manifest")
    if feature_type["default_crs"] != selected_raw["crs"]:
        raise ValueError("GetCapabilities CRS differs from source manifest")

    schema = parse_describe_feature_type(describe_path, feature_type_local)
    describe_sha256 = sha256_file(describe_path)
    if describe_sha256 != selected_raw["describe_feature_type_sha256"]:
        raise ValueError("DescribeFeatureType differs from source manifest")
    print(f"DescribeFeatureType SHA-256: {describe_sha256}")
    print(
        "Confirmed schema fields: "
        + ", ".join(
            f"{role}={field_name} ({schema[field_name]})"
            for role, field_name in SCHEMA_FIELDS.items()
        )
    )

    selected_targets = select_targets_from_raw(raw_path, feature_type_local, targets)
    extracted_at_utc = str(
        selected_raw["permanent_evidence"]["inventory_extracted_at_utc"]
    )
    official_response_mode = "reused_pinned_response"

    print("Manifest-verified official TERYT codes:")
    for target in selected_targets:
        print(
            f"  bit {target['bit_index']} {target['name']}: {target['teryt']} "
            f"(PRG object ID {target['prg_object_id']})"
        )

    records, metadata = build_outputs(
        repository_root,
        feature_type,
        describe_sha256,
        selected_targets,
        extracted_at_utc,
        instance["canonical_start"],
        instance["manifest_sha256"],
        official_response_mode,
        source_manifest,
        source_manifest_sha256,
    )
    write_inventory(results_dir, records, metadata)
    derived_artifacts = write_derived_artifacts(repository_root, metadata)
    write_validation_report(
        results_dir,
        feature_type,
        metadata,
        records,
        source_manifest,
        derived_artifacts,
    )
    write_sha256sums(repository_root)
    print(f"Raw GML SHA-256: {metadata['source']['raw_gml_sha256']}")
    print(PROVENANCE_STATUS)
    print("SOLVER_NOT_STARTED")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract and validate official PRG boundaries for 18 capitals."
    )
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
    )
    parser.add_argument(
        "--refresh-official-responses",
        action="store_true",
        help="Rejected by the provenance lock; retained to fail old callers safely.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    gdal.UseExceptions()
    try:
        run(
            args.repository_root.resolve(),
            refresh_official_responses=args.refresh_official_responses,
        )
    except Exception as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
