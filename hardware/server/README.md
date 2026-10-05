# hardware/server — 앱 API (mock 서버) · 더미 데이터

> **상태: 초안 v0.1 (하드웨어 담당, 팀 확인 전).** 앱에 보여줄 정보가 정리되면 리포트 API를 추가한다.
> 실물 센서가 없어서 지금은 **시나리오 더미 데이터 + 가짜 판단**으로 돌아간다.

```
시나리오 (hardware/mock/scenarios.py)
   │  ScenarioPlayer: 라벨 → 센서 값 (hardware/mock/models.py) + 센서 오류·카메라 5fps·자리 비움 때 센서 끄기
   ├─▶ replay CSV (data/synthetic/mock_*.csv, 수집 CSV와 같은 형식)  → main.py --mode replay
   └─▶ mock 서버 10Hz ─▶ 가짜 판단 (mock_engine.py) ─▶ FastAPI ──(같은 Wi-Fi, JSON)──▶ 안드로이드 앱
```

## 파일

| 파일 | 하는 일 |
|---|---|
| `../mock/scenarios.py` | 시나리오 목록 (자세 하나씩 / 복합 / 경계 / 센서 오류 / 데모) |
| `../mock/generator.py` | 시나리오 → `Sample`(압력·거리·카메라 점). 실시간 생성기 + replay CSV 만들기 |
| `mock_engine.py` | 가짜 판단: AI 엔진(`ai/engine/upper_body.py`)과 같은 모양의 결과(상태, 확신도, deltas, cues, 3초 필터) |
| `schemas.py` | 앱에 주는 JSON 형식. **API 형식을 바꿀 때는 여기만 고친다** |
| `mock_server.py` | FastAPI mock 서버 (세션·시각·센서 전원을 따라 시나리오 재생) |
| `clock.py` | 앱 기준 시각: '앱 시각 − Pi 단조시계' 차이만 저장, `boot_id` |
| `session.py` | 세션 상태 (idle / running / paused), 경과 시간 |
| `power.py` | 센서 전원 정책 (세션 상태 + 착석 구간 → 압력·카메라·거리 켜고 끄기) |
| `seating.py` | 착석 구간 (자리 비움 5초, 정상/비정상/unknown) |
| `../sensors/power.py` | 센서 전원 인터페이스 `Switchable` (실제 센서 클래스가 구현) + mock용 `SimulatedSwitch` |
| `check_seating.py`, `check_session.py` | 확인 도구 (가상 시각으로 시나리오·API 흐름 시험) |
| `openapi.yaml` | API 명세 (자동 생성 — 직접 고치지 않기). 안드로이드 Retrofit 코드 생성 등에 사용 |

## 실행

저장소 최상위(`SPteam6`)에서. 추가 설치 없음 (`requirements.txt`의 fastapi·uvicorn).

```bash
# 더미 데이터 (replay CSV)
python -m hardware.mock.generator --list                 # 시나리오 목록
python -m hardware.mock.generator --all                  # 전부 → data/synthetic/mock_*.csv + .json
python -m hardware.mock.generator --scenario demo --distance-sensor hc-sr04
python main.py --mode replay --file data/synthetic/mock_demo.csv

# mock 서버
python -m hardware.server.mock_server --autostart        # 바로 demo 재생 (PC 시계로 동기화 + 세션 시작), 포트 8000
python -m hardware.server.mock_server                    # 실제처럼: 앱이 /time/sync → /session/start를 보내야 시작
python -m hardware.server.mock_server --scenario forward_head --no-loop --autostart
python -m hardware.server.check_session                  # 시각·세션·센서 전원 흐름 확인 (41개)
python -m hardware.server.mock_server --export-openapi   # schemas.py를 고친 뒤 openapi.yaml 다시 만들기
```

