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
readonly REPOSITORY_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly MANIFEST="${REPOSITORY_ROOT}/instance/pl_18_capitals_static_instance_v2.yaml"
readonly PROFILE="${REPOSITORY_ROOT}/profiles/pl18-car.lua"
readonly EXPECTED_MANIFEST_SHA256="f47d9a805defdb7e28005048d7ad9a7a76f666e75d508422a7edd239ce60f7ce"
readonly EXPECTED_PBF_SIZE="2078786520"
readonly EXPECTED_PBF_MD5="eb188df5acafd002244ed84bb7b650ab"
readonly EXPECTED_PBF_SHA256="2f49ae5a61fbd70de5a8696ffa1cd1ac177bcfc9fea1d69cad43fbf4e5af4f28"
readonly OSRM_COMMIT="3c32a51bf58d12bf30efd0808d0b6ad51d334122"
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

python3 "${REPOSITORY_ROOT}/scripts/test_stage2a_manifest_contract.py"
python3 "${REPOSITORY_ROOT}/scripts/test_stage2a_cache_contract.py"
python3 "${REPOSITORY_ROOT}/scripts/generate_pl18_profile.py" \
  --manifest "$MANIFEST" \
  --upstream-car "${OSRM_SOURCE}/profiles/car.lua" \
  --output "${WORK_ROOT}/generated-pl18-car.lua"
cmp -- "$PROFILE" "${WORK_ROOT}/generated-pl18-car.lua"
UPSTREAM_CAR_PROFILE="${OSRM_SOURCE}/profiles/car.lua" \
  python3 "${REPOSITORY_ROOT}/scripts/test_pl18_profile.py"
python3 "${REPOSITORY_ROOT}/scripts/test_graph_metadata_export.py"

for executable in osrm-extract osrm-partition osrm-customize osrm-graph-dump; do
  test -x "${OSRM_BUILD}/${executable}"
done
test -f "${OSRM_SOURCE}/profiles/lib/set.lua"

readonly TOOLCHAIN="${WORK_ROOT}/toolchain.txt"
{
  printf 'osrm_version=v26.5.0\n'
  printf 'osrm_commit=%s\n' "$(git -C "$OSRM_SOURCE" rev-parse HEAD)"
  printf 'compiler=%s\n' "$("${CXX:-c++}" --version | sed -n '1p')"
  printf 'cmake=%s\n' "$(cmake --version | sed -n '1p')"
  printf 'python=%s\n' "$(python3 --version 2>&1)"
  printf 'pyosmium=%s\n' "$(python3 -c 'import importlib.metadata; print(importlib.metadata.version("osmium"))')"
  printf 'pyproj=%s\n' "$(python3 -c 'import importlib.metadata; print(importlib.metadata.version("pyproj"))')"
  if [[ -n "${VCPKG_ROOT:-}" ]]; then
    printf 'boost=%s\n' "$("${VCPKG_ROOT}/vcpkg" list --x-install-root="${OSRM_BUILD}/vcpkg_installed" | awk '$1 ~ /^boost:/ {print $2; exit}')"
    printf 'lua=%s\n' "$("${VCPKG_ROOT}/vcpkg" list --x-install-root="${OSRM_BUILD}/vcpkg_installed" | awk '$1 ~ /^lua:/ {print $2; exit}')"
    printf 'vcpkg_commit=%s\n' "$(git -C "$VCPKG_ROOT" rev-parse HEAD)"
  else
    printf 'boost=%s\n' "$(dpkg-query -W -f='${Version}\n' libboost-dev)"
    printf 'lua=%s\n' "$(pkg-config --modversion lua5.4)"
    printf 'vcpkg_commit=not_used\n'
  fi
  printf 'os=%s\n' "$(source /etc/os-release && printf '%s %s' "$NAME" "$VERSION_ID")"
  printf 'architecture=%s\n' "$(uname -m)"
  printf 'kernel=%s\n' "$(uname -srvmo)"
  printf 'docker_image_digest=not_used\n'
  printf 'graph_exporter_sha256=%s\n' "$(sha256sum "${REPOSITORY_ROOT}/tools/osrm_graph_dump.cpp" | awk '{print $1}')"
  printf 'graph_exporter_commit=%s\n' "${GITHUB_SHA:-local}"
  printf 'profile_sha256=%s\n' "$(sha256sum "$PROFILE" | awk '{print $1}')"
  printf 'manifest_sha256=%s\n' "$EXPECTED_MANIFEST_SHA256"
} > "$TOOLCHAIN"

