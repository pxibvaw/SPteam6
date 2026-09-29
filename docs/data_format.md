# SitSense 데이터 형식

하드웨어 · AI · 앱이 주고받는 데이터의 약속. 코드 기준은 `common/`이고, 이 문서는 설명용이다.
바꿀 때는 `common/` 코드와 이 문서를 같이 고치고, 상대방이 PR을 확인한 뒤 합친다.

**원칙:** 판단 기준(각도, 거리 기준 등)은 아직 정하는 중이라, 수집할 때는 판단값이 아니라
**센서 원래 값**을 저장한다. 기준이 바뀌어도 다시 수집할 필요 없이 계산만 다시 하면 된다.

```
센서 값 ──▶ 수집 CSV (+ 세션 정보) ──▶ 판단 결과 ──▶ DB / 앱
 ①          ②  ③                      ④
```

## ① 센서 값 — `common/sensors_base.py`

모든 시간(`ts`)은 `time.time()` 초 단위(float).

| 센서 | 형식 | 필드 |
|---|---|---|
| 압력 FSR 406 × 8 (MCP3008) | `PressureReading` | `ts`, `values` (int × 8, 0~1023 원래 값) |
| 거리 VL53L1X | `DistanceReading` | `ts`, `distance_mm` (int, 실패면 None), `range_status` (0 = 정상) |
| 카메라 (정면) | `FrameReading` | `ts`, `image` (BGR) — **저장하지 않음** |
| 상체 점 (카메라 → MediaPipe) | `PoseReading` | `ts`, `detected`, `points` (부위 7개 × x, y, z, visibility) |

- 한 순간의 값은 `SensorHub.read()`가 `Sample` 하나로 묶어 준다 (압력·거리·카메라 동기화).
- 카메라는 초당 약 5장이라 센서(10Hz)보다 느리다. `Sample`에는 가장 최근 카메라 결과가 들어가고,
  `pose.ts`로 몇 초 전 값인지 알 수 있다.
- FSR 채널 번호 ↔ 방석 위치는 `config.yaml`의 `pressure.positions` (배치도 확정 후 수정).

### 카메라 점 7개 (+ 목)

PosturePal(MIT)의 정면 웹캠 상체 8개 점을 따른다.

| 부위 | MediaPipe 번호 | 비고 |
|---|---|---|
| `nose` | 0 | |
| `left_eye`, `right_eye` | 2, 5 | |
| `left_ear`, `right_ear` | 7, 8 | 머리카락에 가려질 수 있음 → `visibility` 보고 사용 여부 결정 |
| `left_shoulder`, `right_shoulder` | 11, 12 | |
| 목 | — | 저장하지 않고 두 어깨의 중점으로 계산 |

- `left` / `right`는 **사람 기준**. 정면 카메라 화면에서는 사람 왼쪽이 화면 오른쪽에 찍힌다.
- `x`, `y`는 0~1 비율. 16:9 화면이라 **각도 계산 전에 픽셀로 바꿔야** 한다 (세션 정보의 해상도 사용).
- 몸 중심은 카메라가 아니라 **압력 중심**(FSR 값으로 위치 가중평균)으로 계산한다.

## ② 수집 CSV — `common/recorder.py`

파일: `data/raw/{session_id}.csv`, `session_id = {subject}_{YYYYMMDD_HHMMSS}`
한 줄 = 한 순간, **총 47칸**.

| 그룹 | 컬럼 | 값 |
|---|---|---|
| 시간·식별 | `ts`, `session_id`, `subject` | |
| 단계 | `phase` | `baseline`(처음 10초, 바른 자세) / `record` |
| 좌면 라벨 | `seat_label` | `normal` `lean_left` `lean_right` `cross_left` `cross_right` `empty` |
| 목 라벨 | `head_label` | `normal` `forward`(거북목) |
| 기울기 라벨 | `tilt_label` | `none` `left` `right` |
| 압력 | `p0` ~ `p7` | 0~1023 |
| 거리 | `distance_mm`, `distance_status` | 실패면 `distance_mm` 빈칸 |
| 카메라 상태 | `cam_ts`, `pose_detected` | 카메라 없으면 `cam_ts` 빈칸 |
| 카메라 좌표 | `{부위}_x`, `{부위}_y`, `{부위}_z`, `{부위}_vis` | 부위 7개 × 4 = 28칸 |

- `cross_left` = 왼다리를 위로 꼰 다리 꼬기, `cross_right` = 오른다리를 위로.
- 거북목과 기울기는 따로 라벨링한다 (동시에 일어날 수 있어서).

## ③ 세션 정보 — `data/raw/{session_id}.json`

