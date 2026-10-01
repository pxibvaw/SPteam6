"""압력 특징 — 몸 중심(압력 중심)

압력 중심 = 각 FSR 위치를 그 센서 값으로 가중평균한 점 = 체중이 실린 중심
    x: 왼쪽 -1 ~ 오른쪽 +1, y: 뒤 -1 ~ 앞 +1 (사람 기준, config.yaml의 pressure.positions)
"""
from __future__ import annotations

import numpy as np


def center_of_pressure(values, positions, min_total: float = 50.0,
                       channels: list[int] | None = None) -> tuple[float, float] | None:
    """압력 중심 (x, y). 전체 압력이 min_total보다 작으면(자리 비움) None

    channels: 8개 중 일부만 쓸 때 (6개 선별 후) 채널 번호 목록
    """
    v = np.asarray(values, dtype=float)
    pos = np.asarray(positions, dtype=float)
    if v.shape[0] != pos.shape[0]:
        raise ValueError(f"압력 값 {v.shape[0]}개와 위치 {pos.shape[0]}개의 수가 다름")
    if channels is not None:
        v, pos = v[channels], pos[channels]
    v = np.clip(v, 0, None)
    total = v.sum()
    if total < min_total:
        return None
    x, y = (v[:, None] * pos).sum(axis=0) / total
    return float(x), float(y)
