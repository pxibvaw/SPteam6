"""센서 인터페이스 + 센서 값 형식 (하드웨어 ↔ AI 약속)

- 하드웨어 팀원: 센서 클래스를 상속해서 실제 센서 코드를 만든다 (hardware/sensors/).
  읽기에 실패하면 SensorError(또는 아무 예외)를 내면 된다. 처리는 SensorHub가 한다.
- AI 담당: 센서 종류(mock / replay / real)와 상관없이 SensorHub.read()로
  "한 순간의 모든 센서 값(Sample)"을 받아 판단한다.

시간(ts)은 모두 time.time() 기준 초 단위(float)로 통일한다.

실패 처리 규칙 (SensorHub):
    압력  실패 → SampleSkipped (압력 없는 줄은 쓸모가 없어서 그 순간은 건너뜀)
    거리  실패 → distance_mm=None, range_status=STATUS_READ_ERROR(-1)로 기록하고 계속
    카메라 실패 → detected=False로 기록하고 계속
    같은 센서가 max_consecutive_errors번 연속 실패하면 SensorError (배선·연결 확인 필요)
"""
from __future__ import annotations

import logging
import math
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import numpy as np

log = logging.getLogger(__name__)

STATUS_OK = 0                # VL53L1X range status 0 = 정상
STATUS_READ_ERROR = -1       # 센서 읽기 자체가 실패 (통신 오류 등) — 우리가 정한 값


class SensorError(Exception):
    """센서를 계속 읽을 수 없음 (연결·배선 확인 필요) → 실행을 멈춘다"""


class SampleSkipped(SensorError):
    """이번 순간만 압력을 못 읽음 → 이 순간은 건너뛰고 계속한다"""


# ---------------------------------------------------------------------------
# 센서 값 형식
# ---------------------------------------------------------------------------
@dataclass
class PressureReading:
    """FSR 406 × 8 → MCP3008"""
    ts: float                    # 측정 시각
    values: list[int]            # ADC 원래 값 0~1023, 길이 = pressure.n_channels
                                 # 채널 번호 ↔ 방석 위치는 config.yaml의 pressure.positions

    def validate(self, n_channels: int, adc_max: int) -> None:
        if len(self.values) != n_channels:
            raise SensorError(f"압력 채널 수가 {n_channels}개가 아님: {len(self.values)}개")
        for i, v in enumerate(self.values):
            if isinstance(v, bool) or not isinstance(v, (int, np.integer)):
                raise SensorError(f"압력 p{i} 값이 정수가 아님: {v!r}")
            if not 0 <= v <= adc_max:
                raise SensorError(f"압력 p{i} 값 {v}가 범위(0~{adc_max})를 벗어남 — 배선 확인")


@dataclass
class DistanceReading:
    """VL53L1X"""
    ts: float
    distance_mm: int | None      # 센서가 주는 그대로 mm. 측정 실패면 None
    range_status: int            # 0 = 정상, 그 외 = 실패·불확실, -1 = 읽기 실패

    @property
    def valid(self) -> bool:
        return self.range_status == STATUS_OK and self.distance_mm is not None

    @classmethod
    def failed(cls, ts: float) -> "DistanceReading":
        return cls(ts=ts, distance_mm=None, range_status=STATUS_READ_ERROR)


@dataclass
class FrameReading:
    """카메라 (정면). 이미지는 판단에만 쓰고 저장하지 않는다."""
    ts: float
    image: np.ndarray            # BGR 이미지, shape (H, W, 3) — OpenCV 형식


@dataclass
class Landmark:
    """MediaPipe 좌표 원래 값. x, y는 0~1 비율 (각도 계산 전 픽셀로 바꿀 것)
    화면 밖으로 조금 벗어나면 0보다 작거나 1보다 클 수도 있다."""
    x: float
    y: float
    z: float
    visibility: float            # 0~1, 보이는 정도 (가려지면 추측값이라 낮음)

    def is_finite(self) -> bool:
        return all(math.isfinite(v) for v in (self.x, self.y, self.z, self.visibility))


@dataclass
class PoseReading:
    """카메라 프레임에서 뽑은 상체 점 7개 (ai/camera가 만든다)"""
    ts: float                    # 이 좌표를 만든 프레임의 촬영 시각
    detected: bool               # 사람을 찾았는지
    points: dict[str, Landmark] = field(default_factory=dict)
    # 키: common.schema.LANDMARK_NAMES
    # (nose, left_eye, right_eye, left_ear, right_ear, left_shoulder, right_shoulder)
    # left/right는 사람 기준 (정면 카메라 화면에서는 반대쪽에 찍힘)

    @classmethod
    def empty(cls, ts: float) -> "PoseReading":
        return cls(ts=ts, detected=False, points={})

    def age(self, now_ts: float) -> float:
        """몇 초 전 프레임인지 (카메라가 멈췄는지 확인용)"""
        return now_ts - self.ts


