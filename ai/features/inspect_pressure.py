"""압력·거리 수집 데이터의 신호 상태 확인 — 그래프 + 요약표 (발표 '아날로그 신호 문제' 자료)

실행 (저장소 최상위, .venv 켠 상태):
    python -m ai.features.inspect_pressure                     # data/raw/pressure 전체
    python -m ai.features.inspect_pressure --jump 600          # 움직임 기준 바꿔 보기
    python -m ai.features.inspect_pressure --folder 다른폴더

저장 (data/processed/pressure_inspect/, git에 올라가지 않음)
    summary_blocks.csv        사람·자세 구간별: 움직임 %, 튐 횟수, 접촉 깜빡임 횟수, 포화 %, 0 근처 %, 거리 결측·벽반사 %, 거리 중앙값
    summary_people.txt        사람별·전체 요약 (화면에도 출력)
    timeline_{사람}.png        자세 13개 × 채널 값 + 움직임 구간(회색) + 접촉 깜빡임(빨간 점) + 거리
    value_histogram.png       전체 압력값 분포 (포화·0 근처 무더기 확인)
    channel_by_posture.png    자세별 채널 중앙값 (사람마다 패턴이 다른지)
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from common.config import repo_path
from ai.features.pressure_pipeline import (
    ADC_MAX, MovementParams, detect_flicker, detect_movement, distance_valid, flicker_by_channel,
    load_folder, show_path, signal_report,
)

log = logging.getLogger("sitsense.inspect")

CH_ORDER = ["L_front", "R_front", "L_mid", "R_mid", "C_front", "C_mid", "L_back", "C_back", "R_back"]
CH_KO = {"L_front": "앞 왼쪽", "R_front": "앞 오른쪽", "L_mid": "중간 왼쪽", "R_mid": "중간 오른쪽",
         "L_back": "뒤 왼쪽", "R_back": "뒤 오른쪽", "C_back": "뒤 가운데", "C_mid": "중간 가운데",
         "C_front": "앞 가운데"}
POSTURE_ORDER = ["바른자세", "왼쪽편향", "오른쪽편향", "왼다리꼬기", "오른다리꼬기"]


def setup_font(plt) -> None:
    """한글 폰트 (맥: AppleGothic, 윈도우: Malgun Gothic, 리눅스: Noto Sans CJK)"""
    from matplotlib import font_manager
    have = {f.name for f in font_manager.fontManager.ttflist}
    for name in ("AppleGothic", "Apple SD Gothic Neo", "Malgun Gothic", "Noto Sans CJK KR", "Noto Sans CJK JP",
                 "NanumGothic", "Noto Sans KR"):
        if name in have:
            plt.rcParams["font.family"] = name
            break
    else:
        log.warning("한글 폰트를 찾지 못해 그래프 글자가 깨질 수 있음")
    plt.rcParams["axes.unicode_minus"] = False


def plot_timelines(df, channels, movement, flicker, out: Path, plt) -> list[Path]:
    paths = []
    chs = [c for c in CH_ORDER if c in channels]
    colors = plt.cm.tab10(np.linspace(0, 1, 10))
    for person, g in df.groupby("person", sort=False):
        blocks = list(g.groupby("block", sort=True))
        n = len(blocks)
        ncol = 3
        nrow = int(np.ceil(n / ncol))
        fig, axes = plt.subplots(nrow, ncol, figsize=(16, 2.6 * nrow), sharey=True)
        axes = np.atleast_1d(axes).ravel()
        for ax, (block, b) in zip(axes, blocks):
            t = b["elapsed_s"].values
            mv = movement.loc[b.index].values
            # 움직임 구간 회색 칠하기
            for s, e in _runs(mv):
                ax.axvspan(t[s], t[e] + 0.5, color="0.85", lw=0)
            for i, c in enumerate(chs):
                ax.plot(t, b[c].values, lw=0.8, color=colors[i], label=CH_KO.get(c, c))
            fl = flicker.loc[b.index].values
            ax.plot(t[fl], np.full(fl.sum(), ADC_MAX), "o", color="red", ms=2.5)
            ax.axhline(3700, color="red", lw=0.6, ls="--")
            ax2 = ax.twinx()
            dv = distance_valid(b["distance_cm"])
            ax2.plot(t, b["distance_cm"].where(dv), color="black", lw=0.7, alpha=0.6)
            bad = b["distance_cm"].isna() | ~dv
            ax2.plot(t[bad.values], np.full(bad.sum(), 5), "x", color="black", ms=3)
            ax2.set_ylim(0, 160)
            ax2.tick_params(labelsize=7)
            ax.set_title(f"{block}. {b['label'].iloc[0]}  (움직임 {mv.mean():.0%}, 깜빡임 {fl.sum()}회)",
                         fontsize=9)
            ax.set_ylim(-100, ADC_MAX + 100)
            ax.tick_params(labelsize=7)
        for ax in axes[n:]:
            ax.axis("off")
        handles, labels = axes[0].get_legend_handles_labels()
        fig.legend(handles + [plt.Line2D([], [], color="black", alpha=0.6),
                              plt.Rectangle((0, 0), 1, 1, color="0.85"),
                              plt.Line2D([], [], color="red", marker="o", ls="", ms=3),
                              plt.Line2D([], [], color="red", ls="--")],
                   labels + ["거리(cm, 오른쪽 축) · x=결측/벽반사", "움직임(자세 고쳐 앉음)",
                             "접촉 깜빡임(센서 하나만 0↔높은 값)", "포화 경계 3700"],
                   loc="upper center", ncol=6, fontsize=8, frameon=False)
        fig.suptitle(f"{person} — 자세별 압력·거리 (x축: 경과 초)", y=1.0 - 0.15 / nrow, fontsize=12)
        fig.tight_layout(rect=(0, 0, 1, 1 - 0.35 / nrow))
        path = out / f"timeline_{person}.png"
        fig.savefig(path, dpi=130)
        plt.close(fig)
        paths.append(path)
    return paths


def _runs(mask):
    """True가 이어지는 [시작, 끝] 인덱스 목록"""
    out, start = [], None
    for i, m in enumerate(mask):
        if m and start is None:
            start = i
        elif not m and start is not None:
            out.append((start, i - 1))
            start = None
    if start is not None:
        out.append((start, len(mask) - 1))
    return out


def plot_histogram(df, channels, out: Path, plt) -> Path:
    v = df[channels].values.ravel()
    v = v[~np.isnan(v)]
    fig, ax = plt.subplots(figsize=(8, 3.6))
    ax.hist(v, bins=np.arange(0, ADC_MAX + 129, 128), color="#4C72B0")
    ax.axvline(3700, color="red", ls="--", lw=1)
    sat = (v >= 3700).mean()
    zero = (v <= 10).mean()
    ax.set_title(f"압력값 분포 — 3700 이상 {sat:.0%}, 0 근처 {zero:.0%}, 최댓값 {int(v.max())} (ADC 최대 {ADC_MAX})")
    ax.set_xlabel("ADC 값")
    ax.set_ylabel("샘플 수")
    fig.tight_layout()
    path = out / "value_histogram.png"
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path


def plot_channel_by_posture(df, channels, movement, out: Path, plt) -> Path:
    chs = [c for c in CH_ORDER if c in channels]
    people = list(df["person"].unique())
    fig, axes = plt.subplots(1, len(people), figsize=(5 * len(people), 3.6), sharey=True)
    axes = np.atleast_1d(axes)
    still = df[~movement]
    for ax, person in zip(axes, people):
        m = still[still["person"] == person].groupby("cls")[chs].median()
        m = m.reindex([p for p in POSTURE_ORDER if p in m.index])
        im = ax.imshow(m.values, vmin=0, vmax=ADC_MAX, cmap="viridis", aspect="auto")
        ax.set_xticks(range(len(chs)), [CH_KO.get(c, c) for c in chs], rotation=60, fontsize=8)
        ax.set_yticks(range(len(m)), m.index, fontsize=9)
        ax.set_title(person)
    fig.colorbar(im, ax=axes, shrink=0.8, label="ADC 중앙값")
    fig.suptitle("자세별 채널 중앙값 (움직임 구간 제외) — 사람마다 패턴이 다른지 비교")
    path = out / "channel_by_posture.png"
    fig.savefig(path, dpi=130, bbox_inches="tight")
    plt.close(fig)
    return path


def people_summary(df, channels, movement, report: pd.DataFrame, params: MovementParams) -> str:
    lines = []
    for person, g in [(p, df[df.person == p]) for p in df.person.unique()] + [("전체", df)]:
        x = g[channels].astype(float)
        mv = movement.loc[g.index]
        r = report if person == "전체" else report[report.person == person]
        first10 = (mv & (g["elapsed_s"] < 10)).sum() / max(mv.sum(), 1)
        lines.append(
            f"{person:>4}: {len(g):5d}줄 | 움직임 {mv.mean():5.1%} (그중 구간 첫 10초 {first10:4.0%}) | "
            f"0.5초에 {params.jump:g}↑ 튐 {int(r.jumps.sum()):4d}회 (접촉 깜빡임 {int(r.flicker.sum()):3d}) | 포화(3700↑) {(x >= 3700).mean().mean():5.1%} | "
            f"0 근처 {(x <= 10).mean().mean():5.1%} | 거리 결측 {g.distance_cm.isna().mean():4.1%} · "
            f"벽반사/범위밖 {(g.distance_cm.notna() & ~distance_valid(g.distance_cm)).mean():4.1%}")
    ch_zero = {c: f"{(df[c] <= 10).mean():.0%}" for c in channels}
    lines.append(f"채널별 0 근처 비율: {ch_zero}")
    lines.append(f"채널별 접촉 깜빡임 횟수 (접촉 불량 의심): {flicker_by_channel(df, channels, params)}")
    return "\n".join(lines)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="[inspect] %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="압력·거리 데이터 신호 상태 확인")
    ap.add_argument("--folder", default="data/raw/pressure")
    ap.add_argument("--out", default="data/processed/pressure_inspect")
    ap.add_argument("--jump", type=float, default=MovementParams.jump, help="움직임 기준 (0.5초 사이 변화량)")
    ap.add_argument("--no-plots", action="store_true")
    args = ap.parse_args()
    try:
        df, channels = load_folder(repo_path(args.folder))
    except (FileNotFoundError, ValueError) as e:
        log.error("%s", e)
        return 1
    out = repo_path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    params = MovementParams(jump=args.jump)
    movement = detect_movement(df, channels, params)
    flicker = detect_flicker(df, channels, params)
    report = signal_report(df, channels, movement, flicker, jump=args.jump)
    report.to_csv(out / "summary_blocks.csv", index=False, encoding="utf-8-sig")
    text = people_summary(df, channels, movement, report, params)
    (out / "summary_people.txt").write_text(text + "\n", encoding="utf-8")
    print(f"사람 {df.person.nunique()}명, 채널 {len(channels)}개 {channels}")
    print(text)

    if not args.no_plots:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        setup_font(plt)
        paths = plot_timelines(df, channels, movement, flicker, out, plt)
        paths += [plot_histogram(df, channels, out, plt),
                  plot_channel_by_posture(df, channels, movement, out, plt)]
        for p in paths:
            print("그림:", show_path(p))
    print("표:", show_path(out / "summary_blocks.csv"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
