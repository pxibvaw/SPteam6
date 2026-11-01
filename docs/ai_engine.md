# AI 엔진 연결 안내 (하드웨어 서버 ↔ AI)

> AI 담당 작성. 서버(`hardware/server/mock_engine.py`)의 가짜 판정을 실제 AI로 바꾸는 방법.
> AI 쪽 파일만 추가했고 `hardware/`는 고치지 않았다. 아래 연결은 하드웨어 담당이 서버 코드에 넣는다.

## 파일

| 파일 | 하는 일 |
|---|---|
| `ai/engine/sitsense_engine.py` | **AI 엔진.** `Sample` 하나 → 좌면·목·기울기·화면 거리 판정 (서버 `/current` 필드와 같은 이름) |
| `ai/engine/seat.py` | 좌면 판정기 (학습한 RF 모델 + 후처리). 엔진이 안에서 씀 |
| `ai/engine/upper_body.py` | 목·기울기 규칙 판정. 엔진이 안에서 씀 |
| `ai/camera/pose_source.py` | **카메라 부품.** Pi 카메라 + MediaPipe를 뒤에서 돌리고 가장 최근 상체 점을 줌 (`PoseSource`, 전원 켜기/끄기) |
| `ai/models/final_seat_model.joblib` | 학습한 좌면 모델 (RF, 유지 4초·확신도 0.7·평균 2초 포함) |
| `ai/engine/check_engine.py` | 엔진 자동 점검 `python -m ai.engine.check_engine` (10개) |

## 시간 기준 (세 센서 공통, `config.yaml`)

| | 판정 간격 | 떨림 줄이기 | 상태 바꾸기 전 유지 |
|---|---|---|---|
| 압력 (좌면) | 0.5초 | 최근 2초 확률 평균 | 4초 (+ 확신도 0.7, 모델 파일) |
| 카메라 (목·기울기) | 0.5초 | 최근 2초 중앙값 (`upper_rules.smooth_sec`) | 4초 (`thresholds.short_filter_sec`) |
| 거리 | 0.5초 | 최근 2초 중앙값 | 4초 (`thresholds.short_filter_sec`) |

- 4초 근거: 압력 데이터 재생 테스트로 고른 값 + ISO 11226 "4초 넘게 유지 = 정적 자세". 이전 3초는 근거 없는 임시값이었다.
- 자세를 바꾸면 약 5초 뒤(떨림 줄이기 약 1초 + 유지 4초) 앱에 반영된다. 점검 스크립트 결과도 셋 다 약 5초.
- `short_filter_sec`는 서버의 착석 구간 '비정상 확정'에도 쓰여서 함께 4초가 된다 (서버 점검 5종 모두 통과 확인).
- `upper_rules.smooth_frames`(5장)는 `smooth_sec`(2초)로 바뀌었다. 장 단위는 카메라 속도에 따라 1~2.5초로 달라졌다.

## 쓰는 법

```python
from ai.engine.sitsense_engine import SitSenseEngine
engine = SitSenseEngine(cfg)                 # cfg = load_config()

res = engine.calibrate(samples)              # 기준 자세 10초 동안 모은 Sample 목록
res.ok, res.reason                           # 실패 이유: pressure_low(덜 눌린 센서) / no_person(카메라에 사람 없음)
                                             # 실패하면 엔진은 이전 기준을 그대로 씀 (서버의 using_previous와 같음)
engine.restore(b.pressure, b.distance_mm, b.pose)   # DB에 저장된 BaselineValue로 복원 (서버 재시작 등)

r = engine.update(sample)                    # 매 순간. 0.1초든 0.5초든 상관없음
r.seat_state, r.head_state, r.tilt_state     # 문자열, 이미 유지 시간을 거친 '확정' 상태
r.confidence                                 # {"seat", "head", "tilt"}
r.pending                                    # [{"kind", "state", "remaining_sec"}]  ← /current의 pending과 같은 모양
r.deltas, r.cues                             # 상체 변화량·단서 (카메라 미인식이면 빈 dict)
r.distance_level                             # ok / caution / warning / unknown (40·50cm, 4초 유지)
r.to_dict()                                  # JSON으로 바로 저장 가능
```

