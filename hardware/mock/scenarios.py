"""더미 데이터 시나리오 — '몇 초 동안 어떤 자세'를 순서대로 적은 목록

시나리오 하나 = Step 여러 개. 맨 앞은 항상 기준 측정(바른 자세 baseline_seconds초).
같은 시나리오를 두 가지로 쓴다 (hardware/mock/generator.py):
    - mock 생성기: 실시간으로 Sample을 만든다 (mock 서버)
    - replay CSV:  수집 CSV와 같은 47칸 형식 파일로 저장 (main.py --mode replay)

라벨 값은 common/schema.py에 있는 것만 쓴다.
센서 오류도 Step에 적어서 흉내 낸다 (거리 측정 실패, 카메라 미인식, 압력 순간 끊김).
"""
from __future__ import annotations

from dataclasses import dataclass

from common.schema import Phase, check_labels


@dataclass(frozen=True)
class Step:
    seconds: float
    seat: str = "normal"
    head: str = "normal"
    tilt: str = "none"
    phase: str = Phase.RECORD.value
    distance_fail: float = 0.0      # 거리 측정이 실패할 확률 (0~1)
    camera_lost: bool = False       # 카메라가 사람을 못 찾음 (pose_detected = 0)
    pressure_drop: float = 0.0      # 압력 읽기가 실패해서 그 순간을 건너뛸 확률 (0~1)

    def __post_init__(self):
        check_labels(self.seat, self.head, self.tilt)
        if self.seconds <= 0:
            raise ValueError(f"Step.seconds는 0보다 커야 함 (지금: {self.seconds})")
        for name in ("distance_fail", "pressure_drop"):
            if not 0.0 <= getattr(self, name) <= 1.0:
                raise ValueError(f"Step.{name}는 0~1 (지금: {getattr(self, name)})")

    @property
    def faults(self) -> list[str]:
        """이 구간에 흉내 내는 센서 오류 이름"""
        out = []
        if self.distance_fail:
            out.append(f"distance_fail({self.distance_fail:g})")
        if self.camera_lost:
            out.append("camera_lost")
        if self.pressure_drop:
            out.append(f"pressure_drop({self.pressure_drop:g})")
        return out


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    steps: tuple[Step, ...]

    @property
    def total_sec(self) -> float:
        return sum(s.seconds for s in self.steps)

    def step_at(self, elapsed: float) -> tuple[int, Step]:
        """시작 후 elapsed초에 해당하는 Step (끝을 넘으면 마지막 Step)"""
        t = 0.0
        for i, s in enumerate(self.steps):
            t += s.seconds
            if elapsed < t:
                return i, s
        return len(self.steps) - 1, self.steps[-1]


def _scenario(name: str, description: str, *steps: Step, baseline_sec: float = 10) -> Scenario:
    """맨 앞에 기준 측정 구간을 붙인다"""
    base = Step(baseline_sec, phase=Phase.BASELINE.value)
    return Scenario(name, description, (base, *steps))


SCENARIOS: dict[str, Scenario] = {s.name: s for s in [
    # --- 자세 하나씩 (앞 10초 바른 자세 → 그 자세 40초) ---
    _scenario("normal", "바른 자세 유지", Step(50)),
    _scenario("forward_head", "거북목", Step(10), Step(40, head="forward")),
    _scenario("tilt_left", "상체 왼쪽 기울기", Step(10), Step(40, tilt="left")),
    _scenario("tilt_right", "상체 오른쪽 기울기", Step(10), Step(40, tilt="right")),
    _scenario("lean_left", "왼쪽 체중 편향", Step(10), Step(40, seat="lean_left")),
    _scenario("lean_right", "오른쪽 체중 편향", Step(10), Step(40, seat="lean_right")),
    _scenario("cross_left", "왼다리 꼬기", Step(10), Step(40, seat="cross_left")),
    _scenario("cross_right", "오른다리 꼬기", Step(10), Step(40, seat="cross_right")),
    _scenario("empty", "자리 비움 후 복귀", Step(10), Step(30, seat="empty"), Step(10)),

    # --- 복합 ---
    _scenario("cross_forward", "왼다리 꼬기 + 거북목 (동시에 일어남)",
              Step(10), Step(40, seat="cross_left", head="forward")),

    # --- 센서 오류 ---
    _scenario("distance_fail", "거리 측정 실패 (가끔 → 계속)",
              Step(10), Step(20, distance_fail=0.5), Step(10, distance_fail=1.0), Step(10)),
    _scenario("camera_lost", "카메라 미인식 (조명·가림)",
              Step(10), Step(20, camera_lost=True), Step(10)),
    _scenario("pressure_dropout", "압력 읽기 순간 끊김",
              Step(10), Step(20, pressure_drop=0.3), Step(10)),

    # --- 데모용 이어 보기 (약 4분 40초) ---
    _scenario("demo", "여러 자세를 차례로 (앱 화면 확인용)",
              Step(30),
              Step(30, head="forward"),
              Step(15),
              Step(30, seat="cross_left"),
              Step(20, seat="lean_right"),
              Step(20, tilt="left"),
              Step(20, seat="empty"),
              Step(20),
              Step(30, seat="cross_right", head="forward"),
              Step(15, distance_fail=0.5),
              Step(15, camera_lost=True),
              Step(15)),
]}


def get_scenario(name: str) -> Scenario:
    try:
        return SCENARIOS[name]
    except KeyError:
        raise ValueError(f"시나리오 '{name}'는 없음. 가능: {list(SCENARIOS)}") from None
