#!/bin/sh
set -eu

ROOT=/home/rtlab/SLIMHUB_v2
ENV_FILE=${SLIMHUB_DB_ENV_FILE:-/home/rtlab/.config/slimhub-v2/db.env}

if [ -r "$ENV_FILE" ]; then
    set -a
    . "$ENV_FILE"
    set +a
fi

exec 9>/tmp/slimhub-v2-db-sync.lock
/usr/bin/flock -w 120 9
exec "$ROOT/.venv/bin/python" "$ROOT/scripts/db_upload.py" --base-dir "$ROOT"
