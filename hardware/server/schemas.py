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
from hardware.server.seating import EndReason, PostureStatus

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
    waiting_seat: bool = Field(False, description="측정을 요청했지만 앉기를 기다리는 중 (이때 state는 measuring)")
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


class SeatingSegment(BaseModel):
    """착석 구간 한 개. 자리 비움이 5초 이어지면 끝나고, 끝 시각 = 판단 시각 − 5초"""
    start: float = Field(..., description="착석 시작 시각 (압력이 처음 들어온 순간)")
    end: float | None = Field(None, description="착석 끝 시각. 진행 중이면 null")
    end_reason: EndReason | None = Field(None, description="away 자리 비움 5초 / stop 정지 버튼. 진행 중이면 null")
    duration_sec: float | None = Field(None, description="구간 길이 (진행 중이면 지금까지)")
    first_abnormal_sec: float | None = Field(
        None, description="구간 시작 → 첫 비정상 자세 확정(3초 유지)까지 초. 아직 없으면 null")
    normal_sec: float = Field(..., description="정상 시간 (목 normal + 기울기 none + 좌면 normal)")
    abnormal_sec: float = Field(..., description="비정상 시간 (나쁜 자세가 하나라도 확정)")
    unknown_sec: float = Field(..., description="판단 못 한 시간 (카메라 미인식·켜는 중, 기준 측정 전, 5초 미만 자리 비움)")


class Seating(BaseModel):
    """지금 착석 구간 — /current의 seated(3초 필터)와 달리 '자리 비움 5초' 규칙을 따른다"""
    ts: float
    seated: bool = Field(..., description="착석 구간 안인지 (5초 미만 자리 비움이면 아직 true)")
    segment: SeatingSegment | None = Field(None, description="진행 중인 구간. 구간 밖이면 null")
    status: PostureStatus | None = Field(None, description="지금 순간 normal / abnormal / unknown. 구간 밖이면 null")
    dominant: str | None = Field(None, description="대표 자세 (postures[0] / normal / unknown / away). 구간 밖이면 null")
    away_sec: float | None = Field(None, description="구간 중 자리 비움이 이어진 초. 앉아 있으면 null")
    end_in_sec: float | None = Field(None, description="이대로 비어 있으면 몇 초 뒤 구간이 끝나는지")


class SeatingSegments(BaseModel):
    """끝난 착석 구간 목록 (오래된 것부터). 지금은 서버 메모리에만 있음"""
    hours: int
    items: list[SeatingSegment]


class ErrorDetail(BaseModel):
    code: str = Field(..., description="앱이 처리할 오류 코드 (CLOCK_NOT_SYNCED, CLOCK_BACKWARD, "
                                       "CLOCK_JUMP_IN_SESSION, CLOCK_BEFORE_LAST_RECORD, SESSION_ACTIVE, "
                                       "NOT_RUNNING, NOT_PAUSED, NO_SESSION, CONFIRM_REQUIRED, INVALID_TIME, "
                                       "INVALID_TIMEZONE)")
    message: str = Field(..., description="개발자용 설명 (앱 화면 문구는 앱이 만든다)")
    resync_required: bool = Field(..., description="true면 POST /time/sync를 다시 보내야 함")
    boot_id: str = Field(..., description="서버가 켜질 때마다 바뀌는 값. 앱이 기억한 값과 다르면 재시작된 것")
    last_record_at: float | None = Field(
        None, description="CLOCK_BEFORE_LAST_RECORD일 때 Pi의 마지막 기록 시각 (앱이 날짜를 보여줄 때)")


class ErrorResponse(BaseModel):
    """오류 응답 (409 상태 충돌, 422 잘못된 값)"""
    detail: ErrorDetail


class TimeSync(BaseModel):
    """앱 → Pi: 현재 시각과 시간대. 연결할 때와 세션 시작 전마다 보낸다"""
    app_time: float = Field(..., description="앱의 현재 시각 (time.time()과 같은 초, 소수 가능)",
                            examples=[1791207600.123])
    timezone: str = Field("Asia/Seoul", description="시간대 (Asia/Seoul만 가능)", examples=["Asia/Seoul"])


