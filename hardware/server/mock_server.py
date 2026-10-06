"""mock 서버 — 더미 데이터로 앱 API를 먼저 맞추기 위한 FastAPI 서버

센서·AI 판정 대신 시나리오(hardware/mock/scenarios.py)를 10Hz로 재생하고,
가짜 판단 엔진(mock_engine.py) 결과를 앱에 준다. 응답 형식은 schemas.py → openapi.yaml.
영상 이미지는 만들지도, 보내지도 않는다 (카메라 좌표 숫자만).

실제 기기와 같은 흐름: 앱이 POST /time/sync(시각 동기화) → POST /session/start(측정 시작)를
보내야 시나리오가 재생되고 기록된다. 기록 시각은 모두 앱 기준 시각.
    --autostart를 주면 서버가 켜지자마자 PC 시계로 동기화하고 세션을 시작한다 (예전처럼 바로 데이터)

실행 (저장소 최상위):
    python -m hardware.server.mock_server                       # 동기화·시작을 기다림, 포트 8000
    python -m hardware.server.mock_server --autostart           # 바로 demo 재생
    python -m hardware.server.mock_server --scenario forward_head --no-loop --autostart
    python -m hardware.server.mock_server --export-openapi      # openapi.yaml 다시 만들기

같은 Wi-Fi의 폰에서: http://<노트북 IP>:8000/current
브라우저로 직접 호출해 보기: http://localhost:8000/docs
"""
from __future__ import annotations

import argparse
import copy
import logging
import socket
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException, Query

from common.config import ConfigError, load_config, repo_path
from common.sensors_base import SampleSkipped
from hardware.mock.generator import ScenarioPlayer
from hardware.mock.scenarios import SCENARIOS, Scenario, Step, get_scenario
from hardware.server.clock import AppClock, ClockError
from hardware.server.db.jobs import DayJobs
from hardware.server.db.store import DEFAULT_SETTINGS, Recorder
from hardware.server.mock_engine import BaselineValue, MockEngine
from hardware.server.power import PowerPolicy
from hardware.server.schemas import (
    API_VERSION, Baseline, CalibrateResult, Current, ErrorResponse, History, RecordsReset,
    RecordsResetResult, ScenarioInfo, ScenarioSelect, Seating, SeatingSegments, SessionStatus, Status,
    TimeStatus, TimeSync,
)
from hardware.server.seating import is_empty
from hardware.server.session import SessionController, SessionError, SessionState

log = logging.getLogger("sitsense.server")
OPENAPI_PATH = Path(__file__).with_name("openapi.yaml")


