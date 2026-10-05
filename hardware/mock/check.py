"""만든 CSV를 요약해서 눈으로 확인 — 구간별 라벨·센서 상태, 자리 비움·착석 구간

main.py --mode replay와 같은 ReplaySource로 읽으므로, 여기서 읽히면 replay에서도 읽힌다.
더미 데이터면 세션 JSON의 정답(extra.expected)과 비교하고, 직접 수집한 CSV면 라벨 구간만 보여준다.

사용 예:
    python -m hardware.mock.check data/synthetic/mock_empty_6s.csv
    python -m hardware.mock.check data/synthetic/mock_*.csv        # 여러 개
"""
from __future__ import annotations

import argparse
import glob
import logging
import sys
from datetime import datetime

import numpy as np

from common.config import ConfigError, load_config
from common.replay import ReplayError, ReplaySource
from hardware.mock.generator import EMPTY_OFF_SEC

GAP_SEC = 0.15                  # 줄 간격이 이보다 길면 빠진 순간 (압력 읽기 실패)


def _clock(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S.%f")[:-5]


def _sensor_counts(items, limits: tuple[float, float]) -> dict[str, int]:
    c = {"rows": 0, "dist_ok": 0, "dist_fail": 0, "dist_out": 0, "cam_on": 0, "cam_miss": 0,
         "cam_off": 0}
    for sample, _ in items:
        c["rows"] += 1
        d = sample.distance
        if not d.valid:
            c["dist_fail"] += 1
        elif not limits[0] <= d.distance_mm <= limits[1]:
            c["dist_out"] += 1
        else:
            c["dist_ok"] += 1
        p = sample.pose
        c["cam_off" if p is None else "cam_on" if p.detected else "cam_miss"] += 1
    return c


def _fmt_counts(c: dict[str, int]) -> str:
    return (f"{c['rows']:>4}줄 | 거리 정상 {c['dist_ok']:>4} 실패 {c['dist_fail']:>3} 범위밖 {c['dist_out']:>2}"
            f" | 카메라 인식 {c['cam_on']:>4} 미인식 {c['cam_miss']:>3} 꺼짐 {c['cam_off']:>3}")


def _runs(items, key) -> list[tuple[float, float, object, list]]:
    """같은 값이 이어지는 구간 (시작 ts, 마지막 ts, 값, 줄들)"""
    out = []
    for item in items:
        v = key(item)
        if out and out[-1][2] == v:
            out[-1][3].append(item)
        else:
            out.append([item[0].ts, item[0].ts, v, [item]])
        out[-1][1] = item[0].ts
    return [tuple(r) for r in out]


def check(path: str, cfg: dict) -> bool:
    n_channels = cfg["pressure"]["n_channels"]
    limits = (cfg["distance"]["min_mm"], cfg["distance"]["max_mm"])
    dt = 1.0 / cfg["sampling"]["hz"]             # 줄 하나의 길이 (구간 길이 = 마지막 - 처음 + dt)
    src = ReplaySource(path)
    items = src.samples
    ts = np.array([s.ts for s, _ in items])
    t0 = ts[0]
    exp = (src.info.extra.get("expected") if src.info else None) or {}
    off_sec = exp.get("empty_off_sec", EMPTY_OFF_SEC)
    ok = True

    print(f"\n=== {path}")
    if src.info:
        e = src.info.extra
        print(f"시나리오 {e.get('scenario', '-')} | seed {e.get('seed', '-')} | "
              f"거리센서 {e.get('distance_sensor', '-')} | {src.info.notes}")
    days = "" if _clock(ts[0])[:10] == _clock(ts[-1])[:10] else "  ← 날짜가 바뀜"
    print(f"시각  {_clock(ts[0])} ~ {_clock(ts[-1])}{days}")
    gaps = int(np.sum(np.diff(ts) > GAP_SEC))
    print(f"줄 수 {len(items)} (깨진 줄 {src.skipped}, 빠진 순간 {gaps}) | 압력 {len(src.channels)}채널 "
          f"{'' if len(src.channels) == n_channels else f'⚠️ config.yaml은 {n_channels}채널 → replay 안 됨'}")
    p = np.array([s.pressure.values for s, _ in items])
    print(f"압력 범위 {p.min()}~{p.max()}, 채널별 평균 {np.round(p.mean(axis=0)).astype(int).tolist()}")

    # 구간별 (정답이 있으면 정답 구간, 없으면 라벨이 바뀌는 곳마다)
    print("\n구간별 라벨 · 센서 상태 (시작 후 초)")
    if exp.get("steps"):
        for st in exp["steps"]:
            part = [it for it in items if st["start"] <= it[0].ts < st["end"]]
            labels = f"{st['seat']}/{st['head']}/{st['tilt']}"
            extra = f" +lean_{st['extra_lean']}" if st["extra_lean"] else ""
            faults = f" [{', '.join(st['faults'])}]" if st["faults"] else ""
            print(f"  {st['start'] - t0:6.1f}~{st['end'] - t0:6.1f}  {st['phase']:<8} "
                  f"{labels + extra:<34} 대표={st['dominant']:<12} {_fmt_counts(_sensor_counts(part, limits))}{faults}")
    else:
        for a, b, v, part in _runs(items, lambda it: tuple(it[1].values())):
            print(f"  {a - t0:6.1f}~{b - t0:6.1f}  {'/'.join(v):<40} {_fmt_counts(_sensor_counts(part, limits))}")

    # 자리 비움 → 착석 구간, 센서 꺼짐
    empties = [r for r in _runs(items, lambda it: it[1]["seat"] == "empty") if r[2]]
    if empties:
        print(f"\n자리 비움 (꺼짐 기준 {off_sec:g}초)")
    for a, b, _, part in empties:
        dur = b - a + dt
        off = [s.ts for s, _ in part if s.pose is None]
        if off:
            msg = f"{off[0] - a:.1f}초째 카메라·거리 꺼짐 → 착석 구간 종료"
        else:
            msg = "센서 켜진 채 유지 → 착석 구간 유지"
        p_mean = np.mean([s.pressure.values for s, _ in part])
        print(f"  {a - t0:6.1f}~{b - t0:6.1f} ({dur:.1f}초) 압력 평균 {p_mean:.1f} 기록 {len(part)}줄 | {msg}")
        if bool(off) != (dur >= off_sec):
            print("    ⚠️ 꺼짐 여부가 기준과 다름")
            ok = False

    # 다시 앉은 뒤 카메라 켜는 중 (pose_detected=0)
    for i, (s, lab) in enumerate(items[1:], start=1):
        if items[i - 1][0].pose is None and s.pose is not None:
            warm = 0
            for s2, _ in items[i:]:
                if s2.pose is None or s2.pose.detected:
                    break
                warm += 1
            print(f"  재착석 {s.ts - t0:6.1f}초: 카메라 켠 뒤 미인식 {warm}줄 ({warm * dt:.1f}초)")

    seated_off = sum(1 for s, lab in items if s.pose is None and lab["seat"] != "empty")
    if seated_off:
        print(f"  ⚠️ 앉아 있는데 카메라가 꺼진 줄 {seated_off}개")
        ok = False

    # 착석 구간 = 긴 자리 비움(off_sec 이상)으로 나뉜 덩어리
    n_got, in_seg = 0, False
    for a, b, is_empty, _ in _runs(items, lambda it: it[1]["seat"] == "empty"):
        if is_empty and b - a + dt >= off_sec:
            in_seg = False
        elif not in_seg:
            n_got, in_seg = n_got + 1, True
    line = f"\n착석 구간 {n_got}개"
    if exp.get("segments") is not None:
        n_exp = len(exp["segments"])
        ok &= n_exp == n_got
        line += (f" (정답 {n_exp}개) {'일치' if n_exp == n_got else '⚠️ 다름'}: "
                 + ", ".join(f"{s['start'] - t0:.0f}~{s['end'] - t0:.0f}초" for s in exp["segments"]))
    print(line)
    return ok


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="[SitSense check] %(message)s")
    ap = argparse.ArgumentParser(description="수집·더미 CSV 요약")
    ap.add_argument("files", nargs="+", help="CSV 경로 (와일드카드 가능)")
    args = ap.parse_args()
    try:
        cfg = load_config()
    except ConfigError as e:
        print(e)
        return 1
    paths = [p for f in args.files for p in (sorted(glob.glob(f)) or [f])]
    all_ok = True
    for path in paths:
        try:
            all_ok &= check(path, cfg)
        except ReplayError as e:
            print(f"\n=== {path}\n읽기 실패: {e}")
            all_ok = False
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
