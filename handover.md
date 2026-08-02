# SLIMHUB_v2 handover

## Current repository state

- Active branch: `integration/dean-node-v2-contract-20260728`
- Working tree: clean at handover time
- Latest integration: candidate-correlated DEAN Node v2 `inout_confirm`
- Baseline snapshot commit on `develop`: `3b49077 Integrate DEAN node sound recording support`

Recent integration commits:

1. `4cf26a5 Integrate INOUT event sequence reconciliation`
2. `a31f426 Add multimodal EVENT and ADL report ingestion`
3. DEAN Node v2 `inout_confirm` contract alignment

## Implemented Central behavior

### Home-wide IN/OUT token
- PIR RAWDATA `detected=10|20` is evidence only; typed ENTER/EXIT owns the
  candidate identity.
- SLIMHUB maps `boot_id/event_seq/signal` to `bid/cid/state` and sends
  `inout_confirm` in one GATT write.
- Results correlate by `(source MAC,bid,cid,rid,target state)`. Both
  `changed=1,reason=applied` and `changed=0,reason=already_applied` ACKs are
  authoritative.
- ACK timeout after a successful write retries the exact same identity and
  rid. Transport write retry also preserves it; `CONFIRM_ERROR` and
  `request_id_conflict` are terminal.
- When A owns the token and B provides a typed ENTER candidate, Central sends
  `exit` to A. A's `legacy=1` ACK, `EXIT_SYNC/D1`, and committed strict DEBUG
  EXIT gate B's correlated `inout_confirm`; `changed=0` skips the legacy
  replay barrier.
- A handoff failure preserves the old authoritative state or enters explicit
  reconciliation; B is never promoted before A's successful EXIT.
- D0/D1 sequence reports are never fed back into token assignment.
- Node uptime is normalized per `(MAC, boot_id)` and INOUT/EVENT/ADL reports use a 1.5-second reorder buffer.

### Multimodal EVENT / ADL reports

- `src=EVENT` (`BASELINE`, `ENV`, `SOUND`) and `src=ADL` (`PREDETECT`, `COMPLETE`, `PARTIAL`, `NO_MATCH`) are parsed into typed JSONL records by `slimhub/multimodal.py`.
- Replay dedupe key: `(MAC, boot_id, analysis_seq)`; gaps in `analysis_seq` are allowed.
- BASELINE snapshots upsert by `(MAC, boot_id, ready_mask)` and retain newer counts.
- ENV IDs map to `E0`–`E4`; SOUND uses the canonical 10-class mapping and validates `class_count=10`.
- D0/D1 boundaries are linked to `session_seq` data by MAC, boot ID, and normalized event time. D1's `event_seq` is not assumed to equal `session_seq`.
- `PREDETECT` is provisional. `overflow=1` final ADL results are not ground-truth eligible.
- No multimodal report is reused as estimator input.

### NUS transport and logging

- `FrameAssembler` reassembles frames across arbitrary BLE notification boundaries, including header/payload/CRLF splits.
- Invalid packet types, length values, and CRLF are boundedly discarded while resynchronizing.
- JSONL records under `programdata/reports/YYYY-MM-DD.jsonl` include raw/report/command/ACK lifecycle data, packet fields, clock metadata, and typed multimodal records.
- B TFLM sound schema is fixed to 10 scores. Padding after score 10 is not dequantized; index 7 is `flushing_end`, 8/9 are `watering_low`/`watering_high`.

## Important files

- `slimhub/unitspace/estimator.py` — IN/OUT candidate, ACK, D0/D1 handling
- `slimhub/unitspace/clock.py` — boot-uptime clock normalization and reorder buffer
- `slimhub/multimodal.py` — EVENT/ADL typed records, dedupe, session grouping
- `slimhub/daemon.py` — report routing, logging, status endpoint
- `slimhub/protocol/nus.py` — NUS frame assembler/parser
- `slimhub/logging/sound_schema.py` — canonical B TFLM labels
- `docs/command-protocol-v2.md` — planned command envelope/ACK revision
- `docs/deployment-manifest.example.json` — per-node field deployment manifest template

## Verification

Run from repository root:

```bash
.venv/bin/python -m compileall -q slimhub tests
.venv/bin/python -m unittest
```

Last verification at handover: **80 tests passed**.

Replay fixtures:

- `tests/fixtures/two_node_inout_replay.jsonl`
- `tests/fixtures/multimodal_adl_replay.jsonl`

## Remaining field work

1. Test with at least two physical DEAN Node v2 devices and retain their JSONL deployment logs.
2. Create `programdata/deployment_manifest.json` from the template for every deployed MAC, using the fixed location profile and the intended private 10-class model hash.
3. Verify firmware emits `EVENT/BASELINE` after subscription/reconnect and `EVENT/SOUND` with `schema=1,class_count=10`.
4. Verify the deployed demo firmware rejects `enter`/`inout_sync`, accepts
   handoff `exit` with a `rid=00000000,legacy=1` ACK, and accepts correlated
   `inout_confirm` for the new occupant.

No firmware source tree is present in this repository, so firmware-side changes and real BLE deployment capture were not performed here.
