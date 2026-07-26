#!/usr/bin/env python3

import hashlib
import json
import subprocess
import tempfile
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

from osgeo import gdal, ogr

from prg_stage import PRG_WFS_URL, build_or_filter, local_name


SELECTED_RAW_SHA256 = (
    "adf261aae31257dd24843b6f6c0e694efb40041aae3913b1c27aa4801866974c"
)
SELECTED_COMPRESSED_SHA256 = (
    "91bdab09b9fe77ea02add8cfdf88a41c03ae84587ff8fdfc1808fb340fa41684"
)
HISTORICAL_RAW_SHA256 = (
    "0e3f113673620334867151f8845853680d31ff4b511fdbf2124eb2d1d64f4838"
)
HISTORICAL_COMPRESSED_SHA256 = (
    "07a1f4912264e1a1fae7686e334cfc3e1aae41c90119c1b46646d9563b6cd394"
)
DESCRIBE_FEATURE_TYPE_SHA256 = (
    "cc81f378de423ad27c91e21ecfeab4e6765b4765140ca27953d6b8d69dfa884b"
)
NORMALIZED_RAW_SHA256 = (
    "057fcd8be1965d32dcdd1f8ead62bee926086da2d090e5d130f0a33f30c5a374"
)
SELECTED_COMPRESSED_PATH = Path("input/prg/prg-18-raw.gml.zst")
HISTORICAL_COMPRESSED_PATH = Path(
    "input/prg/history/"
    "prg-18-raw-0e3f113673620334867151f8845853680d31ff4b511fdbf2124eb2d1d64f4838"
    ".gml.zst"
)
MATERIALIZED_RAW_PATH = Path("results/prg/prg-18-raw.gml")
SOURCE_MANIFEST_PATH = Path("input/prg/source-manifest.json")
HISTORICAL_REQUEST_TERYT = [
    "2061011",
    "0461011",
    "2261011",
    "0861011",
    "2469011",
    "2661011",
    "1261011",
    "0663011",
    "1061011",
    "2862011",
    "1661011",
    "3064011",
    "1863011",
    "3262011",
    "0463011",
    "1465011",
    "0264011",
    "0862011",
]


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def decompress_zstd(path: Path) -> bytes:
    result = subprocess.run(
        ["zstd", "--decompress", "--stdout", str(path)],
        check=True,
        capture_output=True,
    )
    return result.stdout


def timestamp(value: bytes) -> str:
    root = ET.fromstring(value)
    if local_name(root.tag) != "FeatureCollection":
        raise ValueError("saved raw response is not a WFS FeatureCollection")
    result = root.attrib.get("timeStamp", "")
    if not result:
        raise ValueError("saved raw response has no timeStamp")
    return result


def normalize_timestamp(value: bytes) -> bytes:
    text = value.decode("utf-8")
    start = text.index('timeStamp="') + len('timeStamp="')
    end = text.index('"', start)
    return (text[:start] + "NORMALIZED" + text[end:]).encode("utf-8")


def feature_identity(path: Path) -> dict[str, tuple[str, str, bytes]]:
    dataset = gdal.OpenEx(str(path), gdal.OF_VECTOR)
    if dataset is None:
        raise ValueError(f"cannot open {path}")
    layer = dataset.GetLayer(0)
    if layer is None:
        raise ValueError(f"{path} has no layer")
    features: dict[str, tuple[str, str, bytes]] = {}
    for feature in layer:
        teryt = feature.GetFieldAsString("JPT_KOD_JE")
        geometry = feature.GetGeometryRef()
        if teryt in features or geometry is None:
            raise ValueError(f"invalid feature identity for TERYT {teryt}")
        features[teryt] = (
            feature.GetFieldAsString("JPT_NAZWA_"),
            feature.GetFieldAsString("JPT_ID"),
            geometry.ExportToIsoWkb(ogr.wkbNDR),
        )
    dataset = None
    return features


def complete_get_feature_query() -> dict[str, object]:
    parameters = {
        "SERVICE": "WFS",
        "VERSION": "2.0.0",
        "REQUEST": "GetFeature",
        "TYPENAMES": "ms:A03_Granice_gmin",
        "SRSNAME": "urn:ogc:def:crs:EPSG::2180",
        "SORTBY": "JPT_KOD_JE",
        "FILTER": build_or_filter("JPT_KOD_JE", HISTORICAL_REQUEST_TERYT),
    }
    encoded_query = urllib.parse.urlencode(parameters)
    return {
        "parameters": parameters,
        "encoded_query": encoded_query,
        "request_url": f"{PRG_WFS_URL}?{encoded_query}",
    }


