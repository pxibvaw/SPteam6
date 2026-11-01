"""실제 센서 묶음(SensorHub)을 만든다 — main.py --mode real, 서버 real 모드가 같이 쓴다

각 센서 클래스는 아래 약속을 지켜서 만든다 (모듈이 아직 없으면 어떤 파일이 필요한지 알려 준다).

    hardware/sensors/pressure.py   class Mcp3208Pressure(PressureSensor):  __init__(self, cfg)
    hardware/sensors/distance.py   class Hcsr04Distance(DistanceSensor):   __init__(self, cfg)

- 클래스는 common.sensors_base의 PressureSensor / DistanceSensor를 상속하고 read()가 값 한 개를 준다.
- spidev, gpiozero 같은 Pi 전용 라이브러리는 __init__ 안에서 import한다
  (그래야 이 파일과 서버 코드를 Pi 없는 PC에서도 불러올 수 있고, mock 시험이 PC에서 돈다).
- 전원 정책이 끄고 켤 수 있게 power_on(ts) / power_off(ts)를 두어도 된다 (없어도 됨).
- 카메라(PoseSource)는 ai/camera가 준비되면 여기서 같이 만든다. 지금은 pose=None.
"""
from __future__ import annotations

import importlib
import logging

from common.sensors_base import SensorError, SensorHub

log = logging.getLogger("sitsense.sensors")

# (모듈, 클래스, 설명) — 센서가 추가되면 여기만 고친다
PRESSURE = ("hardware.sensors.pressure", "Mcp3208Pressure", "압력센서(MCP3208 + FSR)")
DISTANCE = ("hardware.sensors.distance", "Hcsr04Distance", "거리센서(HC-SR04)")


def _load(spec: tuple[str, str, str], cfg: dict):
    module, cls, label = spec
    try:
        mod = importlib.import_module(module)
    except ModuleNotFoundError as e:
        if e.name == module:                              # 우리 파일이 아직 없음
            raise SensorError(f"{label} 코드가 아직 없음: {module.replace('.', '/')}.py에 "
                              f"{cls} 클래스가 필요함 (hardware/sensors/factory.py 참고)") from e
        raise SensorError(f"{label}에 필요한 라이브러리 '{e.name}'가 없음 — Pi에서 "
                          f"pip install -r requirements-pi.txt 를 했는지 확인") from e
    try:
        return getattr(mod, cls)(cfg)
    except AttributeError as e:
        raise SensorError(f"{module}에 {cls} 클래스가 없음") from e


def build_hub(cfg: dict, *, pose=None) -> SensorHub:
    """실제 센서를 열어서 SensorHub로 묶는다. 하나라도 못 열면 SensorError.

    pose: 카메라(PoseSource)를 이미 만들었으면 넘긴다 (없으면 카메라 없이 동작).
    """
    pr = cfg["pressure"]
    pressure = _load(PRESSURE, cfg)
    try:
        distance = _load(DISTANCE, cfg)
    except Exception:
        pressure.close()                                  # 하나 열고 다른 게 실패하면 닫고 나간다
        raise
    return SensorHub(pressure, distance, pose, n_channels=pr["n_channels"], adc_max=pr["adc_max"])
