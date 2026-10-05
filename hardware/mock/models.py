"""더미 센서 값 모델 — 라벨 → 압력·거리·카메라 좌표

common/mock_sensors.py는 수정하지 않기로 해서, 거기서 안 되는 것만 여기서 새로 만든다.
    - 다리 꼬기 + 체중 편향을 함께 반영한 압력 (mock_sensors는 자세 하나만 받음)
    - HC-SR04 값 튐, 측정 실패는 센서 종류와 상관없이 distance_mm=None / range_status=-1
    - visibility까지 모든 값에 노이즈 + 천천히 움직이는 값(자세를 고쳐 앉는 느낌)

압력 모양·카메라 위치는 common/mock_sensors.py와 같은 규칙에서 시작한다.
주의: 실제 자세 패턴이 아니라 코드·앱 흐름 확인용이다. 정확도 평가에 쓰면 안 된다.
"""
from __future__ import annotations

import numpy as np

from common.schema import HeadState, SeatState, TiltState
from common.sensors_base import (
    STATUS_OK, STATUS_READ_ERROR, DistanceReading, Landmark, PoseReading, PressureReading,
)

# 거리센서 종류별 흉내 (아직 확정 전: VL53L1X 또는 HC-SR04)
#   noise_mm: 측정 떨림, fail_prob: 평소 실패 확률, spike_prob: 평소 값 튐 확률
DISTANCE_SENSORS = {
    "vl53l1x": {"noise_mm": 8.0, "fail_prob": 0.02, "spike_prob": 0.0},
    "hc-sr04": {"noise_mm": 12.0, "fail_prob": 0.02, "spike_prob": 0.005},   # 초음파 반사로 가끔 튐
}
BASE_DISTANCE_MM = 600          # 바른 자세일 때 화면 거리 (mock_sensors 기본값과 같음)
FORWARD_OFFSET_MM = -80         # 거북목이면 화면에 가까워짐 (mock_sensors와 같음)
EMPTY_DISTANCE_MM = 1100        # 자리 비움이면 의자 뒤 벽까지 거리


class Drift:
    """천천히 움직이는 값 (평균으로 돌아가는 랜덤 워크). 같은 값이 계속 나오지 않게"""

    def __init__(self, rng: np.random.Generator, size: int, std: float, tau_sec: float = 5.0):
        self.rng, self.std, self.tau = rng, std, tau_sec
        self.v = np.zeros(size)
        self.ts: float | None = None

    def step(self, ts: float) -> np.ndarray:
        dt = 0.1 if self.ts is None else min(max(ts - self.ts, 0.0), 1.0)
        self.ts = ts
        a = np.exp(-dt / self.tau)
        self.v = a * self.v + self.std * np.sqrt(1 - a * a) * self.rng.normal(size=self.v.shape)
        return self.v


class PressureModel:
    """FSR 위치(config.yaml pressure.positions)를 보고 자세별 압력을 만든다. 채널 수는 위치 개수만큼

    바른 자세: 뒤쪽(엉덩이)이 더 눌림
    체중 편향: 기댄 쪽 앞·뒤 모두 ↑, 반대쪽 ↓
    다리 꼬기: 올린 다리 쪽 뒤 ↑, 앞 ↓
    """

    def __init__(self, positions: np.ndarray, adc_max: int, rng: np.random.Generator,
                 noise: float = 12.0, drift: float = 25.0):
        self.pos = np.asarray(positions, dtype=float)
        self.adc_max = adc_max
        self.rng = rng
        self.noise = noise
        self.drift = Drift(rng, len(self.pos), drift)

    def base(self, seat: str, extra_lean: str | None = None) -> np.ndarray:
        x, y = self.pos[:, 0], self.pos[:, 1]
        left, front = x < 0, y > 0
        v = 450.0 - 130.0 * y                          # 겹친 자세에서도 1023에 붙지 않게
        if seat.startswith("cross_"):
            side = left if seat == SeatState.CROSS_LEFT.value else ~left
            v = np.where(side & ~front, v * 1.4, v)
            v = np.where(side & front, v * 0.5, v)
        lean = seat[len("lean_"):] if seat.startswith("lean_") else extra_lean
        if lean:
            side = left if lean == "left" else ~left
            v = np.where(side, v * 1.3, v * 0.7)
        return v

    def read(self, ts: float, seat: str, extra_lean: str | None = None,
             dead_channel: int | None = None) -> PressureReading:
        n = len(self.pos)
        drift = self.drift.step(ts)
        if seat == SeatState.EMPTY.value:
            v = np.abs(self.rng.normal(2.0, 2.0, n))          # 아무도 없음: 0 근처
        else:
            v = self.base(seat, extra_lean) + drift + self.rng.normal(0, self.noise, n)
        if dead_channel is not None and 0 <= dead_channel < n:
            v[dead_channel] = abs(self.rng.normal(0, 2.0))     # 연결 끊김: 0 근처
        v = np.clip(np.rint(v), 0, self.adc_max).astype(int)
        return PressureReading(ts=ts, values=v.tolist())


