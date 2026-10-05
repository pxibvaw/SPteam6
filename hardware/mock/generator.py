"""시나리오 → 센서 값 (PressureReading / DistanceReading / PoseReading → Sample)

두 가지로 쓴다.
    1. mock 생성기 (실시간): ScenarioPlayer.read()  → mock 서버가 10Hz로 호출
                             python -m hardware.mock.generator --scenario demo --live
    2. replay CSV (파일):    python -m hardware.mock.generator --scenario forward_head
                             → data/synthetic/mock_forward_head.csv + .json (수집 CSV와 같은 형식)
                             → python main.py --mode replay --file data/synthetic/mock_forward_head.csv

값은 hardware/mock/models.py에서 만들고, 여기서는 시나리오에 따라 라벨을 바꾸고
센서 오류·카메라 속도(초당 fps장)·자리 비움 때 센서 끄기를 흉내 낸다.
주의: 실제 자세 패턴이 아니라 코드·앱 흐름 확인용이다. 정확도 평가에 쓰면 안 된다.

자리 비움 (seat=empty)
    - 압력은 계속 기록한다 (0 근처 값)
    - empty_off_sec(5초) 미만: 카메라·거리 켜짐 → 사람 못 찾음(pose_detected=0), 거리는 뒤쪽 벽
    - empty_off_sec 이상: 착석 구간 종료, 카메라·거리 꺼짐 → cam_ts 빈칸, distance_mm 빈칸·status -1
    - 다시 앉으면 켜지고, 카메라는 CAMERA_WARMUP_SEC 동안 사람을 못 찾음(pose_detected=0)

사용 예:
    python -m hardware.mock.generator --list                 # 시나리오 목록
    python -m hardware.mock.generator --scenario demo        # CSV 1개 만들기
    python -m hardware.mock.generator --all --seed 1         # 전부 만들기
    python -m hardware.mock.generator --scenario demo --start "2026-10-15 23:58:00"   # 자정 넘기기
    python -m hardware.mock.generator --scenario normal --distance-sensor hc-sr04
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from common.config import ConfigError, fsr_positions, load_config, repo_path
from common.recorder import FORMAT_VERSION, SessionInfo, csv_columns, sample_to_row
from common.schema import BASELINE_LABELS, Phase
from common.sensors_base import DistanceReading, PoseReading, Sample, SampleSkipped, now
from hardware.mock.models import (
    DISTANCE_SENSORS, DistanceModel, PoseModel, PressureModel,
)
from hardware.mock.scenarios import PRIORITY, SCENARIOS, Scenario, Step, get_scenario

log = logging.getLogger("sitsense.mock")

# ⚠️ config.yaml에 아직 없는 값 → thresholds.empty_off_sec로 추가 제안 (있으면 그 값을 씀)
EMPTY_OFF_SEC = 5.0             # 자리 비움이 이만큼 이어지면 착석 구간 종료, 카메라·거리 꺼짐
CAMERA_WARMUP_SEC = 2.0         # 카메라를 다시 켠 뒤 사람을 찾기까지 걸리는 시간
TS_JITTER_SEC = 0.004           # replay CSV의 시각 흔들림 (실제 10Hz 반복도 정확히 0.1초가 아님)


def step_labels(step: Step) -> dict[str, str]:
    """기준 측정 구간은 바른 자세 라벨"""
    if step.phase == Phase.BASELINE.value:
        return dict(BASELINE_LABELS)
    return {"seat": step.seat, "head": step.head, "tilt": step.tilt}


class ScenarioPlayer:
    """시나리오를 시간에 따라 재생하며 Sample을 만든다.

    read(ts)에 시각을 넘기면 그 시각의 값을 만든다 (CSV는 가상 시각, 서버는 지금 시각).
    시각은 앞으로만 가야 한다 (자리 비움 시간을 세기 때문).
    압력 읽기가 실패하는 순간은 SensorHub와 똑같이 SampleSkipped를 낸다.
    """

    def __init__(self, cfg: dict, scenario: Scenario, *, seed: int | None = None,
                 loop: bool = False, distance_sensor: str | None = None):
        self.cfg = cfg
        self.scenario = scenario
        self.loop = loop
        self.distance_sensor = (distance_sensor or cfg["distance"].get("sensor", "vl53l1x")).lower()
        self.empty_off_sec = float(cfg["thresholds"].get("empty_off_sec", EMPTY_OFF_SEC))

        self.rng = np.random.default_rng(seed)
        cam, pr = cfg["camera"], cfg["pressure"]
        self.pressure = PressureModel(fsr_positions(cfg), pr["adc_max"], self.rng)
        self.distance = DistanceModel(self.distance_sensor, self.rng)
        self.pose = PoseModel(cam["width"], cam["height"], self.rng)
        self.cam_period = 1.0 / cam["fps"]

        self.t0: float | None = None
        self._override: tuple[Step, float] | None = None   # (Step, 끝 시각) — 기준 다시 측정 중
        self._reset_state()

    def _reset_state(self) -> None:
        self._last_pose: PoseReading | None = None
        self._empty_since: float | None = None
        self._cam_on_at: float | None = None
        self.sensors_on = True                  # 카메라·거리센서 (압력은 항상 켜짐)

    # --- 시간 ---------------------------------------------------------------
    def start(self, t0: float | None = None) -> None:
        self.t0 = now() if t0 is None else t0
        self._reset_state()

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

    def camera_warming(self, ts: float) -> bool:
        return self._cam_on_at is not None and ts - self._cam_on_at < CAMERA_WARMUP_SEC

    def _update_power(self, ts: float, empty: bool) -> None:
        """자리 비움 시간에 따라 카메라·거리센서를 끄고 켠다"""
        if empty:
            if self._empty_since is None:
                self._empty_since = ts
            if self.sensors_on and ts - self._empty_since >= self.empty_off_sec:
                self.sensors_on = False                    # 착석 구간 종료
                self._last_pose = None
        else:
            self._empty_since = None
            if not self.sensors_on:                        # 다시 앉음 → 켜기
                self.sensors_on = True
                self._cam_on_at = ts
                self._last_pose = None

    # --- 읽기 ---------------------------------------------------------------
    def read(self, ts: float | None = None) -> Sample:
        ts = now() if ts is None else ts
        _, step = self.current(ts)
        lab = step_labels(step)
        empty = lab["seat"] == "empty"
        self._update_power(ts, empty)

        # 압력: 자리 비움 중에도 계속. 읽기 실패면 이 순간은 건너뜀
        if step.pressure_drop and self.rng.random() < step.pressure_drop:
            raise SampleSkipped("압력 읽기 실패 (시나리오에서 흉내)")
        pressure = self.pressure.read(ts, lab["seat"], step.extra_lean, step.dead_channel)

        if not self.sensors_on:
            return Sample(ts=ts, pressure=pressure, distance=DistanceReading.failed(ts), pose=None)

        distance = self.distance.read(ts, lab["head"], empty, step.distance_fail, step.distance_spike)

        # 카메라: 초당 fps장만 새로 나오고, 그 사이에는 가장 최근 결과
        if self._last_pose is None or ts - self._last_pose.ts >= self.cam_period * 0.9:
            if empty or step.camera_lost or self.camera_warming(ts):
                self._last_pose = PoseReading.empty(ts)
            else:
                self._last_pose = self.pose.read(ts, lab["head"], lab["tilt"], step.hidden_points)

        return Sample(ts=ts, pressure=pressure, distance=distance, pose=self._last_pose)

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# 정답 (세션 JSON의 extra.expected)
# ---------------------------------------------------------------------------
def expected_timeline(scenario: Scenario, t0: float, empty_off_sec: float = EMPTY_OFF_SEC) -> dict:
    """구간별 라벨·대표 자세, 착석 구간, 카메라·거리 꺼진 구간 (시각은 time.time() 초)"""
    steps, t = [], t0
    for s in scenario.steps:
        steps.append({"start": round(t, 3), "end": round(t + s.seconds, 3), "phase": s.phase,
                      **step_labels(s), "extra_lean": s.extra_lean, "postures": s.postures,
                      "dominant": "normal" if s.phase == Phase.BASELINE.value else s.dominant,
                      "faults": s.faults})
        t += s.seconds

    # 이어진 자리 비움 구간
    empties: list[list[float]] = []
    for st in steps:
        if st["seat"] != "empty":
            continue
        if empties and abs(empties[-1][1] - st["start"]) < 1e-6:
            empties[-1][1] = st["end"]
        else:
            empties.append([st["start"], st["end"]])

    # 착석 구간의 끝 = 일어난 시각 (종료가 확정되는 건 empty_off_sec 뒤)
    segments, sensors_off, seg_start = [], [], round(t0, 3)
    for a, b in empties:
        if b - a < empty_off_sec:
            continue                                   # 짧은 자리 비움 → 착석 구간 유지
        if a > seg_start:
            segments.append({"start": seg_start, "end": a})
        sensors_off.append({"start": round(a + empty_off_sec, 3), "end": b})
        seg_start = b
    if seg_start < round(t, 3):
        segments.append({"start": seg_start, "end": round(t, 3)})

    return {"priority": list(PRIORITY), "empty_off_sec": empty_off_sec,
            "camera_warmup_sec": CAMERA_WARMUP_SEC, "segments": segments,
            "sensors_off": sensors_off, "steps": steps}


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
    jitter = np.random.default_rng(None if seed is None else [seed, 1])

    baseline_sec = sum(s.seconds for s in scenario.steps if s.phase == Phase.BASELINE.value)
    info = SessionInfo(
        session_id=f"mock_{scenario.name}", subject="mock",
        date=start.isoformat(timespec="seconds"),
        camera_width=cfg["camera"]["width"], camera_height=cfg["camera"]["height"],
        fsr_channels_used=list(range(n_channels)), baseline_seconds=baseline_sec,
        notes=f"더미 데이터: {scenario.description}",
        format_version=FORMAT_VERSION,
        extra={"scenario": scenario.name, "seed": seed, "distance_sensor": player.distance_sensor,
               "expected": expected_timeline(scenario, t0, player.empty_off_sec)},
    )

    out = repo_path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    csv_path = out / f"{info.session_id}.csv"
    rows = skipped = 0
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(csv_columns(n_channels))
        for i in range(int(round(scenario.total_sec * hz))):
            ts = t0 + i / hz
            if i:
                ts += float(np.clip(jitter.normal(0, TS_JITTER_SEC), -0.01, 0.01))
            _, step = player.current(ts)
            try:
                sample = player.read(ts)
            except SampleSkipped:
                skipped += 1
                continue
            lab = step_labels(step)
            w.writerow(sample_to_row(sample, info, step.phase, lab["seat"], lab["head"], lab["tilt"]))
            rows += 1

    info.end_date = datetime.fromtimestamp(t0 + scenario.total_sec).isoformat(timespec="seconds")
    info.rows = rows
    info.status = "completed"
    info.save(csv_path.with_suffix(".json"))
    log.info("%-17s %5d줄 (건너뜀 %3d, 압력 %d채널) → %s", scenario.name, rows, skipped, n_channels,
             csv_path.relative_to(repo_path(".")))
    return csv_path


# ---------------------------------------------------------------------------
# 실시간 출력
# ---------------------------------------------------------------------------
def describe(sample: Sample, step: Step, player: ScenarioPlayer) -> str:
    lab = step_labels(step)
    d = sample.distance
    dist = (f"{d.distance_mm}mm" if d.valid else
            "꺼짐" if not player.sensors_on else f"실패({d.range_status})")
    pose = sample.pose
    cam = ("꺼짐" if pose is None else "인식" if pose.detected else
           "켜는 중" if player.camera_warming(sample.ts) else "미인식")
    clock = datetime.fromtimestamp(sample.ts).strftime("%H:%M:%S.%f")[:-5]
    return (f"{clock}  {lab['seat']:<11} {lab['head']:<7} {lab['tilt']:<5}  "
            f"압력={sample.pressure.values}  거리={dist}  카메라={cam}")


def live(cfg: dict, scenario: Scenario, seconds: float | None, *, seed: int | None = None,
         loop: bool = False, distance_sensor: str | None = None) -> None:
    hz = cfg["sampling"]["hz"]
    player = ScenarioPlayer(cfg, scenario, seed=seed, loop=loop, distance_sensor=distance_sensor)
    player.start()
    t_end = None if seconds is None else player.t0 + seconds
    log.info("실시간: %s (%s, %.0f초, %gHz) — Ctrl+C로 종료", scenario.name, scenario.description,
             scenario.total_sec, hz)
    while True:
        t = now()
        if (t_end is not None and t >= t_end) or player.finished(t):
            break
        _, step = player.current(t)
        try:
            print(describe(player.read(t), step, player), flush=True)
        except SampleSkipped:
            print(f"{datetime.fromtimestamp(t):%H:%M:%S}  (압력 읽기 실패 → 이 순간 건너뜀)", flush=True)
        time.sleep(max(0.0, 1.0 / hz - (now() - t)))


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="[SitSense mock] %(message)s")
    ap = argparse.ArgumentParser(description="시나리오 더미 데이터 → replay CSV / 실시간 출력")
    ap.add_argument("--list", action="store_true", help="시나리오 목록 보기")
    ap.add_argument("--scenario", action="append", choices=list(SCENARIOS), help="시나리오 (여러 번 가능)")
    ap.add_argument("--all", action="store_true", help="모든 시나리오 만들기")
    ap.add_argument("--seed", type=int, default=1, help="같은 seed면 같은 값 (기본 1)")
    ap.add_argument("--out", default=None, help="저장 폴더 (기본: config.yaml의 paths.synthetic)")
    ap.add_argument("--distance-sensor", choices=list(DISTANCE_SENSORS), default=None,
                    help="거리센서 종류 (기본: config.yaml의 distance.sensor)")
    ap.add_argument("--start", default=None,
                    help='CSV 시작 시각 (한국 시간, 예: "2026-10-15 23:58:00"). 기본: 지금')
    ap.add_argument("--live", action="store_true", help="CSV 대신 실시간 10Hz로 출력 (시나리오 1개)")
    ap.add_argument("--seconds", type=float, default=None, help="--live 실행 시간 (기본: 시나리오 끝까지)")
    ap.add_argument("--loop", action="store_true", help="--live에서 시나리오 반복")
    args = ap.parse_args()

    if args.list:
        for s in SCENARIOS.values():
            print(f"{s.name:<17} {s.total_sec:>5.0f}초  {s.description}")
        return 0
    names = list(SCENARIOS) if args.all else (args.scenario or [])
    if not names:
        ap.error("--scenario 이름, --all, --list 중 하나를 주세요")
    start = None
    if args.start:
        if args.live:
            ap.error("--live는 지금 시각으로 돌아서 --start를 쓸 수 없음")
        try:
            start = datetime.fromisoformat(args.start)
        except ValueError:
            ap.error(f"--start '{args.start}' 형식이 틀림 (예: \"2026-10-15 23:58:00\")")
    if args.live and len(names) != 1:
        ap.error("--live는 --scenario 하나만")
    try:
        cfg = load_config()
    except ConfigError as e:
        log.error("%s", e)
        return 1

    if args.live:
        try:
            live(cfg, get_scenario(names[0]), args.seconds, seed=args.seed, loop=args.loop,
                 distance_sensor=args.distance_sensor)
        except KeyboardInterrupt:
            log.info("Ctrl+C로 종료")
        return 0
    out = args.out or cfg["paths"]["synthetic"]
    for name in names:
        write_csv(cfg, get_scenario(name), out, seed=args.seed,
                  distance_sensor=args.distance_sensor, start=start)
    return 0


if __name__ == "__main__":
    sys.exit(main())
