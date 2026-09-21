# Stage 2A road graph validation report

## Status

- `ROAD_GRAPH_STAGE_OK`
- `CACHE_BUDGET_OK`
- `TURN_RESTRICTIONS_CERTIFIED`
- `START_DIRECTIONS_CERTIFIED`
- `PRG_PROVENANCE_LOCKED`
- `SOLVER_NOT_STARTED`

## Frozen inputs

- Manifest v3 SHA-256: `ee6964ee4c95f3f574a6e1234cb63e91999e64d53a5e5f7c7066114d4c067b9c`
- OSM size: `2091448485` bytes
- OSM MD5: `0db66b478a3ed7c6f52d182e13c6fa27`
- OSM SHA-256: `f28f493c6cc280da1128b03be21ae2eb1973f443c14235dea36088bcbd3e83f3`
- PRG raw GML SHA-256: `adf261aae31257dd24843b6f6c0e694efb40041aae3913b1c27aa4801866974c`
- PRG compressed GML SHA-256: `91bdab09b9fe77ea02add8cfdf88a41c03ae84587ff8fdfc1808fb340fa41684`
- Profile SHA-256: `0b2dc359c4618b215efb512923a02471a2df208024e5d4d9f29e4a86a67f1001`
- OSRM source commit: `3c32a51bf58d12bf30efd0808d0b6ad51d334122`

## Graph counts

- OSM ways read: `33478092`
- Legal directed motorcar edges: `33615581`
- Edge-based turn states: `15728284`
- Enforced turn restrictions: `42849`
- Restriction authority: OSRM's accepted restriction graph after invalid-restriction removal.
- Forbidden ferry edges: `6670`
- Rejected private/non-motorcar edges: `2435460`

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
- Manifest v3 class speeds, maxspeed reduction, surface, tracktype, smoothness and bridge caps.
- Private/non-motorcar roads, ferries and shuttle trains are forbidden.
- Conditional time/day/date/season restrictions and all dynamic inputs are ignored.
- Toll roads have no extra cost; gate/lift-gate penalties are 60 seconds.
- Traffic-signal, stop-sign, ramp, interchange and roundabout extras are zero.

## Audit status

- `EXPORT_REPRODUCIBILITY`: `PASS`
- `GRAPH_REBUILD_REPRODUCIBILITY`: `NOT_RUN`
- `CACHE_BUDGET_STATUS`: `PASS` (`932689698` <= `950000000` bytes)
- `TURN_RESTRICTION_CERTIFICATE_STATUS`: `PASS`
- `START_DIRECTION_CERTIFICATE_STATUS`: `PASS`

## Export reproducibility

- Directed base-edge canonical SHA-256: `4cac4088cdc7f7c65966840816acf029a2143ec70639837f25c86f9850671cd6`
- Edge-based turn-state canonical SHA-256: `e41c43e6d9f703b523b9cae39a4f2e8187b0c70bf84c1c77a67b6adc28238dce`
- Two independent canonical metadata exporter runs: `PASS`
- One frozen OSRM graph build was used for both clean metadata exports.
- Graph rebuild reproducibility is not asserted before the dispatch-only clean rebuild workflow succeeds.
- Stable edge IDs unique, values non-negative, endpoints connected: `PASS`
- Relation-derived expected prohibited transitions absent: `45629` checked, `0` observed.
- Source-derived candidates not enforced by pinned OSRM: `33` transitions, retained with relation-ID provenance and not claimed prohibited.
- Ferry/private rejection and 150 m snap bound: `PASS`
