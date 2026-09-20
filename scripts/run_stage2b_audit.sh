#!/usr/bin/env bash

set -Eeuo pipefail

if (( $# != 6 )); then
  echo "usage: $0 METADATA_ROOT WORK_ROOT RESULTS_ROOT ARTIFACT_ROOT MODE PYTHON" >&2
  exit 2
fi

readonly METADATA_ROOT="$1"
readonly WORK_ROOT="$2"
readonly RESULTS_ROOT="$3"
readonly ARTIFACT_ROOT="$4"
readonly MODE="$5"
readonly PYTHON="$6"
REPOSITORY_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly REPOSITORY_ROOT
readonly EXPECTED_EDGE_SHA256="f27449dfa026193831458712bcb97c1c8b07fdf9fe4f9d33c6c2f2f613a4406a"
readonly EXPECTED_TURN_SHA256="cb613a7ad6019181064b3f09ef36ccabd8a732ad50897f04da49468306e23bad"

if [[ "$MODE" != export && "$MODE" != rebuild ]]; then
  echo "MODE must be export or rebuild" >&2
  exit 2
fi
for path in \
  "${METADATA_ROOT}/directed-base-edges.tsv.zst" \
  "${METADATA_ROOT}/edge-based-turn-states.tsv.zst" \
  "${METADATA_ROOT}/.complete"; do
  test -f "$path"
done

rm -rf -- "$WORK_ROOT" "$RESULTS_ROOT" "$ARTIFACT_ROOT"
mkdir -p -- "$WORK_ROOT/inputs" "$RESULTS_ROOT" "$ARTIFACT_ROOT"
zstd -q -dc "${METADATA_ROOT}/directed-base-edges.tsv.zst" > "${WORK_ROOT}/inputs/directed-base-edges.tsv"
zstd -q -dc "${METADATA_ROOT}/edge-based-turn-states.tsv.zst" > "${WORK_ROOT}/inputs/edge-based-turn-states.tsv"
test "$(sha256sum "${WORK_ROOT}/inputs/directed-base-edges.tsv" | awk '{print $1}')" = "$EXPECTED_EDGE_SHA256"
test "$(sha256sum "${WORK_ROOT}/inputs/edge-based-turn-states.tsv" | awk '{print $1}')" = "$EXPECTED_TURN_SHA256"

build_stage2b() {
  local build_id="$1"
  local root="${WORK_ROOT}/${build_id}"
  test ! -e "$root"
  mkdir -p -- "${root}/results" "${root}/artifacts"
  PYTHONPATH="${REPOSITORY_ROOT}/scripts" "$PYTHON" \
    "${REPOSITORY_ROOT}/scripts/build_stage2b.py" \
    --base-edges "${WORK_ROOT}/inputs/directed-base-edges.tsv" \
    --turn-states "${WORK_ROOT}/inputs/edge-based-turn-states.tsv" \
    --output "${root}/results" \
    --artifact-output "${root}/artifacts"
}

build_stage2b first-clean-build
build_stage2b second-clean-build

"$PYTHON" "${REPOSITORY_ROOT}/scripts/compare_stage2b_builds.py" \
  --first-results "${WORK_ROOT}/first-clean-build/results" \
  --second-results "${WORK_ROOT}/second-clean-build/results" \
  --output "$RESULTS_ROOT" \
  --mode "$MODE"

cp -R -- "${WORK_ROOT}/first-clean-build/artifacts/." "$ARTIFACT_ROOT/"
(
  cd "$ARTIFACT_ROOT"
  sha256sum --check <(
    "$PYTHON" - "$RESULTS_ROOT/stage2b-manifest.json" <<'PY'
import json
import sys
manifest = json.load(open(sys.argv[1], encoding="utf-8"))
for name, metadata in sorted(manifest["artifact_hashes"].items()):
    print(f"{metadata['sha256']}  {name}")
PY
  )
)

echo "SOLVER_NOT_STARTED"
