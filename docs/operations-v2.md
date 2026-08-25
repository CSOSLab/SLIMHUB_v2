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
slimhub-v2 config set --address AA:BB:CC:DD:EE:FF location KITCHEN
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
slimhub-v2 node status --address AA:BB:CC:DD:EE:FF
```

Every connection queues `time_sync`, `node_status`, then `config_get` after
notification subscription. The demo uses SLIMHUB's single home-wide
occupancy token and candidate-correlated `inout_confirm`. During an A→B
handoff only, Central sends `exit` to occupied A, waits for A's successful
legacy-mode ACK plus `EXIT_SYNC/D1` and committed DEBUG EXIT, then confirms
B. `enter` and `inout_sync` are not sent.

Stop it after a deployment check with `slimhub-v2 --quit`.

## 3. Confirm collection and IN/OUT data

The daemon creates `programdata/display.txt` immediately at startup. It is an
operator feed containing only confirmed IN/OUT and inference state records.
Individual ENV/SOUND events, candidates, ACKs, timeouts, and baselines stay in
memory and do not clutter the operator display or routine audit storage. The
matching legacy-compatible JSON is written under each node's
`inference/debugstr/YYYY-MM-DD.txt`. Normal RAWDATA is written only to the
node's `inference/rawdata/YYYY-MM-DD.txt`. On startup, existing feature-only
lines are removed from the current display and today's text archive.

`SLIMHUB_AUDIT_JSONL=minimal` is the default. It writes malformed/security
errors plus lossless schema-2 NODE/CONFIG/INOUT/EVENT/ADL/SOUND contract
records under `programdata/reports/`. Use `full` only for short raw transport
diagnostics.

Strict JSON `DEBUG` and `INFERENCE` records share the same framed NUS `REPORT`
transport as typed schema-2 records. Strict JSON alone owns the legacy
display/debugstr timeline; typed reports stay in structured diagnostics and
correlation state. The JSON intentionally has no schema/session IDs or node
timestamp. SLIMHUB uses the complete-frame receipt wall clock in
`Asia/Seoul`, applies a 4-second MAC-scoped retry dedupe, and hard-rejects a
JSON/frame MAC mismatch before occupancy, pending-command, or legacy file state
can change. Invalid objects remain diagnostic-only.
Compact typed ADL aliases (`cov/m/dur/rst/seq`) are normalized, and a repeated
numeric `src` metric cannot overwrite the leading `src=ADL` routing field.

If a deployed Node v2 image omits final `src=ADL` reports, Central emits only
the conservative location/event signatures learned from the deployed legacy
history. These records have kind `derived_inference` and
`ground_truth_eligible=false`; they must not be treated as firmware truth.

```bash
tail -F programdata/display.txt
tail -F data/*/*/*/inference/debugstr/$(date +%F).txt
```

The daily, legacy-compatible display archive is
`data/display/YYYY-MM-DD.txt`.

### Optional: capture labeled sound PCM

Sound capture is opt-in and does not change the normal display, RAWDATA, REPORT,
or IN/OUT flow. WAV files exist only on the DEAN Node uSD card. BLE carries the
command and lifecycle/completion REPORTs, never PCM or WAV binary:

```bash
slimhub-v2 sound start --location KITCHEN \
  --label pee --threshold-rms 1200 --max-seconds 90 \
  --silence-seconds 5
slimhub-v2 sound background --location KITCHEN --max-seconds 10
slimhub-v2 sound background --location KITCHEN --max-seconds 300 --no-wait
slimhub-v2 sound automatic --location KITCHEN
slimhub-v2 sound status --location KITCHEN
slimhub-v2 sound stop --location KITCHEN
```

The default wait exits only after a terminal REPORT. A successful
`CAPTURE_DONE` or `CAPTURE_COMPLETE` returns zero; incomplete/cancelled/error/timeout returns
non-zero. `--no-wait` returns after `CAPTURE_ARMED` confirms the cid. Node WAVs are
stored under `/sdcard/SOUND/<label>/<cid>.wav`; there is no Central sound directory
or BLE completeness manifest. On an interactive terminal, omitting both wait
options displays an in-place progress bar and ETA. Background uses `max-seconds`
as its estimate; gated start shows an upper bound that includes the ARM timeout.
Explicit `--wait` and non-interactive pipe/cron runs stay quiet until the final line.
The terminal wait deadline scales for uSD WAV flush/fsync:
`max_seconds + max(180 seconds, max_seconds / 2)`, plus the 120-second ARM window
for gated start. Stop waits up to 1020 seconds and no-wait ARMED confirmation up
to 180 seconds, allowing BLE reconnect and cached terminal REPORT delivery.

## 4. Configure the database cron job

Copy `docs/db.env.example` to `/home/rtlab/.config/slimhub-v2/db.env`, fill it,
and set mode 600. Do not put secrets in the repository or directly in the
crontab. Required local variables are
`SLIMHUB_LOCAL_DB_HOST`, `SLIMHUB_LOCAL_DB_USER`, and
`SLIMHUB_LOCAL_DB_NAME`; `SLIMHUB_LOCAL_DB_PASS` is supported. Configure the
corresponding `SLIMHUB_REMOTE_DB_*` variables only after remote upload is
re-enabled in source. The current local-only branch deliberately returns
`skipped` without opening a remote connection.
`house_mac` defaults to the Hub address in `programdata/config.json`; set
`SLIMHUB_HOUSE_MAC` only when the deployment uses a separate house identifier.

Copy the two short entries in [`slimhub-v2.crontab`](slimhub-v2.crontab) into
`crontab -e`. They call `scripts/db_ingest.sh` every three minutes and
`scripts/db_upload.sh` every ten minutes. The shell wrappers load the protected
environment and serialize coincident runs.
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

The command reports the safe database configuration state, source data-file
offsets, local upload offsets, and the last ingest/upload results. It never
prints passwords. By default the first run starts with today's data files, matching
the legacy cron. Set `SLIMHUB_DB_BACKFILL=1` only for an intentional historical
import.

## 6. Confirm remote upload (after re-enabling it)

The current local-only build records a skipped upload and does not connect to the
remote database. After the guarded remote upload implementation is re-enabled,
`last_upload.result.adl` and
`last_upload.result.inout` show `uploaded` row counts and the local `last_id`.
Each stream offset advances immediately after its own remote transaction
commits, so a later stream failure does not resend committed rows. Verify the
same new rows in the remote `event_adl` and `in_out` tables using the normal DB
operator account.

## 7. Release checklist

- Node is connected and its location configuration is correct.
- `display.txt` shows only expected IN/OUT and inference state records.
- `db status` has a successful recent ingest; upload is either explicitly
  `skipped` in local-only mode or successful after re-enablement.
- Remote table rows have been independently checked when remote upload is enabled.
- Preserve `data/`, `programdata/db_sync/last_ingest.json`,
  `programdata/db_sync/last_upload.json`, and the DB cron logs as release evidence.
