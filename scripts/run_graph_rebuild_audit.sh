#!/usr/bin/env bash

set -Eeuo pipefail

if (( $# != 5 )); then
  echo "usage: $0 PBF OSRM_SOURCE OSRM_BUILD WORK_ROOT RESULTS_ROOT" >&2
  exit 2
fi

readonly PBF_PATH="$1"
readonly OSRM_SOURCE="$2"
readonly OSRM_BUILD="$3"
readonly WORK_ROOT="$4"
readonly RESULTS_ROOT="$5"
REPOSITORY_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly REPOSITORY_ROOT
readonly MANIFEST="${REPOSITORY_ROOT}/instance/pl_18_capitals_static_instance_v3.yaml"
readonly PROFILE="${REPOSITORY_ROOT}/profiles/pl18-car.lua"
readonly EXPECTED_MANIFEST_SHA256="ee6964ee4c95f3f574a6e1234cb63e91999e64d53a5e5f7c7066114d4c067b9c"
readonly EXPECTED_PBF_SHA256="f28f493c6cc280da1128b03be21ae2eb1973f443c14235dea36088bcbd3e83f3"
readonly EXPECTED_PROFILE_SHA256="0b2dc359c4618b215efb512923a02471a2df208024e5d4d9f29e4a86a67f1001"
readonly EXPECTED_OSRM_COMMIT="3c32a51bf58d12bf30efd0808d0b6ad51d334122"
readonly JOBS="${JOBS:-1}"
readonly LUA_PATH_VALUE="${OSRM_SOURCE}/profiles/?.lua;${OSRM_SOURCE}/profiles/?/init.lua;;"

test "$(sha256sum "$MANIFEST" | awk '{print $1}')" = "$EXPECTED_MANIFEST_SHA256"
test "$(sha256sum "$PBF_PATH" | awk '{print $1}')" = "$EXPECTED_PBF_SHA256"
test "$(sha256sum "$PROFILE" | awk '{print $1}')" = "$EXPECTED_PROFILE_SHA256"
test "$(git -C "$OSRM_SOURCE" rev-parse HEAD)" = "$EXPECTED_OSRM_COMMIT"
for executable in osrm-extract osrm-partition osrm-customize osrm-graph-dump; do
  test -x "${OSRM_BUILD}/${executable}"
done

rm -rf -- "$WORK_ROOT"
mkdir -p -- "$WORK_ROOT" "$RESULTS_ROOT"

build_graph() {
  local build_id="$1"
  local root="${WORK_ROOT}/${build_id}"
  local graph="${root}/graph"
  local dump="${root}/dump"
  local metadata="${root}/metadata"
  local temporary="${root}/sort-temp"
  test ! -e "$root"
  mkdir -p -- "$graph" "$dump" "$metadata" "$temporary"

  env "LUA_PATH=${LUA_PATH_VALUE}" \
    "${OSRM_BUILD}/osrm-extract" \
      --threads "$JOBS" \
      --profile "$PROFILE" \
      --output "${graph}/poland.osrm" \
      "$PBF_PATH" 2>&1 | tee "${root}/extract.log"
  "${OSRM_BUILD}/osrm-partition" --threads "$JOBS" "${graph}/poland.osrm"
  "${OSRM_BUILD}/osrm-customize" --threads "$JOBS" "${graph}/poland.osrm"
  "${OSRM_BUILD}/osrm-graph-dump" "${graph}/poland.osrm" "$dump"
  python3 "${REPOSITORY_ROOT}/scripts/export_graph_metadata.py" \
    --manifest "$MANIFEST" \
    --pbf "$PBF_PATH" \
    --osrm-dump "$dump" \
    --osrm-extract-log "${root}/extract.log" \
    --output "$metadata" \
    --temp "$temporary"
  python3 "${REPOSITORY_ROOT}/scripts/validate_graph_metadata.py" \
    --metadata "$metadata"

  python3 - "$build_id" "$graph" "${root}/provenance.json" <<'PY'
import json
from pathlib import Path
import sys

Path(sys.argv[3]).write_text(
    json.dumps(
        {
            "build_id": sys.argv[1],
            "clean_graph_root": str(Path(sys.argv[2]).resolve()),
            "ready_graph_cache_restored": False,
            "frozen_pbf_sha256": "f28f493c6cc280da1128b03be21ae2eb1973f443c14235dea36088bcbd3e83f3",
            "manifest_v3_sha256": "ee6964ee4c95f3f574a6e1234cb63e91999e64d53a5e5f7c7066114d4c067b9c",
            "profile_sha256": "0b2dc359c4618b215efb512923a02471a2df208024e5d4d9f29e4a86a67f1001",
            "osrm_commit": "3c32a51bf58d12bf30efd0808d0b6ad51d334122",
        },
        indent=2,
    )
    + "\n",
    encoding="utf-8",
)
PY
}

build_graph first-clean-build
build_graph second-clean-build

python3 "${REPOSITORY_ROOT}/scripts/compare_graph_rebuilds.py" \
  --first-metadata "${WORK_ROOT}/first-clean-build/metadata" \
  --second-metadata "${WORK_ROOT}/second-clean-build/metadata" \
  --first-provenance "${WORK_ROOT}/first-clean-build/provenance.json" \
  --second-provenance "${WORK_ROOT}/second-clean-build/provenance.json" \
  --output "${RESULTS_ROOT}/graph-rebuild-reproducibility.json"

cp -- \
  "${WORK_ROOT}/first-clean-build/metadata/non-enforced-restriction-candidates.json" \
  "${RESULTS_ROOT}/non-enforced-restriction-candidates.json"

python3 "${REPOSITORY_ROOT}/scripts/apply_graph_rebuild_certificate.py" \
  --certificate "${RESULTS_ROOT}/graph-rebuild-reproducibility.json" \
  --results "$RESULTS_ROOT"

echo "ROAD_GRAPH_STAGE_OK"
echo "GRAPH_REBUILD_REPRODUCIBLE"
echo "CACHE_BUDGET_OK"
echo "TURN_RESTRICTIONS_CERTIFIED"
echo "START_DIRECTIONS_CERTIFIED"
echo "PRG_PROVENANCE_LOCKED"
echo "SOLVER_NOT_STARTED"
