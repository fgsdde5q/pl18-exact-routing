# Stage 2A turn-state version diff

## Decision

- Authoritative count: `15654641`.
- Authoritative SHA-256: `cb613a7ad6019181064b3f09ef36ccabd8a732ad50897f04da49468306e23bad`.
- Previous history: `15654639`, `6a6a0c8e63380242569548bff93b74931fb5f2d54ca1320c33afceadb5364061`.
- Canonical schema and sorting are unchanged (`turn-state-canonical-v1`).
- Initial/start states remain separate and are not ordinary turn transitions.

## Exact cause

The old graph used an unpinned parallel OSRM build. The authoritative graph pins all OSRM graph phases to `JOBS=1`. Directed base edges are identical, but the single-thread edge-based graph contains two additional allowed transitions. This is a graph-product difference, not an exporter, schema, sorting, start-state, restriction-certificate, or 25-candidate logic change.

## Set comparison

- Added: `4`.
- Removed: `2`.
- Changed: `2`.

### Added

- `1113411440/0/0/2958930023/10186470794` → `1113411441/0/0/10186470794/7891279887`; duration `0.0` s; category `ordinary_turn`; restriction relation `not applicable`; first graph-build change `5049c28`; first certificate change `69d3746`.
- `1113411441/0/0/10186470794/7891279887` → `1113411441/1/0/7891279887/2958930025`; duration `0.0` s; category `ordinary_turn`; restriction relation `not applicable`; first graph-build change `5049c28`; first certificate change `69d3746`.
- `1113411441/0/1/7891279887/10186470794` → `1113411440/0/1/10186470794/2958930023`; duration `0.0` s; category `ordinary_turn`; restriction relation `not applicable`; first graph-build change `5049c28`; first certificate change `69d3746`.
- `1113411441/1/1/2958930025/7891279887` → `1113411441/0/1/7891279887/10186470794`; duration `0.0` s; category `ordinary_turn`; restriction relation `not applicable`; first graph-build change `5049c28`; first certificate change `69d3746`.

### Removed

- `179044511/2/0/14010797401/1893818934` → `179044511/3/0/1893818934/14010797405`; duration `0.0` s; category `ordinary_turn`; restriction relation `not applicable`; first graph-build change `5049c28`; first certificate change `69d3746`.
- `179044511/3/1/14010797405/1893818934` → `179044511/2/1/1893818934/14010797401`; duration `0.0` s; category `ordinary_turn`; restriction relation `not applicable`; first graph-build change `5049c28`; first certificate change `69d3746`.

### Changed

[{"incoming_stable_edge_id": "179044511/3/0/1893818934/14010797405", "outgoing_stable_edge_id": "991464888/1/0/14010797405/14010797400", "old": {"incoming_stable_edge_id": "179044511/3/0/1893818934/14010797405", "outgoing_stable_edge_id": "991464888/1/0/14010797405/14010797400", "turn_duration_s": 5.1, "restriction_status": "allowed", "restriction_relation_ids": [], "transition_category": "ordinary_turn"}, "new": {"incoming_stable_edge_id": "179044511/3/0/1893818934/14010797405", "outgoing_stable_edge_id": "991464888/1/0/14010797405/14010797400", "turn_duration_s": 3.7, "restriction_status": "allowed", "restriction_relation_ids": [], "transition_category": "ordinary_turn"}, "restriction_relation_ids": [], "transition_category": "ordinary_turn", "reason": "Record payload changed after pinning the OSRM build to one thread.", "first_changed_graph_build_commit": "5049c28cc511550be5cbd20c7f49870b989a13d8", "first_changed_certificate_commit": "69d3746"}, {"incoming_stable_edge_id": "991464888/1/1/14010797400/14010797405", "outgoing_stable_edge_id": "179044511/3/1/14010797405/1893818934", "old": {"incoming_stable_edge_id": "991464888/1/1/14010797400/14010797405", "outgoing_stable_edge_id": "179044511/3/1/14010797405/1893818934", "turn_duration_s": 1.9, "restriction_status": "allowed", "restriction_relation_ids": [], "transition_category": "ordinary_turn"}, "new": {"incoming_stable_edge_id": "991464888/1/1/14010797400/14010797405", "outgoing_stable_edge_id": "179044511/3/1/14010797405/1893818934", "turn_duration_s": 1.1, "restriction_status": "allowed", "restriction_relation_ids": [], "transition_category": "ordinary_turn"}, "restriction_relation_ids": [], "transition_category": "ordinary_turn", "reason": "Record payload changed after pinning the OSRM build to one thread.", "first_changed_graph_build_commit": "5049c28cc511550be5cbd20c7f49870b989a13d8", "first_changed_certificate_commit": "69d3746"}]
