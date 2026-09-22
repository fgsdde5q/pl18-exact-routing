#!/usr/bin/env bash

set -Eeuo pipefail

if (( $# != 2 )); then
  echo "usage: $0 OSRM_SOURCE OSRM_BUILD" >&2
  exit 2
fi

readonly OSRM_SOURCE="$1"
readonly OSRM_BUILD="$2"
readonly REPOSITORY_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly JOBS="${JOBS:-$(nproc)}"

bash "${REPOSITORY_ROOT}/scripts/prepare_osrm_source.sh" "$OSRM_SOURCE"

cmake_arguments=(
  -S "$OSRM_SOURCE"
  -B "$OSRM_BUILD"
  -G Ninja
  -DCMAKE_BUILD_TYPE=Release
  -DBUILD_TESTING=OFF
  -DENABLE_ASSERTIONS=OFF
)
if [[ -n "${VCPKG_ROOT:-}" ]]; then
  cmake_arguments+=("-DCMAKE_TOOLCHAIN_FILE=${VCPKG_ROOT}/scripts/buildsystems/vcpkg.cmake")
fi

cmake "${cmake_arguments[@]}"
cmake --build "$OSRM_BUILD" \
  --target osrm-extract osrm-partition osrm-customize osrm-graph-dump \
  --parallel "$JOBS"

for executable in osrm-extract osrm-partition osrm-customize osrm-graph-dump; do
  test -x "${OSRM_BUILD}/${executable}"
done
