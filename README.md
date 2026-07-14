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
모든 `RAWDATA`, `REPORT`, command lifecycle과 BLE 연결/해제 event는
`programdata/reports/YYYY-MM-DD.jsonl`에 JSONL로 누적됩니다. 각 record에는
frame MAC, BLE source address, packet receive time, `src`, `event`,
parsed fields, connected state가 포함됩니다. `USD STATUS`의 `batt_mv`,
`batt_v`, `batt_pct`, `batt_rem_mah`, `usb`, `chg`, `sd`, `file`, `uptime`,
`ok`는 top-level field로도 저장됩니다.
Runtime 로그는 `programdata/logging.log`에 기록됩니다.
`logging.log`는 연결/해제, command 전송, warning/error 중심으로 작게
유지합니다. 주기 `REPORT` payload와 BLE notify/debug dump는 이 파일에
쓰지 않으며, report 분석은 `programdata/reports/*.jsonl`을 사용합니다.

운영자용 display는 daemon이 필요한 IN/OUT, ENV/SOUND, ADL, BASELINE event만
`programdata/display.txt`에 append합니다. 날짜별 호환 archive는
`data/display/YYYY-MM-DD.txt`에도 같은 내용으로 남습니다. 이 파일은 사람이
빠르게 보는 보조 출력이며, 정식 원본은 `programdata/reports/*.jsonl`입니다.

## DB 증분 적재와 cron

기존 SLIMHUB의 CSV/debugstr 파일 파싱 대신, v2는 append-only
`programdata/reports/*.jsonl`을 byte offset 기준으로 증분 처리합니다. RAWDATA 중
`flag_human_presence=1`은 로컬 MySQL `in_out`에, final ADL 결과는 `event_adl`에
적재합니다. 이어서 local table의 `id` offset을 기준으로 동일 schema의 원격
table에 전송합니다.

DB 자격 증명은 저장소나 crontab에 넣지 말고 실행 환경에서 주입합니다.

```bash
export SLIMHUB_LOCAL_DB_HOST=localhost
export SLIMHUB_LOCAL_DB_PORT=3306
export SLIMHUB_LOCAL_DB_USER='...'
export SLIMHUB_LOCAL_DB_PASS='...'
export SLIMHUB_LOCAL_DB_NAME=adl_event

# 원격 upload를 사용할 때만 설정합니다.
export SLIMHUB_REMOTE_DB_HOST='...'
export SLIMHUB_REMOTE_DB_PORT=3306
export SLIMHUB_REMOTE_DB_USER='...'
export SLIMHUB_REMOTE_DB_PASS='...'
export SLIMHUB_REMOTE_DB_NAME=adl_raw

slimhub-v2 db update            # local ingest + configured remote upload
slimhub-v2 db update --no-upload
slimhub-v2 db ingest
slimhub-v2 db upload
```

테이블 이름은 필요하면 `SLIMHUB_DB_ADL_TABLE`(기본 `event_adl`)과
`SLIMHUB_DB_INOUT_TABLE`(기본 `in_out`)로 바꿀 수 있습니다. ingest/upload
offset은 `programdata/db_sync/`에 보관됩니다. cron 예시는
[`docs/slimhub-v2.crontab`](docs/slimhub-v2.crontab)에 있으며, 실제 설치 전에는
해당 환경변수가 cron에서도 안전하게 제공되는지 확인해야 합니다.
전체 설치·display 확인·cron 반영·local/remote DB 검증·release 절차는
[`docs/operations-v2.md`](docs/operations-v2.md)에 정리돼 있습니다.

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

BLE notification 경계는 NUS frame 경계가 아닙니다. Central은 connection별
byte accumulator에서 16-byte header의 little-endian payload length와 뒤따르는
CRLF를 모두 확인한 뒤에만 frame을 파싱합니다. 잘못된 length, packet type,
CRLF는 bounded resynchronization으로 폐기하므로 MTU 23/247의 header/payload/
CRLF 분할과 연결된 frame stream도 처리합니다.

### DEAN_Node_v2 PIR+RADAR IN/OUT

현재 A(NCS) firmware에서 RAWDATA `flag_human_presence=1, detected=10`은
PIR+RADAR가 확인한 **preliminary ENTER candidate**입니다. Central은 해당
후보에 대해 원하는 node state를 명령하지만, BLE write만으로 점유를 확정하지
않습니다. 같은 node의 `ENTER,code=10` REPORT는 RAW10 metadata sidecar이므로
두 packet은 한 후보로 coalesce됩니다.

