"""압력센서 — FSR 406 × 6 → MCP3208 (SPI)

수집 스크립트(collect_fsr_hcsr.py)에서 실제로 돌려 본 읽기 방식 그대로다.
    - 한 채널당 SPI 전송 1번, 12비트(0~4095), 평균 내지 않음 (학습 데이터와 같은 값이 나오도록)
    - 읽는 채널·SPI 번호·속도는 config.yaml의 pressure에서 가져온다
        channels   [0, 1, 3, 4, 6, 7]   순서 = p0, p1, … (CSV 열·positions 순서)
        spi_bus    0
        spi_device 0                    (CE0)
        spi_speed_hz  (선택, 기본 500000)

spidev는 Pi에만 있어서 __init__ 안에서 import한다 → 이 파일은 PC에서도 불러올 수 있다.
테스트할 때는 spi=에 가짜 SPI 객체(open/xfer2/close)를 넘긴다.
"""
from __future__ import annotations

import logging

from common.sensors_base import PressureReading, PressureSensor, SensorError, now

log = logging.getLogger("sitsense.pressure")

DEFAULT_SPEED_HZ = 500_000          # 3.3V에서 안전한 속도 (수집 때 쓴 값)


def mcp3208_command(ch: int) -> list[int]:
    """MCP3208 single-ended 읽기 명령 3바이트 (start bit + SGL/DIFF + D2..D0)"""
    return [0x06 | (ch >> 2), (ch & 3) << 6, 0]


def mcp3208_value(reply: list[int]) -> int:
    """응답 3바이트 → 12비트 값"""
    return ((reply[1] & 0x0F) << 8) | reply[2]


class Mcp3208Pressure(PressureSensor):
    """cfg: load_config() 결과. spi: 테스트용 가짜 SPI (없으면 spidev로 연다)"""

    def __init__(self, cfg: dict, *, spi=None):
        pr = cfg["pressure"]
        self.channels: list[int] = list(pr["channels"])
        self.adc_max: int = pr["adc_max"]
        if spi is None:
            try:
                import spidev                       # Pi에서만 설치돼 있음
            except ImportError as e:
                raise SensorError("spidev가 없음 — Pi에서 sudo apt install python3-spidev "
                                  "(또는 pip install spidev), raspi-config에서 SPI 켜기") from e
            spi = spidev.SpiDev()
            bus, dev = pr.get("spi_bus", 0), pr.get("spi_device", 0)
            try:
                spi.open(bus, dev)
            except OSError as e:
                raise SensorError(f"SPI {bus}.{dev}을 열 수 없음 (raspi-config에서 SPI 켰는지, "
                                  f"배선 확인): {e}") from e
            spi.max_speed_hz = int(pr.get("spi_speed_hz", DEFAULT_SPEED_HZ))
            spi.mode = 0
        self.spi = spi
        log.info("MCP3208 열림: 채널 %s", self.channels)

    def read(self) -> PressureReading:
        try:
            values = [mcp3208_value(self.spi.xfer2(mcp3208_command(ch))) for ch in self.channels]
        except OSError as e:
            raise SensorError(f"SPI 읽기 실패: {e}") from e
        return PressureReading(ts=now(), values=values)

    def close(self) -> None:
        try:
            self.spi.close()
        except Exception as e:  # noqa: BLE001 — 닫다 실패해도 종료는 계속
            log.warning("SPI 닫기 실패: %s", e)
