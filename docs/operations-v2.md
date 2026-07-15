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
operator feed containing only confirmed IN/OUT and final inference records.
Individual ENV/SOUND events, candidates, ACKs, timeouts, and baselines stay in
JSONL and do not clutter the operator display. The matching legacy-compatible
JSON is written under each node's `inference/debugstr/YYYY-MM-DD.txt`; JSONL
remains the complete forensic source. On startup, existing feature-only lines
are also removed from the current display and today's text archive; JSONL is
never rewritten.

If a deployed Node v2 image omits final `src=ADL` reports, Central emits only
the conservative location/event signatures learned from the deployed legacy
history. These records have kind `derived_inference` and
`ground_truth_eligible=false`; they must not be treated as firmware truth.

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
`house_mac` defaults to the Hub address in `programdata/config.json`; set
`SLIMHUB_HOUSE_MAC` only when the deployment uses a separate house identifier.

Copy the entries in [`slimhub-v2.crontab`](slimhub-v2.crontab) into `crontab -e`.
They mirror the deployed SLIMHUB cadence: local ingest every three minutes and
remote upload every ten minutes. A shared `flock` serializes coincident runs.
Change the first expression to `*/5` when five-minute local ingest is desired.

```bash
crontab -l
tail -F logs/db-ingest.log
tail -F logs/db-upload.log
```

## 5. Confirm local database ingestion

After the selected cron interval, use the status command. `last_ingest.ok`
means the local MySQL transaction completed and the source offset was written.

```bash
slimhub-v2 db status
```

The command reports the safe database configuration state, source JSONL
offsets, local upload offsets, and the last ingest/upload results. It never
prints passwords. By default the first run starts with today's JSONL, matching
the legacy cron. Set `SLIMHUB_DB_BACKFILL=1` only for an intentional historical
import.

## 6. Confirm remote upload

For an enabled remote database, `last_upload.result.adl` and
`last_upload.result.inout` show `uploaded` row counts and the local `last_id`.
Each stream offset advances immediately after its own remote transaction
commits, so a later stream failure does not resend committed rows. Verify the
same new rows in the remote `event_adl` and `in_out` tables using the normal DB
operator account.

## 7. Release checklist

- Node is connected and its location configuration is correct.
- `display.txt` shows only expected IN/OUT and final inference records.
- `db status` has successful recent ingest/upload runs and advancing offsets.
- Remote table rows have been independently checked.
- Preserve `programdata/reports/*.jsonl`, `programdata/db_sync/last_ingest.json`,
  `programdata/db_sync/last_upload.json`, and the DB cron logs as release evidence.
