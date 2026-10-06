"""SQLite 저장 확인 — 가짜 시계로 서버를 돌리며 DB에 맞게 쓰였는지 본다

임시 폴더에 DB를 만들고, 자정을 넘기는 세션(자리 비움·일시정지 포함)을 돌린 뒤
테이블 값을 착석 구간 로직(메모리) 결과와 비교한다. 기준 자세 측정·실패, 비정상 종료 복구,
마지막 기록보다 이전 시각 거절, 채널 수 변경(8 → 6), 기록 초기화, 쓰기 횟수, 실제 용량도 본다.

사용 예 (저장소 최상위):
    python -m hardware.server.check_db
"""
from __future__ import annotations

import json
import logging
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from common.config import ConfigError, load_config
from hardware.mock.scenarios import Step
from hardware.server.check_seating import with_channels
from hardware.server.check_session import KST, Checker, FakeTime, code
from hardware.server.clock import AppClock
from hardware.server.db.migrate import migrate
from hardware.server.db.store import unpack_chunk
from hardware.server.mock_server import create_app

APP_START = datetime(2026, 10, 15, 23, 58, 30, tzinfo=KST).timestamp()   # 90초 뒤 자정


class Env:
    """가짜 시계 + 서버 하나"""

    def __init__(self, cfg: dict, ft: FakeTime, db: Path, scenario: str = "empty_6s"):
        self.ft = ft
        self.app = create_app(cfg, scenario, loop=True, seed=1, db_path=db,
                              clock=AppClock(monotonic=ft.mono, wall=ft.wall))
        self.c = TestClient(self.app)
        self.rt = self.app.state.runtime
        self.db = self.rt.recorder.conn
        self.t0 = ft.m

    def T(self) -> float:
        return round(self.ft.m - self.t0, 1)

    def run_to(self, t: float) -> None:
        while self.T() < t:
            self.ft.advance(0.1)
            self.rt.tick()

    def run_for(self, sec: float) -> None:
        self.run_to(self.T() + sec)

    def sync(self, app_time: float):
        r = self.c.post("/time/sync", json={"app_time": app_time, "timezone": "Asia/Seoul"})
        self.t0 = self.ft.m
        return r

    def q(self, sql: str, *args):
        return self.db.execute(sql, args).fetchall()

    def one(self, sql: str, *args):
        return self.db.execute(sql, args).fetchone()[0]

    def baseline(self) -> dict:
        return self.c.get("/session").json()["baseline"]

    def fail_next_measurement(self) -> None:
        """다음 기준 측정 10초 동안 카메라가 사람을 못 찾게 (mock)"""
        rt = self.rt

        def on_start(ts, seconds):
            rt.player.hold(Step(seconds, camera_lost=True), ts + seconds)
            rt.engine.on_measure_start = rt._on_measure_start
        rt.engine.on_measure_start = on_start


