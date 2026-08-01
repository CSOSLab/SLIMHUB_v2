# Dynamic sound catalog v2

SLIMHUB treats each valid `src=SOUND,event=INFERENCE,schema=2` REPORT as the
authoritative window decision from a DEAN Node. The tuple
`(node MAC, boot ID, model short SHA, location)` identifies a catalog. Labels
are stored exactly as reported; SLIMHUB does not translate `class_index` through
a global label table.

The required REPORT fields are `schema`, `bid`, `location`, `class_index`,
`class_count`, `label`, `semantic`, `confidence`, `model`, `source`, `ts`, and
`duration_ms`. `rms` and `db` are optional finite numbers. Validation accepts
2–20 classes, checks the index and confidence ranges, and accepts only `tflm`
or `rms_gate` sources. Index/background inconsistencies are retained with a
protocol diagnostic. Malformed decisions are rejected without stopping the
daemon, and their MAC, raw payload, and reason remain in the diagnostic log.

## Storage and migration

The daemon creates `programdata/sound_inference.sqlite3`. Its versioned,
append-only objects are:

- `sound_inference_v2`: lossless inference records and ADL eligibility
- `sound_catalog_v2`: observed index/label/count values per catalog key
- `sound_session_v2`: linked NODE/CONFIG metadata for that catalog session
- `sound_diagnostic_v2`: rejects and consistency diagnostics
- `sound_store_meta`: schema version

Startup migration runs in one SQLite transaction, creates only missing v2
objects, and never drops or rewrites existing tables. A failed migration rolls
back all newly created objects. Replaying the same report is idempotent.

`NODE/STATUS` and `CONFIG/APPLIED|REJECTED` metadata is compared with each
inference. Disabled semantic output, rejected/not-ready configuration, or a
location/model/class-count mismatch is stored as a diagnostic and makes the
record ineligible for ADL semantic aggregation. A Node-supplied
`semantic=unknown` and its exact future label are still stored, but also remain
ineligible.

## Legacy coexistence

The 33-byte RAWDATA packet remains telemetry and never creates a v2 inference.
Slots from `raw=2` use the common `home_semantic_v1` order; declared
toilet/kitchen/living profile tensors are mapped by semantic label. Both paths
write the same 24-column union header for every accepted room, with unavailable
labels as `0.0`; reserved raw slots are ignored. A 17–20 class REPORT remains
complete without an adjacent RAWDATA packet. `gas_oven` is not in the deployed
catalog and must not be inferred by renaming `cooking`.

`EVENT/SOUND` is a Node-extracted semantic run and stays on the multimodal path.
It is not merged with, or counted as, the window-level `SOUND/INFERENCE`.

Operators can inspect all observed Nodes or one target:

```text
slimhub-v2 sound catalog
slimhub-v2 sound catalog --location TOILET
```

The current TOILET fixture uses model short SHA `0cb81518`, derived from
`0cb815187f77c155c602bb6394e686947478a1e48a839b0296418278b7765568`.
