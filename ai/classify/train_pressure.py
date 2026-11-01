"""압력(+거리) 좌면 자세 학습·비교 — 모델은 비교만 하고, 저장은 사람이 --final로 고를 때만

5가지 자세: normal(바른자세) / lean_left / lean_right(체중 편향) / cross_left / cross_right(다리 꼬기)
기본 6채널 (뒤 가운데 C_back 제외 — channel_ablation 결과)

무엇을 비교하나 (모두 사람 단위 평가 LOSO: 한 명을 빼고 나머지로 학습 → 뺀 사람으로 시험, 사람마다 반복)
    ① 단계별 추가 실험 (Random Forest)
        S1 원본 값                     최근 2초 중앙값 그대로, 움직임 구간도 학습에 포함
        S2 + 움직임 구간 제외          자세를 고쳐 앉는 순간은 학습에서 뺌
        S3 + 기준 대비 편차 (고정)     측정 시작 10초 바른 자세를 기준으로 편차 비율
        S4 + 기준 대비 편차 (올림)     기준보다 더 눌린 값이 나오면 기준을 올림
        S5 + 흔들림·거리              S3/S4 중 고른 기준 + 최근 2초 흔들림 + 화면 거리
        S6 + 좌우 대칭 증강           왼쪽·오른쪽 센서를 맞바꾸고 라벨도 바꾼(왼다리꼬기↔오른다리꼬기) 학습 데이터를
                                      추가해 2배로 → 사람 몸이 좌우 대칭이라는 지식을 모델에 넣음
        무엇을 켤지: 기본(auto)은 데이터로 결정 (기준 방식은 더 나은 쪽, 흔들림·거리와 증강은 0.5%p 이상 나아질 때만)
                     --ref / --extras / --aug로 직접 지정 가능
    ② 모델 비교 (같은 특징 구성): 규칙(threshold, 학습 데이터로 기준값 탐색) / RF / SVM / KNN
    ③ 모델마다 자세별 정답률 · confusion matrix
    시험은 항상 '움직임 없는 묶음'으로 한다 (라벨이 확실한 순간). 같은 사람 데이터는 학습에 절대 안 섞인다.

최종 모델: 자동으로 정하지 않는다. 비교와 재생 테스트(replay_seat)를 보고 --final로 직접 고르면 저장
    ai/models/final_seat_model.joblib   (이름이 final_로 시작해서 git에 올라감 → Pi에서 그대로 사용)
    채널 순서·특징 목록·기준 방식·규칙 기준값·후처리 기준값(--hold 등)이 같이 들어 있어
    ai/engine/seat.py가 그대로 불러 쓴다.

실행 (저장소 최상위, .venv 켠 상태):
    python -m ai.classify.train_pressure                      # ① 비교만 (selection.json 저장)
    python -m ai.classify.replay_seat                         # ② 후보 모델을 실시간 조건으로 비교
    python -m ai.classify.train_pressure --final rf --hold 4 --min-conf 0.5 --avg 2   # ③ 직접 골라 저장
    python -m ai.classify.train_pressure --drop               # 7채널 전부 사용 (아무것도 안 뺌)
    python -m ai.classify.train_pressure --aug off            # 증강 끄고 비교

저장 (data/processed/train_pressure/, git에 올라가지 않음)
    stages.csv, models.csv                ①·② 결과 (사람별 정확도, 평균, macro F1)
    confusion_{rule,rf,svm,knn}.csv/png   ③
    selection.json                        특징 구성·결정 이유·모델별 점수 (replay_seat가 읽음)
    importance.csv                        (--final rf일 때) 특징 중요도
주의: 사람 3명이면 결과가 흔들린다. 사람을 늘린 뒤 다시 돌려 확인할 것.
"""
from __future__ import annotations

import argparse
import itertools
import json
import logging
import sys
from datetime import datetime

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, confusion_matrix, f1_score
from sklearn.neighbors import KNeighborsClassifier
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC

from common.config import repo_path
from ai.classify.rules import ThresholdRule  # noqa: F401  (다른 파일에서 train_pressure.ThresholdRule로도 씀)
from ai.features.pressure_pipeline import (
    DEFAULT_DROP, SEAT_LABEL_MAP, MovementParams, detect_movement, deviation_features,
    load_folder, reference_frame, rolling_frame, select_channels, session_baselines, show_path,
)

log = logging.getLogger("sitsense.train")

