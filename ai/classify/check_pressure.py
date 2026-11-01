"""압력 파이프라인 자동 점검 — 학습·재생·실시간 코드가 의도대로 동작하는지 PASS/FAIL로 확인

실행 (저장소 최상위, .venv 켠 상태):
    python -m ai.classify.check_pressure          # 1~2분 (작은 모델을 몇 번 학습함)

점검 항목
    데이터    읽기·조인·라벨·채널 위치, 기준값(바른자세 중 10초)
    특징      편차 계산 공식, 좌우 반전(두 번 뒤집으면 원래대로, 라벨 교환), 좌우차 부호
    평가      사람 단위 평가에서 시험할 사람이 학습에 안 섞임, 증강이 시험 데이터에 안 들어감
    실시간    학습 계산 == 실시간 계산 (한 사람 전체를 흘려 확률 비교)
              빈 값·범위 밖 입력 거부 + 거부 뒤에도 기준값이 멀쩡함 (버그 1)
              규칙으로 저장된 모델 파일 동작 (버그 2)
              빈 의자: 순간 값(empty)이 바로 나오고 hold_sec 뒤 상태가 empty (버그 3)
              화면 거리: 40/50cm 경계 · 3초 유지 · 거리 실패 시 직전 값
              후처리: hold_sec 유지 · 확신도 잠깐 낮아도 유지 시간 이어짐 · 움직이는 중 판정 멈춤
    재생      재생 기록의 빈 의자 표시 == 실시간 판정기 (버그 3)
    학습 결정  auto는 0.5%p 이상일 때만 켬, on/off는 지정대로 (버그 4)
"""
from __future__ import annotations

import logging
import sys
import traceback
import warnings

import numpy as np
import pandas as pd

from common.config import repo_path

