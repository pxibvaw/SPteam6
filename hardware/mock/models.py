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

# 거리센서 흉내 (HC-SR04로 확정)
#   noise_mm: 측정 떨림, fail_prob: 평소 실패 확률, spike_prob: 평소 값 튐 확률
DISTANCE_SENSORS = {
    "hc-sr04": {"noise_mm": 6.0, "fail_prob": 0.042, "spike_prob": 0.005},   # 실제 수집에서 측정 안 됨 4.2%, 초음파 반사로 가끔 튐
}
# 압력 크기: 6채널이면 실제 수집값(p1~p3, 자세 바꾼 뒤 10초 이후)의 자세별 채널 평균을 쓴다.
# 순서 = config.yaml pressure.channels [0,1,3,4,6,7] → p0..p5. 앉으면 대부분 채널이 3000~3700대에 붙고,
# 자세에 따라 특정 채널만 크게 빠진다 (다리 꼬기: 올린 다리 쪽 앞, 체중 편향: 반대쪽 뒤/옆).
REAL_MEANS = {
    "normal":      [3030, 3637, 3587, 3582, 3622, 3096],
    "lean_left":   [2063, 3465, 2901, 3618, 3694, 3009],
    "lean_right":  [3269, 3628, 3146, 3473, 3393, 2263],
    "cross_left":  [3465, 3603, 2748, 1714, 3364, 2549],
    "cross_right": [2957, 1775, 3136, 3581, 3645, 3425],
}
REAL_STD = [307, 117, 194, 71, 86, 170]      # 자세 안에서 천천히 변하는 폭 (채널별 표준편차)
REAL_NOISE = 6.0                             # 0.5초마다 생기는 잔잔한 떨림 (실측 0.5초 변화 중앙값 약 6~8 → 표준편차 약 6)
REAL_DRIFT_TAU = 120.0                       # 천천히 변하는 시간 (초). 실측은 0.5초 사이 변화가 6~8밖에 안 될 만큼 매끄러움
REAL_DRIFT_K = 0.35                          # 실제 폭의 35%만 흔들림 (100%로 하면 AI 엔진이 가만히 앉은 mock을 가끔 편향으로 판정)
# 채널 수가 6이 아닌 배치(시험용 7채널 등)는 아래 비율 모양에 PRESSURE_SCALE을 곱해 쓴다
PRESSURE_SCALE = 6.0

# 거리: 실측(p1~p3)에서 앉은 위치별 중앙값 — 앞 약 43.7cm / 중간 약 63.4cm / 뒤 약 82.3cm (3명 평균, 벽 읽기 제외)
POSITION_DISTANCE_MM = {"front": 437, "middle": 634, "back": 823, "none": 634}
# 몸을 기울이면 얼굴이 센서 빔에서 벗어나 뒤쪽 벽(약 165cm)을 읽는다. 실측에서 이 값이 나온 비율:
#   오른쪽 편향 15~57%, 왼쪽 편향 2~12%, 왼다리 꼬기 0~13%, 그 외 거의 0 (평균 연속 길이 약 3샘플)
# 기울기(tilt)는 실측이 없어서 편향과 같은 방향으로 가정
WALL_MM = 1650
WALL_STD_MM = 40.0
WALL_EXIT_PROB = 0.35                         # 벽을 읽다가 돌아올 확률 (샘플당) → 평균 연속 약 3샘플
WALL_RATE = {"lean_right": 0.40, "lean_left": 0.06, "cross_left": 0.07, "cross_right": 0.01,
             "tilt_right": 0.20, "tilt_left": 0.05}
