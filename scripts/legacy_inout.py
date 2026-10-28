#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from slimhub.config import AppPaths
from slimhub.integrations.legacy_inout import convert_debugstr


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "One-time conversion of days recorded before the daemon wrote v1-style IN/OUT: "
            "move each room's Node ENTER/EXIT from debugstr to new_algo_inout and merge the "
            "one-active-room EVENT lines (from rawdata Node ENTERs) into debugstr and display. "
            "Stop the daemon first."
        )
    )
    parser.add_argument("--base-dir", default="/home/rtlab/SLIMHUB_v2")
    parser.add_argument("--from", dest="from_date", required=True, help="first day YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true", help="print counts; write nothing")
    args = parser.parse_args()
    try:
        result = convert_debugstr(
            AppPaths.from_base(args.base_dir), args.from_date, dry_run=args.dry_run
        )
    except Exception as exc:
        print(f"legacy-inout ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
