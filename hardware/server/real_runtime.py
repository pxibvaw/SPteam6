"""real 모드 — 실제 센서(SensorHub)를 읽어 서버를 돌린다

mock 서버(mock_server.py)와 같은 API·세션·기준 자세·착석 구간·DB 흐름을 그대로 쓰고,
데이터 출처만 시나리오 재생 → 실제 센서로 바꾼다.

    MockRuntime  ← ScenarioPlayer(시나리오) + MockEngine(가짜 판단)
    RealRuntime  ← RealSource(SensorHub)    + RealEngine(AI 엔진 ai/engine/sitsense_engine.py)

판단(좌면·목·기울기·화면 거리)은 AI 엔진이 한다. 엔진 결과는 이미 4초 유지를 거친 확정 상태라서
서버의 3초 필터는 건너뛴다 (PassThroughFilter). 카메라가 없으면 목·기울기는 unknown.

센서 전원 정책(power.py)은 센서를 GatedSensor로 감싸서 적용한다:
  꺼진 거리센서는 읽지 않고 '실패'로, 꺼진 카메라는 None으로 채운다 (SensorHub 규칙 그대로).
"""
from __future__ import annotations

import dataclasses
import logging
from pathlib import Path

from common.schema import DistanceLevel, HeadState, SeatState, TiltState
from common.sensors_base import DistanceReading, Sample, Sensor, SensorHub, now
from hardware.mock.generator import CAMERA_WARMUP_SEC
from hardware.mock.scenarios import Step
from hardware.sensors.power import OFF, ON, WARMING, Switchable
from hardware.server.clock import AppClock
from hardware.server.mock_engine import MockEngine
from hardware.server.mock_server import MockRuntime
from hardware.server.power import PowerPolicy
from hardware.server.seating import is_empty

log = logging.getLogger("sitsense.real")

_STEP = Step(1.0)               # real 모드에는 시나리오가 없다 (엔진 호출 모양만 맞춤)


# ---------------------------------------------------------------------------
# 센서 전원 연결
# ---------------------------------------------------------------------------
class GatedSensor(Sensor, Switchable):
    """센서를 감싸서 전원 정책(Switchable)에 연결한다.

    꺼져 있으면 안쪽 센서를 읽지 않고 off_value()를 준다.
    안쪽 센서에 power_on(ts)/power_off(ts)가 있으면 같이 부른다 (카메라 멈춤, 트리거 펄스 중단 등).
    """

    def __init__(self, name: str, inner: Sensor | None, off_value=lambda: None,
                 warmup_sec: float = 0.0):
        self.name = name
        self.inner = inner
        self.off_value = off_value
        self.warmup_sec = warmup_sec
        self._on = False
        self._on_at: float | None = None
        if hasattr(inner, "power_off"):
            inner.power_off(0.0)                      # 세션 시작 전에는 꺼 둔다 (거리 트리거 펄스·카메라)

    @property
    def is_on(self) -> bool:
        return self._on

    def power_on(self, ts: float) -> None:
        if self._on:
            return
        self._on, self._on_at = True, ts
        if hasattr(self.inner, "power_on"):
            self.inner.power_on(ts)
        log.info("%s 켬", self.name)

    def power_off(self, ts: float) -> None:
        if not self._on:
            return
        self._on, self._on_at = False, None
        if hasattr(self.inner, "power_off"):
            self.inner.power_off(ts)
        log.info("%s 끔", self.name)

    def state(self, ts: float) -> str:
        if not self._on:
            return OFF
        return WARMING if ts - self._on_at < self.warmup_sec else ON

    def read(self):
        if not self._on or self.inner is None:
            return self.off_value()
        return self.inner.read()

    def close(self) -> None:
        if self.inner is not None:
            self.inner.close()


