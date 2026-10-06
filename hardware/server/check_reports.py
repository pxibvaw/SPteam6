"""읽기 API(홈·일간·주간·목표) 확인 — 2주치 가짜 기록 + 가짜 시계

hardware/mock/fake_history.py로 임시 DB를 만들고 (2026-09-22 ~ 10-05, 기록 없음 09-24, 자정 걸침 09-26,
하루 종일 자리 비움 10-01), 2026-10-06 10:00으로 동기화한 서버에서 API 값을 DB로 직접 계산한 값과 비교한다.

사용 예 (저장소 최상위):
    python -m hardware.server.check_reports
"""
from __future__ import annotations

import logging
import shutil
import sqlite3
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from common.config import ConfigError, load_config
from hardware.mock.fake_history import build
from hardware.server.check_session import KST, Checker, FakeTime, code
from hardware.server.clock import AppClock
from hardware.server.db.reports import Reports
from hardware.server.db.summary import NUMERIC, POSTURES, compute_day
from hardware.server.mock_server import create_app

END = date(2026, 10, 5)
NOW = datetime(2026, 10, 6, 10, 0, tzinfo=KST).timestamp()


def close(a, b, tol=1e-3) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= tol


def main() -> int:
    logging.basicConfig(level=logging.ERROR)
    try:
        cfg = load_config()
    except ConfigError as e:
        print(e)
        return 1
    tmp = Path(tempfile.mkdtemp(prefix="sitsense_reports_"))
    check = Checker()
    try:
        db = tmp / "history.db"
        out = build(db, cfg, END, 14, seed=1)
        print(f"가짜 기록: {out['start']} ~ {out['end']}, 요약 {len(out['summarized'])}일")
        ft = FakeTime()
        clock = AppClock(monotonic=ft.mono, wall=ft.wall)
        app = create_app(cfg, "normal", loop=True, seed=1, db_path=db, clock=clock)
        c, rt = TestClient(app), app.state.runtime
        raw = sqlite3.connect(db)
        raw.row_factory = sqlite3.Row
        summ = {r["day"]: dict(r) for r in raw.execute("SELECT * FROM daily_summary")}

        print("\n1) 오류")
        r = c.get("/reports/home")
        check("동기화 전 → 409 CLOCK_NOT_SYNCED", code(r) == "409 CLOCK_NOT_SYNCED", code(r))
        c.post("/time/sync", json={"app_time": NOW, "timezone": "Asia/Seoul"})
        for path, params, want in (("/reports/daily", {"date": "2026/10/05"}, "422 INVALID_DATE"),
                                   ("/reports/daily", {"date": "2026-10-07"}, "422 INVALID_DATE"),
                                   ("/reports/weekly", {"start": "2026-10-13"}, "422 INVALID_DATE")):
            r = c.get(path, params=params)
            check(f"{path} {params} → {want}", code(r) == want, code(r))
        nodb = TestClient(create_app(cfg, "normal", clock=AppClock(monotonic=ft.mono, wall=ft.wall)))
        nodb.post("/time/sync", json={"app_time": NOW, "timezone": "Asia/Seoul"})
        r = nodb.get("/reports/home")
        check("DB 없이 켠 서버 → 503 NO_DB", code(r) == "503 NO_DB", code(r))
        lay = nodb.get("/layout").json()
        check("/layout은 DB 없이도 (config.yaml 배치, layout_id null)",
              lay["layout_id"] is None and lay["n_channels"] == cfg["pressure"]["n_channels"], lay["n_channels"])

        print("\n2) 요약 경로 = 실시간 경로 (같은 계산 함수)")
        rep = Reports(db, clock)
        today = date(2026, 10, 6)
        worst = 0.0
        for day in sorted(summ):
            v = rep.day_values(date.fromisoformat(day), today, NOW)
            live = compute_day(rep.conn, day, NOW)
            worst = max(worst, max(abs((v[k] or 0) - (live[k] or 0)) for k in NUMERIC))
            assert v["source"] == "summary"
        check(f"요약 {len(summ)}일 모두 실시간 계산과 같음", worst < 1e-6, f"최대 차이 {worst:.2e}")

        print("\n3) 일간")
        d = c.get("/reports/daily", params={"date": "2026-10-05"}).json()
        s = summ["2026-10-05"]
        judged = s["seated_sec"] - s["unknown_sec"]
        check("source summary, 정상 비율 = 정상 ÷ (착석 − unknown)",
              d["source"] == "summary" and close(d["normal_ratio"], round(s["normal_sec"] / judged, 4), 1e-4),
              d["normal_ratio"])
        check("자세별(정상 제외) 합 = 비정상", close(sum(v for k, v in d["postures"].items() if k != "normal"),
                                            d["abnormal_sec"], 0.01), d["abnormal_sec"])
        top = max(POSTURES, key=lambda p: s[f"{p}_sec"])
        check("가장 많이 나타난 습관 = 정상 제외 최대", d["top_habit"]["posture"] == top, d["top_habit"])
        lb = d["lean_balance"]
        check("좌우 편향: 오른쪽 비율 = 오른 ÷ (왼 + 오른)",
              close(lb["right_ratio"], round(lb["right_sec"] / (lb["left_sec"] + lb["right_sec"]), 4), 1e-4), lb)
        check("무너짐 = 그날 시작한 구간 first_abnormal 평균",
              close(d["collapse"]["avg_sec"], s["collapse_avg_sec"]) and d["collapse"]["count"] == s["collapse_n"],
              d["collapse"])
        for day, has, sessions, label in (("2026-09-24", False, 0, "기록 없음"), ("2026-10-01", False, 1, "하루 종일 자리 비움")):
            j = c.get("/reports/daily", params={"date": day}).json()
            check(f"{day} {label}: has_data={has}, 세션 {sessions}개", j["has_data"] == has and j["sessions"] == sessions
                  and j["normal_ratio"] is None, f"has_data={j['has_data']}, sessions={j['sessions']}")
        mid_start = raw.execute("SELECT MAX(start_ts) FROM seating_segments WHERE day = '2026-09-26'").fetchone()[0]
        h0 = raw.execute("SELECT SUM(seconds) FROM posture_hour WHERE day = '2026-09-27' AND hour = 0").fetchone()[0]
        check("자정 걸침: 09-26 23:30 시작 구간은 09-26 소속, 00시 40분은 09-27 0시로",
              datetime.fromtimestamp(mid_start, KST).strftime("%H:%M") == "23:30" and close(h0, 2400, 1),
              f"시작 {datetime.fromtimestamp(mid_start, KST):%H:%M}, 09-27 0시 {h0:.0f}초")

        print("\n4) 주간 (start=2026-10-01 목요일 → 09-28 월요일)")
        w = c.get("/reports/weekly", params={"start": "2026-10-01"}).json()
        this = [summ.get((date(2026, 9, 28) + timedelta(days=i)).isoformat()) for i in range(7)]
        last = [summ.get((date(2026, 9, 21) + timedelta(days=i)).isoformat()) for i in range(7)]

        def wratio(days):
            vs = [x for x in days if x]
            return round(sum(x["normal_sec"] for x in vs) / sum(x["seated_sec"] - x["unknown_sec"] for x in vs), 4)
        check("월요일로 맞춤", w["start"] == "2026-09-28" and w["requested_start"] == "2026-10-01", w["start"])
        check("주간 정상 비율 = 7일 정상 합 ÷ 7일 (착석 − unknown) 합", close(w["normal_ratio"], wratio(this), 1e-4),
              w["normal_ratio"])
        check("지난주 대비 %p", close(w["delta_pp"], round((wratio(this) - wratio(last)) * 100, 2), 0.02),
              f"{w['last_week']['normal_ratio']} → {w['normal_ratio']} ({w['delta_pp']:+}%p)")
        dd = w["days"]
        check("요일별 7개, 10-01은 기록 없음, 그 외 = 착석 − 정상",
              len(dd) == 7 and not dd[3]["has_data"] and all(
                  close(x["other_sec"], x["seated_sec"] - x["normal_sec"]) for x in dd if x["seated_sec"] is not None),
              [round((x["seated_sec"] or 0) / 3600, 1) for x in dd])
        fh = [x["forward_head_sec"] for x in this if x]
        cmp = w["compare"]
        check("거북목 하루 평균 = 기록 있는 날(6일)만", close(cmp["forward_head"]["this_week"], round(sum(fh) / len(fh), 3))
              and cmp["forward_head"]["days_this"] == 6 and cmp["forward_head"]["days_last"] == 5,
              f"{cmp['forward_head']['this_week']:.0f}초, {cmp['forward_head']['days_this']}일 / 지난주 "
              f"{cmp['forward_head']['days_last']}일")
        tl = sum(x["tilt_left_sec"] for x in this if x)
        tr = sum(x["tilt_right_sec"] for x in this if x)
        want_dir = "left" if tl > tr else "right"
        lw = [x[f"tilt_{want_dir}_sec"] for x in last if x]
        check("기울기 방향: 이번 주 큰 쪽의 하루 평균 vs 지난주 같은 방향",
              cmp["tilt_direction"]["direction"] == want_dir
              and close(cmp["tilt_direction"]["last_week"], round(sum(lw) / len(lw), 3)),
              f"{want_dir}: {cmp['tilt_direction']['this_week']:.0f} vs {cmp['tilt_direction']['last_week']:.0f}초")
        mc = [x["max_continuous_sec"] for x in this if x]
        check("최대 연속 착석 = 하루 최대값의 평균", close(cmp["max_continuous"]["this_week"], round(sum(mc) / len(mc), 3)),
              f"{cmp['max_continuous']['this_week'] / 60:.1f}분")
        dsum, dn = raw.execute("SELECT SUM(distance_sum_mm), SUM(distance_n) FROM hour_metrics "
                               "WHERE day BETWEEN '2026-09-28' AND '2026-10-04'").fetchone()
        check("화면 거리 = 그 주 거리 합 ÷ 개수", close(cmp["distance_mm"]["this_week"], round(dsum / dn, 1), 0.05),
              cmp["distance_mm"])
        hourly = w["hourly"]
        check("시간대별 24개, 착석 합 = 7일 착석 합", len(hourly) == 24 and close(
            sum(h["seated_sec"] for h in hourly), sum(x["seated_sec"] for x in this if x), 0.05),
              f"{sum(h['seated_sec'] for h in hourly) / 3600:.1f}시간")
        cands = [h for h in hourly if h["judged_sec"] >= 600]
        check("worst_hour = 판단 10분 이상 중 비정상 비율 최대", w["worst_hour"] == max(
            cands, key=lambda h: h["abnormal_ratio"])["hour"], f"{w['worst_hour']}시")
        cw = c.get("/reports/weekly", params={"start": "2026-10-06"}).json()
        check("이번 주(진행 중): 월 기록, 화 오늘(실시간), 수~일 미래 null",
              cw["days"][0]["has_data"] and cw["days"][1]["source"] == "live"
              and all(x["seated_sec"] is None for x in cw["days"][2:]), [x["source"] for x in cw["days"]])

        print("\n5) 목표 4종")
        g = c.get("/reports/goals").json()["goals"]
        check("4종, 기본 설정이라 selected 모두 false",
              [x["goal"] for x in g] == ["cross", "forward_head", "lean", "long_sitting"]
              and not any(x["selected"] for x in g), [x["goal"] for x in g])
        raw.execute("UPDATE settings SET value = '[\"cross\", \"lean\"]', updated_at = 1 WHERE key = 'goals'")
        raw.commit()
        g = c.get("/reports/goals").json()["goals"]
        h = c.get("/reports/home").json()["goals"]
        check("설정 goals=[cross, lean] → selected 표시 (홈도 같게)",
              [x["selected"] for x in g] == [True, False, True, False] == [x["selected"] for x in h],
              [x["goal"] for x in g if x["selected"]])
        cross = g[0]
        vals = [(i, x["value"] / 60) for i, x in enumerate(cross["last7"]) if x["value"] is not None]
        mx, my = sum(p[0] for p in vals) / len(vals), sum(p[1] for p in vals) / len(vals)
        slope = sum((p[0] - mx) * (p[1] - my) for p in vals) / sum((p[0] - mx) ** 2 for p in vals)
        want = "flat" if abs(slope) < 1 else ("decreasing" if slope < 0 else "increasing")
        check("추세: 최근 7일 직선 기울기(분/일) + decreasing/increasing/flat",
              close(cross["trend"]["slope_min_per_day"], round(slope, 3)) and cross["trend"]["direction"] == want,
              cross["trend"])
        check("최근 7일 = 09-30 ~ 10-06, 기록 없는 날 null", [x["date"] for x in cross["last7"]][0] == "2026-09-30"
              and cross["last7"][1]["value"] is None and cross["last7"][6]["value"] is None,
              [x["value"] is not None for x in cross["last7"]])
        check("주된 방향: 다리 꼬기·기대기는 오른쪽 (가짜 기록이 오른쪽 많음)",
              g[0]["main_direction"]["direction"] == "right" and g[2]["main_direction"]["direction"] == "right"
              and g[1]["main_direction"] is None, [x["main_direction"] and x["main_direction"]["direction"] for x in g])
        lw_days = [x for x in this if x]
        check("지난주 평균 = 지난 달력주(09-28 ~ 10-04) 기록 있는 날", close(
            g[3]["last_week_avg"], round(sum(x["max_continuous_sec"] for x in lw_days) / len(lw_days), 3))
              and g[3]["last_week_days"] == 6, f"{g[3]['last_week_avg'] / 60:.1f}분, {g[3]['last_week_days']}일")
        check("장시간 연속 착석 = 하루 최대 연속 착석 (10-05)",
              close(g[3]["last7"][5]["value"], summ["2026-10-05"]["max_continuous_sec"]), g[3]["last7"][5]["value"])

        print("\n6) 오늘 실시간 (세션 75초 → 1분 저장 뒤 반영)")
        h0 = c.get("/reports/home").json()
        c.post("/session/start")
        t0 = ft.m
        while ft.m - t0 < 75:
            ft.advance(0.1)
            rt.tick()
        h1 = c.get("/reports/home").json()
        check("오늘: 기록 없음 → 착석 생김 (source live), as_of 갱신",
              not h0["has_data"] and h1["has_data"] and h1["source"] == "live" and h1["as_of"] > (h0["as_of"] or 0),
              f"착석 {h1['seated_sec']:.1f}초, as_of {datetime.fromtimestamp(h1['as_of'], KST):%H:%M:%S}")
        check("오늘 값 = 마지막 저장까지 (최대 1분 늦음)", h1["seated_sec"] <= 75 and h1["seated_sec"] >= 50,
              h1["seated_sec"])
        dp = h1["daily_posture"]
        check("하루 평균 자세 + 비율 (비정상 없으면 normal)", dp is not None and dp["posture"] in ("normal", *POSTURES)
              and 0 <= dp["ratio"] <= 1, dp)
        c.post("/session/stop")
        lay = c.get("/layout").json()
        check("/layout: layout_id·채널 순서·위치", lay["layout_id"] == 1 and len(lay["positions"]) == lay["n_channels"],
              f"id {lay['layout_id']}, {lay['n_channels']}채널")
        raw.close()
    finally:
        logging.disable(logging.CRITICAL)
        shutil.rmtree(tmp, ignore_errors=True)

    bad = [r for r in check.rows if not r[0]]
    print(f"\n전체 {len(check.rows)}개 중 통과 {len(check.rows) - len(bad)}개")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
