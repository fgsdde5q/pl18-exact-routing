#!/usr/bin/env bash

set -Eeuo pipefail

if (( $# != 1 )); then
  echo "usage: $0 OUTPUT_PATH" >&2
  exit 2
fi

readonly OUTPUT_PATH="$1"
readonly URL="https://github.com/fgsdde5q/pl18-exact-routing/releases/download/frozen-osm-2026-09-01/poland-260901.osm.pbf"
readonly SIZE="2091448485"
readonly MD5="0db66b478a3ed7c6f52d182e13c6fa27"
readonly SHA256="f28f493c6cc280da1128b03be21ae2eb1973f443c14235dea36088bcbd3e83f3"

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