class RealSource:
    """ScenarioPlayer 자리에 들어가는, 실제 센서용 읽기 창구 (서버가 쓰는 부분만 같은 모양)"""

    def __init__(self, hub: SensorHub, clock: AppClock, cfg: dict):
        self.hub = hub
        self.clock = clock
        self.distance_sensor = str(cfg["distance"].get("sensor", "hc-sr04")).lower()
        self.t0: float | None = None

    def start(self, t0: float | None = None) -> None:
        self.t0 = now() if t0 is None else t0

    def elapsed(self, ts: float) -> float:
        return 0.0 if self.t0 is None else ts - self.t0

    def finished(self, ts: float) -> bool:
        return False                                  # 실제 센서는 끝나지 않는다

    def current(self, ts: float) -> tuple[int, Step]:
        return 0, _STEP

    def hold(self, step, until_ts) -> None:
        pass                                          # mock에서만 쓰는 '바른 자세 유지'

    def set_sensor_power(self, on: bool, ts: float) -> None:
        pass                                          # 전원은 GatedSensor가 처리

    def read(self, ts: float | None = None) -> Sample:
        """실제 센서를 읽고 시각을 앱 기준으로 바꾼다 (읽은 객체는 고치지 않고 복사해서)"""
        s = self.hub.read()                           # 압력 실패는 SampleSkipped로 나옴
        d = self.clock.now() - now()
        return Sample(
            ts=s.ts + d,
            pressure=dataclasses.replace(s.pressure, ts=s.pressure.ts + d),
            distance=dataclasses.replace(s.distance, ts=s.distance.ts + d),
            pose=None if s.pose is None else dataclasses.replace(s.pose, ts=s.pose.ts + d),
        )

    def close(self) -> None:
        pass                                          # 센서는 RealRuntime.stop()이 닫는다


# ---------------------------------------------------------------------------
# 판단 (AI 엔진 연결 전)
# ---------------------------------------------------------------------------
class PassThroughFilter:
    """AI 엔진이 이미 유지 시간(4초)을 거친 확정 상태를 주므로 서버는 시간 필터를 또 걸지 않는다
    (또 걸면 반영이 8초 넘게 늦어짐 — docs/ai_engine.md)"""

    def update(self, new_state, ts: float):
        return new_state

    def pending(self, ts: float):
        return None


class RealEngine(MockEngine):
    """MockEngine의 기준 자세 흐름·착석 구간·응답 만들기는 그대로 쓰고, 판단만 AI 엔진(SitSenseEngine)으로 바꾼다.

    - 기준 측정: 서버의 품질 검사(no_person / sensor_lost / too_much_motion)를 통과하면 AI 엔진 calibrate()
      → 실패하면 pressure_low / no_person (직전 기준 유지)
    - 매 순간: AI 엔진 결과(좌면·목·기울기 확정 상태, pending, 거리 단계)를 그대로 쓴다
    - 판단 엔진을 못 불러오면(모델 파일 없음 등) 서버를 켤 때 바로 에러로 알린다
    """

    def __init__(self, cfg: dict, seed: int | None = None, *, auto_baseline: bool = True,
                 baseline=None, require_camera: bool = True):
        from ai.engine.sitsense_engine import SitSenseEngine    # 모델·sklearn을 real 모드에서만 불러옴
        super().__init__(cfg, seed, auto_baseline=auto_baseline, baseline=baseline)
        self.ai = SitSenseEngine(cfg, require_camera=require_camera)
        self.require_pose = require_camera
        self.filters = {k: PassThroughFilter() for k in ("seat", "head", "tilt")}
        self._r = None
        if baseline is not None:                    # 저장된 기준으로 시작 (서버 재시작 등)
            self.ai.restore(baseline.pressure, baseline.distance_mm, baseline.pose)

    # --- 기준 자세 ----------------------------------------------------------
    def on_calibrate(self, samples):
        res = self.ai.calibrate(samples)
        if not res.ok:
            log.warning("AI 기준 측정 실패: %s (%s)", res.reason, res.message)
        return res.reason

    # --- 판단 ---------------------------------------------------------------
    def update(self, sample: Sample, step: Step) -> dict:
        self._r = self.ai.update(sample)
        return super().update(sample, step)

    def judge_seat(self, sample: Sample, step: Step):
        if is_empty(sample.pressure.values):        # 기준 측정 전에도 빈 의자는 알아야 함 (착석 구간)
            return SeatState.EMPTY, 0.95
        r = self._r
        return SeatState(r.seat_state), float(r.confidence["seat"])

    def judge_upper(self, sample: Sample, step: Step):
        r = self._r
        return (HeadState(r.head_state), TiltState(r.tilt_state),
                float(r.confidence["head"]), float(r.confidence["tilt"]), r.deltas, r.cues)

    def pending_list(self, ts: float) -> list[dict]:
        return list(self._r.pending) if self._r is not None else []

    def distance_level_of(self, dist_mm):
        # 화면 거리는 엔진의 4초 유지 단계를 쓴다 (잠깐 숙일 때 경고가 깜빡이지 않게)
        return DistanceLevel(self._r.distance_level) if self._r is not None else DistanceLevel.UNKNOWN


