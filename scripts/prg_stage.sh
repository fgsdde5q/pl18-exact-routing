#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
readonly PYTHON_BIN="${PYTHON_BIN:-python3}"

"$PYTHON_BIN" "${SCRIPT_DIR}/prg_stage.py"
"$PYTHON_BIN" "${SCRIPT_DIR}/validate_prg_stage.py"
