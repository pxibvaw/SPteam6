"""AI 판정 엔진 어댑터 — MockEngine의 기준 자세·착석 구간·응답 만들기는 그대로 쓰고, 판단만 AI 엔진(SitSenseEngine)으로 바꾼다

real 서버(real_runtime.py)와 mock 서버(--engine ai)가 같이 쓴다.
    → mock 데이터를 넣어도 실제와 같은 판정 경로(AI 엔진 → 서버 → 앱)로 돌아가는지 확인할 수 있다.
sklearn·모델 파일은 이 엔진을 만들 때만 불러온다 (가짜 판정만 쓰는 경우에는 필요 없음).
"""
from __future__ import annotations

import logging

from common.schema import DistanceLevel, HeadState, SeatState, TiltState
from common.sensors_base import Sample
from hardware.mock.scenarios import Step
from hardware.server.mock_engine import MockEngine
from hardware.server.seating import is_empty

log = logging.getLogger("sitsense.ai")


class PassThroughFilter:
    """AI 엔진이 이미 유지 시간(4초)을 거친 확정 상태를 주므로 서버는 시간 필터를 또 걸지 않는다
    (또 걸면 반영이 8초 넘게 늦어짐 — docs/ai_engine.md)"""

    def update(self, new_state, ts: float):
        return new_state

    def pending(self, ts: float):
        return None


class AiEngine(MockEngine):
    """MockEngine의 기준 자세 흐름·착석 구간·응답 만들기는 그대로 쓰고, 판단만 AI 엔진(SitSenseEngine)으로 바꾼다.

    - 기준 측정: 서버의 품질 검사(no_person / sensor_lost / too_much_motion)를 통과하면 AI 엔진 calibrate()
      → 실패하면 pressure_low / no_person (직전 기준 유지)
    - 매 순간: AI 엔진 결과(좌면·목·기울기 확정 상태, pending, 거리 단계)를 그대로 쓴다
    - 판단 엔진을 못 불러오면(모델 파일 없음 등) 서버를 켤 때 바로 에러로 알린다
    """

    def __init__(self, cfg: dict, seed: int | None = None, *, auto_baseline: bool = True,
                 baseline=None, require_camera: bool = True):
        from ai.engine.sitsense_engine import SitSenseEngine    # 모델·sklearn을 real 모드에서만 불러옴
        super().__init__(cfg, seed, auto_baseline=auto_baseline, baseline=baseline)
        self.ai = SitSenseEngine(cfg, require_camera=require_camera)
        self.require_pose = require_camera
        self.filters = {k: PassThroughFilter() for k in ("seat", "head", "tilt")}
        self._r = None
        if baseline is not None:                    # 저장된 기준으로 시작 (서버 재시작 등)
            self.ai.restore(baseline.pressure, baseline.distance_mm, baseline.pose)

    # --- 기준 자세 ----------------------------------------------------------
    def on_calibrate(self, samples):
        res = self.ai.calibrate(samples)
        if not res.ok:
            log.warning("AI 기준 측정 실패: %s (%s)", res.reason, res.message)
        return res.reason

    # --- 판단 ---------------------------------------------------------------
    def update(self, sample: Sample, step: Step) -> dict:
        self._r = self.ai.update(sample)
        return super().update(sample, step)

    def judge_seat(self, sample: Sample, step: Step):
        if is_empty(sample.pressure.values):        # 기준 측정 전에도 빈 의자는 알아야 함 (착석 구간)
            return SeatState.EMPTY, 0.95
        r = self._r
        return SeatState(r.seat_state), float(r.confidence["seat"])

    def judge_upper(self, sample: Sample, step: Step):
        r = self._r
        return (HeadState(r.head_state), TiltState(r.tilt_state),
                float(r.confidence["head"]), float(r.confidence["tilt"]), r.deltas, r.cues)

    def pending_list(self, ts: float) -> list[dict]:
        return list(self._r.pending) if self._r is not None else []

    def distance_level_of(self, dist_mm):
        # 화면 거리는 엔진의 4초 유지 단계를 쓴다 (잠깐 숙일 때 경고가 깜빡이지 않게)
        return DistanceLevel(self._r.distance_level) if self._r is not None else DistanceLevel.UNKNOWN
