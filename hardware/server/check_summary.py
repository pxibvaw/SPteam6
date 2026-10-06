"""자정 요약 + 원본 삭제 확인 — 가짜 시계로 날짜를 넘기며 DB를 본다

1 자정 넘기기  2 삭제 30분 전후  3 요약 직후 정전  4 3일 건너뛰기(catch-up)
5 두 번 실행(멱등)  6 계산 버전 바뀜(recompute)  7 늦게 닫히는 구간  8 마이그레이션 002

사용 예 (저장소 최상위):
    python -m hardware.server.check_summary
"""
from __future__ import annotations

import logging
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from common.config import ConfigError, load_config
from hardware.server.check_db import Env
from hardware.server.check_session import KST, Checker, FakeTime
from hardware.server.db import jobs as jobs_module
from hardware.server.db.connection import connect
from hardware.server.db.migrate import MIGRATIONS_DIR, migrate
from hardware.server.db.summary import NUMERIC, check_values, compute_day, summarize, write_summary

D, D1 = "2026-10-15", "2026-10-16"


def at(day: int, h: int, m: int = 0, s: float = 0) -> float:
    return datetime(2026, 10, day, h, m, 0, tzinfo=KST).timestamp() + s


def clock_str(ts: float) -> str:
    return datetime.fromtimestamp(ts, KST).strftime("%m-%d %H:%M:%S")


def run_idle(env: Env, until_ts: float, step: float = 5.0) -> None:
    """세션 없이 시간만 보냄 (자정 작업은 계속 돈다)"""
    while env.rt.clock.now() < until_ts:
        env.ft.advance(min(step, until_ts - env.rt.clock.now() + 1e-6))
        env.rt.tick()


def summary(env: Env, day: str) -> dict | None:
    r = env.db.execute("SELECT * FROM daily_summary WHERE day = ?", (day,)).fetchone()
    return dict(r) if r else None


def numbers(row: dict) -> dict:
    return {k: row[k] for k in NUMERIC}


def job(env: Env, day: str) -> dict | None:
    r = env.db.execute("SELECT * FROM day_jobs WHERE day = ?", (day,)).fetchone()
    return dict(r) if r else None


def raw_count(env: Env, day: str) -> int:
    return env.one("SELECT COUNT(*) FROM raw_chunks WHERE day = ?", day)


def new_env(cfg: dict, db: Path, start_ts: float, scenario: str, ft: FakeTime | None = None) -> Env:
    """서버를 켜고 start_ts(앱 시각)로 동기화"""
    ft = ft or FakeTime()
    env = Env(cfg, ft, db, scenario)
    r = env.sync(start_ts)
    assert r.status_code == 200, r.json()
    return env