@dataclass
class Sample:
    """한 순간의 모든 센서 값 = 수집 CSV 한 줄의 측정 부분"""
    ts: float
    pressure: PressureReading
    distance: DistanceReading
    pose: PoseReading | None     # 카메라는 느려서(초당 ~5장) 가장 최근 값. 카메라 없으면 None


# ---------------------------------------------------------------------------
# 센서 인터페이스
# ---------------------------------------------------------------------------
class Sensor(ABC):
    """모든 센서의 공통 틀. with 문으로 쓰면 끝날 때 close()가 자동 호출된다."""

    @abstractmethod
    def read(self):
        """값 하나를 읽어서 돌려준다. 실패하면 예외를 낸다."""

    def close(self) -> None:
        """자원 정리 (카메라 해제, SPI/I2C 닫기 등). 필요 없으면 그대로 둔다."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class PressureSensor(Sensor):
    @abstractmethod
    def read(self) -> PressureReading: ...


class DistanceSensor(Sensor):
    @abstractmethod
    def read(self) -> DistanceReading: ...


class CameraSensor(Sensor):
    @abstractmethod
    def read(self) -> FrameReading | None:
        """프레임을 못 읽으면 None."""


class PoseSource(Sensor):
    """상체 점을 주는 쪽. 실제로는 ai/camera가 카메라 프레임에 MediaPipe를 돌려서 만든다."""

    @abstractmethod
    def read(self) -> PoseReading | None:
        """가장 최근 결과. 아직 없으면 None."""


# ---------------------------------------------------------------------------
# 한 번에 읽기
# ---------------------------------------------------------------------------
class SensorHub:
    """압력·거리·카메라를 같은 순간에 읽어서 Sample 하나로 묶는다.

    mock / real 모두 이걸로 읽고, replay는 ReplaySource가 같은 모양(Sample)을 준다.
    """

    def __init__(self, pressure: PressureSensor, distance: DistanceSensor,
                 pose: PoseSource | None = None, *, n_channels: int = 8,
                 adc_max: int = 1023, max_consecutive_errors: int = 20):
        self.pressure = pressure
        self.distance = distance
        self.pose = pose
        self.n_channels = n_channels
        self.adc_max = adc_max
        self.max_errors = max_consecutive_errors
        self.errors = {"pressure": 0, "distance": 0, "pose": 0}   # 연속 실패 횟수

    def _fail(self, name: str, err: Exception) -> None:
        self.errors[name] += 1
        n = self.errors[name]
        if n == 1 or n % 10 == 0:
            log.warning("%s 센서 읽기 실패 (%d번 연속): %s", name, n, err)
        if n >= self.max_errors:
            raise SensorError(f"{name} 센서가 {n}번 연속 실패 — 연결·배선 확인 필요 "
                              f"(마지막 오류: {err})") from err

    def _ok(self, name: str) -> None:
        if self.errors[name]:
            log.info("%s 센서 복구됨", name)
        self.errors[name] = 0

    def read(self) -> Sample:
        ts = now()

        try:
            pressure = self.pressure.read()
            pressure.validate(self.n_channels, self.adc_max)
            self._ok("pressure")
        except Exception as e:  # noqa: BLE001 — 어떤 센서 오류든 여기서 한 번에 처리
            self._fail("pressure", e)          # 연속 실패가 많으면 여기서 SensorError
            raise SampleSkipped(f"압력 읽기 실패, 이번 순간 건너뜀: {e}") from e

        try:
            distance = self.distance.read()
            self._ok("distance")
        except Exception as e:  # noqa: BLE001
            self._fail("distance", e)
            distance = DistanceReading.failed(ts)

        pose = None
        if self.pose is not None:
            try:
                pose = self.pose.read()
                self._ok("pose")
            except Exception as e:  # noqa: BLE001
                self._fail("pose", e)
                pose = PoseReading.empty(ts)

        return Sample(ts=ts, pressure=pressure, distance=distance, pose=pose)

    def close(self) -> None:
        for s in (self.pressure, self.distance, self.pose):
            if s is None:
                continue
            try:
                s.close()
            except Exception as e:  # noqa: BLE001 — 닫다가 실패해도 나머지는 닫는다
                log.warning("%s 닫기 실패: %s", type(s).__name__, e)


def now() -> float:
    """센서 시간 기준. 모든 ts는 이 함수로 만든다."""
    return time.time()
