"""AI 엔진 자동 점검 — 서버에 끼우기 전에 엔진이 서버 흐름대로 동작하는지 PASS/FAIL로 확인

실행 (저장소 최상위, .venv 켠 상태):
    python -m ai.engine.check_engine          # 1분 안쪽 (카메라·MediaPipe 없이 돌아감)

점검 항목
    설정      config.yaml 압력 채널 순서 == 모델 채널 순서
    좌면      수집 데이터를 엔진에 넣은 결과 == 좌면 판정기(seat.py)에 직접 넣은 결과 (사람 3명)
              같은 데이터를 0.1초 간격(서버 mock 속도)으로 넣어도 결과가 같음 (0.5초마다 한 번만 판정)
    상체      하드웨어 mock 시나리오(압력은 실제 크기로 키움)(바른 자세·거북목·왼쪽/오른쪽 기울기)를 서버처럼 10Hz로 재생 → 목·기울기 정답
              자리 비움 → 좌면 empty, 목·기울기 unknown / 카메라 끊김 → 목·기울기 unknown, 오류 없음
    기준 자세  덜 눌린 센서 → pressure_low, 사람 없음 → no_person, 실패하면 이전 기준 유지
              서버에 저장된 기준 형식(BaselineValue)으로 복원 → 같은 판정
    입력      이상한 압력 값(NaN)이 와도 멈추지 않고 직전 상태 유지, 결과는 JSON으로 저장 가능
    카메라    CameraPoseSource 켜기(warming → on)·최근 결과·끄기·오류 (가짜 카메라로)
"""
from __future__ import annotations

import json
import logging
import sys
import time
import traceback
import warnings

import numpy as np

warnings.filterwarnings("ignore")
RESULTS: list[tuple[str, bool, str]] = []
T0 = 1_790_000_000.0            # 가상 시각 (time.time() 초)
POSTURE_START = 20.0            # mock 시나리오: 기준 10초 + 바른 자세 10초 뒤 자세 시작
MOCK_PRESSURE_SCALE = 7.0       # 하드웨어 mock 압력(약 500) → 실제 수집값 크기(약 3500)
N_ROWS = 1500                   # 사람마다 비교할 순간 수 (전체는 오래 걸림, 앞 12.5분 = 자세 여러 개)


def check(name: str):
    def deco(fn):
        print(f"… {name}", flush=True)
        try:
            RESULTS.append((name, True, fn() or ""))
        except AssertionError as e:
            RESULTS.append((name, False, str(e)))
        except Exception as e:  # noqa: BLE001
            RESULTS.append((name, False, f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=4)}"))
        return fn
    return deco


