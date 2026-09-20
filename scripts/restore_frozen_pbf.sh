#!/usr/bin/env bash

set -Eeuo pipefail

if (( $# != 1 )); then
  echo "usage: $0 OUTPUT_PATH" >&2
  exit 2
fi

readonly OUTPUT_PATH="$1"
readonly URL="https://download.geofabrik.de/europe/poland-260725.osm.pbf"
readonly SIZE="2078786520"
readonly MD5="eb188df5acafd002244ed84bb7b650ab"
readonly SHA256="2f49ae5a61fbd70de5a8696ffa1cd1ac177bcfc9fea1d69cad43fbf4e5af4f28"

verify() {
  test -f "$OUTPUT_PATH"
  test "$(stat --format='%s' "$OUTPUT_PATH")" = "$SIZE"
  test "$(md5sum "$OUTPUT_PATH" | awk '{print $1}')" = "$MD5"
  test "$(sha256sum "$OUTPUT_PATH" | awk '{print $1}')" = "$SHA256"
}

if verify 2>/dev/null; then
  echo "Frozen PBF already present and verified"
  exit 0
fi

mkdir -p -- "$(dirname -- "$OUTPUT_PATH")"
temporary="${OUTPUT_PATH}.part"
trap 'rm -f -- "$temporary"' EXIT
curl --fail --location --retry 5 --retry-delay 5 --continue-at - \
  --output "$temporary" "$URL"
test "$(stat --format='%s' "$temporary")" = "$SIZE"
test "$(md5sum "$temporary" | awk '{print $1}')" = "$MD5"
test "$(sha256sum "$temporary" | awk '{print $1}')" = "$SHA256"
mv -- "$temporary" "$OUTPUT_PATH"
verify
trap - EXIT
echo "Frozen PBF downloaded and verified"
