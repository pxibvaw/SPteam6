"""더미 데이터 시나리오 — '몇 초 동안 어떤 자세'를 순서대로 적은 목록

시나리오 하나 = Step 여러 개. 맨 앞은 항상 기준 측정(바른 자세 baseline_seconds초).
같은 시나리오를 두 가지로 쓴다 (hardware/mock/generator.py):
    - mock 생성기: 실시간으로 Sample을 만든다 (mock 서버)
    - replay CSV:  수집 CSV와 같은 형식 파일로 저장 (main.py --mode replay)
                   열 수는 압력 채널 수에 따라 다름 (6채널 45칸, 8채널 47칸)

라벨 값은 common/schema.py에 있는 것만 쓴다. 라벨 = 그 순간 사람이 실제로 한 자세(정답).
unknown은 라벨로 쓰지 않고, 센서 상태(pose_detected, distance_status, 빠진 줄)로 드러난다.
센서 오류도 Step에 적어서 흉내 낸다 (거리 실패·튐, 카메라 미인식, 압력 끊김).
"""
from __future__ import annotations

from dataclasses import dataclass

from common.schema import LANDMARK_NAMES, Phase, SeatState, check_labels
from hardware.server.seating import PRIORITY, priority_rank   # noqa: F401 — 대표 자세 우선순위 (한 곳에서 관리)


@dataclass(frozen=True)
class Step:
    seconds: float
    seat: str = "normal"
    head: str = "normal"
    tilt: str = "none"
    phase: str = Phase.RECORD.value
    extra_lean: str | None = None   # "left"/"right": 다리 꼬기와 함께 생긴 체중 편향 (압력 모양에만 반영)
                                    # seat 라벨은 하나만 쓸 수 있어서 우선순위가 높은 cross_*를 라벨로 쓴다
    distance_fail: float = 0.0      # 거리 측정이 실패할 확률 (0~1) → distance_mm=None, status=-1
    distance_spike: float = 0.0     # 거리 값이 튈 확률 (0~1, HC-SR04 노이즈) → status=0인데 틀린 값
    camera_lost: bool = False       # 카메라가 사람을 못 찾음 (pose_detected = 0)
    hidden_points: tuple[str, ...] = ()   # 이 점만 안 보임 (예: 머리카락에 가린 귀) → 그 점만 빈칸
    pressure_drop: float = 0.0      # 압력 읽기가 실패해서 그 순간을 건너뛸 확률 (0~1)
    dead_channel: int | None = None # 이 FSR 채널의 연결이 끊김 → 그 채널만 0 근처

    def __post_init__(self):
        check_labels(self.seat, self.head, self.tilt)
        if self.seconds <= 0:
            raise ValueError(f"Step.seconds는 0보다 커야 함 (지금: {self.seconds})")
        for name in ("distance_fail", "distance_spike", "pressure_drop"):
            if not 0.0 <= getattr(self, name) <= 1.0:
                raise ValueError(f"Step.{name}는 0~1 (지금: {getattr(self, name)})")
        if self.extra_lean is not None:
            if self.extra_lean not in ("left", "right"):
                raise ValueError(f"Step.extra_lean은 left 또는 right (지금: {self.extra_lean!r})")
            if not self.seat.startswith("cross_"):
                raise ValueError("Step.extra_lean은 seat가 cross_*일 때만 (체중 편향만이면 seat=lean_*)")
        bad = [p for p in self.hidden_points if p not in LANDMARK_NAMES]
        if bad:
            raise ValueError(f"Step.hidden_points에 없는 부위: {bad}. 가능: {LANDMARK_NAMES}")

    @property
    def empty(self) -> bool:
        return self.seat == SeatState.EMPTY.value

    @property
    def faults(self) -> list[str]:
        """이 구간에 흉내 내는 센서 오류 이름"""
        out = []
        if self.distance_fail:
            out.append(f"distance_fail({self.distance_fail:g})")
        if self.distance_spike:
            out.append(f"distance_spike({self.distance_spike:g})")
        if self.camera_lost:
            out.append("camera_lost")
        if self.hidden_points:
            out.append(f"hidden_points({','.join(self.hidden_points)})")
        if self.pressure_drop:
            out.append(f"pressure_drop({self.pressure_drop:g})")
        if self.dead_channel is not None:
            out.append(f"dead_channel(p{self.dead_channel})")
        return out

    @property
    def postures(self) -> list[str]:
        """이 구간의 나쁜 자세 전부 (우선순위 순서)"""
        if self.phase == Phase.BASELINE.value or self.empty:
            return []
        found = []
        if self.head == "forward":
            found.append("forward_head")
        if self.seat.startswith("cross_"):
            found.append(self.seat)
        if self.seat.startswith("lean_"):
            found.append(self.seat)
        if self.extra_lean:
            found.append(f"lean_{self.extra_lean}")
        if self.tilt != "none":
            found.append(f"tilt_{self.tilt}")
        return sorted(found, key=priority_rank)

    @property
    def dominant(self) -> str:
        """대표 자세: empty / normal / 우선순위가 가장 높은 나쁜 자세"""
        if self.empty:
            return "empty"
        return self.postures[0] if self.postures else "normal"


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


