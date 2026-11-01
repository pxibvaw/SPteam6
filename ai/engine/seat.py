"""실시간 좌면 자세 판정기 — 0.5초마다 압력(6채널) + 거리를 넣으면 지금 자세를 돌려준다

하드웨어 서버에서 쓰는 법
    from ai.engine.seat import SeatClassifier
    seat = SeatClassifier()                          # ai/models/final_seat_model.joblib 불러옴
    seat.channels                                    # 값을 넣을 채널 순서 (예: ['R_back', 'R_front', ...])
    seat.calibrate(baseline_samples)                 # 측정 시작 10초 바른 자세 값들 (줄 × 채널)
                                                     # 덜 눌린 센서가 있으면 ValueError → 측정 실패 처리
    r = seat.update(ts, pressure, distance_cm)       # 0.5초마다. pressure = 위 순서의 리스트 또는 {채널: 값}
                                                     # ts는 '초' 단위 (time.time()). 밀리초면 유지 시간이 틀어짐
    r.state        # normal / lean_left / lean_right / cross_left / cross_right / empty / unknown
    r.confidence   # 0~1, r.moving: 자세 고쳐 앉는 중(판정 멈춤)
    r.distance_state  # 화면 거리: ok(50cm 이상) / caution(40~50cm) / warning(40cm 미만) / unknown(거리 없음)
    seat.adjusted_baseline()                         # 기준이 올라갔으면 새 기준 (서버 baselines의 adjusted)

흐름 (매 0.5초)
    0. 압력 값 검사: 빈 값·범위 밖이면 ValueError (상태를 바꾸지 않음) → 서버는 그 순간을 건너뛴다
    1. 최근 2초(4개) 중앙값·흔들림, 거리(유효한 값의 중앙값, 없으면 직전 값)
    2. 움직임 감지: 0.5초 사이 크게 변한 채널이 2개 이상이거나 하나라도 0 근처를 오가지 않으면 '몸 움직임'
       (채널 하나만 0 근처를 오가면 접촉 깜빡임 → 움직임 아님). 최근 2초 안에 있었으면 moving
    3. 기준 대비 편차 특징 (ai/features/pressure_pipeline.deviation_features — 학습과 같은 함수)
       기준 방식이 raise면 기준보다 더 눌린 값이 나올 때 기준을 올린다
    4. 모델 → 자세별 확률 (또는 규칙 방식)
    5. 후처리 (PostProcessor): 움직이는 중이면 판정 멈춤 → 최근 avg_sec 확률 평균 → 가장 높은 자세가 후보
       → 후보가 hold_sec 동안 이어지고, 그 순간 평균 확률이 min_conf 이상이면 상태를 바꿈
    자리 비움: 모든 채널이 기준보다 80% 이상 빠지면 empty (학습 데이터에 없어서 규칙. 빈 의자 데이터로 조정 필요)
    6. 화면 거리 (압력 모델과 따로, 규칙): 최근 2초 거리 중앙값을 지침 기준으로 나눈다
       ok      50cm 이상   OSHA(50~100cm)·AOA(50~70cm) 권장 하한
       caution 40~50cm     고용노동부 VDT 작업관리지침(40cm 이상)은 지키지만 국제 권장보다 가까움
       warning 40cm 미만   VDT 지침 미달
       같은 구간이 dist_hold_sec(3초) 이어져야 상태를 바꿈 → 잠깐 숙이는 정도로는 경고가 안 뜸.
       거리는 cm라는 물리량이고 공인 기준이 있어서 학습하지 않는다 (압력은 사람마다 달라서 학습).
       주의: 지침은 눈~화면 거리 → 센서는 얼굴 높이를 향하게 달아야 기준과 맞음

후처리 기준값(hold_sec 등)은 재생 테스트(python -m ai.classify.replay_seat)로 비교해서 사람이 정하고,
train_pressure --final로 모델 파일에 저장한다. 모델 파일에 없으면 SeatParams의 임시 기본값을 쓴다.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from common.config import repo_path
from ai.features.pressure_pipeline import (
    ADC_MAX, BASELINE_MIN_FRAC, SEAT_LABEL_MAP, MovementParams, baseline_from_samples, deviation_features,
)

MODEL_PATH = "ai/models/final_seat_model.joblib"
SAMPLE_SEC = 0.5
UNKNOWN, EMPTY = "unknown", "empty"
RULE_LABELS = list(SEAT_LABEL_MAP.values())     # normal, lean_left, lean_right, cross_left, cross_right


@dataclass
class SeatParams:
    """후처리 기준값. 재생 테스트(replay_seat)를 보고 정한 값을 train_pressure --final ... --hold --min-conf --avg로
    모델 파일에 저장하면 그 값을 쓴다. 아래 숫자는 정하기 전에 쓰는 임시 기본값이다."""
    hold_sec: float = 4.0            # 새 자세가 이만큼 이어져야 상태를 바꿈 (ISO 11226: 4초 넘게 유지 = 정적 자세)
    avg_sec: float = 2.0             # 확률을 평균낼 시간
    min_conf: float = 0.5            # 상태를 바꿀 때 평균 확률이 이 이상이어야 함 (낮으면 이전 상태 유지)
    empty_level: float = -0.8        # 모든 채널 편차가 이보다 낮으면 자리 비움
    dist_warn_cm: float = 40.0       # 이보다 가까우면 warning (고용노동부 VDT 작업관리지침 40cm 이상)
    dist_caution_cm: float = 50.0    # 이보다 가까우면 caution (OSHA·AOA 권장 하한 50cm)
    dist_hold_sec: float = 3.0       # 같은 거리 구간이 이만큼 이어져야 상태를 바꿈
    distance_valid_cm: tuple = (15.0, 150.0)   # 이 범위 밖 거리는 측정 실패로 보고 직전 값 사용


@dataclass
class SeatResult:
    """dataclasses.asdict(result)로 바로 JSON에 넣을 수 있다 (모두 기본 숫자·문자열)"""
    ts: float
    state: str                       # 앱에 보여줄 자세
    confidence: float                # 지금 후보 자세의 평균 확률
    candidate: str | None            # 지금 순간의 후보 (hold_sec 전이거나 확신도가 낮으면 state와 다를 수 있음)
    probs: dict                      # 이번 순간 모델 확률 (움직이는 중이면 빈 dict)
    moving: bool                     # 자세를 고쳐 앉는 중 → 판정 멈춤
    empty: bool                      # 이 순간 빈 의자로 보임 (상태가 empty로 바뀌려면 hold_sec 필요)
    distance_state: str              # ok / caution / warning / unknown (dist_hold_sec 적용된 화면 거리 상태)
    distance_cm: float | None        # 최근 2초 거리 중앙값 (없으면 None)
    deviation: dict = field(default_factory=dict)   # 채널별 기준 대비 편차 (-1 ~ +1)


class PostProcessor:
    """확률 → 앱에 보여줄 상태. 실시간 판정기와 재생 테스트가 같이 쓴다"""

    def __init__(self, params: SeatParams, labels: list[str], sample_sec: float = SAMPLE_SEC):
        self.p = params
        self.labels = list(labels)
        self.n_avg = max(1, int(round(params.avg_sec / sample_sec)))
        self.reset()

    def reset(self) -> None:
        self.buf: deque = deque(maxlen=self.n_avg)
        self.state = UNKNOWN
        self.cand: str | None = None
        self.cand_since: float | None = None
        self.conf = 0.0

    def step(self, ts: float, probs: np.ndarray | None, moving: bool = False,
             empty: bool = False) -> tuple[str, float, str | None]:
        if empty:
            cand, self.conf = EMPTY, 1.0
            self.buf.clear()
        elif moving or probs is None:
            return self.state, self.conf, self.cand          # 움직이는 중: 그대로 유지
        else:
            self.buf.append(np.asarray(probs, dtype=float))
            avg = np.mean(self.buf, axis=0)
            i = int(np.argmax(avg))
            self.conf = float(avg[i])
            cand = self.labels[i]
        if cand != self.cand:                    # 후보가 바뀔 때만 유지 시간을 다시 잼
            self.cand, self.cand_since = cand, ts
        held = ts - self.cand_since >= self.p.hold_sec - 1e-9
        # 상태를 바꾸는 순간에만 확신도를 본다 (잠깐 낮아져도 유지 시간은 이어짐)
        if cand != self.state and held and self.conf >= self.p.min_conf:
            self.state = cand
        return self.state, self.conf, self.cand


class DistanceFilter:
    """거리(cm) → 화면 거리 상태. 같은 구간이 hold 동안 이어져야 바꿈 (거리 없으면 상태 유지)"""

    def __init__(self, params: SeatParams):
        self.p = params
        self.reset()

    def reset(self) -> None:
        self.state = UNKNOWN
        self.cand: str | None = None
        self.cand_since: float | None = None

    def zone(self, dist: float) -> str:
        return "warning" if dist < self.p.dist_warn_cm else "caution" if dist < self.p.dist_caution_cm else "ok"

    def step(self, ts: float, dist: float | None) -> str:
        if dist is None:
            return self.state
        cand = self.zone(dist)
        if cand != self.cand:
            self.cand, self.cand_since = cand, ts
        if cand != self.state and ts - self.cand_since >= self.p.dist_hold_sec - 1e-9:
            self.state = cand
        return self.state


class SeatClassifier:
    def __init__(self, model=MODEL_PATH, params: SeatParams | None = None, method: str = "model"):
        """model: 모델 파일 경로 또는 이미 불러온 bundle(dict). method: model(학습 모델) / rule(규칙 방식)"""
        if isinstance(model, dict):
            self.bundle = model
        else:
            import joblib
            path = repo_path(model)
            if not path.exists():
                raise FileNotFoundError(f"모델 파일이 없음: {path} — python -m ai.classify.train_pressure로 먼저 학습")
            self.bundle = joblib.load(path)
        b = self.bundle
        if method not in ("model", "rule"):
            raise ValueError("method는 model 또는 rule")
        if b.get("model_name") == "rule":
            method = "rule"                             # 규칙으로 저장된 모델 파일은 규칙 방식으로만 동작
        self.method = method
        self.model = b["model"]
        if hasattr(self.model, "n_jobs"):
            self.model.n_jobs = 1                       # 한 줄씩 예측할 때는 병렬이 오히려 느림
        self.channels: list[str] = list(b["channels"])
        self.labels: list[str] = list(b["labels"]) if method == "model" else list(RULE_LABELS)
        self._rule = None
        if method == "rule":
            from ai.classify.rules import ThresholdRule
            t = b["rule_thresholds"]
            self._rule = ThresholdRule.from_thresholds(t["t1"], t["t2"])
        elif self.model is None:
            raise ValueError("모델 파일에 학습 모델이 없음 (규칙 모델 파일이면 method='rule')")
        self.features: list[str] = list(b["feature_names"])
        self.window = int(b.get("window", 4))
        self.ref_mode = b.get("ref_mode", "fixed")
        self.use_sd, self.use_dist = bool(b.get("use_sd")), bool(b.get("use_distance"))
        self.default_dist = float(b.get("default_distance_cm", 60.0))
        self.mv = MovementParams(**b.get("movement", {}))
        self.params = params or (SeatParams(**b["post"]) if b.get("post") else SeatParams())
        self.post = PostProcessor(self.params, self.labels)
        self.dist_filter = DistanceFilter(self.params)
        self.base: pd.Series | None = None
        self._reset_buffers()

    # ------------------------------------------------------------------ 기준
    def calibrate(self, samples, check: bool = True) -> pd.Series:
        """측정 시작 10초 바른 자세 값들 (줄 × 채널, 또는 {채널: 값} 목록) → 기준값. 판정 상태도 초기화

        덜 눌린 센서가 있으면(최대치의 25% 미만) ValueError = 측정 실패 → 서버는 직전 성공 기준으로 계속
        (서버 설계: 측정 실패 시 직전 성공 기준 사용, 성공한 기준이 없으면 세션 정지). 실패하면 상태는 그대로.
        """
        rows = [self._to_row(s) for s in samples]
        base = baseline_from_samples(rows, self.channels, min_frac=BASELINE_MIN_FRAC if check else None)
        self.base = base
        self.ref = self.base.to_numpy(dtype=float).copy()
        self._reset_buffers()
        self.post.reset()
        self.dist_filter.reset()
        return self.base

    def set_baseline(self, values) -> None:
        """서버에 저장된 기준값을 그대로 쓸 때 (측정 실패 → 직전 성공 기준). 이미 검사를 통과한 값이라 검사 생략"""
        self.calibrate([values], check=False)

    def adjusted_baseline(self) -> dict | None:
        """raise 방식에서 기준이 올라갔으면 새 기준 {채널: 값}, 아니면 None"""
        if self.base is None or not np.any(self.ref > self.base.to_numpy() + 1e-9):
            return None
        return {c: round(float(v), 1) for c, v in zip(self.channels, self.ref)}

    # ------------------------------------------------------------------ 판정
    def update(self, ts: float, pressure, distance_cm: float | None = None) -> SeatResult:
        """ts: 초 단위 시각 (time.time()), pressure: 채널 순서 리스트 또는 {채널: 값}, distance_cm: 없으면 None"""
        if self.base is None:
            raise RuntimeError("기준 자세가 없음 — calibrate()를 먼저 호출")
        x = self._to_row(pressure)                       # 값이 이상하면 여기서 ValueError (상태는 그대로)
        # 움직임 (학습의 detect_movement와 같은 규칙, 실시간이라 '직전 1초'는 볼 수 없음)
        body = False
        if self._prev is not None:
            big = np.abs(x - self._prev) >= self.mv.jump
            n_big = int(big.sum())
            touches = (x <= self.mv.zero_level) | (self._prev <= self.mv.zero_level)
            flick = n_big == 1 and bool((big & touches).any())
            body = n_big >= 2 or (n_big == 1 and not flick)
        self._prev = x
        self._body.append(body)
        moving = any(self._body)
        self._buf.append(x)
        # 거리
        lo, hi = self.params.distance_valid_cm
        if distance_cm is not None and np.isfinite(distance_cm) and lo <= distance_cm <= hi:
            self._dist.append(float(distance_cm))
        elif len(self._dist):
            self._dist.append(self._dist[-1])            # 실패하면 직전 값 유지
        dist = float(np.median(self._dist)) if len(self._dist) else None
        # 특징
        arr = np.vstack(self._buf)
        med = np.median(arr, axis=0)
        if self.ref_mode == "raise":
            self.ref = np.maximum(self.ref, med)
        med_df = pd.DataFrame([med], columns=self.channels)
        ref_df = pd.DataFrame([self.ref], columns=self.channels)
        sd_df = None
        if self.use_sd:
            sd = arr.std(axis=0, ddof=1) if len(arr) > 1 else np.zeros(len(self.channels))
            sd_df = pd.DataFrame([sd], columns=[f"{c}_sd" for c in self.channels])
        dist_s = pd.Series([dist if dist is not None else self.default_dist]) if self.use_dist else None
        F = deviation_features(med_df, ref_df, self.channels, sd=sd_df, distance=dist_s)
        dev = {c: round(float(F[f"d_{c}"].iloc[0]), 3) for c in self.channels}
        empty = all(v < self.params.empty_level for v in dev.values())
        probs = None
        if not moving and not empty:
            probs = self._predict(F[self.features])
        state, conf, cand = self.post.step(ts, probs, moving=moving, empty=empty)
        dist_state = self.dist_filter.step(ts, dist)
        return SeatResult(ts=ts, state=state, confidence=round(float(conf), 3), candidate=cand,
                          probs={} if probs is None else {l: round(float(v), 3) for l, v in zip(self.labels, probs)},
                          moving=moving, empty=empty, distance_state=dist_state,
                          distance_cm=None if dist is None else round(dist, 1), deviation=dev)

    def _predict(self, F: pd.DataFrame) -> np.ndarray:
        if self.method == "rule":
            pred = self._rule.predict(F)[0]
            return np.array([1.0 if l == pred else 0.0 for l in self.labels])
        proba = self.model.predict_proba(F)[0]
        order = [list(self.model.classes_).index(l) for l in self.labels]
        return proba[order]

    def _to_row(self, v) -> np.ndarray:
        """압력 값 검사. 빈 값(None·NaN)이나 범위(0~ADC_MAX) 밖이면 ValueError → 서버는 그 순간을 건너뛴다
        (SensorHub의 '압력 실패 → 이번 순간 건너뜀'과 같은 규칙. 한 번의 이상값이 기준값을 망가뜨리지 않게)"""
        if isinstance(v, dict):
            missing = [c for c in self.channels if c not in v]
            if missing:
                raise ValueError(f"압력 값에 채널이 없음: {missing}")
            v = [v[c] for c in self.channels]
        try:
            arr = np.asarray([np.nan if e is None else e for e in np.ravel(np.asarray(v, dtype=object))],
                             dtype=float)
        except (TypeError, ValueError):
            raise ValueError(f"압력 값이 숫자가 아님: {v!r}") from None
        if arr.shape[0] != len(self.channels):
            raise ValueError(f"압력 값은 {len(self.channels)}개여야 함 ({self.channels}), 지금 {arr.shape[0]}개")
        bad = ~np.isfinite(arr) | (arr < 0) | (arr > ADC_MAX)
        if bad.any():
            raise ValueError(f"압력 값이 비었거나 범위(0~{ADC_MAX}) 밖: "
                             f"{ {c: v for c, v, b in zip(self.channels, arr, bad) if b} } — 이 순간은 건너뛰기")
        return arr

    def _reset_buffers(self) -> None:
        self._buf: deque = deque(maxlen=self.window)
        self._dist: deque = deque(maxlen=self.window)
        self._body: deque = deque(maxlen=self.mv.window)
        self._prev: np.ndarray | None = None
