"""세션 — 시작 / 일시정지 / 재개 / 정지

상태: idle(세션 없음) → running ⇄ paused → (정지) idle
    elapsed_sec = 시작부터 지금(끝났으면 끝)까지 − 일시정지한 시간
시각은 모두 앱 기준(clock.AppClock). 동기화 전에는 시작할 수 없다.
"""
from __future__ import annotations

from enum import Enum


class SessionState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"


class SessionError(Exception):
    """앱이 code를 보고 처리할 수 있는 오류 (HTTP 409)"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class SessionController:
    def __init__(self):
        self.state = SessionState.IDLE
        self.session_id: str | None = None
        self.started_at: float | None = None
        self.ended_at: float | None = None
        self.paused_total = 0.0
        self.paused_since: float | None = None

    def start(self, ts: float, session_id: str) -> None:
        if self.state is not SessionState.IDLE:
            raise SessionError("SESSION_ACTIVE", f"이미 세션이 있음 ({self.state.value}). 정지한 뒤 시작하세요")
        self.state = SessionState.RUNNING
        self.session_id, self.started_at, self.ended_at = session_id, ts, None
        self.paused_total, self.paused_since = 0.0, None

    def pause(self, ts: float) -> None:
        if self.state is not SessionState.RUNNING:
            raise SessionError("NOT_RUNNING", f"실행 중이 아니라 일시정지할 수 없음 ({self.state.value})")
        self.state, self.paused_since = SessionState.PAUSED, ts

    def resume(self, ts: float) -> None:
        if self.state is not SessionState.PAUSED:
            raise SessionError("NOT_PAUSED", f"일시정지 상태가 아니라 재개할 수 없음 ({self.state.value})")
        self.paused_total += ts - self.paused_since
        self.state, self.paused_since = SessionState.RUNNING, None

    def stop(self, ts: float) -> None:
        if self.state is SessionState.IDLE:
            raise SessionError("NO_SESSION", "정지할 세션이 없음")
        if self.paused_since is not None:
            self.paused_total += ts - self.paused_since
            self.paused_since = None
        self.state, self.ended_at = SessionState.IDLE, ts

    def paused_sec(self, ts: float) -> float:
        return self.paused_total + (ts - self.paused_since if self.paused_since is not None else 0.0)

    def elapsed_sec(self, ts: float) -> float | None:
        if self.started_at is None:
            return None
        end = self.ended_at if self.ended_at is not None else ts
        return max(0.0, end - self.started_at - self.paused_sec(end))
