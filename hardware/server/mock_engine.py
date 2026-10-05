"""가짜 판단 엔진 — AI 엔진이 hardware 브랜치에 합쳐지기 전까지 mock 서버에서 쓰는 대역

AI 엔진(ai 브랜치 ai/engine/upper_body.py)과 같은 모양의 결과를 낸다.
    - 좌면: 시나리오 라벨 그대로 (압력 합이 50 미만이면 empty — seating.is_empty, AI 코드와 같은 기준)
    - 착석 구간: seating.SeatingTracker (자리 비움 5초 → 구간 종료, 구간별 정상/비정상/unknown 시간)
    - 목·기울기: 라벨에 맞는 변화량(deltas)을 흉내 내고, 판단 규칙은 upper_body.judge()와 같게
      (threshold는 config.yaml의 upper_rules, 없으면 AI 코드의 기본값)
    - 시간 필터: 새 상태가 short_filter_sec(3초) 이상 이어져야 확정 (upper_body.StateFilter와 같은 방식)

AI 엔진이 합쳐지면 judge_upper()/judge_seat() 대신 진짜 결과를 넣으면 된다.
AI 코드는 import하지 않는다 (hardware 브랜치에 아직 없음, 수정하지 않기로 함).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np

from common.config import fsr_positions
from common.schema import (
    LANDMARK_NAMES, DistanceLevel, HeadState, Phase, SeatState, TiltState, classify_distance,
)
from common.sensors_base import Sample
from hardware.mock.scenarios import Step
from hardware.server.seating import SeatingTracker, is_empty

# ai/engine/upper_body.py UpperRules 기본값과 같게 유지
UPPER_RULE_DEFAULTS = {
    "neck_drop_ratio": 0.12,
    "face_grow_ratio": 0.08,
    "shoulder_angle_deg": 5.0,
    "head_angle_deg": 7.0,
    "head_offset": 0.10,
}
SENSOR_OK_SEC = 2.0             # 최근 이 시간 안에 정상 값을 읽었으면 센서 정상
MAX_GAP_SEC = 5.0               # 기록이 이보다 오래 끊기면 연속 착석이 끊긴 것으로 봄
HISTORY_SEC = 3600              # 최근 판단 기록 보관 (1초에 1개)

POSTURE_KEYS = {                # 앱에 보여줄 나쁜 자세 이름. 순서 = 우선순위 (postures[0]이 대표 자세)
    ("head", "forward"): "forward_head",        # 거북목 > 다리 꼬기 > 체중 편향 > 기울어진 자세
    ("seat", "cross_left"): "cross_left",
    ("seat", "cross_right"): "cross_right",
    ("seat", "lean_left"): "lean_left",
    ("seat", "lean_right"): "lean_right",
    ("tilt", "left"): "tilt_left",
    ("tilt", "right"): "tilt_right",
}


class StateFilter:
    """새 상태가 hold_sec 이상 계속될 때만 바꾼다 (ai/engine/upper_body.StateFilter와 같은 방식)"""

    def __init__(self, hold_sec: float):
        self.hold = hold_sec
        self.state = None
        self._candidate = None
        self._since: float | None = None

    def update(self, new_state, ts: float):
        if self.state is None:
            self.state = new_state
        elif new_state == self.state:
            self._candidate, self._since = None, None
        elif new_state != self._candidate:
            self._candidate, self._since = new_state, ts
        elif ts - self._since >= self.hold:
            self.state, self._candidate, self._since = new_state, None, None
        return self.state

    def pending(self, ts: float):
        if self._candidate is None:
            return None
        return self._candidate, max(0.0, self.hold - (ts - self._since))


@dataclass
class BaselineValue:
    created_at: float
    seconds: float
    pressure: list[float]
    distance_mm: float | None
    pose: dict[str, list[float]] | None


class MockEngine:
    def __init__(self, cfg: dict, seed: int | None = None):
        th = cfg["thresholds"]
        self.rules = {**UPPER_RULE_DEFAULTS, **(cfg.get("upper_rules") or {})}
        self.close_delta_mm = th["close_delta_mm"]
        self.ok_mm, self.warn_mm = th["distance_ok_mm"], th["distance_warning_mm"]
        self.baseline_seconds = th["baseline_seconds"]
        self.positions = fsr_positions(cfg)
        self.rng = np.random.default_rng(seed)
        hold = th["short_filter_sec"]
        self.filters = {"seat": StateFilter(hold), "head": StateFilter(hold), "tilt": StateFilter(hold)}

        self.baseline: BaselineValue | None = None
        self._measure_until: float | None = None
        self._measure_start: float | None = None
        self._buf: list[Sample] = []

        self.latest: dict | None = None
        self.history: deque[dict] = deque(maxlen=HISTORY_SEC)
        self._last_history_sec: int | None = None
        self._state: tuple | None = None
        self._state_since: float | None = None
        self._sitting_since: float | None = None
        self._last_seated_ts: float | None = None
        self.last_ok = {"pressure": None, "distance": None, "camera": None}
        self.seating = SeatingTracker(cfg)

    # --- 기준 자세 ----------------------------------------------------------
    def start_baseline(self, ts: float) -> float:
        """기준 자세 측정 시작. 측정 시간(초)을 돌려준다"""
        self._measure_start, self._measure_until = ts, ts + self.baseline_seconds
        self._buf = []
        return self.baseline_seconds

    @property
    def measuring(self) -> bool:
        return self._measure_until is not None

    def baseline_remaining(self, ts: float) -> float | None:
        return max(0.0, self._measure_until - ts) if self.measuring else None

    def _collect_baseline(self, sample: Sample) -> None:
        self._buf.append(sample)
        if sample.ts < self._measure_until:
            return
        p = np.mean([s.pressure.values for s in self._buf], axis=0)
        dists = [s.distance.distance_mm for s in self._buf if s.distance.valid]
        poses = [s.pose for s in self._buf if s.pose is not None and s.pose.detected]
        pose = None
        if poses:
            pose = {n: [round(float(np.mean([getattr(q.points[n], a) for q in poses if n in q.points])), 4)
                        for a in ("x", "y", "z", "visibility")]
                    for n in LANDMARK_NAMES}
        self.baseline = BaselineValue(
            created_at=sample.ts, seconds=round(sample.ts - self._measure_start, 1),
            pressure=[round(float(v), 1) for v in p],
            distance_mm=float(np.median(dists)) if dists else None, pose=pose)
        self._measure_until = self._measure_start = None
        self._buf = []

    # --- 가짜 판단 ----------------------------------------------------------
    def judge_seat(self, sample: Sample, step: Step) -> tuple[SeatState, float]:
        if is_empty(sample.pressure.values):
            return SeatState.EMPTY, 0.95
        label = SeatState.NORMAL if step.phase == Phase.BASELINE.value else SeatState(step.seat)
        return label, round(float(self.rng.uniform(0.75, 0.95)), 2)

    def judge_upper(self, sample: Sample, step: Step):
        """(head, tilt, head_conf, tilt_conf, deltas, cues) — upper_body.judge()와 같은 규칙"""
        pose = sample.pose
        if pose is None or not pose.detected or self.baseline is None:
            return HeadState.UNKNOWN, TiltState.UNKNOWN, 0.0, 0.0, {}, {}
        r, rng = self.rules, self.rng
        baseline_phase = step.phase == Phase.BASELINE.value
        fwd = not baseline_phase and step.head == "forward"
        sign = 0 if baseline_phase else {"none": 0, "left": 1, "right": -1}[step.tilt]

        # 라벨에 맞는 변화량 흉내 (실제로는 카메라 점 → 특징 → 기준과 비교)
        deltas = {
            "neck_drop": rng.normal(0.18 if fwd else 0.0, 0.03),
            "face_grow": rng.normal(0.13 if fwd else 0.0, 0.025),
            "shoulder_angle": rng.normal(8.0 * sign, 1.0),
            "head_angle": rng.normal(9.0 * sign, 1.5),
            "head_offset": rng.normal(0.13 * sign, 0.03),
        }
        cues = {"neck_drop": deltas["neck_drop"] >= r["neck_drop_ratio"],
                "face_grow": deltas["face_grow"] >= r["face_grow_ratio"]}
        if sample.distance.valid and self.baseline.distance_mm is not None:
            deltas["closer_mm"] = self.baseline.distance_mm - sample.distance.distance_mm
            cues["closer"] = deltas["closer_mm"] >= self.close_delta_mm

        cam_hits = cues["neck_drop"] + cues["face_grow"]
        if cam_hits == 0:
            head, head_conf = HeadState.NORMAL, 0.8
        else:
            head = HeadState.FORWARD
            head_conf = {1: 0.6, 2: 0.8}[cam_hits] + (0.15 if cues.get("closer") else 0.0)

        scores = {"shoulder_angle": deltas["shoulder_angle"] / r["shoulder_angle_deg"],
                  "head_angle": deltas["head_angle"] / r["head_angle_deg"],
                  "head_offset": deltas["head_offset"] / r["head_offset"]}
        over = {k: s for k, s in scores.items() if abs(s) >= 1.0}
        cues.update({k: k in over for k in scores})
        if not over:
            tilt, tilt_conf = TiltState.NONE, 0.8
        else:
            direction = sum(over.values())
            tilt = TiltState.LEFT if direction > 0 else TiltState.RIGHT
            agree = sum(1 for s in over.values() if (s > 0) == (direction > 0))
            tilt_conf = {1: 0.6, 2: 0.8, 3: 0.95}[agree] if agree == len(over) else 0.4

        deltas = {k: round(float(v), 4) for k, v in deltas.items()}
        cues = {k: bool(v) for k, v in cues.items()}
        return head, tilt, min(head_conf, 1.0), tilt_conf, deltas, cues

    # --- 한 순간 처리 -------------------------------------------------------
    def update(self, sample: Sample, step: Step) -> dict:
        ts = sample.ts
        if self.baseline is None and not self.measuring:
            self.start_baseline(ts)                 # 처음에는 자동으로 기준 측정
        if self.measuring:
            self._collect_baseline(sample)

        self.last_ok["pressure"] = ts
        if sample.distance.valid:
            self.last_ok["distance"] = ts
        if sample.pose is not None:
            self.last_ok["camera"] = ts

        seat_raw, seat_conf = self.judge_seat(sample, step)
        head_raw, tilt_raw, head_conf, tilt_conf, deltas, cues = self.judge_upper(sample, step)
        if seat_raw == SeatState.EMPTY:            # 자리 비움이면 목·기울기는 판단하지 않음
            head_raw, tilt_raw, head_conf, tilt_conf, deltas, cues = (
                HeadState.UNKNOWN, TiltState.UNKNOWN, 0.0, 0.0, {}, {})
        seat = self.filters["seat"].update(seat_raw, ts)
        head = self.filters["head"].update(head_raw, ts)
        tilt = self.filters["tilt"].update(tilt_raw, ts)

        state = (seat, head, tilt)
        if state != self._state:
            self._state, self._state_since = state, ts

        seated = seat != SeatState.EMPTY
        if seated:
            gap = self._last_seated_ts is not None and ts - self._last_seated_ts > MAX_GAP_SEC
            if self._sitting_since is None or gap:
                self._sitting_since = ts
            self._last_seated_ts = ts
        else:
            self._sitting_since = None

        values = [int(v) for v in sample.pressure.values]
        total = float(sum(values))
        ratio = cop = None
        if not is_empty(values):
            ratio = [round(v / total, 4) for v in values]
            x, y = (np.asarray(values, dtype=float)[:, None] * self.positions).sum(axis=0) / total
            cop = {"x": round(float(x), 4), "y": round(float(y), 4)}

        d = sample.distance
        dist_mm = d.distance_mm if d.valid else None
        closer = None
        if self.baseline and self.baseline.distance_mm is not None and dist_mm is not None:
            closer = self.baseline.distance_mm - dist_mm >= self.close_delta_mm

        postures = [name for (kind, value), name in POSTURE_KEYS.items()
                    if {"seat": seat, "head": head, "tilt": tilt}[kind].value == value]
        self.seating.update(ts, empty=is_empty(values), seat=seat.value, head=head.value,
                            tilt=tilt.value, postures=postures)
        pending = []
        for kind, f in self.filters.items():
            p = f.pending(ts)
            if p is not None:
                pending.append({"kind": kind, "state": p[0].value, "remaining_sec": round(p[1], 1)})

        pose = sample.pose
        self.latest = {
            "ts": ts,
            "seated": seated,
            "sitting_since": self._sitting_since,
            "state_since": self._state_since,
            "seat_state": seat, "head_state": head, "tilt_state": tilt,
            "confidence": {"seat": seat_conf, "head": head_conf, "tilt": tilt_conf},
            "postures": postures,
            "pending": pending,
            "deltas": deltas,
            "cues": cues,
            "distance_mm": dist_mm,
            "distance_level": classify_distance(dist_mm, self.ok_mm, self.warn_mm) if seated
            else DistanceLevel.UNKNOWN,
            "closer_than_baseline": closer if seated else None,
            "pressure": values,
            "pressure_ratio": ratio,
            "center_of_pressure": cop,
            "pose_detected": bool(pose and pose.detected),
            "camera_age_sec": round(ts - pose.ts, 2) if pose else None,
            "baseline_ready": self.baseline is not None,
        }

        sec = int(ts)
        if sec != self._last_history_sec:          # 1초에 1개만 기록
            self._last_history_sec = sec
            self.history.append({k: self.latest[k] for k in (
                "ts", "seated", "seat_state", "head_state", "tilt_state",
                "distance_mm", "distance_level")})
        return self.latest

    def sensor_ok(self, name: str, ts: float) -> bool:
        last = self.last_ok[name]
        return last is not None and ts - last <= SENSOR_OK_SEC
