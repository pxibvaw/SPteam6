"""거리센서 클래스 확인 — 가짜 gpiozero 센서로 PC에서 시험한다 (Pi·gpiozero 필요 없음)

사용 예 (저장소 최상위):
    python -m hardware.sensors.check_distance
"""
from __future__ import annotations

import sys

from common.config import ConfigError, load_config
from common.mock_sensors import MockPressureSensor
from common.config import fsr_positions
from common.sensors_base import SensorError, SensorHub
from hardware.sensors.distance import STATUS_NO_ECHO, STATUS_TOO_CLOSE, Hcsr04Distance
from hardware.sensors.factory import build_hub


class FakeGpioSensor:
    """gpiozero DistanceSensor 흉내: .distance(m), .close()"""

    def __init__(self, meters: float):
        self.meters = meters
        self.closed = False
        self.fail = False

    @property
    def distance(self):
        if self.fail:
            raise OSError("GPIO 오류 흉내")
        return self.meters

    def close(self):
        self.closed = True


def main() -> int:
    try:
        cfg = load_config()
    except ConfigError as e:
        print(e)
        return 1
    rows: list[tuple[bool, str, str]] = []

    def ok(name: str, cond: bool, got) -> None:
        rows.append((bool(cond), name, str(got)))
        print(f"  {'✓' if cond else '⚠️'} {name:<46} {got}")

    made: list[FakeGpioSensor] = []

    def factory():
        s = FakeGpioSensor(0.612)
        made.append(s)
        return s

    d = Hcsr04Distance(cfg, sensor_factory=factory)
    r = d.read()
    ok("정상 값: m → mm 정수, status 0", r.distance_mm == 612 and r.valid and r.range_status == 0, r)

    made[-1].meters = cfg["distance"]["max_mm"] / 1000
    r = d.read()
    ok("max 거리(반사 없음) → None, status 1", r.distance_mm is None and r.range_status == STATUS_NO_ECHO and not r.valid, r)

    made[-1].meters = 0.012
    r = d.read()
    ok("min 미만(너무 가까움) → None, status 2", r.distance_mm is None and r.range_status == STATUS_TOO_CLOSE, r)

    made[-1].meters = cfg["distance"]["min_mm"] / 1000
    ok("min 경계값은 정상", d.read().valid, d.read().distance_mm)

    made[-1].fail = True
    try:
        d.read()
        ok("GPIO 오류 → SensorError", False, "예외 없음")
    except SensorError:
        ok("GPIO 오류 → SensorError", True, "SensorError")
    made[-1].fail = False

    # 허브: 거리 실패는 distance_mm=None, status -1로 기록하고 계속 (압력은 정상)
    pressure = MockPressureSensor(fsr_positions(cfg), cfg["pressure"]["adc_max"], seed=1)
    hub = SensorHub(pressure, d, None, n_channels=cfg["pressure"]["n_channels"], adc_max=cfg["pressure"]["adc_max"])
    made[-1].meters = 0.55
    ok("SensorHub에서 읽힘 (550mm)", hub.read().distance.distance_mm == 550, hub.read().distance.distance_mm)
    made[-1].fail = True
    smp = hub.read()
    ok("SensorHub: 거리 실패는 None·-1로 기록하고 계속", smp.distance.distance_mm is None and smp.distance.range_status == -1,
       smp.distance)
    made[-1].fail = False

    # 전원 정책
    first = made[-1]
    d.power_off(0.0)
    ok("power_off: gpiozero 닫음(펄스 중단)", first.closed, first.closed)
    try:
        d.read()
        ok("꺼진 상태에서 읽으면 SensorError", False, "예외 없음")
    except SensorError:
        ok("꺼진 상태에서 읽으면 SensorError", True, "SensorError")
    d.power_on(1.0)
    ok("power_on: 다시 열림", len(made) == 2 and d.read().valid, len(made))
    d.power_on(2.0)
    ok("power_on 두 번 불러도 한 번만 열림", len(made) == 2, len(made))

    d.close()
    ok("close가 센서를 닫음", made[-1].closed, made[-1].closed)

    # 열기 실패
    def broken():
        raise OSError("핀 사용 중")

    try:
        Hcsr04Distance(cfg, sensor_factory=broken)
        ok("열기 실패 → SensorError(핀 안내)", False, "예외 없음")
    except SensorError as e:
        ok("열기 실패 → SensorError(핀 안내)", "trigger" in str(e), str(e)[:40])

    # gpiozero 없는 PC
    try:
        Hcsr04Distance(cfg)
        ok("gpiozero 없는 PC에서 열면 SensorError", False, "열림")
    except SensorError as e:
        ok("gpiozero 없는 PC에서 열면 SensorError", "gpiozero" in str(e) or "HC-SR04" in str(e), str(e)[:40])

    # factory: 두 클래스 모두 있으면 PC에서는 spidev 때문에 압력에서 안내
    try:
        build_hub(cfg)
    except SensorError as e:
        ok("build_hub: PC에서는 압력(spidev) 안내로 멈춤", "spidev" in str(e) or "gpiozero" in str(e), str(e)[:30])

    bad = [r for r in rows if not r[0]]
    print(f"\n{len(rows) - len(bad)}/{len(rows)} 통과")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
