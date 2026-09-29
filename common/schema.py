"""라벨 목록 + 카메라 점 목록 + 판단 결과 형식 (AI → 하드웨어(서버) → 앱 약속)

- 라벨 이름은 반드시 여기 있는 값만 쓴다 (수집 CSV, 학습, DB, 앱 표시 모두).
- AI 엔진은 PostureEvent를 만들고, 하드웨어 팀원은 to_dict() 결과를 DB에 저장해 앱에 넘긴다.
  DB에서 다시 읽을 때는 PostureEvent.from_dict()를 쓰면 값 검사까지 된다.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from enum import Enum


class LabelError(ValueError):
    """허용되지 않은 라벨·상태 값"""


# ---------------------------------------------------------------------------
# 상태(라벨) 값
# ---------------------------------------------------------------------------
class SeatState(str, Enum):
    """좌면 자세 (압력센서)"""
    NORMAL = "normal"
    LEAN_LEFT = "lean_left"         # 왼쪽 체중 편향
    LEAN_RIGHT = "lean_right"
    CROSS_LEFT = "cross_left"       # 왼다리를 위로 꼰 다리 꼬기
    CROSS_RIGHT = "cross_right"     # 오른다리를 위로 꼰 다리 꼬기
    EMPTY = "empty"                 # 자리 비움
    UNKNOWN = "unknown"             # 판단 불가 (판단 결과에서만 사용)


class HeadState(str, Enum):
    """목 전방 자세 (카메라 + 거리센서)"""
    NORMAL = "normal"
    FORWARD = "forward"             # 거북목
    UNKNOWN = "unknown"


class TiltState(str, Enum):
    """상체 좌우 기울어짐 (카메라). 방향은 사람 기준"""
    NONE = "none"
    LEFT = "left"
    RIGHT = "right"
    UNKNOWN = "unknown"


class Phase(str, Enum):
    """수집 단계"""
    BASELINE = "baseline"           # 세션 시작 후 바른 자세 기준 측정 (기본 10초)
    RECORD = "record"               # 실제 자세 수집


class DistanceLevel(str, Enum):
    """화면 거리 단계 (기준값은 config.yaml의 thresholds)"""
    OK = "ok"                       # 50cm 이상 — OSHA(50~100cm)·AOA(50~70cm) 권장 하한
    CAUTION = "caution"             # 40~50cm — 국내 VDT 지침(40cm 이상)은 지키지만 권장보다 가까움
    WARNING = "warning"             # 40cm 미만 — 국내 VDT 작업관리지침 미달
    UNKNOWN = "unknown"             # 측정 실패


# 수집할 때 쓸 수 있는 라벨 (unknown 제외)
SEAT_LABELS = [s.value for s in SeatState if s is not SeatState.UNKNOWN]
HEAD_LABELS = [s.value for s in HeadState if s is not HeadState.UNKNOWN]
TILT_LABELS = [s.value for s in TiltState if s is not TiltState.UNKNOWN]

# 기준 측정(baseline) 구간에서 쓰는 바른 자세 라벨
BASELINE_LABELS = {"seat": SeatState.NORMAL.value, "head": HeadState.NORMAL.value,
                   "tilt": TiltState.NONE.value}


def check_labels(seat: str, head: str, tilt: str) -> None:
    """수집 라벨 3개가 허용된 값인지 검사. 틀리면 LabelError"""
    for value, allowed, what in ((seat, SEAT_LABELS, "seat"), (head, HEAD_LABELS, "head"),
                                 (tilt, TILT_LABELS, "tilt")):
        if value not in allowed:
            raise LabelError(f"{what} 라벨 '{value}'는 쓸 수 없음. 가능: {allowed}")


def parse_state(enum_cls: type[Enum], value) -> Enum:
    """문자열 → 상태 Enum. 틀리면 LabelError (DB·CSV에서 읽을 때 사용)"""
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(value)
    except ValueError:
        allowed = [e.value for e in enum_cls]
        raise LabelError(f"{enum_cls.__name__}에 '{value}' 값은 없음. 가능: {allowed}") from None


def merge_cross(label: str) -> str:
    """cross_left / cross_right → cross (다리 꼬기 방향 없이 학습할 때)"""
    if label in (SeatState.CROSS_LEFT.value, SeatState.CROSS_RIGHT.value):
        return "cross"
    return label


def classify_distance(distance_mm: float | None, ok_mm: float = 500,
                      warning_mm: float = 400) -> DistanceLevel:
    """거리(mm) → 단계. 측정 실패(None, NaN, 0 이하)는 UNKNOWN"""
    if warning_mm >= ok_mm:
        raise ValueError(f"warning_mm({warning_mm})는 ok_mm({ok_mm})보다 작아야 함")
    if distance_mm is None or not math.isfinite(distance_mm) or distance_mm <= 0:
        return DistanceLevel.UNKNOWN
    if distance_mm >= ok_mm:
        return DistanceLevel.OK
    if distance_mm >= warning_mm:
        return DistanceLevel.CAUTION
    return DistanceLevel.WARNING


# ---------------------------------------------------------------------------
# 카메라 점 (MediaPipe Pose 번호)
# PosturePal(MIT)의 상체 8개 점 = 아래 7개 + 목(두 어깨의 중점, 계산)
# ---------------------------------------------------------------------------
LANDMARKS: dict[str, int] = {
    "nose": 0,
    "left_eye": 2,
    "right_eye": 5,
    "left_ear": 7,          # 머리카락에 가려질 수 있음 → visibility 보고 사용 여부 결정
    "right_ear": 8,
    "left_shoulder": 11,
    "right_shoulder": 12,
}
LANDMARK_NAMES: list[str] = list(LANDMARKS)


# ---------------------------------------------------------------------------
# 판단 결과
# ---------------------------------------------------------------------------
@dataclass
class PostureEvent:
    """기록 한 줄 = '언제부터 언제까지 어떤 자세였나'"""
    start: float                    # 시작 시각 (time.time())
    end: float                      # 끝 시각
    seat_state: SeatState
    head_state: HeadState
    tilt_state: TiltState
    distance_mm: float | None       # 이 구간 평균 화면 거리 (앱에서 cm로 표시)
    distance_level: DistanceLevel
    confidence: float               # 0~1

    def __post_init__(self):
        # 문자열로 들어와도 Enum으로 바꾸고, 틀린 값이면 LabelError
        self.seat_state = parse_state(SeatState, self.seat_state)
        self.head_state = parse_state(HeadState, self.head_state)
        self.tilt_state = parse_state(TiltState, self.tilt_state)
        self.distance_level = parse_state(DistanceLevel, self.distance_level)
        for name in ("start", "end", "confidence"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise ValueError(f"PostureEvent.{name}는 숫자여야 함 (지금: {v!r})")
        if self.end < self.start:
            raise ValueError(f"PostureEvent: end({self.end})가 start({self.start})보다 빠름")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"PostureEvent.confidence는 0~1 (지금: {self.confidence})")
        if self.distance_mm is not None and (
                not math.isfinite(self.distance_mm) or self.distance_mm < 0):
            raise ValueError(f"PostureEvent.distance_mm가 잘못됨 (지금: {self.distance_mm})")

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("seat_state", "head_state", "tilt_state", "distance_level"):
            d[k] = getattr(self, k).value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "PostureEvent":
        """to_dict() 결과(또는 DB에서 읽은 dict) → PostureEvent. 빠진 필드가 있으면 ValueError"""
        fields = ("start", "end", "seat_state", "head_state", "tilt_state",
                  "distance_mm", "distance_level", "confidence")
        missing = [f for f in fields if f not in d]
        if missing:
            raise ValueError(f"PostureEvent에 필요한 필드가 없음: {missing}")
        return cls(**{f: d[f] for f in fields})