class MockRuntime:
    """시나리오 재생 + 가짜 판단을 백그라운드에서 10Hz로 돌린다.

    세션 상태(session.py)와 착석 구간을 보고 센서 전원(power.py)을 정하고,
    켜진 센서만 읽는다. 기록·판단은 세션이 running일 때만 한다.
    세션을 시작하면 앉은 게 확인된 뒤 기준 자세를 10초 잰다 (재개 때는 다시 재지 않음).
    db_path가 있으면 세션·구간·기준 자세·시간대별 시간·원본을 SQLite에 쓴다 (db/store.py).
    """

    def __init__(self, cfg: dict, scenario: str = "demo", loop: bool = True, seed: int | None = None,
                 clock: AppClock | None = None, db_path: str | Path | None = None):
        self.cfg = cfg
        self.period = 1.0 / cfg["sampling"]["hz"]
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.clock = clock or AppClock()
        self.session = SessionController()
        self.power = PowerPolicy()
        self.seated_now: bool | None = None     # 지금 압력이 있는지 (압력센서가 꺼져 있으면 None)
        self.end_reason: str | None = None      # 마지막 세션이 끝난 이유
        self._pending_stop: str | None = None   # 판단 도중 정해진 정지 (기준 자세 실패)
        self.recorder = Recorder(db_path, cfg, self.clock) if db_path else None
        self.jobs = DayJobs(self.recorder.conn, self.clock, self.recorder) if self.recorder else None   # 자정 요약·원본 삭제
        self.settings = self.recorder.settings() if self.recorder else dict(DEFAULT_SETTINGS)
        self.session_settings: dict = {}
        self.current_baseline: BaselineValue | None = None
        self.cal_engine: MockEngine | None = None       # 세션 없이 기준 자세만 잴 때 (온보딩)
        self.last_baseline_result: dict | None = None
        if self.recorder:
            self.clock.min_time = self.recorder.last_seen()     # 첫 동기화가 이보다 이전이면 거절
            b = self.recorder.current_baseline()
            if b:
                self.current_baseline = BaselineValue(
                    created_at=b["measured_at"], seconds=b["seconds"] or 0.0, pressure=b["pressure"],
                    distance_mm=b["distance_mm"], pose=b["pose"], kind=b["kind"])
        self.select(scenario, loop, seed)

    def now(self) -> float:
        """앱 기준 지금 시각 (동기화 전에는 Pi 시각 — 그때는 기록이 없다)"""
        return self.clock.now_or_pi()

    def _new_engine(self) -> MockEngine:
        """세션 설정(자세 인정 기준)과 지금 기준 자세로 판단 엔진을 만든다"""
        cfg = self.cfg
        hold = self.session_settings.get("posture_hold_sec")
        if hold is not None and hold != cfg["thresholds"]["short_filter_sec"]:
            cfg = copy.deepcopy(cfg)
            cfg["thresholds"]["short_filter_sec"] = hold        # 앱 설정이 config.yaml보다 우선
        e = MockEngine(cfg, seed=self.seed, auto_baseline=False, baseline=self.current_baseline)
        e.on_measure_start = self._on_measure_start
        e.on_baseline = self._on_baseline
        return e

    def select(self, name: str, loop: bool = True, seed: int | None = None) -> Scenario:
        """시나리오 바꾸기. 세션 중이면 시나리오는 처음부터 (기준 자세·착석 구간은 이어감)"""
        scenario = get_scenario(name)
        with self.lock:
            old = getattr(self, "engine", None)
            self.scenario, self.loop, self.seed = scenario, loop, seed
            self.player = ScenarioPlayer(self.cfg, scenario, seed=seed, loop=loop)
            self.engine = self._new_engine()
            if old is not None:
                self.engine.seating = old.seating
                if old.measure is not None:
                    self.engine.request_baseline(old.measure["kind"])
            if self.session.state is not SessionState.IDLE:
                self.player.start(self.now())
        log.info("시나리오: %s (%s, %.0f초, %s)", name, scenario.description, scenario.total_sec,
                 "반복" if loop else "한 번")
        return scenario

    # --- 기준 자세 ------------------------------------------------------------
    def _on_measure_start(self, ts: float, seconds: float) -> None:
        self.player.hold(Step(seconds), ts + seconds)          # mock: 측정하는 동안 바른 자세로 앉음

    def _on_baseline(self, result: dict) -> None:
        if self.recorder:
            self.recorder.save_baseline(result)         # 세션 없이 잰 것은 session_id 없이
        self.last_baseline_result = result
        in_session = self.session.state is not SessionState.IDLE
        if result["status"] == "ok":
            self.current_baseline = result["value"]
            log.info("기준 자세 측정 완료 (%s)", result["kind"])
        elif self.current_baseline is not None:
            log.warning("기준 자세 측정 실패 (%s) → 직전 기준 유지", result["fail_reason"])
        elif in_session:
            log.warning("기준 자세 측정 실패 (%s), 성공한 기준이 없어 세션을 멈춤", result["fail_reason"])
            self._pending_stop = "baseline_failed"
        else:
            log.warning("기준 자세 측정 실패 (%s), 기준 없음", result["fail_reason"])

    @property
    def calibrating(self) -> bool:
        """세션 없이 기준 자세를 재는 중 (온보딩)"""
        return self.cal_engine is not None and self.cal_engine.measure is not None

    @property
    def baseline_engine(self) -> MockEngine:
        """기준 자세 측정 상태를 볼 엔진 (세션 없이 재는 중이면 측정용 엔진)"""
        return self.cal_engine if self.calibrating else self.engine

    def calibrate(self) -> float:
        """기준 자세 측정. 앉은 게 확인되면 10초 잰다
        - 세션 없음 (온보딩): 측정용 엔진으로 재기만 하고 구간·원본 기록은 남기지 않음 (baselines에만 저장)
        - 세션 실행 중 (설정의 다시 측정): 판단을 계속하면서 잰다
        """
        with self.lock:
            ts = self.clock.now()
            if ts is None:
                raise SessionError("CLOCK_NOT_SYNCED", "시각 동기화 전이라 측정할 수 없음. POST /time/sync를 먼저 보내세요")
            kind = "recalibration" if self.current_baseline else "initial"
            if self.session.state is SessionState.RUNNING:
                return self.engine.request_baseline(kind)
            if self.session.state is SessionState.PAUSED:
                raise SessionError("NOT_RUNNING", "일시정지 중에는 잴 수 없음. 재개한 뒤 다시 요청하세요")
            self.cal_engine = self._new_engine()
            self.player = ScenarioPlayer(self.cfg, self.scenario, seed=self.seed, loop=self.loop)
            self.player.start(ts)
            log.info("세션 없이 기준 자세 측정 (%s): 앉으면 시작", kind)
            return self.cal_engine.request_baseline(kind)

    # --- 세션 ---------------------------------------------------------------
    def start_session(self) -> None:
        with self.lock:
            ts = self.clock.now()
            if ts is None:
                raise SessionError("CLOCK_NOT_SYNCED",
                                   "시각 동기화 전이라 세션을 시작할 수 없음. POST /time/sync를 먼저 보내세요")
            sid = "s_" + self.clock.local(ts).strftime("%Y%m%d_%H%M%S")
            self.session.start(ts, sid)
            if self.recorder:
                self.settings = self.recorder.settings()
            self.session_settings = dict(self.settings)        # 설정은 다음 세션부터 적용
            self.end_reason = None
            self.cal_engine = None                      # 세션 없이 재던 중이었으면 그만 (세션 측정으로 대신)
            self.player = ScenarioPlayer(self.cfg, self.scenario, seed=self.seed, loop=self.loop)
            self.player.start(ts)                       # 시나리오도 처음(기준 자세)부터
            self.engine = self._new_engine()
            self.engine.request_baseline("session" if self.current_baseline else "initial")
            self.power.apply(ts, SessionState.RUNNING.value, in_segment=False)
            if self.recorder:
                self.recorder.session_started(sid, ts, self.session_settings)
        log.info("세션 시작: %s (기준 자세: 앉으면 측정)", sid)

    def pause_session(self) -> None:
        with self.lock:
            ts = self.clock.now()
            self.session.pause(ts)
            self.engine.pause_baseline()                # 측정 중이었으면 재개 후 앉으면 처음부터
            self.engine.seating.pause(ts)               # 착석 구간은 여기서 끝 (end_reason=pause)
            if self.recorder:
                self.recorder.sync_segment(self.engine.seating, ts)
                self.recorder.event("pause", ts)
            self.power.apply(ts, SessionState.PAUSED.value, in_segment=False)
        log.info("세션 일시정지")

    def resume_session(self) -> None:
        with self.lock:
            ts = self.clock.now()
            self.session.resume(ts)                     # 기준 자세는 다시 재지 않음
            if self.recorder:
                self.recorder.event("resume", ts)
            self.power.apply(ts, SessionState.RUNNING.value, in_segment=False)
        log.info("세션 재개")

    def stop_session(self, reason: str = "stop") -> None:
        with self.lock:
            ts = self.clock.now()
            self.session.stop(ts)
            self.engine.cancel_baseline()
            self.engine.seating.stop(ts)
            if self.recorder:
                self.recorder.sync_segment(self.engine.seating, ts)
                self.recorder.session_ended(ts, reason)
            self.end_reason = reason
            self.power.apply(ts, SessionState.IDLE.value, in_segment=False)
            self.seated_now = None
        log.info("세션 정지: %s (%s)", self.session.session_id, reason)

    def sync_time(self, app_time: float, timezone_name: str) -> dict:
        with self.lock:
            in_session = self.session.state is not SessionState.IDLE
            first = not self.clock.synced
            change = self.clock.sync(app_time, timezone_name, in_session=in_session)
            if self.recorder and change["applied"] and change["offset_change_sec"] is not None:
                self.recorder.event("time_sync", self.clock.now(), {"offset_change_sec": change["offset_change_sec"]})
            if first and self.jobs:                     # 켠 뒤 첫 동기화: 밀린 날짜 바로 처리 (catch-up)
                self.jobs.run(self.clock.now())
            return change

    def reset_records(self) -> dict[str, int]:
        """기록만 삭제 (설정·기준 자세 유지). 시각 동기화도 지워서 앱이 다시 보내게 한다"""
        with self.lock:
            if self.session.state is not SessionState.IDLE:
                raise SessionError("SESSION_ACTIVE", "세션이 있는 동안에는 기록을 지울 수 없음. 정지한 뒤 다시 요청하세요")
            deleted = self.recorder.reset_records() if self.recorder else {}
            self.clock.unsync()
            self.clock.min_time = None
            self.session = SessionController()
            self.engine = self._new_engine()
            self.cal_engine = self.last_baseline_result = None
            self.end_reason = None
            return deleted

    def _baseline_snapshot(self, ts: float) -> dict:
        e, cur = self.baseline_engine, self.current_baseline
        last = self.last_baseline_result
        failed = last is not None and last["status"] == "failed"
        if e.measure is not None:
            state, kind = e.measure["state"], e.measure["kind"]
        elif failed:
            state, kind = "failed", last["kind"]
        elif cur is not None:
            state, kind = "ready", cur.kind
        else:
            state, kind = "none", None
        return {
            "state": state,
            "kind": kind,
            "remaining_sec": None if e.baseline_remaining(ts) is None else round(e.baseline_remaining(ts), 1),
            "fail_reason": last["fail_reason"] if failed else None,
            "using_previous": failed and cur is not None,
            "measured_at": cur.created_at if cur else None,
        }

    def session_snapshot(self) -> dict:
        with self.lock:
            ts = self.now()
            s = self.session
            seated_sec = None
            if s.started_at is not None:
                tracker = self.engine.seating
                segs = [g for g in tracker.segments if g.start >= s.started_at]
                total = sum(g.end - g.start for g in segs)
                if tracker.current is not None:
                    total += ts - tracker.current.start
                seated_sec = round(total, 1)
            elapsed = s.elapsed_sec(ts)
            return {
                "state": s.state.value,
                "session_id": s.session_id,
                "started_at": s.started_at,
                "ended_at": s.ended_at,
                "elapsed_sec": None if elapsed is None else round(elapsed, 1),
                "paused_sec": None if s.started_at is None else round(s.paused_sec(ts), 1),
                "seated_sec": seated_sec,
                "seated_now": self.seated_now,
                "sensors": self.power.states(ts),
                "clock_synced": self.clock.synced,
                "boot_id": self.clock.boot_id,
                "end_reason": None if s.state is not SessionState.IDLE else self.end_reason,
                "baseline": self._baseline_snapshot(ts),
            }

    # --- 매 순간 --------------------------------------------------------------
    def tick(self) -> None:
        with self.lock:
            ts = self.clock.now()
            if ts is None:
                return                                  # 동기화 전: 기록 없음
            if self.jobs:
                self.jobs.maybe_run(ts)                 # 1분마다: 지난 날 요약·확인, 30분 뒤 원본 삭제
            state = self.session.state.value
            cal = self.cal_engine if state == SessionState.IDLE.value and self.calibrating else None
            eng = cal or self.engine
            self.power.apply(ts, "calibrating" if cal else state, in_segment=eng.seating.current is not None)
            if not self.power.is_on("pressure"):
                self.seated_now = None
                return                                  # 세션 없음·정지: 아무것도 읽지 않음
            if self.player.finished(ts):
                return                                  # 한 번만 재생: 끝나면 기록이 멈춤
            self.player.set_sensor_power(self.power.is_on("camera"), ts)
            _, step = self.player.current(ts)
            try:
                sample = self.player.read(ts)
            except SampleSkipped:
                return
            empty = is_empty(sample.pressure.values)
            self.seated_now = not empty
            if cal is not None:                         # 세션 없이 측정: 기준 자세만 (기록 안 함)
                cal.update(sample, step)
                return
            if state != SessionState.RUNNING.value:     # 일시정지: 압력만 보고 기록·판단 안 함
                return
            self.engine.update(sample, step)
            if self.recorder:
                self.recorder.on_tick(ts, sample, self.engine.latest, self.engine.seating, empty)
            if self._pending_stop:
                reason, self._pending_stop = self._pending_stop, None
                self.stop_session(reason)

    def _run(self) -> None:
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                self.tick()
            except Exception:  # noqa: BLE001 — 한 순간 오류로 서버가 멈추지 않게
                log.exception("mock 데이터 생성 오류")
            self._stop.wait(max(0.0, self.period - (time.monotonic() - t0)))

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="mock-runtime", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """서버 종료: 진행 중인 세션은 정지로 닫고 모아 둔 것을 쓴다"""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        with self.lock:
            if self.session.state is not SessionState.IDLE:
                self.stop_session("stop")
            if self.recorder:
                self.recorder.close(self.clock.now())


