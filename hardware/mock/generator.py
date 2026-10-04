"""시나리오 → 센서 값 (PressureReading / DistanceReading / PoseReading → Sample)

두 가지로 쓴다.
    1. mock 생성기 (실시간): ScenarioPlayer.read()  → mock 서버가 10Hz로 호출
    2. replay CSV (파일):    python -m hardware.mock.generator --scenario forward_head
                             → data/synthetic/mock_forward_head.csv + .json (수집 CSV와 같은 47칸)
                             → python main.py --mode replay --file data/synthetic/mock_forward_head.csv

값 자체는 common/mock_sensors.py의 가짜 센서를 그대로 쓰고(수정 없음),
여기서는 시나리오에 따라 라벨을 바꾸고 센서 오류·카메라 속도(초당 5장)를 흉내 낸다.
주의: 실제 자세 패턴이 아니라 코드·앱 흐름 확인용이다. 정확도 평가에 쓰면 안 된다.

사용 예:
    python -m hardware.mock.generator --list                 # 시나리오 목록
    python -m hardware.mock.generator --scenario demo        # CSV 1개 만들기
    python -m hardware.mock.generator --all --seed 1         # 전부 만들기
    python -m hardware.mock.generator --scenario normal --distance-sensor hc-sr04
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

from common.config import ConfigError, fsr_positions, load_config, repo_path
from common.mock_sensors import MockDistanceSensor, MockPoseSource, MockPressureSensor
from common.recorder import FORMAT_VERSION, SessionInfo, csv_columns, sample_to_row
from common.schema import BASELINE_LABELS, Phase, SeatState
from common.sensors_base import (
    STATUS_READ_ERROR, DistanceReading, PoseReading, Sample, SampleSkipped, now,
)
from hardware.mock.scenarios import SCENARIOS, Scenario, Step, get_scenario

log = logging.getLogger("sitsense.mock")

# 거리센서 종류별 흉내 (아직 확정 전: VL53L1X 또는 HC-SR04)
#   noise_mm: 측정 떨림, fail_status: 측정 실패 때 range_status 값
DISTANCE_SENSORS = {
    "vl53l1x": {"noise_mm": 8.0, "fail_status": 2},                  # VL53L1X range status 2 = 신호 약함
    "hc-sr04": {"noise_mm": 12.0, "fail_status": STATUS_READ_ERROR},  # 초음파: 에코가 안 오면 읽기 실패
}
BASE_DISTANCE_MM = 600          # 바른 자세일 때 화면 거리 (mock_sensors 기본값과 같음)
EMPTY_DISTANCE_MM = 1100        # 자리 비움이면 뒤쪽 벽·의자까지 거리
RANDOM_FAIL_PROB = 0.02         # 평소에도 가끔 나는 거리 측정 실패


class ScenarioPlayer:
    """시나리오를 시간에 따라 재생하며 Sample을 만든다.

    read(ts)에 시각을 넘기면 그 시각의 값을 만든다 (CSV는 가상 시각, 서버는 지금 시각).
    압력 읽기가 실패하는 순간은 SensorHub와 똑같이 SampleSkipped를 낸다.
    """

    def __init__(self, cfg: dict, scenario: Scenario, *, seed: int | None = None,
                 loop: bool = False, distance_sensor: str | None = None):
        self.cfg = cfg
        self.scenario = scenario
        self.loop = loop
        self.distance_sensor = (distance_sensor or cfg["distance"].get("sensor", "vl53l1x")).lower()
        if self.distance_sensor not in DISTANCE_SENSORS:
            raise ValueError(f"거리센서 '{self.distance_sensor}'는 흉내 낼 수 없음. 가능: {list(DISTANCE_SENSORS)}")
        spec = DISTANCE_SENSORS[self.distance_sensor]
        self.fail_status = spec["fail_status"]

        cam, pr = cfg["camera"], cfg["pressure"]
        self.pressure = MockPressureSensor(fsr_positions(cfg), pr["adc_max"], seed=seed)
        self.distance = MockDistanceSensor(base_mm=BASE_DISTANCE_MM, noise_mm=spec["noise_mm"],
                                           fail_prob=RANDOM_FAIL_PROB, seed=seed)
        self.pose = MockPoseSource(cam["width"], cam["height"], seed=seed)
        self.cam_period = 1.0 / cam["fps"]
        self.rng = np.random.default_rng(seed)

        self.t0: float | None = None
        self._last_pose: PoseReading | None = None
        self._override: tuple[Step, float] | None = None   # (Step, 끝 시각) — 기준 다시 측정 중

    # --- 시간 ---------------------------------------------------------------
    def start(self, t0: float | None = None) -> None:
        self.t0 = now() if t0 is None else t0
        self._last_pose = None

    def elapsed(self, ts: float) -> float:
        if self.t0 is None:
            self.start(ts)
        e = ts - self.t0
        return e % self.scenario.total_sec if self.loop else e

    def finished(self, ts: float) -> bool:
        return not self.loop and self.t0 is not None and ts - self.t0 >= self.scenario.total_sec

    def hold(self, step: Step, until_ts: float) -> None:
        """시나리오와 상관없이 until_ts까지 step 자세를 유지 (기준 자세 다시 측정할 때)"""
        self._override = (step, until_ts)

    def current(self, ts: float) -> tuple[int, Step]:
        """(Step 번호, Step). 기준 다시 측정 중이면 번호는 -1"""
        if self._override and ts < self._override[1]:
            return -1, self._override[0]
        self._override = None
        return self.scenario.step_at(self.elapsed(ts))

    # --- 읽기 ---------------------------------------------------------------
    def read(self, ts: float | None = None) -> Sample:
        ts = now() if ts is None else ts
        _, step = self.current(ts)
        empty = step.seat == SeatState.EMPTY.value

        # 라벨 → 가짜 센서 (기준 측정 구간은 바른 자세)
        labels = BASELINE_LABELS if step.phase == Phase.BASELINE.value else \
            {"seat": step.seat, "head": step.head, "tilt": step.tilt}
        self.pressure.label = labels["seat"]
        self.pose.head, self.pose.tilt = labels["head"], labels["tilt"]
        self.distance.offset_mm = (EMPTY_DISTANCE_MM - BASE_DISTANCE_MM if empty
                                   else -80 if labels["head"] == "forward" else 0)

        # 압력: 실패하면 이 순간은 건너뜀
        if step.pressure_drop and self.rng.random() < step.pressure_drop:
            raise SampleSkipped("압력 읽기 실패 (시나리오에서 흉내)")
        pressure = self.pressure.read()
        pressure.ts = ts

        # 거리
        if step.distance_fail and self.rng.random() < step.distance_fail:
            distance = DistanceReading(ts=ts, distance_mm=None, range_status=self.fail_status)
        else:
            distance = self.distance.read()
            distance.ts = ts
            if not distance.valid:
                distance.range_status = self.fail_status

        # 카메라: 초당 fps장만 새로 나오고, 그 사이에는 가장 최근 결과
        if self._last_pose is None or ts - self._last_pose.ts >= self.cam_period:
            if step.camera_lost or empty:
                pose = PoseReading.empty(ts)
            else:
                pose = self.pose.read()
                pose.ts = ts
            self._last_pose = pose

        return Sample(ts=ts, pressure=pressure, distance=distance, pose=self._last_pose)

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# replay CSV 만들기
# ---------------------------------------------------------------------------
def write_csv(cfg: dict, scenario: Scenario, out_dir: str | Path, *, seed: int | None = None,
              distance_sensor: str | None = None, start: datetime | None = None) -> Path:
    """시나리오 전체를 가상 시각으로 빠르게 만들어 수집 CSV 형식으로 저장. 같은 이름 파일은 덮어씀"""
    start = start or datetime.now().replace(microsecond=0)
    t0 = start.timestamp()
    hz = cfg["sampling"]["hz"]
    n_channels = cfg["pressure"]["n_channels"]
    player = ScenarioPlayer(cfg, scenario, seed=seed, distance_sensor=distance_sensor)
    player.start(t0)

    baseline_sec = sum(s.seconds for s in scenario.steps if s.phase == Phase.BASELINE.value)
    info = SessionInfo(
        session_id=f"mock_{scenario.name}", subject="mock",
        date=start.isoformat(timespec="seconds"),
        camera_width=cfg["camera"]["width"], camera_height=cfg["camera"]["height"],
        fsr_channels_used=list(range(n_channels)), baseline_seconds=baseline_sec,
        notes=f"더미 데이터: {scenario.description}",
        format_version=FORMAT_VERSION,
        extra={"scenario": scenario.name, "seed": seed, "distance_sensor": player.distance_sensor},
    )

    out = repo_path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    csv_path = out / f"{info.session_id}.csv"
    rows = skipped = 0
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(csv_columns(n_channels))
        n_ticks = int(round(scenario.total_sec * hz))
        for i in range(n_ticks):
            ts = t0 + i / hz
            _, step = player.current(ts)
            try:
                sample = player.read(ts)
            except SampleSkipped:
                skipped += 1
                continue
            if step.phase == Phase.BASELINE.value:
                labels = BASELINE_LABELS
            else:
                labels = {"seat": step.seat, "head": step.head, "tilt": step.tilt}
            w.writerow(sample_to_row(sample, info, step.phase,
                                     labels["seat"], labels["head"], labels["tilt"]))
            rows += 1

    info.end_date = datetime.fromtimestamp(t0 + scenario.total_sec).isoformat(timespec="seconds")
    info.rows = rows
    info.status = "completed"
    info.save(csv_path.with_suffix(".json"))
    log.info("%s: %d줄 (건너뜀 %d) → %s", scenario.name, rows, skipped,
             csv_path.relative_to(repo_path(".")))
    return csv_path


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="[SitSense mock] %(message)s")
    ap = argparse.ArgumentParser(description="시나리오 더미 데이터 → replay CSV")
    ap.add_argument("--list", action="store_true", help="시나리오 목록 보기")
    ap.add_argument("--scenario", action="append", choices=list(SCENARIOS), help="만들 시나리오 (여러 번 가능)")
    ap.add_argument("--all", action="store_true", help="모든 시나리오 만들기")
    ap.add_argument("--seed", type=int, default=1, help="같은 seed면 같은 값 (기본 1)")
    ap.add_argument("--out", default=None, help="저장 폴더 (기본: config.yaml의 paths.synthetic)")
    ap.add_argument("--distance-sensor", choices=list(DISTANCE_SENSORS), default=None,
                    help="거리센서 종류 (기본: config.yaml의 distance.sensor)")
    args = ap.parse_args()

    if args.list:
        for s in SCENARIOS.values():
            print(f"{s.name:<18} {s.total_sec:>5.0f}초  {s.description}")
        return 0
    names = list(SCENARIOS) if args.all else (args.scenario or [])
    if not names:
        ap.error("--scenario 이름, --all, --list 중 하나를 주세요")
    try:
        cfg = load_config()
    except ConfigError as e:
        log.error("%s", e)
        return 1
    out = args.out or cfg["paths"]["synthetic"]
    for name in names:
        write_csv(cfg, get_scenario(name), out, seed=args.seed, distance_sensor=args.distance_sensor)
    return 0


if __name__ == "__main__":
    sys.exit(main())
