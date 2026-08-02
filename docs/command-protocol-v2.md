# DEAN Node v2 command protocol

This integration follows the DEAN Node v2 home-wide token demo contract.
SLIMHUB is the only occupancy authority.

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

Node notifications may be fragmented, but the Node RX command parser does not
accumulate separate GATT writes. Central therefore checks the negotiated
write-without-response capacity and sends each complete `inout_confirm` frame
as one GATT value. The 55-byte example payload produces a 73-byte frame and
requires an ATT MTU of at least 76 bytes.

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

PIR RAWDATA (`detected=10|20`) is candidate evidence and never changes Node
occupancy by itself. The typed ENTER/EXIT sidecar supplies the candidate
identity used by Central:

```text
REPORT boot_id -> COMMAND bid
REPORT event_seq -> COMMAND cid
REPORT signal=enter|exit -> COMMAND state=in|out
inout_confirm,bid=<hex>,cid=<decimal>,state=in|out,rid=<nonzero-hex>
```

Results use:

```text
src=INOUT,event=CONFIRM_ACK|CONFIRM_ERROR,schema=2,bid=<hex>,cid=<decimal>,
rid=<hex>,state=in|out,source=slimhub,applied=0|1,changed=0|1,
reason=...,legacy=0
```

Transactions and result dedupe use `(source MAC,bid,cid,rid,target state)`.
Both exact `CONFIRM_ACK,applied=1,changed=1,reason=applied,legacy=0` and
`CONFIRM_ACK,applied=1,changed=0,reason=already_applied,legacy=0` are
authoritative. After a successful GATT write, an ACK timeout retries the exact
same bid/cid/rid/state identity. It never allocates a new rid merely because
the ACK was lost. A transport write retry also preserves that identity.
`CONFIRM_ERROR` is terminal.
Reusing a successful rid for a different bid/cid/state returns terminal
`request_id_conflict`.
`enter` and `inout_sync` are rejected. `exit` is reserved for authoritative
multi-node handoff: when A owns the home token and B supplies a typed ENTER
candidate, Central sends `exit` to A without inventing a candidate-correlated
OUT transaction. A responds with `rid=00000000,state=out,legacy=1`.

For `changed=1,reason=applied`, Central waits for both A's typed
`EXIT_SYNC/D1` boundary and the durable strict DEBUG EXIT display/debugstr
write before sending B's correlated `inout_confirm`. For
`changed=0,reason=already_applied`, A is already OUT and Central proceeds
without waiting for legacy replay. A failure or timeout never promotes B
first.

## Node configuration

Supported commands are:

```text
node_status
config_get
config_set,location=<LOCATION>
config_set,location=<LOCATION>,sound_profile=<PROFILE>
config_reload
```

Allowed pairs are TOILET/toilet_v1 (10 classes), KITCHEN/kitchen_v1 (9),
LIVING/living_v1 (5), and BEDROOM/living_v1 (5). Location-only lets the Node derive
the profile; the explicit profile form remains compatible. `config_set` and
`config_reload` require cached
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