def scenario_info(s: Scenario) -> dict:
    return {"name": s.name, "description": s.description, "total_sec": s.total_sec,
            "steps": [{"seconds": st.seconds, "phase": st.phase, "seat": st.seat, "head": st.head,
                       "tilt": st.tilt, "faults": st.faults} for st in s.steps]}


ERRORS = {409: {"model": ErrorResponse, "description": "지금 상태에서 할 수 없음 (detail.code 참고)"},
          422: {"model": ErrorResponse, "description": "잘못된 값 (detail.code 참고)"}}


def create_app(cfg: dict, scenario: str = "demo", loop: bool = True, seed: int | None = None,
               *, clock: AppClock | None = None, autostart: bool = False,
               db_path: str | Path | None = None) -> FastAPI:
    runtime = MockRuntime(cfg, scenario, loop, seed, clock=clock, db_path=db_path)
    if autostart:                                   # 예전처럼 바로 데이터: PC 시계로 동기화 + 세션 시작
        runtime.clock.sync(time.time(), "Asia/Seoul")
        runtime.start_session()

    def fail(status: int, code: str, message: str, **extra):
        raise HTTPException(status, detail={"code": code, "message": message,
                                            "resync_required": not runtime.clock.synced,
                                            "boot_id": runtime.clock.boot_id, **extra})

    def session_call(fn) -> dict:
        try:
            fn()
        except SessionError as e:
            fail(409, e.code, str(e))
        return runtime.session_snapshot()

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
        ts = runtime.now()
        with runtime.lock:
            e = runtime.engine
            latest = e.latest
            return {
                "mode": "mock",
                "server_time": ts,
                "last_record_ts": latest["ts"] if latest else None,
                "baseline_ready": runtime.current_baseline is not None,
                "sensors": {
                    "pressure": {"ok": e.sensor_ok("pressure", ts),
                                 "detail": f"FSR {cfg['pressure']['n_channels']}채널 (MCP3008)"},
                    "distance": {"ok": e.sensor_ok("distance", ts), "detail": runtime.player.distance_sensor},
                    "camera": {"ok": e.sensor_ok("camera", ts), "detail": "Pi Camera V2"},
                },
                "scenario": runtime.scenario.name,
                "scenario_elapsed_sec": (None if runtime.player.t0 is None
                                         else round(runtime.player.elapsed(ts), 1)),
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
        cutoff = runtime.now() - seconds
        return {"seconds": seconds, "items": [i for i in items if i["ts"] >= cutoff]}

    @app.get("/seating", response_model=Seating, tags=["착석"],
             summary="지금 착석 구간 (자리 비움 5초 규칙, 정상/비정상/unknown)")
    def seating():
        ts = runtime.now()
        with runtime.lock:
            return runtime.engine.seating.snapshot(ts)

    @app.get("/seating/segments", response_model=SeatingSegments, tags=["착석"],
             summary="끝난 착석 구간 목록")
    def seating_segments(hours: int = Query(24, ge=1, le=48, description="최근 몇 시간 안에 끝난 구간")):
        with runtime.lock:
            items = runtime.engine.seating.finished(since_ts=runtime.now() - hours * 3600)
        return {"hours": hours, "items": items}

    @app.get("/baseline", response_model=Baseline, tags=["기준 자세"], summary="기준(바른) 자세 측정값")
    def baseline():
        ts = runtime.now()
        with runtime.lock:
            e = runtime.baseline_engine
            b = runtime.current_baseline
            state = "measuring" if e.measuring else ("ready" if b else "none")
            out = {"state": state, "remaining_sec": e.baseline_remaining(ts),
                   "waiting_seat": e.measure is not None and e.measure["state"] == "waiting_seat"}
            if b:
                out.update(created_at=b.created_at, seconds=b.seconds, pressure=b.pressure,
                           distance_mm=b.distance_mm, pose=b.pose)
            return out

    @app.post("/calibrate", response_model=CalibrateResult, tags=["기준 자세"],
              summary="기준 자세 측정 (앉으면 10초). 세션 없이(온보딩) 또는 세션 중(설정의 다시 측정)",
              responses=ERRORS)
    def calibrate():
        try:
            return {"started": True, "seconds": runtime.calibrate()}
        except SessionError as e:
            fail(409, e.code, str(e))

    @app.post("/time/sync", response_model=TimeStatus, tags=["시각"], responses=ERRORS,
              summary="앱 시각·시간대 보내기 (연결할 때, 세션 시작 전마다)")
    def time_sync(req: TimeSync):
        try:
            change = runtime.sync_time(req.app_time, req.timezone)
        except ClockError as e:
            fail(e.status, e.code, str(e), **e.extra)
        return {**runtime.clock.snapshot(), **change}

    @app.get("/time", response_model=TimeStatus, tags=["시각"],
             summary="시각 동기화 상태 (boot_id가 바뀌었거나 synced=false면 다시 동기화)")
    def time_status():
        return runtime.clock.snapshot()

    @app.get("/session", response_model=SessionStatus, tags=["세션"], summary="세션 상태·경과 시간·센서 전원")
    def session_status():
        return runtime.session_snapshot()

    @app.post("/session/start", response_model=SessionStatus, tags=["세션"], responses=ERRORS,
              summary="측정 시작 (시각 동기화 후에만). 기준 자세 측정부터")
    def session_start():
        return session_call(runtime.start_session)

    @app.post("/session/pause", response_model=SessionStatus, tags=["세션"], responses=ERRORS,
              summary="일시정지: 기록·판단 멈춤, 카메라·거리 끔 (착석 구간은 여기서 끝)")
    def session_pause():
        return session_call(runtime.pause_session)

    @app.post("/session/resume", response_model=SessionStatus, tags=["세션"], responses=ERRORS,
              summary="재개")
    def session_resume():
        return session_call(runtime.resume_session)

    @app.post("/session/stop", response_model=SessionStatus, tags=["세션"], responses=ERRORS,
              summary="정지: 세션 끝, 센서 모두 끔 (앉아도 자동으로 다시 시작하지 않음)")
    def session_stop():
        return session_call(runtime.stop_session)

    @app.post("/records/reset", response_model=RecordsResetResult, tags=["기록"], responses=ERRORS,
              summary="기록 초기화 (설정·기준 자세는 유지). 세션이 없을 때만, 확인 문구 필요")
    def records_reset(req: RecordsReset):
        if req.confirm != "DELETE_RECORDS":
            fail(422, "CONFIRM_REQUIRED", "confirm에 'DELETE_RECORDS'를 넣어야 기록을 지움")
        try:
            deleted = runtime.reset_records()
        except SessionError as e:
            fail(409, e.code, str(e))
        return {"deleted": deleted, "resync_required": True}

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
    ap.add_argument("--db", default="data/synthetic/sitsense_mock.db",
                    help="SQLite 파일 (기본: data/synthetic/sitsense_mock.db — 실제 기기 DB와 따로, "
                         "-wal·-shm 파일까지 git에서 무시되는 폴더)")
    ap.add_argument("--no-db", action="store_true", help="저장하지 않음")
    ap.add_argument("--autostart", action="store_true",
                    help="켜자마자 PC 시계로 시각 동기화 + 세션 시작 (앱 없이 바로 데이터 보기)")
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
    db_path = None if args.no_db else repo_path(args.db)
    app = create_app(cfg, args.scenario, loop=not args.no_loop, seed=args.seed, autostart=args.autostart,
                     db_path=db_path)
    log.info("저장: %s", db_path or "안 함 (--no-db)")
    if not args.autostart:
        log.info("앱이 POST /time/sync → POST /session/start를 보내면 측정을 시작함 (바로 보려면 --autostart)")
    log.info("폰에서 접속: http://%s:%d/current   (API 문서: http://localhost:%d/docs)",
             lan_ip(), args.port, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