LABELS = list(SEAT_LABEL_MAP.values())          # normal, lean_left, lean_right, cross_left, cross_right
LABEL_KO = {v: k for k, v in SEAT_LABEL_MAP.items()}
WINDOW = 4                                       # 최근 2초 (0.5초 × 4)
DEFAULT_DISTANCE_CM = 60.0                       # 아직 유효한 거리 값이 없을 때 (실시간도 같은 값)
MODEL_PATH = "ai/models/final_seat_model.joblib"
MODEL_VERSION = 1


# ---------------------------------------------------------------------------
# 데이터 → 특징
# ---------------------------------------------------------------------------
class Dataset:
    """줄마다 실시간과 같은 방식(최근 2초)으로 특징을 계산하고, 학습·시험에 쓸 줄을 고른다"""

    def __init__(self, df: pd.DataFrame, all_channels: list[str], channels: list[str],
                 params: MovementParams):
        self.df, self.channels = df, channels
        self.movement = detect_movement(df, channels, params)       # 실시간과 같이 쓰는 채널로만 판단
        self.roll = rolling_frame(df, channels, WINDOW)
        self.roll["distance_cm"] = self.roll["distance_cm"].fillna(DEFAULT_DISTANCE_CM)
        self.baselines = session_baselines(df, channels, self.movement)
        self.y = df["seat_label"]
        self.person = df["person"]
        # 최근 2초가 한 자세 구간 안에 다 들어 있는 줄 (구간이 바뀌는 순간 제외)
        pos = df.groupby(["person", "block"]).cumcount()
        full = pos >= WINDOW - 1
        clean_win = ~self.movement.astype(float).groupby(
            [df["person"], df["block"]]).transform(lambda s: s.rolling(WINDOW, min_periods=1).max()).astype(bool)
        step = (pos - (WINDOW - 1)) % WINDOW == 0       # 2초 간격으로 겹치지 않게
        self.train_all = full & step                    # 움직임 포함
        self.train_clean = full & step & clean_win      # 움직임 제외
        self.test_rows = self.train_clean               # 시험은 항상 라벨이 확실한 묶음
        self._cache: dict = {}

    def features(self, kind: str) -> pd.DataFrame:
        """kind: raw / fixed / raise / fixed+ / raise+ (+ = 흔들림·거리 추가)"""
        if kind in self._cache:
            return self._cache[kind]
        if kind == "raw":
            F = self.roll[self.channels].copy()
        else:
            mode = kind.rstrip("+")
            ref = reference_frame(self.df, self.roll, self.channels, self.baselines, mode)
            extra = kind.endswith("+")
            F = deviation_features(self.roll, ref, self.channels,
                                   sd=self.roll if extra else None,
                                   distance=self.roll["distance_cm"] if extra else None)
        self._cache[kind] = F
        return F


SWAP_LABEL = {"normal": "normal", "lean_left": "lean_right", "lean_right": "lean_left",
              "cross_left": "cross_right", "cross_right": "cross_left"}


def mirror(F: pd.DataFrame, y: pd.Series) -> tuple[pd.DataFrame, pd.Series]:
    """좌우 반전: 왼쪽·오른쪽 채널 특징을 맞바꾸고, 좌우차(LR_*, diag)는 부호를 바꾸고, 라벨도 좌우를 바꾼다"""
    M = F.copy()
    for c in F.columns:
        if c.startswith("d_L_") and f"d_R_{c[4:]}" in F.columns:
            M[c] = F[f"d_R_{c[4:]}"]
        elif c.startswith("d_R_") and f"d_L_{c[4:]}" in F.columns:
            M[c] = F[f"d_L_{c[4:]}"]
        elif c.startswith(("LR_", "diag")):
            M[c] = -F[c]
        elif c.startswith("L_") and f"R_{c[2:]}" in F.columns:      # 원본 값 특징
            M[c] = F[f"R_{c[2:]}"]
        elif c.startswith("R_") and f"L_{c[2:]}" in F.columns:
            M[c] = F[f"L_{c[2:]}"]
    return M, y.map(SWAP_LABEL)


def augment(F: pd.DataFrame, y: pd.Series) -> tuple[pd.DataFrame, pd.Series]:
    M, my = mirror(F, y)
    return pd.concat([F, M], ignore_index=True), pd.concat([y, my], ignore_index=True)


