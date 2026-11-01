"""며칠치 가짜 기록 — 리포트(일간·주간·목표) 화면 확인용

10Hz로 실제로 돌리면 2주가 너무 오래 걸려서, 세션·착석 구간·시간대별 기록(posture_hour, hour_metrics)을
직접 만들고 하루 요약은 진짜 자정 작업(hardware/server/db/jobs.py)으로 만든다. 원본(raw_chunks)은 이미 지워진 상태.

패턴 (리포트에서 차이가 보이게)
    - 오후 3시 이후 거북목이 늘어남 (주간 '이번 주 패턴')
    - 뒤쪽 날일수록 나쁜 자세가 줄어듦 (지난주 대비 개선, 목표 추세 decreasing)
    - 다리 꼬기·기대기는 오른쪽이 더 많음 (주된 방향)
특별한 날 (시작일 기준 순서)
    3번째 날   기록 없음
    5번째 날   23:30 ~ 다음 날 00:40 자정에 걸친 착석 구간
    10번째 날  하루 종일 자리 비움 (세션은 있었지만 착석 0)

사용 예 (저장소 최상위):
    python -m hardware.mock.fake_history                                  # 어제까지 14일
    python -m hardware.mock.fake_history --end 2026-10-05 --days 14 --seed 1 --overwrite
    python -m hardware.server.mock_server --db data/synthetic/sitsense_history.db   # /docs에서 리포트 확인
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from common.config import ConfigError, load_config, repo_path
from hardware.server.clock import TIMEZONE, AppClock, load_zone
from hardware.server.db.jobs import DayJobs
from hardware.server.db.store import DEFAULT_SETTINGS, Recorder
from hardware.server.seating import PostureStatus, classify, postures_of

log = logging.getLogger("sitsense.fake_history")
KST = load_zone(TIMEZONE)
DEFAULT_DB = "data/synthetic/sitsense_history.db"

NO_RECORD_DAY, MIDNIGHT_DAY, EMPTY_DAY = 2, 4, 9        # 시작일 기준 0부터 센 순서
BASE_DISTANCE_MM = 600
FORWARD_DISTANCE_MM = 490                               # 기준보다 11cm 가까움 → closer
CLOSE_DELTA_MM = 100

# (좌면, 목, 기울기) 조합과 기본 확률
COMBOS = [
    (("normal", "normal", "none"), 0.62),
    (("normal", "forward", "none"), 0.10),
    (("cross_right", "normal", "none"), 0.07),
    (("cross_left", "normal", "none"), 0.03),
    (("lean_right", "normal", "none"), 0.05),
    (("lean_left", "normal", "none"), 0.02),
    (("normal", "normal", "left"), 0.03),
    (("normal", "normal", "right"), 0.02),
    (("cross_right", "forward", "none"), 0.03),         # 겹침: 대표는 거북목
    (("unknown", "unknown", "unknown"), 0.03),          # 카메라 미인식 등
]


def at(d: date, h: int, m: int = 0) -> float:
    return datetime(d.year, d.month, d.day, h, m, tzinfo=KST).timestamp()


class Builder:
    def __init__(self, rec: Recorder, rng: random.Random):
        self.rec, self.c, self.rng = rec, rec.conn, rng
        self.posture: dict[tuple, float] = {}
        self.metrics: dict[tuple, list[float]] = {}
        self.last_ts = 0.0

    def local(self, ts: float) -> datetime:
        return datetime.fromtimestamp(ts, KST)

    def split(self, t0: float, t1: float):
        t = t0
        while t < t1 - 1e-9:
            d = self.local(t)
            nxt = (d.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)).timestamp()
            e = min(t1, nxt)
            yield d.strftime("%Y-%m-%d"), d.hour, e - t
            t = e

    def pick(self, hour: int, improve: float) -> tuple[str, str, str]:
        weights = []
        for combo, p in COMBOS:
            bad = combo != ("normal", "normal", "none") and combo[0] != "unknown"
            if bad:
                p *= improve
            if combo[1] == "forward" and hour >= 15:
                p *= 2.5                                  # 오후 3시 이후 거북목
            weights.append(p)
        return self.rng.choices([c for c, _ in COMBOS], weights)[0]

    def run(self, t0: float, t1: float, combo: tuple[str, str, str]) -> None:
        seat, head, tilt = combo
        status, _ = classify(seat, head, tilt, postures_of(seat, head, tilt))
        for day, hour, sec in self.split(t0, t1):
            key = (day, hour, *combo)
            self.posture[key] = self.posture.get(key, 0.0) + sec
            m = self.metrics.setdefault((day, hour), [0.0, 0.0, 0, 0.0, 0.0])
            m[0] += sec
            if status is not PostureStatus.UNKNOWN:
                mm = (FORWARD_DISTANCE_MM if head == "forward" else BASE_DISTANCE_MM) + self.rng.uniform(-15, 15)
                n = int(sec * 10)                         # 10Hz
                m[1] += mm * n
                m[2] += n
                m[3] += sec
                if BASE_DISTANCE_MM - mm >= CLOSE_DELTA_MM:
                    m[4] += sec

    def segment(self, sid: str, start: float, end: float, reason: str, first: bool, improve: float) -> None:
        """착석 구간 하나: 앞부분 unknown(기준 측정·필터) 뒤 자세가 몇 분씩 바뀜"""
        counts = {"normal": 0.0, "abnormal": 0.0, "unknown": 0.0}
        first_abnormal = None
        t = start
        warm = min(13.0 if first else 4.0, end - start)
        self.run(t, t + warm, ("unknown", "unknown", "unknown"))
        counts["unknown"] += warm
        t += warm
        while t < end:
            dur = min(self.rng.uniform(60, 480), end - t)
            combo = self.pick(self.local(t).hour, improve)
            status, _ = classify(*combo, postures_of(*combo))
            if status is PostureStatus.ABNORMAL and first_abnormal is None:
                first_abnormal = t - start + 3.0                 # 3초 유지해야 확정
            self.run(t, t + dur, combo)
            counts[status.value] += dur
            t += dur
        self.c.execute(
            "INSERT INTO seating_segments (session_id, start_ts, end_ts, day, end_reason, first_abnormal_sec, "
            "normal_sec, abnormal_sec, unknown_sec) VALUES (?,?,?,?,?,?,?,?,?)",
            (sid, start, end, self.local(start).strftime("%Y-%m-%d"), reason, first_abnormal,
             counts["normal"], counts["abnormal"], counts["unknown"]))

    def session(self, start: float, end: float, improve: float, *, sit: bool = True,
                sit_from: float | None = None) -> str:
        sid = "s_" + self.local(start).strftime("%Y%m%d_%H%M%S")
        self.c.execute("INSERT INTO sessions (id, started_at, ended_at, end_reason, boot_id, timezone, settings, "
                       "layout_id) VALUES (?,?,?,?,?,?,?,?)",
                       (sid, start, end, "stop", "fake", TIMEZONE, json.dumps(DEFAULT_SETTINGS), self.rec.layout_id))
        self.c.execute("INSERT INTO session_events (session_id, ts, kind) VALUES (?, ?, 'start')", (sid, start))
        self.c.execute("INSERT INTO session_events (session_id, ts, kind) VALUES (?, ?, 'stop')", (sid, end))
        self.last_ts = max(self.last_ts, end)
        if not sit:
            return sid
        t, first = sit_from or start + self.rng.uniform(5, 60), True
        while t < end - 60:
            seg_end = min(t + self.rng.uniform(40, 70) * 60, end)
            last = seg_end >= end - 60
            self.segment(sid, t, seg_end, "stop" if last else "away", first, improve)
            first = False
            t = seg_end + self.rng.uniform(5, 15) * 60           # 5분 넘게 자리 비움 → 다음 구간
        return sid

    def flush(self) -> None:
        self.c.executemany("INSERT INTO posture_hour (day, hour, seat, head, tilt, seconds) VALUES (?,?,?,?,?,?) "
                           "ON CONFLICT DO UPDATE SET seconds = seconds + excluded.seconds",
                           [(*k, round(v, 3)) for k, v in self.posture.items()])
        self.c.executemany("INSERT INTO hour_metrics (day, hour, seated_sec, distance_sum_mm, distance_n, "
                           "distance_known_sec, closer_sec) VALUES (?,?,?,?,?,?,?)",
                           [(*k, *[round(x, 3) for x in v]) for k, v in self.metrics.items()])


def build(db: Path, cfg: dict, end: date, days: int, seed: int) -> dict:
    rng = random.Random(seed)
    start = end - timedelta(days=days - 1)
    now = at(end + timedelta(days=1), 1, 30)                     # 마지막 날 다음 날 01:30에 자정 작업
    t = [0.0]
    clock = AppClock(monotonic=lambda: t[0], wall=lambda: now)
    clock.sync(now, TIMEZONE)
    rec = Recorder(db, cfg, clock)
    b = Builder(rec, rng)
    rec.conn.execute("BEGIN")
    for i in range(days):
        d = start + timedelta(days=i)
        improve = 1.3 - 0.6 * i / max(days - 1, 1)               # 앞쪽 날 나쁨 → 뒤쪽 날 좋아짐
        if i == NO_RECORD_DAY:
            continue
        if i == EMPTY_DAY:
            b.session(at(d, 9), at(d, 17), improve, sit=False)   # 세션은 켰지만 하루 종일 자리 비움
            continue
        weekend = d.weekday() >= 5
        j = lambda: rng.uniform(-20, 20) * 60                    # noqa: E731 — 시각 흔들기
        b.session(at(d, 9) + j(), at(d, 12) + j(), improve)
        if not weekend:
            b.session(at(d, 13, 30) + j(), at(d, 18) + j(), improve)
        if i == MIDNIGHT_DAY:                                    # 23:30 ~ 다음 날 00:40 한 구간으로 착석
            nxt = d + timedelta(days=1)
            sid = b.session(at(d, 22, 30), at(nxt, 0, 45), improve, sit=False)
            b.segment(sid, at(d, 23, 30), at(nxt, 0, 40), "stop", True, improve)
    b.flush()
    rec.conn.execute("INSERT INTO baselines (kind, status, measured_at, seconds, layout_id, pressure, distance_mm, "
                     "is_current) VALUES ('initial', 'ok', ?, 10, ?, ?, ?, 1)",
                     (at(start, 9), rec.layout_id, json.dumps([500.0] * rec.n_channels), BASE_DISTANCE_MM))
    rec.conn.execute("INSERT INTO meta (key, value) VALUES ('last_seen', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (repr(b.last_ts),))
    rec.conn.execute("COMMIT")
    report = DayJobs(rec.conn, clock).run(now)                   # 진짜 자정 작업으로 요약
    rec.conn.close()
    return {"start": start.isoformat(), "end": end.isoformat(), "summarized": report["summarized"],
            "failed": report["failed"], "last_seen": b.last_ts}


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="[SitSense history] %(message)s")
    ap = argparse.ArgumentParser(description="며칠치 가짜 기록 (리포트 확인용)")
    ap.add_argument("--db", default=DEFAULT_DB, help=f"만들 DB (기본 {DEFAULT_DB})")
    ap.add_argument("--end", default=None, help="마지막 날 YYYY-MM-DD (기본: 어제, 한국 시간)")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--overwrite", action="store_true", help="같은 파일이 있으면 지우고 새로")
    args = ap.parse_args()
    if args.days < 10:
        ap.error("--days는 10 이상 (특별한 날이 3·5·10번째 날)")
    end = date.fromisoformat(args.end) if args.end else datetime.now(KST).date() - timedelta(days=1)
    db = repo_path(args.db)
    if db.exists():
        if not args.overwrite:
            ap.error(f"{db}가 이미 있음 (--overwrite로 새로 만들기)")
        for suffix in ("", "-wal", "-shm", ".bak"):
            Path(str(db) + suffix).unlink(missing_ok=True)
    try:
        cfg = load_config()
    except ConfigError as e:
        print(e)
        return 1
    out = build(db, cfg, end, args.days, args.seed)
    print(f"{db}: {out['start']} ~ {out['end']}, 요약 {len(out['summarized'])}일 (실패 {len(out['failed'])}일)")
    print(f"  기록 없음 {date.fromisoformat(out['start']) + timedelta(days=NO_RECORD_DAY)}, "
          f"자정 걸침 {date.fromisoformat(out['start']) + timedelta(days=MIDNIGHT_DAY)}, "
          f"하루 종일 자리 비움 {date.fromisoformat(out['start']) + timedelta(days=EMPTY_DAY)}")
    print(f"  서버: python -m hardware.server.mock_server --db {args.db}  (마지막 기록 이후 시각으로 동기화)")
    return 0 if not out["failed"] else 1


if __name__ == "__main__":
    sys.exit(main())