## `mock_engine.py`에 넣는 곳 (제안)

```python
# __init__
from ai.engine.sitsense_engine import SitSenseEngine
self.ai = SitSenseEngine(cfg)
if baseline is not None:                     # 저장된 기준으로 시작할 때
    self.ai.restore(baseline.pressure, baseline.distance_mm, baseline.pose)

# _finish_baseline(): 기존 품질 검사(no_person / sensor_lost / too_much_motion)를 통과한 뒤
res = self.ai.calibrate(buf)
if not res.ok:
    reason = res.reason                      # pressure_low가 새로 생김 → 실패 이유 목록에 추가 필요

# update(): judge_seat()·judge_upper() 대신
r = self.ai.update(sample)
seat = SeatState(r.seat_state)               # ⚠️ self.filters["seat"/"head"/"tilt"]를 거치지 않는다 (아래 1번)
head, tilt = HeadState(r.head_state), TiltState(r.tilt_state)
seat_conf, head_conf, tilt_conf = r.confidence["seat"], r.confidence["head"], r.confidence["tilt"]
deltas, cues, pending = r.deltas, r.cues, r.pending
```

## 맞춰야 할 것

1. **유지 시간이 두 번 걸리지 않게.** 엔진 결과는 이미 확정 상태다 (좌면: 4초 유지 + 확신도 0.7, 목·기울기·거리: 4초).
   서버 `StateFilter`를 또 거치면 8초 넘게 늦어진다. 엔진 결과를 그대로 쓰고 `pending`도 엔진 것을 쓴다.
2. **기준 측정 실패 이유 `pressure_low` 추가.** 압력 기준은 모든 센서가 최대치(4095)의 25% 이상 눌려야 한다.
   하나라도 덜 눌리면(측정 중 덜 앉음 등) 실패 → 직전 기준 유지. 앱 안내 문구 예: "방석에 깊숙이 바르게 앉아 다시 측정해 주세요".
3. **mock 압력 값 크기.** `hardware/mock/models.py`의 압력이 약 300~600인데, 실제 MCP3208 수집값은 바르게 앉으면 약 3200~3800이다.
   이대로 엔진에 넣으면 기준 측정이 `pressure_low`로 실패한다. mock 값을 실제 크기로 올려 주거나,
   mock 서버에서는 엔진 대신 기존 가짜 판정을 쓰는 옵션을 두는 게 좋다 (점검 스크립트는 7배로 키워서 시험함).
4. **화면 거리.** 서버도 `classify_distance`로 바로 판정한다 (유지 시간 없음). 엔진의 `distance_level`은 같은 40·50cm 기준에
   4초 유지가 있어서 잠깐 숙일 때 경고가 안 깜빡인다. 어느 쪽을 `/current`에 쓸지 정하기.
5. **판정 속도.** 좌면 판정은 0.5초마다 한 번(맥에서 약 13ms). 서버가 0.1초마다 불러도 사이에는 직전 결과를 준다.
   Pi에서 실제 속도는 처음 돌릴 때 확인 필요.

## 실제 센서 모드 (main.py real / 서버 real)

카메라는 `CameraPoseSource`를 `SensorHub(pose=...)`에 넣으면 된다. `read()`는 기다리지 않고 가장 최근 결과를 준다.

```python
from ai.camera.pose_source import CameraPoseSource
pose_src = CameraPoseSource.from_config(cfg, camera="picam")   # 맥 테스트: camera="webcam"
pose_src.power_on(ts)          # 서버 전원 정책: Switchable과 같은 is_on / power_on / power_off / state
hub = SensorHub(pressure, distance, pose_src, n_channels=6, adc_max=4095)
```

- Pi 준비는 `docs/pi_camera_collect.md`와 같다 (`python3-picamera2`, `--system-site-packages` 가상환경).
- 카메라를 거꾸로 달았으면 `from_config(cfg, camera="picam", rotate180=True)`. 좌우 반전은 하지 않는다 (왼쪽·오른쪽이 바뀜).
- 카메라 결과가 1.5초보다 오래되면 엔진이 목·기울기를 `unknown`으로 둔다.
