"""mock 서버 — 더미 데이터로 앱 API를 먼저 맞추기 위한 FastAPI 서버

센서·AI 판정 대신 시나리오(hardware/mock/scenarios.py)를 10Hz로 재생하고,
가짜 판단 엔진(mock_engine.py) 결과를 앱에 준다. 응답 형식은 schemas.py → openapi.yaml.
영상 이미지는 만들지도, 보내지도 않는다 (카메라 좌표 숫자만).

실행 (저장소 최상위):
    python -m hardware.server.mock_server                       # demo 시나리오 반복, 포트 8000
    python -m hardware.server.mock_server --scenario forward_head --no-loop
    python -m hardware.server.mock_server --export-openapi      # openapi.yaml 다시 만들기

같은 Wi-Fi의 폰에서: http://<노트북 IP>:8000/current
브라우저로 직접 호출해 보기: http://localhost:8000/docs
"""
from __future__ import annotations

import argparse
import logging
import socket
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException, Query

from common.config import ConfigError, load_config
from common.sensors_base import SampleSkipped, now
from hardware.mock.generator import ScenarioPlayer
from hardware.mock.scenarios import SCENARIOS, Scenario, Step, get_scenario
from hardware.server.mock_engine import MockEngine
from hardware.server.schemas import (
    API_VERSION, Baseline, CalibrateResult, Current, History, ScenarioInfo, ScenarioSelect, Seating,
    SeatingSegments, Status,
)

log = logging.getLogger("sitsense.server")
OPENAPI_PATH = Path(__file__).with_name("openapi.yaml")


class MockRuntime:
    """시나리오 재생 + 가짜 판단을 백그라운드에서 10Hz로 돌린다"""

    def __init__(self, cfg: dict, scenario: str = "demo", loop: bool = True, seed: int | None = None):
        self.cfg = cfg
        self.period = 1.0 / cfg["sampling"]["hz"]
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.select(scenario, loop, seed)

    def select(self, name: str, loop: bool = True, seed: int | None = None) -> Scenario:
        scenario = get_scenario(name)
        with self.lock:
            self.scenario, self.loop = scenario, loop
            self.player = ScenarioPlayer(self.cfg, scenario, seed=seed, loop=loop)
            self.engine = MockEngine(self.cfg, seed=seed)    # 새 시나리오 = 기준 자세부터 다시
            self.player.start()
        log.info("시나리오: %s (%s, %.0f초, %s)", name, scenario.description, scenario.total_sec,
                 "반복" if loop else "한 번")
        return scenario

    def tick(self) -> None:
        ts = now()
        with self.lock:
            if self.player.finished(ts):
                return                             # 한 번만 재생: 끝나면 기록이 멈춤 (앱은 '연결 끊김' 확인 가능)
            _, step = self.player.current(ts)
            try:
                sample = self.player.read(ts)
            except SampleSkipped:
                return
            self.engine.update(sample, step)

    def calibrate(self) -> float:
        with self.lock:
            ts = now()
            seconds = self.engine.start_baseline(ts)
            self.player.hold(Step(seconds), ts + seconds)    # 측정하는 동안 바른 자세로 앉음
            return seconds

    def _run(self) -> None:
        while not self._stop.is_set():
            t0 = now()
            try:
                self.tick()
            except Exception:  # noqa: BLE001 — 한 순간 오류로 서버가 멈추지 않게
                log.exception("mock 데이터 생성 오류")
            self._stop.wait(max(0.0, self.period - (now() - t0)))

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="mock-runtime", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)


def scenario_info(s: Scenario) -> dict:
    return {"name": s.name, "description": s.description, "total_sec": s.total_sec,
            "steps": [{"seconds": st.seconds, "phase": st.phase, "seat": st.seat, "head": st.head,
                       "tilt": st.tilt, "faults": st.faults} for st in s.steps]}


