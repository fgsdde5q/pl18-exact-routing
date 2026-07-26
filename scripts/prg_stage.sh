#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
readonly PYTHON_BIN="${PYTHON_BIN:-python3}"

if [[ "${PRG_REFRESH_OFFICIAL_RESPONSES:-0}" == "1" ]]; then
  "$PYTHON_BIN" "${SCRIPT_DIR}/prg_stage.py" --refresh-official-responses
else
  "$PYTHON_BIN" "${SCRIPT_DIR}/prg_stage.py"
fi

"$PYTHON_BIN" "${SCRIPT_DIR}/validate_prg_stage.py"
"$PYTHON_BIN" "${SCRIPT_DIR}/test_prg_manifest_contract.py"