`detected=1`은 legacy PIR-only low-confidence evidence입니다. flood가 나도
strong occupancy transition이나 `enter` command를 만들지 않습니다. 점유
전이는 `EVENT id=C0/C1` command application ACK 및 실제 전이가 발생한
`SEQUENCE ... event_id=D0/D1`으로 확인·기록합니다. `SEQUENCE` REPORT는
feedback loop를 막기 위해 estimator 후보로 다시 입력하지 않습니다.

Firmware는 다음과 같은 IN/OUT report packet도 보냅니다.

```text
src=INOUT,event=ENTER,signal=enter,code=10,boot_id=12ab34cd,event_seq=41,event_ts_ms=123456
src=INOUT,event=EVENT,id=C0,boot_id=12ab34cd,primary_seq=41,occupied=1,target_match=1
src=INOUT,event=SEQUENCE,result=ENTER_CONFIRMED,event_id=D0,boot_id=12ab34cd,event_seq=41,event_ts_ms=124000
src=INOUT,event=STATE,occupied=1,boot_id=12ab34cd,event_ts_ms=124100
src=USD,event=STATUS,uptime=12345,file=LOG/001.CSV,ok=1,batt_valid=1,batt_v=3.980,batt_mv=3980,batt_pct=75,batt_rem_mah=1125,batt_cap_mah=1500,usb=0,chg=1,sd=0
```

모든 RAW/REPORT/command/ACK/candidate record는 `programdata/reports/*.jsonl`에
append-only로 저장합니다. 이 record에는 frame MAC, BLE alias, boot/sequence
ID, 원본 payload hex, Central receipt time, node uptime 보정 offset/오차,
BLE session 및 estimator before/after state가 포함됩니다. 서로 다른 node의
`event_ts_ms`는 직접 비교하지 않고 `(MAC, boot_id)`별 clock offset으로
정규화합니다.

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
현재 배포 firmware와의 호환을 위해 frame MAC은 target node MAC이고 payload는
여전히 UTF-8 `enter`/`exit`입니다. Central은 노드별 최종 desired state만
보관해 reconnect FIFO 재생을 방지하며, C0/C1 ACK 또는 reconnect `STATE`로
수렴합니다. 다음 revision의 `cmd_id`, desired epoch, canonical node ID/alias
계약은 [docs/command-protocol-v2.md](docs/command-protocol-v2.md)에 정리돼
있습니다.

## Multimodal EVENT / ADL reports

`src=EVENT`의 `BASELINE`, `ENV`, `SOUND`와 `src=ADL`의 `PREDETECT`,
`COMPLETE`, `PARTIAL`, `NO_MATCH`는 `programdata/reports/*.jsonl`에 typed
record로 추가 저장됩니다. 이 stream은 IN/OUT estimator에 절대 재입력하지
않습니다. `analysis_seq`는 `(MAC, boot_id, analysis_seq)` replay dedupe key이며
gap은 허용됩니다. EVENT history는 wrap-aware `event_ts_ms`, 동일 시각에서는
`analysis_seq`로 정렬합니다.

`D0`는 session을 열고 `D1`는 닫지만 D1의 `event_seq`는 ADL의 `session_seq`와
같다고 가정하지 않습니다. Central은 같은 MAC/boot의 normalized time boundary와
`session_seq`로 ENV/SOUND, PREDETECT, final ADL record를 연결합니다. PREDETECT와
`overflow=1` final record는 ground-truth 집계 대상이 아닙니다. trace report가
없다고 해서 즉시 `NO_MATCH`로 판단하지 않습니다.

`EVENT/BASELINE`은 `(MAC, boot_id, ready_mask)`를 idempotent upsert하며, 재연결
subscription snapshot의 더 최신 channel count를 보존합니다. `configured_profile`
은 `programdata/deployment_manifest.json`의 fixed image 정보 및 node location과
검증됩니다. template은 [deployment-manifest.example.json](docs/deployment-manifest.example.json)에
있습니다. ADL report의 `profile`은 AUTO build에서도 winning candidate이므로 image
profile 판정에 사용하지 않습니다.

## Sound schema

현재 B TFLM schema는 `b-tflm-v1`, `class_count=10`입니다. score 8/9는
`watering_low`/`watering_high`이고 `microwave`/`cooking`이 아닙니다. SOUND
REPORT는 `src=EVENT,event=SOUND,schema=1,class_count=10`을 포함해야 하며,
index 7은 `flushing_end`입니다. RAWDATA의 zero padding score는 0.5로
dequantize하지 않고 빈 값으로 기록합니다. RAW score window와 firmware가 여러
window를 합쳐 확정한 `EVENT/SOUND` run은 별도 telemetry이며, Central은 이를
ADL evidence로 중복 합산하지 않습니다.

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
