# hardware/mock — 시나리오 더미 데이터

실물 센서가 오기 전까지 쓰는 더미 데이터. 설명과 실행 방법은 [`../server/README.md`](../server/README.md).

```bash
python -m hardware.mock.generator --list      # 시나리오 목록
python -m hardware.mock.generator --all       # data/synthetic/mock_*.csv + .json (수집 CSV와 같은 47칸)
```

시나리오를 추가하려면 `scenarios.py`의 `SCENARIOS`에 `_scenario(이름, 설명, Step(...), ...)`를 더한다.
`Step(초, seat=, head=, tilt=, distance_fail=, camera_lost=, pressure_drop=)` — 라벨 값은 `common/schema.py`에 있는 것만.
