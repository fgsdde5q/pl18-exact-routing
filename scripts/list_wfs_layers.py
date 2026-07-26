#!/usr/bin/env python3

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def feature_type_names(root: ET.Element) -> list[str]:
    names: list[str] = []
    for feature_type in root.iter():
        if local_name(feature_type.tag) != "FeatureType":
            continue
        for child in feature_type:
            if local_name(child.tag) == "Name" and child.text:
                name = child.text.strip()
                if name:
                    names.append(name)
                break
    return sorted(set(names))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List FeatureType names from a WFS GetCapabilities document."
    )
    parser.add_argument("capabilities", type=Path, help="GetCapabilities XML file")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        root = ET.parse(args.capabilities).getroot()
    except (OSError, ET.ParseError) as error:
        print(f"error: cannot parse {args.capabilities}: {error}", file=sys.stderr)
        return 1

    names = feature_type_names(root)
    if not names:
        print("error: no WFS FeatureType names found", file=sys.stderr)
        return 1

    for name in names:
        print(name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
