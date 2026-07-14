# SLIMHUB_v2 release workflow

This is the DEAN Node v2 equivalent of the legacy SLIMHUB operating flow.
The daemon, its display, and the database cron job use the same repository
base directory below; replace it only when the deployment location differs.

```bash
export SLIMHUB_HOME=/home/rtlab/SLIMHUB_v2
cd "$SLIMHUB_HOME"
source .venv/bin/activate
```

## 1. Install and power the DEAN Node v2

Confirm the node is powered and advertising with the configured NUS device
name. Before starting the daemon, set the location for a known MAC if needed:

```bash
slimhub-v2 --config AA:BB:CC:DD:EE:FF location KITCHEN
```

## 2. Start SLIMHUB in the background

`slimhub-background` is the v2 replacement for the legacy alias. It is
installed by `python -m pip install -e .`; the explicit command below is an
equivalent fallback.

```bash
slimhub-background
# or: slimhub-v2 --run --background
```

Check startup and connected devices:

```bash
tail -n 50 logs/slimhub-v2.out
slimhub-v2 --list
```

Stop it after a deployment check with `slimhub-v2 --quit`.

## 3. Confirm collection and IN/OUT data

The daemon creates `programdata/display.txt` immediately at startup. It is an
operator feed for IN/OUT, ENV/SOUND, ADL, and BASELINE records; JSONL remains
the complete forensic source.

```bash
tail -F programdata/display.txt
tail -F programdata/reports/$(date +%F).jsonl
```

The daily, legacy-compatible display archive is
`data/display/YYYY-MM-DD.txt`.

## 4. Configure the database cron job

Provide database credentials to the cron environment through a protected
wrapper or service environment file. Do not put secrets in the repository or
directly in the crontab. Required local variables are
`SLIMHUB_LOCAL_DB_HOST`, `SLIMHUB_LOCAL_DB_USER`, and
`SLIMHUB_LOCAL_DB_NAME`; `SLIMHUB_LOCAL_DB_PASS` is supported. Configure the
corresponding `SLIMHUB_REMOTE_DB_*` variables when remote upload is required.

Copy the entry in [`slimhub-v2.crontab`](slimhub-v2.crontab) into `crontab -e`.
It runs every three minutes, protects against overlap with `flock`, runs local
ingest, then uploads to the configured remote database. Adjust `*/3` to
`*/5` or `*/10` for five- or ten-minute operation.

```bash
crontab -l
tail -F logs/db-update.log
```

## 5. Confirm local database ingestion

After the selected cron interval, use the status command. `last_update.ok`
means the local MySQL transaction completed and the source offset was written.

```bash
slimhub-v2 db status
```

The command reports the safe database configuration state, source JSONL
offsets, local upload offsets, and the last combined update result. It never
prints passwords.

## 6. Confirm remote upload

For an enabled remote database, `last_update.upload.adl` and
`last_update.upload.inout` show `uploaded` row counts and the local `last_id`
that advanced only after the remote transaction committed. Verify the same
new rows in the remote `event_adl` and `in_out` tables using the normal DB
operator account.

## 7. Release checklist

- Node is connected and its location configuration is correct.
- `display.txt` shows expected collection and IN/OUT events.
- `db status` has a successful recent update and advancing offsets.
- Remote table rows have been independently checked.
- Preserve `programdata/reports/*.jsonl`, `programdata/db_sync/last_update.json`,
  and `logs/db-update.log` as release evidence.
