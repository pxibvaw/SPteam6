"""앱에 주는 JSON 형식 (FastAPI 응답 모델) — 여기서 openapi.yaml이 만들어진다

필드는 AI 엔진(ai 브랜치 ai/engine/upper_body.py)의 판단 결과를 근거로 정했다.
    UpperJudgement.head / tilt               → head_state / tilt_state
    head_confidence / tilt_confidence        → confidence.head / confidence.tilt
    deltas (기준 대비 변화량)                  → deltas
    cues (어떤 단서가 기준값을 넘었는지)        → cues
    StateFilter.pending (바뀌기를 기다리는 상태) → pending
    Baseline.ready / remaining               → /baseline
    ai/features/pressure.center_of_pressure  → center_of_pressure

규칙: 서버는 숫자 원본만 준다 (시각 = time.time() 초, 거리 mm, 각도 °, 비율 0~1).
"52cm", "5분째" 같은 글자는 앱이 만든다. 영상 이미지는 어떤 응답에도 넣지 않는다.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from common.schema import DistanceLevel, HeadState, SeatState, TiltState

API_VERSION = "0.1.0-mock"


class Confidence(BaseModel):
    seat: float = Field(..., ge=0, le=1, description="좌면 판단 확신도 (0~1)")
    head: float = Field(..., ge=0, le=1, description="목 판단 확신도 (0~1). 카메라 미인식이면 0")
    tilt: float = Field(..., ge=0, le=1, description="기울기 판단 확신도 (0~1). 카메라 미인식이면 0")


class Deltas(BaseModel):
    """기준 자세 대비 변화량. 카메라 미인식·기준값 없음이면 null"""
    neck_drop: float | None = Field(None, description="목 높이가 기준보다 줄어든 비율. +0.12 이상이면 거북목 단서")
    face_grow: float | None = Field(None, description="얼굴 크기가 기준보다 커진 비율. +0.08 이상이면 거북목 단서")
    closer_mm: float | None = Field(None, description="기준보다 화면에 가까워진 거리 (mm). +면 가까워짐")
    shoulder_angle: float | None = Field(None, description="어깨 선 기울기 변화 (°). + 사람 왼쪽 / - 오른쪽")
    head_angle: float | None = Field(None, description="눈 선 기울기 변화 (°). + 왼쪽 / - 오른쪽")
    head_offset: float | None = Field(None, description="코의 좌우 이동 (어깨 너비 대비 비율). + 왼쪽 / - 오른쪽")


class Cues(BaseModel):
    """각 단서가 기준값(threshold)을 넘었는지. 판단할 수 없으면 null"""
    neck_drop: bool | None = None
    face_grow: bool | None = None
    closer: bool | None = None
    shoulder_angle: bool | None = None
    head_angle: bool | None = None
    head_offset: bool | None = None


class StateKind(str, Enum):
    seat = "seat"
    head = "head"
    tilt = "tilt"


class Pending(BaseModel):
    """새 자세가 나타났지만 아직 short_filter_sec(3초)가 안 지나 확정되지 않은 것"""
    kind: StateKind = Field(..., description="어떤 판단인지 (seat / head / tilt)")
    state: str = Field(..., description="바뀔 상태 값 (예: forward)")
    remaining_sec: float = Field(..., ge=0, description="확정까지 남은 초")


class Point(BaseModel):
    x: float
    y: float


class Current(BaseModel):
    """지금 자세 — 홈 화면용, 1~3초마다 호출"""
    ts: float = Field(..., description="측정 시각 (time.time() 초)")
    seated: bool = Field(..., description="앉아 있는지 (seat_state != empty)")
    sitting_since: float | None = Field(None, description="이번 연속 착석 시작 시각. 자리 비움이면 null")
    state_since: float = Field(..., description="지금 자세(좌면·목·기울기)가 시작된 시각")
    seat_state: SeatState
    head_state: HeadState
    tilt_state: TiltState
    confidence: Confidence
    postures: list[str] = Field(..., description="지금 나타난 나쁜 자세 (cross_left, cross_right, lean_left, "
                                                 "lean_right, forward_head, tilt_left, tilt_right). 없으면 []")
    pending: list[Pending] = Field(..., description="확정 대기 중인 자세 변화")
    deltas: Deltas
    cues: Cues
    distance_mm: int | None = Field(None, description="화면 거리 (mm). 측정 실패면 null")
    distance_level: DistanceLevel = Field(..., description="ok 50cm 이상 / caution 40~50cm / warning 40cm 미만 / unknown")
    closer_than_baseline: bool | None = Field(None, description="기준보다 10cm 이상 가까운지. 기준값 없으면 null")
    pressure: list[int] = Field(..., description="FSR 채널별 원래 값 (0~1023). 순서 = config.yaml pressure.positions")
    pressure_ratio: list[float] | None = Field(None, description="채널별 압력 비율 (합 1). 자리 비움이면 null")
    center_of_pressure: Point | None = Field(None, description="압력 중심. x 왼쪽 -1 ~ 오른쪽 +1, "
                                                               "y 뒤 -1 ~ 앞 +1 (사람 기준). 자리 비움이면 null")
    pose_detected: bool = Field(..., description="카메라가 사람을 찾았는지")
    camera_age_sec: float | None = Field(None, description="카메라 결과가 몇 초 전 것인지 (초당 약 5장)")
    baseline_ready: bool = Field(..., description="기준 자세가 있는지. false면 deltas 대부분이 null")


class HistoryItem(BaseModel):
    ts: float
    seated: bool
    seat_state: SeatState
    head_state: HeadState
    tilt_state: TiltState
    distance_mm: int | None
    distance_level: DistanceLevel


class History(BaseModel):
    """최근 판단 기록 (1초에 1개)"""
    seconds: int
    items: list[HistoryItem]


class BaselineState(str, Enum):
    none = "none"
    measuring = "measuring"
    ready = "ready"


class Baseline(BaseModel):
    """기준(바른) 자세 측정값. 측정 중에도 이전 기준값을 준다"""
    state: BaselineState = Field(..., description="none 측정 전 / measuring 측정 중 / ready 완료")
    remaining_sec: float | None = Field(None, description="측정 중이면 남은 초")
    created_at: float | None = Field(None, description="기준값을 만든 시각")
    seconds: float | None = Field(None, description="측정 시간")
    pressure: list[float] | None = Field(None, description="채널별 평균 압력")
    distance_mm: float | None = Field(None, description="기준 화면 거리 (중앙값)")
    pose: dict[str, list[float]] | None = Field(
        None, description="부위 7개의 평균 좌표 [x, y, z, visibility] (0~1 비율, 영상 아님)")


class CalibrateResult(BaseModel):
    started: bool
    seconds: float = Field(..., description="측정에 걸리는 초 (이 동안 바른 자세로 앉기)")


class SensorStatus(BaseModel):
    ok: bool = Field(..., description="최근 2초 안에 정상 값을 읽었는지")
    detail: str | None = None


class Sensors(BaseModel):
    pressure: SensorStatus
    distance: SensorStatus
    camera: SensorStatus


class Status(BaseModel):
    api_version: str = API_VERSION
    mode: str = Field(..., description="mock(더미 데이터) / real. mock이면 앱에 '예시 데이터' 표시")
    server_time: float
    last_record_ts: float | None
    baseline_ready: bool
    sensors: Sensors
    scenario: str | None = Field(None, description="mock 모드에서 재생 중인 시나리오")
    scenario_elapsed_sec: float | None = None
    scenario_total_sec: float | None = None


class ScenarioStep(BaseModel):
    seconds: float
    phase: str
    seat: SeatState
    head: HeadState
    tilt: TiltState
    faults: list[str]


class ScenarioInfo(BaseModel):
    name: str
    description: str
    total_sec: float
    steps: list[ScenarioStep]


class ScenarioSelect(BaseModel):
    name: str = Field(..., examples=["demo"])
    loop: bool = Field(True, description="끝나면 처음부터 다시")
    seed: int | None = Field(None, description="같은 seed면 같은 값")
