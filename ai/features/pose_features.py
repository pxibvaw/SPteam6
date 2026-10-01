"""카메라 점 → 판단용 특징

1. PosturePal(MIT) 방식: 목 기준 좌표 + 점 쌍 사이 코사인 각도  → posturepal_features()
2. 우리 특징 5개 (기준 자세와 비교용)                            → upper_features()

좌표 방향 (웹캠 원본 영상, 좌우 반전 전)
    x: 화면 왼쪽 → 오른쪽,  y: 화면 위 → 아래 (아래로 갈수록 큼)
    사람의 왼쪽(left_*)이 화면 오른쪽에 찍힌다.

모든 길이는 어깨 너비로 나눠서 비율로 쓴다 → 카메라와 가까워지거나 멀어져도 비교 가능.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import asdict, dataclass

import numpy as np

from common.schema import LANDMARK_NAMES
from common.sensors_base import PoseReading

# 판단에 꼭 필요한 점 (귀는 가려질 수 있어서 제외)
REQUIRED_POINTS = ("nose", "left_eye", "right_eye", "left_shoulder", "right_shoulder")


def to_pixels(pose: PoseReading, width: int, height: int) -> dict[str, np.ndarray]:
    """0~1 비율 좌표 → 픽셀 좌표. 16:9 화면에서 각도를 제대로 계산하려면 꼭 필요"""
    return {name: np.array([lm.x * width, lm.y * height]) for name, lm in pose.points.items()}


def neck_point(px: dict[str, np.ndarray]) -> np.ndarray:
    """목 = 두 어깨의 중점 (OpenPose의 목 점과 같은 위치)"""
    return (px["left_shoulder"] + px["right_shoulder"]) / 2


def _tilt_deg(right_pt: np.ndarray, left_pt: np.ndarray) -> float:
    """오른쪽 점 → 왼쪽 점 선이 수평과 이루는 각도.
    + 이면 사람 왼쪽이 더 낮음 (왼쪽으로 기울어짐), - 이면 오른쪽으로 기울어짐"""
    dx, dy = left_pt - right_pt
    return math.degrees(math.atan2(dy, dx))


def usable(pose: PoseReading | None, min_visibility: float = 0.5) -> bool:
    """판단에 쓸 수 있는 프레임인지 (사람 인식 + 필수 점이 잘 보임)"""
    if pose is None or not pose.detected:
        return False
    for name in REQUIRED_POINTS:
        lm = pose.points.get(name)
        if lm is None or not lm.is_finite() or lm.visibility < min_visibility:
            return False
    return True


# ---------------------------------------------------------------------------
# 우리 특징 5개
# ---------------------------------------------------------------------------
@dataclass
class UpperFeatures:
    neck_height: float       # ① (목 높이 − 코 높이) ÷ 어깨 너비   거북목이면 ↓
    face_ratio: float        # ② 두 눈 사이 거리 ÷ 어깨 너비        거북목이면 ↑ (얼굴만 가까워짐)
    shoulder_angle: float    # ③ 어깨 선 기울기 (도)                 + 왼쪽 / - 오른쪽
    head_angle: float        # ④ 두 눈 선 기울기 (도)                + 왼쪽 / - 오른쪽
    head_offset: float       # ⑤ (코 x − 목 x) ÷ 어깨 너비           + 왼쪽 / - 오른쪽
    shoulder_width_px: float  # 참고용 (화면 속 어깨 너비)

    def as_dict(self) -> dict[str, float]:
        return asdict(self)


def upper_features(pose: PoseReading | None, width: int, height: int,
                   min_visibility: float = 0.5) -> UpperFeatures | None:
    """판단할 수 없는 프레임(사람 없음, 점이 가려짐, 옆으로 돌아앉음)이면 None"""
    if not usable(pose, min_visibility):
        return None
    px = to_pixels(pose, width, height)
    ls, rs = px["left_shoulder"], px["right_shoulder"]
    sw = float(np.linalg.norm(ls - rs))
    if sw < 1.0 or ls[0] <= rs[0]:
        # 어깨가 겹치거나 좌우가 뒤집힘 = 옆이나 뒤를 보고 있음 → 정면 판단 불가
        return None
    neck = neck_point(px)
    nose = px["nose"]
    eye_dist = float(np.linalg.norm(px["left_eye"] - px["right_eye"]))
    return UpperFeatures(
        neck_height=float(neck[1] - nose[1]) / sw,
        face_ratio=eye_dist / sw,
        shoulder_angle=_tilt_deg(rs, ls),
        head_angle=_tilt_deg(px["right_eye"], px["left_eye"]),
        head_offset=float(nose[0] - neck[0]) / sw,
        shoulder_width_px=sw,
    )


# ---------------------------------------------------------------------------
# PosturePal 방식 (학습 모델 비교용)
# ---------------------------------------------------------------------------
def posturepal_features(pose: PoseReading | None, width: int, height: int,
                        scale: bool = False) -> dict[str, float] | None:
    """점 7개 + 목(계산) 기준 특징. PosturePal 그대로면 scale=False.

    - 목 기준 좌표: 각 점 − 목 (14개)
    - 코사인 각도: 목에서 각 점으로 가는 벡터 두 개씩 사이의 cos (7개 중 2개 = 21개)
    scale=True면 좌표를 어깨 너비로 나눈다 (우리 추가 방식).
    점이 없으면 NaN (학습 전에 처리).
    """
    if pose is None or not pose.detected:
        return None
    px = to_pixels(pose, width, height)
    if "left_shoulder" not in px or "right_shoulder" not in px:
        return None
    neck = neck_point(px)
    sw = float(np.linalg.norm(px["left_shoulder"] - px["right_shoulder"])) if scale else 1.0
    if sw < 1.0:
        return None
    feats: dict[str, float] = {}
    vec: dict[str, np.ndarray | None] = {}
    for name in LANDMARK_NAMES:
        v = (px[name] - neck) / sw if name in px else None
        vec[name] = v
        feats[f"{name}_dx"] = float(v[0]) if v is not None else math.nan
        feats[f"{name}_dy"] = float(v[1]) if v is not None else math.nan
    for a, b in itertools.combinations(LANDMARK_NAMES, 2):
        va, vb = vec[a], vec[b]
        if va is None or vb is None:
            feats[f"cos_{a}__{b}"] = math.nan
            continue
        na, nb = np.linalg.norm(va), np.linalg.norm(vb)
        feats[f"cos_{a}__{b}"] = float(va @ vb / (na * nb)) if na > 0 and nb > 0 else math.nan
    return feats