- 브라우저로 직접 호출해 보기: `http://localhost:8000/docs`
- 폰(같은 Wi-Fi)에서: 서버 시작 때 출력되는 `http://<노트북 IP>:8000/current`
  - 윈도우는 처음 실행할 때 방화벽 허용 창이 뜨면 **개인 네트워크 허용**
  - 학교 Wi-Fi처럼 기기끼리 통신을 막는 곳에서는 안 될 수 있음 → 휴대폰 핫스팟으로 확인
  - 안드로이드 9 이상은 `http://`를 기본으로 막는다 → 앱 `AndroidManifest.xml`에
    `android:usesCleartextTraffic="true"` 필요 (앱 담당)

## API

| 메서드 | 주소 | 용도 |
|---|---|---|
| GET | `/status` | 서버·센서 상태, mock 여부, 재생 중인 시나리오 |
| GET | `/current` | 지금 자세 (홈 화면, 1~3초마다 호출) |
| GET | `/history?seconds=60` | 최근 판단 기록 (1초에 1개, 최대 1시간) |
| GET | `/baseline` | 기준(바른) 자세 측정값, 측정 중이면 남은 시간 |
| POST | `/calibrate` | 기준 자세 다시 측정 (10초 동안 바른 자세) |
| GET | `/seating` | 지금 착석 구간 (자리 비움 5초 규칙), 지금 순간 normal / abnormal / unknown |
| GET | `/seating/segments?hours=24` | 끝난 착석 구간 (시작·끝·종료 사유·첫 비정상까지 초·정상/비정상/unknown 초) |
| POST | `/time/sync` | 앱 시각·시간대 보내기 `{"app_time": 1791207600.1, "timezone": "Asia/Seoul"}` |
| GET | `/time` | 동기화 상태, `boot_id` (바뀌었거나 `synced=false`면 다시 동기화) |
| GET | `/session` | 세션 상태·경과 시간·착석 시간·센서 전원 |
| POST | `/session/start` · `/pause` · `/resume` · `/stop` | 측정 시작·일시정지·재개·정지 |
| GET | `/mock/scenarios` | **mock 전용.** 시나리오 목록 |
| POST | `/mock/scenario` | **mock 전용.** 시나리오 바꾸기 `{"name": "forward_head", "loop": true}` |

필드 설명은 `/docs` 화면이나 `openapi.yaml`에 한글로 있다. 주요 규칙:
- 시각 `time.time()` 초, 거리 mm, 각도 °, 비율 0~1. **"52cm" 같은 표시 글자는 앱이 만든다**
- 판단할 수 없는 값은 `null` (카메라 미인식이면 `deltas`가 null, 목·기울기는 `unknown`)
- **영상 이미지는 어떤 응답에도 없다.** 카메라는 상체 점 좌표(0~1 비율)만
- `postures` 순서 = 대표 자세 우선순위: **거북목 > 다리 꼬기 > 체중 편향 > 기울어진 자세** (`postures[0]`이 대표)

### 착석 구간 (`seating.py`)

| 규칙 | 값 (상수, ⚠️ 임시값은 실물로 조정) |
|---|---|
| 자리 비움 = 그 순간 압력 **합**이 기준 미만 (AI 코드 `ai/features/pressure.py`의 `min_total`과 같은 값) | `EMPTY_TOTAL_ADC = 50` ⚠️ |
| 자리 비움이 이어지면 구간 종료, **종료 시각 = 판단 시각 − 5초** | `EMPTY_END_SEC = 5` |
| 구간 밖에서 압력이 이어지면 새 구간, **시작 = 압력이 처음 들어온 시각** | `SIT_CONFIRM_SEC = 1` ⚠️ |
| 5초 미만 자리 비움 | 구간 유지, 그 시간은 `unknown` |
| `normal` = 목 normal + 기울기 none + 좌면 normal / `abnormal` = 나쁜 자세 확정 / 그 외 `unknown` | 3초 필터 거친 상태 기준 |
| 첫 비정상까지 초 | 구간 시작 → 처음 `abnormal` 확정 (3초 유지) |

