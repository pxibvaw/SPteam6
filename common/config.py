"""config.yaml 읽기 + 검사

사용 예:
    from common.config import load_config, repo_path, fsr_positions
    cfg = load_config()
    hz = cfg["sampling"]["hz"]
    raw_dir = repo_path(cfg["paths"]["raw"])
    pos = fsr_positions(cfg)          # (8, 2) 배열

설정이 잘못되면 ConfigError가 어느 항목이 왜 틀렸는지 알려준다.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

MODES = ("mock", "replay", "real")
MCP3008_CHANNELS = 8


class ConfigError(Exception):
    """config.yaml이 없거나, 문법이 틀렸거나, 값이 잘못됨"""


def repo_path(relative: str | Path) -> Path:
    """저장소 최상위 기준 경로 → 절대 경로 (어디서 실행해도 같은 파일을 가리키게)"""
    p = Path(relative)
    return p if p.is_absolute() else REPO_ROOT / p


def load_config(path: str | Path = "config.yaml") -> dict:
    full = repo_path(path)
    try:
        with open(full, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
    except FileNotFoundError:
        raise ConfigError(f"설정 파일이 없음: {full}") from None
    except yaml.YAMLError as e:
        raise ConfigError(f"{full} 문법 오류 (들여쓰기·콜론 확인):\n{e}") from None
    if not isinstance(cfg, dict):
        raise ConfigError(f"{full}이 비어 있거나 형식이 잘못됨")
    validate_config(cfg)
    return cfg


def fsr_positions(cfg: dict) -> np.ndarray:
    """FSR 채널별 방석 위치 (n, 2). x: 왼쪽 -1 ~ 오른쪽 +1, y: 뒤 -1 ~ 앞 +1 (사람 기준)"""
    return np.asarray(cfg["pressure"]["positions"], dtype=float)


# ---------------------------------------------------------------------------
# 검사
# ---------------------------------------------------------------------------
def _get(cfg: dict, key: str):
    """'pressure.n_channels' 같은 경로로 값을 꺼낸다. 없으면 ConfigError"""
    cur = cfg
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise ConfigError(f"config.yaml에 '{key}' 항목이 없음")
        cur = cur[part]
    return cur


def _number(cfg: dict, key: str, *, min_value: float | None = None,
            max_value: float | None = None, integer: bool = False) -> float:
    v = _get(cfg, key)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ConfigError(f"'{key}'는 숫자여야 함 (지금: {v!r})")
    if integer and not isinstance(v, int):
        raise ConfigError(f"'{key}'는 정수여야 함 (지금: {v!r})")
    if min_value is not None and v < min_value:
        raise ConfigError(f"'{key}'는 {min_value} 이상이어야 함 (지금: {v})")
    if max_value is not None and v > max_value:
        raise ConfigError(f"'{key}'는 {max_value} 이하여야 함 (지금: {v})")
    return v


def validate_config(cfg: dict) -> None:
    mode = _get(cfg, "mode")
    if mode not in MODES:
        raise ConfigError(f"'mode'는 {MODES} 중 하나여야 함 (지금: {mode!r})")
    subject = _get(cfg, "subject")
    if not isinstance(subject, str) or not subject.strip():
        raise ConfigError("'subject'는 비어 있지 않은 문자열이어야 함 (예: p1)")

    _number(cfg, "sampling.hz", min_value=0.1, max_value=100)

    # 압력: MCP3008 1개 = 8채널
    n = _number(cfg, "pressure.n_channels", min_value=1, max_value=MCP3008_CHANNELS,
                integer=True)
    _number(cfg, "pressure.adc_max", min_value=1, integer=True)
    pos = _get(cfg, "pressure.positions")
    if not isinstance(pos, list) or len(pos) != n:
        got = len(pos) if isinstance(pos, list) else pos
        raise ConfigError(f"'pressure.positions'는 채널 수({n})만큼 있어야 함 (지금: {got})")
    for i, p in enumerate(pos):
        ok = (isinstance(p, list) and len(p) == 2
              and all(isinstance(c, (int, float)) and -1 <= c <= 1 for c in p))
        if not ok:
            raise ConfigError(f"'pressure.positions'의 p{i} 위치 {p!r}가 잘못됨 "
                              f"([x, y], 각각 -1 ~ 1)")

    # 거리
    lo = _number(cfg, "distance.min_mm", min_value=0)
    hi = _number(cfg, "distance.max_mm", min_value=1)
    if lo >= hi:
        raise ConfigError(f"'distance.min_mm'({lo})가 'distance.max_mm'({hi})보다 작아야 함")
    _number(cfg, "distance.median_window", min_value=1, integer=True)

    # 카메라
    _number(cfg, "camera.width", min_value=1, integer=True)
    _number(cfg, "camera.height", min_value=1, integer=True)
    _number(cfg, "camera.fps", min_value=0.1)

    # 기준값
    _number(cfg, "thresholds.baseline_seconds", min_value=0)
    ok_mm = _number(cfg, "thresholds.distance_ok_mm", min_value=1)
    warn_mm = _number(cfg, "thresholds.distance_warning_mm", min_value=1)
    if warn_mm >= ok_mm:
        raise ConfigError(f"'thresholds.distance_warning_mm'({warn_mm})가 "
                          f"'distance_ok_mm'({ok_mm})보다 작아야 함")
    short = _number(cfg, "thresholds.short_filter_sec", min_value=0)
    long_ = _number(cfg, "thresholds.long_filter_sec", min_value=0)
    if short > long_:
        raise ConfigError(f"'short_filter_sec'({short})가 'long_filter_sec'({long_})보다 클 수 없음")

    for key in ("paths.raw", "paths.synthetic", "paths.processed", "paths.models"):
        if not isinstance(_get(cfg, key), str):
            raise ConfigError(f"'{key}'는 경로 문자열이어야 함")

    replay_file = _get(cfg, "replay.file")
    if replay_file is not None and not isinstance(replay_file, str):
        raise ConfigError("'replay.file'은 경로 문자열 또는 null이어야 함")
    if not isinstance(_get(cfg, "replay.loop"), bool):
        raise ConfigError("'replay.loop'는 true 또는 false여야 함")