| 필드 | 설명 |
|---|---|
| `session_id`, `subject`, `date` | 기본 식별 |
| `camera_width`, `camera_height` | 좌표를 픽셀로 바꿀 때 사용 |
| `fsr_channels_used` | 8개 중 실제로 쓴 채널 (6개 선별 후 변경) |
| `baseline_seconds` | 기준 측정 시간 |
| `chair`, `cushion`, `notes` | 의자·방석, 특이사항 |
| `format_version` | 파일 형식 버전 (형식이 바뀌면 올림, 재생할 때 확인) |
| `end_date`, `rows` | 끝난 시각, 저장된 줄 수 (저장이 끝나면 채워짐) |
| `status` | `completed` 정상 종료 / `interrupted` Ctrl+C / `error` 오류로 중단 / `recording` 기록 중 (이 상태로 남아 있으면 비정상 종료) |

키·체중은 저장하지 않는다 (기준 자세 대비 변화로 판단하므로 불필요).
`subject`는 영문·숫자로만 (실명 금지).

## ④ 판단 결과 — `common/schema.py`의 `PostureEvent`

AI 엔진이 만들고, 하드웨어 서버가 DB에 저장해 앱에 전달한다.

| 필드 | 값 |
|---|---|
| `start`, `end` | 자세 유지 구간 (`time.time()` 초) |
| `seat_state` | 좌면 라벨 값 + `unknown` |
| `head_state` | `normal` / `forward` / `unknown` |
| `tilt_state` | `none` / `left` / `right` / `unknown` |
| `distance_mm` | 구간 평균 화면 거리 (앱에서 cm로 표시) |
| `distance_level` | `ok` / `caution` / `warning` / `unknown` |
| `confidence` | 0~1 |

`PostureEvent`는 만들 때 값을 검사한다 (끝 ≥ 시작, 확신도 0~1, 허용된 상태 값만).
DB에서 읽을 때는 `PostureEvent.from_dict(d)`를 쓰면 같은 검사를 거친다.

## ⑤ 오류 처리 규칙

| 상황 | 동작 | 오류 이름 |
|---|---|---|
| `config.yaml` 없음·문법 오류·값 잘못됨 (예: 채널 9개, 50cm ≤ 40cm) | 어느 항목이 왜 틀렸는지 알려주고 종료 | `ConfigError` |
| 압력 한 번 읽기 실패, 채널 수·값 범위(0~1023) 이상 | 그 순간만 건너뛰고 계속 | `SampleSkipped` |
| 거리 읽기 실패 | `distance_mm` 빈칸, `distance_status = -1`로 기록하고 계속 | — |
| 카메라 읽기 실패 | `pose_detected = 0`으로 기록하고 계속 | — |
| 같은 센서가 20번 연속 실패 | 배선·연결 확인 메시지와 함께 종료 | `SensorError` |
| 허용되지 않은 라벨·상태 값 | 종료 | `LabelError` |
| 같은 이름 세션 파일이 이미 있음 | `_2`, `_3`을 붙여 새로 저장 (덮어쓰지 않음) | — |
| 저장 중 꺼짐·Ctrl+C | 매 줄 바로 저장되어 있음. 세션 정보에 종료 상태 기록 | — |
| 재생 파일 없음·예전 형식·채널 수 다름 | 종료 | `ReplayError` |
| 재생 파일 중간에 깨진 줄 | 그 줄만 건너뛰고 개수를 알려줌 | — |

하드웨어 팀원이 실제 센서 클래스를 만들 때는 **읽기에 실패하면 예외를 내기만** 하면 된다.
처리는 `SensorHub`가 위 규칙대로 한다. `main.py` 종료 코드: 0 정상 / 1 설정·입력 / 2 센서 / 3 저장 / 130 Ctrl+C.

### 화면 거리 단계 근거

| 단계 | 기준 | 출처 |
|---|---|---|
| `ok` | 50cm 이상 | OSHA 50~100cm, AOA 50~70cm 권장 하한 |
| `caution` | 40~50cm | 국내 지침은 지키지만 국제 권장보다 가까움 |
| `warning` | 40cm 미만 | 고용노동부 VDT 취급근로자 작업관리지침 제6조 (40cm 이상) 미달 |

- [고용노동부 VDT 취급근로자 작업관리지침](https://www.law.go.kr/admRulLsInfoP.do?admRulSeq=2100000185801)
- [OSHA eTools: Computer Workstations — Monitors](https://www.osha.gov/etools/computer-workstations/components/monitors)
- [AOA — Computer vision syndrome](https://www.aoa.org/healthy-eyes/eye-and-vision-conditions/computer-vision-syndrome)
- [PosturePal: Real-Time Posture Classification with a Laptop Webcam (MIT)](https://ethanweber.me/documents/posturepal.pdf)
