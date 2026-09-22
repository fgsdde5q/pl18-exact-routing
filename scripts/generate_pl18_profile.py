#!/usr/bin/env python3

import argparse
import json
import re
import subprocess
from pathlib import Path


def load_manifest(repository_root: Path, manifest_path: Path) -> dict:
    result = subprocess.run(
        [
            "ruby",
            str(repository_root / "scripts/manifest_json.rb"),
            str(manifest_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def lua_scalar(value: object) -> str:
    if value is None:
        return "nil"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return f"{value:g}"
    return json.dumps(value, ensure_ascii=False)


def lua_key(key: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        return key
    return f"[{json.dumps(key, ensure_ascii=False)}]"


def lua_mapping(mapping: dict, indent: int) -> str:
    prefix = " " * indent
    return "\n".join(
        f"{prefix}{lua_key(key)} = {lua_scalar(value)},"
        for key, value in mapping.items()
    )


def replace_block(source: str, start_marker: str, end_marker: str, replacement: str) -> str:
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[:start] + replacement + source[end:]


def require_replace(source: str, old: str, new: str) -> str:
    count = source.count(old)
    if count != 1:
        raise ValueError(f"expected exactly one upstream occurrence, found {count}: {old!r}")
    return source.replace(old, new)


def generate(upstream: str, manifest: dict, manifest_sha256: str) -> str:
    routing = manifest["routing_engine"]
    access = manifest["access_model"]
    speed = manifest["speed_model"]
    turn = manifest["turn_cost_model"]
    vehicle = manifest["vehicle"]

    source = upstream
    source = require_replace(
        source,
        "Measure = require(\"lib/measure\")\n",
        "Measure = require(\"lib/measure\")\nTags = require(\"lib/tags\")\n",
    )
    source = require_replace(
        source,
        "weight_name                     = 'routability',",
        f"weight_name                     = '{routing['weight_name']}',",
    )
    source = require_replace(
        source,
        "process_call_tagless_node      = false,",
        "process_call_tagless_node      = false,\n"
        f"      weight_precision               = {routing['weight_precision_decimals']},",
    )
    source = require_replace(
        source,
        "u_turn_penalty                 = 20,",
        f"u_turn_penalty                 = {turn['u_turn_extra_s']:g},",
    )
    source = require_replace(
        source,
        "default_speed             = 10,",
        f"default_speed             = {speed['default_speed_kmh']:g},",
    )
    source = require_replace(
        source,
        "side_road_multiplier      = 0.8,",
        f"side_road_multiplier      = {speed['way_duration_multipliers']['side_road']:g},",
    )
    source = require_replace(
        source,
        "turn_penalty              = 7.5,",
        f"turn_penalty              = {turn['angle_penalty']['maximum_s']:g},",
    )
    source = require_replace(
        source,
        "speed_reduction           = 0.8,",
        f"speed_reduction           = {speed['maxspeed_reduction_factor']:g},",
    )
    source = require_replace(
        source,
        "turn_bias                 = 1.075,",
        f"turn_bias                 = {turn['angle_penalty']['right_hand_traffic_bias']:g},",
    )
    source = require_replace(
        source,
        "lane_markings_penalty     = 0.75,",
        f"lane_markings_penalty     = {speed['way_duration_multipliers']['lane_markings_no']:g},",
    )
    source = require_replace(source, "vehicle_height = 2.0,", f"vehicle_height = {vehicle['height_m']:g},")
    source = require_replace(source, "vehicle_width = 1.9,", f"vehicle_width = {vehicle['width_m']:g},")
    source = require_replace(source, "vehicle_length = 4.8,", f"vehicle_length = {vehicle['length_m']:g},")
    source = require_replace(source, "vehicle_weight = 2000,", f"vehicle_weight = {vehicle['weight_kg']:g},")

    access_whitelist = "\n".join(
        f"      {json.dumps(value)}," for value in access["allowed_values"]
    )
    source = replace_block(
        source,
        "    access_tag_whitelist = Set {",
        "    access_tag_blacklist = Set {",
        "    access_tag_whitelist = Set {\n"
        f"{access_whitelist}\n"
        "    },\n\n",
    )
    access_blacklist = "\n".join(
        f"      {json.dumps(value)}," for value in access["forbidden_values"]
    )
    source = replace_block(
        source,
        "    access_tag_blacklist = Set {",
        "    -- tags disallow access",
        "    access_tag_blacklist = Set {\n"
        f"{access_blacklist}\n"
        "    },\n\n",
    )
    source = replace_block(
        source,
        "    restricted_access_tag_list = Set {",
        "    access_tags_hierarchy = Sequence {",
        "    restricted_access_tag_list = Set {\n"
        "    },\n\n",
    )
    source = replace_block(
        source,
        "    speeds = Sequence {",
        "    service_penalties = {",
        "    speeds = Sequence {\n"
        "      highway = {\n"
        f"{lua_mapping(speed['class_speed_kmh'], 8)}\n"
        "      }\n"
        "    },\n\n",
    )
    service_mapping = {
        "alley": speed["way_duration_multipliers"]["service_alley"],
        "parking": speed["way_duration_multipliers"]["service_parking"],
        "parking_aisle": speed["way_duration_multipliers"]["service_parking_aisle"],
        "driveway": speed["way_duration_multipliers"]["service_driveway"],
        "drive-through": speed["way_duration_multipliers"]["service_drive-through"],
        "drive-thru": speed["way_duration_multipliers"]["service_drive-thru"],
    }
    source = replace_block(
        source,
        "    service_penalties = {",
        "    barrier_penalties = {",
        "    service_penalties = {\n"
        f"{lua_mapping(service_mapping, 6)}\n"
        "    },\n\n",
    )
    source = replace_block(
        source,
        "    barrier_penalties = {",
        "    restricted_highway_whitelist = Set {",
        "    barrier_penalties = {\n"
        f"{lua_mapping(turn['barrier_penalty_s'], 6)}\n"
        "    },\n\n",
    )
    source = replace_block(
        source,
        "    route_speeds = {",
        "    bridge_speeds = {",
        "    route_speeds = {\n"
        "    },\n\n",
    )
    source = replace_block(
        source,
        "    bridge_speeds = {",
        "    -- surface/trackype/smoothness",
        "    bridge_speeds = {\n"
        f"{lua_mapping(speed['bridge_cap_kmh'], 6)}\n"
        "    },\n\n",
    )
    source = replace_block(
        source,
        "    surface_speeds = {",
        "    -- max speed for tracktypes",
        "    surface_speeds = {\n"
        f"{lua_mapping(speed['surface_cap_kmh'], 6)}\n"
        "    },\n\n",
    )
    source = replace_block(
        source,
        "    tracktype_speeds = {",
        "    -- max speed for smoothnesses",
        "    tracktype_speeds = {\n"
        f"{lua_mapping(speed['tracktype_cap_kmh'], 6)}\n"
        "    },\n\n",
    )
    source = replace_block(
        source,
        "    smoothness_speeds = {",
        "    -- http://wiki.openstreetmap.org/wiki/Speed_limits",
        "    smoothness_speeds = {\n"
        f"{lua_mapping(speed['smoothness_cap_kmh'], 6)}\n"
        "    },\n\n",
    )
    source = replace_block(
        source,
        "    maxspeed_table_default = {",
        "    -- List only exceptions",
        "    maxspeed_table_default = {\n"
        "    },\n\n",
    )
    source = replace_block(
        source,
        "    maxspeed_table = {",
        "    relation_types = Sequence {",
        "    maxspeed_table = {\n"
        f"{lua_mapping(speed['symbolic_maxspeed_kmh'], 6)}\n"
        "    },\n\n",
    )

    source = require_replace(source, "\n  Obstacles.process_node(profile, node)\n", "\n")

    project_handlers = f"""
local function project_static_policy(profile, way, result, data)
  if data.route == "ferry" or data.route == "shuttle_train" then
    result.forward_mode = mode.inaccessible
    result.backward_mode = mode.inaccessible
    return false
  end
  if not data.highway or not profile.speeds.highway[data.highway] then
    result.forward_mode = mode.inaccessible
    result.backward_mode = mode.inaccessible
    return false
  end
end

local function project_access(profile, way, result, data)
  if WayHandlers.access(profile, way, result, data) == false then
    return false
  end
  if data.forward_access and not profile.access_tag_whitelist[data.forward_access] then
    result.forward_mode = mode.inaccessible
  end
  if data.backward_access and not profile.access_tag_whitelist[data.backward_access] then
    result.backward_mode = mode.inaccessible
  end
  if result.forward_mode == mode.inaccessible and result.backward_mode == mode.inaccessible then
    return false
  end
end

local function project_has_nonpositive_numeric_maxspeed(value)
  if not value then
    return false
  end
  local numeric = tonumber(string.match(value, "^%s*([%+%-]?[%d%.]+)"))
  return numeric and numeric <= 0
end

local function project_maxspeed(profile, way, result, data)
  local forward_value = way:get_value_by_key("maxspeed:forward") or way:get_value_by_key("maxspeed")
  local backward_value = way:get_value_by_key("maxspeed:backward") or way:get_value_by_key("maxspeed")
  local symbolic = way:get_value_by_key("source:maxspeed")
  local forward = WayHandlers.parse_maxspeed(forward_value or symbolic, profile)
  local backward = WayHandlers.parse_maxspeed(backward_value or symbolic, profile)
  if project_has_nonpositive_numeric_maxspeed(forward_value) then
    result.forward_mode = mode.inaccessible
  elseif forward and forward > 0 and result.forward_speed > 0 then
    result.forward_speed = math.min(result.forward_speed, forward * profile.speed_reduction)
  end
  if project_has_nonpositive_numeric_maxspeed(backward_value) then
    result.backward_mode = mode.inaccessible
  elseif backward and backward > 0 and result.backward_speed > 0 then
    result.backward_speed = math.min(result.backward_speed, backward * profile.speed_reduction)
  end
end

"""
    source = require_replace(source, "function process_way(profile, way, result, relations)\n", project_handlers + "function process_way(profile, way, result, relations)\n")
    source = require_replace(
        source,
        "    WayHandlers.default_mode,\n",
        "    WayHandlers.default_mode,\n"
        "    project_static_policy,\n",
    )
    source = require_replace(source, "    WayHandlers.access,\n", "    project_access,\n")
    source = require_replace(source, "    WayHandlers.maxspeed,\n", "    project_maxspeed,\n")

    header = (
        "-- Generated by scripts/generate_pl18_profile.py; do not edit by hand.\n"
        "-- Base: Project-OSRM/osrm-backend v26.5.0 commit 3c32a51.\n"
        f"-- Frozen manifest SHA-256: {manifest_sha256}.\n"
    )
    return header + source


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--upstream-car", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    repository_root = Path(__file__).resolve().parent.parent
    manifest_bytes = args.manifest.read_bytes()
    import hashlib

    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    manifest = load_manifest(repository_root, args.manifest)
    output = generate(
        args.upstream_car.read_text(encoding="utf-8"),
        manifest,
        manifest_sha256,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(output, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