def main() -> int:
    logging.basicConfig(level=logging.ERROR)
    try:
        cfg = load_config()
    except ConfigError as e:
        print(e)
        return 1
    tmp = Path(tempfile.mkdtemp(prefix="sitsense_summary_"))
    check = Checker()
    try:
        # ------------------------------------------------------------------
        print("1) 자정 넘기기 (demo: 23:58:00 시작, 23:59:00 거북목, 00:00:05 다리 꼬기, 00:03 정지)")
        db = tmp / "a.db"
        env = new_env(cfg, db, at(15, 23, 58), "demo")
        env.c.post("/session/start")
        env.run_to(300)
        env.c.post("/session/stop")
        j = job(env, D)
        check("10-15 요약 + 확인 (midnight)", j and j["summary_checked"] == 1 and j["note"] in ("midnight", "recompute"),
              f"요약 {clock_str(j['summarized_at'])}, note {j['note']}")
        check("요약 시각 = 자정 직후 1분 안", at(16, 0) <= j["summarized_at"] < at(16, 0, 1, 1),
              clock_str(j["summarized_at"]))
        s = summary(env, D)
        check("값끼리 맞음 (정상+비정상+unknown=착석, 겹치지 않는 합=비정상, 원래 값 ≥)", check_values(s) == [],
              check_values(s) or "ok")
        check("착석 ≈ 23:58:00.1 ~ 24:00 (119.9초)", abs(s["seated_sec"] - 119.9) <= 0.2, s["seated_sec"])
        check("최대 연속 착석: 자정에서 자름", abs(s["max_continuous_sec"] - 119.9) <= 0.2, s["max_continuous_sec"])
        check("거북목 25초 (23:59:03 확정 ~ 23:59:28 해제), 다리 꼬기는 다음 날",
              abs(s["forward_head_sec"] - 25) <= 0.5 and s["cross_left_sec"] == 0,
              f"거북목 {s['forward_head_sec']}, 다리 꼬기 {s['cross_left_sec']}")
        check("무너짐: 시작한 날에 1개 (63초)", s["collapse_n"] == 1 and abs(s["collapse_avg_sec"] - 63) <= 0.2,
              f"{s['collapse_n']}개, {s['collapse_avg_sec']}초")
        now = env.rt.clock.now()
        both = [compute_day(env.db, d, now) for d in (D, D1)]
        segs = env.q("SELECT SUM(normal_sec), SUM(abnormal_sec), SUM(unknown_sec) FROM seating_segments")[0]
        diff = max(abs(sum(b[k] for b in both) - segs[i])
                   for i, k in enumerate(("normal_sec", "abnormal_sec", "unknown_sec")))
        check("이틀 합 = 착석 구간 값 합 (정상·비정상·unknown)", diff <= 0.3, f"최대 차이 {diff:.2f}초")
        run_idle(env, at(16, 0, 5))
        j2, s2 = job(env, D), summary(env, D)
        check("정지로 구간이 닫힘 → 다시 계산(recompute), 값은 그대로",
              j2["needs_recompute"] == 0 and s2["source"] == "recompute" and numbers(s2) == numbers(s),
              f"source {s2['source']}")

        # ------------------------------------------------------------------
        print("\n2) 원본 삭제 30분 전후")
        before = numbers(summary(env, D))
        run_idle(env, at(16, 0, 29))
        check("00:29: 10-15 원본 남음", raw_count(env, D) > 0 and job(env, D)["raw_deleted_at"] is None,
              f"{raw_count(env, D)}덩어리")
        run_idle(env, at(16, 0, 31))
        j = job(env, D)
        check("00:31: 10-15 원본 삭제 + 삭제 시각 기록", raw_count(env, D) == 0 and j["raw_deleted_at"] is not None,
              clock_str(j["raw_deleted_at"]))
        check("오늘(10-16) 원본은 남음", raw_count(env, D1) > 0, f"{raw_count(env, D1)}덩어리")
        bad = summarize(env.db, D, "recompute", env.rt.clock.now())
        check("원본 삭제 뒤 다시 계산해도 값이 같음 (원본에 의존 안 함)", not bad and numbers(summary(env, D)) == before,
              "같음" if numbers(summary(env, D)) == before else "다름")

        # ------------------------------------------------------------------
        print("\n5) 두 번 실행 (멱등)")
        a1 = numbers(summary(env, D))
        summarize(env.db, D, "recompute", env.rt.clock.now())
        summarize(env.db, D, "recompute", env.rt.clock.now())
        check("같은 날 요약 2번 → 값 같음", numbers(summary(env, D)) == a1, "같음")
        r = env.rt.jobs.run(env.rt.clock.now())
        r2 = env.rt.jobs.run(env.rt.clock.now())
        check("자정 작업 2번 더 → 할 일 없음, 오류 없음", r == r2 == {"summarized": [], "failed": [], "raw_deleted": []},
              r2)

        # ------------------------------------------------------------------
        print("\n6) 계산 버전이 바뀜 → 서버를 켤 때 다시 계산")
        env.db.execute("UPDATE daily_summary SET calc_version = 0 WHERE day = ?", (D,))
        env6 = new_env(cfg, db, env.rt.clock.now() + 1, "demo",
                       ft=FakeTime())
        s6 = summary(env6, D)
        check("calc_version 1, source recompute, 값 같음",
              s6["calc_version"] == 1 and s6["source"] == "recompute" and numbers(s6) == a1,
              f"calc_version {s6['calc_version']}, {s6['source']}")

        # ------------------------------------------------------------------
        print("\n7) 늦게 닫히는 구간 (23:59:45 시작, 첫 비정상 00:00:08, 00:00:45 정지)")
        env7 = new_env(cfg, tmp / "b.db", at(15, 23, 59, 45), "forward_head")
        env7.c.post("/session/start")
        env7.run_to(16)                                     # 00:00:01
        env7.rt.jobs.run(env7.rt.clock.now())               # 자정 직후의 1분 작업 (첫 비정상 00:00:08보다 먼저)
        s = summary(env7, D)
        check("00:00:01 요약 때는 무너짐 없음 (첫 비정상이 아직)", s and s["collapse_n"] == 0, s and s["collapse_n"])
        env7.run_to(60)
        env7.c.post("/session/stop")
        check("정지로 구간이 닫힘 → 10-15 needs_recompute", job(env7, D)["needs_recompute"] == 1, 1)
        run_idle(env7, env7.rt.clock.now() + 61)
        s = summary(env7, D)
        check("다시 계산 → 시작한 날(10-15) 무너짐 1개 (≈23초)",
              s["collapse_n"] == 1 and abs(s["collapse_avg_sec"] - 23) <= 0.2 and s["source"] == "recompute",
              f"{s['collapse_n']}개, {s['collapse_avg_sec']}초, {s['source']}")
        check("최대 연속 착석은 자정에서 자름 (≈15초)", abs(s["max_continuous_sec"] - 15) <= 0.2, s["max_continuous_sec"])

        # ------------------------------------------------------------------
        print("\n3) 요약 직후 정전 (저장은 됐는데 확인 전에 꺼짐)")
        db3 = tmp / "c.db"
        env3 = new_env(cfg, db3, at(15, 23, 59), "normal")
        env3.c.post("/session/start")
        env3.run_to(30)
        env3.c.post("/session/stop")
        real = jobs_module.summarize

        def crash(conn, day, source, now_ts):
            write_summary(conn, day, compute_day(conn, day, now_ts), source, now_ts)
            raise RuntimeError("정전 흉내")
        jobs_module.summarize = crash
        try:
            while env3.rt.clock.now() < at(16, 0, 0, 30):
                env3.ft.advance(1)
                try:
                    env3.rt.tick()
                except RuntimeError:
                    break
        finally:
            jobs_module.summarize = real
        j = job(env3, D)
        check("요약은 저장됨, 확인 전 (summary_checked=0), 원본 남음",
              j and j["summary_checked"] == 0 and raw_count(env3, D) > 0, f"checked={j and j['summary_checked']}")
        first_at = j["summarized_at"]
        ft3 = FakeTime()
        env3b = new_env(cfg, db3, env3.rt.clock.now() + 120, "normal", ft=ft3)       # 2분 뒤 다시 켬
        j = job(env3b, D)
        check("다시 켜고 동기화 → 다시 요약·확인", j["summary_checked"] == 1 and check_values(summary(env3b, D)) == [],
              f"checked={j['summary_checked']}")
        check("원본 삭제 기준은 첫 요약 시각 그대로", j["summarized_at"] == first_at, clock_str(j["summarized_at"]))
        run_idle(env3b, first_at + 29 * 60)
        check("첫 요약 + 29분: 원본 남음", raw_count(env3b, D) > 0, raw_count(env3b, D))
        run_idle(env3b, first_at + 31 * 60)
        check("첫 요약 + 31분: 원본 삭제", raw_count(env3b, D) == 0, raw_count(env3b, D))

        # ------------------------------------------------------------------
        print("\n4) 3일 건너뛰기: 10-15·10-16 기록 → 10-19 10:00에 켬")
        db4 = tmp / "d.db"
        ft4 = FakeTime()
        env4 = new_env(cfg, db4, at(15, 10), "normal", ft=ft4)
        env4.rt.jobs = None                         # 자정 작업 없이 기록만 된 이틀 (catch-up 대상 만들기)
        for _ in range(2):
            env4.c.post("/session/start")
            env4.run_for(60)
            env4.c.post("/session/stop")
            ft4.advance(86400 - 60)                 # 다음 날 같은 시각 (꺼져 있음 → tick 없음)
        ft4.advance(2 * 86400)                      # 10-19 10:00 근처
        env4b = new_env(cfg, db4, env4.rt.clock.now(), "normal", ft=FakeTime())
        rows = env4b.q("SELECT day, note, summary_checked FROM day_jobs ORDER BY summarized_at, day")
        check("오래된 날부터 catch-up (10-15 → 10-16), 확인 완료",
              [tuple(r) for r in rows] == [(D, "catchup", 1), (D1, "catchup", 1)], [tuple(r) for r in rows])
        check("기록 없는 날(10-17·10-18)은 요약 없음",
              env4b.one("SELECT COUNT(*) FROM daily_summary WHERE day IN ('2026-10-17', '2026-10-18')") == 0, 0)
        check("각 날 착석 ≈ 60초", all(abs(summary(env4b, d)["seated_sec"] - 59.9) <= 0.2 for d in (D, D1)),
              [summary(env4b, d)["seated_sec"] for d in (D, D1)])
        check("원본은 아직 남음 (확인 후 30분 전)", raw_count(env4b, D) > 0 and raw_count(env4b, D1) > 0,
              [raw_count(env4b, d) for d in (D, D1)])
        run_idle(env4b, env4b.rt.clock.now() + 31 * 60)
        check("30분 뒤 두 날 원본 삭제", raw_count(env4b, D) == 0 and raw_count(env4b, D1) == 0,
              [raw_count(env4b, d) for d in (D, D1)])

        # ------------------------------------------------------------------
        print("\n8) 마이그레이션 002 (버전 1 DB에 적용)")
        db8 = tmp / "v1.db"
        c = connect(db8)
        c.executescript("BEGIN;\n" + (MIGRATIONS_DIR / "001_init.sql").read_text(encoding="utf-8") +
                        "\nINSERT INTO schema_migrations VALUES (1, 0, 'init');\nPRAGMA user_version = 1;\n"
                        "INSERT INTO day_jobs (day, summarized_at, summary_checked) VALUES ('2026-10-01', 1, 1);\n"
                        "COMMIT;")
        applied = migrate(c, db8)
        cols = [r[1] for r in c.execute("PRAGMA table_info(day_jobs)")]
        row = c.execute("SELECT summary_checked, needs_recompute FROM day_jobs").fetchone()
        check("002 적용, 버전 2, .bak 백업", applied == [2] and c.execute("PRAGMA user_version").fetchone()[0] == 2
              and db8.with_suffix(".db.bak").exists(), applied)
        check("기존 줄 그대로 + needs_recompute 기본값 0", "needs_recompute" in cols and tuple(row) == (1, 0),
              tuple(row))
        c.close()
    finally:
        logging.disable(logging.CRITICAL)
        shutil.rmtree(tmp, ignore_errors=True)

    bad = [r for r in check.rows if not r[0]]
    print(f"\n전체 {len(check.rows)}개 중 통과 {len(check.rows) - len(bad)}개")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
