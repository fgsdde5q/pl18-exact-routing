# PRG Stage 1 validation report

- Stage status: `PRG_STAGE_OK`
- Provenance status: `PRG_PROVENANCE_LOCKED`
- Solver status: `SOLVER_NOT_STARTED`
- Official WFS FeatureType: `ms:A03_Granice_gmin`
- TERYT field: `JPT_KOD_JE`
- PRG object ID field: `JPT_ID`
- City name field: `JPT_NAZWA_`
- Unit type field: `JPT_SJR_KO`
- Geometry field: `msGeometry`
- Frozen manifest SHA-256: `9b3508725f5e19a843235b931d8e37d7bd4fcba976bf6a71a905b6cb0630a3b4`
- Source CRS: `EPSG:2180`
- Extraction time: `2026-07-26T16:04:26+00:00`
- DescribeFeatureType SHA-256: `cc81f378de423ad27c91e21ecfeab4e6765b4765140ca27953d6b8d69dfa884b`
- Selected raw GML SHA-256: `adf261aae31257dd24843b6f6c0e694efb40041aae3913b1c27aa4801866974c`
- Selected compressed GML SHA-256: `91bdab09b9fe77ea02add8cfdf88a41c03ae84587ff8fdfc1808fb340fa41684`
- Source manifest SHA-256: `2d084b7fdc9c0048d83e7153ad5abff32f9184efa42a0bfbff29731ddabcc092`
- Official response mode: `reused_pinned_response`
- Original geometries valid in GEOS: `18/18`
- Computational MakeValid copies created: `0`

## Automated checks

| Check | Result |
|---|---|
| Exactly 18 PRG objects | `PASS` |
| City set matches the frozen manifest | `PASS` |
| 18 unique TERYT codes | `PASS` |
| No empty geometries | `PASS` |
| Exact manifest start coordinate is inside Warszawa | `PASS` |
| Computational CRS is EPSG:2180 | `PASS` |
| GeoJSON is published in EPSG:4326 | `PASS` |
| All EPSG:2180 areas are positive | `PASS` |
| Canonical order and bit indexes follow the frozen manifest | `PASS` |
| Materialized raw GML matches the pinned source manifest | `PASS` |
| Every mapped derived product uses the selected raw GML | `PASS` |
| The two saved responses contain identical canonical PRG objects | `PASS` |

## Canonical start coordinate

| Point | EPSG:4326 longitude | EPSG:4326 latitude | Inside Warszawa |
|---|---:|---:|---|
| Plac Wilsona | 20.986450 | 52.268850 | `PASS` |

## Raw input provenance

| Role | WFS timestamp | Bytes | Raw SHA-256 | Compressed SHA-256 |
|---|---|---:|---|---|
| Selected pinned input | `2026-07-26T16:04:25` | 1695301 | `adf261aae31257dd24843b6f6c0e694efb40041aae3913b1c27aa4801866974c` | `91bdab09b9fe77ea02add8cfdf88a41c03ae84587ff8fdfc1808fb340fa41684` |
| Old saved report response | `2026-07-26T16:11:05` | 1695301 | `0e3f113673620334867151f8845853680d31ff4b511fdbf2124eb2d1d64f4838` | `07a1f4912264e1a1fae7686e334cfc3e1aae41c90119c1b46646d9563b6cd394` |

The old and selected raw GML files are both retained. Their only byte-level difference is `wfs:FeatureCollection/@timeStamp`: `2026-07-26T16:11:05` in the old response and `2026-07-26T16:04:25` in the selected response. After replacing only that attribute with a fixed marker, both responses have SHA-256 `057fcd8be1965d32dcdd1f8ead62bee926086da2d090e5d130f0a33f30c5a374`.

The earlier report mode `reused_saved_responses` meant only that no new response was downloaded; it did not identify the reused file. The committed raw GML, embedded vector and inventory source hashes, and 18/18 raw-to-original geometry comparison identify the selected response unambiguously.

The 18 canonical objects were joined by `JPT_KOD_JE` and compared using `JPT_NAZWA_`, `JPT_ID`, and ISO WKB. Result: `18/18 identical`. No geometry or PRG identity difference was found.

The selected input is permanently pinned at `input/prg/prg-18-raw.gml.zst`. The old response is retained at `input/prg/history/prg-18-raw-0e3f113673620334867151f8845853680d31ff4b511fdbf2124eb2d1d64f4838.gml.zst`. Neither depends on a temporary Actions artifact.

## Derived artifact lineage