build_graph() {
  local build_name="$1"
  local build_root="${WORK_ROOT}/${build_name}"
  local graph_root="${build_root}/graph"
  local dump_root="${build_root}/dump"
  local metadata_root="${build_root}/metadata"
  local temp_root="${build_root}/sort-temp"
  local resource_log="${build_root}/resource-usage.txt"
  mkdir -p -- "$graph_root" "$dump_root" "$metadata_root" "$temp_root"

  /usr/bin/time -v -o "${build_root}/extract.time" \
    env "LUA_PATH=${OSRM_PROFILE_LUA_PATH}" \
    "${OSRM_BUILD}/osrm-extract" \
    --threads "$JOBS" \
    --profile "$PROFILE" \
    --output "${graph_root}/poland.osrm" \
    "$PBF_PATH"
  /usr/bin/time -v -o "${build_root}/partition.time" \
    "${OSRM_BUILD}/osrm-partition" \
    --threads "$JOBS" \
    "${graph_root}/poland.osrm"
  /usr/bin/time -v -o "${build_root}/customize.time" \
    "${OSRM_BUILD}/osrm-customize" \
    --threads "$JOBS" \
    "${graph_root}/poland.osrm"
  /usr/bin/time -v -o "${build_root}/dump.time" \
    "${OSRM_BUILD}/osrm-graph-dump" \
    "${graph_root}/poland.osrm" \
    "$dump_root"
  /usr/bin/time -v -o "${build_root}/metadata.time" \
    python3 "${REPOSITORY_ROOT}/scripts/export_graph_metadata.py" \
      --manifest "$MANIFEST" \
      --pbf "$PBF_PATH" \
      --osrm-dump "$dump_root" \
      --output "$metadata_root" \
      --temp "$temp_root"

  {
    for phase in extract partition customize dump metadata; do
      printf '## %s %s\n' "$build_name" "$phase"
      cat "${build_root}/${phase}.time"
    done
    printf '## disk after %s\n' "$build_name"
    df -h "$WORK_ROOT"
    printf '## graph disk usage\n'
    du -sh "$graph_root" "$metadata_root"
  } > "$resource_log"

  python3 - "$metadata_root" "${build_root}/canonical-hashes.json" <<'PY'
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

  (
    cd "$graph_root"
    find . -maxdepth 1 -type f -name 'poland.osrm*' -print0 |
      sort -z |
      xargs -0 sha256sum |
      sed 's#  \./#  #' > "${build_root}/graph-binary-hashes.txt"
  )
}

build_graph first

python3 "${REPOSITORY_ROOT}/scripts/validate_graph_metadata.py" \
  --metadata "${WORK_ROOT}/first/metadata"

zstd -19 -T0 --rm "${WORK_ROOT}/first/metadata/directed-base-edges.tsv"
zstd -19 -T0 --rm "${WORK_ROOT}/first/metadata/edge-based-turn-states.tsv"
rm -rf -- "${WORK_ROOT}/first/graph" "${WORK_ROOT}/first/dump" "${WORK_ROOT}/first/sort-temp"

build_graph second

cmp -- "${WORK_ROOT}/first/canonical-hashes.json" "${WORK_ROOT}/second/canonical-hashes.json"

cat "${WORK_ROOT}/first/resource-usage.txt" "${WORK_ROOT}/second/resource-usage.txt" \
  > "${WORK_ROOT}/resource-usage.txt"

python3 "${REPOSITORY_ROOT}/scripts/certify_graph_stage.py" \
  --manifest "$MANIFEST" \
  --profile "$PROFILE" \
  --pbf "$PBF_PATH" \
  --metadata "${WORK_ROOT}/second/metadata" \
  --first-hashes "${WORK_ROOT}/first/canonical-hashes.json" \
  --second-hashes "${WORK_ROOT}/second/canonical-hashes.json" \
  --toolchain "$TOOLCHAIN" \
  --resource-usage "${WORK_ROOT}/resource-usage.txt" \
  --graph-binary-hashes-first "${WORK_ROOT}/first/graph-binary-hashes.txt" \
  --graph-binary-hashes-second "${WORK_ROOT}/second/graph-binary-hashes.txt" \
  --output "$RESULTS_ROOT"

zstd -19 -T0 --rm "${WORK_ROOT}/second/metadata/directed-base-edges.tsv"
zstd -19 -T0 --rm "${WORK_ROOT}/second/metadata/edge-based-turn-states.tsv"

rm -rf -- "$CACHE_ROOT"
mkdir -p -- "${CACHE_ROOT}/graph" "${CACHE_ROOT}/canonical" "${CACHE_ROOT}/results"
mv -- "${WORK_ROOT}/second/graph/"* "${CACHE_ROOT}/graph/"
cp -- "${WORK_ROOT}/second/metadata/"*.zst "${CACHE_ROOT}/canonical/"
for archive in "${WORK_ROOT}/first/metadata/"*.zst; do
  cp -- "$archive" "${CACHE_ROOT}/canonical/first-$(basename "$archive")"
done
cp -R -- "${RESULTS_ROOT}/." "${CACHE_ROOT}/results/"

echo "ROAD_GRAPH_STAGE_OK"
echo "PRG_PROVENANCE_LOCKED"
echo "SOLVER_NOT_STARTED"
