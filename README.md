# SLIMHUB_v2

SLIMHUB_v2는 여러 대의 `DEAN_NODE_V2` BLE 주변기기를 관리하는
NUS 전용 Python daemon입니다. 주요 역할은 BLE 연결 관리, rawdata 로깅,
unitspace 추정, 그리고 `slimhub-v2` CLI 제공입니다.

## 설치

```bash
cd /home/hmkang/SLIMHUB_v2
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

Python 3.11 이상을 지원합니다. 이 프로젝트는 Python 3.11, 3.12, 3.13에서
실행하는 것을 기준으로 합니다.

## 수동 실행

저장소 루트에서 daemon을 시작합니다.

```bash
source .venv/bin/activate
slimhub-v2 run
```

여러 노드를 디버깅할 때는 debug 로그를 켜고 scan 시간을 조금 더 길게 둡니다.

```bash
slimhub-v2 --debug run --scan-timeout 8 --scan-interval 5
```

터미널을 닫아도 daemon을 계속 실행하려면 background mode를 사용합니다.

```bash
slimhub-v2 --debug run --background --scan-timeout 8 --scan-interval 5
tail -f logs/slimhub-v2.out
slimhub-v2 --quit
```

SLIMHUB 호환 flag 스타일도 지원하며, 운영자용 인터페이스로는 이 방식을
권장합니다.

```bash
slimhub-v2 --run
slimhub-v2 --debug --run --scan-timeout 8 --scan-interval 5
```

다른 터미널에서 자주 쓰는 명령입니다.

```bash
slimhub-v2 --list
slimhub-v2 --config AA:BB:CC:DD:EE:FF location ENTRY
slimhub-v2 --apply
slimhub-v2 --service AA:BB:CC:DD:EE:FF enable inference rawdata
slimhub-v2 command send --address AA:BB:CC:DD:EE:FF --command enter
slimhub-v2 raw tail --address AA:BB:CC:DD:EE:FF --lines 20
slimhub-v2 unitspace status
slimhub-v2 power status --address AA:BB:CC:DD:EE:FF
slimhub-v2 battery status --address AA:BB:CC:DD:EE:FF
slimhub-v2 --quit
```

v2 진단용 legacy subcommand도 계속 사용할 수 있습니다.

```bash
slimhub-v2 run
slimhub-v2 devices
slimhub-v2 connect --address AA:BB:CC:DD:EE:FF
slimhub-v2 unitspace status
slimhub-v2 power status
slimhub-v2 battery status
```

daemon은 `programdata/slimhub.sock`에서 요청을 받습니다. Hub 설정은
`programdata/config.json`에 저장됩니다. 장치 설정은
`programdata/config/<MAC>.json` 아래에 저장되며, SLIMHUB 스타일의
`address`, `type`, `name`, `location` 필드를 사용합니다.

Rawdata 로그는
`data/<location>/<type>/<MAC>/inference/rawdata/YYYY-MM-DD.txt`에 누적됩니다.
Alert/debug 텍스트는
`data/<location>/<type>/<MAC>/inference/debugstr/YYYY-MM-DD.txt`에 누적됩니다.
`REPORT src=POWER/INOUT/ENV/USD`와 BLE 연결/해제 event는
`programdata/reports/YYYY-MM-DD.jsonl`에 JSONL로 누적됩니다. 각 record에는
frame MAC, BLE source address, packet receive time, `src`, `event`,
parsed fields, connected state가 포함됩니다. `USD STATUS`의 `batt_mv`,
`batt_v`, `batt_pct`, `batt_rem_mah`, `usb`, `chg`, `sd`, `file`, `uptime`,
`ok`는 top-level field로도 저장됩니다.
Runtime 로그는 `programdata/logging.log`에 기록됩니다.
`logging.log`는 연결/해제, command 전송, warning/error 중심으로 작게
유지합니다. 주기 `REPORT` payload와 BLE notify/debug dump는 이 파일에
쓰지 않으며, report 분석은 `programdata/reports/*.jsonl`을 사용합니다.

여러 `DEAN_NODE_V2` 주변기기는 정규화된 MAC 주소로 관리합니다. BLE 주소와
NUS frame MAC이 다를 경우, SLIMHUB_v2는 frame MAC을 활성 BLE session에
alias로 연결합니다. 그래서 unitspace command가 올바른 주변기기로 계속
전달됩니다.

## 프로토콜

NUS frame은 다음 binary layout을 사용합니다.

```text
[MAC address 6B][Packet Type 8B][Packet Length uint16 LE][Packet Data][End FLAG 0D 0A]
```

Inbound packet type은 다음과 같습니다.

- `RAWDATA`: 33-byte little-endian payload
- `ALERT`: UTF-8 text payload
- `REPORT`: UTF-8 comma-separated key/value payload

### DEAN_Node_v2 PIR+RADAR IN/OUT

현재 DEAN_Node_v2 firmware는 PIR 단독 trigger만으로 RAWDATA unitspace enter를
보내지 않습니다. PIR은 firmware 내부 IN/OUT state machine을 시작하고,
이후 RADAR가 presence를 confirm하면 RAWDATA에서 `flag_human_presence=1`,
`detected=10`을 보냅니다. SLIMHUB_v2는 `detected=10`을 primary
RADAR-confirmed unitspace `enter`로 처리합니다.

`detected=1`은 이전 firmware를 위한 legacy PIR-only enter signal로
유지합니다. `detected=20`은 계속 unitspace `exit` signal입니다.

Firmware는 다음과 같은 IN/OUT report packet도 보냅니다.

```text
src=INOUT,event=ENTER,signal=enter,code=10,pir=1,radar=1,dist_cm=75,state=inside_moving,reason=radar_confirmed
src=INOUT,event=STATE,state=wait_radar,pir=1,radar=0,reason=pir_triggered
src=INOUT,event=STATE,state=outside,pir=0,radar=0,reason=absence
src=USD,event=STATUS,uptime=12345,file=LOG/001.CSV,ok=1,batt_valid=1,batt_v=3.980,batt_mv=3980,batt_pct=75,batt_rem_mah=1125,batt_cap_mah=1500,usb=0,chg=1,sd=0
```

`src=INOUT` report는 runtime log에 기록되며, `power status`에서도 마지막
IN/OUT event/state/code/reason으로 확인할 수 있습니다. ENTER/code `10` 또는
`inside_*` state는 shadow power state를 `RADAR_CONFIRMED_ACTIVE`로
전환합니다.

새 PIR+RADAR flow를 하드웨어에서 확인할 때는 다음 명령을 사용합니다.

```bash
slimhub-v2 --debug --run --scan-timeout 8 --scan-interval 5
slimhub-v2 raw tail --address AA:BB:CC:DD:EE:FF --lines 20
slimhub-v2 unitspace status
slimhub-v2 power status --address AA:BB:CC:DD:EE:FF
slimhub-v2 battery status --address AA:BB:CC:DD:EE:FF
tail -n 50 programdata/logging.log
```

Outbound unitspace command는 NUS RX로 `COMMAND` frame을 보내는 방식입니다.
Frame MAC은 target node MAC이고, payload는 UTF-8 command인 `enter` 또는
`exit`입니다. `strong_enter`, `weak_enter`, `strong_exit`, `weak_exit` 같은
이전 운영자 표현은 frame 생성 전에 정규화되므로, NUS에는 비표준 command
payload가 기록되지 않습니다.

## Shadow Power State

SLIMHUB_v2는 각 DEAN node에 대해 RPI5 측 shadow power-state simulation을
유지합니다. 이 상태는 RAWDATA human-presence 필드, PIR/RADAR/MIC token에
대한 보수적인 ALERT/debug parsing, local `enter`/`exit` command hint,
BLE 연결/해제 timestamp를 사용합니다. 이 기능은 logging과 visibility 용도일
뿐이며, ESP32로 새로운 power-control command를 보내지 않습니다.

Shadow transition은 `programdata/power_shadow.log`에 JSON lines 형식으로
누적됩니다. 현재 상태는 다음 명령으로 조회할 수 있습니다.

```bash
slimhub-v2 power status
slimhub-v2 power status --address AA:BB:CC:DD:EE:FF
```

현재 NUS payload가 항상 MIC RMS나 RADAR distance를 노출하지는 않을 수
있습니다. 이후 더 정확한 simulation이 필요하면 DEAN Node ALERT text에
`RADAR presence active: dist_cm=<number>`와
`MIC activity active: rms=<number>` 같은 안정적인 필드를 포함시키는 것이
좋습니다.

## 호환 Reader

daemon을 실행하지 않고 빠르게 하드웨어를 확인할 때 사용합니다.

```bash
python ble_nus_central.py --name DEAN_NODE_V2 --debug
```

## 테스트

```bash
python -m compileall slimhub tests
python -m unittest
```
