# SLIMHUB_v2

SLIMHUB_v2 is a NUS-only Python daemon for managing multiple `DEAN_NODE_V2`
BLE peripherals. It focuses on four core jobs: BLE connection management,
rawdata logging, unitspace estimation, and the `slimhub` CLI.

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

Useful commands from another terminal:

```bash
slimhub devices
slimhub connect --address AA:BB:CC:DD:EE:FF
slimhub config set AA:BB:CC:DD:EE:FF location ENTRY
slimhub raw tail --address AA:BB:CC:DD:EE:FF --lines 20
slimhub unitspace status
slimhub stop
```

If an old shell alias still points `slimhub` at `/home/hmkang/SLIMHUB/main.py`,
either remove/comment that alias from `~/.bashrc` and run `hash -r`, or use the
collision-free console script:

```bash
slimhub-v2 run
slimhub-v2 devices
```

The daemon listens on `programdata/slimhub.sock`. Device config is stored under
`programdata/config/<MAC>.json`; rawdata logs are appended to
`data/<location>/<MAC>/rawdata/YYYY-MM-DD.csv`.

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
is the target node MAC and the payload is a UTF-8 command such as `enter` or
`exit`.

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
