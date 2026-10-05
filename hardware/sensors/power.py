"""센서 전원 인터페이스 — 서버가 센서를 끄고 켤 때 쓰는 약속

실제 센서 클래스(Pi Camera, HC-SR04, MCP3008)가 Switchable을 구현하면
서버의 전원 정책(hardware/server/power.py)이 그대로 끄고 켤 수 있다.
    카메라: power_off()에서 picamera2 멈춤 + MediaPipe 처리 중단, power_on()에서 다시 시작
    거리:   power_off()에서 트리거 펄스를 그만 보냄
    압력:   power_off()에서 SPI 읽기를 멈춤
꺼진 센서는 읽지 않는다 (서버가 꺼진 값으로 채운다: 거리 None/-1, 카메라 None).

mock 단계에서는 SimulatedSwitch로 상태만 바꾼다.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod

log = logging.getLogger("sitsense.power")

ON, OFF, WARMING = "on", "off", "warming"


class Switchable(ABC):
    """끄고 켤 수 있는 센서. ts는 서버 기록 시각(앱 시각 기준 초)"""

    name: str

    @property
    @abstractmethod
    def is_on(self) -> bool: ...

    @abstractmethod
    def power_on(self, ts: float) -> None: ...

    @abstractmethod
    def power_off(self, ts: float) -> None: ...

    def state(self, ts: float) -> str:
        """on / off / warming (켰지만 아직 값이 안 나옴). 기본은 on / off"""
        return ON if self.is_on else OFF


class SimulatedSwitch(Switchable):
    """mock용: 전원 상태만 바꾼다. warmup_sec이 있으면 켠 뒤 그동안 warming"""

    def __init__(self, name: str, warmup_sec: float = 0.0):
        self.name = name
        self.warmup_sec = warmup_sec
        self._on = False
        self._on_at: float | None = None

    @property
    def is_on(self) -> bool:
        return self._on

    def power_on(self, ts: float) -> None:
        if not self._on:
            self._on, self._on_at = True, ts
            log.info("%s 켬", self.name)

    def power_off(self, ts: float) -> None:
        if self._on:
            self._on, self._on_at = False, None
            log.info("%s 끔", self.name)

    def state(self, ts: float) -> str:
        if not self._on:
            return OFF
        return WARMING if ts - self._on_at < self.warmup_sec else ON
