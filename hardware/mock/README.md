# hardware/mock — 시나리오 더미 데이터

실물 센서가 오기 전까지 쓰는 더미 데이터. mock 서버 설명은 [`../server/README.md`](../server/README.md).
**코드·앱 흐름 확인용**이고 실제 자세 패턴이 아니라서 정확도 평가에 쓰면 안 된다.

| 파일 | 하는 일 |
|---|---|
| `scenarios.py` | 시나리오 목록 (`Step(초, 자세, 센서 오류)` 여러 개). 대표 자세 우선순위 |
| `models.py` | 라벨 → 압력·거리·카메라 좌표 (노이즈 포함, 영상은 만들지 않음) |
| `generator.py` | 시나리오 재생: 실시간 `Sample` 생성(mock 서버, `--live`) + replay CSV 저장 |
| `check.py` | 만든 CSV 요약 (구간별 라벨·센서 상태, 자리 비움·착석 구간, 정답과 비교) |
| `fake_history.py` | 며칠치 가짜 기록(세션·착석 구간·시간대별 기록 → 진짜 자정 작업으로 요약). 리포트 화면 확인용 |

## 실행 (저장소 최상위에서)

```bash
python -m hardware.mock.generator --list                       # 시나리오 목록
python -m hardware.mock.generator --all --seed 1               # 전부 → data/synthetic/mock_*.csv + .json
python -m hardware.mock.generator --scenario demo --start "2026-10-15 23:58:00"   # 시작 시각 지정 (자정 넘기기)
python -m hardware.mock.generator --scenario empty_6s --live --seconds 40         # 실시간 10Hz 출력
python -m hardware.mock.check data/synthetic/mock_empty_6s.csv                    # 요약 (와일드카드 가능)
python main.py --mode replay --file data/synthetic/mock_demo.csv                  # 재생
```

- 압력 채널 수는 `config.yaml`의 `pressure.n_channels`를 따른다 (6채널 → `p0~p5`, 46칸 / 7채널 → 47칸 / 8채널 → 48칸).
  CSV와 `config.yaml`의 채널 수가 다르면 replay가 멈추니, 설정을 바꿨으면 CSV를 다시 만든다.
- 거리센서는 HC-SR04 (`config.yaml`의 `distance.sensor`, 값 튐 흉내 포함).
- `--start`는 한국 시간. CSV의 `ts`는 `time.time()`과 같은 초 단위.
- 윈도우 Git Bash에서 한글이 깨지면 `PYTHONIOENCODING=utf-8`을 앞에 붙인다.

## 규칙

| 상황 | CSV에 나타나는 모습 |
|---|---|
| 라벨 | 그 순간 실제로 한 자세(정답). `unknown`은 라벨로 쓰지 않음 |
| 카메라 켜짐, 사람 못 찾음 | `cam_ts` 있음, `pose_detected=0`, 좌표 빈칸 |
| 일부 점만 안 보임 | 그 점의 열만 빈칸 |
| 거리 측정 실패 (두 센서 모두) | `distance_mm` 빈칸, `distance_status=-1` |
| HC-SR04 값 튐 | `distance_status=0`인데 틀린 값 (범위 밖 + 범위 안) |
| 압력 읽기 실패 | 그 줄이 없음 (시각에 구멍) |
| FSR 한 채널 끊김 | 그 채널만 0 근처 |
| 자리 비움 5초 미만 | 압력 0 근처, 카메라 미인식, 거리는 뒤쪽 벽(약 1100mm) |
| 자리 비움 5초 이상 | 착석 구간 종료. **압력은 계속 기록**, 카메라 꺼짐(`cam_ts` 빈칸), 거리 꺼짐(빈칸, `-1`) |
| 다시 앉음 | 카메라·거리 다시 켜짐. 카메라는 약 2초 동안 `pose_detected=0` |

- 다리 꼬기와 체중 편향이 함께면 `seat` 라벨은 우선순위가 높은 `cross_*`, 체중 편향은 압력 모양과 JSON에만.
- 대표 자세 우선순위: 거북목 > 다리 꼬기 > 체중 편향 > 기울어진 자세 (`hardware/server/seating.py`의 `PRIORITY`를 같이 씀).
- 세션 JSON의 `extra.expected`에 정답이 있다: 구간별 라벨·대표 자세·센서 오류(`steps`),
  착석 구간(`segments`), 카메라·거리 꺼진 구간(`sensors_off`).