class DistanceModel:
    """화면 거리 (mm). 실패는 센서 종류와 상관없이 distance_mm=None, range_status=-1"""

    def __init__(self, sensor: str, rng: np.random.Generator):
        if sensor not in DISTANCE_SENSORS:
            raise ValueError(f"거리센서 '{sensor}'는 흉내 낼 수 없음. 가능: {list(DISTANCE_SENSORS)}")
        self.sensor = sensor
        self.spec = DISTANCE_SENSORS[sensor]
        self.rng = rng
        self.drift = Drift(rng, 1, 10.0)

    def true_mm(self, head: str, empty: bool) -> float:
        if empty:
            return EMPTY_DISTANCE_MM
        return BASE_DISTANCE_MM + (FORWARD_OFFSET_MM if head == HeadState.FORWARD.value else 0)

    def _spike(self, true_mm: float) -> int:
        """튄 값: 반은 범위 밖(min_mm/max_mm로 걸러짐), 반은 범위 안 틀린 값(중앙값 필터로만 걸러짐)"""
        r = self.rng
        if r.random() < 0.5:
            mm = r.uniform(20, 120) if r.random() < 0.5 else r.uniform(2000, 4000)
        else:
            mm = true_mm * (r.uniform(0.45, 0.7) if r.random() < 0.5 else r.uniform(1.3, 1.6))
        return int(mm)

    def read(self, ts: float, head: str, empty: bool, fail_prob: float = 0.0,
             spike_prob: float = 0.0) -> DistanceReading:
        r = self.rng
        drift = float(self.drift.step(ts)[0])
        if r.random() < max(fail_prob, self.spec["fail_prob"]):
            return DistanceReading(ts=ts, distance_mm=None, range_status=STATUS_READ_ERROR)
        true_mm = self.true_mm(head, empty)
        if r.random() < max(spike_prob, self.spec["spike_prob"]):
            return DistanceReading(ts=ts, distance_mm=self._spike(true_mm), range_status=STATUS_OK)
        mm = true_mm + drift + r.normal(0, self.spec["noise_mm"])
        return DistanceReading(ts=ts, distance_mm=int(round(mm)), range_status=STATUS_OK)


# 1280×720 화면에서 바른 자세일 때의 대략적인 위치 (common/mock_sensors.py와 같은 값)
# left/right는 사람 기준 → 정면 카메라 화면에서는 사람 왼쪽이 화면 오른쪽(x 큼)
BASE_POSE = {
    "nose": (0.50, 0.40),
    "left_eye": (0.53, 0.36), "right_eye": (0.47, 0.36),
    "left_ear": (0.57, 0.38), "right_ear": (0.43, 0.38),
    "left_shoulder": (0.65, 0.72), "right_shoulder": (0.35, 0.72),
}
HEAD_POINTS = ("nose", "left_eye", "right_eye", "left_ear", "right_ear")


class PoseModel:
    """자세 라벨 → 상체 점 7개 (ai/camera 대신). 영상은 만들지 않고 좌표만

    거북목: 얼굴이 커지고(카메라에 가까워짐) 아래로 내려옴
    기울기: 어깨 중점 기준으로 상체 전체를 회전
    """

    def __init__(self, width: int, height: int, rng: np.random.Generator, noise: float = 0.003):
        self.w, self.h = width, height
        self.rng = rng
        self.noise = noise
        self.sway = Drift(rng, 2, 0.006)                 # 몸 전체가 조금씩 흔들림 (비율)

    def read(self, ts: float, head: str, tilt: str, hidden: tuple[str, ...] = ()) -> PoseReading:
        r = self.rng
        sway = self.sway.step(ts)
        px = {k: np.array([x * self.w, y * self.h]) for k, (x, y) in BASE_POSE.items()}
        forward = head == HeadState.FORWARD.value
        if forward:
            c = px["nose"].copy()
            for k in HEAD_POINTS:
                px[k] = c + (px[k] - c) * 1.15 + np.array([0, 0.04 * self.h])
        if tilt != TiltState.NONE.value:
            t = np.radians(8.0 if tilt == TiltState.LEFT.value else -8.0)
            rot = np.array([[np.cos(t), -np.sin(t)], [np.sin(t), np.cos(t)]])
            c = (px["left_shoulder"] + px["right_shoulder"]) / 2
            px = {k: c + rot @ (p - c) for k, p in px.items()}

        points = {}
        for k, (x, y) in px.items():
            if k in hidden:
                continue
            z = (-0.08 if forward and k in HEAD_POINTS else 0.0) + r.normal(0, 0.02)
            vis = (0.45 if k.endswith("ear") else 0.95) + r.normal(0, 0.03)   # 귀는 가려지기 쉬움
            points[k] = Landmark(x=float(x / self.w + sway[0] + r.normal(0, self.noise)),
                                 y=float(y / self.h + sway[1] + r.normal(0, self.noise)),
                                 z=float(z), visibility=float(np.clip(vis, 0.0, 1.0)))
        return PoseReading(ts=ts, detected=True, points=points)
