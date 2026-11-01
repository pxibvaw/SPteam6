"""재생 테스트 — 수집 CSV를 0.5초씩 실시간 판정기(ai/engine/seat.py)에 흘려 넣어, 후보 모델들이 앱에서
어떻게 보였을지 비교한다. 최종 모델을 고르기 전에 돌리는 비교용이다 (모델을 저장하지 않음).

무엇을 하나
    0. train_pressure가 저장한 selection.json에서 특징 구성(기준 방식·흔들림·거리·증강)을 읽는다
    1. 사람마다: 그 사람을 뺀 나머지로 후보 모델(규칙/RF/SVM/KNN)을 학습 → 처음 보는 사람 조건
    2. 그 사람의 바른자세(중) 처음 10초로 calibrate() → 전체 기록을 시간 순서대로 update()
       (실시간 코드를 그대로 쓴다. 학습 모델은 일괄 계산 확률과 같은지도 확인해서 출력)
    3. 후처리 기준을 바꿔 가며 비교 (상태 변경 시간 hold_sec × 확신도 min_conf × 확률 평균 avg_sec)
       - 정확도         모든 순간에 앱 상태 == 실제 자세 (지연·판정 전 unknown도 틀린 것으로)
       - 보인 것 정확도  unknown(처음 판정 전)이 아닐 때만
       - 판정 비율      unknown이 아닌 비율
       - 분당 바뀜      앱 상태가 1분에 몇 번 바뀌는지 (실제 자세 변화 횟수와 비교)
       - 반응 지연      자세 구간이 시작된 뒤 앱에 맞게 뜨기까지 걸린 초 (끝까지 못 맞추면 그 구간 길이)
       - 못 맞춘 구간   구간 끝까지 한 번도 맞게 안 뜬 구간 수 (사람 평균)

실행 (저장소 최상위, .venv 켠 상태, train_pressure를 먼저 돌려 selection.json이 있어야 함):
    python -m ai.classify.replay_seat                        # 규칙·RF·SVM·KNN 전부
    python -m ai.classify.replay_seat --models rule rf       # 일부만

저장 (data/processed/replay_seat/, git에 올라가지 않음)
    sweep.csv        모델 × 조합별 결과 (사람 평균)
    per_person.csv   모델 × 조합별 사람별 결과
주의: 수집 데이터는 자세 구간 사이(10초)를 찍지 않아서 '바뀌는 순간'이 실제보다 갑작스럽고,
      빈 의자 데이터가 없어서 자리 비움은 평가되지 않는다.
"""
from __future__ import annotations

import argparse
import itertools
import json
import logging
import sys
import time

import numpy as np
import pandas as pd

from common.config import repo_path
from ai.classify.train_pressure import MODEL_KO, SELECTION_FILE, Dataset, make_bundle
from ai.engine.seat import PostProcessor, SeatClassifier, SeatParams
from ai.features.pressure_pipeline import (
    ADC_MAX, BASELINE_CLS, BASELINE_DEPTH, BASELINE_MIN_FRAC, BASELINE_SAMPLES, MovementParams, load_folder,
    show_path,
)

log = logging.getLogger("sitsense.replay")

HOLDS = (0.0, 2.0, 4.0, 6.0)
CONFS = (0.0, 0.4, 0.5, 0.6, 0.7)
AVGS = (1.0, 2.0)


def stream_person(seat: SeatClassifier, g: pd.DataFrame, movement: pd.Series) -> dict:
    """한 사람 기록을 실시간 판정기에 흘려 넣고 순간마다 확률·움직임·빈 의자를 기록
    기준 10초는 학습과 같은 규칙: 바른자세(중)에서 움직임 없고 모든 센서가 눌린 첫 10초"""
    b = g[(g["cls"] == BASELINE_CLS) & (g["depth"] == BASELINE_DEPTH) & ~movement.loc[g.index]]
    b = b[(b[seat.channels] >= BASELINE_MIN_FRAC * ADC_MAX).all(axis=1)]
    seat.calibrate(b[seat.channels].head(BASELINE_SAMPLES).values)
    ts = g["ts"].astype("int64").to_numpy() / 1e9
    probs, moving, empty = [], [], []
    for t, row, d in zip(ts, g[seat.channels].to_numpy(dtype=float), g["distance_cm"].to_numpy(dtype=float)):
        r = seat.update(t, row, None if np.isnan(d) else d)
        probs.append(None if not r.probs else np.array([r.probs[l] for l in seat.labels]))
        moving.append(r.moving)
        empty.append(r.empty)
    return {"ts": ts, "probs": probs, "moving": np.array(moving), "empty": np.array(empty),
            "labels": seat.labels}


