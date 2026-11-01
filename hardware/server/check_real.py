"""real 모드 서버 확인 — 실제 센서 대신 mock 센서(MockSensorHub)를 끼워서 PC에서 돌려 본다

시험하는 것은 '실제 센서를 읽는 길'이다: SensorHub → RealSource(시각 변환) → GatedSensor(전원)
→ RealEngine(자리 비움만 판단) → 기준 자세·착석 구간·DB·API. 센서 값 자체는 가짜다.
가짜 시계로 시간을 넘기며 runtime.tick()을 직접 부르고(백그라운드 스레드 없음), API는 TestClient로 부른다.

사용 예 (저장소 최상위):
    python -m hardware.server.check_real
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

import common.mock_sensors as mock_sensors
import common.sensors_base as sensors_base
import hardware.server.real_runtime as real_runtime
from common.config import ConfigError, load_config
from common.mock_sensors import MockSensorHub
from common.sensors_base import SampleSkipped
from hardware.server.check_session import APP_START, Checker, FakeTime
from hardware.server.clock import AppClock
from hardware.server.mock_server import create_app
from hardware.server.real_runtime import RealRuntime

DT = 0.5                                    # config.yaml sampling.hz = 2


class ScaledPressure:
    """common의 mock 압력(약 400~600)을 실제 MCP3208 크기(약 3000대)로 키움 — AI 엔진은 최대치의 25% 이상 눌려야 기준 측정 성공"""
    SCALE = 7

    def __init__(self, inner, scale: int = SCALE):
        object.__setattr__(self, "scale", scale)
        object.__setattr__(self, "inner", inner)

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def __setattr__(self, name, value):
        if name in ("inner", "scale"):
            object.__setattr__(self, name, value)
        else:
            setattr(self.inner, name, value)

    def read(self):
        r = self.inner.read()
        r.values = [min(4095, v * self.scale) for v in r.values]
        return r

    def close(self):
        self.inner.close()


def build(cfg: dict, ft: FakeTime, *, camera: bool = True, db: str | None = None, scale: int = ScaledPressure.SCALE):
    hub = MockSensorHub(cfg, seed=1)
    hub.pressure = ScaledPressure(hub.pressure, scale)
    if not camera:
        hub.pose = None
    clock = AppClock(monotonic=ft.mono, wall=ft.wall)
    rt = RealRuntime(cfg, hub, clock=clock, db_path=db)
    return TestClient(create_app(cfg, runtime=rt)), rt


def run(rt: RealRuntime, ft: FakeTime, sec: float) -> None:
    for _ in range(int(round(sec / DT))):
        ft.advance(DT)
        rt.tick()


def main() -> int:
    try:
        cfg = load_config()
    except ConfigError as e:
        print(e)
        return 1
    ft = FakeTime()
    # 센서·변환 코드가 부르는 time.time()을 가짜 Pi 시계로 (Pi 시계는 일부러 틀리게 둠)
    for mod in (sensors_base, mock_sensors, real_runtime):
        mod.now = ft.wall
    ok = Checker()

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        c, rt = build(cfg, ft, db=str(Path(tmp) / "real.db"))
        inner_p = rt.gates["pressure"].inner.inner       # 가짜 센서 (ScaledPressure 안쪽)
        st = c.get("/status").json()
        ok("mode가 real", st["mode"] == "real", st["mode"])
        ok("시나리오 정보 없음", st["scenario"] is None and st["scenario_total_sec"] is None, st["scenario"])
        ok("mock 전용 API 없음", c.get("/mock/scenarios").status_code == 404, c.get("/mock/scenarios").status_code)
        ok("동기화 전 세션 시작 거부", c.post("/session/start").status_code == 409, "409")

        c.post("/time/sync", json={"app_time": APP_START, "timezone": "Asia/Seoul"})
        s = c.post("/session/start").json()
        ok("세션 시작: 기준 자세 대기", s["state"] == "running" and s["baseline"]["state"] == "waiting_seat",
           s["baseline"]["state"])
        ok("센서 전원: 압력만 켜짐", s["sensors"]["pressure"] == "on" and s["sensors"]["distance"] == "off",
           s["sensors"])

        run(rt, ft, 3)
        s = c.get("/session").json()
        cur = c.get("/current").json()
        ok("앉음 감지", s["seated_now"] is True, s["seated_now"])
        ok("기록 시각이 앱 기준 (Pi 시계 아님)", abs(cur["ts"] - (APP_START + 3 + 0.0)) < 5, round(cur["ts"] - APP_START, 1))
        ok("압력 6채널 실제값 전달", len(cur["pressure"]) == cfg["pressure"]["n_channels"], cur["pressure"])
        ok("기준 자세 전에는 unknown", cur["seat_state"] == "unknown" and cur["head_state"] == "unknown",
           (cur["seat_state"], cur["head_state"]))
        ok("거리·카메라 켜짐 (착석 구간 안)", s["sensors"]["distance"] in ("on", "warming"), s["sensors"])

        run(rt, ft, 16)
        b = c.get("/baseline").json()
        ok("기준 자세 측정 완료", b["state"] == "ready" and b["distance_mm"] is not None, b["state"])
        cur = c.get("/current").json()
        ok("거리 값 실제 전달", cur["distance_mm"] is not None, cur["distance_mm"])
        run(rt, ft, 8)                          # AI 엔진의 4초 유지가 지나야 확정
        cur = c.get("/current").json()
        ok("AI 판단: 좌면 normal", cur["seat_state"] == "normal", cur["seat_state"])
        ok("AI 판단: 목 normal, 기울기 none", (cur["head_state"], cur["tilt_state"]) == ("normal", "none"),
           (cur["head_state"], cur["tilt_state"]))
        ok("화면 거리 단계 ok (600mm)", cur["distance_level"] == "ok", cur["distance_level"])
        ok("엔진 신뢰도 전달", cur["confidence"]["seat"] > 0, cur["confidence"])
        ok("압력 중심 계산", cur["center_of_pressure"] is not None, cur["center_of_pressure"])

        # 자리 비움
        inner_p.label, inner_p.noise = "empty", 2.0      # 빈 의자: 가짜 압력 잡음을 작게 (12비트에서 잡음 20이면 합이 50을 넘음)
        run(rt, ft, 4)                          # 상태는 3초 유지돼야 확정
        cur = c.get("/current").json()
        ok("빈 의자: seated false, seat_state empty", cur["seated"] is False and cur["seat_state"] == "empty",
           (cur["seated"], cur["seat_state"]))
        run(rt, ft, 4)
        seg = c.get("/seating/segments").json()["items"]
        ok("자리 비움 5초 → 착석 구간 종료", len(seg) == 1 and seg[0]["end_reason"] == "away", len(seg))
        s = c.get("/session").json()
        ok("구간 밖: 거리·카메라 꺼짐", s["sensors"]["distance"] == "off" and s["sensors"]["camera"] == "off", s["sensors"])
        inner_p.label, inner_p.noise = "normal", 20.0

        # 일시정지 → 거리 꺼짐, 정지 → 모두 꺼짐
        run(rt, ft, 3)
        s = c.post("/session/pause").json()
        ok("일시정지: 압력만", s["sensors"]["pressure"] == "on" and s["sensors"]["distance"] == "off", s["sensors"])
        c.post("/session/resume")
        run(rt, ft, 2)

        # 압력 읽기 한 번 실패 → 그 순간만 건너뜀, 서버는 계속
        calls = {"n": 0}
        orig = inner_p.read

        def flaky():
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("SPI 오류 흉내")
            return orig()

        inner_p.read = flaky
        try:
            run(rt, ft, 3)
            ok("압력 일시 실패에도 계속", calls["n"] >= 4 and c.get("/current").status_code == 200, calls["n"])
        except SampleSkipped:
            ok("압력 일시 실패에도 계속", False, "SampleSkipped가 밖으로 나옴")
        inner_p.read = orig

        s = c.post("/session/stop").json()
        ok("정지: 센서 모두 꺼짐", set(s["sensors"].values()) == {"off"}, s["sensors"])
        rep = c.get("/reports/daily", params={"date": "2026-10-15"})
        ok("리포트 API 응답", rep.status_code == 200, rep.status_code)
        rt.recorder.close(rt.clock.now())

    # 카메라 없이: 기준 자세가 사람 인식 없이도 잴 수 있어야 함
    ft2 = FakeTime()
    for mod in (sensors_base, mock_sensors, real_runtime):
        mod.now = ft2.wall
    c, rt = build(cfg, ft2, camera=False)
    c.post("/time/sync", json={"app_time": APP_START, "timezone": "Asia/Seoul"})
    c.post("/session/start")
    run(rt, ft2, 28)
    b = c.get("/baseline").json()
    st = c.get("/status").json()
    cur = c.get("/current").json()
    ok("카메라 없이 기준 자세 완료", b["state"] == "ready" and b["pose"] is None, b["state"])
    ok("카메라 없으면 목·기울기 unknown, 좌면은 판정", (cur["head_state"], cur["tilt_state"]) == ("unknown", "unknown")
       and cur["seat_state"] == "normal", (cur["seat_state"], cur["head_state"], cur["tilt_state"]))
    ok("카메라 없음 표시", st["sensors"]["camera"]["ok"] is False and "연결 안 됨" in st["sensors"]["camera"]["detail"],
       st["sensors"]["camera"])
    ok("세션이 멈추지 않음", c.get("/session").json()["state"] == "running", "running")

    # 압력이 약하면(덜 앉음) 기준 측정이 pressure_low로 실패하고, 성공한 기준이 없으면 세션이 멈춘다
    ft3 = FakeTime()
    for mod in (sensors_base, mock_sensors, real_runtime):
        mod.now = ft3.wall
    c, rt = build(cfg, ft3, scale=1)
    c.post("/time/sync", json={"app_time": APP_START, "timezone": "Asia/Seoul"})
    c.post("/session/start")
    run(rt, ft3, 16)
    s = c.get("/session").json()
    ok("덜 눌리면 기준 측정 실패 pressure_low", s["baseline"]["state"] == "failed"
       and s["baseline"]["fail_reason"] == "pressure_low", (s["baseline"]["state"], s["baseline"]["fail_reason"]))
    ok("성공한 기준이 없으면 세션 정지", s["state"] == "idle" and s["end_reason"] is None or s["state"] == "idle",
       (s["state"], s["end_reason"]))

    bad = [r for r in ok.rows if not r[0]]
    print(f"\n{len(ok.rows) - len(bad)}/{len(ok.rows)} 통과")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