def _repeat(times: int, *steps: Step) -> list[Step]:
    return [s for _ in range(times) for s in steps]


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
    _scenario("empty", "자리 비움 30초 후 복귀 (5초 뒤 카메라·거리 꺼짐)",
              Step(10), Step(30, seat="empty"), Step(10)),

    # --- 복합 (우선순위: 거북목 > 다리 꼬기 > 체중 편향 > 기울기) ---
    _scenario("cross_forward", "거북목 + 왼다리 꼬기 → 대표: 거북목",
              Step(10), Step(40, seat="cross_left", head="forward")),
    _scenario("cross_lean", "오른다리 꼬기 + 오른쪽 체중 편향 → 대표: 다리 꼬기",
              Step(10), Step(40, seat="cross_right", extra_lean="right")),
    _scenario("lean_tilt", "왼쪽 체중 편향 + 왼쪽 기울기 → 대표: 체중 편향",
              Step(10), Step(40, seat="lean_left", tilt="left")),
    _scenario("all_at_once", "거북목 + 오른다리 꼬기 + 오른쪽 편향 + 오른쪽 기울기 → 대표: 거북목",
              Step(10), Step(40, seat="cross_right", extra_lean="right", head="forward", tilt="right")),

    # --- 경계 ---
    # 3초 필터(short_filter_sec) 확인. 10Hz라 정확히 3.0초는 줄 하나 차이로 갈려서 2.9 / 3.5초로 나눔
    _scenario("short_blip", "3초 미만 순간 나쁜 자세 (2.9초 ×3, 1초 ×1 → 인정 X / 마지막 3.5초 → 인정)",
              Step(10), Step(2.9, head="forward"),
              Step(10), Step(2.9, seat="cross_left"),
              Step(10), Step(2.9, tilt="left"),
              Step(10), Step(1.0, seat="lean_right"),
              Step(10), Step(3.5, head="forward"),
              Step(10)),
    _scenario("empty_4s", "4초 자리 비움 → 착석 구간 유지 (센서 계속 켜짐)",
              Step(20), Step(4, seat="empty"), Step(20)),
    _scenario("empty_6s", "6초 자리 비움 → 5초째 착석 구간 종료, 카메라·거리 꺼짐",
              Step(20), Step(6, seat="empty"), Step(20)),
    _scenario("sit_stand", "앉았다 일어남 반복 (15초 앉음·2초 비움 ×3 → 10초 앉음·7초 비움 ×2)",
              *_repeat(3, Step(15), Step(2, seat="empty")),
              *_repeat(2, Step(10), Step(7, seat="empty")),
              Step(10)),

    # --- 센서 오류 ---
    _scenario("distance_fail", "거리 측정 실패 (가끔 → 계속)",
              Step(10), Step(20, distance_fail=0.5), Step(10, distance_fail=1.0), Step(10)),
    _scenario("distance_spike", "거리 값 튐 (HC-SR04 노이즈: 범위 밖 값 + 범위 안 틀린 값)",
              Step(10), Step(30, distance_spike=0.08), Step(10, head="forward", distance_spike=0.08),
              Step(10)),
    _scenario("camera_lost", "카메라 미인식 15초 → 귀만 안 보임 15초",
              Step(10), Step(15, camera_lost=True), Step(10),
              Step(15, hidden_points=("left_ear", "right_ear")), Step(10)),
    _scenario("pressure_dropout", "압력 읽기 순간 끊김 (줄 건너뜀) → p1 채널 2초 끊김",
              Step(10), Step(20, pressure_drop=0.3), Step(10), Step(2, dead_channel=1), Step(10)),

    # --- 데모용 이어 보기 (5분, 바른 자세 약 70%) ---
    _scenario("demo", "여러 자세를 차례로 5분 (앱 화면 확인용)",
              Step(50),
              Step(25, head="forward"),
              Step(40),
              Step(25, seat="cross_left"),
              Step(30),
              Step(20, seat="empty"),
              Step(40),
              Step(20, seat="cross_right", head="forward"),
              Step(25),
              Step(2, head="forward"),      # 순간 거북목 (3초 미만 → 인정 안 돼야 함)
              Step(13)),
]}


def get_scenario(name: str) -> Scenario:
    try:
        return SCENARIOS[name]
    except KeyError:
        raise ValueError(f"시나리오 '{name}'는 없음. 가능: {list(SCENARIOS)}") from None