- ⚠️ `EMPTY_TOTAL_ADC = 50`은 임시값이다. 실물 FSR로 빈 의자와 앉았을 때 압력 합을 재서 다시 맞춰야 한다 (AI 코드와 같이 바꿀 것).
- `/current`의 `seated`·`sitting_since`는 예전처럼 3초 필터를 따른다 (형식 유지). **착석 시간은 `/seating` 기준으로 쓴다.**
- 끝난 구간은 지금은 서버 메모리에만 있다 (재시작하면 사라짐). DB 저장·자정 처리·앱 시각 동기화는 다음 단계.
- 확인: `python -m hardware.server.check_seating` (22개 시나리오 × 8·6채널, 정답과 비교)

### 시각 동기화 · 세션 · 센서 전원

- **시각:** Pi OS 시계는 바꾸지 않는다. `POST /time/sync`를 받으면 '앱 시각 − Pi 단조시계' 차이만 저장하고,
  모든 기록 시각(`/current`, `/history`, `/seating` …)을 앱 기준으로 찍는다. 차이는 메모리에만 있어서
  서버가 다시 켜지면(재부팅 포함) 사라지고 `boot_id`가 바뀐다 → 앱은 연결할 때·세션 시작 전마다 보낸다.
- **세션:** 동기화 전에는 시작을 거절한다 (`409 CLOCK_NOT_SYNCED`). 기록·판단은 `running`일 때만.
  `elapsed_sec`·`seated_sec`에서 일시정지 시간은 빠진다. 일시정지하면 그때 착석 구간이 끝난다 (`end_reason: pause`).
- **오류:** `{"detail": {"code", "message", "resync_required", "boot_id"}}`. 앱은 `code`로 처리한다.

| code | HTTP | 언제 |
|---|---|---|
| `CLOCK_NOT_SYNCED` | 409 | 동기화 전에 세션 시작 |
| `CLOCK_BACKWARD` | 409 | 다시 동기화했는데 시각이 0.5초 넘게 뒤로 감 (0.5초 이내는 200 `applied: false`, 시각 그대로) |
| `CLOCK_JUMP_IN_SESSION` | 409 | 측정 중 다시 동기화했는데 2초 넘게 앞으로 감 (정지한 뒤 보내면 됨) |
| `SESSION_ACTIVE` | 409 | 세션이 있는데 또 시작 |
| `NOT_RUNNING` | 409 | 실행 중이 아닌데 일시정지 / 기준 자세 측정 |
| `NOT_PAUSED` | 409 | 일시정지가 아닌데 재개 |
| `NO_SESSION` | 409 | 세션이 없는데 정지 |
| `INVALID_TIME` / `INVALID_TIMEZONE` | 422 | 잘못된 시각(2024년 이전 등) / Asia/Seoul이 아닌 시간대 |

| 상황 | 압력 | 카메라·거리 |
|---|---|---|
| 세션 없음 / 정지 | 끔 | 끔 (앉아도 자동으로 다시 시작 안 함) |
| 실행 중, 착석 구간 안 (5초 미만 비움 포함) | 켬 | 켬 |
| 실행 중, 자리 비움 5초 / 아직 안 앉음 | 켬 | 끔 → 압력 1초 확인(새 구간)되면 켬, 카메라는 약 2초 `warming` |
| 일시정지 | 켬 (기록·판단 안 함, `seated_now`만) | 끔 |

- `--autostart` 없이 켜면 앱이 동기화·시작을 보내기 전까지 `/current`는 404 (기록 없음).

### `/current`의 판단 필드 ← AI 엔진 근거

