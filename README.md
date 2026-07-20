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

SLIMHUB v1 호환 flag 스타일도 유지하지만, 새 운영에는 계층형 v2 command를
권장합니다. 호환 option 목록은 `slimhub-v2 --legacy-help`로 확인할 수 있습니다.

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
slimhub-v2 sound status --address AA:BB:CC:DD:EE:FF
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
uSD 중복 쓰기를 피하기 위해 정상 `RAWDATA`, 주기 `REPORT`, ENV/SOUND와 내부
상태 record는 `programdata/reports/`에 다시 복제하지 않습니다. 기본
`SLIMHUB_AUDIT_JSONL=minimal` 정책은 malformed JSON, identity validation 실패,
command 실패 같은 오류 record만 JSONL로 보존합니다. 일시적인 상세 진단이 필요할
때만 `SLIMHUB_AUDIT_JSONL=full`을 지정합니다. 기존 reports 파일은 자동 삭제하지
않습니다.
Runtime 로그는 `programdata/logging.log`에 기록됩니다.
`logging.log`는 연결/해제, command 전송, warning/error 중심으로 작게
유지합니다. 주기 `REPORT` payload와 BLE notify/debug dump는 이 파일에
쓰지 않습니다.
일시적인 BLE 연결 실패는 traceback 없이 한 번의 warning으로 요약하고,
`logging.log`는 5 MiB 단위로 최대 5개 backup까지 순환합니다. typed multimodal
record에는 해당 event만 저장하며 누적 session 전체를 반복 복사하지 않습니다.

## Sound domain-adaptation capture

명시적인 운영자 명령이 있을 때만 Node의 PCM AUDIO를 저장합니다. label은 1–24자의
영문/숫자/`_`/`-`만 허용하며 path traversal 문자열은 거부합니다.

```bash
slimhub-v2 sound start --address AA:BB:CC:DD:EE:FF \
  --label pee --dest both --threshold-rms 1200 --max-seconds 90 \
  --silence-seconds 5

slimhub-v2 sound background --address AA:BB:CC:DD:EE:FF \
  --dest ble --max-seconds 600

slimhub-v2 sound status --address AA:BB:CC:DD:EE:FF
slimhub-v2 sound stop --address AA:BB:CC:DD:EE:FF
```

`sound start`는 threshold를 넘기 전 `ARMED`, 실제 PCM이 시작되면 `ACTIVE`입니다.
`--threshold-rms 0`은 gate를 해제하며 silence도 자동으로 0이 됩니다. background
명령은 임의 label을 받지 않고 항상 `background`, threshold 0, silence 0을
사용합니다. legacy `command record`와 `record-stop`도 계속 지원합니다.

BLE PCM은 16 kHz, mono, PCM16 WAV와 JSON sidecar로 저장됩니다.

```text
data/sound/<NODE_MAC>/<label>/<cid>.wav
data/sound/<NODE_MAC>/<label>/<cid>.json
```

sidecar에는 명령 파라미터, ARMED/START/DONE 원문 REPORT, 수신 sample/block 수,
missing range, firmware 정보, queue/ble drop, 종료 원인과 `complete`가 들어갑니다.
sequence 또는 sample-offset gap, drop, disconnect, timeout이 있으면 WAV는 복구
가능하게 닫되 `complete=false`로 표시합니다. 원본 WAV는 dataset export 시에도
수정하지 않습니다.

운영자용 display는 daemon이 확정한 IN/OUT(D0/D1)과 inference 상태 전이만
`programdata/display.txt`에 append합니다. 개별 ENV/SOUND, candidate, command
ACK, timeout, baseline은 display와 기본 audit JSONL에 표시하지 않습니다.
동일한 IN/OUT 및 inference 원문 JSON은 node별
`inference/debugstr/YYYY-MM-DD.txt`에 저장되며, 날짜별 평문 archive는
`data/display/YYYY-MM-DD.txt`에도 같은 내용으로 남습니다. daemon 시작 시 기존
`programdata/display.txt`와 당일 archive의 ENV/SOUND 줄도 제거합니다.

현재 배포된 Node v2처럼 `src=ADL` final report를 보내지 않는 image에서는 과거
debugstr에서 확인된 보수적인 location/event signature만 `derived_inference`로
보완합니다. 예를 들어 BEDROOM session의 S2는 watchTV로 변환됩니다.
이 fallback은 `ground_truth_eligible=false`로 기록되어 firmware ADL truth와
구분됩니다.

## DB 증분 적재와 cron

기존 SLIMHUB와 같이 별도 DB 프로그램이 `data/`를 직접 읽습니다.
`debugstr/YYYY-MM-DD.txt`의 검증된 `EVENT`(`ENTER=10`, `EXIT=20`)는 로컬
MySQL `in_out`에, final inference(`POP`, `COMPLETE`, `PARTIAL`, `NO_MATCH`)는
`event_adl`에 적재합니다. `rawdata`는 수집 원본으로만 보존하며 DB 입력에는
사용하지 않습니다. 파일별 byte offset은
`programdata/db_sync/data_offsets.json`에 저장합니다. 이어서 local table의 `id`
offset을 기준으로 동일 schema의 원격 table에 전송합니다.

`house_mac`은 기본적으로 `programdata/config.json`의 Hub `address`를 사용하며,
배포 식별자를 별도로 써야 하면 `SLIMHUB_HOUSE_MAC`으로 재정의합니다. `in_out`의
`location`은 `<room>:<node MAC>`, `event_adl`의 `location`은 node MAC으로 저장해
기존 운영 DB 의미를 유지합니다. firmware의 0–100 ADL truth는 기존 DB의 0–1
범위로 변환합니다.

