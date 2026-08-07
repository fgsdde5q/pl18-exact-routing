# Stage 2A road graph validation report

## Status

- `ROAD_GRAPH_STAGE_OK`
- `GRAPH_REBUILD_REPRODUCIBLE`
- `CACHE_BUDGET_OK`
- `TURN_RESTRICTIONS_CERTIFIED`
- `START_DIRECTIONS_CERTIFIED`
- `PRG_PROVENANCE_LOCKED`
- `SOLVER_NOT_STARTED`

## Frozen inputs

- Manifest v2 SHA-256: `f47d9a805defdb7e28005048d7ad9a7a76f666e75d508422a7edd239ce60f7ce`
- OSM size: `2078786520` bytes
- OSM MD5: `eb188df5acafd002244ed84bb7b650ab`
- OSM SHA-256: `2f49ae5a61fbd70de5a8696ffa1cd1ac177bcfc9fea1d69cad43fbf4e5af4f28`
- PRG raw GML SHA-256: `adf261aae31257dd24843b6f6c0e694efb40041aae3913b1c27aa4801866974c`
- PRG compressed GML SHA-256: `91bdab09b9fe77ea02add8cfdf88a41c03ae84587ff8fdfc1808fb340fa41684`
- Profile SHA-256: `0caa3c72a43df86fc5f5df8fa039b1dce9e9584aed120857a4df8624c23f2839`
- OSRM source commit: `3c32a51bf58d12bf30efd0808d0b6ad51d334122`

## Graph counts

- OSM ways read: `33321336`
- Legal directed motorcar edges: `33473569`
- Edge-based turn states: `15654641`
- Enforced turn restrictions: `42627`
- Restriction authority: OSRM's accepted restriction graph after invalid-restriction removal.
- Forbidden ferry edges: `6612`
- Rejected private/non-motorcar edges: `2401005`

## Start snap

- Input: `20.98645, 52.26885`
- Snapped coordinate: `20.98634735, 52.268784436`
- EPSG:2180 coordinate: `635498.142, 491053.819`
- Distance: `10.111` m
- Selected OSM way: `25857664`
- Selected stable edge: `25857664/4/0/3069840595/611255856`
- Available legal initial directions: `25857664/4/0/3069840595/611255856`
- Single-direction explanation: `START_DIRECTIONS_CERTIFIED`
- Exact start inside Warszawa frozen PRG polygon: `PASS`

## inherited_from_OSRM_v26.5.0

- Directed oneway handling and physical vehicle limits.
- Unconditional static turn restriction expansion.
- Edge-based graph construction and sigmoid angle penalty implementation.

## explicit_project_overrides

- Duration is the routing weight.
- Manifest v2 class speeds, maxspeed reduction, surface, tracktype, smoothness and bridge caps.
- Private/non-motorcar roads, ferries and shuttle trains are forbidden.
- Conditional time/day/date/season restrictions and all dynamic inputs are ignored.
- Toll roads have no extra cost; gate/lift-gate penalties are 60 seconds.
- Traffic-signal, stop-sign, ramp, interchange and roundabout extras are zero.

## Audit status

- `EXPORT_REPRODUCIBILITY`: `PASS`
- `GRAPH_REBUILD_REPRODUCIBILITY`: `PASS`
- `CACHE_BUDGET_STATUS`: `PASS` (`932689698` <= `950000000` bytes)
- `TURN_RESTRICTION_CERTIFICATE_STATUS`: `PASS`
- `START_DIRECTION_CERTIFICATE_STATUS`: `PASS`

## Export reproducibility

- Directed base-edge canonical SHA-256: `f27449dfa026193831458712bcb97c1c8b07fdf9fe4f9d33c6c2f2f613a4406a`
- Edge-based turn-state canonical SHA-256: `cb613a7ad6019181064b3f09ef36ccabd8a732ad50897f04da49468306e23bad`
- Two independent canonical metadata exporter runs: `PASS`
- One frozen OSRM graph build was used for both clean metadata exports.
- Two independent clean OSRM graph rebuilds from frozen inputs produced matching canonical exports and certificates.
- Stable edge IDs unique, values non-negative, endpoints connected: `PASS`
- Relation-derived expected prohibited transitions absent: `45363` checked, `0` observed.
- Source-derived candidates not enforced by pinned OSRM: `25` transitions, retained with relation-ID provenance and not claimed prohibited.
- Ferry/private rejection and 150 m snap bound: `PASS`
