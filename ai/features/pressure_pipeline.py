"""압력(FSR) + 거리(HC-SR04) 수집 데이터 읽기 → 정리 → 움직임 감지

학습(ai/classify)과 실시간 판정(ai/engine)이 같은 함수를 쓰도록 여기 모은다.

입력 파일 (data/raw/pressure/, git에 올라가지 않음)
    {사람}_posture.csv   timestamp, label, sample, elapsed_s, CH0(맨 뒤 오른쪽), CH1(맨 앞 오른쪽), …
    {사람}_distance.csv  timestamp, sample, distance_cm
    - 두 파일의 timestamp가 같다 → timestamp로 조인 (다르면 가장 가까운 값끼리, 0.25초 이내)
    - 0.5초마다 한 줄 (2Hz). 자세 하나 = 3분 = 360줄, 자세가 바뀌면 sample이 0부터 다시 시작
    - label 예: '왼다리꼬기(앞)' → 자세 cls='왼다리꼬기', 앉은 위치 depth='앞'

채널 이름
    CSV 열 이름의 괄호 속 위치('맨 앞 왼쪽' 등)를 읽어서 위치 이름으로 바꾼다.
        L_front L_mid L_back / R_front R_mid R_back / C_front C_mid C_back
    채널 번호가 바뀌거나 빠져도(지금은 CH2 없음) 열 이름만 맞으면 그대로 동작한다.

움직임 감지 (detect_movement)
    직접 수집하다 보니 3분 중간에 몸을 고쳐 앉는 몇 초가 섞여 있다 (라벨과 실제 자세가 어긋나는 순간).
    어떤 채널이든 0.5초 사이 jump 이상 변하면, 그 순간부터 2초(window)와 그 직전 1초(before)를 움직임으로 표시한다.
    (몸을 움직이기 시작할 때는 작은 변화가 먼저 오고, 큰 변화 뒤에도 값이 잠시 흔들려서)
    학습에서는 이 구간을 빼고, 실시간에서는 이 구간에 판정을 멈추고 이전 상태를 유지한다.

접촉 깜빡임 (detect_flicker) — 움직임과 구분
    센서 하나만 0 근처(접촉 끊김)와 높은 값을 오가는 건 몸이 움직인 게 아니라 접촉 불량이다.
    '그 순간 크게 변한 채널이 하나뿐이고, 변하기 전이나 후 값이 0 근처'면 깜빡임으로 보고 움직임에서 뺀다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

SAMPLE_SEC = 0.5                      # 수집 간격
ADC_MAX = 4095                        # MCP3208 12-bit

# 수집 데이터 라벨(한글) → common/schema.py 좌면 라벨
SEAT_LABEL_MAP = {
    "바른자세": "normal",
    "왼쪽편향": "lean_left",
    "오른쪽편향": "lean_right",
    "왼다리꼬기": "cross_left",
    "오른다리꼬기": "cross_right",
}
DEPTH_MAP = {"앞": "front", "중": "middle", "뒤": "back"}

_ROW = {"앞": "front", "중간": "mid", "뒤": "back"}
_SIDE = {"왼쪽": "L", "오른쪽": "R", "가운데": "C"}


@dataclass
class MovementParams:
    jump: float = 800.0               # 0.5초 사이 이만큼 변하면 '움직임' (ADC 값)
    window: int = 4                   # 큰 변화부터 몇 개(=2초)를 움직임으로 볼지
    before: int = 2                   # 큰 변화 직전 몇 개(=1초)를 더 표시할지
    zero_level: float = 10.0          # 이 값 이하 = 접촉 끊김 (깜빡임 판단용)


def show_path(path: str | Path) -> str:
    """화면 출력용 경로: 저장소 안이면 상대 경로, 밖이면 그대로"""
    from common.config import REPO_ROOT
    path = Path(path).resolve()
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def channel_key(col: str) -> str | None:
    """'CH4(맨 앞 왼쪽)' → 'L_front'. CH 열이 아니면 None"""
    m = re.match(r"\s*CH(\d+)\s*\((.*)\)", col)
    if not m:
        return None
    desc = m.group(2)
    row = next((v for k, v in _ROW.items() if k in desc), None)
    side = next((v for k, v in _SIDE.items() if k in desc), None)
    if row is None or side is None:
        raise ValueError(f"채널 위치를 읽을 수 없음: {col!r} (예: 'CH4(맨 앞 왼쪽)')")
    return f"{side}_{row}"


def load_session(posture_csv: str | Path, distance_csv: str | Path | None = None,
                 person: str | None = None) -> tuple[pd.DataFrame, list[str]]:
    """수집 파일 1쌍 → (DataFrame, 채널 이름 목록)

    열: ts(datetime), person, block(1부터), elapsed_s, label, cls, seat_label, depth, 채널들, distance_cm
    """
    p = pd.read_csv(posture_csv, encoding="utf-8-sig")
    need = {"timestamp", "label", "sample"}
    if not need <= set(p.columns):
        raise ValueError(f"{posture_csv}: {sorted(need - set(p.columns))} 열이 없음")
    rename, channels = {}, []
    for c in p.columns:
        k = channel_key(c)
        if k:
            if k in channels:
                raise ValueError(f"{posture_csv}: 같은 위치 채널이 두 개 ({k})")
            rename[c] = k
            channels.append(k)
    if not channels:
        raise ValueError(f"{posture_csv}: CH 열이 없음")
    p = p.rename(columns=rename)
    p["ts"] = pd.to_datetime(p["timestamp"])
    p["block"] = (p["sample"] == 0).cumsum()
    if "elapsed_s" not in p.columns:
        p["elapsed_s"] = p["sample"] * SAMPLE_SEC
    p["cls"] = p["label"].str.replace(r"\s*\(.*\)", "", regex=True).str.strip()
    p["depth"] = p["label"].str.extract(r"\((.)\)")[0].map(DEPTH_MAP)
    p["seat_label"] = p["cls"].map(SEAT_LABEL_MAP)
    unknown = sorted(set(p.loc[p["seat_label"].isna(), "cls"]))
    if unknown:
        raise ValueError(f"{posture_csv}: 모르는 자세 라벨 {unknown} → SEAT_LABEL_MAP에 추가")
    p["person"] = person or Path(posture_csv).stem.split("_")[0]

    if distance_csv is not None:
        d = pd.read_csv(distance_csv, encoding="utf-8-sig")
        d["ts"] = pd.to_datetime(d["timestamp"])
        if len(d) == len(p) and (d["ts"].values == p["ts"].values).all():
            p["distance_cm"] = d["distance_cm"].values          # 시각이 완전히 같음
        else:
            m = pd.merge_asof(p[["ts"]].sort_values("ts"), d[["ts", "distance_cm"]].sort_values("ts"),
                              on="ts", direction="nearest", tolerance=pd.Timedelta(seconds=0.25))
            p["distance_cm"] = m["distance_cm"].values
    else:
        p["distance_cm"] = np.nan
    cols = ["ts", "person", "block", "elapsed_s", "label", "cls", "seat_label", "depth"]
    return p[cols + channels + ["distance_cm"]].reset_index(drop=True), channels


def load_folder(folder: str | Path) -> tuple[pd.DataFrame, list[str]]:
    """폴더 안의 {사람}_posture.csv(+ {사람}_distance.csv)를 모두 읽어 합친다"""
    folder = Path(folder)
    files = sorted(folder.glob("*_posture.csv"))
    if not files:
        raise FileNotFoundError(f"{folder}에 *_posture.csv가 없음")
    frames, channels = [], None
    for f in files:
        person = f.stem.split("_")[0]
        dist = folder / f"{person}_distance.csv"
        df, ch = load_session(f, dist if dist.exists() else None, person)
        if channels is None:
            channels = ch
        elif set(ch) != set(channels):
            raise ValueError(f"{f.name}의 채널 {ch}이 다른 파일 {channels}과 다름")
        frames.append(df)
    return pd.concat(frames, ignore_index=True), channels


def _jump_events(g: pd.DataFrame, channels: list[str], params: MovementParams):
    """한 구간에서 (몸 움직임 순간, 접촉 깜빡임 순간) 두 가지 bool 배열"""
    x = g[channels].astype(float)
    prev = x.shift()
    big = x.diff().abs() >= params.jump                       # 채널별 큰 변화
    touches_zero = (x <= params.zero_level) | (prev <= params.zero_level)
    n_big = big.sum(axis=1)
    flick = (n_big == 1) & (big & touches_zero).any(axis=1)  # 채널 하나만, 0 근처를 오감
    body = (n_big >= 2) | ((n_big == 1) & ~flick)
    return body.values, flick.values


def _spread(mask: np.ndarray, params: MovementParams) -> np.ndarray:
    """큰 변화 순간 → 그 순간부터 window개 + 직전 before개까지 표시"""
    s = pd.Series(mask)
    out = s.rolling(params.window, min_periods=1).max().astype(bool)
    if params.before:                         # 뒤집어서 굴리면 '앞쪽' 몇 개가 표시됨
        out = out | s[::-1].rolling(params.before + 1, min_periods=1).max()[::-1].astype(bool)
    return out.values


def detect_movement(df: pd.DataFrame, channels: list[str],
                    params: MovementParams = MovementParams()) -> pd.Series:
    """자세를 고쳐 앉는 순간 = True (접촉 깜빡임은 제외). 사람·자세 구간(block)마다 따로 계산"""
    out = pd.Series(False, index=df.index)
    for _, g in df.groupby(["person", "block"], sort=False):
        body, _ = _jump_events(g, channels, params)
        out.loc[g.index] = _spread(body, params)
    return out


def detect_flicker(df: pd.DataFrame, channels: list[str],
                   params: MovementParams = MovementParams()) -> pd.Series:
    """접촉 깜빡임 순간 = True (센서 하나가 0 근처와 높은 값을 오감)"""
    out = pd.Series(False, index=df.index)
    for _, g in df.groupby(["person", "block"], sort=False):
        _, flick = _jump_events(g, channels, params)
        out.loc[g.index] = flick
    return out


def flicker_by_channel(df: pd.DataFrame, channels: list[str],
                       params: MovementParams = MovementParams()) -> dict[str, int]:
    """채널별 접촉 깜빡임 횟수 (어느 센서 접촉이 불량한지)"""
    counts = dict.fromkeys(channels, 0)
    for _, g in df.groupby(["person", "block"], sort=False):
        x = g[channels].astype(float)
        big = x.diff().abs() >= params.jump
        tz = (x <= params.zero_level) | (x.shift() <= params.zero_level)
        single = big.sum(axis=1) == 1
        for c in channels:
            counts[c] += int((big[c] & tz[c] & single).sum())
    return counts


def distance_valid(dist: pd.Series, lo: float = 15.0, hi: float = 150.0) -> pd.Series:
    """초음파 값이 쓸 만한지 (빈 값, 벽 반사 165cm 등은 False)"""
    return dist.notna() & (dist >= lo) & (dist <= hi)


def signal_report(df: pd.DataFrame, channels: list[str], movement: pd.Series,
                  flicker: pd.Series | None = None, sat_level: float = 3700,
                  zero_level: float = 10, jump: float = 800) -> pd.DataFrame:
    """사람·자세 구간별 신호 상태 요약표 (발표 '아날로그 문제' 근거)"""
    rows = []
    for (person, block), g in df.groupby(["person", "block"], sort=False):
        x = g[channels].astype(float)
        dv = distance_valid(g["distance_cm"])
        jumps = (x.diff().abs() >= jump).sum().sum()
        rows.append({
            "person": person, "block": block, "label": g["label"].iloc[0], "rows": len(g),
            "movement_pct": round(movement.loc[g.index].mean() * 100, 1),
            "movement_in_first10s_pct": round(
                movement.loc[g.index][g["elapsed_s"] < 10].sum() / max(movement.loc[g.index].sum(), 1) * 100, 1),
            "jumps": int(jumps),
            "flicker": int(flicker.loc[g.index].sum()) if flicker is not None else np.nan,
            "saturated_pct": round((x >= sat_level).mean().mean() * 100, 1),
            "zero_pct": round((x <= zero_level).mean().mean() * 100, 1),
            "dist_missing_pct": round(g["distance_cm"].isna().mean() * 100, 1),
            "dist_outlier_pct": round((g["distance_cm"].notna() & ~dv).mean() * 100, 1),
            "dist_median_cm": round(g.loc[dv, "distance_cm"].median(), 1) if dv.any() else np.nan,
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 2초 묶음 + 하중 빠짐 특징 (학습·실험 공통)
# ---------------------------------------------------------------------------
def make_windows(df: pd.DataFrame, channels: list[str], size: int = 4,
                 exclude: pd.Series | None = None) -> pd.DataFrame:
    """사람·자세 구간마다 size개(=2초)씩 겹치지 않게 묶어 채널 중앙값을 낸다

    exclude(예: 움직임 구간)가 True인 줄은 빼고 묶는다.
    열: person, block, label, cls, seat_label, depth, 채널들(중앙값), distance_cm(유효값 중앙값)
    """
    if exclude is not None:
        df = df[~exclude.reindex(df.index, fill_value=False)]
    rows = []
    for (person, block), g in df.groupby(["person", "block"], sort=False):
        dv = distance_valid(g["distance_cm"])
        for i in range(0, len(g) - size + 1, size):
            w = g.iloc[i:i + size]
            r = {"person": person, "block": block, "label": w["label"].iloc[0], "cls": w["cls"].iloc[0],
                 "seat_label": w["seat_label"].iloc[0], "depth": w["depth"].iloc[0]}
            r.update(w[channels].median().to_dict())
            wd = w.loc[dv.loc[w.index], "distance_cm"]
            r["distance_cm"] = wd.median() if len(wd) else np.nan
            rows.append(r)
    return pd.DataFrame(rows)


def _side_row(ch: str) -> tuple[str, str]:
    side, row = ch.split("_")
    return side, row


def unloading_features(win: pd.DataFrame, channels: list[str], ceil_q: float = 0.95,
                       unloaded_level: float = 0.3) -> pd.DataFrame:
    """포화를 역이용한 '하중 빠짐' 특징 (사람마다 천장값을 따로 추정)

    천장값  = 그 사람·채널 값의 상위 (1 - ceil_q) 지점 → '꽉 눌림'
    하중 빠짐 u = (천장값 - 값) / 천장값, 0(꽉 눌림) ~ 1(완전히 들림)
    특징: 채널별 u, 앞·중간·뒤 좌우차(왼쪽 - 오른쪽, +면 왼쪽이 더 들림), 전체 좌우차,
          대각선(다리 꼬기), 들린 센서 개수
    실험에서는 사람 전체 데이터로 천장값을 정하고, 실시간에서는 최근 몇 분 값으로 계속 갱신한다.
    """
    F = pd.DataFrame(index=win.index)
    left = [c for c in channels if _side_row(c)[0] == "L"]
    right = [c for c in channels if _side_row(c)[0] == "R"]
    for person in win["person"].unique():
        m = win["person"] == person
        ceil = win.loc[m, channels].quantile(ceil_q).replace(0, 1)
        u = ((ceil - win.loc[m, channels]) / ceil).clip(0, 1)
        for c in channels:
            F.loc[m, f"u_{c}"] = u[c]
        for row in ("front", "mid", "back"):
            if f"L_{row}" in channels and f"R_{row}" in channels:
                F.loc[m, f"LR_{row}"] = u[f"L_{row}"] - u[f"R_{row}"]
        if left and right:
            F.loc[m, "LR_all"] = u[left].mean(axis=1) - u[right].mean(axis=1)
        if {"L_front", "R_back", "R_front", "L_back"} <= set(channels):
            F.loc[m, "diag"] = (u["L_front"] + u["R_back"]) - (u["R_front"] + u["L_back"])
        F.loc[m, "n_unloaded"] = (u > unloaded_level).sum(axis=1)
    return F


# ---------------------------------------------------------------------------
# 기준 자세 대비 편차 (학습·실시간 공통) — 원래 설계: 측정 시작마다 바른 자세 10초를 기준으로
# ---------------------------------------------------------------------------
BASELINE_CLS = "바른자세"            # 수집 데이터에서 '세션 시작 기준 측정' 대신 쓸 구간
BASELINE_DEPTH = "middle"            # 바른자세(중)
BASELINE_SAMPLES = 20                # 10초 = 20개
BASELINE_MIN_FRAC = 0.25             # 기준 측정 때 모든 센서가 최대치(ADC_MAX)의 25% 이상 눌려야 유효
                                     # (자세를 잡는 중이라 덜 눌린 센서가 기준에 들어가면 편차가 망가짐 — p2 사례)
REF_MODES = ("fixed", "raise")       # fixed: 시작 기준 고정 / raise: 더 눌린 값이 나오면 기준을 올림
DEFAULT_DROP = ("C_back",)           # 센서 제거 실험 결과 제외한 채널 (기여도 0, 접촉 끊김 35%)


def select_channels(channels: list[str], drop=DEFAULT_DROP) -> list[str]:
    return [c for c in channels if c not in set(drop or ())]


def session_baselines(df: pd.DataFrame, channels: list[str],
                      movement: pd.Series | None = None, n: int = BASELINE_SAMPLES,
                      min_frac: float = BASELINE_MIN_FRAC) -> dict[str, pd.Series]:
    """사람마다 '측정 시작 10초 바른 자세' 기준값 (채널별 중앙값)

    수집 데이터에는 따로 찍은 시작 측정이 없어서, 바른자세(중) 구간에서 움직임이 없고
    모든 센서가 최대치의 min_frac 이상 눌린 첫 n개(10초)로 대신한다.
    (구간 시작 직후 자세를 잡는 동안 덜 눌린 센서가 기준에 들어가지 않게)
    실제 서비스에서는 서버가 측정 시작 때 잰 10초 값을 SeatClassifier.calibrate()에 넣고,
    calibrate()가 같은 기준으로 검사해 덜 눌린 센서가 있으면 측정 실패로 알린다.
    """
    out = {}
    for person, g in df.groupby("person", sort=False):
        b = g[(g["cls"] == BASELINE_CLS) & (g["depth"] == BASELINE_DEPTH)]
        if movement is not None:
            b = b[~movement.reindex(b.index, fill_value=False)]
        b = b[(b[channels] >= min_frac * ADC_MAX).all(axis=1)]
        if len(b) < n:
            raise ValueError(f"{person}: 바른자세(중)에서 모든 센서가 눌린 순간이 {len(b)}개뿐 (10초 = {n}개 필요)")
        out[person] = baseline_from_samples(b[channels].head(n).values, channels, min_frac=min_frac)
    return out


def baseline_from_samples(samples, channels: list[str], min_frac: float | None = BASELINE_MIN_FRAC,
                          floor: float = 1.0) -> pd.Series:
    """10초 동안의 값들(줄 × 채널) → 채널별 중앙값

    min_frac: 중앙값이 최대치의 이 비율보다 낮은 센서가 있으면 ValueError (덜 앉은 채로 측정 = 측정 실패).
              None이면 검사하지 않음 (서버에 저장된 기준을 다시 넣을 때 등)
    """
    arr = np.asarray(samples, dtype=float)
    if arr.ndim != 2 or arr.shape[1] != len(channels) or len(arr) == 0:
        raise ValueError(f"기준 측정 값 모양이 (n, {len(channels)})이어야 함: {arr.shape}")
    if not np.isfinite(arr).all():
        raise ValueError("기준 측정 값에 빈 값(NaN)이 있음")
    med = np.median(arr, axis=0)
    if min_frac is not None:
        low = {c: round(float(v)) for c, v in zip(channels, med) if v < min_frac * ADC_MAX}
        if low:
            raise ValueError(f"기준 측정 실패: 덜 눌린 센서 {low} (최대치의 {min_frac:.0%} 미만) — "
                             f"바르게 앉았는지 확인 후 다시 측정")
    return pd.Series(np.maximum(med, floor), index=channels)


def rolling_frame(df: pd.DataFrame, channels: list[str], size: int = 4) -> pd.DataFrame:
    """실시간과 같은 '최근 size개' 중앙값·표준편차를 사람 타임라인 순서대로 (줄마다 하나)

    실시간 판정기는 0.5초마다 최근 2초(4개)를 본다. 재생 테스트에서 이 결과가 실시간과 같아야 한다.
    """
    parts = []
    for _, g in df.groupby("person", sort=False):
        x = g[channels].astype(float)
        med = x.rolling(size, min_periods=1).median()
        sd = x.rolling(size, min_periods=1).std().fillna(0.0)
        # 실패·벽반사 값은 직전 유효값으로 채운 뒤 최근 size개 중앙값 (실시간 판정기와 같은 방식)
        dist = g["distance_cm"].where(distance_valid(g["distance_cm"])).ffill()
        dist = dist.rolling(size, min_periods=1).median()
        part = pd.concat([med, sd.add_suffix("_sd")], axis=1)
        part["distance_cm"] = dist
        parts.append(part)
    return pd.concat(parts).loc[df.index]


def reference_frame(df: pd.DataFrame, med: pd.DataFrame, channels: list[str],
                    baselines: dict[str, pd.Series], mode: str = "fixed") -> pd.DataFrame:
    """줄마다 기준값. fixed = 시작 기준 그대로 / raise = 지금까지 본 '최근 2초 중앙값'의 최댓값까지 올림"""
    if mode not in REF_MODES:
        raise ValueError(f"mode는 {REF_MODES} 중 하나 (지금: {mode})")
    parts = []
    for person, g in df.groupby("person", sort=False):
        base = baselines[person][channels]
        ref = pd.DataFrame(np.tile(base.values, (len(g), 1)), index=g.index, columns=channels)
        if mode == "raise":
            ref = np.maximum(ref, med.loc[g.index, channels].cummax())
        parts.append(ref)
    return pd.concat(parts).loc[df.index]


def deviation_features(med: pd.DataFrame, ref: pd.DataFrame, channels: list[str],
                       sd: pd.DataFrame | None = None, distance: pd.Series | None = None,
                       unloaded_level: float = -0.3) -> pd.DataFrame:
    """기준 대비 편차 특징. 학습·재생·실시간 모두 이 함수 하나로 계산한다.

    편차 d = (값 - 기준) / 기준   → 0 = 기준과 같음, -1 = 완전히 들림, + = 기준보다 더 눌림 (최대 +1)
    특징
        d_{채널}            채널별 편차
        LR_{front|mid|back} 같은 줄의 왼쪽 편차 - 오른쪽 편차 (음수 = 왼쪽이 더 빠짐)
        LR_all              왼쪽 평균 - 오른쪽 평균
        diag                (앞왼 + 뒤오) - (앞오 + 뒤왼)  ← 다리 꼬기 대각선
        n_unloaded          편차가 unloaded_level보다 낮은(많이 들린) 센서 수
        sd_mean             최근 2초 흔들림 (기준 대비 비율 평균)
        distance_cm         (넣으면) 화면까지 거리
    """
    r = ref[channels].to_numpy(dtype=float)
    d = (med[channels].to_numpy(dtype=float) - r) / np.maximum(r, 1.0)
    d = pd.DataFrame(np.clip(d, -1.0, 1.0), index=med.index, columns=channels)
    F = d.add_prefix("d_")
    left = [c for c in channels if c.startswith("L_")]
    right = [c for c in channels if c.startswith("R_")]
    for row in ("front", "mid", "back"):
        if f"L_{row}" in channels and f"R_{row}" in channels:
            F[f"LR_{row}"] = d[f"L_{row}"] - d[f"R_{row}"]
    if left and right:
        F["LR_all"] = d[left].mean(axis=1) - d[right].mean(axis=1)
    if {"L_front", "R_back", "R_front", "L_back"} <= set(channels):
        F["diag"] = (d["L_front"] + d["R_back"]) - (d["R_front"] + d["L_back"])
    F["n_unloaded"] = (d < unloaded_level).sum(axis=1).astype(float)
    if sd is not None:
        F["sd_mean"] = (sd[[f"{c}_sd" for c in channels]].to_numpy() / np.maximum(r, 1.0)).mean(axis=1)
    if distance is not None:
        F["distance_cm"] = distance.to_numpy(dtype=float)
    return F
