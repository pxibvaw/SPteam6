"""센서 전원 정책 — 세션 상태와 착석 구간을 보고 매 순간 어떤 센서를 켤지 정한다

| 상황                                  | 압력 | 카메라·거리 |
| 세션 없음 / 정지                       | 끔   | 끔          |
| 실행 중, 착석 구간 안 (5초 미만 비움 포함) | 켬   | 켬          |
| 실행 중, 착석 구간 밖 (비움 5초 / 아직 안 앉음) | 켬 | 끔        |
| 일시정지                               | 켬   | 끔          |  ← 압력만 읽고 기록·판단은 안 함
| 세션 없이 기준 자세 측정 (온보딩)          | 켬   | 앉으면 켬    |  ← 측정만, 기록 안 함

다시 앉으면 착석 구간이 시작될 때(압력 1초 확인) 카메라·거리를 켠다.
센서는 hardware/sensors/power.py의 Switchable이면 무엇이든 된다 (mock은 SimulatedSwitch).
"""
from __future__ import annotations

from collections import deque

from hardware.mock.generator import CAMERA_WARMUP_SEC
from hardware.sensors.power import SimulatedSwitch, Switchable

SENSORS = ("pressure", "camera", "distance")


def decide(session_state: str, in_segment: bool) -> dict[str, bool]:
    """세션 상태(idle/running/paused, 세션 없이 측정 중이면 calibrating) + 착석 구간 안인지 → 센서별 켤지"""
    if session_state in ("running", "calibrating"):
        return {"pressure": True, "camera": in_segment, "distance": in_segment}
    if session_state == "paused":
        return {"pressure": True, "camera": False, "distance": False}
    return {"pressure": False, "camera": False, "distance": False}


class PowerPolicy:
    def __init__(self, switches: dict[str, Switchable] | None = None):
        self.switches = switches or {
            "pressure": SimulatedSwitch("pressure"),
            "camera": SimulatedSwitch("camera", warmup_sec=CAMERA_WARMUP_SEC),
            "distance": SimulatedSwitch("distance"),
        }
        self.events: deque[tuple[float, str, bool]] = deque(maxlen=500)   # (시각, 센서, 켬/끔)

    def apply(self, ts: float, session_state: str, in_segment: bool) -> dict[str, bool]:
        want = decide(session_state, in_segment)
        for name, on in want.items():
            sw = self.switches[name]
            if on != sw.is_on:
                (sw.power_on if on else sw.power_off)(ts)
                self.events.append((ts, name, on))
        return want

    def is_on(self, name: str) -> bool:
        return self.switches[name].is_on

    def states(self, ts: float) -> dict[str, str]:
        return {name: self.switches[name].state(ts) for name in SENSORS}
