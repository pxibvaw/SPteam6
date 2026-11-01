"""거리센서 — HC-SR04 (초음파, GPIO)

수집 스크립트(collect_fsr_hcsr.py)에서 실제로 돌려 본 읽기 방식 그대로다.
    - gpiozero DistanceSensor: 배경에서 계속 재고, 최근 median_window개의 중앙값을 .distance(m)로 준다
    - 반사가 없거나 max_distance 이상이면 max_distance 그대로 나온다 → '측정 안 됨'으로 처리
    - 설정은 config.yaml의 distance
        trigger_pin 23 / echo_pin 24 (BCM)   Echo는 분압 회로로 3.3V로 낮춰서 연결
        min_mm 20 / max_mm 2000              범위 밖은 측정 안 됨
        median_window 5                      gpiozero queue_len

결과 약속 (common.sensors_base.DistanceReading)
    distance_mm  정수 mm, 측정 안 됨이면 None
    range_status 0 정상 / 1 반사 없음·범위 밖(멀다) / 2 너무 가까움 / -1 읽기 실패
    → 0이 아니면 valid=False라서 기준 거리·판정에 쓰이지 않는다.

서버 전원 정책이 거리센서를 끌 때(power_off) gpiozero를 닫아 트리거 펄스를 멈추고, 켤 때(power_on) 다시 연다.
켠 직후에는 중앙값 큐가 채워지는 짧은 시간(약 0.5초) 값이 덜 안정적이다.

gpiozero는 Pi에만 있어서 __init__ 안에서 import한다 → 이 파일은 PC에서도 불러올 수 있다.
테스트할 때는 sensor_factory=에 가짜 센서(.distance, .close())를 만드는 함수를 넘긴다.
"""
from __future__ import annotations

import logging
import warnings

from common.sensors_base import STATUS_OK, DistanceReading, DistanceSensor, SensorError, now

log = logging.getLogger("sitsense.distance")

STATUS_NO_ECHO = 1          # 반사 없음 / 최대 거리 이상
STATUS_TOO_CLOSE = 2        # 최소 거리 미만


class Hcsr04Distance(DistanceSensor):
    """cfg: load_config() 결과. sensor_factory: 테스트용 (없으면 gpiozero로 연다)"""

    def __init__(self, cfg: dict, *, sensor_factory=None):
        d = cfg["distance"]
        self.trigger_pin, self.echo_pin = d["trigger_pin"], d["echo_pin"]
        self.min_mm, self.max_mm = d["min_mm"], d["max_mm"]
        self.window = d["median_window"]
        self._factory = sensor_factory or self._gpiozero_factory
        self._sensor = None
        self._open()

    # --- 열고 닫기 ------------------------------------------------------------
    def _gpiozero_factory(self):
        try:
            from gpiozero import DistanceSensor as GpioDistance, DistanceSensorNoEcho
        except ImportError as e:
            raise SensorError("gpiozero가 없음 — Pi에서 sudo apt install python3-gpiozero "
                              "(또는 pip install gpiozero)") from e
        warnings.simplefilter("ignore", DistanceSensorNoEcho)     # 반사 없음 경고가 화면을 어지럽히지 않게
        return GpioDistance(echo=self.echo_pin, trigger=self.trigger_pin,
                            max_distance=self.max_mm / 1000.0, queue_len=self.window)

    def _open(self) -> None:
        try:
            self._sensor = self._factory()
        except SensorError:
            raise
        except Exception as e:  # noqa: BLE001 — 핀 사용 중, 핀 팩토리 없음 등
            raise SensorError(f"HC-SR04를 열 수 없음 (trigger {self.trigger_pin}, echo {self.echo_pin} 배선·"
                              f"다른 프로그램이 핀을 쓰는지 확인): {e}") from e
        log.info("HC-SR04 열림: trigger %s, echo %s, 중앙값 %d개", self.trigger_pin, self.echo_pin, self.window)

    def _shut(self) -> None:
        sensor, self._sensor = self._sensor, None
        if sensor is not None:
            try:
                sensor.close()
            except Exception as e:  # noqa: BLE001 — 닫다 실패해도 계속
                log.warning("HC-SR04 닫기 실패: %s", e)

    # --- 서버 전원 정책 ---------------------------------------------------------
    def power_off(self, ts: float | None = None) -> None:
        self._shut()                                  # 트리거 펄스를 그만 보낸다

    def power_on(self, ts: float | None = None) -> None:
        if self._sensor is None:
            self._open()

    # --- 읽기 -----------------------------------------------------------------
    def read(self) -> DistanceReading:
        ts = now()
        if self._sensor is None:
            raise SensorError("HC-SR04가 꺼져 있음 (power_on 필요)")
        try:
            meters = self._sensor.distance
        except Exception as e:  # noqa: BLE001
            raise SensorError(f"HC-SR04 읽기 실패: {e}") from e
        mm = meters * 1000.0
        if mm >= self.max_mm - 1.0:                   # gpiozero는 반사가 없으면 max_distance를 준다
            return DistanceReading(ts=ts, distance_mm=None, range_status=STATUS_NO_ECHO)
        if mm < self.min_mm:
            return DistanceReading(ts=ts, distance_mm=None, range_status=STATUS_TOO_CLOSE)
        return DistanceReading(ts=ts, distance_mm=int(round(mm)), range_status=STATUS_OK)

    def close(self) -> None:
        self._shut()
