# pl18-exact-routing

## Stage 0: preflight

The preflight workflow verifies the pinned Poland OSM PBF and records the
official PRG WFS capabilities. The PBF itself is retained only in the GitHub
Actions cache and is not uploaded as an artifact.

## Stage 1: official PRG boundaries

The frozen instance manifest is the only source of truth for the canonical
start coordinate, city set, TERYT codes, city order, and future bit indexes.
`scripts/prg_stage.sh` uses the saved Stage 0 `GetCapabilities` document to
select the gmina boundary FeatureType and regenerates Stage 1 products from
the saved official `DescribeFeatureType` and the permanently pinned
`input/prg/prg-18-raw.gml.zst`. `input/prg/source-manifest.json` records the
complete WFS request and both compressed and uncompressed hashes.

The original PRG geometries are copied without repair. A separate
EPSG:2180 computational GeoPackage applies GEOS MakeValid only if an original
geometry is invalid. The published GeoJSON is RFC 7946 / EPSG:4326.

Run locally with GDAL Python bindings available:

```bash
bash scripts/prg_stage.sh
```

`results/prg/derived-artifacts.json` maps every Stage 1 vector and inventory
product to the exact raw GML, source manifest, GDAL/GEOS versions, and
generator script hash. The provenance lock rejects the old refresh flag:
future official responses must be retained under new immutable paths and
audited before the selected source manifest can change.

Stage 1 does not download or access the OSM PBF, and it does not start a route
optimizer or solver.
