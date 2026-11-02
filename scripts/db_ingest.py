#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from slimhub.config import AppPaths
from slimhub.integrations.battery_log import export_battery_logs
from slimhub.integrations.database import DataDirectoryDatabaseUpdater


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Incrementally load debugstr INFERENCE and EVENT ENTER/EXIT, and battery/ "
            "BATTERY lines into local MySQL."
        )
    )
    parser.add_argument("--base-dir", default="/home/rtlab/SLIMHUB_v2")
    args = parser.parse_args()
    paths = AppPaths.from_base(args.base_dir)
    # Node 배터리 측정(validation VLB1)을 먼저 battery/<날짜>.txt에 덧붙입니다.
    # 실패해도 ADL/INOUT 적재는 계속합니다.
    try:
        print(json.dumps({"battery_export": export_battery_logs(paths)}, sort_keys=True))
    except Exception as exc:
        print(f"battery-export ERROR: {exc}", file=sys.stderr)
    updater = DataDirectoryDatabaseUpdater(paths)
    try:
        result = updater.ingest()
    except Exception as exc:
        print(f"db-ingest ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