def main() -> int:
    logging.basicConfig(level=logging.ERROR)
    from ai.camera.pose_source import CameraPoseSource
    from ai.engine.seat import SeatClassifier
    from ai.engine.sitsense_engine import SitSenseEngine, channel_names_from_positions
    from ai.features.pressure_pipeline import (
        ADC_MAX, BASELINE_CLS, BASELINE_DEPTH, BASELINE_MIN_FRAC, BASELINE_SAMPLES, MovementParams,
        detect_movement, load_folder,
    )
    from common.config import load_config, repo_path
    from common.sensors_base import (
        DistanceReading, PoseReading, PressureReading, Sample, SampleSkipped, SensorError,
    )
    from hardware.mock.generator import ScenarioPlayer
    from hardware.mock.scenarios import get_scenario

    cfg = load_config()
    model_path = repo_path("ai/models/final_seat_model.joblib")

    def sample(ts, pressure, dist_mm=None, pose=None):
        d = DistanceReading(ts, int(dist_mm), 0) if dist_mm is not None else DistanceReading.failed(ts)
        return Sample(ts=ts, pressure=PressureReading(ts, [int(v) for v in pressure]), distance=d, pose=pose)

    @check("설정: config.yaml 압력 채널 순서 == 모델 채널 순서")
    def _():
        names = channel_names_from_positions(cfg["pressure"]["positions"])
        model = SeatClassifier(model_path).channels
        assert names == model, f"config {names} / 모델 {model}"
        return " ".join(names)

    # ------------------------------------------------------------------ 좌면 (수집 데이터)
    df, _ = load_folder(repo_path("data/raw/pressure"))
    seat_ch = SeatClassifier(model_path).channels
    bundle_mv = MovementParams(**SeatClassifier(model_path).bundle["movement"])
    movement = detect_movement(df, seat_ch, bundle_mv)

    def person_rows(p):
        g = df[df["person"] == p]
        b = g[(g["cls"] == BASELINE_CLS) & (g["depth"] == BASELINE_DEPTH) & ~movement.loc[g.index]]
        b = b[(b[seat_ch] >= BASELINE_MIN_FRAC * ADC_MAX).all(axis=1)].head(BASELINE_SAMPLES)
        dist = g["distance_cm"].to_numpy(dtype=float)
        return g[seat_ch].to_numpy(dtype=float), dist, b[seat_ch].to_numpy(dtype=float)

    @check("좌면: 엔진 결과 == 좌면 판정기 직접 결과 (수집 데이터 3명 × 앞 1500순간, 0.5초 간격)")
    def _():
        out = []
        for p in sorted(df["person"].unique()):
            X, dist, base = person_rows(p)
            X, dist = X[:N_ROWS], dist[:N_ROWS]
            seat = SeatClassifier(model_path, overrides={"dist_warn_cm": 40, "dist_caution_cm": 50})
            seat.calibrate(base)
            eng = SitSenseEngine(cfg, model_path, require_camera=False)
            res = eng.calibrate([sample(T0, row) for row in base])
            assert res.ok, res.message
            diff = 0
            for i, (row, d) in enumerate(zip(X, dist)):
                ts = T0 + 100 + i * 0.5
                dcm = None if np.isnan(d) else float(int(d * 10)) / 10
                a = seat.update(ts, row, dcm)
                b = eng.update(sample(ts, row, None if dcm is None else dcm * 10))
                diff += (a.state != b.seat_state) or (a.distance_state != b.distance_level)
            assert diff == 0, f"{p}: {diff}순간 다름"
            out.append(f"{p} {len(X)}순간")
        return ", ".join(out) + " 모두 같음"

    @check("좌면: 0.1초 간격(서버 mock 속도)으로 넣어도 0.5초 간격과 같은 결과")
    def _():
        p = sorted(df["person"].unique())[0]
        X, dist, base = person_rows(p)
        X, dist = X[:300], dist[:300]
        e2 = SitSenseEngine(cfg, model_path, require_camera=False)
        e10 = SitSenseEngine(cfg, model_path, require_camera=False)
        for e in (e2, e10):
            e.calibrate([sample(T0, row) for row in base])
        diff = 0
        for i, (row, d) in enumerate(zip(X, dist)):
            dm = None if np.isnan(d) else d * 10
            a = e2.update(sample(T0 + 100 + i * 0.5, row, dm))
            for k in range(5):                         # 같은 값이 0.1초마다 5번 (마지막 4번은 판정 건너뜀)
                b = e10.update(sample(T0 + 100 + i * 0.5 + k * 0.1, row, dm))
                if k == 0:
                    diff += a.seat_state != b.seat_state
        assert diff == 0, f"{diff}순간 다름"
        return f"{len(X)}순간 같음"

    # ------------------------------------------------------------------ 상체 (하드웨어 mock 시나리오)
    def scaled_player(name, seed):
        """하드웨어 mock 압력은 300~600 정도라 실제 MCP3208 수집값(3200~3800)보다 훨씬 작다.
        상체 흐름만 보려고 압력을 MOCK_PRESSURE_SCALE배(최대 4095)로 키운다 (모양은 그대로, 빈 의자 0 근처도 그대로)"""
        pl = ScenarioPlayer(cfg, get_scenario(name), seed=seed)
        read = pl.read

        def scaled(ts=None):
            s = read(ts)
            v = np.clip(np.asarray(s.pressure.values) * MOCK_PRESSURE_SCALE, 0, 4095).astype(int).tolist()
            return Sample(ts=s.ts, pressure=PressureReading(s.pressure.ts, v), distance=s.distance, pose=s.pose)
        pl.read = scaled
        return pl

    def run_scenario(name, seconds=None, seed=1, hz=10):
        """서버처럼: 처음 10초 Sample을 모아 calibrate → 이후 update. (결과 목록, 엔진)"""
        sc = get_scenario(name)
        pl = scaled_player(name, seed)
        pl.start(T0)
        eng = SitSenseEngine(cfg, model_path)
        base, results = [], []
        total = sc.total_sec if seconds is None else seconds
        for i in range(int(total * hz)):
            ts = T0 + i / hz
            try:
                s = pl.read(ts)
            except SampleSkipped:
                continue
            if ts - T0 < cfg["thresholds"]["baseline_seconds"]:
                base.append(s)
                continue
            if not eng.baseline_ready:
                res = eng.calibrate(base)
                assert res.ok, f"{name}: 기준 측정 실패 {res.reason} {res.message}"
            results.append((ts - T0, eng.update(s)))
        return results, eng

    def tail_states(results, key, from_sec):
        return {getattr(r, key) for t, r in results if t >= from_sec}

    @check("상체: mock 바른 자세 → 목 normal, 기울기 none")
    def _():
        res, _ = run_scenario("normal")
        h, t = tail_states(res, "head_state", 15), tail_states(res, "tilt_state", 15)
        assert h == {"normal"} and t == {"none"}, f"목 {h}, 기울기 {t}"

    @check("상체: mock 거북목 → forward / 왼쪽 기울기 → left / 오른쪽 → right")
    def _():
        out = []
        for name, key, want in (("forward_head", "head_state", "forward"), ("tilt_left", "tilt_state", "left"),
                                ("tilt_right", "tilt_state", "right")):
            res, _ = run_scenario(name)
            states = [getattr(r, key) for t, r in res]
            assert want in states, f"{name}: {want}가 한 번도 안 나옴 ({set(states)})"
            first = next(t for t, r in res if getattr(r, key) == want)
            last = tail_states(res, key, first + 1)
            assert last == {want}, f"{name}: {want} 뒤에 {last}로 바뀜"
            out.append(f"{name} 자세 시작 {first - POSTURE_START:.1f}초 뒤 {want}")
        return ", ".join(out)

    @check("상체: mock 자리 비움 → 좌면 empty, 목·기울기 unknown / 카메라 끊김 → unknown, 오류 없음")
    def _():
        res, _ = run_scenario("empty")
        seat = [r.seat_state for t, r in res]
        assert "empty" in seat, f"empty가 안 나옴 {set(seat)}"
        first = next(t for t, r in res if r.seat_state == "empty")
        after = {(r.head_state, r.tilt_state) for t, r in res if t >= first + 4 and r.seat_state == "empty"}
        assert after == {("unknown", "unknown")}, f"자리 비움인데 목·기울기 {after}"
        res2, _ = run_scenario("camera_lost")
        lost = [r for t, r in res2 if not r.pose_fresh or r.deltas == {}]
        assert lost, "카메라 끊김 구간이 안 보임"
        assert any(r.head_state == "unknown" for r in lost), "카메라가 끊겼는데 목 상태가 unknown이 안 됨"
        return f"일어난 뒤 {first - POSTURE_START:.1f}초 만에 empty"

    # ------------------------------------------------------------------ 기준 자세
    @check("기준 자세: 덜 눌린 센서 → pressure_low, 사람 없음 → no_person, 실패하면 이전 기준 유지")
    def _():
        res, eng = run_scenario("normal", seconds=20)
        before = list(eng.pressure_base)
        last = res[-1][1]
        weak = [sample(T0 + 50 + i * 0.5, [3500, 3500, 3500, 3500, 3500, 100], 600) for i in range(20)]
        r1 = SitSenseEngine(cfg, model_path, require_camera=False).calibrate(weak)
        assert not r1.ok and r1.reason == "pressure_low", f"덜 눌린 센서: {r1}"
        r2 = eng.calibrate(weak)                              # 카메라 필요 → 사람 없음이 먼저
        assert not r2.ok and r2.reason == "no_person", f"사람 없음: {r2}"
        assert eng.pressure_base == before, "실패했는데 기준이 바뀜"
        assert last.baseline_ready
        return "두 경우 모두 실패 처리, 이전 기준 유지"

    @check("기준 자세: 서버 저장 형식(BaselineValue)으로 복원 → 같은 판정")
    def _():
        sc = get_scenario("tilt_left")
        pl = scaled_player("tilt_left", 3)
        pl.start(T0)
        samples = [pl.read(T0 + i / 10) for i in range(int(sc.total_sec * 10))]
        base = [s for s in samples if s.ts - T0 < 10]
        a = SitSenseEngine(cfg, model_path)
        assert a.calibrate(base).ok
        poses = [s.pose for s in base if s.pose is not None and s.pose.detected]
        names = poses[0].points.keys()
        pose_mean = {n: [float(np.mean([getattr(q.points[n], k) for q in poses if n in q.points]))
                         for k in ("x", "y", "z", "visibility")] for n in names}
        b = SitSenseEngine(cfg, model_path)
        b.restore(np.mean([s.pressure.values for s in base], axis=0).tolist(),
                  float(np.median([s.distance.distance_mm for s in base if s.distance.valid])), pose_mean)
        same = tot = 0
        for s in samples:
            if s.ts - T0 < 10:
                continue
            ra, rb = a.update(s), b.update(s)
            tot += 1
            same += (ra.head_state, ra.tilt_state) == (rb.head_state, rb.tilt_state)
        assert same / tot >= 0.97, f"목·기울기 일치 {same}/{tot}"
        return f"목·기울기 일치 {same}/{tot}"

    # ------------------------------------------------------------------ 입력·출력
    @check("입력: NaN·범위 밖 압력 → 멈추지 않고 직전 상태 유지, 결과는 JSON 저장 가능")
    def _():
        res, eng = run_scenario("normal", seconds=20)
        last = res[-1][1]
        ts = T0 + 30
        bad = Sample(ts=ts, pressure=PressureReading(ts, [float("nan")] * 6),
                     distance=DistanceReading(ts, 600, 0), pose=None)
        r = eng.update(bad)
        assert r.seat_state == last.seat_state, f"이상한 값 뒤 상태 {last.seat_state} → {r.seat_state}"
        r2 = eng.update(sample(ts + 0.5, [5000] * 6, 600))
        assert r2.seat_state == last.seat_state
        json.dumps(r.to_dict())
        json.dumps(last.to_dict())
        fresh = SitSenseEngine(cfg, model_path).update(sample(ts, [3000] * 6, 600))
        assert fresh.seat_state == "unknown" and not fresh.baseline_ready, "기준 없이 판정함"
        return "직전 상태 유지, JSON OK, 기준 전에는 unknown"

    # ------------------------------------------------------------------ 카메라 부품
    @check("카메라: 켜기(warming → on)·최근 결과·끄기·오류 (가짜 카메라)")
    def _():
        class FakeCam:
            width, height = 1280, 720
            def __init__(self, fail=False):
                self.fail, self.n = fail, 0
            def read(self):
                if self.fail:
                    return None
                self.n += 1
                time.sleep(0.01)
                return type("F", (), {"ts": time.time(), "image": None})()
            def close(self):
                pass

        class FakeExt:
            def extract(self, frame):
                return PoseReading(frame.ts, True, {})
            def close(self):
                pass

        logging.getLogger("sitsense.camera").setLevel(logging.CRITICAL)   # 아래 일부러 낸 오류 로그 숨김
        src = CameraPoseSource(lambda: FakeCam(), lambda: FakeExt(), fps=50)
        assert src.state() == "off" and src.read() is None
        src.power_on()
        t_end = time.time() + 2
        while src.read() is None and time.time() < t_end:
            time.sleep(0.01)
        assert src.state() == "on" and src.read() is not None, f"켠 뒤 결과 없음 ({src.state()})"
        t1 = src.read().ts
        time.sleep(0.2)
        assert src.read().ts > t1, "최근 결과가 갱신되지 않음"
        a = src.read()
        a.ts += 1000                                  # 서버 clock.stamp()처럼 시각을 그 자리에서 바꿔도
        assert src.read().ts < a.ts, "read()가 같은 객체를 줘서 시각 변경이 쌓임 (clock.stamp 문제)"
        src.power_off()
        assert src.state() == "off" and src.read() is None, "끈 뒤에도 결과가 남음"
        bad = CameraPoseSource(lambda: FakeCam(fail=True), lambda: FakeExt(), fps=50)
        bad.power_on()
        t_end = time.time() + 5
        while bad.error is None and time.time() < t_end:
            time.sleep(0.05)
        try:
            bad.read()
            raise AssertionError("카메라 오류인데 read()가 SensorError를 안 냄")
        except SensorError:
            pass
        bad.close()
        return f"프레임 {src.frames}장 처리, 오류 감지 OK"

    print()
    w = max(len(n) for n, _, _ in RESULTS) + 2
    for name, ok, detail in RESULTS:
        print(f"{'PASS' if ok else 'FAIL'}  {name:{w}s}{detail}")
    n_ok = sum(ok for _, ok, _ in RESULTS)
    print(f"\n{n_ok}/{len(RESULTS)} 통과")
    return 0 if n_ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