DB 자격 증명은 저장소나 crontab에 넣지 말고 실행 환경에서 주입합니다.
백업 SLIMHUB에서 사용하던 `ADL_DB_*`, `LOCAL_DB_*`, `REMOTE_DB_*` 이름도
fallback으로 인식하지만, 새 배포에는 아래 `SLIMHUB_*` 이름을 권장합니다.
환경 파일 template은 [`docs/db.env.example`](docs/db.env.example)이며 기본 wrapper는
`/home/rtlab/.config/slimhub-v2/db.env`를 읽습니다.

```bash
export SLIMHUB_LOCAL_DB_HOST=localhost
export SLIMHUB_LOCAL_DB_PORT=3306
export SLIMHUB_LOCAL_DB_USER='...'
export SLIMHUB_LOCAL_DB_PASS='...'
export SLIMHUB_LOCAL_DB_NAME=adl_event
# 필요할 때만 Hub address 대신 배포용 house identifier를 지정합니다.
export SLIMHUB_HOUSE_MAC='...'

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
slimhub-v2 db status
```

테이블 이름은 필요하면 `SLIMHUB_DB_ADL_TABLE`(기본 `event_adl`)과
`SLIMHUB_DB_INOUT_TABLE`(기본 `in_out`)로 바꿀 수 있습니다. ingest/upload
offset은 `programdata/db_sync/`에 보관됩니다. cron 예시는
[`docs/slimhub-v2.crontab`](docs/slimhub-v2.crontab)에 있으며, 실제 설치 전에는
해당 환경변수가 cron에서도 안전하게 제공되는지 확인해야 합니다.
기본 ingest는 백업 SLIMHUB처럼 오늘 data 파일부터 시작합니다. 과거 파일까지 의도적으로
적재할 때만 `SLIMHUB_DB_BACKFILL=1`을 사용합니다. cron은 local ingest를 3분마다,
remote upload를 10분마다 독립 실행합니다.
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
- `REPORT`: UTF-8 comma-separated key/value 또는 NCS-compatible JSON payload
- `AUDIO`: v1 binary header와 512-sample signed PCM16LE block

BLE notification 경계는 NUS frame 경계가 아닙니다. Central은 connection별
byte accumulator에서 16-byte header의 little-endian payload length와 뒤따르는
CRLF를 모두 확인한 뒤에만 frame을 파싱합니다. 잘못된 length, packet type,
CRLF는 bounded resynchronization으로 폐기하므로 MTU 23/247/517의 header/payload/
CRLF 분할과 연결된 frame stream도 처리합니다. 연결 직후 가능한 최대 MTU를
요청하지만 작은 ATT notification에서도 같은 accumulator가 AUDIO frame 전체를
복원합니다.

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

frame MAC, BLE alias, boot/sequence ID와 node uptime은 메모리 상태에서 처리하고,
확정 IN/OUT과 inference만 `data/`에 저장합니다. full audit를 명시적으로 켠 경우에만
RAW/REPORT/command record를 `programdata/reports/*.jsonl`에 저장합니다. 서로 다른 node의
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

`src=EVENT`의 `BASELINE`, `ENV`, `SOUND`와 `src=ADL`의 `PREDETECT`, `POP`,
`COMPLETE`, `PARTIAL`, `NO_MATCH`는 메모리에서 typed record로 처리됩니다.
확정 inference만 `data/.../debugstr`에 기록되며 이 stream은 IN/OUT estimator에 절대 재입력하지
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

### NCS-compatible JSON migration reports

`REPORT` payload의 첫 non-whitespace byte가 `{`이면 comma-separated REPORT가
아닌 one-frame JSON record로 파싱합니다. notification 분할/합침은 기존
connection별 frame accumulator에서 먼저 복원하므로 ATT notification 경계와
JSON 경계를 같다고 가정하지 않습니다. JSON의 uppercase colon `device`와 frame
MAC이 다르면 security warning을 기록하고 frame MAC을 authoritative node identity로
사용합니다.

Schema 2 JSON `EVENT`(`ENTER=10`, `EXIT=20`)는 legacy UI timeline에만 기록하고
movement estimator에는 다시 입력하지 않습니다. 값 또는 identity가 유효하지 않은
record는 minimal audit JSONL에는 남지만 movement timeline에서는 제외합니다. JSON
`INFERENCE`의 `PRE-DETECT`, `POP`, `COMPLETE`, `PARTIAL`, `NO_MATCH`는
`(frame MAC,bid,aid)`로 activity upsert됩니다. 같은 key의 typed `src=ADL`
record가 도착하면 score/coverage/margin/reset을 가진 typed detail을 canonical로
보존하고 activity를 두 번 세지 않습니다. POP은 final activity이며 같은 `sid`의
D1 terminal 결과와 별도 activity로 유지합니다.

현재 schema 2 compact ADL의 `cov/m/dur/rst/seq` alias도 canonical detail로
정규화합니다. payload가 routing용 `src=ADL` 뒤에 source-count metric `src=N`을
다시 포함하면 첫 `src`를 routing identity로 유지하고 두 번째 값은
`source_count`와 `duplicate_fields`에 보존합니다.

JSON schema 2의 `truth`는 adaptive matcher의 0–1 score ratio이고 display에
`(adaptive)`로 표시합니다. 기존 heap truth는 `(legacy)`로 구분합니다. unknown
JSON key, future schema, 31자를 넘은 sequence와 malformed JSON도 parser를
중단시키지 않고 원문과 validation error를 minimal audit JSONL에 보존합니다.

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
