#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from slimhub.config import AppPaths
from slimhub.integrations.database import DataDirectoryDatabaseUpdater


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Upload new local MySQL event_adl/in_out rows to remote MySQL."
    )
    parser.add_argument("--base-dir", default="/home/rtlab/SLIMHUB_v2")
    args = parser.parse_args()
    updater = DataDirectoryDatabaseUpdater(AppPaths.from_base(args.base_dir))
    try:
        result = updater.upload()
    except Exception as exc:
        print(f"db-upload ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
