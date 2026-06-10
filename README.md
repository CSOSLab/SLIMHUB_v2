# SLIMHUB_v2

SLIMHUB_v2 is a NUS-only Python daemon for managing multiple `DEAN_NODE_V2`
BLE peripherals. It focuses on four core jobs: BLE connection management,
rawdata logging, unitspace estimation, and the `slimhub-v2` CLI.

## Setup

```bash
cd /home/hmkang/SLIMHUB_v2
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

Supported Python versions are 3.11 or newer. The project is intended to run on
Python 3.11, 3.12, and 3.13.

## Run Manually

Start the daemon from the repository root:

```bash
source .venv/bin/activate
slimhub-v2 run
```

For multi-node debugging, enable logs and give scanning a little more time:

```bash
slimhub-v2 --debug run --scan-timeout 8 --scan-interval 5
```

SLIMHUB-compatible flag style is also supported and is the preferred operator
interface:

```bash
slimhub-v2 --run
slimhub-v2 --debug --run --scan-timeout 8 --scan-interval 5
```

Useful commands from another terminal:

```bash
slimhub-v2 --list
slimhub-v2 --config AA:BB:CC:DD:EE:FF location ENTRY
slimhub-v2 --apply
slimhub-v2 --service AA:BB:CC:DD:EE:FF enable inference rawdata
slimhub-v2 command send --address AA:BB:CC:DD:EE:FF --command enter
slimhub-v2 raw tail --address AA:BB:CC:DD:EE:FF --lines 20
slimhub-v2 --quit
```

The legacy subcommands remain available for v2-specific diagnostics:

```bash
slimhub-v2 run
slimhub-v2 devices
slimhub-v2 connect --address AA:BB:CC:DD:EE:FF
slimhub-v2 unitspace status
slimhub-v2 power status
```

The daemon listens on `programdata/slimhub.sock`. Hub config is stored at
`programdata/config.json`. Device config is stored under
`programdata/config/<MAC>.json` using SLIMHUB-style `address`, `type`, `name`,
and `location` fields.

Rawdata logs are appended to
`data/<location>/<type>/<MAC>/inference/rawdata/YYYY-MM-DD.txt`.
Alert/debug text is appended to
`data/<location>/<type>/<MAC>/inference/debugstr/YYYY-MM-DD.txt`.
Runtime logs go to `programdata/logging.log`.

Multiple `DEAN_NODE_V2` peripherals are managed by normalized MAC address. If
the BLE address and NUS frame MAC differ, SLIMHUB_v2 aliases the frame MAC to the
active BLE session so unitspace commands still route to the correct peripheral.

## Protocol

NUS frames use this binary layout:

```text
[MAC address 6B][Packet Type 8B][Packet Length uint16 LE][Packet Data][End FLAG 0D 0A]
```

Inbound packet types:

- `RAWDATA`: 33-byte little-endian payload
- `ALERT`: UTF-8 text payload

Outbound unitspace commands are sent as `COMMAND` frames to NUS RX. The frame MAC
is the target node MAC and the payload is a UTF-8 command: `enter` or `exit`.
Older operator vocabulary such as `strong_enter`, `weak_enter`, `strong_exit`,
and `weak_exit` is normalized before frame construction, so nonstandard command
payloads are not written over NUS.

## Shadow Power State

SLIMHUB_v2 keeps an RPI5-side shadow power-state simulation for each DEAN node.
It uses RAWDATA human-presence fields, conservative ALERT/debug parsing for
PIR/RADAR/MIC tokens, local `enter`/`exit` command hints, and BLE
connection/disconnection timestamps. This is logging and visibility only; it
does not send new power-control commands to the ESP32.

Shadow transitions are appended to `programdata/power_shadow.log` as JSON lines.
Current state can be queried with:

```bash
slimhub-v2 power status
slimhub-v2 power status --address AA:BB:CC:DD:EE:FF
```

The current NUS payloads may not always expose MIC RMS or RADAR distance. For
more accurate simulation later, DEAN Node ALERT text should include stable
fields such as `RADAR presence active: dist_cm=<number>` and
`MIC activity active: rms=<number>`.

## Compatibility Reader

For quick hardware checks without running the daemon:

```bash
python ble_nus_central.py --name DEAN_NODE_V2 --debug
```

## Test

```bash
python -m compileall slimhub tests
python -m unittest
```
