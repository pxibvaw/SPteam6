"""센서 제거 실험 — 압력센서 하나씩 빼 보며 자세 구분에 얼마나 기여하는지 평가

질문: 뒤 가운데(C_back)를 포함한 7개가 의미 있나, 6개로 충분한가?
방법
    1. 움직임 구간을 빼고 2초씩 묶어 '하중 빠짐' 특징 계산 (ai/features/pressure_pipeline.py)
    2. 사람 단위 평가(LOSO): 한 명을 시험용으로 빼고 나머지로 Random Forest 학습 → 사람마다 반복
    3. 7개 전부 vs 채널 하나씩 뺀 경우의 정확도·macro F1 비교 (난수를 바꿔 여러 번 평균)
    4. 특징 중요도: 시험할 때 특징 하나를 섞으면 정확도가 얼마나 떨어지는지 (permutation importance)
    5. 채널별 신호 신뢰도: 0 근처 비율, 접촉 깜빡임 횟수

실행 (저장소 최상위, .venv 켠 상태):
    python -m ai.classify.channel_ablation                 # data/raw/pressure 전체
    python -m ai.classify.channel_ablation --seeds 5       # 더 여러 번 평균 (느려짐)
    python -m ai.classify.channel_ablation --drop C_back   # 특정 채널을 뺀 구성을 기준으로

저장 (data/processed/channel_ablation/, git에 올라가지 않음)
    ablation.csv          조건별 사람별 정확도, 평균, macro F1, 기준 대비 차이
    importance.csv        특징 중요도 (사람별 + 평균)
    reliability.csv       채널별 0 근처 비율, 접촉 깜빡임 횟수
    ablation.png          조건별 평균 정확도 막대그래프
    결과 요약은 화면에도 출력

주의: 사람이 적으면(3명) 결과가 흔들린다. 사람을 늘린 뒤 다시 돌려 확인할 것.
"""
from __future__ import annotations

import argparse
import logging
import sys

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.inspection import permutation_importance
from sklearn.metrics import accuracy_score, f1_score

from common.config import repo_path
from ai.features.pressure_pipeline import (
    MovementParams, detect_movement, flicker_by_channel, load_folder, make_windows,
    show_path, unloading_features,
)

log = logging.getLogger("sitsense.ablation")

CH_KO = {"L_front": "앞 왼쪽", "R_front": "앞 오른쪽", "L_mid": "중간 왼쪽", "R_mid": "중간 오른쪽",
         "L_back": "뒤 왼쪽", "R_back": "뒤 오른쪽", "C_back": "뒤 가운데", "C_mid": "중간 가운데",
         "C_front": "앞 가운데"}


def make_model(seed: int) -> RandomForestClassifier:
    return RandomForestClassifier(n_estimators=300, min_samples_leaf=3, class_weight="balanced",
                                  random_state=seed, n_jobs=-1)


def loso(win: pd.DataFrame, channels: list[str], seeds: list[int]) -> dict:
    """사람 단위 평가. 반환: 사람별 정확도(난수 평균), 평균 정확도, macro F1"""
    F = unloading_features(win, channels)
    y = win["cls"]
    people = list(win["person"].unique())
    acc = np.zeros((len(seeds), len(people)))
    f1 = np.zeros_like(acc)
    for i, seed in enumerate(seeds):
        for j, person in enumerate(people):
            tr, te = win["person"] != person, win["person"] == person
            pred = make_model(seed).fit(F[tr], y[tr]).predict(F[te])
            acc[i, j] = accuracy_score(y[te], pred)
            f1[i, j] = f1_score(y[te], pred, average="macro")
    per_person = acc.mean(axis=0)
    return {"per_person": dict(zip(people, per_person)), "acc": per_person.mean(),
            "f1": f1.mean()}


