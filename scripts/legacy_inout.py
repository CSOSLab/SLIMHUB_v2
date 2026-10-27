#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from slimhub.config import AppPaths
from slimhub.integrations.data_ingest import legacy_inout_from
from slimhub.integrations.legacy_inout import LegacyInOutGenerator


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build data/legacy_inout (v1 one-active-room IN/OUT) from rawdata ENTERs."
    )
    parser.add_argument("--base-dir", default="/home/rtlab/SLIMHUB_v2")
    parser.add_argument("--from", dest="from_date",
                        help="cut-over date YYYY-MM-DD (default: SLIMHUB_LEGACY_INOUT_FROM)")
    parser.add_argument("--rebuild", action="store_true",
                        help="back up existing output from the cut-over date and regenerate")
    parser.add_argument("--dry-run", action="store_true",
                        help="print per-day counts and sample lines; write nothing")
    args = parser.parse_args()
    paths = AppPaths.from_base(args.base_dir)
    from_date = args.from_date or legacy_inout_from(paths, dict(os.environ))
    if from_date is None:
        print("legacy-inout ERROR: no cut-over date (--from or SLIMHUB_LEGACY_INOUT_FROM)",
              file=sys.stderr)
        return 2
    generator = LegacyInOutGenerator.from_environ(paths, from_date)
    try:
        result = generator.run(rebuild=args.rebuild, dry_run=args.dry_run)
    except Exception as exc:
        print(f"legacy-inout ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
