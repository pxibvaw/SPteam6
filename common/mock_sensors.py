"""가짜 센서 — 하드웨어 없이 전체 흐름을 테스트할 때 사용

주의: 여기서 나오는 값은 실제 자세 패턴이 아니다. 코드가 끝까지 도는지
확인하는 용도이고, 정확도 평가에 쓰면 안 된다.

사용 예:
    hub = MockSensorHub(cfg)
    hub.set_labels(seat="cross_right", head="forward", tilt="left")
    sample = hub.read()
"""
from __future__ import annotations

import numpy as np

from common.config import fsr_positions
from common.schema import HeadState, SeatState, TiltState, check_labels
from common.sensors_base import (
    CameraSensor, DistanceReading, DistanceSensor, FrameReading, Landmark,
    PoseReading, PoseSource, PressureReading, PressureSensor, SensorHub, now,
)


class MockPressureSensor(PressureSensor):
    """config.yaml의 FSR 위치(pressure.positions)를 보고 자세별 압력을 흉내 낸다.

    다리 꼬기: 위에 올린 다리 쪽 좌골에 하중 → 그쪽 뒤↑ 앞↓
    기대기:    기댄 쪽 앞·뒤 모두 ↑
    """

    def __init__(self, positions: np.ndarray, adc_max: int = 1023,
                 noise: float = 20.0, seed: int | None = None):
        self.pos = np.asarray(positions, dtype=float)
        self.adc_max = adc_max
        self.noise = noise
        self.label = SeatState.NORMAL.value
        self.rng = np.random.default_rng(seed)

    def _base(self) -> np.ndarray:
        x, y = self.pos[:, 0], self.pos[:, 1]
        left, front = x < 0, y > 0
        v = 500.0 - 150.0 * y                       # 뒤쪽(엉덩이)이 더 눌림
        lab = self.label
        if lab == SeatState.EMPTY.value:
            return np.zeros(len(v))
        if lab == SeatState.LEAN_LEFT.value:
            v = np.where(left, v * 1.3, v * 0.7)
        elif lab == SeatState.LEAN_RIGHT.value:
            v = np.where(left, v * 0.7, v * 1.3)
        elif lab in (SeatState.CROSS_LEFT.value, SeatState.CROSS_RIGHT.value):
            side = left if lab == SeatState.CROSS_LEFT.value else ~left
            v = np.where(side & ~front, v * 1.4, v)   # 올린 다리 쪽 뒤 ↑
            v = np.where(side & front, v * 0.5, v)    # 올린 다리 쪽 앞 ↓
        return v

    def read(self) -> PressureReading:
        v = self._base() + self.rng.normal(0, self.noise, len(self.pos))
        v = np.clip(v, 0, self.adc_max).astype(int)
        return PressureReading(ts=now(), values=v.tolist())


class MockDistanceSensor(DistanceSensor):
    """VL53L1X 흉내. 가끔 측정 실패(range_status ≠ 0)를 섞어서 필터 테스트용으로 씀"""

    def __init__(self, base_mm: int = 600, noise_mm: float = 8.0,
                 fail_prob: float = 0.03, seed: int | None = None):
        self.base_mm = base_mm
        self.offset_mm = 0                          # 거북목이면 음수 (가까워짐)
        self.noise_mm = noise_mm
        self.fail_prob = fail_prob
        self.rng = np.random.default_rng(seed)

    def read(self) -> DistanceReading:
        if self.rng.random() < self.fail_prob:
            return DistanceReading(ts=now(), distance_mm=None, range_status=2)
        mm = self.rng.normal(self.base_mm + self.offset_mm, self.noise_mm)
        return DistanceReading(ts=now(), distance_mm=int(mm), range_status=0)


# 1280×720 화면에서 바른 자세일 때의 대략적인 위치 (x, y는 0~1 비율)
# left/right는 사람 기준 → 정면 카메라 화면에서는 사람 왼쪽이 화면 오른쪽(x 큼)
_BASE_POSE = {
    "nose": (0.50, 0.40),
    "left_eye": (0.53, 0.36), "right_eye": (0.47, 0.36),
    "left_ear": (0.57, 0.38), "right_ear": (0.43, 0.38),
    "left_shoulder": (0.65, 0.72), "right_shoulder": (0.35, 0.72),
}
_HEAD = ("nose", "left_eye", "right_eye", "left_ear", "right_ear")


class MockPoseSource(PoseSource):
    """자세 라벨에 맞춰 상체 점 7개를 흉내 낸다 (ai/camera 대신 테스트용)

    거북목: 얼굴이 커지고(카메라에 가까워짐) 아래로 내려옴
    기울기: 어깨 중점 기준으로 상체 전체를 회전 (사람 왼쪽으로 기울면 화면 오른쪽이 내려감)
    """

    def __init__(self, width: int = 1280, height: int = 720, noise: float = 0.003,
                 seed: int | None = None):
        self.w, self.h = width, height
        self.noise = noise
        self.head = HeadState.NORMAL.value
        self.tilt = TiltState.NONE.value
        self.rng = np.random.default_rng(seed)

    def read(self) -> PoseReading:
        px = {k: np.array([x * self.w, y * self.h]) for k, (x, y) in _BASE_POSE.items()}
        if self.head == HeadState.FORWARD.value:
            c = px["nose"].copy()
            for k in _HEAD:
                px[k] = c + (px[k] - c) * 1.15 + np.array([0, 0.04 * self.h])
        if self.tilt != TiltState.NONE.value:
            deg = 8.0 if self.tilt == TiltState.LEFT.value else -8.0
            t = np.radians(deg)
            rot = np.array([[np.cos(t), -np.sin(t)], [np.sin(t), np.cos(t)]])
            c = (px["left_shoulder"] + px["right_shoulder"]) / 2
            px = {k: c + rot @ (p - c) for k, p in px.items()}
        pts = {}
        for k, (x, y) in px.items():
            vis = 0.4 if k.endswith("ear") else 0.95   # 귀는 가려지기 쉬움
            pts[k] = Landmark(x=float(x / self.w + self.rng.normal(0, self.noise)),
                              y=float(y / self.h + self.rng.normal(0, self.noise)),
                              z=float(self.rng.normal(0, 0.02)), visibility=vis)
        return PoseReading(ts=now(), detected=True, points=pts)


class MockCameraSensor(CameraSensor):
    """검은 화면만 돌려준다. 실제 카메라 테스트는 맥 웹캠(cv2.VideoCapture)으로."""

    def __init__(self, width: int = 1280, height: int = 720):
        self.shape = (height, width, 3)

    def read(self) -> FrameReading:
        return FrameReading(ts=now(), image=np.zeros(self.shape, dtype=np.uint8))


class MockSensorHub(SensorHub):
    """가짜 압력·거리·카메라를 한 번에. set_labels로 자세를 바꾼다."""

    def __init__(self, cfg: dict, seed: int | None = None):
        cam, pr = cfg["camera"], cfg["pressure"]
        super().__init__(
            pressure=MockPressureSensor(fsr_positions(cfg), pr["adc_max"], seed=seed),
            distance=MockDistanceSensor(seed=seed),
            pose=MockPoseSource(cam["width"], cam["height"], seed=seed),
            n_channels=pr["n_channels"], adc_max=pr["adc_max"],
        )

    def set_labels(self, seat: str = "normal", head: str = "normal", tilt: str = "none") -> None:
        check_labels(seat, head, tilt)
        self.pressure.label = seat
        self.pose.head = head
        self.pose.tilt = tilt
        self.distance.offset_mm = -80 if head == HeadState.FORWARD.value else 0