def feature_importance(win: pd.DataFrame, channels: list[str], seed: int = 0,
                       repeats: int = 10) -> pd.DataFrame:
    F = unloading_features(win, channels)
    y = win["cls"]
    cols = {}
    for person in win["person"].unique():
        tr, te = win["person"] != person, win["person"] == person
        model = make_model(seed).fit(F[tr], y[tr])
        r = permutation_importance(model, F[te], y[te], n_repeats=repeats, random_state=seed, n_jobs=-1)
        cols[person] = pd.Series(r.importances_mean, index=F.columns)
    imp = pd.DataFrame(cols)
    imp["mean"] = imp.mean(axis=1)
    return imp.sort_values("mean", ascending=False)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="[ablation] %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="압력센서 제거 실험")
    ap.add_argument("--folder", default="data/raw/pressure")
    ap.add_argument("--out", default="data/processed/channel_ablation")
    ap.add_argument("--seeds", type=int, default=3, help="난수를 바꿔 몇 번 평균낼지")
    ap.add_argument("--window", type=int, default=4, help="묶음 크기 (4 = 2초)")
    ap.add_argument("--jump", type=float, default=MovementParams.jump, help="움직임 기준")
    ap.add_argument("--drop", nargs="*", default=[], help="처음부터 뺄 채널 (예: C_back)")
    ap.add_argument("--no-plot", action="store_true")
    args = ap.parse_args()
    if args.seeds < 1:
        ap.error("--seeds는 1 이상")

    try:
        df, channels = load_folder(repo_path(args.folder))
    except (FileNotFoundError, ValueError) as e:
        log.error("%s", e)
        return 1
    bad = [c for c in args.drop if c not in channels]
    if bad:
        log.error("없는 채널 %s. 가능: %s", bad, channels)
        return 1
    params = MovementParams(jump=args.jump)
    base_channels = [c for c in channels if c not in args.drop]
    if df["person"].nunique() < 2:
        log.error("사람 단위 평가에는 2명 이상 필요 (지금 %d명)", df["person"].nunique())
        return 1

    movement = detect_movement(df, channels, params)
    win = make_windows(df, channels, size=args.window, exclude=movement)
    seeds = list(range(args.seeds))
    people = list(win["person"].unique())
    out = repo_path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    print(f"사람 {len(people)}명 {people}, 묶음 {len(win)}개 (움직임 {movement.mean():.1%} 제외), "
          f"난수 {len(seeds)}번 평균")
    print(f"기준 구성: {len(base_channels)}개 {base_channels}\n")

    # 1) 기준 vs 하나씩 빼기
    rows = []
    base = loso(win, base_channels, seeds)
    rows.append({"condition": f"{len(base_channels)}개 전부", "removed": "", **base})
    for c in base_channels:
        log.info("%s 빼고 평가 중…", c)
        r = loso(win, [x for x in base_channels if x != c], seeds)
        rows.append({"condition": f"{CH_KO.get(c, c)} 빼기", "removed": c, **r})
    table = pd.DataFrame([{"condition": r["condition"], "removed": r["removed"],
                           **{f"acc_{p}": round(r["per_person"][p], 3) for p in people},
                           "acc_mean": round(r["acc"], 3), "macro_f1": round(r["f1"], 3),
                           "diff_vs_base_pp": round((r["acc"] - base["acc"]) * 100, 1)} for r in rows])
    table.to_csv(out / "ablation.csv", index=False, encoding="utf-8-sig")
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        print(table.drop(columns="removed").to_string(index=False))

    # 2) 특징 중요도
    log.info("특징 중요도 계산 중…")
    imp = feature_importance(win, base_channels)
    imp.round(4).to_csv(out / "importance.csv", encoding="utf-8-sig")
    print("\n특징 중요도 (섞었을 때 정확도 하락, 사람 평균):")
    print(imp["mean"].round(3).to_string())

    # 3) 채널 신뢰도 (움직임 포함 원본 기준)
    flick = flicker_by_channel(df, channels, params)
    rel = pd.DataFrame({"channel": channels,
                        "name": [CH_KO.get(c, c) for c in channels],
                        "zero_pct": [round((df[c] <= params.zero_level).mean() * 100, 1) for c in channels],
                        "flicker": [flick[c] for c in channels]})
    for p in df["person"].unique():
        rel[f"zero_pct_{p}"] = [round((df.loc[df.person == p, c] <= params.zero_level).mean() * 100, 1)
                                for c in channels]
    rel.to_csv(out / "reliability.csv", index=False, encoding="utf-8-sig")
    print("\n채널 신뢰도 (0 근처 % = 접촉 끊김, flicker = 접촉 깜빡임 횟수):")
    print(rel.to_string(index=False))

    # 4) 요약
    worst = table.iloc[1:].sort_values("diff_vs_base_pp", ascending=False).iloc[0]
    print(f"\n빼도 손해가 가장 적은 채널: {worst['condition']} ({worst['diff_vs_base_pp']:+.1f}%p)")
    print("※ 사람이 적으면 ±2%p 정도는 오차 범위. 사람을 늘린 뒤 다시 확인할 것.")

    if not args.no_plot:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from ai.features.inspect_pressure import setup_font
        setup_font(plt)
        fig, ax = plt.subplots(figsize=(8, 3.8))
        colors = ["#4C72B0"] + ["#DD8452" if d < -2 else "#55A868" for d in table["diff_vs_base_pp"][1:]]
        ax.bar(table["condition"], table["acc_mean"] * 100, color=colors)
        ax.axhline(base["acc"] * 100, color="0.3", ls="--", lw=0.8)
        for i, v in enumerate(table["acc_mean"] * 100):
            ax.text(i, v + 1, f"{v:.1f}", ha="center", fontsize=8)
        ax.set_ylabel("사람 단위 평균 정확도 (%)")
        ax.set_ylim(0, 105)
        ax.set_title("압력센서 하나씩 뺐을 때 자세 분류 정확도 (주황: 2%p 넘게 하락)")
        plt.setp(ax.get_xticklabels(), rotation=30, ha="right", fontsize=8)
        fig.tight_layout()
        fig.savefig(out / "ablation.png", dpi=130)
        plt.close(fig)
    print(f"\n저장: {show_path(out)}/ (ablation.csv, importance.csv, reliability.csv"
          f"{'' if args.no_plot else ', ablation.png'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
