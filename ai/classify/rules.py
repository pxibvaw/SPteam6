"""비교용 규칙 방식 (threshold) — 학습 스크립트가 아닌 별도 파일에 둔다

모델 파일(joblib)에 객체를 저장했다가 다른 곳(seat.py, Pi)에서 불러올 수 있으려면 클래스가
'python -m'으로 실행되는 스크립트(__main__)가 아닌 모듈에 있어야 한다. (규칙 모델은 기준값만 저장)
"""
from __future__ import annotations

import itertools

import numpy as np


class ThresholdRule:
    """비교용 규칙 방식 (사람이 정한 판단 순서 + 기준값 2개는 학습 데이터로 탐색)

        앞쪽 좌우차 LR_front <  -t1 → cross_left  (왼쪽 앞이 빠짐 = 왼다리를 들어 꼼)
                             >  +t1 → cross_right
        아니면 중간·뒤 좌우차 >  +t2 → lean_left   (오른쪽이 빠짐 = 왼쪽으로 체중)
                             <  -t2 → lean_right
        아니면 normal
    """

    T1_GRID = np.round(np.arange(0.01, 1.0001, 0.01), 3)     # 앞쪽 좌우차 기준 후보
    T2_GRID = np.round(np.arange(0.0, 0.6001, 0.005), 3)     # 옆쪽 좌우차 기준 후보

    def __init__(self):
        self.t1 = self.t2 = None
        self.at_edge: list[str] = []      # 찾은 값이 후보 범위 끝이면 기록 (범위를 넓혀야 한다는 신호)

    @staticmethod
    def _side(F):
        cols = [c for c in ("LR_mid", "LR_back") if c in F.columns]
        return F[cols].mean(axis=1).to_numpy()

    def _predict(self, F, t1, t2):
        front = F["LR_front"].to_numpy()
        side = self._side(F)
        out = np.full(len(F), "normal", dtype=object)
        out[side > t2] = "lean_left"
        out[side < -t2] = "lean_right"
        out[front < -t1] = "cross_left"
        out[front > t1] = "cross_right"
        return out

    def fit(self, F, y):
        """학습 데이터 정확도가 가장 높은 (t1, t2)를 후보 격자에서 찾는다"""
        y = np.asarray(y)
        best = (-1, None)
        for t1, t2 in itertools.product(self.T1_GRID, self.T2_GRID):
            acc = (self._predict(F, t1, t2) == y).mean()
            if acc > best[0]:
                best = (acc, (t1, t2))
        self.t1, self.t2 = (float(v) for v in best[1])
        self.at_edge = [name for name, v, grid in (("t1", self.t1, self.T1_GRID), ("t2", self.t2, self.T2_GRID))
                        if v in (grid[0], grid[-1]) and not (name == "t2" and v == 0.0)]
        return self

    def predict(self, F):
        return self._predict(F, self.t1, self.t2)

    @classmethod
    def from_thresholds(cls, t1: float, t2: float) -> "ThresholdRule":
        rule = cls()
        rule.t1, rule.t2 = float(t1), float(t2)
        return rule
