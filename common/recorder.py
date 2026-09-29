"""수집 파일 형식 — CSV 컬럼과 세션 정보는 여기서만 정의한다

한 세션 = CSV 1개 + 세션 정보 JSON 1개
    data/raw/{session_id}.csv
    data/raw/{session_id}.json
    session_id = {subject}_{YYYYMMDD_HHMMSS}     예) p1_20261015_143012
    (같은 초에 또 시작하면 _2, _3을 붙여서 기존 파일을 덮어쓰지 않는다)

CSV 한 줄 = 한 순간의 모든 센서 값 (총 47칸)
    ts, session_id, subject, phase,
    seat_label, head_label, tilt_label,
    p0 ~ p7,
    distance_mm, distance_status,
    cam_ts, pose_detected,
    {부위}_x, {부위}_y, {부위}_z, {부위}_vis   (부위 7개: common.schema.LANDMARK_NAMES)

세션마다 처음 baseline_seconds(기본 10초)는 phase=baseline (바른 자세 기준 측정),
그다음부터 phase=record.

안전장치
    - 매 줄마다 디스크에 바로 저장 → 중간에 꺼져도 그때까지의 데이터는 남는다
    - 끝나면 세션 JSON에 줄 수, 종료 시각, 종료 상태(completed / interrupted / error)를 기록
"""
from __future__ import annotations

import csv
import json
import logging
import math
import os
import re
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path

from common.config import repo_path
from common.schema import BASELINE_LABELS, LANDMARK_NAMES, Phase, check_labels
from common.sensors_base import Sample

log = logging.getLogger(__name__)

FORMAT_VERSION = 1          # CSV·세션 정보 형식이 바뀌면 올린다 (replay에서 확인)
SUBJECT_RE = re.compile(r"^[A-Za-z0-9_-]{1,20}$")


class RecorderError(Exception):
    """저장 폴더·파일 문제, 잘못된 입력"""


# ---------------------------------------------------------------------------
# 세션 정보
# ---------------------------------------------------------------------------
@dataclass
class SessionInfo:
    session_id: str
    subject: str                     # p1, p2, ... (실명 쓰지 않기)
    date: str                        # 시작 시각 (ISO)
    camera_width: int                # 좌표(0~1)를 픽셀로 바꿀 때 필요
    camera_height: int
    fsr_channels_used: list[int]     # 8개 중 실제로 쓴 채널 (선별 전에는 전부)
    baseline_seconds: float
    chair: str = ""
    cushion: str = ""
    notes: str = ""                  # 특이사항 (체형은 "마른 편/보통/큰 편" 정도만)
    format_version: int = FORMAT_VERSION
    end_date: str | None = None      # 끝난 시각 (저장이 끝나면 채워짐)
    rows: int | None = None          # 저장된 줄 수
    status: str = "recording"        # recording / completed / interrupted / error
    extra: dict = field(default_factory=dict)

    @classmethod
    def new(cls, subject: str, cfg: dict, **kw) -> "SessionInfo":
        if not SUBJECT_RE.match(subject or ""):
            raise RecorderError(f"subject '{subject}'는 영문·숫자·_- 20자 이내로 (예: p1)")
        t = datetime.now()
        return cls(
            session_id=f"{subject}_{t:%Y%m%d_%H%M%S}",
            subject=subject,
            date=t.isoformat(timespec="seconds"),
            camera_width=cfg["camera"]["width"],
            camera_height=cfg["camera"]["height"],
            fsr_channels_used=list(range(cfg["pressure"]["n_channels"])),
            baseline_seconds=cfg["thresholds"]["baseline_seconds"],
            **kw,
        )

    def save(self, path: str | Path) -> None:
        tmp = repo_path(path).with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)
        os.replace(tmp, repo_path(path))          # 쓰다가 꺼져도 JSON이 깨지지 않게

    @classmethod
    def load(cls, path: str | Path) -> "SessionInfo":
        try:
            with open(repo_path(path), encoding="utf-8") as f:
                d = json.load(f)
        except FileNotFoundError:
            raise RecorderError(f"세션 정보 파일이 없음: {path}") from None
        except json.JSONDecodeError as e:
            raise RecorderError(f"세션 정보 파일이 깨짐: {path} ({e})") from None
        known = {f.name for f in fields(cls)}
        unknown = {k: v for k, v in d.items() if k not in known}
        d = {k: v for k, v in d.items() if k in known}
        if unknown:                                 # 나중에 필드가 늘어나도 읽을 수 있게
            d.setdefault("extra", {}).update(unknown)
        try:
            return cls(**d)
        except TypeError as e:
            raise RecorderError(f"세션 정보에 필요한 항목이 없음: {path} ({e})") from None


# ---------------------------------------------------------------------------
# CSV 컬럼
# ---------------------------------------------------------------------------
LANDMARK_FIELDS = ("x", "y", "z", "vis")


def csv_columns(n_channels: int) -> list[str]:
    cols = ["ts", "session_id", "subject", "phase",
            "seat_label", "head_label", "tilt_label"]
    cols += [f"p{i}" for i in range(n_channels)]
    cols += ["distance_mm", "distance_status", "cam_ts", "pose_detected"]
    cols += [f"{name}_{f}" for name in LANDMARK_NAMES for f in LANDMARK_FIELDS]
    return cols