WALL_RATE_DEFAULT = 0.01
BASE_DISTANCE_MM = 600          # (사용 안 함: 앉은 위치별 값을 씀. fake_history 등 예전 코드와 맞추려고 남김)
FORWARD_OFFSET_MM = -80         # 거북목이면 화면에 가까워짐 (mock_sensors와 같음)
EMPTY_DISTANCE_MM = 1650        # 자리 비움이면 뒤쪽 벽까지 거리 (실측 벽 읽기 약 165cm)


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
                 noise: float | None = None, drift: float | None = None):
        self.pos = np.asarray(positions, dtype=float)
        self.adc_max = adc_max
        self.rng = rng
        self.real = len(self.pos) == 6               # 실제 수집값 표를 쓸 수 있는 배치
        self.noise = noise if noise is not None else (REAL_NOISE if self.real else 12.0 * PRESSURE_SCALE)
        if drift is None:
            drift = np.array(REAL_STD, dtype=float) * REAL_DRIFT_K if self.real else 25.0 * PRESSURE_SCALE
        self.drift = Drift(rng, len(self.pos), drift, tau_sec=REAL_DRIFT_TAU if self.real else 5.0)

    def _real_base(self, seat: str, extra_lean: str | None) -> np.ndarray:
        """실제 평균 × (자세 평균 ÷ 바른 자세 평균). 다리 꼬기와 체중 편향이 겹치면 두 비율을 곱함"""
        normal = np.array(REAL_MEANS["normal"], dtype=float)
        v = normal.copy()
        for key in (seat if seat in REAL_MEANS else None, f"lean_{extra_lean}" if extra_lean else None):
            if key and key != "normal":
                v = v * np.array(REAL_MEANS[key], dtype=float) / normal
        return v

    def base(self, seat: str, extra_lean: str | None = None) -> np.ndarray:
        if self.real:
            return self._real_base(seat, extra_lean)
        x, y = self.pos[:, 0], self.pos[:, 1]
        left, front = x < 0, y > 0
        v = (450.0 - 130.0 * y) * PRESSURE_SCALE       # 겹친 자세에서도 4095에 붙지 않게
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
        self.drift = Drift(rng, 1, 35.0, tau_sec=20.0)     # 자세 안에서 거리가 천천히 변함 (실측 표준편차 약 4cm)
        self.wall = False                                   # 지금 얼굴이 빔에서 벗어나 벽을 읽는 중
        # 벽 읽기는 별도 난수로 (압력 끊김 등 다른 모델의 난수 순서가 바뀌지 않게). 같은 seed면 같은 결과
        self.wall_rng = np.random.default_rng(rng.bit_generator.seed_seq.spawn(1)[0])

    def true_mm(self, head: str, empty: bool, pos: str = "middle") -> float:
        if empty:
            return EMPTY_DISTANCE_MM
        return POSITION_DISTANCE_MM.get(pos, POSITION_DISTANCE_MM["middle"]) + (
            FORWARD_OFFSET_MM if head == HeadState.FORWARD.value else 0)

    def _wall_step(self, seat: str, tilt: str) -> bool:
        """벽 읽기 상태를 한 샘플 진행 (자세에 따라 들어갈 확률이 다르고, 들어가면 몇 샘플 이어짐)"""
        rate = max(WALL_RATE.get(seat, WALL_RATE_DEFAULT), WALL_RATE.get(f"tilt_{tilt}", WALL_RATE_DEFAULT))
        if self.wall:
            self.wall = self.wall_rng.random() >= WALL_EXIT_PROB
        else:
            enter = rate * WALL_EXIT_PROB / (1.0 - rate)
            self.wall = self.wall_rng.random() < enter
        return self.wall

    def _spike(self, true_mm: float) -> int:
        """튄 값: 반은 범위 밖(min_mm/max_mm로 걸러짐), 반은 범위 안 틀린 값(중앙값 필터로만 걸러짐)"""
        r = self.rng
        if r.random() < 0.5:
            mm = r.uniform(20, 120) if r.random() < 0.5 else r.uniform(2000, 4000)
        else:
            mm = true_mm * (r.uniform(0.45, 0.7) if r.random() < 0.5 else r.uniform(1.3, 1.6))
        return int(mm)

    def read(self, ts: float, head: str, empty: bool, fail_prob: float = 0.0,
             spike_prob: float = 0.0, pos: str = "middle", seat: str = "normal",
             tilt: str = "none") -> DistanceReading:
        r = self.rng
        drift = float(self.drift.step(ts)[0])
        if r.random() < max(fail_prob, self.spec["fail_prob"]):
            return DistanceReading(ts=ts, distance_mm=None, range_status=STATUS_READ_ERROR)
        true_mm = self.true_mm(head, empty, pos)
        if not empty and self._wall_step(seat, tilt):         # 벽을 읽음: 값은 정상(status 0)인데 틀림
            return DistanceReading(ts=ts, distance_mm=int(round(self.wall_rng.normal(WALL_MM, WALL_STD_MM))), range_status=STATUS_OK)
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
