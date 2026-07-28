#!/usr/bin/env bash

set -Eeuo pipefail

if (( $# != 6 )); then
  echo "usage: $0 PBF OSRM_SOURCE OSRM_BUILD WORK_ROOT CACHE_ROOT RESULTS_ROOT" >&2
  exit 2
fi

readonly PBF_PATH="$1"
readonly OSRM_SOURCE="$2"
readonly OSRM_BUILD="$3"
readonly WORK_ROOT="$4"
readonly CACHE_ROOT="$5"
readonly RESULTS_ROOT="$6"
readonly CHECKPOINT_ROOT="${OSRM_CHECKPOINT_ROOT:?OSRM_CHECKPOINT_ROOT is required}"
readonly METADATA_ROOT="${METADATA_CHECKPOINT_ROOT:?METADATA_CHECKPOINT_ROOT is required}"
REPOSITORY_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly REPOSITORY_ROOT
readonly MANIFEST="${REPOSITORY_ROOT}/instance/pl_18_capitals_static_instance_v2.yaml"
readonly PROFILE="${REPOSITORY_ROOT}/profiles/pl18-car.lua"
readonly EXPECTED_MANIFEST_SHA256="f47d9a805defdb7e28005048d7ad9a7a76f666e75d508422a7edd239ce60f7ce"
readonly EXPECTED_PBF_SIZE="2078786520"
readonly EXPECTED_PBF_MD5="eb188df5acafd002244ed84bb7b650ab"
readonly EXPECTED_PBF_SHA256="2f49ae5a61fbd70de5a8696ffa1cd1ac177bcfc9fea1d69cad43fbf4e5af4f28"
readonly EXPECTED_OSRM_COMMIT="3c32a51bf58d12bf30efd0808d0b6ad51d334122"
readonly JOBS="${JOBS:-$(nproc)}"
readonly OSRM_PROFILE_LUA_PATH="${OSRM_SOURCE}/profiles/?.lua;${OSRM_SOURCE}/profiles/?/init.lua;;"

mkdir -p -- "$WORK_ROOT" "$CACHE_ROOT" "$RESULTS_ROOT"

verify_equal() {
  local label="$1"
  local expected="$2"
  local actual="$3"
  if [[ "$actual" != "$expected" ]]; then
    printf '%s mismatch: expected %s, got %s\n' "$label" "$expected" "$actual" >&2
    exit 1
  fi
}

verify_equal "manifest SHA-256" "$EXPECTED_MANIFEST_SHA256" "$(sha256sum "$MANIFEST" | awk '{print $1}')"
verify_equal "PBF size" "$EXPECTED_PBF_SIZE" "$(stat --format='%s' "$PBF_PATH")"
verify_equal "PBF MD5" "$EXPECTED_PBF_MD5" "$(md5sum "$PBF_PATH" | awk '{print $1}')"
verify_equal "PBF SHA-256" "$EXPECTED_PBF_SHA256" "$(sha256sum "$PBF_PATH" | awk '{print $1}')"
verify_equal "OSRM source commit" "$EXPECTED_OSRM_COMMIT" "$(git -C "$OSRM_SOURCE" rev-parse HEAD)"

python3 "${REPOSITORY_ROOT}/scripts/test_stage2a_manifest_contract.py"
python3 "${REPOSITORY_ROOT}/scripts/test_stage2a_cache_contract.py"
python3 "${REPOSITORY_ROOT}/scripts/test_canonical_build_comparison.py"
python3 "${REPOSITORY_ROOT}/scripts/test_graph_spatial_contract.py"
python3 "${REPOSITORY_ROOT}/scripts/generate_pl18_profile.py" \
  --manifest "$MANIFEST" \
  --upstream-car "${OSRM_SOURCE}/profiles/car.lua" \
  --output "${WORK_ROOT}/generated-pl18-car.lua"
cmp -- "$PROFILE" "${WORK_ROOT}/generated-pl18-car.lua"
UPSTREAM_CAR_PROFILE="${OSRM_SOURCE}/profiles/car.lua" \
  python3 "${REPOSITORY_ROOT}/scripts/test_pl18_profile.py"
python3 "${REPOSITORY_ROOT}/scripts/test_graph_metadata_export.py"
test -f "${OSRM_SOURCE}/profiles/lib/set.lua"

build_checkpoint() {
  for executable in osrm-extract osrm-partition osrm-customize osrm-graph-dump; do
    test -x "${OSRM_BUILD}/${executable}"
  done

  rm -rf -- "$CHECKPOINT_ROOT"
  mkdir -p -- "${CHECKPOINT_ROOT}/graph" "${CHECKPOINT_ROOT}/dump"

  /usr/bin/time -v -o "${CHECKPOINT_ROOT}/extract.time" \
    env "LUA_PATH=${OSRM_PROFILE_LUA_PATH}" \
    "${OSRM_BUILD}/osrm-extract" \
    --threads "$JOBS" \
    --profile "$PROFILE" \
    --output "${CHECKPOINT_ROOT}/graph/poland.osrm" \
    "$PBF_PATH" 2>&1 | tee "${CHECKPOINT_ROOT}/extract.log"
  /usr/bin/time -v -o "${CHECKPOINT_ROOT}/partition.time" \
    "${OSRM_BUILD}/osrm-partition" \
    --threads "$JOBS" \
    "${CHECKPOINT_ROOT}/graph/poland.osrm"
  /usr/bin/time -v -o "${CHECKPOINT_ROOT}/customize.time" \
    "${OSRM_BUILD}/osrm-customize" \
    --threads "$JOBS" \
    "${CHECKPOINT_ROOT}/graph/poland.osrm"
  /usr/bin/time -v -o "${CHECKPOINT_ROOT}/dump.time" \
    "${OSRM_BUILD}/osrm-graph-dump" \
    "${CHECKPOINT_ROOT}/graph/poland.osrm" \
    "${CHECKPOINT_ROOT}/dump"

  (
    cd "${CHECKPOINT_ROOT}/graph"
    find . -maxdepth 1 -type f -name 'poland.osrm*' -print0 |
      sort -z |
      xargs -0 sha256sum |
      sed 's#  \./#  #' > "${CHECKPOINT_ROOT}/graph-binary-hashes.txt"
  )

  {
    printf 'osrm_version=v26.5.0\n'
    printf 'osrm_commit=%s\n' "$EXPECTED_OSRM_COMMIT"
    printf 'compiler=%s\n' "$("${CXX:-c++}" --version | sed -n '1p')"
    printf 'cmake=%s\n' "$(cmake --version | sed -n '1p')"
    printf 'python=%s\n' "$(python3 --version 2>&1)"
    printf 'pyosmium=%s\n' "$(python3 -c 'import importlib.metadata; print(importlib.metadata.version("osmium"))')"
    printf 'pyproj=%s\n' "$(python3 -c 'import importlib.metadata; print(importlib.metadata.version("pyproj"))')"
    printf 'boost=%s\n' "$("${VCPKG_ROOT}/vcpkg" list --x-install-root="${OSRM_BUILD}/vcpkg_installed" | awk '$1 ~ /^boost:/ {print $2; exit}')"
    printf 'lua=%s\n' "$("${VCPKG_ROOT}/vcpkg" list --x-install-root="${OSRM_BUILD}/vcpkg_installed" | awk '$1 ~ /^lua:/ {print $2; exit}')"
    printf 'vcpkg_commit=%s\n' "$(git -C "$VCPKG_ROOT" rev-parse HEAD)"
    printf 'os=%s\n' "$(awk -F= '
      $1 == "NAME" {gsub(/^"|"$/, "", $2); name = $2}
      $1 == "VERSION_ID" {gsub(/^"|"$/, "", $2); version = $2}
      END {print name " " version}
    ' /etc/os-release)"
    printf 'architecture=%s\n' "$(uname -m)"
    printf 'kernel=%s\n' "$(uname -srvmo)"
    printf 'docker_image_digest=not_used\n'
    printf 'graph_exporter_sha256=%s\n' "$(sha256sum "${REPOSITORY_ROOT}/tools/osrm_graph_dump.cpp" | awk '{print $1}')"
    printf 'graph_exporter_commit=%s\n' "${GITHUB_SHA:-local}"
    printf 'profile_sha256=%s\n' "$(sha256sum "$PROFILE" | awk '{print $1}')"
  } > "${CHECKPOINT_ROOT}/toolchain.txt"

  {
    for phase in extract partition customize dump; do
      printf '## osrm %s\n' "$phase"
      cat "${CHECKPOINT_ROOT}/${phase}.time"
    done
    printf '## OSRM checkpoint disk usage\n'
    du -sh "${CHECKPOINT_ROOT}/graph" "${CHECKPOINT_ROOT}/dump"
  } > "${CHECKPOINT_ROOT}/resource-usage.txt"

  touch "${CHECKPOINT_ROOT}/.complete"
}

verify_checkpoint() {
  for path in \
    "${CHECKPOINT_ROOT}/.complete" \
    "${CHECKPOINT_ROOT}/extract.log" \
    "${CHECKPOINT_ROOT}/graph-binary-hashes.txt" \
    "${CHECKPOINT_ROOT}/resource-usage.txt" \
    "${CHECKPOINT_ROOT}/toolchain.txt" \
    "${CHECKPOINT_ROOT}/dump/osrm-directed-segments.tsv" \
    "${CHECKPOINT_ROOT}/dump/osrm-turn-states.tsv"; do
    test -f "$path"
  done
  (
    cd "${CHECKPOINT_ROOT}/graph"
    sha256sum --check "${CHECKPOINT_ROOT}/graph-binary-hashes.txt"
  )
  verify_equal \
    "checkpoint profile SHA-256" \
    "$(sha256sum "$PROFILE" | awk '{print $1}')" \
    "$(awk -F= '$1 == "profile_sha256" {print $2}' "${CHECKPOINT_ROOT}/toolchain.txt")"
}

if [[ ! -f "${CHECKPOINT_ROOT}/.complete" ]]; then
  build_checkpoint
fi
verify_checkpoint

write_canonical_hashes() {
  local metadata_root="$1"
  local output="$2"
  python3 - "$metadata_root" "$output" <<'PY'
import hashlib
import json
from pathlib import Path
import sys

root = Path(sys.argv[1])
def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()
result = {
    "directed_base_edges_sha256": digest(root / "directed-base-edges.tsv"),
    "edge_based_turn_states_sha256": digest(root / "edge-based-turn-states.tsv"),
    "edge_counts_sha256": digest(root / "export-summary.json"),
    "start_snap_sha256": digest(root / "start-snap.json"),
}
Path(sys.argv[2]).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
PY
}

build_metadata() {
  local build_name="$1"
  local build_root="${WORK_ROOT}/${build_name}"
  local metadata_root="${build_root}/metadata"
  local temp_root="${build_root}/sort-temp"
  rm -rf -- "$build_root"
  mkdir -p -- "$metadata_root" "$temp_root"

  /usr/bin/time -v -o "${build_root}/metadata.time" \
    python3 "${REPOSITORY_ROOT}/scripts/export_graph_metadata.py" \
      --manifest "$MANIFEST" \
      --pbf "$PBF_PATH" \
      --osrm-dump "${CHECKPOINT_ROOT}/dump" \
      --osrm-extract-log "${CHECKPOINT_ROOT}/extract.log" \
      --output "$metadata_root" \
      --temp "$temp_root"
  write_canonical_hashes "$metadata_root" "${build_root}/canonical-hashes.json"
}

build_metadata_checkpoint() {
  build_metadata first
  python3 "${REPOSITORY_ROOT}/scripts/validate_graph_metadata.py" \
    --metadata "${WORK_ROOT}/first/metadata"
  rm -rf -- "${WORK_ROOT}/first/metadata"
  build_metadata second

  python3 "${REPOSITORY_ROOT}/scripts/compare_canonical_builds.py" \
    --first "${WORK_ROOT}/first/canonical-hashes.json" \
    --second "${WORK_ROOT}/second/canonical-hashes.json" \
    --diagnostics "${WORK_ROOT}/determinism-diff.json"

  rm -rf -- "$METADATA_ROOT"
  mkdir -p -- "$METADATA_ROOT"
  cp -- \
    "${WORK_ROOT}/first/canonical-hashes.json" \
    "${METADATA_ROOT}/first-canonical-hashes.json"
  cp -- \
    "${WORK_ROOT}/second/canonical-hashes.json" \
    "${METADATA_ROOT}/second-canonical-hashes.json"
  cp -- \
    "${CHECKPOINT_ROOT}/graph-binary-hashes.txt" \
    "${METADATA_ROOT}/graph-binary-hashes.txt"
  cp -- \
    "${WORK_ROOT}/second/metadata/export-summary.json" \
    "${WORK_ROOT}/second/metadata/start-snap.json" \
    "$METADATA_ROOT/"
  zstd -6 -T0 \
    "${WORK_ROOT}/second/metadata/directed-base-edges.tsv" \
    -o "${METADATA_ROOT}/directed-base-edges.tsv.zst"
  zstd -6 -T0 \
    "${WORK_ROOT}/second/metadata/edge-based-turn-states.tsv" \
    -o "${METADATA_ROOT}/edge-based-turn-states.tsv.zst"
  {
    printf '## first metadata\n'
    cat "${WORK_ROOT}/first/metadata.time"
    printf '## second metadata\n'
    cat "${WORK_ROOT}/second/metadata.time"
  } > "${METADATA_ROOT}/resource-usage.txt"
  touch "${METADATA_ROOT}/.complete"
}

verify_metadata_checkpoint() {
  for path in \
    "${METADATA_ROOT}/.complete" \
    "${METADATA_ROOT}/directed-base-edges.tsv.zst" \
    "${METADATA_ROOT}/edge-based-turn-states.tsv.zst" \
    "${METADATA_ROOT}/export-summary.json" \
    "${METADATA_ROOT}/first-canonical-hashes.json" \
    "${METADATA_ROOT}/graph-binary-hashes.txt" \
    "${METADATA_ROOT}/resource-usage.txt" \
    "${METADATA_ROOT}/second-canonical-hashes.json" \
    "${METADATA_ROOT}/start-snap.json"; do
    test -f "$path"
  done
  zstd --test \
    "${METADATA_ROOT}/directed-base-edges.tsv.zst" \
    "${METADATA_ROOT}/edge-based-turn-states.tsv.zst"
  python3 "${REPOSITORY_ROOT}/scripts/compare_canonical_builds.py" \
    --first "${METADATA_ROOT}/first-canonical-hashes.json" \
    --second "${METADATA_ROOT}/second-canonical-hashes.json" \
    --diagnostics "${WORK_ROOT}/determinism-diff.json"
  cmp -- \
    "${CHECKPOINT_ROOT}/graph-binary-hashes.txt" \
    "${METADATA_ROOT}/graph-binary-hashes.txt"
}

metadata_built=false
if [[ -f "${METADATA_ROOT}/.complete" ]] && ! cmp -s -- \
  "${CHECKPOINT_ROOT}/graph-binary-hashes.txt" \
  "${METADATA_ROOT}/graph-binary-hashes.txt"; then
  rm -rf -- "$METADATA_ROOT"
fi
if [[ ! -f "${METADATA_ROOT}/.complete" ]]; then
  build_metadata_checkpoint
  metadata_built=true
fi
verify_metadata_checkpoint

if [[ "$metadata_built" == true ]]; then
  certification_metadata="${WORK_ROOT}/second/metadata"
else
  certification_metadata="${WORK_ROOT}/certification-metadata"
  rm -rf -- "$certification_metadata"
  mkdir -p -- "$certification_metadata"
  cp -- \
    "${METADATA_ROOT}/export-summary.json" \
    "${METADATA_ROOT}/start-snap.json" \
    "$certification_metadata/"
  zstd -dc "${METADATA_ROOT}/directed-base-edges.tsv.zst" \
    > "${certification_metadata}/directed-base-edges.tsv"
  zstd -dc "${METADATA_ROOT}/edge-based-turn-states.tsv.zst" \
    > "${certification_metadata}/edge-based-turn-states.tsv"
fi

{
  cat "${CHECKPOINT_ROOT}/resource-usage.txt"
  cat "${METADATA_ROOT}/resource-usage.txt"
} > "${WORK_ROOT}/resource-usage.txt"

python3 "${REPOSITORY_ROOT}/scripts/certify_graph_stage.py" \
  --manifest "$MANIFEST" \
  --profile "$PROFILE" \
  --pbf "$PBF_PATH" \
  --metadata "$certification_metadata" \
  --first-hashes "${METADATA_ROOT}/first-canonical-hashes.json" \
  --second-hashes "${METADATA_ROOT}/second-canonical-hashes.json" \
  --toolchain "${CHECKPOINT_ROOT}/toolchain.txt" \
  --resource-usage "${WORK_ROOT}/resource-usage.txt" \
  --graph-binary-hashes "${CHECKPOINT_ROOT}/graph-binary-hashes.txt" \
  --output "$RESULTS_ROOT"

rm -rf -- "$CACHE_ROOT"
mkdir -p -- "${CACHE_ROOT}/results"
cp -R -- "${RESULTS_ROOT}/." "${CACHE_ROOT}/results/"

echo "ROAD_GRAPH_STAGE_OK"
echo "PRG_PROVENANCE_LOCKED"
echo "SOLVER_NOT_STARTED"