def build_source_manifest(repository_root: Path) -> dict[str, object]:
    selected_compressed_path = repository_root / SELECTED_COMPRESSED_PATH
    historical_compressed_path = repository_root / HISTORICAL_COMPRESSED_PATH
    selected_compressed_sha256 = sha256_file(selected_compressed_path)
    historical_compressed_sha256 = sha256_file(historical_compressed_path)
    if selected_compressed_sha256 != SELECTED_COMPRESSED_SHA256:
        raise ValueError("selected compressed raw input SHA-256 changed")
    if historical_compressed_sha256 != HISTORICAL_COMPRESSED_SHA256:
        raise ValueError("historical compressed raw input SHA-256 changed")

    selected = decompress_zstd(selected_compressed_path)
    historical = decompress_zstd(historical_compressed_path)
    if sha256_bytes(selected) != SELECTED_RAW_SHA256:
        raise ValueError("selected uncompressed raw input SHA-256 changed")
    if sha256_bytes(historical) != HISTORICAL_RAW_SHA256:
        raise ValueError("historical uncompressed raw input SHA-256 changed")
    materialized = (repository_root / MATERIALIZED_RAW_PATH).read_bytes()
    if materialized != selected:
        raise ValueError("materialized raw GML differs from selected pinned input")

    selected_normalized = sha256_bytes(normalize_timestamp(selected))
    historical_normalized = sha256_bytes(normalize_timestamp(historical))
    if selected_normalized != NORMALIZED_RAW_SHA256:
        raise ValueError("selected normalized raw SHA-256 changed")
    if historical_normalized != NORMALIZED_RAW_SHA256:
        raise ValueError("historical normalized raw SHA-256 changed")

    with tempfile.TemporaryDirectory(prefix="pl18-prg-provenance-") as directory:
        selected_path = Path(directory) / "selected.gml"
        historical_path = Path(directory) / "historical.gml"
        selected_path.write_bytes(selected)
        historical_path.write_bytes(historical)
        selected_features = feature_identity(selected_path)
        historical_features = feature_identity(historical_path)
    if selected_features != historical_features or len(selected_features) != 18:
        raise ValueError("saved raw responses differ in canonical PRG objects")

    describe_path = repository_root / "input/prg/DescribeFeatureType.xml"
    if sha256_file(describe_path) != DESCRIBE_FEATURE_TYPE_SHA256:
        raise ValueError("DescribeFeatureType SHA-256 changed")

    return {
        "schema_version": 1,
        "provenance_status": "PRG_PROVENANCE_LOCKED",
        "selected_raw_input": {
            "wfs_url": PRG_WFS_URL,
            "get_feature_query": complete_get_feature_query(),
            "feature_type": "ms:A03_Granice_gmin",
            "crs": "urn:ogc:def:crs:EPSG::2180",
            "retrieval_timestamp": timestamp(selected),
            "retrieval_timestamp_source": "wfs:FeatureCollection/@timeStamp",
            "http_metadata": {
                "request_method": "GET",
                "request_headers": {
                    "Accept": (
                        "application/gml+xml; version=3.2, application/xml"
                    ),
                    "User-Agent": "pl18-exact-routing-prg-stage/1",
                },
                "response_status": None,
                "response_headers": {},
                "availability": (
                    "The initial Stage 1 fetch kept HTTP response headers only "
                    "in memory. The committed response body, WFS timeStamp, and "
                    "request metadata are available; status and response headers "
                    "were not persisted."
                ),
            },
            "byte_size": len(selected),
            "raw_sha256": SELECTED_RAW_SHA256,
            "compressed_path": SELECTED_COMPRESSED_PATH.as_posix(),
            "compressed_byte_size": selected_compressed_path.stat().st_size,
            "compressed_sha256": SELECTED_COMPRESSED_SHA256,
            "materialized_path": MATERIALIZED_RAW_PATH.as_posix(),
            "describe_feature_type_path": "input/prg/DescribeFeatureType.xml",
            "describe_feature_type_sha256": DESCRIBE_FEATURE_TYPE_SHA256,
            "permanent_evidence": {
                "first_git_commit": (
                    "f5fc0ef45d381a1c00ffcdce2f681b10f44cd210"
                ),
                "inventory_extracted_at_utc": "2026-07-26T16:04:26+00:00",
            },
        },
        "historical_raw_inputs": [
            {
                "role": "old_report_response",
                "wfs_collection_timestamp": timestamp(historical),
                "byte_size": len(historical),
                "raw_sha256": HISTORICAL_RAW_SHA256,
                "compressed_path": HISTORICAL_COMPRESSED_PATH.as_posix(),
                "compressed_byte_size": historical_compressed_path.stat().st_size,
                "compressed_sha256": HISTORICAL_COMPRESSED_SHA256,
                "saved_run": {
                    "url": (
                        "https://github.com/fgsdde5q/pl18-exact-routing/"
                        "actions/runs/30209806566"
                    ),
                    "job_id": 89814017936,
                    "artifact_id": 8634127615,
                    "artifact_name": "prg-stage-30209806566",
                    "artifact_archive_sha256": (
                        "1dcca03a67f5496664c9437036319bd950951c0d620a69141df7ab5d57bb6444"
                    ),
                },
            }
        ],
        "hash_difference_resolution": {
            "prior_report_mode": "reused_saved_responses",
            "selected_raw_sha256": SELECTED_RAW_SHA256,
            "historical_raw_sha256": HISTORICAL_RAW_SHA256,
            "byte_sizes_equal": len(selected) == len(historical),
            "only_byte_difference": "wfs:FeatureCollection/@timeStamp",
            "selected_timestamp": timestamp(selected),
            "historical_timestamp": timestamp(historical),
            "timestamp_normalized_sha256": NORMALIZED_RAW_SHA256,
            "canonical_object_comparison": {
                "join_key": "JPT_KOD_JE",
                "compared_fields": ["JPT_NAZWA_", "JPT_ID", "ISO_WKB"],
                "feature_count": len(selected_features),
                "identical": True,
            },
            "used_for_current_derived_artifacts": SELECTED_RAW_SHA256,
            "resolution": (
                "The committed raw GML, embedded vector and inventory source "
                "hashes, and 18/18 raw-to-original geometry comparison identify "
                "the selected response unambiguously."
            ),
        },
    }


def main() -> int:
    repository_root = Path(__file__).resolve().parent.parent
    gdal.UseExceptions()
    source_manifest = build_source_manifest(repository_root)
    output_path = repository_root / SOURCE_MANIFEST_PATH
    output_path.write_text(
        json.dumps(source_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {output_path}")
    print("PRG_PROVENANCE_LOCKED")
    print("SOLVER_NOT_STARTED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
