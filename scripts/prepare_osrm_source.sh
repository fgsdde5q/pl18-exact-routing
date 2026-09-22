#!/usr/bin/env bash

set -Eeuo pipefail

if (( $# != 1 )); then
  echo "usage: $0 OSRM_SOURCE_DIRECTORY" >&2
  exit 2
fi

readonly REPOSITORY_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly OSRM_SOURCE="$1"
readonly TOOL_SOURCE="${REPOSITORY_ROOT}/tools/osrm_graph_dump.cpp"
readonly TOOL_TARGET="${OSRM_SOURCE}/src/tools/graph_dump.cpp"
readonly CMAKE_FILE="${OSRM_SOURCE}/CMakeLists.txt"

if [[ "$(git -C "$OSRM_SOURCE" rev-parse --short=7 HEAD)" != "3c32a51" ]]; then
  echo "OSRM source is not pinned commit 3c32a51" >&2
  exit 1
fi

grep -Fq "inline ToNumeric from_alias" "${OSRM_SOURCE}/include/util/alias.hpp"
grep -Fq "osrm::from_alias<typename Alias::value_type>(input)" "$TOOL_SOURCE"

cp -- "$TOOL_SOURCE" "$TOOL_TARGET"

python3 - "$CMAKE_FILE" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
source = path.read_text(encoding="utf-8")
executable_anchor = "add_executable(osrm-customize src/tools/customize.cpp)\n"
link_anchor = "target_link_libraries(osrm-customize osrm_customize ${Boost_PROGRAM_OPTIONS_LIBRARY})\n"
executable_line = "add_executable(osrm-graph-dump src/tools/graph_dump.cpp)\n"
link_line = "target_link_libraries(osrm-graph-dump osrm_extract ${Boost_PROGRAM_OPTIONS_LIBRARY})\n"
if executable_line not in source:
    if source.count(executable_anchor) != 1:
        raise SystemExit("unexpected OSRM executable CMake anchor")
    source = source.replace(executable_anchor, executable_anchor + executable_line)
if link_line not in source:
    if source.count(link_anchor) != 1:
        raise SystemExit("unexpected OSRM link CMake anchor")
    source = source.replace(link_anchor, link_anchor + link_line)
path.write_text(source, encoding="utf-8", newline="\n")
PY

printf 'osrm_commit=%s\n' "$(git -C "$OSRM_SOURCE" rev-parse HEAD)"
printf 'graph_exporter_sha256=%s\n' "$(sha256sum "$TOOL_SOURCE" | awk '{print $1}')"
