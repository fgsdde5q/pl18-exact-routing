# PRG Stage 1 validation report

- Stage status: `PRG_STAGE_OK`
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
- Raw GML SHA-256: `adf261aae31257dd24843b6f6c0e694efb40041aae3913b1c27aa4801866974c`
- Official response mode: `reused_saved_responses`
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

## Canonical start coordinate

| Point | EPSG:4326 longitude | EPSG:4326 latitude | Inside Warszawa |
|---|---:|---:|---|
| Plac Wilsona | 20.986450 | 52.268850 | `PASS` |

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
