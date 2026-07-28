# DEAN Node v2 integration operations

Production setup expects at least one Node reporting
`authority=slimhub_confirmed`, `config=READY`, and `semantic=1`.

## Inspect and configure a Node

```bash
slimhub-v2 node status --address AA:BB:CC:DD:EE:FF
slimhub-v2 node config get --address AA:BB:CC:DD:EE:FF
slimhub-v2 node config set --address AA:BB:CC:DD:EE:FF \
  --node-location KITCHEN --profile kitchen_v1
slimhub-v2 node config reload --address AA:BB:CC:DD:EE:FF
```

Set/reload is rejected unless the last NODE/STATUS says occupancy OUT and
capture IDLE. The set response is pending; verify CONFIG/APPLIED before using
the new cached profile.

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

| Index | TOILET | KITCHEN | LIVING/BEDROOM |
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

Never interpret an index without the same Node's profile/location,
class_count, semantic flag, and model identifier. The legacy CSV keeps its
fixed toilet-v1 columns; dynamic raw scores and metadata are written to
`programdata/reports/*.jsonl`.

## Field gate before production

With two physical Nodes, verify:

- command frame target MAC equals each Node's actual source MAC;
- identical bid/cid values on two Nodes do not collide;
- PIR+RADAR ENTER and EXIT candidates require SLIMHUB confirmation;
- D0/D1 and EVENT/ADL reports arrive without feedback loops;
- KITCHEN index 4 resolves to cooking while TOILET index 4 resolves to brushing;
- automatic capture arms, segments, completes, and stops;
- BLE remains connected for at least 30 minutes.

The automated suite covers mock transport and correlation. These RF, sensor,
uSD, and duration checks require real hardware.