warnings.filterwarnings("ignore")
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str):
    """데코레이터: 정의하는 즉시 실행하고 결과를 RESULTS에 기록"""
    def deco(fn):
        print(f"… {name}", flush=True)
        try:
            detail = fn() or ""
            RESULTS.append((name, True, detail))
        except AssertionError as e:
            RESULTS.append((name, False, str(e)))
        except Exception as e:  # noqa: BLE001
            RESULTS.append((name, False, f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}"))
        return fn
    return deco


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    from ai.classify.train_pressure import (
        LABELS, Dataset, augment, decide, loso, make_bundle, make_model, mirror,
    )
    from ai.classify.replay_seat import evaluate, person_bundle, stream_person
    from ai.engine.seat import PostProcessor, SeatClassifier, SeatParams
    from ai.features.pressure_pipeline import (
        MovementParams, deviation_features, load_folder, select_channels,
    )

    folder = repo_path("data/raw/pressure")
    df, all_ch = load_folder(folder)
    ch = select_channels(all_ch)
    ds = Dataset(df, all_ch, ch, MovementParams())
    F = ds.features("raise")
    people = list(ds.person.unique())

    @check("데이터: 읽기·조인·라벨")
    def _():
        assert df["person"].nunique() >= 2, "사람이 2명 이상이어야 함"
        assert set(df["seat_label"].unique()) <= set(LABELS), f"모르는 라벨 {set(df['seat_label']) - set(LABELS)}"
        assert df["seat_label"].notna().all(), "라벨이 빈 줄이 있음"
        assert df["distance_cm"].notna().mean() > 0.5, "거리 조인이 거의 안 됨"
        assert all(c.split("_")[0] in "LRC" for c in all_ch), f"채널 이름 이상 {all_ch}"
        return f"{len(people)}명, {len(df)}줄, 채널 {all_ch}, 사용 {ch}"

    @check("데이터: 기준값 (바른자세 중, 모든 센서 눌린 10초)")
    def _():
        from ai.features.pressure_pipeline import ADC_MAX, BASELINE_MIN_FRAC
        for p, b in ds.baselines.items():
            assert b.notna().all(), f"{p} 기준값에 빈 값"
            assert (b >= BASELINE_MIN_FRAC * ADC_MAX).all(), f"{p} 덜 눌린 센서가 기준에 들어감 {b.round().to_dict()}"
        return ", ".join(f"{p}: {int(b.min())}~{int(b.max())}" for p, b in ds.baselines.items())

    @check("특징: 편차 공식 (값-기준)/기준, -1~+1")
    def _():
        med = pd.DataFrame([[3500, 0, 1750, 3500, 9999, 3500]], columns=ch)
        ref = pd.DataFrame([[3500] * 6], columns=ch)
        d = deviation_features(med, ref, ch)
        exp = {c: v for c, v in zip(ch, [0.0, -1.0, -0.5, 0.0, 1.0, 0.0])}
        for c, v in exp.items():
            assert abs(d[f"d_{c}"].iloc[0] - v) < 1e-9, f"d_{c} = {d[f'd_{c}'].iloc[0]} (기대 {v})"

    @check("특징: 좌우 반전 (두 번 = 원래, 라벨 교환, 좌우차 부호)")
    def _():
        X = F.head(300)
        y = ds.y.loc[X.index]
        M, my = mirror(X, y)
        MM, mmy = mirror(M, my)
        assert np.allclose(MM.values, X.values), "두 번 뒤집어도 원래로 안 돌아옴"
        assert (mmy.values == y.values).all(), "라벨이 원래로 안 돌아옴"
        assert np.allclose(M["LR_front"], -X["LR_front"]), "좌우차 부호가 안 바뀜"
        assert np.allclose(M["d_L_front"], X["d_R_front"]), "왼쪽·오른쪽이 안 바뀜"
        sw = dict(zip(y, my))
        for a, b in [("cross_left", "cross_right"), ("lean_left", "lean_right"), ("normal", "normal")]:
            if a in sw:
                assert sw[a] == b, f"{a} → {sw[a]} (기대 {b})"

    @check("평가: 시험할 사람이 학습에 안 섞임 + 증강은 학습 데이터만")
    def _():
        for p in people:
            tr = ds.train_clean & (ds.person != p)
            te = ds.test_rows & (ds.person == p)
            assert not (tr & te).any(), f"{p}: 학습·시험 줄이 겹침"
            assert p not in set(ds.person[tr]), f"{p}가 학습에 들어감"
            Xa, _ = augment(F[tr], ds.y[tr])
            assert len(Xa) == 2 * int(tr.sum()), "증강 크기 이상"
        assert not (ds.test_rows & ~ds.train_clean).any(), "시험 줄에 움직임 구간이 섞임"
        r = loso(ds, "raise", "rule", [0])
        assert 0 <= r["acc"] <= 1
        return f"규칙 사람 단위 정확도 {r['acc']:.1%}"

    sel = {"channels": ch, "ref_mode": "raise", "use_extras": False, "augment": True,
           "movement": vars(MovementParams()), "window": 4}
    p_test = people[-1]
    g = df[df["person"] == p_test]

    @check("실시간: 학습 계산 == 실시간 계산 (한 사람 전체)")
    def _():
        b = person_bundle(sel, F, ds, p_test, "rf")
        b["model"].set_params(n_estimators=60)
        b["model"].fit(*augment(F[ds.train_clean & (ds.person != p_test)],
                                ds.y[ds.train_clean & (ds.person != p_test)]))
        b["labels"] = list(b["model"].classes_)
        rec = stream_person(SeatClassifier(b), g, ds.movement)
        batch = b["model"].predict_proba(F.loc[g.index])
        order = [list(b["model"].classes_).index(l) for l in rec["labels"]]
        ok = [i for i, pr in enumerate(rec["probs"]) if pr is not None]
        diff = np.abs(np.vstack([rec["probs"][i] for i in ok]) - batch[ok][:, order]).max()
        assert diff <= 0.0006, f"확률 차이 {diff:.4f} (반올림 0.0005 넘음)"
        assert len(ok) > 0.8 * len(g), f"판정한 순간이 너무 적음 {len(ok)}/{len(g)}"
        return f"{p_test} {len(g)}순간, 최대 차이 {diff:.4f}"

    small = person_bundle(sel, F, ds, p_test, "rule")

    @check("실시간: 빈 값·범위 밖 입력 거부, 거부 뒤 기준값 멀쩡 (버그 1)")
    def _():
        s = SeatClassifier(small)
        s.calibrate([[3500] * len(ch)] * 20)
        bad_inputs = [[3500, np.nan] + [3500] * (len(ch) - 2), [3500, None] + [3500] * (len(ch) - 2),
                      [3500, 99999] + [3500] * (len(ch) - 2), [3500, -5] + [3500] * (len(ch) - 2),
                      ["a"] * len(ch), [3500] * (len(ch) - 1)]
        for v in bad_inputs:
            try:
                s.update(0, v, 60)
                raise AssertionError(f"거부 안 됨: {v}")
            except ValueError:
                pass
        for i in range(10):
            r = s.update(i * 0.5, [3500] * len(ch), 60)
        assert np.isfinite(s.ref).all(), f"기준값이 망가짐 {s.ref}"
        assert all(np.isfinite(v) for v in r.deviation.values()), "편차에 NaN"
        return f"{len(bad_inputs)}가지 이상 입력 모두 거부, 이후 상태 {r.state}"

    @check("실시간: 기준 측정에 덜 눌린 센서 → 측정 실패, 직전 기준 유지")
    def _():
        s = SeatClassifier(small)
        good = s.calibrate([[3500] * len(ch)] * 20)
        try:
            s.calibrate([[3500] * (len(ch) - 1) + [4]] * 20)
            raise AssertionError("덜 눌린 센서가 있는데 측정이 통과됨")
        except ValueError:
            pass
        assert s.base.equals(good), "측정 실패 뒤 기준값이 바뀜"
        s.set_baseline([3500] * (len(ch) - 1) + [500])    # 서버에 저장된 기준은 검사 없이 사용
        r = s.update(0, [3500] * len(ch), 60)
        return f"실패 뒤 직전 기준 유지, 저장된 기준 불러오기 OK (상태 {r.state})"

    @check("학습: selection.json 저장 형식 (numpy 값도 JSON으로)")
    def _():
        import json
        from ai.classify.train_pressure import _json_default
        on, _why = decide("auto", np.float64(0.02), "x")
        assert type(on) is bool, f"decide가 {type(on)}를 돌려줌"
        txt = json.dumps({"a": np.bool_(True), "b": np.float64(0.5), "c": np.int64(3), "d": on},
                         default=_json_default)
        assert json.loads(txt) == {"a": True, "b": 0.5, "c": 3, "d": True}

    @check("실시간: 규칙으로 저장된 모델 파일 (버그 2)")
    def _():
        rb = {**small, "model": None, "model_name": "rule"}
        s = SeatClassifier(rb)                        # method 기본값 model이어도 규칙으로 동작해야 함
        assert s.method == "rule", f"method = {s.method}"
        s.calibrate([[3500] * len(ch)] * 20)
        base = dict(zip(ch, [3500] * len(ch)))
        for i in range(30):
            v = dict(base, L_front=800)               # 앞 왼쪽만 빠짐 → 왼다리꼬기
            r = s.update(i * 0.5, v, 50)
        assert r.state == "cross_left", f"상태 {r.state} (기대 cross_left)"

    @check("실시간: 빈 의자 순간 표시 + hold 뒤 empty (버그 3)")
    def _():
        s = SeatClassifier(small, params=SeatParams(hold_sec=4.0))
        s.calibrate([[3500] * len(ch)] * 20)
        r = s.update(0, [3500] * len(ch), 60)
        flags, states = [], []
        for i in range(1, 30):
            r = s.update(i * 0.5, [3] * len(ch), None)
            flags.append(r.empty)
            states.append(r.state)
        assert flags[0] or flags[1], "빈 의자 순간 표시가 바로 안 나옴"
        first_empty = states.index("empty") * 0.5 + 0.5
        assert 4.0 <= first_empty <= 5.0, f"empty가 {first_empty}초 뒤 (기대 약 4초)"
        return f"상태가 empty로 바뀐 시점 {first_empty:g}초"

    @check("실시간: 화면 거리 40/50cm 구간 + 3초 유지 + 거리 없으면 유지")
    def _():
        s = SeatClassifier(small)
        s.calibrate([[3500] * len(ch)] * 20)
        z = s.dist_filter.zone
        assert (z(39.9), z(40.0), z(49.9), z(50.0)) == ("warning", "caution", "caution", "ok"), "구간 경계가 틀림"
        t, states = 0.0, []
        for d in [60] * 10 + [35] * 12:               # 5초 적정 → 6초 가까이
            states.append(s.update(t, [3500] * len(ch), d).distance_state)
            t += 0.5
        assert states[0] == "unknown" and states[9] == "ok", f"처음 상태 {states[:10]}"
        first_warn = states.index("warning")
        # 중앙값(4개)이 35로 넘어가는 데 약 1초 + 3초 유지
        assert 10 + 6 <= first_warn <= 10 + 9, f"warning이 {(first_warn - 10) * 0.5}초 뒤 (기대 약 4초)"
        for _ in range(2):                            # 잠깐 거리 실패(None) → 직전 값으로 상태 유지
            assert s.update(t, [3500] * len(ch), None).distance_state == "warning"
            t += 0.5
        blip = [s.update(t + i * 0.5, [3500] * len(ch), d).distance_state for i, d in enumerate([60, 60, 35, 35])]
        assert "ok" not in blip, f"1초 멀어진 걸로 상태가 바뀜 {blip}"
        return f"warning까지 {(first_warn - 10) * 0.5:g}초, 1초 깜빡임 무시"

    @check("재생: 빈 의자 기록 == 실시간 판정기 (버그 3)")
    def _():
        n = 40
        gg = pd.DataFrame({c: [3500] * 20 + [3] * n for c in ch})
        gg["distance_cm"] = 60.0
        gg["ts"] = pd.to_datetime(pd.Series(np.arange(len(gg)) * 0.5), unit="s")
        gg["cls"], gg["depth"] = "바른자세", "middle"
        rec = stream_person(SeatClassifier(small), gg, pd.Series(False, index=gg.index))
        s = SeatClassifier(small)
        s.calibrate(gg[ch].head(20).values)
        live = [s.update(i * 0.5, gg[ch].iloc[i].values, 60).state for i in range(len(gg))]
        y = np.array(["normal"] * 20 + ["empty"] * n, dtype=object)
        post = PostProcessor(SeatParams(), rec["labels"])
        replay = [post.step(t, p, m, e)[0] for t, p, m, e in
                  zip(rec["ts"], rec["probs"], rec["moving"], rec["empty"])]
        assert replay == live, "재생 후처리 결과가 실시간과 다름"
        r = evaluate(rec, y, np.array([1] * 20 + [2] * n), SeatParams())
        return f"재생 = 실시간 일치, 빈 의자 구간 정확도 {r['acc']:.0%}"

    @check("모델 파일: 저장 → 다른 프로세스에서 불러오기 → 판정 (RF·규칙)")
    def _():
        import subprocess
        import tempfile
        from pathlib import Path
        import joblib
        tr = ds.train_clean & (ds.person != p_test)
        outs = []
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("rf", "rule"):
                bnd = make_bundle(name, F[tr].head(600), ds.y[tr].head(600), channels=ch, ref_mode="raise",
                                  use_extras=False, use_aug=True, movement=vars(MovementParams()),
                                  post={"hold_sec": 0.0, "min_conf": 0.0, "avg_sec": 0.5})
                path = Path(tmp) / f"{name}.joblib"
                joblib.dump(bnd, path, compress=3)
                code = ("import sys; sys.path.insert(0, '.'); from ai.engine.seat import SeatClassifier; "
                        f"s = SeatClassifier(r'{path}'); s.calibrate([[3500] * {len(ch)}] * 20); "
                        "v = dict(zip(s.channels, [3500] * len(s.channels))); v['R_front'] = 500; "
                        "[s.update(i * 0.5, v, 60) for i in range(8)]; r = s.update(5, v, 60); "
                        "print(s.method, r.state)")
                res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                     cwd=str(repo_path(".")), timeout=120)
                assert res.returncode == 0, f"{name} 불러오기 실패: {res.stderr.strip().splitlines()[-1]}"
                outs.append(res.stdout.strip())
        return " / ".join(outs)

    @check("규칙: 기준값이 후보 범위 안쪽에서 찾아짐")
    def _():
        from ai.classify.rules import ThresholdRule
        rule = ThresholdRule().fit(F[ds.train_clean], ds.y[ds.train_clean])
        assert not rule.at_edge, f"범위 끝에 걸림 {rule.at_edge} (t1={rule.t1}, t2={rule.t2})"
        return f"t1 = {rule.t1:.3f}, t2 = {rule.t2:.3f}"

    @check("후처리: 유지 시간·확신도·움직임")
    def _():
        L = ["a", "b"]
        pp = PostProcessor(SeatParams(hold_sec=2, min_conf=0.6, avg_sec=0.5), L)
        seq = [[.9, .1]] * 6 + [[.3, .7]] * 2 + [[.45, .55]] * 3 + [[.2, .8]] * 3
        out = [pp.step(i * 0.5, np.array(p))[0] for i, p in enumerate(seq)]
        assert out[3] == "unknown" and out[4] == "a", f"처음 2초 유지 뒤 a가 아님 {out[:6]}"
        assert out[10] == "a" and out[11] == "b", f"확신도 낮은 동안 유지, 회복 시 b 기대 {out[6:]}"
        pp2 = PostProcessor(SeatParams(hold_sec=0, min_conf=0, avg_sec=0.5), L)
        pp2.step(0, np.array([.9, .1]))
        st = pp2.step(0.5, np.array([.1, .9]), moving=True)[0]
        assert st == "a", "움직이는 중인데 상태가 바뀜"

    @check("학습 결정: auto는 0.5%p 이상일 때만, on/off는 지정대로 (버그 4)")
    def _():
        assert decide("auto", 0.004, "x")[0] is False
        assert decide("auto", 0.005, "x")[0] is True
        assert decide("auto", -0.02, "x")[0] is False
        assert decide("on", -0.5, "x")[0] is True
        assert decide("off", 0.5, "x")[0] is False
        assert hasattr(make_model("svm"), "predict_proba") and hasattr(make_model("knn"), "predict_proba")

    print()
    width = max(len(n) for n, _, _ in RESULTS)
    fails = 0
    for name, ok, detail in RESULTS:
        fails += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}  {detail.splitlines()[0] if detail else ''}")
        if not ok and "\n" in detail:
            print("      " + "\n      ".join(detail.splitlines()[1:]))
    print(f"\n{len(RESULTS) - fails}/{len(RESULTS)} 통과")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
