"""압력센서 클래스 확인 — 가짜 MCP3208(SPI)로 PC에서 시험한다 (Pi·spidev 필요 없음)

가짜 SPI가 MCP3208의 명령 형식(start bit, SGL, 채널 D2~D0)을 해석해서 채널별로 정해 둔 값을 돌려준다.
→ 명령 바이트·12비트 조립·채널 순서·실패 처리가 맞는지, 서버 real 경로(SensorHub)에 끼워지는지 본다.

사용 예 (저장소 최상위):
    python -m hardware.sensors.check_pressure
"""
from __future__ import annotations

import sys

from common.config import ConfigError, load_config
from common.sensors_base import SampleSkipped, SensorError, SensorHub
from hardware.sensors.factory import _load, DISTANCE
from hardware.sensors.pressure import Mcp3208Pressure, mcp3208_command, mcp3208_value
from common.mock_sensors import MockDistanceSensor

ADC_VALUES = {0: 3400, 1: 3500, 2: 111, 3: 3600, 4: 3450, 5: 222, 6: 3550, 7: 4095}   # CH2·CH5는 읽으면 안 됨


class FakeSpi:
    """MCP3208 흉내: [0x06|D2, D1D0<<6, 0] → [0, 상위 4비트, 하위 8비트]"""

    def __init__(self, values: dict[int, int]):
        self.values = values
        self.asked: list[int] = []
        self.closed = False
        self.fail = False

    def xfer2(self, cmd):
        if self.fail:
            raise OSError("SPI 오류 흉내")
        assert len(cmd) == 3 and cmd[0] & 0x06 == 0x06 and cmd[2] == 0, f"잘못된 명령 {cmd}"
        ch = ((cmd[0] & 1) << 2) | (cmd[1] >> 6)
        self.asked.append(ch)
        v = self.values[ch]
        return [0, (v >> 8) & 0x0F, v & 0xFF]

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

    ok("명령 바이트 CH0", mcp3208_command(0) == [0x06, 0x00, 0], mcp3208_command(0))
    ok("명령 바이트 CH7", mcp3208_command(7) == [0x07, 0xC0, 0], mcp3208_command(7))
    ok("명령 바이트 CH3", mcp3208_command(3) == [0x06, 0xC0, 0], mcp3208_command(3))
    ok("12비트 조립 (0x0FFF)", mcp3208_value([0, 0xFF, 0xFF]) == 4095, mcp3208_value([0, 0xFF, 0xFF]))

    spi = FakeSpi(ADC_VALUES)
    s = Mcp3208Pressure(cfg, spi=spi)
    r = s.read()
    want = [ADC_VALUES[c] for c in cfg["pressure"]["channels"]]
    ok("config 채널 순서대로 읽음 (p0=CH0 … p5=CH7)", r.values == want, r.values)
    ok("CH2·CH5는 읽지 않음", 2 not in spi.asked and 5 not in spi.asked, spi.asked)
    ok("채널당 SPI 전송 1번", len(spi.asked) == 6, len(spi.asked))
    ok("12비트 최댓값 4095 그대로", r.values[-1] == 4095, r.values[-1])

    hub = SensorHub(s, MockDistanceSensor(seed=1), None,
                    n_channels=cfg["pressure"]["n_channels"], adc_max=cfg["pressure"]["adc_max"])
    smp = hub.read()
    ok("SensorHub에서 검증 통과 (6채널, 0~4095)", smp.pressure.values == want, smp.pressure.values)

    spi.fail = True
    try:
        s.read()
        ok("SPI 오류 → SensorError", False, "예외 없음")
    except SensorError:
        ok("SPI 오류 → SensorError", True, "SensorError")
    try:
        hub.read()
        ok("SensorHub: 압력 실패는 그 순간만 건너뜀", False, "예외 없음")
    except SampleSkipped:
        ok("SensorHub: 압력 실패는 그 순간만 건너뜀", True, "SampleSkipped")
    spi.fail = False
    ok("복구 후 다시 읽힘", hub.read().pressure.values == want, "ok")

    s.close()
    ok("close가 SPI를 닫음", spi.closed, spi.closed)

    try:
        Mcp3208Pressure(cfg)
        ok("spidev 없는 PC에서 열면 SensorError", False, "열림")
    except SensorError as e:
        ok("spidev 없는 PC에서 열면 SensorError", "spidev" in str(e) or "SPI" in str(e), str(e)[:40])
    except Exception as e:  # noqa: BLE001
        ok("spidev 없는 PC에서 열면 SensorError", False, repr(e))

    try:
        _load(DISTANCE, cfg)
        ok("거리센서 클래스 불러오기 (PC에서는 gpiozero 안내)", False, "열림")
    except SensorError as e:
        ok("거리센서 클래스 불러오기 (PC에서는 gpiozero 안내)", "gpiozero" in str(e), str(e)[:30])

    bad = [r for r in rows if not r[0]]
    print(f"\n{len(rows) - len(bad)}/{len(rows)} 통과")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
