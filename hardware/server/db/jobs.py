"""자정 작업 — 지난 날 요약 → 다시 읽어 확인 → 30분 뒤 그날 원본 삭제 (밀린 날짜 포함)

서버 실행 루프가 1분마다, 그리고 서버가 켜진 뒤 첫 시각 동기화 직후 run()을 부른다.
(동기화 전에는 오늘 날짜를 모르므로 돌리지 않는다.)

    1. 요약할 날이 있으면 모아 둔 기록을 먼저 쓴다 (recorder.flush) — 23:59대 1분치가 요약에서 빠지지 않게
    2. 오늘보다 이전이면서 기록이 있는 날 중 요약 확인 전 / 다시 계산할 날을 오래된 날부터
       요약(덮어쓰기) → 다시 읽어 확인 → summary_checked = 1
       확인이 안 되면 다음 실행 때 다시, 원본은 지우지 않음
    3. 확인 완료 + 첫 요약 후 30분이 지난 날의 raw_chunks 삭제 → raw_deleted_at

같은 날을 다시 돌려도 결과가 같다 (요약은 시간대별 표·구간에서만 계산해 덮어쓰고, 삭제는 한 번만 일어남).
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import timedelta

from hardware.server.clock import AppClock
from hardware.server.db.summary import CALC_VERSION, summarize

log = logging.getLogger("sitsense.jobs")

RUN_EVERY_SEC = 60.0
RAW_DELETE_DELAY_SEC = 30 * 60      # 요약 확인 후 이만큼 지나면 그날 원본 삭제
VACUUM_PAGES = 1000                 # 삭제 뒤 돌려줄 페이지 수 (한 번에 조금씩)

DATA_DAYS_SQL = """
    SELECT day FROM posture_hour UNION SELECT day FROM hour_metrics
    UNION SELECT day FROM seating_segments UNION SELECT day FROM raw_chunks
"""


class DayJobs:
    def __init__(self, conn: sqlite3.Connection, clock: AppClock, recorder=None):
        self.conn = conn
        self.clock = clock
        self.recorder = recorder
        self._last_run: float | None = None
        self.mark_outdated()

    def mark_outdated(self) -> int:
        """계산 방식(CALC_VERSION)이 바뀐 요약을 다시 계산할 날로 표시"""
        n = self.conn.execute(
            "UPDATE day_jobs SET needs_recompute = 1 WHERE day IN "
            "(SELECT day FROM daily_summary WHERE calc_version < ?)", (CALC_VERSION,)).rowcount
        if n:
            log.info("계산 방식이 바뀌어 %d일을 다시 계산할 예정", n)
        return n

    def maybe_run(self, now: float) -> dict | None:
        if self._last_run is not None and now - self._last_run < RUN_EVERY_SEC:
            return None
        return self.run(now)

    def _day(self, ts: float) -> str:
        return self.clock.local(ts).strftime("%Y-%m-%d")

    def pending_days(self, today: str) -> list[str]:
        rows = self.conn.execute(
            f"SELECT d.day FROM ({DATA_DAYS_SQL}) d LEFT JOIN day_jobs j ON j.day = d.day "
            "WHERE d.day < ? AND (j.day IS NULL OR j.summary_checked = 0 OR j.needs_recompute = 1) "
            "ORDER BY d.day", (today,)).fetchall()
        return [r[0] for r in rows]

    def run(self, now: float) -> dict:
        self._last_run = now
        report = {"summarized": [], "failed": [], "raw_deleted": []}
        today = self._day(now)
        yesterday = self._day(now - 86400)
        days = self.pending_days(today)
        if days and self.recorder is not None:      # 요약할 날이 있을 때만 모아 둔 기록을 먼저 씀
            self.recorder.flush(now)                # (매번 쓰면 1분 원본 덩어리가 잘게 나뉨)
            days = self.pending_days(today)
        for day in days:
            had_summary = self.conn.execute("SELECT 1 FROM daily_summary WHERE day = ?", (day,)).fetchone()
            source = "recompute" if had_summary else ("midnight" if day == yesterday else "catchup")
            bad = summarize(self.conn, day, source, now)
            (report["failed"] if bad else report["summarized"]).append(day)
            if not bad:
                log.info("%s 요약 완료 (%s)", day, source)
        due = self.conn.execute(
            "SELECT day FROM day_jobs WHERE summary_checked = 1 AND raw_deleted_at IS NULL "
            "AND summarized_at <= ? AND day < ? ORDER BY day", (now - RAW_DELETE_DELAY_SEC, today)).fetchall()
        for (day,) in due:
            self._delete_raw(day, now)
            report["raw_deleted"].append(day)
        return report

    def _delete_raw(self, day: str, now: float) -> None:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            n = self.conn.execute("DELETE FROM raw_chunks WHERE day = ?", (day,)).rowcount
            self.conn.execute("UPDATE day_jobs SET raw_deleted_at = ? WHERE day = ?", (now, day))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        try:
            self.conn.execute(f"PRAGMA incremental_vacuum({VACUUM_PAGES})")
        except sqlite3.Error:
            pass
        log.info("%s 원본 %d덩어리 삭제", day, n)


def days_between(clock: AppClock, t0: float, t1: float) -> list[str]:
    """[t0, t1]이 걸친 날짜들 (한국 시간)"""
    d0, d1 = clock.local(t0).date(), clock.local(max(t0, t1)).date()
    return [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]
