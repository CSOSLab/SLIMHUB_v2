# Command protocol revision 2

The deployed DEAN Node v2 firmware still accepts the v1 `COMMAND` payloads
`enter` and `exit`. SLIMHUB therefore continues to transmit exactly those
strings until every node advertises `command_protocol=v2`.

For v2-capable nodes, the `COMMAND` payload is UTF-8 key/value text:

```text
cmd_id=<central-id>,desired_epoch=<uint64>,target_mac=<frame-mac>,ble_alias=<central-ble-address>,action=enter
```

The node must reply with an INOUT `EVENT` report such as:

```text
src=INOUT,event=EVENT,id=C0,cmd_id=<central-id>,applied_state=1,target_match=1,source_count=12
```

`id=C0` acknowledges `enter`; `id=C1` acknowledges `exit`. A no-op command
still emits C0/C1 but does not emit D0/D1. D0/D1 remain sequence transition
records and are not estimator input.

Canonical node identity is the pair `(target_mac, BLE alias)`. During the
rollout the firmware must report `target_match=0` rather than strictly
rejecting a legacy alias mismatch. Strict rejection may be enabled only after
the integration test has observed both the NUS frame MAC and the active BLE
alias mapping for each deployed node.

Central commands are idempotent by `cmd_id` and `desired_epoch`. While a node
is offline, Central retains only its most recent desired state. A successful
BLE write never confirms occupancy: it remains pending until C0/C1 or a
reconnect `STATE occupied=<0|1>` reconciliation report.