| API 필드 | AI 코드 (`ai` 브랜치) |
|---|---|
| `head_state`, `tilt_state` | `UpperJudgement.head` / `.tilt` (3초 필터 거친 값) |
| `confidence.head` / `.tilt` | `head_confidence` / `tilt_confidence` |
| `deltas` (neck_drop, face_grow, closer_mm, shoulder_angle, head_angle, head_offset) | `UpperJudgement.deltas` |
| `cues` | `UpperJudgement.cues` |
| `pending` | `StateFilter.pending()` |
| `baseline_ready`, `/baseline` | `Baseline.ready`, `remaining()` |
| `center_of_pressure` | `ai/features/pressure.center_of_pressure()` |
| `seat_state`, `confidence.seat` | 좌면 분류기 (아직 없음 → mock은 시나리오 라벨) |

## 시나리오

앞 10초는 항상 기준 측정(바른 자세). `python -m hardware.mock.generator --list`로 확인.

| 종류 | 이름 |
|---|---|
| 자세 하나씩 (9) | `normal` `forward_head` `tilt_left` `tilt_right` `lean_left` `lean_right` `cross_left` `cross_right` `empty` |
| 복합 (4) | `cross_forward` (거북목 + 왼다리 꼬기 → 거북목) `cross_lean` (오른다리 꼬기 + 오른쪽 편향 → 다리 꼬기) `lean_tilt` (왼쪽 편향 + 왼쪽 기울기 → 체중 편향) `all_at_once` (네 가지 동시 → 거북목) |
| 경계 (4) | `short_blip` (3초 미만 순간 자세) `empty_4s` (구간 유지) `empty_6s` (구간 종료) `sit_stand` (앉았다 일어남 반복) |
| 센서 오류 (4) | `distance_fail` `distance_spike` (HC-SR04 튐) `camera_lost` `pressure_dropout` |
| 데모 (1) | `demo` (5분, 정상 위주 + 거북목·다리 꼬기·자리 비움) |

총 22개. 복합 시나리오의 화살표 뒤는 대표 자세(우선순위 기준). 자세한 규칙은 [`../mock/README.md`](../mock/README.md).

주의: 더미 값은 실제 자세 패턴이 아니다. 앱·서버 흐름 확인용이고 정확도 평가에 쓰면 안 된다.

## 팀 확인이 필요한 것

| # | 항목 | 지금 코드 | 누구와 |
|---|---|---|---|
| 1 | 거리센서 | 문서·`config.yaml`은 VL53L1X, 하드웨어 확정 목록은 HC-SR04/미정 → mock은 둘 다 흉내 (`--distance-sensor`) | 전체 |
| 2 | 리포트(일간·주간) API | 아직 없음. 앱 화면 정보가 정리되면 추가 | 앱 |
| 3 | `postures`에 기울기 포함 | `tilt_left`/`tilt_right`도 나쁜 자세로 넣음 | 전체 |
| 4 | 3초 필터 | 서버가 주는 상태는 3초 필터를 거친 값 (`pending`에 대기 중인 변화) | AI |
| 5 | FSR 개수 | 수집 단계 8칸 → 실험 후 6칸 선별, **최종 6칸** (2열 × 3행). `config.yaml`은 아직 8 → 6채널 제안값은 `../mock/README.md` (공용 파일이라 팀 상의 후 변경) | AI |
| 6 | API 문서 위치 | `hardware/server/openapi.yaml`. 확정되면 `docs/`로 옮길지 | 전체 |

## 나중에 바꿀 곳

- **AI 엔진이 합쳐지면:** `mock_engine.py`의 `judge_upper()` / `judge_seat()` 대신 AI 판단 결과를 넣는다 (응답 형식은 그대로)
- **실제 센서가 오면:** `ScenarioPlayer` 대신 실제 센서로 읽는 real 모드 서버를 추가. 센서 클래스는
  `../sensors/power.py`의 `Switchable`을 구현하고, 꺼진 센서는 읽지 않는 hub(`common`의 `SensorHub`를 감싸기)를 만든다.
  센서 값은 Pi 시각으로 찍히므로 읽은 직후 `clock.stamp(sample)`로 앱 시각으로 바꾼다