class TimeStatus(BaseModel):
    """Pi의 시각 동기화 상태. Pi OS 시계는 바꾸지 않고 '앱 시각 − Pi 단조시계' 차이만 저장"""
    synced: bool = Field(..., description="이 서버가 켜진 뒤 동기화했는지")
    boot_id: str = Field(..., description="서버가 켜질 때마다(재부팅 포함) 바뀜. 바뀌었으면 다시 동기화")
    timezone: str | None = None
    app_time: float | None = Field(None, description="앱 기준 지금 시각. 동기화 전이면 null")
    local_time: str | None = Field(None, description="app_time을 시간대로 바꾼 ISO 문자열 (확인용)")
    synced_at: float | None = Field(None, description="마지막 동기화 때 앱이 보낸 시각")
    resync_required: bool = Field(..., description="true면 POST /time/sync 필요")
    offset_change_sec: float | None = Field(
        None, description="다시 동기화했을 때 시각이 바뀐 양 (초). 첫 동기화·조회에서는 null")
    applied: bool | None = Field(
        None, description="이번 동기화를 적용했는지. 0.5초 이내로 뒤로 가는 변경은 false(시각 그대로). 조회에서는 null")
    message: str | None = None


class SensorPower(str, Enum):
    on = "on"
    off = "off"
    warming = "warming"


class SessionSensors(BaseModel):
    """센서 전원 (자리 비움 5초면 카메라·거리 끔, 일시정지면 압력만, 정지면 모두 끔)"""
    pressure: SensorPower
    camera: SensorPower = Field(..., description="warming = 켰지만 아직 사람을 찾기 전 (약 2초)")
    distance: SensorPower


class SessionState(str, Enum):
    idle = "idle"
    running = "running"
    paused = "paused"


class BaselinePhase(str, Enum):
    none = "none"
    waiting_seat = "waiting_seat"
    measuring = "measuring"
    ready = "ready"
    failed = "failed"


class SessionBaseline(BaseModel):
    """세션의 기준 자세 — 시작 버튼을 누르면 앉은 게 확인된 뒤 10초 측정 (재개 때는 다시 재지 않음)"""
    state: BaselinePhase = Field(..., description="none 기준 없음 / waiting_seat 앉기를 기다림 / measuring 측정 중 / "
                                                  "ready 기준 있음 / failed 마지막 측정 실패")
    kind: str | None = Field(None, description="측정 중이거나 마지막 측정의 종류 (initial / session / recalibration)")
    remaining_sec: float | None = Field(None, description="측정 중이면 남은 초 (앉기를 기다리는 중이면 측정 시간 전체)")
    fail_reason: str | None = Field(None, description="실패 이유 (no_person / sensor_lost / too_much_motion)")
    using_previous: bool = Field(..., description="이번 측정이 실패해서 직전 성공 기준으로 판단 중인지")
    measured_at: float | None = Field(None, description="지금 쓰는 기준을 잰 시각")


class SessionStatus(BaseModel):
    """측정 세션 상태 — 홈 하단 측정 바"""
    state: SessionState = Field(..., description="idle 세션 없음 / running 측정 중 / paused 일시정지")
    session_id: str | None = Field(None, description="마지막(또는 지금) 세션 ID. 한 번도 안 했으면 null")
    started_at: float | None = None
    ended_at: float | None = Field(None, description="정지한 시각. 진행 중이면 null")
    elapsed_sec: float | None = Field(None, description="경과 시간 (일시정지 시간 제외)")
    paused_sec: float | None = Field(None, description="일시정지한 시간 합")
    seated_sec: float | None = Field(None, description="이 세션의 착석 시간 (착석 구간 합, 일시정지 제외)")
    seated_now: bool | None = Field(None, description="지금 압력이 있는지 (일시정지 중에도). 압력센서가 꺼져 있으면 null")
    sensors: SessionSensors
    clock_synced: bool
    boot_id: str
    end_reason: str | None = Field(None, description="마지막 세션이 끝난 이유 (stop / baseline_failed). 진행 중이면 null")
    baseline: SessionBaseline


class RecordsReset(BaseModel):
    """기록 초기화 요청 — 앱에서 확인 절차를 거친 뒤 보낸다"""
    confirm: str = Field(..., description="반드시 'DELETE_RECORDS'", examples=["DELETE_RECORDS"])


class RecordsResetResult(BaseModel):
    deleted: dict[str, int] = Field(..., description="테이블별 지운 줄 수 (설정·기준 자세는 유지)")
    resync_required: bool = Field(..., description="true — 시각 동기화를 지웠으니 POST /time/sync를 다시 보내야 함")


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
