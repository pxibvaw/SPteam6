"""리포트 계산 (홈·일간·주간·목표) — 읽기 전용 연결만 쓴다

날짜별 값은 day_values() 하나에서만 받는다 (값이 어긋나지 않게).
    지난 날   확인된 daily_summary (요약 경로)
    오늘 / 아직 요약 전인 날   summary.compute_day() 실시간 계산 (요약과 같은 함수)
그 위에서 정상 비율·대표 자세·주간 합계 같은 파생 값을 한 번만 계산한다.

규칙 (hardware/docs/db_design.md 7장)
    정상 비율 = 정상 ÷ (착석 − unknown), 분모 0이면 None. 총 착석은 unknown 포함
    자세 시간은 모두 겹치지 않는 값 (우선순위 seating.PRIORITY). 겹치는 원래 값은 일간 postures_raw에만
    "기록 있는 날" = 착석 > 0, "하루 평균" = 기록 있는 날만 평균
    한 주 = 월~일, 날짜는 한국 시간 'YYYY-MM-DD'
오늘 값은 1분마다 저장되는 기록 기준이라 최대 1분 늦다 (as_of = 마지막 저장 시각).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, timedelta
from pathlib import Path

from hardware.server.db.connection import connect
from hardware.server.db.summary import NUMERIC, POSTURES, compute_day, day_bounds
from hardware.server.seating import PostureStatus, classify, postures_of

WORST_HOUR_MIN_JUDGED_SEC = 600     # 이번 주 패턴: 판단 시간이 이보다 짧은 시간대는 '가장 나쁜 시간대' 후보에서 뺌
TREND_FLAT_MIN_PER_DAY = 1.0        # 목표 추세: 하루 변화가 이보다 작으면 flat

# 개선 목표 4종 (값은 모두 겹치지 않는 값, 초)
GOALS = {
    "cross": {"label": "다리 꼬기", "keys": ("cross_left_sec", "cross_right_sec"),
              "directions": {"left": "cross_left_sec", "right": "cross_right_sec"}},   # 왼다리/오른다리 위
    "forward_head": {"label": "목 앞으로 내미는 자세", "keys": ("forward_head_sec",), "directions": None},
    "lean": {"label": "한쪽으로 기대기", "keys": ("lean_left_sec", "lean_right_sec"),
             "directions": {"left": "lean_left_sec", "right": "lean_right_sec"}},
    "long_sitting": {"label": "장시간 연속 착석", "keys": ("max_continuous_sec",), "directions": None},
}


class ReportError(ValueError):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.status = code, status


def parse_day(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except (TypeError, ValueError):
        raise ReportError("INVALID_DATE", f"날짜 형식은 YYYY-MM-DD (지금: {text!r})") from None


def _ratio(a: float, b: float) -> float | None:
    return round(a / b, 4) if b > 0 else None


def _mean(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 3) if xs else None


class Reports:
    """요청마다 스레드별 읽기 전용 연결로 계산"""

    def __init__(self, db_path: str | Path, clock):
        self.db_path = Path(db_path)
        self.clock = clock
        self._local = threading.local()

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._local.conn = connect(self.db_path, readonly=True)
        return c

    # --- 공통 ---------------------------------------------------------------
    def now(self) -> float:
        t = self.clock.now()
        if t is None:
            raise ReportError("CLOCK_NOT_SYNCED", "시각 동기화 전이라 오늘 날짜를 모름. POST /time/sync를 먼저 보내세요",
                              status=409)
        return t

    def today(self, now: float) -> date:
        return self.clock.local(now).date()

    def check_day(self, d: date, today: date) -> None:
        if d > today:
            raise ReportError("INVALID_DATE", f"미래 날짜 ({d.isoformat()})")

    def as_of(self) -> float | None:
        r = self.conn.execute("SELECT value FROM meta WHERE key = 'last_seen'").fetchone()
        return float(r[0]) if r else None

    def day_values(self, d: date, today: date, now: float) -> dict:
        """그날 값 (요약 또는 실시간). has_data = 착석 > 0"""
        day = d.isoformat()
        v, source = None, "live"
        if d < today:
            r = self.conn.execute(
                "SELECT s.* FROM daily_summary s JOIN day_jobs j ON j.day = s.day "
                "WHERE s.day = ? AND j.summary_checked = 1 AND j.needs_recompute = 0", (day,)).fetchone()
            if r is not None:
                v, source = {k: r[k] for k in NUMERIC}, "summary"
        if v is None:
            v = compute_day(self.conn, day, now)
        v = dict(v)
        v["date"], v["source"] = day, source
        v["judged_sec"] = round(v["seated_sec"] - v["unknown_sec"], 3)
        v["has_data"] = v["seated_sec"] > 0
        return v

    def sessions_on(self, d: date) -> int:
        s, e = day_bounds(d.isoformat())
        return self.conn.execute("SELECT COUNT(*) FROM sessions WHERE started_at < ? AND COALESCE(ended_at, ?) > ?",
                                 (e, e, s)).fetchone()[0]

    @staticmethod
    def top_posture(v: dict) -> dict | None:
        """대표 자세: 겹치지 않는 자세별 누적 중 정상 제외 최대 (없으면 정상). 비율 = 그 시간 ÷ (착석 − unknown)"""
        if v["judged_sec"] <= 0:
            return None
        name, sec = max(((p, v[f"{p}_sec"]) for p in POSTURES), key=lambda x: x[1])
        if sec <= 0:
            name, sec = "normal", v["normal_sec"]
        return {"posture": name, "seconds": round(sec, 3), "ratio": _ratio(sec, v["judged_sec"])}

    @staticmethod
    def lean_balance(v: dict) -> dict:
        left, right = v["lean_left_sec"], v["lean_right_sec"]
        return {"left_sec": left, "right_sec": right, "right_ratio": _ratio(right, left + right)}

    @staticmethod
    def base_fields(v: dict) -> dict:
        return {k: v[k] for k in ("seated_sec", "normal_sec", "abnormal_sec", "unknown_sec", "judged_sec")} | {
            "normal_ratio": _ratio(v["normal_sec"], v["judged_sec"])}

    def selected_goals(self) -> list[str]:
        r = self.conn.execute("SELECT value FROM settings WHERE key = 'goals'").fetchone()
        return json.loads(r[0]) if r else []

    @staticmethod
    def goal_value(v: dict, goal: str) -> float:
        return round(sum(v[k] for k in GOALS[goal]["keys"]), 3)

    # --- 홈 -----------------------------------------------------------------
    def home(self, day: str | None = None) -> dict:
        now = self.now()
        today = self.today(now)
        d = parse_day(day) if day else today
        self.check_day(d, today)
        v = self.day_values(d, today, now)
        prev = self.day_values(d - timedelta(days=1), today, now)
        selected = self.selected_goals()
        goals = []
        for g in GOALS:
            a = self.goal_value(v, g) if v["has_data"] else None              # 기록 없는 날은 null (/reports/goals와 같게)
            b = self.goal_value(prev, g) if prev["has_data"] else None
            goals.append({"goal": g, "label": GOALS[g]["label"], "selected": g in selected, "today": a,
                          "yesterday": b, "change": round(a - b, 3) if a is not None and b is not None else None})
        return {"date": d.isoformat(), "source": v["source"], "has_data": v["has_data"], "as_of": self.as_of(),
                **self.base_fields(v), "daily_posture": self.top_posture(v), "lean_balance": self.lean_balance(v),
                "goals": goals}

    # --- 일간 -----------------------------------------------------------------
    def daily(self, day: str) -> dict:
        now = self.now()
        today = self.today(now)
        d = parse_day(day)
        self.check_day(d, today)
        v = self.day_values(d, today, now)
        top = self.top_posture(v)
        return {
            "date": d.isoformat(), "source": v["source"], "has_data": v["has_data"], "sessions": self.sessions_on(d),
            "as_of": self.as_of(), **self.base_fields(v),
            "max_continuous_sec": v["max_continuous_sec"],
            "collapse": {"avg_sec": v["collapse_avg_sec"], "count": v["collapse_n"], "segments": v["segment_count"]},
            "postures": {"normal": v["normal_sec"], **{p: v[f"{p}_sec"] for p in POSTURES}},
            "postures_raw": {p: v[f"raw_{p}_sec"] for p in POSTURES},
            "lean_balance": self.lean_balance(v),
            "distance": {"avg_mm": v["avg_distance_mm"], "closer_sec": v["closer_sec"]},
            "top_habit": top if top and top["posture"] != "normal" else None,
        }

    # --- 주간 -----------------------------------------------------------------
    def _week(self, monday: date, today: date, now: float) -> list[dict | None]:
        return [self.day_values(monday + timedelta(days=i), today, now) if monday + timedelta(days=i) <= today
                else None for i in range(7)]

    @staticmethod
    def _week_ratio(days: list[dict | None]) -> float | None:
        vs = [v for v in days if v]
        return _ratio(sum(v["normal_sec"] for v in vs), sum(v["judged_sec"] for v in vs))

    @staticmethod
    def _daily_avg(days: list[dict | None], fn) -> tuple[float | None, int]:
        vals = [fn(v) for v in days if v and v["has_data"]]
        return _mean(vals), len(vals)

    def _compare(self, this: list, last: list, fn) -> dict:
        a, na = self._daily_avg(this, fn)
        b, nb = self._daily_avg(last, fn)
        return {"this_week": a, "last_week": b, "change": round(a - b, 3) if a is not None and b is not None else None,
                "days_this": na, "days_last": nb}

    def _hourly(self, monday: date, until: date) -> list[dict]:
        hours = [{"hour": h, "seated_sec": 0.0, "judged_sec": 0.0, "abnormal_sec": 0.0} for h in range(24)]
        for hour, seat, head, tilt, sec in self.conn.execute(
                "SELECT hour, seat, head, tilt, SUM(seconds) FROM posture_hour WHERE day BETWEEN ? AND ? "
                "GROUP BY hour, seat, head, tilt", (monday.isoformat(), until.isoformat())):
            status, _ = classify(seat, head, tilt, postures_of(seat, head, tilt))
            h = hours[hour]
            h["seated_sec"] += sec
            if status is not PostureStatus.UNKNOWN:
                h["judged_sec"] += sec
            if status is PostureStatus.ABNORMAL:
                h["abnormal_sec"] += sec
        for h in hours:
            for k in ("seated_sec", "judged_sec", "abnormal_sec"):
                h[k] = round(h[k], 3)
            h["abnormal_ratio"] = _ratio(h["abnormal_sec"], h["judged_sec"])
        return hours

    def weekly(self, start: str) -> dict:
        now = self.now()
        today = self.today(now)
        req = parse_day(start)
        monday = req - timedelta(days=req.weekday())
        self.check_day(monday, today)
        this = self._week(monday, today, now)
        last = self._week(monday - timedelta(days=7), today, now)
        ratio, last_ratio = self._week_ratio(this), self._week_ratio(last)

        # 기울기 방향: 이번 주 왼·오른 합이 큰 쪽 → 그 방향의 하루 평균을 지난주 같은 방향과 비교
        tl = sum(v["tilt_left_sec"] for v in this if v)
        tr = sum(v["tilt_right_sec"] for v in this if v)
        direction = None if tl == tr else ("left" if tl > tr else "right")
        tilt = {"direction": direction, **(self._compare(this, last, lambda v: v[f"tilt_{direction}_sec"])
                                          if direction else {"this_week": None, "last_week": None, "change": None,
                                                             "days_this": 0, "days_last": 0})}
        dist = {}
        for name, m in (("this_week", monday), ("last_week", monday - timedelta(days=7))):
            dsum, dn = self.conn.execute(          # 그 주 거리 합 ÷ 개수 (시간대별 표는 영구 보관)
                "SELECT SUM(distance_sum_mm), SUM(distance_n) FROM hour_metrics WHERE day BETWEEN ? AND ?",
                (m.isoformat(), (m + timedelta(days=6)).isoformat())).fetchone()
            dist[name] = round(dsum / dn, 1) if dn else None
        dist["change"] = (round(dist["this_week"] - dist["last_week"], 1)
                          if dist["this_week"] is not None and dist["last_week"] is not None else None)

        hourly = self._hourly(monday, min(monday + timedelta(days=6), today))
        cands = [h for h in hourly if h["judged_sec"] >= WORST_HOUR_MIN_JUDGED_SEC and h["abnormal_ratio"] is not None]
        worst = max(cands, key=lambda h: h["abnormal_ratio"])["hour"] if cands else None
        return {
            "start": monday.isoformat(), "end": (monday + timedelta(days=6)).isoformat(),
            "requested_start": req.isoformat(), "as_of": self.as_of(),
            "normal_ratio": ratio,
            "last_week": {"start": (monday - timedelta(days=7)).isoformat(), "normal_ratio": last_ratio},
            "delta_pp": round((ratio - last_ratio) * 100, 2) if ratio is not None and last_ratio is not None else None,
            "days": [{"date": (monday + timedelta(days=i)).isoformat(), "weekday": i,
                      "has_data": bool(v and v["has_data"]), "source": v["source"] if v else None,
                      "seated_sec": v["seated_sec"] if v else None, "normal_sec": v["normal_sec"] if v else None,
                      "other_sec": round(v["seated_sec"] - v["normal_sec"], 3) if v else None}
                     for i, v in enumerate(this)],
            "compare": {
                "forward_head": self._compare(this, last, lambda v: v["forward_head_sec"]),
                "cross": self._compare(this, last, lambda v: v["cross_left_sec"] + v["cross_right_sec"]),
                "tilt_direction": tilt,
                "max_continuous": self._compare(this, last, lambda v: v["max_continuous_sec"]),
                "distance_mm": dist,
            },
            "hourly": hourly,
            "worst_hour": worst,
        }

    # --- 목표 -----------------------------------------------------------------
    def goals(self, day: str | None = None) -> dict:
        now = self.now()
        today = self.today(now)
        d = parse_day(day) if day else today
        self.check_day(d, today)
        last7 = [self.day_values(d - timedelta(days=6 - i), today, now) for i in range(7)]
        monday = d - timedelta(days=d.weekday())
        prev_week = self._week(monday - timedelta(days=7), today, now)
        selected = self.selected_goals()
        out = []
        for g, spec in GOALS.items():
            vals = [self.goal_value(v, g) if v["has_data"] else None for v in last7]
            today_v, yest_v = vals[6], vals[5]
            lw, lw_n = self._daily_avg(prev_week, lambda v, g=g: self.goal_value(v, g))
            pts = [(i, x / 60) for i, x in enumerate(vals) if x is not None]       # 분
            slope = None
            if len(pts) >= 2:
                mx = sum(p[0] for p in pts) / len(pts)
                my = sum(p[1] for p in pts) / len(pts)
                den = sum((p[0] - mx) ** 2 for p in pts)
                slope = round(sum((p[0] - mx) * (p[1] - my) for p in pts) / den, 3) if den else 0.0
            trend = None if slope is None else (
                "flat" if abs(slope) < TREND_FLAT_MIN_PER_DAY else ("decreasing" if slope < 0 else "increasing"))
            main = None
            if spec["directions"]:
                left = round(sum(v[spec["directions"]["left"]] for v in last7), 3)
                right = round(sum(v[spec["directions"]["right"]] for v in last7), 3)
                main = {"direction": None if left == right else ("left" if left > right else "right"),
                        "left_sec": left, "right_sec": right}
            out.append({
                "goal": g, "label": spec["label"], "selected": g in selected, "unit": "sec",
                "today": today_v, "yesterday": yest_v,
                "change": round(today_v - yest_v, 3) if today_v is not None and yest_v is not None else None,
                "last_week_avg": lw, "last_week_days": lw_n,
                "last7": [{"date": v["date"], "value": x, "has_data": v["has_data"]} for v, x in zip(last7, vals)],
                "trend": {"direction": trend, "slope_min_per_day": slope, "days": len(pts)},
                "main_direction": main,
            })
        return {"date": d.isoformat(), "as_of": self.as_of(), "goals": out}

    # --- 센서 배치 ---------------------------------------------------------------
    def layout(self, layout_id: int) -> dict:
        r = self.conn.execute("SELECT * FROM sensor_layouts WHERE id = ?", (layout_id,)).fetchone()
        return {"layout_id": r["id"], "n_channels": r["n_channels"], "channel_map": json.loads(r["channel_map"]),
                "positions": json.loads(r["positions"])}