| Derived file | Exact raw GML SHA-256 | Artifact SHA-256 |
|---|---|---|
| `results/prg/prg-18-original.gpkg` | `adf261aae31257dd24843b6f6c0e694efb40041aae3913b1c27aa4801866974c` | `de6d9431bfbb5da36bf2cfe61108f5c62d694a4d48d9b12d9322d9d3d4cb1263` |
| `results/prg/prg-18-computational.gpkg` | `adf261aae31257dd24843b6f6c0e694efb40041aae3913b1c27aa4801866974c` | `ae8c1a6ac592434e48f0d1ae1db261da5c89ac82b482605bd9387eee36134a4c` |
| `results/prg/prg-18.geojson` | `adf261aae31257dd24843b6f6c0e694efb40041aae3913b1c27aa4801866974c` | `67f1b0ba044faa40b0bc22f2f4b08d61d1ad1982aca2e6615e6ab12a9334ed6d` |
| `results/prg/prg-city-inventory.csv` | `adf261aae31257dd24843b6f6c0e694efb40041aae3913b1c27aa4801866974c` | `20a249ce9b92faaf4f3db5b261d505965f25cfc4bf51e4a497a45f3af76b0af5` |
| `results/prg/prg-city-inventory.json` | `adf261aae31257dd24843b6f6c0e694efb40041aae3913b1c27aa4801866974c` | `3a62fd84d8019bcb668d45e9a28aed708a51ca8d4fe318b89361b774aff059cb` |

All five products above were generated from selected raw GML `adf261aae31257dd24843b6f6c0e694efb40041aae3913b1c27aa4801866974c`. Their machine-readable mapping is `results/prg/derived-artifacts.json`.

## Canonical city and bitmask order

| Bit index | City | TERYT | PRG object ID | Unit | Geometry | Multipart | GEOS valid | Area EPSG:2180 (m²) |
|---:|---|---|---|---|---|---|---|---:|
| 0 | Warszawa | `1465011` | `2799189` | `GMI/1` | `POLYGON` | `false` | `true` | 516723744.607 |
| 1 | Łódź | `1061011` | `10027506` | `GMI/1` | `POLYGON` | `false` | `true` | 292850136.805 |
| 2 | Kielce | `2661011` | `2747998` | `GMI/1` | `POLYGON` | `false` | `true` | 109529310.698 |
| 3 | Lublin | `0663011` | `10002086` | `GMI/1` | `POLYGON` | `false` | `true` | 147479996.570 |
| 4 | Rzeszów | `1863011` | `10036131` | `GMI/1` | `POLYGON` | `false` | `true` | 128970131.849 |
| 5 | Kraków | `1261011` | `10016301` | `GMI/1` | `POLYGON` | `false` | `true` | 326429348.930 |
| 6 | Katowice | `2469011` | `10036150` | `GMI/1` | `POLYGON` | `false` | `true` | 164498278.377 |
| 7 | Opole | `1661011` | `10040196` | `GMI/1` | `POLYGON` | `false` | `true` | 148836184.758 |
| 8 | Wrocław | `0264011` | `10027472` | `GMI/1` | `POLYGON` | `false` | `true` | 292523688.828 |
| 9 | Zielona Góra | `0862011` | `10019236` | `GMI/1` | `POLYGON` | `false` | `true` | 275348752.910 |
| 10 | Gorzów Wielkopolski | `0861011` | `10019134` | `GMI/1` | `POLYGON` | `false` | `true` | 85742807.352 |
| 11 | Szczecin | `3262011` | `10004395` | `GMI/1` | `POLYGON` | `false` | `true` | 300732828.134 |
| 12 | Poznań | `3064011` | `10027538` | `GMI/1` | `POLYGON` | `false` | `true` | 261677978.505 |
| 13 | Bydgoszcz | `0461011` | `10027391` | `GMI/1` | `POLYGON` | `false` | `true` | 175832535.376 |
| 14 | Toruń | `0463011` | `2788878` | `GMI/1` | `POLYGON` | `false` | `true` | 115550744.991 |
| 15 | Gdańsk | `2261011` | `10021887` | `GMI/1` | `POLYGON` | `false` | `true` | 682005967.410 |
| 16 | Olsztyn | `2862011` | `2772685` | `GMI/1` | `POLYGON` | `false` | `true` | 88219832.128 |
| 17 | Białystok | `2061011` | `2753749` | `GMI/1` | `POLYGON` | `false` | `true` | 102178558.849 |

Original geometries are retained without MakeValid. If an original geometry is invalid, only the separate computational copy is repaired.

No route optimizer or solver was started in this stage.

`PRG_PROVENANCE_LOCKED`

`SOLVER_NOT_STARTED`
