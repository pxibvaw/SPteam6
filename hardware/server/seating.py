"""착석 구간 — 언제부터 언제까지 앉아 있었나, 그동안 자세는 어땠나

규칙
    - 자리 비움 판단은 3초 필터와 별개로, 그 순간의 압력으로 한다 (압력 합 < EMPTY_TOTAL_ADC).
      AI 코드와 같은 기준. 합이라서 채널 수가 줄면 채널당 기준은 조금 높아진다 (8채널 6.25, 6채널 8.3)
    - 자리 비움이 EMPTY_END_SEC(5초) 이어지면 구간 종료. 종료 시각 = 판단 시각 − 5초 (마지막 5초는 착석에 안 넣음)
    - 5초 미만 자리 비움은 구간을 유지하고, 그 시간은 unknown으로 센다.
    - 구간이 없을 때 압력이 SIT_CONFIRM_SEC(1초) 이어지면 새 구간. 시작 시각 = 압력이 처음 들어온 시각
    - 순간마다 normal / abnormal / unknown (3초 필터를 거친 상태 기준)
        normal   목 normal + 기울기 none + 좌면 normal (체중 편향이면 정상 아님)
        abnormal 나쁜 자세가 하나라도 확정됨 (다른 부분이 unknown이어도)
        unknown  나쁜 자세는 없지만 판단 못 한 부분이 있음 (카메라 미인식·켜는 중, 기준 측정 전, 짧은 자리 비움)
    - 구간별 '첫 비정상 확정까지 걸린 초' = 구간 시작 → 처음 abnormal이 된 순간 (3초 필터 확정 시각)

센서·AI 종류와 상관없이 쓸 수 있게 FastAPI·mock 코드에 의존하지 않는다.
끝난 구간은 지금은 메모리에만 둔다 (DB 저장은 다음 단계).
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from enum import Enum

# ⚠️ 임시값 — 실물 FSR 값을 보고 조정
EMPTY_TOTAL_ADC = 50.0      # 압력 합(음수는 0으로)이 이보다 작으면 자리 비움. AI 코드와 같은 값:
                            #   ai 브랜치(3a94581) ai/features/pressure.py 11줄 center_of_pressure(min_total=50.0),
                            #   25줄 `if total < min_total` — 실물 FSR로 빈 의자·앉았을 때를 재서 다시 맞출 것
EMPTY_END_SEC = 5.0         # 자리 비움이 이만큼 이어지면 구간 종료 (config.yaml thresholds.empty_off_sec가 있으면 그 값)
SIT_CONFIRM_SEC = 1.0       # 구간이 없을 때 압력이 이만큼 이어져야 새 구간 (의자를 잠깐 건드린 것과 구분)
MAX_STEP_SEC = 1.0          # 기록 간격이 이보다 길면 이만큼만 센다 (기록이 끊긴 시간은 넣지 않음)
KEEP_SEGMENTS = 2000        # 메모리에 둘 끝난 구간 수


class PostureStatus(str, Enum):
    NORMAL = "normal"
    ABNORMAL = "abnormal"
    UNKNOWN = "unknown"


class EndReason(str, Enum):
    AWAY = "away"           # 자리 비움 5초
    STOP = "stop"           # 정지 버튼 (세션 API가 생기면 사용)


def is_empty(values) -> bool:
    """그 순간 자리가 비었는지 (AI 코드 center_of_pressure와 같은 압력 합 기준)"""
    values = list(values)
    return not values or sum(max(v, 0) for v in values) < EMPTY_TOTAL_ADC


def classify(seat: str, head: str, tilt: str, postures: list[str]) -> tuple[PostureStatus, str]:
    """(normal/abnormal/unknown, 대표 자세). postures는 우선순위 순서"""
    if postures:
        return PostureStatus.ABNORMAL, postures[0]
    if seat == "normal" and head == "normal" and tilt == "none":
        return PostureStatus.NORMAL, "normal"
    return PostureStatus.UNKNOWN, "unknown"


@dataclass
class Segment:
    start: float
    end: float | None = None                # 진행 중이면 None
    end_reason: str | None = None
    first_abnormal_sec: float | None = None # 끝까지 비정상이 없으면 None
    normal_sec: float = 0.0
    abnormal_sec: float = 0.0
    unknown_sec: float = 0.0

    def add(self, status: PostureStatus, sec: float) -> None:
        name = f"{status.value}_sec"
        setattr(self, name, getattr(self, name) + sec)

    def to_dict(self, now_ts: float | None = None) -> dict:
        d = asdict(self)
        end = self.end if self.end is not None else now_ts
        d["duration_sec"] = round(end - self.start, 1) if end is not None else None
        for k in ("normal_sec", "abnormal_sec", "unknown_sec", "first_abnormal_sec"):
            if d[k] is not None:
                d[k] = round(d[k], 1)
        return d


class SeatingTracker:
    """매 순간 update()를 부르면 착석 구간을 나누고 구간별 자세 시간을 센다.

    사용 예:
        tracker = SeatingTracker(cfg)
        tracker.update(ts, empty=is_empty(pressure), seat=..., head=..., tilt=..., postures=[...])
        tracker.snapshot(ts)        # 지금 구간 + 지금 상태
        tracker.segments            # 끝난 구간 (오래된 것부터)
    """

    def __init__(self, cfg: dict | None = None, *, end_sec: float | None = None,
                 confirm_sec: float = SIT_CONFIRM_SEC):
        th = (cfg or {}).get("thresholds", {})
        self.end_sec = float(end_sec if end_sec is not None else th.get("empty_off_sec", EMPTY_END_SEC))
        self.confirm_sec = confirm_sec
        self.current: Segment | None = None
        self.segments: deque[Segment] = deque(maxlen=KEEP_SEGMENTS)
        self.status: PostureStatus | None = None
        self.dominant: str | None = None
        self._away_since: float | None = None      # 구간 중 자리 비움이 시작된 시각
        self._sit_since: float | None = None       # 구간 밖에서 압력이 들어온 시각
        self._last_ts: float | None = None

    def update(self, ts: float, *, empty: bool, seat: str, head: str, tilt: str,
               postures: list[str]) -> None:
        dt = 0.0 if self._last_ts is None else min(max(ts - self._last_ts, 0.0), MAX_STEP_SEC)
        self._last_ts = ts

        if self.current is None:
            self.status = self.dominant = None
            if empty:
                self._sit_since = None
                return
            if self._sit_since is None:
                self._sit_since = ts
            if ts - self._sit_since < self.confirm_sec:
                return
            # 새 구간: 시작은 압력이 처음 들어온 시각. 확인하는 동안은 3초 필터가 아직 좌면을
            # empty로 들고 있어서 판단할 수 없으므로 unknown으로 센다
            self.current = Segment(start=self._sit_since)
            self.current.add(PostureStatus.UNKNOWN, ts - self._sit_since)
            self._sit_since = self._away_since = None
            dt = 0.0

        seg = self.current
        if empty:
            if self._away_since is None:
                self._away_since = ts
            status, dominant = PostureStatus.UNKNOWN, "away"
        else:
            self._away_since = None
            status, dominant = classify(seat, head, tilt, postures)

        seg.add(status, dt)
        self.status, self.dominant = status, dominant
        if status is PostureStatus.ABNORMAL and seg.first_abnormal_sec is None:
            seg.first_abnormal_sec = ts - seg.start

        if self._away_since is not None and ts - self._away_since >= self.end_sec:
            self._end(ts - self.end_sec, EndReason.AWAY, ts)

    def stop(self, ts: float) -> None:
        """정지 버튼: 지금 시각으로 구간 종료"""
        if self.current is not None:
            self._end(ts, EndReason.STOP, ts)

    def _end(self, end: float, reason: EndReason, ts: float) -> None:
        seg = self.current
        seg.end, seg.end_reason = max(end, seg.start), reason.value
        # 종료 시각 뒤(자리 비움 마지막 5초)에 센 시간은 빼기 — 이 동안은 unknown으로 셌음
        seg.unknown_sec = max(0.0, seg.unknown_sec - (ts - seg.end))
        self.segments.append(seg)
        self.current = None
        self.status = self.dominant = None
        self._away_since = self._sit_since = None

    def snapshot(self, ts: float) -> dict:
        seg = self.current
        away = None if self._away_since is None else round(ts - self._away_since, 1)
        return {
            "ts": ts,
            "seated": seg is not None,
            "segment": seg.to_dict(ts) if seg else None,
            "status": self.status.value if self.status else None,
            "dominant": self.dominant,
            "away_sec": away,
            "end_in_sec": None if away is None else round(max(0.0, self.end_sec - away), 1),
        }

    def finished(self, since_ts: float | None = None) -> list[dict]:
        return [s.to_dict() for s in self.segments if since_ts is None or s.end >= since_ts]
