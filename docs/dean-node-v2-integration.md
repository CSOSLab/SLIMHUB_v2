# DEAN Node v2 integration operations

Production setup expects the home-wide token demo firmware with
`config=READY` and `semantic=1`. SLIMHUB owns occupancy authority.

## Inspect and configure a Node

```bash
slimhub-v2 node status --address AA:BB:CC:DD:EE:FF
slimhub-v2 node config get --address AA:BB:CC:DD:EE:FF
slimhub-v2 node config set --address AA:BB:CC:DD:EE:FF \
  --node-location KITCHEN
slimhub-v2 node config reload --address AA:BB:CC:DD:EE:FF
```

Set/reload is rejected unless the last NODE/STATUS says occupancy OUT and
capture IDLE. The set response is pending; verify CONFIG/APPLIED before using
the new cached profile.

On each connection SLIMHUB compares the normalized MAC's local JSON location
after fresh NODE/STATUS and CONFIG/STATUS reports. A valid production room is
applied once with `config_set,location=<LOCATION>` when the Node is OUT+IDLE.
IN/active capture defers the change; unknown/undefined local locations never
overwrite Node persistence. CONFIG/REJECTED or command errors are terminal for
that connection and remain in the diagnostic JSONL.

## Automatic capture

```bash
# Use Node-stored 57/52 dB thresholds.
slimhub-v2 sound automatic --location KITCHEN

# Explicit pair override.
slimhub-v2 sound automatic --location KITCHEN \
  --open-db 60 --close-db 55 --max-seconds 300 --silence-seconds 20
```

Supplying only one threshold is a validation error. The CLI can wait beyond
300 seconds for segmented capture, uSD finalization, reconnect, and
CAPTURE_COMPLETE. `sound status` and `sound stop` remain available.

## Location sound classes

| Index | TOILET | KITCHEN | ENTRY/LIVING/BEDROOM |
|---:|---|---|---|
| 0 | background | background | background |
| 1 | hitting | hitting | hitting |
| 2 | speech_tv | speech_tv | speech_tv |
| 3 | air_appliances | air_appliances | air_appliances |
| 4 | brushing | cooking | snoring |
| 5 | peeing | microwave | — |
| 6 | flushing | watering_low | — |
| 7 | flushing_end | watering_high | — |
| 8 | watering_low | appliances | — |
| 9 | watering_high | — | — |

The table above applies to typed model inference metadata. The 33-byte
RAWDATA packet has a separate fixed 16-slot wire mapping that never changes by
location/model: `background,hitting,speech_tv,air_appliances,brushing,peeing,`
`flushing,flushing_end,watering_low,watering_high,cooking,microwave,appliances,`
`snoring,gas_oven,reserved`. Every accepted room writes the same 26-column raw
CSV and maps all 16 slots directly.

Strict legacy JSON `REPORT` objects use `type=DEBUG` or `type=INFERENCE` and
are the only source for `display/YYYY-MM-DD.txt` and per-node `debugstr`.
Typed schema-2 reports remain structured/correlation evidence and never add a
second legacy line.

## Field gate before production

With two physical Nodes, verify:

- command frame target MAC equals each Node's actual source MAC;
- identical bid/cid/rid values on two Nodes do not collide;
- PIR-only `detected=10` makes its source active without an ENTER echo;
- a later Node's `detected=10` sends only `exit` to the previous active Node;
- PIR-only `detected=20` clears its Node without an EXIT echo;
- each confirmation maps candidate `boot_id/event_seq/signal` to
  `bid/cid/state`;
- a new Node ENTER candidate sends `exit` to the previous occupied Node;
- the previous Node's `legacy=1` ACK, `EXIT_SYNC/D1`, and committed strict
  DEBUG EXIT complete before the queued new Node IN confirmation;
- reused rid and `CONFIRM_ERROR` never trigger an automatic retry;
- D0/D1 and EVENT/ADL reports arrive without feedback loops;
- KITCHEN index 4 resolves to cooking while TOILET index 4 resolves to brushing;
- automatic capture arms, segments, completes, and stops;
- BLE remains connected for at least 30 minutes.

The automated suite covers mock transport and correlation. These RF, sensor,
uSD, and duration checks require real hardware.
