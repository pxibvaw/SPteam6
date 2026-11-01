"""하루 요약 계산 — posture_hour, hour_metrics, seating_segments에서만 (원본 raw_chunks에 의존하지 않음)

    seated_sec        그날 posture_hour 합 (unknown·5초 미만 자리 비움 포함)
    normal/abnormal/unknown_sec
                      자세 조합마다 착석 구간 로직(seating.classify)과 같은 규칙
    {자세}_sec        겹치지 않는 값: abnormal 조합의 초를 우선순위 첫 자세에만 (seating.PRIORITY)
    raw_{자세}_sec    겹치는 원래 값: 조합에 들어 있는 모든 자세에
    max_continuous_sec 착석 구간을 그날 [00:00, 다음 날 00:00)으로 잘라서 가장 긴 것 (열린 구간은 지금까지)
    collapse_*        그날 '시작한' 구간 중 첫 비정상 시간이 있는 것의 평균·개수
    segment_count     그날 시작한 구간 수
    avg_distance_mm, closer_sec  hour_metrics
정상 비율(정상 ÷ (착석 − unknown))은 저장하지 않고 읽기 API에서 계산한다.

계산식이나 우선순위를 바꾸면 CALC_VERSION을 올린다 → 서버가 켜질 때 지난 요약을 다시 계산할 날로 표시.
수동으로 다시 계산:
    python -m hardware.server.db.summary --db data/sitsense.db --recompute --day 2026-10-15
    python -m hardware.server.db.summary --db data/sitsense.db --recompute --all
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from datetime import datetime, timedelta

from hardware.server.clock import TIMEZONE, load_zone
from hardware.server.seating import PRIORITY, PostureStatus, classify, postures_of

log = logging.getLogger("sitsense.summary")

CALC_VERSION = 1
PRIORITY_TEXT = ">".join(PRIORITY)
POSTURES = ("forward_head", "cross_left", "cross_right", "lean_left", "lean_right", "tilt_left", "tilt_right")
NUMERIC = ("seated_sec", "normal_sec", "abnormal_sec", "unknown_sec",
           *(f"{p}_sec" for p in POSTURES), *(f"raw_{p}_sec" for p in POSTURES),
           "max_continuous_sec", "segment_count", "collapse_avg_sec", "collapse_n", "avg_distance_mm", "closer_sec")
COLUMNS = ("day", *NUMERIC, "calc_version", "priority", "created_at", "source")
TOL = 1e-6


def day_bounds(day: str) -> tuple[float, float]:
    """'YYYY-MM-DD' (한국 시간) → [그날 00:00, 다음 날 00:00) epoch 초"""
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=load_zone(TIMEZONE))
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def _r(v: float | None) -> float | None:
    return None if v is None else round(v, 3)


def compute_day(conn: sqlite3.Connection, day: str, now_ts: float) -> dict:
    """그날 요약 값 (저장하지 않음). now_ts: 아직 열린 구간을 어디까지로 볼지"""
    out = {k: 0.0 for k in NUMERIC}
    for r in conn.execute("SELECT seat, head, tilt, SUM(seconds) FROM posture_hour WHERE day = ? "
                          "GROUP BY seat, head, tilt", (day,)):
        seat, head, tilt, sec = r[0], r[1], r[2], r[3]
        ps = postures_of(seat, head, tilt)
        status, _ = classify(seat, head, tilt, ps)
        out["seated_sec"] += sec
        out[f"{status.value}_sec"] += sec
        if status is PostureStatus.ABNORMAL:
            out[f"{ps[0]}_sec"] += sec
            for p in ps:
                out[f"raw_{p}_sec"] += sec

    start, end = day_bounds(day)
    longest = 0.0
    for s, e in conn.execute("SELECT start_ts, end_ts FROM seating_segments "
                             "WHERE start_ts < ? AND (end_ts IS NULL OR end_ts > ?)", (end, start)):
        e = now_ts if e is None else e
        longest = max(longest, min(e, end) - max(s, start))
    out["max_continuous_sec"] = max(0.0, longest)

    n_seg, avg, n_col = conn.execute(
        "SELECT COUNT(*), AVG(first_abnormal_sec), COUNT(first_abnormal_sec) FROM seating_segments WHERE day = ?",
        (day,)).fetchone()
    out["segment_count"], out["collapse_avg_sec"], out["collapse_n"] = n_seg, avg, n_col

    dsum, dn, closer = conn.execute("SELECT SUM(distance_sum_mm), SUM(distance_n), SUM(closer_sec) "
                                    "FROM hour_metrics WHERE day = ?", (day,)).fetchone()
    out["avg_distance_mm"] = dsum / dn if dn else None
    out["closer_sec"] = closer or 0.0

    out = {k: (v if k in ("segment_count", "collapse_n") else _r(v)) for k, v in out.items()}
    out["segment_count"], out["collapse_n"] = int(out["segment_count"]), int(out["collapse_n"])
    return out


def check_values(v: dict) -> list[str]:
    """요약 값이 서로 맞는지 (틀린 이유 목록, 맞으면 [])"""
    bad = []
    if abs(v["normal_sec"] + v["abnormal_sec"] + v["unknown_sec"] - v["seated_sec"]) > 0.01:
        bad.append("정상 + 비정상 + unknown ≠ 착석")
    if abs(sum(v[f"{p}_sec"] for p in POSTURES) - v["abnormal_sec"]) > 0.01:
        bad.append("겹치지 않는 자세별 합 ≠ 비정상")
    for p in POSTURES:
        if v[f"raw_{p}_sec"] + 0.01 < v[f"{p}_sec"]:
            bad.append(f"raw_{p} < {p}")
    return bad


def write_summary(conn: sqlite3.Connection, day: str, values: dict, source: str, now_ts: float) -> None:
    """daily_summary를 덮어쓰고 day_jobs를 '요약함, 확인 전'으로. 한 트랜잭션"""
    row = {**values, "day": day, "calc_version": CALC_VERSION, "priority": PRIORITY_TEXT,
           "created_at": now_ts, "source": source}
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(f"INSERT OR REPLACE INTO daily_summary ({', '.join(COLUMNS)}) "
                     f"VALUES ({', '.join('?' * len(COLUMNS))})", [row[c] for c in COLUMNS])
        conn.execute("INSERT INTO day_jobs (day, summarized_at, summary_checked, needs_recompute, note) "
                     "VALUES (?, ?, 0, 0, ?) ON CONFLICT(day) DO UPDATE SET "
                     "summarized_at = COALESCE(day_jobs.summarized_at, excluded.summarized_at), "
                     "summary_checked = 0, needs_recompute = 0, note = excluded.note", (day, now_ts, source))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def verify_summary(conn: sqlite3.Connection, day: str, values: dict) -> list[str]:
    """저장된 요약을 다시 읽어 계산값과 같은지 + 서로 맞는지 (틀린 이유 목록)"""
    r = conn.execute("SELECT * FROM daily_summary WHERE day = ?", (day,)).fetchone()
    if r is None:
        return ["요약 줄이 없음"]
    bad = []
    for k in NUMERIC:
        a, b = r[k], values[k]
        if (a is None) != (b is None) or (a is not None and abs(a - b) > TOL):
            bad.append(f"{k}: 저장 {a} ≠ 계산 {b}")
    if r["calc_version"] != CALC_VERSION:
        bad.append("calc_version이 다름")
    return bad + check_values(dict(r))


def mark_checked(conn: sqlite3.Connection, day: str) -> None:
    conn.execute("UPDATE day_jobs SET summary_checked = 1 WHERE day = ?", (day,))


def summarize(conn: sqlite3.Connection, day: str, source: str, now_ts: float) -> list[str]:
    """계산 → 저장 → 확인. 확인되면 summary_checked=1. 틀린 이유 목록을 돌려준다 ([]면 성공)"""
    values = compute_day(conn, day, now_ts)
    write_summary(conn, day, values, source, now_ts)
    bad = verify_summary(conn, day, values)
    if bad:
        log.error("%s 요약 확인 실패: %s", day, bad)
    else:
        mark_checked(conn, day)
    return bad


def main() -> int:
    from hardware.server.db.connection import connect
    from hardware.server.db.migrate import migrate

    logging.basicConfig(level=logging.INFO, format="[SitSense summary] %(message)s")
    ap = argparse.ArgumentParser(description="하루 요약 다시 계산 (원본 없이 시간대별 표·착석 구간에서)")
    ap.add_argument("--db", required=True, help="SQLite 파일 (예: data/sitsense.db)")
    ap.add_argument("--recompute", action="store_true", help="다시 계산해서 저장")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--day", action="append", help="날짜 (YYYY-MM-DD, 여러 번 가능)")
    g.add_argument("--all", action="store_true", help="요약이 있는 모든 날")
    args = ap.parse_args()

    conn = connect(args.db)
    migrate(conn, args.db)
    days = args.day or [r[0] for r in conn.execute("SELECT day FROM daily_summary ORDER BY day")]
    last = conn.execute("SELECT value FROM meta WHERE key = 'last_seen'").fetchone()
    now_ts = float(last[0]) if last else time.time()        # 열린 구간은 마지막 기록까지로
    failed = 0
    for day in days:
        if args.recompute:
            bad = summarize(conn, day, "recompute", now_ts)
            failed += bool(bad)
            print(f"{day}: {'다시 계산 완료' if not bad else '확인 실패 ' + str(bad)}")
        else:
            print(day, compute_day(conn, day, now_ts))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