def evaluate(rec: dict, y: np.ndarray, block: np.ndarray, params: SeatParams) -> dict:
    post = PostProcessor(params, rec["labels"])
    states = np.array([post.step(t, p, m, e)[0] for t, p, m, e in
                       zip(rec["ts"], rec["probs"], rec["moving"], rec["empty"])], dtype=object)
    shown = states != "unknown"
    changes = int((states[1:] != states[:-1]).sum())
    minutes = len(states) * 0.5 / 60
    lat, never = [], 0
    for bl in pd.unique(block):
        idx = np.where(block == bl)[0]
        hit = np.where(states[idx] == y[idx])[0]
        if len(hit):
            lat.append(hit[0] * 0.5)
        else:
            lat.append(len(idx) * 0.5)
            never += 1
    return {"acc": float((states == y).mean()),
            "acc_shown": float((states[shown] == y[shown]).mean()) if shown.any() else np.nan,
            "coverage": float(shown.mean()), "flips_per_min": changes / minutes,
            "latency_s": float(np.mean(lat)), "never_blocks": never}


def person_bundle(sel: dict, F: pd.DataFrame, ds: Dataset, person: str, model_name: str) -> dict:
    """그 사람을 뺀 데이터로 학습한 bundle (최종 모델 파일과 같은 함수로 만듦)"""
    tr = ds.train_clean & (ds.person != person)
    return make_bundle(model_name, F[tr], ds.y[tr], channels=sel["channels"], ref_mode=sel["ref_mode"],
                       use_extras=sel["use_extras"], use_aug=sel["augment"], movement=sel["movement"])


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="[replay] %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="실시간 판정기 재생 테스트 (후보 모델 비교)")
    ap.add_argument("--folder", default="data/raw/pressure")
    ap.add_argument("--selection", default=f"data/processed/train_pressure/{SELECTION_FILE}",
                    help="train_pressure가 저장한 특징 구성 파일")
    ap.add_argument("--models", nargs="+", choices=["rule", "rf", "svm", "knn"],
                    default=["rule", "rf", "svm", "knn"])
    ap.add_argument("--out", default="data/processed/replay_seat")
    args = ap.parse_args()
    sel_path = repo_path(args.selection)
    if not sel_path.exists():
        log.error("%s가 없음 — python -m ai.classify.train_pressure를 먼저 실행", sel_path)
        return 1
    sel = json.loads(sel_path.read_text(encoding="utf-8"))
    try:
        df, all_ch = load_folder(repo_path(args.folder))
    except (FileNotFoundError, ValueError) as e:
        log.error("%s", e)
        return 1
    channels = sel["channels"]
    missing = [c for c in channels if c not in all_ch]
    if missing:
        log.error("데이터에 채널이 없음: %s", missing)
        return 1
    if sorted(df["person"].unique()) != sorted(sel["people"]) or len(df) != sel["n_rows"]:
        log.warning("데이터가 selection.json을 만들 때와 다름 → train_pressure를 다시 돌리는 게 맞음")
    try:
        ds = Dataset(df, all_ch, channels, MovementParams(**sel["movement"]))
    except ValueError as e:
        log.error("%s", e)
        return 1
    kind = sel["ref_mode"] + ("+" if sel["use_extras"] else "")
    F = ds.features(kind)
    people = list(ds.person.unique())
    print(f"특징 구성 (selection.json): 기준 {sel['ref_mode']}, 흔들림·거리 {sel['use_extras']}, "
          f"증강 {sel['augment']} · 사람 {people} · 후보 {args.models}\n")

    records = {}
    for p in people:
        g = df[df["person"] == p]
        for m in args.models:
            t0 = time.time()
            bundle = person_bundle(sel, F, ds, p, m)
            seat = SeatClassifier(bundle)
            records[(m, p)] = rec = stream_person(seat, g, ds.movement)
            log.info("%s · %s: %d순간 재생 (%.0f초)", p, MODEL_KO[m], len(g), time.time() - t0)
            if m != "rule":                     # 실시간 계산 == 일괄 계산 확인
                model = bundle["model"]
                batch = model.predict_proba(F.loc[g.index])
                order = [list(model.classes_).index(l) for l in rec["labels"]]
                ok = [i for i, pr in enumerate(rec["probs"]) if pr is not None]
                diff = np.abs(np.vstack([rec["probs"][i] for i in ok]) - batch[ok][:, order]).max()
                flag = "같음" if diff <= 0.0006 else "⚠ 다름 — 실시간과 학습 계산이 어긋남"
                print(f"  {p} · {MODEL_KO[m]}: 실시간 vs 일괄 확률 최대 차이 {diff:.4f} ({flag})")

    rows, per_rows = [], []
    for m in args.models:
        for hold, conf, avg in itertools.product(HOLDS, CONFS, AVGS):
            prm = SeatParams(hold_sec=hold, min_conf=conf, avg_sec=avg)
            res = []
            for p in people:
                g = df[df["person"] == p]
                r = evaluate(records[(m, p)], g["seat_label"].to_numpy(), g["block"].to_numpy(), prm)
                res.append(r)
                per_rows.append({"model": m, "hold_sec": hold, "min_conf": conf, "avg_sec": avg, "person": p,
                                 **{k: round(v, 3) for k, v in r.items()}})
            mean = {k: float(np.nanmean([r[k] for r in res])) for k in res[0]}
            rows.append({"model": m, "hold_sec": hold, "min_conf": conf, "avg_sec": avg,
                         **{k: round(v, 3) for k, v in mean.items()}})
    sweep = pd.DataFrame(rows)
    out = repo_path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    sweep.to_csv(out / "sweep.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(per_rows).to_csv(out / "per_person.csv", index=False, encoding="utf-8-sig")
    ideal = np.mean([(df.loc[df.person == p, "block"].nunique() - 1) / (len(df[df.person == p]) * 0.5 / 60)
                     for p in people])

    with pd.option_context("display.width", 220, "display.max_columns", 20):
        print("\n상태 변경 시간별 비교 (확률 평균 2초) — 모델 × 확신도 기준")
        view = sweep[sweep.avg_sec == 2.0].drop(columns=["avg_sec", "acc_shown", "coverage"])
        for m in args.models:
            v = view[view.model == m].drop(columns="model")
            print(f"\n[{MODEL_KO[m]}]")
            print(v.to_string(index=False))
        print("\n모델별 정확도가 가장 높은 조합 (같으면 분당 바뀜이 적은 쪽)")
        for m in args.models:
            s = sweep[sweep.model == m].sort_values(["acc", "flips_per_min"], ascending=[False, True]).iloc[0]
            print(f"  {MODEL_KO[m]:16s} 상태 변경 {s.hold_sec:g}초 · 확신도 {s.min_conf:g} · 평균 {s.avg_sec:g}초 → "
                  f"정확도 {s.acc:.1%}, 분당 바뀜 {s.flips_per_min:.2f}, 반응 지연 {s.latency_s:.1f}초, "
                  f"못 맞춘 구간 {s.never_blocks:.1f}")
    print(f"\n참고: 이 데이터에서 자세가 실제로 바뀌는 횟수는 분당 약 {ideal:.2f}회 → 이보다 크면 깜빡임")
    print("※ 이 표로 모델과 기준값을 고른 뒤: python -m ai.classify.train_pressure --final <모델> "
          "--hold <초> --min-conf <값> --avg <초>")
    print(f"저장: {show_path(out)}/ (sweep.csv, per_person.csv)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
