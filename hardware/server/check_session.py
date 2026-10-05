"""시각 동기화 + 세션 제어 + 센서 전원 확인 — 가짜 시계로 서버를 돌리며 API를 부른다

실시간을 기다리지 않는다: 가짜 단조시계를 0.1초씩 넘기며 runtime.tick()을 직접 부르고,
API는 TestClient로 부른다 (백그라운드 스레드는 켜지 않음).
Pi 시계는 일부러 틀리게(2026-01-01) 두고, 앱 시각(2026-10-15 23:59:50 KST)으로 기록되는지 본다.

사용 예 (저장소 최상위):
    python -m hardware.server.check_session
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from common.config import ConfigError, load_config
from hardware.server.clock import AppClock
from hardware.server.mock_server import create_app

KST = timezone(timedelta(hours=9))
APP_START = datetime(2026, 10, 15, 23, 59, 50, tzinfo=KST).timestamp()   # 10초 뒤 자정
PI_WALL = datetime(2026, 1, 1, 9, 0, 0, tzinfo=KST).timestamp()          # 일부러 틀린 Pi 시계


class FakeTime:
    def __init__(self):
        self.m, self.w = 1000.0, PI_WALL

    def mono(self) -> float:
        return self.m

    def wall(self) -> float:
        return self.w

    def advance(self, sec: float) -> None:
        self.m += sec
        self.w += sec


class Checker:
    def __init__(self):
        self.rows: list[tuple[bool, str, str]] = []

    def __call__(self, name: str, ok: bool, got) -> bool:
        self.rows.append((bool(ok), name, str(got)))
        print(f"  {'✓' if ok else '⚠️'} {name:<46} {got}")
        return ok


def make(cfg: dict, ft: FakeTime, scenario: str = "empty_6s"):
    app = create_app(cfg, scenario, loop=True, seed=1, clock=AppClock(monotonic=ft.mono, wall=ft.wall))
    return TestClient(app), app.state.runtime


def code(r) -> str:
    return f"{r.status_code} {r.json()['detail']['code']}" if r.status_code >= 400 else str(r.status_code)


def main() -> int:
    try:
        cfg = load_config()
    except ConfigError as e:
        print(e)
        return 1
    ft = FakeTime()
    c, rt = make(cfg, ft)
    check = Checker()
    t_sync = 0.0

    def T() -> float:
        return round(ft.m - t_sync, 1)

    def run_to(t: float) -> None:
        while T() < t:
            ft.advance(0.1)
            rt.tick()

    def sess() -> dict:
        return c.get("/session").json()

    def sensors() -> str:
        s = sess()["sensors"]
        return f"압력 {s['pressure']}, 카메라 {s['camera']}, 거리 {s['distance']}"

    print("1) 동기화 전")
    r = c.post("/session/start")
    check("세션 시작 거절", code(r) == "409 CLOCK_NOT_SYNCED" and r.json()["detail"]["resync_required"], code(r))
    check("GET /time synced=false, resync_required", (lambda j: not j["synced"] and j["resync_required"])(
        c.get("/time").json()), c.get("/time").json()["message"])
    for path, want in (("/session/pause", "NOT_RUNNING"), ("/session/resume", "NOT_PAUSED"),
                       ("/session/stop", "NO_SESSION"), ("/calibrate", "NOT_RUNNING")):
        check(f"{path} (세션 없음)", code(c.post(path)) == f"409 {want}", code(c.post(path)))
    r = c.post("/time/sync", json={"app_time": 5, "timezone": "Asia/Seoul"})
    check("잘못된 시각", code(r) == "422 INVALID_TIME", code(r))
    r = c.post("/time/sync", json={"app_time": APP_START, "timezone": "Mars/Base"})
    check("잘못된 시간대", code(r) == "422 INVALID_TIMEZONE", code(r))

    print("\n2) 동기화 (앱 2026-10-15 23:59:50 KST, Pi 시계는 2026-01-01) → 시작")
    r = c.post("/time/sync", json={"app_time": APP_START, "timezone": "Asia/Seoul"})
    t_sync = ft.m
    check("동기화", r.status_code == 200 and r.json()["local_time"] == "2026-10-15T23:59:50+09:00",
          r.json()["local_time"])
    r = c.post("/session/start")
    j = r.json()
    check("세션 시작", r.status_code == 200 and j["state"] == "running", f"{j['state']} {j['session_id']}")
    check("시작 시각 = 앱 시각", abs(j["started_at"] - APP_START) < 1e-6, j["started_at"])
    check("또 시작 → 거절", code(c.post("/session/start")) == "409 SESSION_ACTIVE", code(c.post("/session/start")))
    check("일시정지 아닌데 재개 → 거절", code(c.post("/session/resume")) == "409 NOT_PAUSED",
          code(c.post("/session/resume")))
    check("시작 직후 센서 (아직 착석 확인 전)", sess()["sensors"]["camera"] == "off", sensors())

    print("\n3) 착석 → 자정 넘김 (시나리오 empty_6s 반복: 30~36초 자리 비움)")
    run_to(1.5)
    check("1.5초: 착석 확인 → 카메라 켜는 중", sess()["sensors"]["camera"] == "warming", sensors())
    run_to(4)
    check("4초: 카메라 켜짐", sess()["sensors"]["camera"] == "on", sensors())
    run_to(15)
    cur = c.get("/current").json()
    day = datetime.fromtimestamp(cur["ts"], KST)
    check("15초: /current 시각 = 앱 기준, 자정 넘김", abs(cur["ts"] - (APP_START + 15)) < 0.15
          and day.day == 16, day.isoformat(timespec="milliseconds"))
    check("기록 시각과 Pi 시계 차이 (Pi 시계 안 씀)", abs(cur["ts"] - ft.wall()) > 1e6,
          f"{(cur['ts'] - ft.wall()) / 86400:.1f}일")

    print("\n4) 자리 비움 6초 → 다시 앉음")
    run_to(33)
    s = c.get("/seating").json()
    check("33초: 비운 지 3초, 센서 유지", s["away_sec"] is not None and sess()["sensors"]["camera"] == "on",
          f"away {s['away_sec']}초, {sensors()}")
    run_to(35.3)
    j = sess()
    check("35.3초: 5초 → 카메라·거리 끔, 압력 유지",
          j["sensors"] == {"pressure": "on", "camera": "off", "distance": "off"} and j["seated_now"] is False,
          f"{sensors()}, seated_now={j['seated_now']}")
    segs = c.get("/seating/segments").json()["items"]
    check("구간 0~30 종료 (away)", len(segs) == 1 and segs[0]["end_reason"] == "away"
          and abs(segs[0]["end"] - (APP_START + 30)) < 0.15,
          [(round(g["start"] - APP_START, 1), round(g["end"] - APP_START, 1), g["end_reason"]) for g in segs])
    run_to(37.5)
    check("37.5초: 다시 앉음(36) + 1초 확인 → 카메라 켜는 중", sess()["sensors"]["camera"] == "warming", sensors())
    run_to(39.5)
    check("39.5초: 카메라 켜짐", sess()["sensors"]["camera"] == "on", sensors())

    print("\n5) 일시정지 40초 → 재개 60초")
    run_to(40)
    r = c.post("/session/pause")
    check("일시정지", r.status_code == 200 and r.json()["state"] == "paused", r.json()["state"])
    check("일시정지 센서: 압력만", r.json()["sensors"] == {"pressure": "on", "camera": "off", "distance": "off"},
          sensors())
    last_ts = c.get("/current").json()["ts"]
    run_to(50)
    j = sess()
    check("50초: 경과 시간 멈춤 (40)", abs(j["elapsed_sec"] - 40) <= 0.1, j["elapsed_sec"])
    check("50초: 기록 안 함 (/current 그대로)", c.get("/current").json()["ts"] == last_ts,
          round(last_ts - APP_START, 1))
    check("50초: 압력으로 앉아 있음 확인", j["seated_now"] is True, j["seated_now"])
    segs = c.get("/seating/segments").json()["items"]
    check("구간 36~40 종료 (pause)", segs[-1]["end_reason"] == "pause"
          and abs(segs[-1]["end"] - (APP_START + 40)) < 0.15,
          (round(segs[-1]["start"] - APP_START, 1), round(segs[-1]["end"] - APP_START, 1), segs[-1]["end_reason"]))
    check("일시정지 중 또 일시정지 → 거절", code(c.post("/session/pause")) == "409 NOT_RUNNING",
          code(c.post("/session/pause")))
    run_to(60)
    r = c.post("/session/resume")
    check("재개", r.status_code == 200 and r.json()["state"] == "running", r.json()["state"])
    run_to(61.5)
    s = c.get("/seating").json()
    check("61.5초: 새 구간 시작 60초, 카메라 켜는 중",
          s["segment"] and abs(s["segment"]["start"] - (APP_START + 60)) < 0.15
          and sess()["sensors"]["camera"] == "warming",
          f"시작 {round(s['segment']['start'] - APP_START, 1)}, {sensors()}")

    print("\n6) 정지 70초")
    run_to(70)
    r = c.post("/session/stop")
    j = r.json()
    check("정지 → idle, 센서 모두 끔", j["state"] == "idle" and set(j["sensors"].values()) == {"off"}, sensors())
    check("경과 50 (70 − 일시정지 20)", abs(j["elapsed_sec"] - 50) <= 0.1 and abs(j["paused_sec"] - 20) <= 0.1,
          f"경과 {j['elapsed_sec']}, 일시정지 {j['paused_sec']}")
    # 시작·재개 뒤 첫 압력은 다음 틱(0.1초 뒤)에 읽힌다 → 그 두 구간은 0.1초씩 짧음
    check("착석 43.8 (29.9 + 4 + 9.9)", abs(j["seated_sec"] - 43.8) <= 0.05, j["seated_sec"])
    segs = c.get("/seating/segments").json()["items"]
    check("구간 3개: away / pause / stop", [g["end_reason"] for g in segs] == ["away", "pause", "stop"],
          [(round(g["start"] - APP_START, 1), round(g["end"] - APP_START, 1), g["end_reason"]) for g in segs])
    run_to(80)
    j = sess()
    check("80초: 앉아 있어도 자동 시작 안 함, 압력도 안 읽음",
          j["state"] == "idle" and j["seated_now"] is None and c.get("/seating").json()["segment"] is None,
          f"{j['state']}, seated_now={j['seated_now']}")
    check("정지 후 또 정지 → 거절", code(c.post("/session/stop")) == "409 NO_SESSION", code(c.post("/session/stop")))

    print("\n7) 다시 동기화 규칙 (뒤로 0.5초 이내 → 적용 안 함, 넘으면 거절 / 세션 중 +2초 넘게 → 거절)")

    def resync(delta: float):
        """지금 앱 시각 + delta로 다시 동기화 → (응답, 그 뒤 시각이 delta만큼 바뀌었는지 확인용 값)"""
        before = c.get("/time").json()["app_time"]
        r = c.post("/time/sync", json={"app_time": before + delta, "timezone": "Asia/Seoul"})
        return r, round(c.get("/time").json()["app_time"] - before, 3)

    c.post("/session/start")
    run_to(85)
    r, moved = resync(3)
    check("세션 중 +3초 → 거절, 시각 그대로", code(r) == "409 CLOCK_JUMP_IN_SESSION" and moved == 0,
          f"{code(r)}, 바뀐 양 {moved}")
    r, moved = resync(1.5)
    check("세션 중 +1.5초 → 적용", r.status_code == 200 and r.json()["applied"] and moved == 1.5,
          f"applied={r.json().get('applied')}, 바뀐 양 {moved}")
    r, moved = resync(-0.3)
    check("세션 중 −0.3초 → 적용 안 함 (200)", r.status_code == 200 and r.json()["applied"] is False
          and moved == 0, f"applied={r.json().get('applied')}, 바뀐 양 {moved}")
    r, moved = resync(-1)
    check("세션 중 −1초 → 거절", code(r) == "409 CLOCK_BACKWARD" and moved == 0, f"{code(r)}, 바뀐 양 {moved}")
    c.post("/session/stop")
    r, moved = resync(10)
    check("세션 밖 +10초 → 적용", r.status_code == 200 and r.json()["applied"] and moved == 10,
          f"applied={r.json().get('applied')}, 바뀐 양 {moved}")
    r, moved = resync(-5)
    check("세션 밖 −5초 → 거절", code(r) == "409 CLOCK_BACKWARD" and moved == 0, f"{code(r)}, 바뀐 양 {moved}")
    r = c.post("/time/sync", json={"app_time": c.get("/time").json()["app_time"], "timezone": "America/New_York"})
    check("Asia/Seoul이 아닌 시간대 → 거절", code(r) == "422 INVALID_TIMEZONE", code(r))

    print("\n8) 서버 재시작 (재부팅 흉내)")
    old_boot = c.get("/time").json()["boot_id"]
    c2, _ = make(cfg, ft)
    t = c2.get("/time").json()
    check("boot_id 바뀜, synced=false", t["boot_id"] != old_boot and not t["synced"],
          f"{old_boot} → {t['boot_id']}")
    r = c2.post("/session/start")
    check("시작 거절 (다시 동기화 필요)", code(r) == "409 CLOCK_NOT_SYNCED" and r.json()["detail"]["resync_required"],
          code(r))

    bad = [r for r in check.rows if not r[0]]
    print(f"\n전체 {len(check.rows)}개 중 통과 {len(check.rows) - len(bad)}개")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
