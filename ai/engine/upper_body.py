"""상체 판단 — 기준 자세 대비 변화로 거북목·좌우 기울기 판단 (규칙 방식 초안)

흐름
    0. FeatureSmoother: 최근 몇 프레임 특징의 중앙값 (점 떨림 완화, 특히 눈 선 각도)
    1. Baseline: 처음 baseline_seconds 동안 바른 자세의 특징을 모아 중앙값을 기준으로 저장
    2. judge(): 지금 특징 − 기준 특징이 threshold를 넘으면 나쁜 자세
    3. StateFilter: 같은 상태가 short_filter_sec 이상 유지돼야 상태를 바꿈 (깜빡임 방지)

기준값(threshold)은 config.yaml의 upper_rules에 있고, 전부 임시값이다.
웹캠으로 직접 앉아서 조정하고, 데이터를 모은 뒤 분포를 보고 다시 정한다.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, fields

import numpy as np

from ai.features.pose_features import UpperFeatures
from common.schema import HeadState, TiltState


# ---------------------------------------------------------------------------
# 기준값
# ---------------------------------------------------------------------------
@dataclass
class UpperRules:
    min_visibility: float = 0.5        # 이보다 안 보이는 점이 있으면 판단하지 않음
    neck_drop_ratio: float = 0.12      # 목 높이 비율이 기준보다 12% 이상 줄면 거북목 단서
    face_grow_ratio: float = 0.08      # 얼굴 크기 비율이 기준보다 8% 이상 커지면 거북목 단서
    close_delta_mm: float = 100.0      # 화면 거리가 기준보다 10cm 이상 가까워지면 거북목 단서 (거리센서)
    shoulder_angle_deg: float = 5.0    # 어깨 선이 기준보다 5도 이상 기울면
    head_angle_deg: float = 7.0        # 눈 선이 기준보다 7도 이상 기울면
    head_offset: float = 0.10          # 코가 기준보다 어깨 너비의 10% 이상 옆으로 가면
    smooth_frames: float = 5           # 특징을 최근 몇 프레임의 중앙값으로 부드럽게 할지

    @classmethod
    def from_config(cls, cfg: dict) -> "UpperRules":
        """config.yaml의 upper_rules (없는 항목은 기본값). close_delta_mm는 thresholds에서"""
        src = dict(cfg.get("upper_rules") or {})
        if "close_delta_mm" in cfg.get("thresholds", {}):
            src.setdefault("close_delta_mm", cfg["thresholds"]["close_delta_mm"])
        known = {f.name for f in fields(cls)}
        unknown = set(src) - known
        if unknown:
            raise ValueError(f"config.yaml upper_rules에 모르는 항목: {sorted(unknown)}")
        rules = cls(**{k: float(v) for k, v in src.items()})
        for f in fields(rules):
            if getattr(rules, f.name) <= 0:
                raise ValueError(f"upper_rules.{f.name}는 0보다 커야 함")
        return rules


# ---------------------------------------------------------------------------
# 특징 떨림 완화
# ---------------------------------------------------------------------------
class FeatureSmoother:
    """최근 n프레임 특징의 중앙값. 사람이 사라지면(None) 쌓인 값을 비운다."""

    def __init__(self, n: int = 5):
        self.buf: deque[UpperFeatures] = deque(maxlen=max(1, int(n)))

    def update(self, feat: UpperFeatures | None) -> UpperFeatures | None:
        if feat is None:
            self.buf.clear()
            return None
        self.buf.append(feat)
        keys = [f.name for f in fields(UpperFeatures)]
        return UpperFeatures(**{k: float(np.median([getattr(s, k) for s in self.buf]))
                                for k in keys})

    def reset(self) -> None:
        self.buf.clear()


# ---------------------------------------------------------------------------
# 기준 자세
# ---------------------------------------------------------------------------
@dataclass
class Baseline:
    """처음 seconds 동안 바른 자세 특징을 모아 중앙값으로 기준을 만든다.
    중간에 판단 불가 프레임(None)은 건너뛰고, 모인 프레임이 min_samples보다 적으면 계속 모은다."""
    seconds: float = 10.0
    min_samples: int = 10
    _t0: float | None = None
    _samples: list[UpperFeatures] = field(default_factory=list)
    _distances: list[float] = field(default_factory=list)
    value: UpperFeatures | None = None
    distance_mm: float | None = None

    @property
    def ready(self) -> bool:
        return self.value is not None

    def remaining(self, ts: float) -> float:
        return self.seconds if self._t0 is None else max(0.0, self.seconds - (ts - self._t0))

    def add(self, feat: UpperFeatures | None, ts: float, distance_mm: float | None = None) -> bool:
        """기준 측정 중이면 샘플을 더하고, 기준이 완성되면 True"""
        if self.ready:
            return True
        if self._t0 is None:
            self._t0 = ts
        if feat is not None:
            self._samples.append(feat)
        if distance_mm is not None:
            self._distances.append(distance_mm)
        if ts - self._t0 >= self.seconds and len(self._samples) >= self.min_samples:
            keys = [f.name for f in fields(UpperFeatures)]
            med = {k: float(np.median([getattr(s, k) for s in self._samples])) for k in keys}
            self.value = UpperFeatures(**med)
            self.distance_mm = float(np.median(self._distances)) if self._distances else None
        return self.ready

    @property
    def count(self) -> int:
        return len(self._samples)

    def reset(self) -> None:
        self._t0, self.value, self.distance_mm = None, None, None
        self._samples.clear()
        self._distances.clear()


# ---------------------------------------------------------------------------
# 판단
# ---------------------------------------------------------------------------
@dataclass
class UpperJudgement:
    head: HeadState
    tilt: TiltState
    head_confidence: float
    tilt_confidence: float
    deltas: dict[str, float]           # 기준 대비 변화량 (화면 표시·threshold 조정용)
    cues: dict[str, bool]              # 어떤 단서가 threshold를 넘었는지

    @property
    def confidence(self) -> float:
        return min(self.head_confidence, self.tilt_confidence)


def unknown_judgement() -> UpperJudgement:
    return UpperJudgement(HeadState.UNKNOWN, TiltState.UNKNOWN, 0.0, 0.0, {}, {})


def judge(feat: UpperFeatures | None, base: Baseline, rules: UpperRules,
          distance_mm: float | None = None) -> UpperJudgement:
    """지금 특징을 기준 자세와 비교. 판단 불가(사람 없음, 기준 없음)면 UNKNOWN"""
    if feat is None or not base.ready:
        return unknown_judgement()
    b = base.value

    # --- 거북목: 목 높이 ↓, 얼굴 크기 ↑, (거리센서) 화면에 가까워짐 ---
    neck_drop = (b.neck_height - feat.neck_height) / b.neck_height if b.neck_height > 0 else 0.0
    face_grow = feat.face_ratio / b.face_ratio - 1 if b.face_ratio > 0 else 0.0
    cues = {
        "neck_drop": neck_drop >= rules.neck_drop_ratio,
        "face_grow": face_grow >= rules.face_grow_ratio,
    }
    deltas = {"neck_drop": neck_drop, "face_grow": face_grow}
    if distance_mm is not None and base.distance_mm is not None:
        closer = base.distance_mm - distance_mm
        deltas["closer_mm"] = closer
        cues["closer"] = closer >= rules.close_delta_mm

    cam_hits = cues["neck_drop"] + cues["face_grow"]
    if cam_hits == 0:
        # 카메라 단서가 없으면 거리만 가까워도 거북목은 아님 (상체 전체를 숙였거나 의자를 당김)
        head, head_conf = HeadState.NORMAL, 0.8
    else:
        head = HeadState.FORWARD
        head_conf = {1: 0.6, 2: 0.8}[cam_hits] + (0.15 if cues.get("closer") else 0.0)

    # --- 좌우 기울기: 어깨 선, 눈 선, 코 좌우 위치 (+ 왼쪽 / - 오른쪽) ---
    scores = {
        "shoulder_angle": (feat.shoulder_angle - b.shoulder_angle) / rules.shoulder_angle_deg,
        "head_angle": (feat.head_angle - b.head_angle) / rules.head_angle_deg,
        "head_offset": (feat.head_offset - b.head_offset) / rules.head_offset,
    }
    deltas.update({
        "shoulder_angle": feat.shoulder_angle - b.shoulder_angle,
        "head_angle": feat.head_angle - b.head_angle,
        "head_offset": feat.head_offset - b.head_offset,
    })
    over = {k: s for k, s in scores.items() if abs(s) >= 1.0}
    cues.update({k: k in over for k in scores})
    if not over:
        tilt, tilt_conf = TiltState.NONE, 0.8
    else:
        direction = sum(over.values())
        tilt = TiltState.LEFT if direction > 0 else TiltState.RIGHT
        agree = sum(1 for s in over.values() if (s > 0) == (direction > 0))
        tilt_conf = {1: 0.6, 2: 0.8, 3: 0.95}[agree]
        if agree < len(over):                  # 단서끼리 방향이 엇갈리면 확신도 낮춤
            tilt_conf = 0.4

    return UpperJudgement(head, tilt, min(head_conf, 1.0), tilt_conf, deltas, cues)


# ---------------------------------------------------------------------------
# 시간 필터
# ---------------------------------------------------------------------------
class StateFilter:
    """새 상태가 hold_sec 이상 계속될 때만 바꾼다 (잠깐 고개를 돌린 것 등은 무시)"""

    def __init__(self, hold_sec: float = 3.0, initial=None):
        self.hold = hold_sec
        self.state = initial
        self._candidate = None
        self._since: float | None = None

    def update(self, new_state, ts: float):
        if new_state == self.state:
            self._candidate, self._since = None, None
        elif new_state != self._candidate:
            self._candidate, self._since = new_state, ts
        elif ts - self._since >= self.hold:
            self.state, self._candidate, self._since = new_state, None, None
        if self.state is None:                 # 처음에는 바로 받아들임
            self.state = new_state
        return self.state

    def pending(self, ts: float) -> tuple[object, float] | None:
        """바뀌기를 기다리는 상태와 남은 시간 (화면 표시용)"""
        if self._candidate is None:
            return None
        return self._candidate, max(0.0, self.hold - (ts - self._since))
