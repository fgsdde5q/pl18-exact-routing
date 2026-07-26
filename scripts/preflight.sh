#!/usr/bin/env bash

set -Eeuo pipefail

readonly PBF_FILENAME="poland-260725.osm.pbf"
readonly PBF_URL="https://download.geofabrik.de/europe/${PBF_FILENAME}"
readonly PBF_MD5_URL="${PBF_URL}.md5"
readonly EXPECTED_PBF_SIZE_BYTES="2078786520"
readonly EXPECTED_PBF_MD5="eb188df5acafd002244ed84bb7b650ab"
readonly EXPECTED_PBF_SHA256="2f49ae5a61fbd70de5a8696ffa1cd1ac177bcfc9fea1d69cad43fbf4e5af4f28"
readonly PRG_WFS_URL="https://mapy.geoportal.gov.pl/wss/service/PZGIK/PRG/WFS/AdministrativeBoundaries"

if (( $# > 1 )); then
  echo "usage: $0 [output-directory]" >&2
  exit 2
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
readonly OUTPUT_DIR="${1:-${SCRIPT_DIR}/../preflight-output}"
readonly TEMP_ROOT="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
readonly PBF_CACHE_DIR="${PBF_CACHE_DIR:-}"
WORK_DIR="$(mktemp -d "${TEMP_ROOT%/}/pl18-preflight.XXXXXX")"
readonly WORK_DIR
trap 'rm -rf -- "$WORK_DIR"' EXIT

if [[ -n "$PBF_CACHE_DIR" ]]; then
  mkdir -p -- "$PBF_CACHE_DIR"
  PBF_PATH="${PBF_CACHE_DIR%/}/${PBF_FILENAME}"
else
  PBF_PATH="${WORK_DIR}/${PBF_FILENAME}"
fi
readonly PBF_PATH
readonly MD5_PATH="${WORK_DIR}/${PBF_FILENAME}.md5"
readonly CAPABILITIES_PATH="${OUTPUT_DIR}/prg-capabilities.xml"
readonly LAYERS_PATH="${OUTPUT_DIR}/prg-layers.txt"
readonly SHA256SUMS_PATH="${OUTPUT_DIR}/SHA256SUMS"
readonly ENVIRONMENT_PATH="${OUTPUT_DIR}/environment.txt"
readonly SUMMARY_PATH="${OUTPUT_DIR}/preflight-summary.md"

mkdir -p -- "$OUTPUT_DIR"

echo "Fetching the published Geofabrik MD5..."
curl \
  --fail \
  --location \
  --silent \
  --show-error \
  --retry 5 \
  --retry-all-errors \
  --retry-delay 5 \
  --connect-timeout 30 \
  --output "$MD5_PATH" \
  "$PBF_MD5_URL"

read -r published_md5 published_filename < "$MD5_PATH"
published_md5="${published_md5,,}"
if [[ ! "$published_md5" =~ ^[0-9a-f]{32}$ ]]; then
  echo "Published MD5 is malformed: ${published_md5}" >&2
  exit 1
fi
if [[ "$published_filename" != "$PBF_FILENAME" ]]; then
  echo "Published MD5 names an unexpected file: ${published_filename}" >&2
  exit 1
fi
if [[ "$published_md5" != "$EXPECTED_PBF_MD5" ]]; then
  echo "Published MD5 changed: expected ${EXPECTED_PBF_MD5}, got ${published_md5}" >&2
  exit 1
fi

if [[ -f "$PBF_PATH" ]]; then
  pbf_cache_status="hit"
  echo "Using cached ${PBF_FILENAME}..."
else
  pbf_cache_status="miss"
  echo "Downloading ${PBF_FILENAME} into runner temporary storage..."
  curl \
    --fail \
    --location \
    --silent \
    --show-error \
    --retry 5 \
    --retry-all-errors \
    --retry-delay 5 \
    --connect-timeout 30 \
    --continue-at - \
    --output "$PBF_PATH" \
    "$PBF_URL"
fi
readonly pbf_cache_status

actual_size_bytes="$(stat --format='%s' "$PBF_PATH")"
readonly actual_size_bytes
if [[ "$actual_size_bytes" != "$EXPECTED_PBF_SIZE_BYTES" ]]; then
  echo "PBF size mismatch: expected ${EXPECTED_PBF_SIZE_BYTES}, got ${actual_size_bytes}" >&2
  exit 1
fi

echo "Checking the PBF against the published MD5..."
pbf_md5="$(md5sum "$PBF_PATH" | awk '{print $1}')"
readonly pbf_md5
if [[ "$pbf_md5" != "$published_md5" ]]; then
  echo "PBF MD5 mismatch: expected ${published_md5}, got ${pbf_md5}" >&2
  exit 1
fi

echo "Computing SHA-256..."
pbf_sha256="$(sha256sum "$PBF_PATH" | awk '{print $1}')"
readonly pbf_sha256
if [[ "$pbf_sha256" != "$EXPECTED_PBF_SHA256" ]]; then
  echo "PBF SHA-256 mismatch: expected ${EXPECTED_PBF_SHA256}, got ${pbf_sha256}" >&2
  exit 1
fi

echo "Fetching PRG WFS GetCapabilities..."
curl \
  --fail \
  --get \
  --location \
  --silent \
  --show-error \
  --retry 5 \
  --retry-all-errors \
  --retry-delay 5 \
  --connect-timeout 30 \
  --data-urlencode "SERVICE=WFS" \
  --data-urlencode "REQUEST=GetCapabilities" \
  --output "$CAPABILITIES_PATH" \
  "$PRG_WFS_URL"

python3 "${SCRIPT_DIR}/list_wfs_layers.py" "$CAPABILITIES_PATH" > "$LAYERS_PATH"
layer_count="$(wc -l < "$LAYERS_PATH" | tr -d '[:space:]')"
readonly layer_count
capabilities_sha256="$(sha256sum "$CAPABILITIES_PATH" | awk '{print $1}')"
readonly capabilities_sha256

{
  printf '%s  %s\n' "$pbf_sha256" "$PBF_FILENAME"
  printf '%s  %s\n' "$capabilities_sha256" "prg-capabilities.xml"
} > "$SHA256SUMS_PATH"

{
  printf 'solver_status=SOLVER_NOT_STARTED\n'
  printf 'pbf_cache_status=%s\n' "$pbf_cache_status"
  printf 'generated_at_utc=%s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
  printf 'runner_os=%s\n' "${RUNNER_OS:-unknown}"
  printf 'runner_arch=%s\n' "${RUNNER_ARCH:-unknown}"
  printf 'runner_image_os=%s\n' "${ImageOS:-unknown}"
  printf 'runner_image_version=%s\n' "${ImageVersion:-unknown}"
  printf 'kernel=%s\n' "$(uname -srvmo)"
  printf 'bash=%s\n' "${BASH_VERSION}"
  printf 'python=%s\n' "$(python3 --version 2>&1)"
  printf 'curl=%s\n' "$(curl --version | sed -n '1p')"
  printf 'sha256sum=%s\n' "$(sha256sum --version | sed -n '1p')"
  printf 'md5sum=%s\n' "$(md5sum --version | sed -n '1p')"
} > "$ENVIRONMENT_PATH"

{
  printf '# Preflight summary\n\n'
  printf -- "- Status: \`SOLVER_NOT_STARTED\`\n"
  printf -- "- PBF cache: \`%s\`\n" "$pbf_cache_status"
  printf -- "- OSM input: \`%s\`\n" "$PBF_FILENAME"
  printf -- "- Verified size: \`%s\` bytes\n" "$actual_size_bytes"
  printf -- "- Verified published MD5: \`%s\`\n" "$published_md5"
  printf -- "- Computed PBF SHA-256: \`%s\`\n" "$pbf_sha256"
  printf -- "- PRG WFS FeatureType count: \`%s\`\n" "$layer_count"
  printf -- '- PBF artifact policy: omitted; persistence is handled only by GitHub Actions cache.\n'
  printf '\nNo optimizer or solver was executed during this stage.\n'
} > "$SUMMARY_PATH"

echo "Preflight completed with status SOLVER_NOT_STARTED."