# ---------------------------------------------------------------------------
# 모델
# ---------------------------------------------------------------------------
def make_model(name: str, seed: int = 0):
    if name == "rf":
        return RandomForestClassifier(n_estimators=300, min_samples_leaf=3, class_weight="balanced",
                                      random_state=seed, n_jobs=-1)
    if name == "svm":
        return make_pipeline(StandardScaler(), SVC(C=3.0, gamma="scale", class_weight="balanced",
                                                   probability=True, random_state=seed))
    if name == "knn":
        return make_pipeline(StandardScaler(), KNeighborsClassifier(n_neighbors=15, weights="distance"))
    if name == "rule":
        return ThresholdRule()
    raise ValueError(name)


MODEL_KO = {"rule": "규칙(threshold)", "rf": "Random Forest", "svm": "SVM", "knn": "KNN"}


def loso(ds: Dataset, kind: str, model: str, seeds: list[int], clean_train: bool = True,
         collect: bool = False, aug: bool = False) -> dict:
    F = ds.features(kind)
    train_rows = ds.train_clean if clean_train else ds.train_all
    people = list(ds.person.unique())
    use_seeds = seeds if model == "rf" else [0]          # 난수 영향이 있는 건 RF뿐
    acc = np.zeros((len(use_seeds), len(people)))
    f1 = np.zeros_like(acc)
    y_true, y_pred = [], []
    for i, seed in enumerate(use_seeds):
        for j, p in enumerate(people):
            tr = train_rows & (ds.person != p)
            te = ds.test_rows & (ds.person == p)
            Xtr, ytr = (augment(F[tr], ds.y[tr]) if aug and model != "rule" else (F[tr], ds.y[tr]))
            m = make_model(model, seed).fit(Xtr, ytr)
            pred = m.predict(F[te])
            acc[i, j] = accuracy_score(ds.y[te], pred)
            f1[i, j] = f1_score(ds.y[te], pred, average="macro", labels=LABELS, zero_division=0)
            if collect and i == 0:
                y_true += list(ds.y[te])
                y_pred += list(pred)
    per = acc.mean(axis=0)
    out = {"per_person": dict(zip(people, per)), "acc": per.mean(), "f1": f1.mean()}
    if collect:
        out["y_true"], out["y_pred"] = y_true, y_pred
    return out


def as_row(name: str, r: dict, people: list[str], base: float | None = None) -> dict:
    row = {"condition": name, **{f"acc_{p}": round(r["per_person"][p], 3) for p in people},
           "acc_mean": round(r["acc"], 3), "macro_f1": round(r["f1"], 3)}
    if base is not None:
        row["diff_vs_prev_pp"] = round((r["acc"] - base) * 100, 1)
    return row


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------
SELECTION_FILE = "selection.json"


def decide(choice: str, gain: float, name: str, threshold: float = 0.005) -> tuple[bool, str]:
    """auto면 데이터로 결정 (threshold 이상 나아질 때만 켬), on/off면 사람이 정한 대로"""
    if choice == "on":
        return True, f"{name}: 켬 (직접 지정, 데이터상 {gain * 100:+.1f}%p)"
    if choice == "off":
        return False, f"{name}: 끔 (직접 지정, 데이터상 {gain * 100:+.1f}%p)"
    on = bool(gain >= threshold)                 # numpy bool → 파이썬 bool (JSON 저장용)
    return on, f"{name}: {'켬' if on else '끔'} (데이터: {gain * 100:+.1f}%p, {threshold * 100:.1f}%p 이상일 때 켬)"


def _json_default(o):
    """numpy 숫자·bool을 JSON에 저장할 수 있게"""
    if isinstance(o, np.generic):
        return o.item()
    raise TypeError(f"JSON으로 저장할 수 없음: {type(o).__name__}")