# ---------------------------------------------------------------------------
class RealRuntime(MockRuntime):
    mode = "real"
    engine_cls = RealEngine

    def __init__(self, cfg: dict, hub: SensorHub, *, clock: AppClock | None = None,
                 db_path: str | Path | None = None, seed: int | None = None):
        has_camera = hub.pose is not None
        self.has_camera = has_camera
        self.camera_detail = "Pi Camera V2" if has_camera else "연결 안 됨 (카메라 없이 실행)"
        # 전원 정책이 끄고 켤 수 있게 거리·카메라를 감싼다 (압력은 서버가 켜져 있을 때만 읽음)
        self.gates = {
            "pressure": GatedSensor("pressure", hub.pressure),
            "distance": GatedSensor("distance", hub.distance,
                                    off_value=lambda: DistanceReading.failed(now())),
            "camera": GatedSensor("camera", hub.pose, warmup_sec=CAMERA_WARMUP_SEC),
        }
        hub.pressure = self.gates["pressure"]
        hub.distance = self.gates["distance"]
        hub.pose = self.gates["camera"] if has_camera else None
        self.hub = hub
        super().__init__(cfg, scenario="real", loop=False, seed=seed, clock=clock, db_path=db_path)

    def _make_power(self) -> PowerPolicy:
        return PowerPolicy(dict(self.gates))

    def _new_player(self):
        return RealSource(self.hub, self.clock, self.cfg)

    def _new_engine(self):
        # 카메라가 없으면 기준 자세에 사람 인식을 요구하지 않음 (목·기울기는 unknown)
        cfg = self.cfg
        hold = self.session_settings.get("posture_hold_sec")
        if hold is not None and hold != cfg["thresholds"]["short_filter_sec"]:
            import copy
            cfg = copy.deepcopy(cfg)
            cfg["thresholds"]["short_filter_sec"] = hold        # 앱 설정이 config.yaml보다 우선
        e = RealEngine(cfg, seed=self.seed, auto_baseline=False, baseline=self.current_baseline,
                       require_camera=self.has_camera)
        e.on_measure_start = self._on_measure_start
        e.on_baseline = self._on_baseline
        return e

    def _on_measure_start(self, ts: float, seconds: float) -> None:
        pass                                          # 사람이 직접 바르게 앉는다

    def select(self, name: str, loop: bool = True, seed: int | None = None):
        """시나리오가 없다. 읽기 창구와 엔진만 새로 만든다 (MockRuntime.__init__이 부름)"""
        with self.lock:
            old = getattr(self, "engine", None)
            self.scenario, self.loop, self.seed = None, False, seed
            self.player = self._new_player()
            self.engine = self._new_engine()
            if old is not None:
                self.engine.seating = old.seating
        return None

    def stop(self) -> None:
        super().stop()
        self.hub.close()