def _num(v: float, fmt: str) -> str:
    """NaN·무한대는 빈칸으로"""
    return format(v, fmt) if v is not None and math.isfinite(v) else ""


def sample_to_row(s: Sample, info: SessionInfo, phase: str,
                  seat: str, head: str, tilt: str) -> list:
    row = [f"{s.ts:.3f}", info.session_id, info.subject, phase, seat, head, tilt]
    row += [int(v) for v in s.pressure.values]
    d = s.distance
    row += ["" if d.distance_mm is None else int(d.distance_mm), d.range_status]
    pose = s.pose
    row += ["", 0] if pose is None else [f"{pose.ts:.3f}", int(pose.detected)]
    for name in LANDMARK_NAMES:
        lm = pose.points.get(name) if pose and pose.detected else None
        if lm is None or not lm.is_finite():
            row += ["", "", "", ""]
        else:
            row += [_num(lm.x, ".5f"), _num(lm.y, ".5f"), _num(lm.z, ".5f"),
                    _num(lm.visibility, ".3f")]
    return row


# ---------------------------------------------------------------------------
# 저장
# ---------------------------------------------------------------------------
class SessionRecorder:
    """사용 예:
        info = SessionInfo.new("p1", cfg, chair="학교 의자")
        with SessionRecorder("data/raw", info, n_channels=8) as rec:
            rec.write(hub.read(), seat="cross_left", head="normal", tilt="none")
    처음 baseline_seconds 동안은 라벨과 상관없이 phase=baseline, 바른 자세 라벨로 저장된다.
    with 문을 쓰면 오류나 Ctrl+C로 끝나도 파일이 닫히고 세션 정보에 종료 상태가 남는다.
    """

    def __init__(self, out_dir: str | Path, info: SessionInfo, n_channels: int):
        out = repo_path(out_dir)
        try:
            out.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise RecorderError(f"저장 폴더를 만들 수 없음: {out} ({e})") from None
        if not os.access(out, os.W_OK):
            raise RecorderError(f"저장 폴더에 쓸 권한이 없음: {out}")

        # 같은 이름이 이미 있으면 _2, _3 … (기존 데이터 덮어쓰기 방지)
        base, k = info.session_id, 1
        while (out / f"{info.session_id}.csv").exists() or (out / f"{info.session_id}.json").exists():
            k += 1
            info.session_id = f"{base}_{k}"

        self.info = info
        self.n = n_channels
        self.csv_path = out / f"{info.session_id}.csv"
        self.json_path = out / f"{info.session_id}.json"
        self._t0: float | None = None
        self.count = 0
        self._closed = False
        try:
            self._f = open(self.csv_path, "x", newline="", encoding="utf-8")
        except OSError as e:
            raise RecorderError(f"CSV 파일을 만들 수 없음: {self.csv_path} ({e})") from None
        self._w = csv.writer(self._f)
        self._w.writerow(csv_columns(n_channels))
        self._f.flush()
        info.save(self.json_path)

    def phase_at(self, ts: float) -> str:
        if self._t0 is None:
            self._t0 = ts
        in_baseline = ts - self._t0 < self.info.baseline_seconds
        return Phase.BASELINE.value if in_baseline else Phase.RECORD.value

    def write(self, sample: Sample, seat: str, head: str, tilt: str,
              phase: str | None = None) -> str:
        """phase를 안 주면 sample.ts로 정한다. 미리 phase_at()으로 정했다면 같은 값을 넘길 것."""
        if self._closed:
            raise RecorderError("이미 닫힌 세션에 쓰려고 함")
        if len(sample.pressure.values) != self.n:
            raise RecorderError(f"압력 채널 수가 {self.n}개가 아님: {len(sample.pressure.values)}")
        phase = phase or self.phase_at(sample.ts)
        if phase not in (Phase.BASELINE.value, Phase.RECORD.value):
            raise RecorderError(f"phase '{phase}'는 baseline 또는 record여야 함")
        if phase == Phase.BASELINE.value:
            seat, head, tilt = BASELINE_LABELS["seat"], BASELINE_LABELS["head"], BASELINE_LABELS["tilt"]
        check_labels(seat, head, tilt)
        try:
            self._w.writerow(sample_to_row(sample, self.info, phase, seat, head, tilt))
            self._f.flush()                       # 매 줄 바로 저장
        except OSError as e:
            raise RecorderError(f"CSV 저장 실패 (디스크 공간 확인): {e}") from None
        self.count += 1
        return phase

    def close(self, status: str = "completed") -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._f.flush()
            os.fsync(self._f.fileno())
        except OSError as e:
            log.warning("CSV 마무리 저장 실패: %s", e)
        finally:
            self._f.close()
        self.info.end_date = datetime.now().isoformat(timespec="seconds")
        self.info.rows = self.count
        self.info.status = status
        try:
            self.info.save(self.json_path)
        except OSError as e:
            log.warning("세션 정보 저장 실패: %s", e)
        if self.count == 0:
            log.warning("세션 %s에 저장된 줄이 없음", self.info.session_id)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.close("completed")
        elif issubclass(exc_type, KeyboardInterrupt):
            self.close("interrupted")
        else:
            self.close("error")
        return False
