# DEAN Node v2 command protocol

This integration follows DEAN Node v2 firmware contract commit
`66c6b45bcc2dda40dd1046819aae717c8acbbf28`. Production Nodes use
`authority=slimhub_confirmed`; `local_standalone` is a test-only profile.

## NUS framing

Every frame is:

```text
[target/source MAC:6][type ASCII padded to 8][length:u16le][payload][0d 0a]
```

COMMAND payloads are 1–128 bytes, REPORT payloads are at most 256 bytes, and
RAWDATA is exactly 33 bytes. The connection-local stream assembler uses the
declared length and CRLF, not ATT notification boundaries. A notification may
contain a partial frame or multiple frames. Invalid type/length/CRLF, embedded
NUL REPORT text, and malformed UTF-8 are rejected without clearing another
Node's state.

## Connection initialization

After notification subscription succeeds, Central queues exactly this order
for every new BLE connection session:

```text
time_sync,epoch_ms=<unix-ms>,tz_min=<offset-minutes>
node_status
config_get
```

Reconnect creates a new session and repeats this sequence. NODE/STATUS is
cached by source MAC in `programdata/dean_node_state.json`. A new `bid`
expires pending confirmation transactions for the previous boot.

## Occupancy authority

RAWDATA direction values (`detected=10` ENTER, `detected=20` EXIT) are
diagnostic candidates only because the 33-byte payload has no `bid` or `cid`.
Central waits for the matching typed REPORT:

```text
src=INOUT,event=ENTER|EXIT,schema=2,
boot_id=<hex>,event_seq=<decimal>,event_ts_ms=<decimal>,...
```

The parser preserves original fields and also normalizes:

- `boot_id` or `bid` to `bid`
- `event_seq` or `cid` to `cid`
- `event_ts_ms` or `ts` to `timestamp`

An approved production candidate is confirmed using the frame's actual
source MAC:

```text
inout_confirm,bid=<hex>,cid=<decimal>,state=in|out,rid=<nonzero-hex>
```

Transactions are keyed by `(MAC,bid,cid,rid)`. Request IDs are nonzero,
32-bit, unique per Node process lifetime, and a retry gets a new ID.
Only `CONFIRM_ACK,source=slimhub,applied=1` with the exact key and state changes
authoritative cached occupancy. `stale_boot`, `stale_candidate`,
`state_mismatch`, `duplicate_request`, and `no_pending_candidate` remain
diagnostic outcomes. `no_pending_candidate` permits one retry with a new rid
inside the 45-second feedback window.

For `authority=local_standalone`, Central never sends `inout_confirm` or legacy
`enter`/`exit`. It only observes `source=local,applied=1`.

Legacy `enter`/`exit` remains available for Nodes that have not advertised the
schema-2 authority contract. It is not used for unsolicited synchronization
once a Node is known to be schema 2.

## Node configuration

Supported commands are:

```text
node_status
config_get
config_set,location=<LOCATION>,sound_profile=<PROFILE>
config_reload
```

Allowed pairs are TOILET/toilet_v1 (10 classes),
KITCHEN/kitchen_v1 (9), LIVING/living_v1 (5), and
BEDROOM/living_v1 (5). `config_set` and `config_reload` require cached
occupancy OUT and capture IDLE. Cached configuration changes only after
CONFIG/APPLIED; CONFIG/REJECTED preserves the old values and reason.

If `semantic=0` or config is not READY, status, IN/OUT, and sound capture
remain available, but Central does not invent semantic sound labels.

## Sound capture

Named and background capture remain compatible. Automatic capture defaults to:

```text
sound_auto,max=300,silence=20
```

This intentionally omits thresholds and uses the Node's stored 57/52 dB
values. Overrides must be supplied as a pair:

```text
sound_auto,max=300,silence=20,open_db=60,close_db=55
```

CAPTURE_ARMED is authoritative for `cid`, label, mode, threshold_rms,
open_db, close_db, max_ms, and silence_ms. Capture state is keyed by
`(MAC,cid)`. A SOUND COMMAND_ERROR is attached only when its `command` matches
the pending request. Both CAPTURE_DONE and CAPTURE_COMPLETE are terminal
success events; segmented captures and reconnects do not change the key.

## Typed report retention and dedupe

Schema-2 NODE, CONFIG, INOUT, EVENT, ADL, and SOUND fields are retained
losslessly in the JSONL audit stream, including unknown future fields.
Occupancy uses `(MAC,bid,cid)`, confirmation uses `(MAC,bid,cid,rid)`, and
EVENT/SOUND uses `(MAC,boot_id,session_seq,event_ts_ms,class_index)` for dedupe.
Firmware-extracted EVENT/ADL results are never fed back into the estimator.
