"""착석 구간 로직 확인 — 시나리오를 가상 시각으로 돌려서 정답과 비교

정답: 착석 구간은 generator.expected_timeline()의 segments,
      첫 비정상은 '구간 안에서 나쁜 자세가 short_filter_sec(3초) 이상 이어진 첫 Step의 시작 + 3초'.
압력 채널 수를 메모리에서만 바꿔서(config.yaml은 그대로) 8채널·6채널 결과가 같은지도 본다.
자리 비움 판정(seating.is_empty)이 라벨과 맞는지, 깜빡이는지(바뀌는 횟수), 기준까지 여유도 본다.

사용 예 (저장소 최상위):
    python -m hardware.server.check_seating                    # 22개 × 8·6채널
    python -m hardware.server.check_seating --scenario empty_6s --channels 6
"""
from __future__ import annotations

import argparse
import copy
import sys

from common.config import ConfigError, load_config
from common.schema import Phase
from common.sensors_base import SampleSkipped
from hardware.mock.generator import ScenarioPlayer, expected_timeline
from hardware.mock.scenarios import SCENARIOS, Scenario
from hardware.server.mock_engine import MockEngine
from hardware.server.seating import EMPTY_TOTAL_ADC, is_empty

T0 = 1_800_000_000.0            # 가상 시작 시각
TOL_SEC = 0.15                  # 시각 비교 허용 오차 (10Hz → 한 줄 0.1초)

LAYOUTS = {                     # 시험용 FSR 배치 (x: 왼 -1 ~ 오른 +1, y: 뒤 -1 ~ 앞 +1)
    8: [[-0.5, 0.75], [0.5, 0.75], [-0.5, 0.25], [0.5, 0.25],          # config.yaml 지금 임시 배치
        [-0.5, -0.25], [0.5, -0.25], [-0.5, -0.75], [0.5, -0.75]],
    6: [[-0.5, 0.67], [0.5, 0.67], [-0.5, 0.0], [0.5, 0.0],            # hardware/mock/README.md 제안
        [-0.5, -0.67], [0.5, -0.67]],
}


def with_channels(cfg: dict, n: int) -> dict:
    cfg = copy.deepcopy(cfg)
    cfg["pressure"]["n_channels"] = n
    cfg["pressure"]["positions"] = LAYOUTS[n]
    return cfg


def run(cfg: dict, scenario: Scenario, seed: int = 1, stats: dict | None = None) -> list[dict]:
    """시나리오 끝까지 돌린 뒤 구간 목록 (끝난 것 + 진행 중인 것).
    stats를 주면 자리 비움 판정 통계를 채운다: 라벨과 다른 줄 수, 판정이 바뀐 횟수(정답 대비),
    빈 의자일 때 압력 합 최대, 앉았을 때 압력 합 최소"""
    st = {"wrong": 0, "flips": 0, "expected_flips": 0, "empty_max": None, "seated_min": None}
    prev = prev_label = None
    player = ScenarioPlayer(cfg, scenario, seed=seed)
    engine = MockEngine(cfg, seed=seed)
    player.start(T0)
    hz = cfg["sampling"]["hz"]
    ts = T0
    for i in range(int(round(scenario.total_sec * hz))):
        ts = T0 + i / hz
        _, step = player.current(ts)
        try:
            sample = player.read(ts)
        except SampleSkipped:
            continue
        engine.update(sample, step)
        total = sum(max(v, 0) for v in sample.pressure.values)
        got, label = is_empty(sample.pressure.values), step.empty and step.phase != Phase.BASELINE.value
        st["wrong"] += got != label
        st["flips"] += prev is not None and got != prev
        st["expected_flips"] += prev_label is not None and label != prev_label
        prev, prev_label = got, label
        key, pick = ("empty_max", max) if label else ("seated_min", min)
        st[key] = total if st[key] is None else pick(st[key], total)
    if stats is not None:
        stats.update(st)
    tracker = engine.seating
    segs = tracker.finished()
    if tracker.current is not None:
        segs.append(tracker.current.to_dict(ts))
    return segs


def expected_first_abnormal(scenario: Scenario, seg: dict, hold: float) -> float | None:
    t = T0
    for s in scenario.steps:
        start, t = t, t + s.seconds
        if s.phase == Phase.BASELINE.value or not s.postures or s.seconds < hold:
            continue
        if seg["start"] - TOL_SEC <= start < seg["end"]:
            return start + hold - seg["start"]
    return None


