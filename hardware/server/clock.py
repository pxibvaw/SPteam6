"""앱 기준 시각 — Pi OS 시계는 건드리지 않고 '앱 시각 − Pi 단조시계' 차이만 저장

    기록 시각 = time.monotonic() + offset   (단조시계라 Pi 시계가 바뀌어도 뒤로 가지 않음)

- 앱이 POST /time/sync로 현재 시각과 시간대(Asia/Seoul 고정)를 보내면 offset을 정한다.
- 다시 동기화할 때 (변화량 = 직전 동기화 대비, 한 번 바뀌는 양만 본다)
    뒤로 0.5초 이내        → 적용하지 않음 (applied=false, 네트워크 지연 오차)
    뒤로 0.5초 넘게        → 거절 CLOCK_BACKWARD (세션 안팎 모두)
    세션 중 2초 넘게 앞으로 → 거절 CLOCK_JUMP_IN_SESSION
- offset은 메모리에만 있다. Pi가 재부팅되거나 서버가 다시 켜지면 사라지고 boot_id가 바뀐다
  → 앱은 boot_id가 달라졌거나 synced=false면 다시 보내야 한다.
- 센서 값(common.sensors_base)은 Pi 시각(time.time())으로 찍혀 나오므로, 읽은 직후
  stamp(sample)로 한 번에 앱 시각으로 옮긴다 (압력·거리·카메라 사이 간격은 그대로).
"""
from __future__ import annotations

import logging
import math
import time
import uuid
from datetime import datetime, timedelta, timezone, tzinfo

log = logging.getLogger("sitsense.clock")

MIN_APP_TIME = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()   # 이보다 이전 시각은 잘못된 값
MAX_APP_TIME = datetime(2100, 1, 1, tzinfo=timezone.utc).timestamp()
BACKWARD_TOLERANCE_SEC = 0.5    # 이 안쪽으로 뒤로 가는 변경은 오류 대신 '적용 안 함'
SESSION_MAX_JUMP_SEC = 2.0      # 세션 중 앞으로 갈 수 있는 최대 변화
TIMEZONE = "Asia/Seoul"         # 시간대 고정 (hardware/docs/db_design.md 결정 10)
FIXED_ZONES = {TIMEZONE: timezone(timedelta(hours=9), "KST")}   # zoneinfo 데이터가 없을 때 (서머타임 없음)


class ClockError(ValueError):
    """status: 422 잘못된 값 / 409 지금 상태에서 받을 수 없는 변경"""

    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code = code
        self.status = status


def load_zone(name: str) -> tzinfo:
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    except ImportError:
        pass
    if name in FIXED_ZONES:
        return FIXED_ZONES[name]
    raise ClockError("INVALID_TIMEZONE", f"시간대 '{name}'를 알 수 없음 (예: Asia/Seoul)")


class AppClock:
    def __init__(self, monotonic=time.monotonic, wall=time.time):
        self._mono = monotonic
        self._wall = wall
        self.boot_id = uuid.uuid4().hex[:12]        # 서버가 켜질 때마다 새로 (재부팅 포함)
        self.offset: float | None = None
        self.timezone_name: str | None = None
        self.tz: tzinfo | None = None
        self.synced_at: float | None = None         # 마지막 동기화 시각 (앱 시각)

    @property
    def synced(self) -> bool:
        return self.offset is not None

    def sync(self, app_time: float, timezone_name: str, *, in_session: bool = False) -> dict:
        """앱 시각으로 맞춘다. 돌려주는 값: applied(적용했는지), offset_change_sec(직전 대비 변화)"""
        if isinstance(app_time, bool) or not isinstance(app_time, (int, float)) \
                or not math.isfinite(app_time) or not MIN_APP_TIME <= app_time < MAX_APP_TIME:
            raise ClockError("INVALID_TIME", f"app_time {app_time!r}가 잘못됨 (time.time() 초, 2024년 이후)")
        if timezone_name != TIMEZONE:
            raise ClockError("INVALID_TIMEZONE", f"시간대는 {TIMEZONE}만 가능 (지금: {timezone_name!r})")
        tz = load_zone(timezone_name)
        new = app_time - self._mono()
        change = None if self.offset is None else new - self.offset
        # TODO(②): 첫 동기화(change is None)가 DB meta.last_seen보다 이전이면 거절
        if change is not None:
            if change < -BACKWARD_TOLERANCE_SEC:
                raise ClockError("CLOCK_BACKWARD", f"시각이 {-change:.1f}초 뒤로 가는 변경은 받을 수 없음 "
                                 f"(기록 시각이 뒤로 가면 안 됨)", status=409)
            if change < 0:
                log.info("시각 동기화: %.3f초 뒤로 → 적용 안 함 (오차 범위)", change)
                return {"applied": False, "offset_change_sec": round(change, 3)}
            if in_session and change > SESSION_MAX_JUMP_SEC:
                raise ClockError("CLOCK_JUMP_IN_SESSION", f"측정 중에는 시각을 {SESSION_MAX_JUMP_SEC:g}초 넘게 "
                                 f"바꿀 수 없음 (요청: +{change:.1f}초). 세션을 정지한 뒤 다시 보내세요",
                                 status=409)
            if change > SESSION_MAX_JUMP_SEC:
                log.warning("시각 동기화: 앱 시각이 %.1f초 앞으로 바뀜", change)
        self.offset, self.tz, self.timezone_name = new, tz, timezone_name
        self.synced_at = app_time
        log.info("시각 동기화: %s (%s), Pi 시계와 차이 %.1f초",
                 datetime.fromtimestamp(app_time, tz).isoformat(timespec="seconds"), timezone_name,
                 app_time - self._wall())
        return {"applied": True, "offset_change_sec": None if change is None else round(change, 3)}

    def now(self) -> float | None:
        """앱 기준 지금 시각. 동기화 전이면 None"""
        return None if self.offset is None else self._mono() + self.offset

    def now_or_pi(self) -> float:
        """동기화 전에는 Pi 시각 (기록에는 쓰지 않고 화면 표시용)"""
        t = self.now()
        return self._wall() if t is None else t

    def stamp(self, sample):
        """Pi 시각으로 찍힌 Sample → 앱 시각 (실제 센서 연결 때 사용)"""
        d = self.now() - self._wall()
        sample.ts += d
        sample.pressure.ts += d
        sample.distance.ts += d
        if sample.pose is not None:
            sample.pose.ts += d
        return sample

    def local(self, ts: float) -> datetime:
        return datetime.fromtimestamp(ts, self.tz or FIXED_ZONES["Asia/Seoul"])

    def snapshot(self) -> dict:
        t = self.now()
        return {
            "synced": self.synced,
            "boot_id": self.boot_id,
            "timezone": self.timezone_name,
            "app_time": t,
            "local_time": None if t is None else self.local(t).isoformat(timespec="seconds"),
            "synced_at": self.synced_at,
            "resync_required": not self.synced,
            "message": None if self.synced else
            "서버가 켜진 뒤 시각 동기화가 없음. POST /time/sync로 앱 시각과 시간대를 보내세요",
        }