## 시나리오 (모두 맨 앞 10초는 기준 측정)

| 종류 | 이름 |
|---|---|
| 단일 | `normal` `forward_head` `cross_left` `cross_right` `lean_left` `lean_right` `tilt_left` `tilt_right` `empty` |
| 복합 | `cross_forward` (거북목+다리 꼬기) `cross_lean` (다리 꼬기+체중 편향) `lean_tilt` (체중 편향+기울기) `all_at_once` |
| 경계 | `short_blip` (2.9초·1초 순간 자세, 비교용 3.5초) `empty_4s` `empty_6s` `sit_stand` (앉았다 일어남 반복) |
| 센서 오류 | `distance_fail` `distance_spike` (HC-SR04 튐) `camera_lost` (미인식 + 귀만 가림) `pressure_dropout` (줄 건너뜀 + 채널 끊김) |
| 데모 | `demo` (5분, 정상 위주 + 거북목·다리 꼬기·자리 비움·순간 거북목) |

시나리오를 추가하려면 `scenarios.py`의 `SCENARIOS`에 `_scenario(이름, 설명, Step(...), ...)`를 더한다.
`Step(초, seat=, head=, tilt=, pos=, extra_lean=, distance_fail=, distance_spike=, camera_lost=, hidden_points=, pressure_drop=, dead_channel=)`
— 라벨 값은 `common/schema.py`에 있는 것만. `pos`(앉은 위치)를 안 쓰면 자리 비움은 `none`, 그 외는 `middle`. 더미 압력·거리는 앉은 위치를 흉내 내지 않는다.

## 공용 파일 수정 제안 (팀 확인 후)

`config.yaml`은 실제 수집 장비(MCP3208, FSR 6채널(CH0, 1, 3, 4, 6, 7), HC-SR04, 0.5초 간격)에 맞췄다. 아직 남은 제안:

- **6채널로 줄일 때:** 실험 후 `n_channels`, `channels`, `positions`를 같이 바꾼다 (2열 × 3행 임시 배치는
  `hardware/server/check_seating.py`의 `LAYOUTS[6]`).
- `thresholds.empty_off_sec: 5` — 자리 비움이 이만큼 이어지면 착석 구간 종료 (지금은 `hardware/server/seating.py`의 `EMPTY_END_SEC`)
- 거리센서 꺼짐을 실패(`-1`)와 다른 값으로 구분할지 (지금은 `-1`, 카메라 `cam_ts` 빈칸으로 구분)
- `seating.EMPTY_TOTAL_ADC = 50`은 10bit(0~1023) 때 정한 임시값. 12bit(0~4095) 실물 값으로 다시 재서 정한다
  (더미 압력은 아직 0~1023 규모라 이 값으로 시험이 통과한다)

## 센서 값 크기 (실제 수집 기준)

- **압력**: 6채널이면 실제 수집값(p1~p3, 자세 바꾼 뒤 10초 이후)의 자세별 채널 평균 표(`models.py` `REAL_MEANS`)를 쓴다.
  앉으면 대부분 채널이 3000~3700대이고, 다리 꼬기·체중 편향이면 특정 채널만 크게 빠진다 (예: 오른다리 꼬기 → p1 약 1800).
  채널 수가 6이 아닌 배치는 예전 모양에 `PRESSURE_SCALE`을 곱해 쓴다. 흔들림은 실제 폭의 35%(`REAL_DRIFT_K`)만 준다.
- **거리**: 측정 안 됨 4.4%(실제 수집값), 튀는 값 0.5%. 앉은 위치(앞/중/뒤)에 따른 거리 차이(약 40/65/85cm)는 넣지 않았다 —
  화면 거리 단계가 시나리오와 상관없이 바뀌어 앱 화면 시험이 어려워지기 때문.
- 판단은 여전히 시나리오 정답(가짜 판정)이다. 같은 값을 AI 엔진에 넣으면 대체로 비슷하게 나오지만 항상 같지는 않다.