def check(cfg: dict, scenario: Scenario, verbose: bool) -> tuple[bool, list]:
    hold = cfg["thresholds"]["short_filter_sec"]
    exp = expected_timeline(scenario, T0)["segments"]
    got = run(cfg, scenario)
    ok = len(got) == len(exp)
    lines = []
    for i, g in enumerate(got):
        e = exp[i] if i < len(exp) else None
        end = g["end"] if g["end"] is not None else T0 + scenario.total_sec
        fa_exp = expected_first_abnormal(scenario, {"start": g["start"], "end": end}, hold)
        fa = g["first_abnormal_sec"]
        good = e is not None and abs(g["start"] - e["start"]) <= TOL_SEC \
            and abs(end - e["end"]) <= TOL_SEC \
            and ((fa is None and fa_exp is None) or (fa is not None and fa_exp is not None
                                                     and abs(fa - fa_exp) <= TOL_SEC))
        total = g["normal_sec"] + g["abnormal_sec"] + g["unknown_sec"]
        good &= abs(total - (end - g["start"])) <= 0.25
        ok &= good
        fa_s = "-" if fa is None else f"{fa:.1f}"
        fa_e = "-" if fa_exp is None else f"{fa_exp:.1f}"
        lines.append(f"    {g['start'] - T0:6.1f}~{end - T0:6.1f}초 {g['end_reason'] or '진행 중':<5}"
                     f" 첫 비정상 {fa_s:>5} (정답 {fa_e:>5}) | 정상 {g['normal_sec']:5.1f} 비정상 "
                     f"{g['abnormal_sec']:5.1f} unknown {g['unknown_sec']:5.1f} (합 {total:5.1f})"
                     f" {'✓' if good else '⚠️'}")
    head = (f"  {scenario.name:<17} 구간 {len(got)}개 (정답 {len(exp)}개) {'✓' if ok else '⚠️'}")
    if verbose or not ok:
        print("\n".join([head, *lines]))
    else:
        print(head)
    return ok, got


def main() -> int:
    ap = argparse.ArgumentParser(description="착석 구간 로직 확인")
    ap.add_argument("--scenario", action="append", choices=list(SCENARIOS), help="시나리오 (여러 번 가능)")
    ap.add_argument("--channels", choices=["8", "6", "both"], default="both")
    ap.add_argument("-v", "--verbose", action="store_true", help="구간마다 자세히")
    args = ap.parse_args()
    try:
        base = load_config()
    except ConfigError as e:
        print(e)
        return 1
    names = args.scenario or list(SCENARIOS)
    channels = [8, 6] if args.channels == "both" else [int(args.channels)]

    results, all_ok = {}, True
    for n in channels:
        cfg = with_channels(base, n)
        print(f"\n=== 압력 {n}채널")
        for name in names:
            ok, got = check(cfg, SCENARIOS[name], args.verbose)
            all_ok &= ok
            results[(n, name)] = got

    print(f"\n=== 자리 비움 판정 (압력 합 < {EMPTY_TOTAL_ADC:g}) — 라벨과 다른 줄, 판정이 바뀐 횟수(정답), "
          f"빈 의자 합 최대 / 앉았을 때 합 최소")
    for n in channels:
        cfg = with_channels(base, n)
        rows = []
        for name in names:
            st: dict = {}
            run(cfg, SCENARIOS[name], stats=st)
            rows.append((name, st))
        bad = {nm for nm, st in rows if st["wrong"] or st["flips"] != st["expected_flips"]}
        empty_max = max((st["empty_max"] for _, st in rows if st["empty_max"] is not None), default=None)
        seated_min = min(st["seated_min"] for _, st in rows if st["seated_min"] is not None)
        print(f"  {n}채널: 라벨과 다른 줄 {sum(st['wrong'] for _, st in rows)}개, 깜빡임 있는 시나리오 "
              f"{len(bad)}개 | 빈 의자 합 최대 {empty_max} / 앉았을 때 합 최소 {seated_min}")
        for nm, st in rows:
            if st["expected_flips"] or nm in bad:
                print(f"    {nm:<17} 다른 줄 {st['wrong']:>2}  바뀐 횟수 {st['flips']:>2} (정답 {st['expected_flips']:>2})"
                      f"  빈 의자 합 최대 {st['empty_max']!s:>4} / 앉았을 때 합 최소 {st['seated_min']}"
                      f"{'  ⚠️' if nm in bad else ''}")
        all_ok &= not bad

    if len(channels) == 2:
        same = [name for name in names if results[(8, name)] == results[(6, name)]]
        print(f"\n8채널 vs 6채널: 결과(구간·첫 비정상·정상/비정상/unknown 초)가 같은 시나리오 "
              f"{len(same)}/{len(names)}")
        for name in names:
            if name not in same:
                print(f"  ⚠️ {name}: 8채널 {results[(8, name)]}\n           6채널 {results[(6, name)]}")
        all_ok &= len(same) == len(names)
    print("\n전체:", "통과" if all_ok else "⚠️ 다른 곳 있음")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
