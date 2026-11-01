"""카메라 → MediaPipe → 가장 최근 상체 점 (PoseSource). main.py real 모드·서버가 쓰는 카메라 부품

    from ai.camera.pose_source import CameraPoseSource
    src = CameraPoseSource.from_config(cfg, camera="picam")   # Pi: picam, 맥: webcam
    src.power_on(ts)                  # 뒤에서 카메라 읽기 + MediaPipe 시작 (켠 직후 잠깐 warming)
    pose = src.read()                 # 가장 최근 PoseReading, 아직 없으면 None (기다리지 않음)
    src.power_off(ts)                 # 카메라 멈춤 + MediaPipe 처리 중단 (서버 전원 정책)
    src.close()

왜 뒤에서(스레드) 도나
    압력·거리는 0.5초마다 읽는데 MediaPipe는 Pi 4에서 한 장에 0.2~0.5초 걸린다. 같은 줄에서 돌리면
    압력 읽기가 밀린다. 그래서 카메라는 따로 돌고, SensorHub는 읽을 때 '가장 최근 결과'만 가져간다.
    PoseReading.ts는 그 프레임을 찍은 시각이라 얼마나 오래된 값인지 알 수 있다 (엔진이 오래된 값은 버림).

전원 (hardware/sensors/power.py의 Switchable 약속)
    is_on / power_on(ts) / power_off(ts) / state(ts) = on / off / warming
    warming = 켰지만 아직 첫 결과가 안 나옴. 꺼져 있으면 read()는 None.

좌우: 영상을 거울처럼 뒤집지 않는다 (MediaPipe의 left/right = 사람 기준이 유지됨). 카메라를 거꾸로 달았으면
rotate180=True.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace

from common.sensors_base import CameraSensor, PoseReading, PoseSource, SensorError

log = logging.getLogger("sitsense.camera")

ON, OFF, WARMING = "on", "off", "warming"     # hardware/sensors/power.py와 같은 값
MAX_CONSECUTIVE_FAILS = 30                    # 프레임을 이만큼 연달아 못 읽으면 카메라 오류로 멈춤


def make_camera(kind: str, cfg: dict, rotate180: bool = False) -> CameraSensor:
    """kind: picam (Pi CSI 카메라) / webcam (맥 웹캠)"""
    cam = cfg["camera"]
    if kind == "picam":
        from ai.camera.picam import PiCamera
        return PiCamera(width=cam["width"], height=cam["height"], rotate180=rotate180)
    if kind == "webcam":
        from ai.camera.pose import Webcam
        return Webcam(index=cam["index"], width=cam["width"], height=cam["height"])
    raise ValueError(f"camera는 picam 또는 webcam (지금: {kind})")


class CameraPoseSource(PoseSource):
    name = "camera"

    def __init__(self, camera_factory, extractor_factory, fps: float = 5.0, width: int = 1280,
                 height: int = 720):
        """camera_factory() → CameraSensor, extractor_factory() → .extract(frame)/.close()가 있는 객체.
        켤 때마다 새로 만든다 (끄면 카메라를 완전히 놓아서 다른 프로그램도 쓸 수 있게)."""
        if fps <= 0:
            raise ValueError("fps는 0보다 커야 함")
        self._camera_factory, self._extractor_factory = camera_factory, extractor_factory
        self.period = 1.0 / fps
        self.width, self.height = width, height       # 특징 계산(픽셀 변환)에 쓰는 해상도
        self._lock = threading.Lock()
        self._latest: PoseReading | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._on = False
        self.error: str | None = None                 # 스레드가 멈춘 이유 (없으면 None)
        self.frames = 0                               # 처리한 프레임 수 (상태 확인용)

    @classmethod
    def from_config(cls, cfg: dict, camera: str = "picam", rotate180: bool = False) -> "CameraPoseSource":
        cam = cfg["camera"]

        def extractor():
            from ai.camera.pose import PoseExtractor
            return PoseExtractor(model_complexity=cam["model_complexity"])

        return cls(lambda: make_camera(camera, cfg, rotate180), extractor, fps=cam["fps"],
                   width=cam["width"], height=cam["height"])

    # ------------------------------------------------------------------ 전원
    @property
    def is_on(self) -> bool:
        return self._on

    def power_on(self, ts: float | None = None) -> None:
        if self._on:
            return
        self._stop.clear()
        self.error = None
        with self._lock:
            self._latest = None
        self._on = True
        self._thread = threading.Thread(target=self._run, name="pose-source", daemon=True)
        self._thread.start()
        log.info("카메라 켬")

    def power_off(self, ts: float | None = None, timeout: float = 3.0) -> None:
        if not self._on:
            return
        self._on = False
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                log.warning("카메라 스레드가 %g초 안에 멈추지 않음 (다음에 켤 때 새로 만듦)", timeout)
        self._thread = None
        with self._lock:
            self._latest = None
        log.info("카메라 끔")

    def state(self, ts: float | None = None) -> str:
        if not self._on:
            return OFF
        with self._lock:
            return WARMING if self._latest is None else ON

    # ------------------------------------------------------------------ 읽기
    def read(self) -> PoseReading | None:
        """가장 최근 결과 (기다리지 않음). 꺼져 있거나 아직 결과가 없으면 None.
        스레드가 오류로 멈췄으면 SensorError (SensorHub가 카메라 오류로 처리)"""
        if self.error is not None:
            raise SensorError(f"카메라 멈춤: {self.error}")
        with self._lock:
            latest = self._latest
        # 복사본을 준다: 서버 clock.stamp()가 sample.pose.ts를 그 자리에서 바꾸는데(+= 시각 차이),
        # 같은 객체를 계속 주면 읽을 때마다 시각이 더해져 '오래된 값' 판단이 틀어진다
        return None if latest is None else replace(latest)

    def close(self) -> None:
        self.power_off()

    def _run(self) -> None:
        cam = ext = None
        try:
            cam = self._camera_factory()
            ext = self._extractor_factory()
            w, h = getattr(cam, "width", None), getattr(cam, "height", None)
            if w and h and (w, h) != (self.width, self.height):
                log.warning("카메라 해상도 %dx%d (설정 %dx%d) → 실제 값으로 특징 계산", w, h, self.width, self.height)
                self.width, self.height = w, h
            fails = 0
            while not self._stop.is_set():
                t0 = time.monotonic()
                frame = cam.read()
                if frame is None:
                    fails += 1
                    if fails >= MAX_CONSECUTIVE_FAILS:
                        raise SensorError(f"프레임을 {fails}번 연달아 못 읽음")
                    self._stop.wait(0.05)
                    continue
                fails = 0
                pose = ext.extract(frame)
                with self._lock:
                    if self._on:
                        self._latest = pose
                self.frames += 1
                self._stop.wait(max(0.0, self.period - (time.monotonic() - t0)))
        except Exception as e:  # noqa: BLE001 — 카메라·MediaPipe 오류 종류가 다양함
            self.error = f"{type(e).__name__}: {e}"
            log.error("카메라 스레드 오류: %s", self.error)
        finally:
            for obj in (ext, cam):
                if obj is not None:
                    try:
                        obj.close()
                    except Exception as e:  # noqa: BLE001
                        log.warning("카메라 정리 실패: %s", e)