def main() -> int:
    logging.basicConfig(level=logging.ERROR)
    try:
        cfg = load_config()
    except ConfigError as e:
        print(e)
        return 1
    tmp = Path(tempfile.mkdtemp(prefix="sitsense_db_"))
    db = tmp / "sitsense.db"
    check = Checker()
    ft = FakeTime()
    try:
        print("1) 마이그레이션")
        env = Env(cfg, ft, db)
        tables = [r[0] for r in env.q("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        check("테이블 13개", len(tables) == 13, len(tables))
        check("user_version 1, WAL, auto_vacuum=incremental",
              env.one("PRAGMA user_version") == 1 and env.one("PRAGMA journal_mode") == "wal"
              and env.one("PRAGMA auto_vacuum") == 2, f"{env.one('PRAGMA user_version')}, "
              f"{env.one('PRAGMA journal_mode')}, {env.one('PRAGMA auto_vacuum')}")
        check("설정 기본값 7개", env.one("SELECT COUNT(*) FROM settings") == 7,
              dict(env.q("SELECT key, value FROM settings")))
        check("다시 실행 → 적용 없음, 백업 없음", migrate(env.db, db) == [] and not db.with_suffix(".db.bak").exists(),
              "[]")
        check("센서 배치 1개 (8채널, channel_map 0~7)",
              env.q("SELECT n_channels, channel_map FROM sensor_layouts")[0][:] == (8, "[0,1,2,3,4,5,6,7]"),
              tuple(env.q("SELECT n_channels, channel_map FROM sensor_layouts")[0]))

        print("\n2) 첫 세션: 기준 자세 (23:58:30 시작 → 자정 넘김)")
        env.sync(APP_START)
        r = env.c.post("/session/start")
        check("시작 → 앉기를 기다림 (initial)", r.json()["baseline"]["state"] == "waiting_seat"
              and r.json()["baseline"]["kind"] == "initial", r.json()["baseline"]["state"])
        env.run_to(1.5)
        b = env.baseline()
        check("1.5초: 앉음 확인 → 측정 중", b["state"] == "measuring", f"{b['state']}, 남은 {b['remaining_sec']}초")
        env.run_to(12)
        b = env.baseline()
        check("12초: 측정 완료 (ready, initial)", b["state"] == "ready" and b["kind"] == "initial", b["state"])
        rows = env.q("SELECT kind, status, is_current FROM baselines")
        check("baselines: initial 1줄, 현재 기준", [tuple(x) for x in rows] == [("initial", "ok", 1)],
              [tuple(x) for x in rows])

        print("\n3) 자리 비움 6초 · 일시정지 40~50초 · 자정 · 정지 200초")
        env.run_to(40)
        env.c.post("/session/pause")
        env.run_to(50)
        env.c.post("/session/resume")
        env.run_to(55)
        check("재개 → 기준 다시 안 잼", env.baseline()["state"] == "ready"
              and env.one("SELECT COUNT(*) FROM baselines") == 1, env.baseline()["state"])
        tx0, t_tx0 = env.rt.recorder.tx_count, env.T()
        env.run_to(200)
        tx1 = env.rt.recorder.tx_count
        env.c.post("/session/stop")
        tracker = env.rt.engine.seating
        mem = [(round(s.start, 1), round(s.end, 1), s.end_reason, round(s.normal_sec, 1), round(s.abnormal_sec, 1),
                round(s.unknown_sec, 1)) for s in tracker.segments]
        dbs = [(round(a, 1), round(b_, 1), c_, round(n, 1), round(ab, 1), round(u, 1)) for a, b_, c_, n, ab, u in
               env.q("SELECT start_ts, end_ts, end_reason, normal_sec, abnormal_sec, unknown_sec "
                     "FROM seating_segments ORDER BY start_ts")]
        check("구간 = 메모리 결과 (시작·끝·사유·정상/비정상/unknown)", mem == dbs,
              [(round(x[0] - APP_START, 1), round(x[1] - APP_START, 1), x[2]) for x in dbs])
        events = [r_[0] for r_ in env.q("SELECT kind FROM session_events ORDER BY ts, id")]
        check("이벤트 start → pause → resume → stop", events == ["start", "pause", "resume", "stop"], events)
        seg_total = env.one("SELECT SUM(end_ts - start_ts) FROM seating_segments")
        ph_total = env.one("SELECT SUM(seconds) FROM posture_hour")
        check("posture_hour 합 = 구간 길이 합 (±0.2초)", abs(ph_total - seg_total) <= 0.2,
              f"{ph_total:.1f} / {seg_total:.1f}")
        hm_total = env.one("SELECT SUM(seated_sec) FROM hour_metrics")
        check("hour_metrics 착석 합 = posture_hour 합", abs(hm_total - ph_total) < 1e-6, f"{hm_total:.1f}")
        hours = [tuple(x) for x in env.q("SELECT day, hour, ROUND(SUM(seconds), 1) FROM posture_hour "
                                         "GROUP BY day, hour ORDER BY day, hour")]
        check("자정에서 나뉨 (10-15 23시 / 10-16 0시)", [h[:2] for h in hours] == [("2026-10-15", 23),
                                                                             ("2026-10-16", 0)], hours)
        away = env.one("SELECT COALESCE(SUM(seconds), 0) FROM posture_hour WHERE seat = 'away'")
        # 6초 비움 3번은 구간 밖(끝 = 첫 빈 샘플)이라 각 0.1초만, 198초부터 비우다 200초에 정지한 2.1초는 구간 안
        check("away = 정지 직전 비움 2.1초 + 6초 비움 시작 순간 0.1초 × 3", abs(away - 2.4) <= 0.05,
              f"away {away:.1f}초")
        dist = env.q("SELECT SUM(distance_n), SUM(distance_sum_mm) / SUM(distance_n) FROM hour_metrics")[0]
        check("거리 평균 (기준 약 600mm)", 550 < dist[1] < 650, f"{dist[1]:.0f}mm ({dist[0]}개)")
        n_raw = env.one("SELECT SUM(n_samples) FROM raw_chunks")
        chunks = env.q("SELECT day, n_samples, has_pose, LENGTH(data) FROM raw_chunks ORDER BY start_ts")
        check("원본 덩어리: 1분·자정·일시정지에서 나뉨, 좌표 없음",
              len(chunks) >= 4 and all(x[2] == 0 for x in chunks),
              [(x[0][5:], x[1]) for x in chunks])
        check("원본 샘플 수 = 판단한 순간 (일시정지 10초 제외 약 1900)", 1850 <= n_raw <= 1910, n_raw)
        a = unpack_chunk(env.q("SELECT data FROM raw_chunks ORDER BY start_ts LIMIT 1")[0][0])
        check("덩어리 풀기: 압력 (n, 8), 좌표 없음", a["pressure"].shape[1] == 8 and "landmarks" not in a,
              f"{a['pressure'].shape}, {sorted(a)}")
        per_min = (tx1 - tx0) / ((200 - t_tx0) / 60)
        check("쓰기 횟수: 분당 트랜잭션", per_min <= 3, f"{tx1 - tx0}번 / {200 - t_tx0:.0f}초 = 분당 {per_min:.1f}번")
        full = [x for x in chunks if x[1] >= 590]
        kb = sum(x[3] for x in full) / len(full) / 1024
        check("실제 용량: 1분 덩어리 (설계 어림 약 8KB)", kb < 16, f"{kb:.1f}KB")

        print("\n4) 두 번째 세션: 기준 자세 session으로 다시 잼")
        env.run_for(5)
        env.sync(env.rt.clock.now())
        env.c.post("/session/start")
        env.run_to(13)
        rows = [tuple(x) for x in env.q("SELECT kind, status, is_current FROM baselines ORDER BY id")]
        check("baselines: initial(이전) + session(현재)", rows == [("initial", "ok", 0), ("session", "ok", 1)], rows)
        env.c.post("/session/stop")

        print("\n5) 측정 실패, 직전 기준 있음 → 직전 기준으로 계속")
        env.c.post("/session/start")
        env.fail_next_measurement()
        env.run_to(env.T() + 13)
        b = env.baseline()
        j = env.c.get("/session").json()
        check("세션 계속, 실패 + 직전 기준 사용", j["state"] == "running" and b["state"] == "failed"
              and b["using_previous"] and b["fail_reason"] == "no_person",
              f"{j['state']}, {b['state']}, {b['fail_reason']}, using_previous={b['using_previous']}")
        rows = [tuple(x) for x in env.q("SELECT kind, status, fail_reason, is_current FROM baselines ORDER BY id")]
        check("baselines: 실패 이력 추가, 현재 기준 그대로", rows[-1] == ("session", "failed", "no_person", 0)
              and rows[1][3] == 1, rows[-1])
        env.c.post("/session/stop")

        print("\n6) 채널 수 변경 (8 → 6) — 같은 DB")
        ft6 = FakeTime()
        ft6.m, ft6.w = ft.m + 100, ft.w + 100
        env6 = Env(with_channels(cfg, 6), ft6, db)
        layouts = [tuple(x) for x in env6.q("SELECT id, n_channels, channel_map FROM sensor_layouts ORDER BY id")]
        check("배치 2개 (8채널, 6채널)", [x[1] for x in layouts] == [8, 6], layouts)
        env6.sync(env.rt.clock.now() + 100)
        r = env6.c.post("/session/start")
        check("저장된 기준은 8채널이라 못 씀 → initial로 새로 잼", r.json()["baseline"]["kind"] == "initial",
              r.json()["baseline"]["kind"])
        env6.run_to(70)
        env6.c.post("/session/stop")
        shapes = sorted({(lid, unpack_chunk(d)["pressure"].shape[1])
                         for lid, d in env6.q("SELECT layout_id, data FROM raw_chunks")})
        check("예전(8채널)·새(6채널) 덩어리 둘 다 풀림", shapes == [(1, 8), (2, 6)], shapes)

        print("\n7) 비정상 종료 → 다시 켬")
        env6.run_for(2)
        env6.c.post("/session/start")
        env6.run_to(env6.T() + 75)                         # 1분 저장 한 번 지나감
        last_seen = env6.rt.recorder.last_seen()
        crash_at = env6.rt.clock.now()                      # 여기서 정지 없이 꺼짐
        ft7 = FakeTime()
        ft7.m, ft7.w = ft6.m + 30, ft6.w + 30
        env7 = Env(with_channels(cfg, 6), ft7, db)
        s = env7.q("SELECT end_reason, ended_at FROM sessions ORDER BY started_at DESC LIMIT 1")[0]
        check("열린 세션 → crash, 끝 = 마지막 저장 시각", s[0] == "crash" and abs(s[1] - last_seen) < 1e-6,
              f"{s[0]}, 꺼진 시각보다 {crash_at - s[1]:.1f}초 앞 (최대 1분 손실)")
        seg = env7.q("SELECT end_reason FROM seating_segments WHERE end_reason = 'crash'")
        check("열린 구간 → crash", len(seg) == 1, len(seg))
        check("recovered 이벤트", env7.one("SELECT COUNT(*) FROM session_events WHERE kind = 'recovered'") == 1, 1)

        print("\n8) 다시 켠 뒤 첫 동기화가 마지막 기록보다 이전")
        r = env7.sync(last_seen - 3600)
        d = r.json()["detail"]
        check("409 CLOCK_BEFORE_LAST_RECORD + last_record_at", code(r) == "409 CLOCK_BEFORE_LAST_RECORD"
              and abs(d["last_record_at"] - last_seen) < 1e-6,
              f"{code(r)}, last_record_at={datetime.fromtimestamp(d['last_record_at'], KST):%m-%d %H:%M:%S}")
        r = env7.sync(last_seen + 60)
        check("마지막 기록 이후 시각 → 받아들임", r.status_code == 200, r.status_code)

        print("\n9) 기록 초기화")
        r = env7.c.post("/records/reset", json={"confirm": "yes"})
        check("확인 문구 틀림 → 422", code(r) == "422 CONFIRM_REQUIRED", code(r))
        env7.c.post("/session/start")
        r = env7.c.post("/records/reset", json={"confirm": "DELETE_RECORDS"})
        check("세션 중 → 409", code(r) == "409 SESSION_ACTIVE", code(r))
        env7.c.post("/session/stop")
        r = env7.c.post("/records/reset", json={"confirm": "DELETE_RECORDS"})
        j = r.json()
        check("초기화 → 기록 0, resync_required", r.status_code == 200 and j["resync_required"]
              and sum(env7.one(f"SELECT COUNT(*) FROM {t}") for t in j["deleted"]) == 0,
              {k: v for k, v in j["deleted"].items() if v})
        check("설정·기준 자세·배치는 남음", env7.one("SELECT COUNT(*) FROM settings") == 7
              and env7.one("SELECT COUNT(*) FROM baselines") > 0
              and env7.one("SELECT COUNT(*) FROM sensor_layouts") == 2,
              f"baselines {env7.one('SELECT COUNT(*) FROM baselines')}줄")
        r = env7.sync(last_seen - 3600)
        check("초기화 뒤에는 이전 시각으로도 동기화 가능 (복구)", r.status_code == 200, r.status_code)

        print("\n9-1) 온보딩: 세션 없이 기준 자세 측정 (새 DB)")
        envo = Env(cfg, FakeTime(), tmp / "onboard.db")
        envo.sync(APP_START)
        r = envo.c.post("/calibrate")
        check("세션 없이 /calibrate → 200", r.status_code == 200 and r.json()["seconds"] == 10, r.json())
        envo.run_to(0.5)
        b = envo.c.get("/baseline").json()
        j = envo.c.get("/session").json()
        check("0.5초: /baseline measuring + waiting_seat=true", b["state"] == "measuring" and b["waiting_seat"],
              f"{b['state']}, waiting_seat={b['waiting_seat']}, 세션 {j['state']}")
        check("측정 중 센서: 압력 켬, 카메라는 앉을 때까지 끔", j["sensors"]["pressure"] == "on"
              and j["sensors"]["camera"] == "off", j["sensors"])
        envo.run_to(1.5)
        b = envo.c.get("/baseline").json()
        check("1.5초: 앉음 확인 → waiting_seat=false, 카메라 켜는 중",
              b["state"] == "measuring" and not b["waiting_seat"]
              and envo.c.get("/session").json()["sensors"]["camera"] == "warming", b["waiting_seat"])
        envo.run_to(12)
        b = envo.c.get("/baseline").json()
        check("12초: ready, waiting_seat=false", b["state"] == "ready" and not b["waiting_seat"], b["state"])
        rows = [tuple(x) for x in envo.q("SELECT kind, status, is_current, session_id FROM baselines")]
        check("baselines에만 저장 (initial, session_id 없음)", rows == [("initial", "ok", 1, None)], rows)
        counts = {t: envo.one(f"SELECT COUNT(*) FROM {t}") for t in
                  ("sessions", "seating_segments", "raw_chunks", "posture_hour", "hour_metrics")}
        check("기록 안 쌓임 (세션·구간·원본·시간대별 0줄)", sum(counts.values()) == 0, counts)
        envo.run_to(14)
        check("측정 끝 → 센서 모두 끔", set(envo.c.get("/session").json()["sensors"].values()) == {"off"},
              envo.c.get("/session").json()["sensors"])
        envo.c.post("/session/start")
        envo.run_to(envo.T() + 13)
        envo.c.post("/calibrate")
        envo.run_for(0.1)                                   # 요청 → 다음 순간 앉아 있는 걸 보고 측정 시작
        check("세션 중 /calibrate (설정의 다시 측정) → 0.1초 뒤 측정 시작",
              envo.c.get("/baseline").json()["state"] == "measuring"
              and not envo.c.get("/baseline").json()["waiting_seat"], envo.c.get("/baseline").json()["state"])
        envo.run_to(envo.T() + 11)
        rows = [r_[0] for r_ in envo.q("SELECT kind FROM baselines WHERE status = 'ok' ORDER BY id")]
        check("baselines: initial → session → recalibration", rows == ["initial", "session", "recalibration"], rows)
        envo.c.post("/session/pause")
        r = envo.c.post("/calibrate")
        check("일시정지 중 /calibrate → 409 NOT_RUNNING", code(r) == "409 NOT_RUNNING", code(r))
        envo.c.post("/session/stop")

        print("\n10) 측정 실패, 기준이 한 번도 없음 → 세션 멈춤 (새 DB)")
        ft10 = FakeTime()
        env10 = Env(cfg, ft10, tmp / "fresh.db")
        env10.sync(APP_START)
        env10.c.post("/session/start")
        env10.fail_next_measurement()
        env10.run_to(14)
        j = env10.c.get("/session").json()
        check("세션 정지 (baseline_failed)", j["state"] == "idle" and j["end_reason"] == "baseline_failed",
              f"{j['state']}, {j['end_reason']}, {j['baseline']['state']} {j['baseline']['fail_reason']}")
        check("sessions.end_reason, baselines 실패 1줄",
              env10.one("SELECT end_reason FROM sessions") == "baseline_failed"
              and [tuple(x) for x in env10.q("SELECT kind, status FROM baselines")] == [("initial", "failed")],
              [tuple(x) for x in env10.q("SELECT kind, status, is_current FROM baselines")])

        print("\n11) 원본에 좌표 저장 켬 (raw_store_pose) — 용량 비교")
        env10.db.execute("UPDATE settings SET value = 'true', updated_at = 1 WHERE key = 'raw_store_pose'")
        env10.c.post("/session/start")
        env10.run_to(env10.T() + 75)
        env10.c.post("/session/stop")
        rows = env10.q("SELECT n_samples, has_pose, LENGTH(data) FROM raw_chunks WHERE has_pose = 1")
        full = [x for x in rows if x[0] >= 590]
        kb = sum(x[2] for x in full) / max(len(full), 1) / 1024
        lm = unpack_chunk(env10.q("SELECT data FROM raw_chunks WHERE has_pose = 1 LIMIT 1")[0][0])["landmarks"]
        check("좌표 포함 덩어리 (설계 어림 약 36KB)", len(full) >= 1 and lm.shape[1:] == (7, 4),
              f"1분 {kb:.1f}KB, 좌표 {lm.shape}")
    finally:
        logging.disable(logging.CRITICAL)
        shutil.rmtree(tmp, ignore_errors=True)

    bad = [r for r in check.rows if not r[0]]
    print(f"\n전체 {len(check.rows)}개 중 통과 {len(check.rows) - len(bad)}개")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