def create_app(cfg: dict, scenario: str = "demo", loop: bool = True, seed: int | None = None) -> FastAPI:
    runtime = MockRuntime(cfg, scenario, loop, seed)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        runtime.start()
        yield
        runtime.stop()

    app = FastAPI(
        title="SitSense API (mock)",
        version=API_VERSION,
        description="Raspberry Pi 서버 → 안드로이드 앱. 같은 Wi-Fi에서 `http://<Pi IP>:8000`.\n\n"
                    "지금은 **mock 서버**: 시나리오 더미 데이터와 가짜 판단 결과를 준다. "
                    "시각은 time.time() 초, 거리 mm, 각도 °, 비율 0~1. 표시용 글자는 앱이 만든다. "
                    "영상 이미지는 어떤 응답에도 없다.",
        lifespan=lifespan,
    )
    app.state.runtime = runtime

    @app.get("/status", response_model=Status, tags=["기기"], summary="서버·센서 상태")
    def status():
        ts = now()
        with runtime.lock:
            e = runtime.engine
            latest = e.latest
            return {
                "mode": "mock",
                "server_time": ts,
                "last_record_ts": latest["ts"] if latest else None,
                "baseline_ready": e.baseline is not None,
                "sensors": {
                    "pressure": {"ok": e.sensor_ok("pressure", ts),
                                 "detail": f"FSR {cfg['pressure']['n_channels']}채널 (MCP3008)"},
                    "distance": {"ok": e.sensor_ok("distance", ts), "detail": runtime.player.distance_sensor},
                    "camera": {"ok": e.sensor_ok("camera", ts), "detail": "Pi Camera V2"},
                },
                "scenario": runtime.scenario.name,
                "scenario_elapsed_sec": round(runtime.player.elapsed(ts), 1),
                "scenario_total_sec": runtime.scenario.total_sec,
            }

    @app.get("/current", response_model=Current, tags=["자세"], summary="지금 자세 (1~3초마다 호출)",
             responses={404: {"description": "아직 기록 없음"}})
    def current():
        with runtime.lock:
            latest = runtime.engine.latest
            if latest is None:
                raise HTTPException(404, "기록 없음")
            return latest

    @app.get("/history", response_model=History, tags=["자세"], summary="최근 판단 기록 (1초에 1개)")
    def history(seconds: int = Query(60, ge=1, le=3600, description="최근 몇 초")):
        with runtime.lock:
            items = list(runtime.engine.history)
        cutoff = now() - seconds
        return {"seconds": seconds, "items": [i for i in items if i["ts"] >= cutoff]}

    @app.get("/seating", response_model=Seating, tags=["착석"],
             summary="지금 착석 구간 (자리 비움 5초 규칙, 정상/비정상/unknown)")
    def seating():
        ts = now()
        with runtime.lock:
            return runtime.engine.seating.snapshot(ts)

    @app.get("/seating/segments", response_model=SeatingSegments, tags=["착석"],
             summary="끝난 착석 구간 목록")
    def seating_segments(hours: int = Query(24, ge=1, le=48, description="최근 몇 시간 안에 끝난 구간")):
        with runtime.lock:
            items = runtime.engine.seating.finished(since_ts=now() - hours * 3600)
        return {"hours": hours, "items": items}

    @app.get("/baseline", response_model=Baseline, tags=["기준 자세"], summary="기준(바른) 자세 측정값")
    def baseline():
        ts = now()
        with runtime.lock:
            e = runtime.engine
            b = e.baseline
            state = "measuring" if e.measuring else ("ready" if b else "none")
            out = {"state": state, "remaining_sec": e.baseline_remaining(ts)}
            if b:
                out.update(created_at=b.created_at, seconds=b.seconds, pressure=b.pressure,
                           distance_mm=b.distance_mm, pose=b.pose)
            return out

    @app.post("/calibrate", response_model=CalibrateResult, tags=["기준 자세"],
              summary="기준 자세 다시 측정 (그동안 바른 자세로 앉기)")
    def calibrate():
        return {"started": True, "seconds": runtime.calibrate()}

    @app.get("/mock/scenarios", response_model=list[ScenarioInfo], tags=["mock 전용"],
             summary="시나리오 목록")
    def scenarios():
        return [scenario_info(s) for s in SCENARIOS.values()]

    @app.post("/mock/scenario", response_model=ScenarioInfo, tags=["mock 전용"],
              summary="재생할 시나리오 바꾸기 (기준 자세부터 다시 시작)",
              responses={404: {"description": "없는 시나리오"}})
    def select_scenario(req: ScenarioSelect):
        if req.name not in SCENARIOS:
            raise HTTPException(404, f"시나리오 '{req.name}' 없음. 가능: {list(SCENARIOS)}")
        return scenario_info(runtime.select(req.name, req.loop, req.seed))

    return app


def export_openapi(cfg: dict, path: Path = OPENAPI_PATH) -> Path:
    spec = create_app(cfg).openapi()
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("# 자동 생성 파일 — 직접 고치지 말고 hardware/server/schemas.py를 고친 뒤\n"
                "#   python -m hardware.server.mock_server --export-openapi\n")
        yaml.safe_dump(spec, f, allow_unicode=True, sort_keys=False)
    return path


def lan_ip() -> str:
    """같은 Wi-Fi의 폰에서 접속할 주소 (실제로 패킷을 보내지는 않음)"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="[SitSense] %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="SitSense mock 서버")
    ap.add_argument("--scenario", default="demo", choices=list(SCENARIOS))
    ap.add_argument("--no-loop", action="store_true", help="시나리오를 한 번만 재생")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--host", default="0.0.0.0", help="0.0.0.0 = 같은 Wi-Fi의 폰에서도 접속 가능")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--export-openapi", action="store_true", help=f"{OPENAPI_PATH.name}만 만들고 종료")
    args = ap.parse_args()

    try:
        cfg = load_config()
    except ConfigError as e:
        log.error("%s", e)
        return 1
    if args.export_openapi:
        log.info("저장: %s", export_openapi(cfg))
        return 0

    import uvicorn
    app = create_app(cfg, args.scenario, loop=not args.no_loop, seed=args.seed)
    log.info("폰에서 접속: http://%s:%d/current   (API 문서: http://localhost:%d/docs)",
             lan_ip(), args.port, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
