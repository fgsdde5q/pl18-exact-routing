# pl18-exact-routing

## Stage 0: preflight

The preflight workflow verifies the pinned Poland OSM PBF and records the
official PRG WFS capabilities. The PBF itself is retained only in the GitHub
Actions cache and is not uploaded as an artifact.

## Stage 1: official PRG boundaries

`scripts/prg_stage.sh` uses the saved Stage 0 `GetCapabilities` document to
select the gmina boundary FeatureType, fetches and records
`DescribeFeatureType`, discovers the 18 capitals, and performs the final WFS
export using only verified TERYT codes.

The original PRG geometries are copied without repair. A separate
EPSG:2180 computational GeoPackage applies GEOS MakeValid only if an original
geometry is invalid. The published GeoJSON is RFC 7946 / EPSG:4326.

Run locally with GDAL Python bindings available:

```bash
bash scripts/prg_stage.sh
```

Stage 1 does not download or access the OSM PBF, and it does not start a route
optimizer or solver.
