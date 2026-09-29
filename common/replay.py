"""저장한 세션 CSV를 센서처럼 재생 — 맥북에서 같은 데이터로 반복 테스트할 때 사용

recorder.py가 만든 CSV를 한 줄씩 읽어서, SensorHub와 똑같은 모양(Sample)으로 돌려준다.
한 줄 = 한 순간이라 압력·거리·카메라가 어긋나지 않는다.

사용 예:
    src = ReplaySource("data/raw/p1_20261015_143012.csv")
    sample = src.read()
    src.labels   # {"phase": "record", "seat": "cross_left", "head": "normal", "tilt": "none"}
    src.info     # 같은 이름의 세션 정보 JSON (없으면 None)

깨진 줄(숫자가 아니거나 칸이 빈 줄)은 건너뛰고 src.skipped에 개수를 센다.
"""
from __future__ import annotations

import csv
import logging
from pathlib import Path

from common.config import repo_path
from common.recorder import FORMAT_VERSION, RecorderError, SessionInfo, csv_columns
from common.schema import LANDMARK_NAMES
from common.sensors_base import (
    DistanceReading, Landmark, PoseReading, PressureReading, Sample,
)

log = logging.getLogger(__name__)


class ReplayError(Exception):
    """재생할 파일이 없거나 형식이 맞지 않음"""


class ReplayFinished(Exception):
    """loop=False일 때 파일 끝까지 다 읽으면 발생"""


def load_rows(path: str | Path) -> tuple[list[str], list[dict]]:
    full = repo_path(path)
    if full.suffix != ".csv":
        raise ReplayError(f"CSV 파일이 아님: {full}")
    try:
        with open(full, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            header = reader.fieldnames or []
    except FileNotFoundError:
        raise ReplayError(f"재생할 파일이 없음: {full}") from None
    except (UnicodeDecodeError, csv.Error) as e:
        raise ReplayError(f"CSV를 읽을 수 없음: {full} ({e})") from None
    if not header:
        raise ReplayError(f"빈 파일: {full}")
    return header, rows


def _opt_float(v: str | None) -> float | None:
    return float(v) if v not in ("", None) else None


def row_to_sample(row: dict, channels: list[str]) -> Sample:
    """CSV 한 줄 → Sample. 형식이 틀리면 ValueError"""
    ts = float(row["ts"])
    pressure = PressureReading(ts=ts, values=[int(row[c]) for c in channels])
    mm = _opt_float(row["distance_mm"])
    distance = DistanceReading(ts=ts, distance_mm=int(mm) if mm is not None else None,
                               range_status=int(row["distance_status"]))
    pose = None
    cam_ts = _opt_float(row["cam_ts"])
    if cam_ts is not None:
        detected = row["pose_detected"] == "1"
        points = {}
        if detected:
            for n in LANDMARK_NAMES:
                x = _opt_float(row.get(f"{n}_x"))
                if x is None:                   # 그 점만 없으면 빼고 계속
                    continue
                points[n] = Landmark(x=x, y=float(row[f"{n}_y"]), z=float(row[f"{n}_z"]),
                                     visibility=float(row[f"{n}_vis"]))
        pose = PoseReading(ts=cam_ts, detected=detected, points=points)
    return Sample(ts=ts, pressure=pressure, distance=distance, pose=pose)


class ReplaySource:
    def __init__(self, path: str | Path, loop: bool = False,
                 expected_channels: int | None = None):
        header, rows = load_rows(path)
        self.channels = sorted((k for k in header if k[:1] == "p" and k[1:].isdigit()),
                               key=lambda k: int(k[1:]))
        if not self.channels:
            raise ReplayError(f"{path}에 압력 컬럼(p0, p1, …)이 없음 — 수집 CSV가 맞는지 확인")
        if expected_channels is not None and len(self.channels) != expected_channels:
            raise ReplayError(f"{path}의 압력 채널이 {len(self.channels)}개인데 "
                              f"config.yaml은 {expected_channels}개")
        missing = [c for c in csv_columns(len(self.channels)) if c not in header]
        if missing:
            raise ReplayError(f"{path}에 필요한 컬럼이 없음: {missing[:5]}"
                              f"{' 외' if len(missing) > 5 else ''} — 예전 형식 파일일 수 있음")

        # 깨진 줄은 미리 걸러낸다
        self.samples: list[tuple[Sample, dict]] = []
        self.skipped = 0
        for i, row in enumerate(rows, start=2):      # 1번 줄은 컬럼 이름
            try:
                sample = row_to_sample(row, self.channels)
            except (ValueError, TypeError, KeyError) as e:
                self.skipped += 1
                if self.skipped <= 3:
                    log.warning("%s %d번 줄 건너뜀: %s", path, i, e)
                continue
            labels = {"phase": row["phase"], "seat": row["seat_label"],
                      "head": row["head_label"], "tilt": row["tilt_label"]}
            self.samples.append((sample, labels))
        if self.skipped:
            log.warning("%s: 깨진 줄 %d개를 건너뜀", path, self.skipped)
        if not self.samples:
            raise ReplayError(f"{path}에 재생할 수 있는 줄이 없음")

        # 세션 정보 (있으면)
        self.info: SessionInfo | None = None
        json_path = repo_path(path).with_suffix(".json")
        if json_path.exists():
            try:
                self.info = SessionInfo.load(json_path)
            except RecorderError as e:
                log.warning("세션 정보를 읽지 못함: %s", e)
        else:
            log.warning("세션 정보 파일이 없음: %s (좌표 → 픽셀 변환에 해상도가 필요)", json_path.name)
        if self.info and self.info.format_version != FORMAT_VERSION:
            log.warning("형식 버전이 다름: 파일 %s, 코드 %s", self.info.format_version, FORMAT_VERSION)

        self.loop = loop
        self.i = 0
        self.labels: dict[str, str] = {}

    def __len__(self) -> int:
        return len(self.samples)

    def read(self) -> Sample:
        if self.i >= len(self.samples):
            if not self.loop:
                raise ReplayFinished
            self.i = 0
        sample, self.labels = self.samples[self.i]
        self.i += 1
        return sample

    def close(self) -> None:
        pass