def plot_confusion(cm, title, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from ai.features.inspect_pressure import setup_font
    setup_font(plt)
    fig, ax = plt.subplots(figsize=(5.8, 4.8))
    norm = cm / np.maximum(cm.sum(axis=1, keepdims=True), 1)
    ax.imshow(norm, cmap="Blues", vmin=0, vmax=1)
    for i, j in itertools.product(range(len(LABELS)), repeat=2):
        ax.text(j, i, f"{norm[i, j]:.0%}\n({cm[i, j]})", ha="center", va="center", fontsize=8,
                color="white" if norm[i, j] > 0.6 else "black")
    ko = [LABEL_KO[x] for x in LABELS]
    ax.set_xticks(range(len(ko)), ko, rotation=30, ha="right")
    ax.set_yticks(range(len(ko)), ko)
    ax.set_xlabel("예측")
    ax.set_ylabel("정답")
    ax.set_title(title)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def make_bundle(model_name: str, X: pd.DataFrame, y: pd.Series, *, channels: list[str], ref_mode: str,
                use_extras: bool, use_aug: bool, movement: dict, post: dict | None = None,
                info: dict | None = None) -> dict:
    """모델 파일 내용 (seat.py가 불러 씀). 규칙 모델은 객체 대신 기준값만 저장한다 (어디서나 불러올 수 있게)"""
    import sklearn
    rule = ThresholdRule().fit(X, y)
    if rule.at_edge:
        logging.getLogger("sitsense.train").warning(
            "규칙 기준값 %s가 후보 범위 끝에 걸림 → rules.py의 T1_GRID/T2_GRID 범위를 넓혀 볼 것", rule.at_edge)
    if model_name == "rule":
        model, labels = None, LABELS
    else:
        Xtr, ytr = augment(X, y) if use_aug else (X, y)
        model = make_model(model_name, 0).fit(Xtr, ytr)
        labels = list(model.classes_)
    return {
        "version": MODEL_VERSION,
        "created": datetime.now().isoformat(timespec="seconds"),
        "model": model,
        "model_name": model_name,
        "labels": labels,
        "rule_thresholds": {"t1": float(rule.t1), "t2": float(rule.t2)},
        "augmented": bool(use_aug and model_name != "rule"),
        "feature_names": list(X.columns),
        "channels": list(channels),                 # 이 순서대로 값을 넣어야 함
        "ref_mode": ref_mode,                       # fixed / raise
        "use_sd": bool(use_extras),
        "use_distance": bool(use_extras),
        "window": WINDOW,
        "default_distance_cm": DEFAULT_DISTANCE_CM,
        "movement": dict(movement),
        "post": post,                               # 후처리 기준값 (None이면 seat.py 임시 기본값)
        "n_train": int(len(X)),
        "sklearn": sklearn.__version__,
        **(info or {}),
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="[train] %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="압력 좌면 자세 학습·비교 (모델 저장은 --final로 직접 선택할 때만)")
    ap.add_argument("--folder", default="data/raw/pressure")
    ap.add_argument("--out", default="data/processed/train_pressure")
    ap.add_argument("--drop", nargs="*", default=list(DEFAULT_DROP),
                    help="뺄 채널 (기본 C_back). '--drop'만 쓰면 아무것도 안 뺌")
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--jump", type=float, default=MovementParams.jump)
    ap.add_argument("--ref", choices=["auto", "fixed", "raise"], default="auto", help="기준 방식 (auto = 데이터로)")
    ap.add_argument("--extras", choices=["auto", "on", "off"], default="auto", help="흔들림·거리 특징")
    ap.add_argument("--aug", choices=["auto", "on", "off"], default="auto", help="좌우 대칭 증강")
    ap.add_argument("--final", choices=["rf", "svm", "knn", "rule"], default=None,
                    help="최종 모델을 직접 골라 저장 (안 주면 비교만)")
    ap.add_argument("--hold", type=float, default=None, help="(--final과 함께) 상태 변경 시간(초)")
    ap.add_argument("--min-conf", type=float, default=None, help="(--final과 함께) 상태 변경 확신도 기준")
    ap.add_argument("--avg", type=float, default=None, help="(--final과 함께) 확률 평균 시간(초)")
    ap.add_argument("--no-plot", action="store_true")
    args = ap.parse_args()
    post_args = {"hold_sec": args.hold, "min_conf": args.min_conf, "avg_sec": args.avg}
    if args.final is None and any(v is not None for v in post_args.values()):
        ap.error("--hold / --min-conf / --avg는 --final과 함께 쓸 때만 저장됩니다")
    if args.final and sum(v is not None for v in post_args.values()) not in (0, 3):
        ap.error("--hold, --min-conf, --avg는 셋 다 주거나 셋 다 빼세요")
    try:
        df, all_ch = load_folder(repo_path(args.folder))
    except (FileNotFoundError, ValueError) as e:
        log.error("%s", e)
        return 1
    bad = [c for c in args.drop if c not in all_ch]
    if bad:
        log.error("없는 채널 %s. 가능: %s", bad, all_ch)
        return 1
    if df["person"].nunique() < 2:
        log.error("사람 단위 평가에는 2명 이상 필요")
        return 1
    channels = select_channels(all_ch, args.drop)
    if not {"L_front", "R_front"} <= set(channels):
        log.error("규칙 방식·좌우차 특징에 앞쪽 좌우 센서(L_front, R_front)가 필요함")
        return 1
    params = MovementParams(jump=args.jump)
    seeds = list(range(max(1, args.seeds)))
    try:
        ds = Dataset(df, all_ch, channels, params)
    except ValueError as e:
        log.error("%s", e)
        return 1
    people = list(ds.person.unique())
    out = repo_path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    print(f"사람 {len(people)}명 {people} · 채널 {len(channels)}개 {channels}")
    print(f"학습 묶음: 움직임 포함 {int(ds.train_all.sum())}개 / 제외 {int(ds.train_clean.sum())}개 "
          f"(시험도 움직임 없는 묶음) · RF 난수 {len(seeds)}번 평균\n")

    # ① 단계별 (모든 단계를 다 계산하고, 무엇을 켤지는 데이터 또는 직접 지정으로)
    log.info("① 단계별 추가 실험…")
    res = {}
    res["S1 원본 값"] = loso(ds, "raw", "rf", seeds, clean_train=False)
    res["S2 + 움직임 구간 제외"] = loso(ds, "raw", "rf", seeds)
    res["S3 + 기준 대비 편차 (고정)"] = loso(ds, "fixed", "rf", seeds)
    res["S4 + 기준 대비 편차 (올림)"] = loso(ds, "raise", "rf", seeds)
    gain_raise = res["S4 + 기준 대비 편차 (올림)"]["acc"] - res["S3 + 기준 대비 편차 (고정)"]["acc"]
    if args.ref == "auto":
        ref_mode = "raise" if bool(gain_raise > 0) else "fixed"
        why_ref = f"기준 방식: {'올림' if ref_mode == 'raise' else '고정'} (데이터: 올림이 {gain_raise * 100:+.1f}%p)"
    else:
        ref_mode = args.ref
        why_ref = f"기준 방식: {'올림' if ref_mode == 'raise' else '고정'} (직접 지정, 데이터상 올림 {gain_raise * 100:+.1f}%p)"
    base_name = "S4 + 기준 대비 편차 (올림)" if ref_mode == "raise" else "S3 + 기준 대비 편차 (고정)"
    ko_ref = "올림" if ref_mode == "raise" else "고정"
    s5 = f"S5 + 흔들림·거리 ({ko_ref} 기준)"
    res[s5] = loso(ds, ref_mode + "+", "rf", seeds)
    use_extras, why_extras = decide(args.extras, res[s5]["acc"] - res[base_name]["acc"], "흔들림·거리")
    kind = ref_mode + ("+" if use_extras else "")
    no_aug_name = s5 if use_extras else base_name
    s6 = f"S6 + 좌우 대칭 증강 ({ko_ref} 기준{' + 흔들림·거리' if use_extras else ''})"
    res[s6] = loso(ds, kind, "rf", seeds, aug=True)
    use_aug, why_aug = decide(args.aug, res[s6]["acc"] - res[no_aug_name]["acc"], "좌우 대칭 증강")
    stage_rows, prev = [], None
    for name in res:
        stage_rows.append(as_row(name, res[name], people, prev))
        prev = res[name]["acc"]
    stages = pd.DataFrame(stage_rows)
    stages.to_csv(out / "stages.csv", index=False, encoding="utf-8-sig")
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        print("① 단계별 (Random Forest, 사람 단위 정확도, diff = 바로 윗줄 대비)")
        print(stages.to_string(index=False))
    print("\n특징 구성 결정")
    for w in (why_ref, why_extras, why_aug):
        print("  -", w)

    # ② 모델 비교 (같은 특징 구성으로 4가지 모두)
    log.info("② 모델 비교…")
    mres = {m: loso(ds, kind, m, seeds, collect=True, aug=use_aug) for m in ("rule", "rf", "svm", "knn")}
    models = pd.DataFrame([{"model": m, **as_row(MODEL_KO[m], r, people)} for m, r in mres.items()])
    models.to_csv(out / "models.csv", index=False, encoding="utf-8-sig")
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        print(f"\n② 모델 비교 (같은 특징{' + 증강' if use_aug else ''}, 사람 단위. 규칙은 증강과 무관)")
        print(models.drop(columns="model").to_string(index=False))

    # ③ confusion matrix (모델마다)
    print("\n③ 자세별 정답률 (사람 단위 시험 합계)")
    for m, r in mres.items():
        cm = confusion_matrix(r["y_true"], r["y_pred"], labels=LABELS)
        pd.DataFrame(cm, index=[LABEL_KO[x] for x in LABELS], columns=[LABEL_KO[x] for x in LABELS]) \
            .to_csv(out / f"confusion_{m}.csv", encoding="utf-8-sig")
        recall = cm.diagonal() / np.maximum(cm.sum(axis=1), 1)
        print(f"  {MODEL_KO[m]:16s}", "  ".join(f"{LABEL_KO[l]} {v:.0%}" for l, v in zip(LABELS, recall)))
        if not args.no_plot:
            plot_confusion(cm, f"{MODEL_KO[m]} · 사람 단위 평가 (평균 {r['acc']:.1%})", out / f"confusion_{m}.png")

    ranking = sorted(mres, key=lambda m: mres[m]["acc"], reverse=True)
    selection = {
        "created": datetime.now().isoformat(timespec="seconds"),
        "people": people, "n_rows": int(len(df)), "channels": channels,
        "ref_mode": ref_mode, "use_extras": use_extras, "augment": use_aug,
        "movement": vars(params), "window": WINDOW,
        "reasons": [why_ref, why_extras, why_aug],
        "models": {m: {"acc": round(r["acc"], 4), "macro_f1": round(r["f1"], 4),
                       "per_person": {k: round(v, 4) for k, v in r["per_person"].items()}}
                   for m, r in mres.items()},
        "ranking_by_acc": ranking,
    }
    (out / SELECTION_FILE).write_text(json.dumps(selection, ensure_ascii=False, indent=2, default=_json_default),
                                      encoding="utf-8")
    print("\n정확도 순위:", " > ".join(f"{MODEL_KO[m]} {mres[m]['acc']:.1%}" for m in ranking))
    print("※ 최종 모델은 자동으로 정하지 않습니다. 재생 테스트(python -m ai.classify.replay_seat)까지 보고 고르세요.")

    if args.final is None:
        print("\n다음: python -m ai.classify.replay_seat  → 결과를 보고")
        print("      python -m ai.classify.train_pressure --final <rf|svm|knn|rule> --hold <초> --min-conf <값> --avg <초>")
        print(f"결과: {show_path(out)}/ (stages.csv, models.csv, confusion_*.csv/png, {SELECTION_FILE})")
        return 0

    # 최종 모델 저장 (직접 고른 모델)
    import joblib
    best_model = args.final
    F = ds.features(kind)
    rows = ds.train_clean
    post = {k: float(v) for k, v in post_args.items()} if args.hold is not None else None
    r = mres[best_model]
    bundle = make_bundle(best_model, F[rows], ds.y[rows], channels=channels, ref_mode=ref_mode,
                         use_extras=use_extras, use_aug=use_aug, movement=vars(params), post=post,
                         info={"trained_on": people, "cv_acc": round(r["acc"], 4),
                               "cv_per_person": {k: round(v, 4) for k, v in r["per_person"].items()},
                               "selection_reasons": selection["reasons"]})
    if best_model == "rf":
        imp = pd.Series(bundle["model"].feature_importances_, index=F.columns).sort_values(ascending=False)
        imp.round(4).to_csv(out / "importance.csv", header=["importance"], encoding="utf-8-sig")
    path = repo_path(MODEL_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, path, compress=3)
    print(f"\n최종 모델 저장: {show_path(path)} — {MODEL_KO[best_model]} (직접 선택), "
          f"사람 단위 정확도 {r['acc']:.1%}, 사람 {len(people)}명 {int(rows.sum())}묶음으로 학습")
    if best_model == "rule":
        print(f"  규칙 기준값: t1 = {bundle['rule_thresholds']['t1']:.3f}, t2 = {bundle['rule_thresholds']['t2']:.3f}")
    if post:
        print(f"  후처리: 상태 변경 {post['hold_sec']:g}초, 확신도 {post['min_conf']:g}, 평균 {post['avg_sec']:g}초")
    else:
        print("  후처리 기준값을 안 줘서 seat.py 임시 기본값을 씀 (재생 테스트로 정한 뒤 --hold --min-conf --avg로 저장 권장)")
    print(f"결과: {show_path(out)}/")
    print("※ 사람이 적으면 결과가 흔들린다. 사람을 늘린 뒤 다시 돌려 확인할 것.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
