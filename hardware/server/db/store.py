"""저장 경로 — 서버 실행 루프가 부르는 Recorder (hardware/docs/db_design.md)

쓰는 시점
    즉시   세션 시작·일시정지·재개·정지, 시각 재동기화, 착석 구간 시작·종료, 기준 자세, 기록 초기화
    1분마다 원본 덩어리 + posture_hour·hour_metrics 더하기 + 진행 중 구간 + meta.last_seen (한 트랜잭션)
시간 계산은 SeatingTracker와 똑같이 한다 (같은 시각, 같은 MAX_STEP_SEC):
    - 착석 구간 안의 시간만 센다 → posture_hour 합계 = 착석 구간 길이 합
    - 5초 미만 자리 비움은 seat=away로 넣고, 5초가 되어 구간이 끝나면 그 비움 시간은 버린다
    - 구간이 시작될 때 착석 확인 시간(1초)은 unknown
켤 때 열린 채 남은 세션·구간은 meta.last_seen에 사유 crash로 닫는다 (정전 손실 최대 1분).
"""
from __future__ import annotations

import io
import json
import logging
import sqlite3
from datetime import timedelta
from pathlib import Path

import numpy as np

from common.schema import LANDMARK_NAMES
from hardware.server.clock import TIMEZONE, AppClock
from hardware.server.db.connection import connect
from hardware.server.db.migrate import migrate
from hardware.server.seating import MAX_STEP_SEC

log = logging.getLogger("sitsense.db")

FLUSH_SEC = 60.0
RAW_CODEC = "npz-zlib"
RAW_FORMAT_VERSION = 1
DEFAULT_SETTINGS = {                # 001_init.sql의 기본값과 같게
    "goals": [], "posture_hold_sec": 3, "alert_after_sec": 300, "stand_reminder_sec": 3000,
    "alert_enabled": True, "stand_reminder_enabled": True, "raw_store_pose": False,
}
RECORD_TABLES = ("raw_chunks", "session_events", "seating_segments", "sessions", "posture_hour",
                 "hour_metrics", "daily_summary", "day_jobs")    # 기록 초기화 대상 (지우는 순서)


def _json(v) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def unpack_chunk(blob: bytes) -> dict[str, np.ndarray]:
    """raw_chunks.data → 배열 (t_ms, pressure, distance_mm, status, pose_detected[, cam_t_ms, landmarks])"""
    with np.load(io.BytesIO(blob)) as z:
        return {k: z[k] for k in z.files}


class Recorder:
    def __init__(self, path: str | Path, cfg: dict, clock: AppClock, *, flush_sec: float = FLUSH_SEC):
        self.path = Path(path)
        self.conn = connect(self.path)
        migrate(self.conn, self.path)
        self.clock = clock
        self.flush_sec = flush_sec
        self.tx_count = 0                       # 트랜잭션 수 (쓰기 횟수 확인용)
        self.layout_id, self.n_channels = self._ensure_layout(cfg)
        self.session_id: str | None = None
        self.session_settings: dict = {}        # 이 세션에 적용한 설정 (raw_store_pose 등)
        self._reset_memory()
        self.recovered = self._recover()

    def _reset_memory(self) -> None:
        self._seg = None                        # 진행 중 구간 (SeatingTracker의 Segment 객체)
        self._seg_row: int | None = None
        self._prev_ts: float | None = None
        self._away: list[tuple[float, float]] = []             # 확정 전 자리 비움 구간 [(t0, t1)]
        self._posture: dict[tuple, float] = {}                  # (day, hour, seat, head, tilt) → 초
        self._metrics: dict[tuple, list[float]] = {}            # (day, hour) → [착석, 거리합, 거리수, 거리 잰 초, 가까운 초]
        self._raw: list[tuple] = []
        self._raw_day: str | None = None
        self._chunks: list[dict] = []
        self._last_pose_ts: float | None = None
        self._last_flush: float | None = None

    # --- 트랜잭션 -------------------------------------------------------------
    def _tx(self, fn) -> None:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            fn(self.conn)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        self.tx_count += 1

    def _set_last_seen(self, c: sqlite3.Connection, ts: float) -> None:
        c.execute("INSERT INTO meta (key, value) VALUES ('last_seen', ?) "
                  "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (repr(ts),))

    # --- 시작할 때 --------------------------------------------------------------
    def _ensure_layout(self, cfg: dict) -> tuple[int, int]:
        pr = cfg["pressure"]
        n = pr["n_channels"]
        channel_map = _json(pr.get("channels", list(range(n))))     # config.yaml에 없으면 0 ~ n-1
        positions = _json(pr["positions"])
        row = self.conn.execute("SELECT id FROM sensor_layouts WHERE channel_map = ? AND positions = ?",
                                (channel_map, positions)).fetchone()
        if row:
            return row["id"], n
        out = {}

        def insert(c):
            out["id"] = c.execute(
                "INSERT INTO sensor_layouts (n_channels, channel_map, positions, created_at, note) "
                "VALUES (?, ?, ?, ?, ?)", (n, channel_map, positions, self.clock.now_or_pi(),
                                           "config.yaml에서 자동 추가")).lastrowid
        self._tx(insert)
        log.info("센서 배치 추가: id=%d, %d채널", out["id"], n)
        return out["id"], n

    def last_seen(self) -> float | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = 'last_seen'").fetchone()
        return float(row["value"]) if row else None

    def _recover(self) -> dict:
        """열린 채 남은 세션·구간을 last_seen에 crash로 닫는다"""
        ls = self.last_seen()
        if ls is None:
            return {"sessions": 0, "segments": 0}
        out = {}

        def fix(c):
            open_sessions = [r["id"] for r in c.execute("SELECT id FROM sessions WHERE ended_at IS NULL")]
            for sid in open_sessions:
                c.execute("UPDATE sessions SET ended_at = MAX(started_at, ?), end_reason = 'crash' WHERE id = ?",
                          (ls, sid))
                c.execute("INSERT INTO session_events (session_id, ts, kind, detail) VALUES (?, ?, 'recovered', ?)",
                          (sid, ls, _json({"reason": "서버가 정지 없이 꺼짐"})))
            out["segments"] = c.execute(
                "UPDATE seating_segments SET end_ts = MAX(start_ts, ?), end_reason = 'crash' "
                "WHERE end_ts IS NULL", (ls,)).rowcount
            out["sessions"] = len(open_sessions)
        self._tx(fix)
        if out["sessions"] or out["segments"]:
            log.warning("비정상 종료 복구: 세션 %d개, 구간 %d개를 %s에 닫음", out["sessions"], out["segments"],
                        self.clock.local(ls).isoformat(timespec="seconds"))
        return out

    def settings(self) -> dict:
        s = dict(DEFAULT_SETTINGS)
        for r in self.conn.execute("SELECT key, value FROM settings"):
            s[r["key"]] = json.loads(r["value"])
        return s

    def current_baseline(self) -> dict | None:
        """지금 기준 (채널 수가 지금 배치와 다르면 쓸 수 없으므로 None)"""
        r = self.conn.execute("SELECT * FROM baselines WHERE is_current = 1").fetchone()
        if r is None:
            return None
        pressure = json.loads(r["pressure"])
        if len(pressure) != self.n_channels:
            log.warning("저장된 기준 자세의 압력 채널(%d)이 지금(%d)과 달라 쓰지 않음", len(pressure), self.n_channels)
            return None
        return {"id": r["id"], "kind": r["kind"], "measured_at": r["measured_at"], "seconds": r["seconds"],
                "pressure": pressure, "distance_mm": r["distance_mm"],
                "pose": json.loads(r["pose"]) if r["pose"] else None}

    # --- 세션 · 기준 자세 (즉시) ------------------------------------------------
    def session_started(self, sid: str, ts: float, settings: dict) -> None:
        self.flush(ts)
        self.session_id, self._prev_ts = sid, None
        self.session_settings = dict(settings)

        def w(c):
            c.execute("INSERT INTO sessions (id, started_at, boot_id, timezone, settings, layout_id) "
                      "VALUES (?, ?, ?, ?, ?, ?)",
                      (sid, ts, self.clock.boot_id, TIMEZONE, _json(settings), self.layout_id))
            c.execute("INSERT INTO session_events (session_id, ts, kind) VALUES (?, ?, 'start')", (sid, ts))
            self._set_last_seen(c, ts)
        self._tx(w)

    def event(self, kind: str, ts: float, detail: dict | None = None) -> None:
        """pause / resume / time_sync (세션 중일 때만)"""
        if self.session_id is None:
            return
        self.flush(ts)

        def w(c):
            c.execute("INSERT INTO session_events (session_id, ts, kind, detail) VALUES (?, ?, ?, ?)",
                      (self.session_id, ts, kind, _json(detail) if detail else None))
            self._set_last_seen(c, ts)
        self._tx(w)

    def session_ended(self, ts: float, reason: str = "stop") -> None:
        if self.session_id is None:
            return
        self.flush(ts)
        sid = self.session_id

        def w(c):
            c.execute("UPDATE sessions SET ended_at = ?, end_reason = ? WHERE id = ?", (ts, reason, sid))
            c.execute("INSERT INTO session_events (session_id, ts, kind, detail) VALUES (?, ?, 'stop', ?)",
                      (sid, ts, _json({"reason": reason})))
            self._set_last_seen(c, ts)
        self._tx(w)
        self.session_id = None

    def save_baseline(self, result: dict) -> int:
        """측정 결과 저장. 성공이면 현재 기준으로 표시, 실패면 이력만 (현재 기준 유지)"""
        v = result.get("value")
        out = {}

        def w(c):
            prev = c.execute("SELECT id FROM baselines WHERE is_current = 1").fetchone()
            ok = result["status"] == "ok"
            if ok and prev:
                c.execute("UPDATE baselines SET is_current = 0 WHERE id = ?", (prev["id"],))
            out["id"] = c.execute(
                "INSERT INTO baselines (kind, status, fail_reason, measured_at, session_id, seconds, layout_id, "
                "pressure, distance_mm, pose, based_on_id, is_current) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (result["kind"], result["status"], result.get("fail_reason"), result["measured_at"],
                 self.session_id, result.get("seconds"), self.layout_id,
                 _json(v.pressure) if v else None, v.distance_mm if v else None,
                 _json(v.pose) if v and v.pose else None, prev["id"] if prev else None, int(ok))).lastrowid
            self._set_last_seen(c, result["measured_at"])
        self._tx(w)
        return out["id"]

    # --- 착석 구간 --------------------------------------------------------------
    def sync_segment(self, tracker, ts: float) -> None:
        """착석 구간이 끝났거나 새로 시작됐으면 바로 쓴다 (tick·일시정지·정지 뒤에 부름)"""
        cur = tracker.current
        if self._seg is not None and self._seg is not cur:
            self._close_segment(self._seg)
        if cur is not None and self._seg is not cur:
            self._open_segment(cur, ts)

    def _open_segment(self, seg, ts: float) -> None:
        out = {}

        def w(c):
            out["id"] = c.execute(
                "INSERT INTO seating_segments (session_id, start_ts, day) VALUES (?, ?, ?)",
                (self.session_id, seg.start, self._day(seg.start))).lastrowid
            self._set_last_seen(c, ts)
        self._tx(w)
        self._seg, self._seg_row, self._prev_ts = seg, out["id"], ts
        self._add(seg.start, ts, ("unknown", "unknown", "unknown"))      # 착석 확인하는 동안 = unknown

    def _close_segment(self, seg) -> None:
        # 자리 비움 5초로 끝났으면 구간 끝(첫 빈 샘플) 뒤의 비움은 착석이 아님 (SeatingTracker와 같게)
        self._commit_away(until=seg.end if seg.end_reason == "away" else None)
        row = self._seg_row

        def w(c):
            c.execute("UPDATE seating_segments SET end_ts = ?, end_reason = ?, first_abnormal_sec = ?, "
                      "normal_sec = ?, abnormal_sec = ?, unknown_sec = ? WHERE id = ?",
                      (seg.end, seg.end_reason, seg.first_abnormal_sec, seg.normal_sec, seg.abnormal_sec,
                       seg.unknown_sec, row))
            self._set_last_seen(c, seg.end)
        self._tx(w)
        self._seg = self._seg_row = self._prev_ts = None

    # --- 매 순간 (모아 두기) -------------------------------------------------------
    def on_tick(self, ts: float, sample, latest: dict, tracker, empty: bool) -> None:
        """판단한 순간마다 (세션 running). 원본을 모으고 착석 구간 안의 시간을 센다"""
        self._add_raw(ts, sample)
        just_opened = self._seg is None and tracker.current is not None
        self.sync_segment(tracker, ts)
        if self._seg is not None and not just_opened and self._prev_ts is not None:
            dt = min(max(ts - self._prev_ts, 0.0), MAX_STEP_SEC)
            if empty:
                self._away.append((ts - dt, ts))
            else:
                self._commit_away()
                seat = latest["seat_state"].value
                combo = ("unknown" if seat == "empty" else seat, latest["head_state"].value,
                         latest["tilt_state"].value)
                self._add(ts - dt, ts, combo)
                day, hour = self._day_hour(ts)
                m = self._metric(day, hour)
                if latest["distance_mm"] is not None:
                    m[1] += latest["distance_mm"]
                    m[2] += 1
                    m[3] += dt
                if latest["closer_than_baseline"]:
                    m[4] += dt
            self._prev_ts = ts
        self.maybe_flush(ts)

    def _day_hour(self, ts: float) -> tuple[str, int]:
        d = self.clock.local(ts)
        return d.strftime("%Y-%m-%d"), d.hour

    def _day(self, ts: float) -> str:
        return self.clock.local(ts).strftime("%Y-%m-%d")

    def _split(self, t0: float, t1: float) -> list[tuple[str, int, float]]:
        """[t0, t1)을 한국 시간 정각에서 나눔 → (day, hour, 초)"""
        out, t = [], t0
        while t < t1 - 1e-9:
            d = self.clock.local(t)
            nxt = (d.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)).timestamp()
            e = min(t1, nxt)
            out.append((d.strftime("%Y-%m-%d"), d.hour, e - t))
            t = e
        return out

    def _metric(self, day: str, hour: int) -> list[float]:
        return self._metrics.setdefault((day, hour), [0.0, 0.0, 0, 0.0, 0.0])

    def _add(self, t0: float, t1: float, combo: tuple[str, str, str]) -> None:
        self._add_parts(self._split(t0, t1), combo)

    def _add_parts(self, parts, combo) -> None:
        for day, hour, sec in parts:
            key = (day, hour, *combo)
            self._posture[key] = self._posture.get(key, 0.0) + sec
            self._metric(day, hour)[0] += sec

    def _commit_away(self, until: float | None = None) -> None:
        """모아 둔 자리 비움을 seat=away로 넣는다. until이 있으면 그 시각 이전 부분만"""
        for t0, t1 in self._away:
            if until is not None:
                t1 = min(t1, until)
            if t1 > t0:
                self._add(t0, t1, ("away", "unknown", "unknown"))
        self._away = []

    # --- 원본 -----------------------------------------------------------------
    def _add_raw(self, ts: float, sample) -> None:
        day = self._day(ts)
        if self._raw and (day != self._raw_day or ts - self._raw[0][0] >= self.flush_sec):
            self._pack_raw()                    # 1분 또는 자정에서 덩어리를 나눔
        self._raw_day = day
        pose = sample.pose
        frame = None
        if self._store_pose and pose is not None and pose.detected and pose.ts != self._last_pose_ts:
            frame = (pose.ts, [[getattr(pose.points[n], a) if n in pose.points else np.nan
                                for a in ("x", "y", "z", "visibility")] for n in LANDMARK_NAMES])
        if pose is not None:
            self._last_pose_ts = pose.ts
        d = sample.distance
        self._raw.append((ts, list(sample.pressure.values), d.distance_mm if d.valid else None, d.range_status,
                          int(bool(pose and pose.detected)), frame))

    @property
    def _store_pose(self) -> bool:
        return bool(self.session_settings.get("raw_store_pose", False))

    def _pack_raw(self) -> None:
        rows, self._raw = self._raw, []
        if not rows:
            return
        ts = np.array([r[0] for r in rows])
        arrays = {
            "t_ms": np.round((ts - ts[0]) * 1000).astype(np.int32),
            "pressure": np.array([r[1] for r in rows], dtype=np.uint16),
            "distance_mm": np.array([-1 if r[2] is None else r[2] for r in rows], dtype=np.int16),
            "status": np.array([r[3] for r in rows], dtype=np.int8),
            "pose_detected": np.array([r[4] for r in rows], dtype=np.uint8),
        }
        frames = [r[5] for r in rows if r[5] is not None]
        if frames:
            arrays["cam_t_ms"] = np.round((np.array([f[0] for f in frames]) - ts[0]) * 1000).astype(np.int32)
            arrays["landmarks"] = np.array([f[1] for f in frames], dtype=np.float32)
        buf = io.BytesIO()
        np.savez_compressed(buf, **arrays)
        self._chunks.append({"start_ts": float(ts[0]), "end_ts": float(ts[-1]), "n": len(rows),
                             "day": self._day(float(ts[0])), "has_pose": int(bool(frames)),
                             "data": buf.getvalue()})

    # --- 1분마다 쓰기 -------------------------------------------------------------
    def maybe_flush(self, ts: float) -> None:
        if self._last_flush is None:
            self._last_flush = ts
        elif ts - self._last_flush >= self.flush_sec:
            self.flush(ts)

    def flush(self, ts: float) -> None:
        """모아 둔 것을 한 트랜잭션으로 쓴다"""
        self._pack_raw()
        chunks, posture, metrics = self._chunks, self._posture, self._metrics
        self._chunks, self._posture, self._metrics = [], {}, {}
        seg, row = self._seg, self._seg_row

        def w(c):
            for ch in chunks:
                c.execute("INSERT INTO raw_chunks (session_id, start_ts, end_ts, n_samples, day, layout_id, has_pose, "
                          "codec, format_version, data) VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (self.session_id, ch["start_ts"], ch["end_ts"], ch["n"], ch["day"], self.layout_id,
                           ch["has_pose"], RAW_CODEC, RAW_FORMAT_VERSION, ch["data"]))
            c.executemany("INSERT INTO posture_hour (day, hour, seat, head, tilt, seconds) VALUES (?,?,?,?,?,?) "
                          "ON CONFLICT DO UPDATE SET seconds = seconds + excluded.seconds",
                          [(*k, v) for k, v in posture.items()])
            c.executemany("INSERT INTO hour_metrics (day, hour, seated_sec, distance_sum_mm, distance_n, "
                          "distance_known_sec, closer_sec) VALUES (?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET "
                          "seated_sec = seated_sec + excluded.seated_sec, "
                          "distance_sum_mm = distance_sum_mm + excluded.distance_sum_mm, "
                          "distance_n = distance_n + excluded.distance_n, "
                          "distance_known_sec = distance_known_sec + excluded.distance_known_sec, "
                          "closer_sec = closer_sec + excluded.closer_sec",
                          [(*k, *v) for k, v in metrics.items()])
            if seg is not None:
                c.execute("UPDATE seating_segments SET first_abnormal_sec = ?, normal_sec = ?, abnormal_sec = ?, "
                          "unknown_sec = ? WHERE id = ?", (seg.first_abnormal_sec, seg.normal_sec,
                                                           seg.abnormal_sec, seg.unknown_sec, row))
            self._set_last_seen(c, ts)
        self._tx(w)
        self._last_flush = ts

    # --- 기록 초기화 · 종료 -------------------------------------------------------
    def reset_records(self) -> dict[str, int]:
        """기록만 삭제 (설정·기준 자세·센서 배치는 유지). 세션이 없을 때만 부른다"""
        out = {}

        def w(c):
            for t in RECORD_TABLES:
                out[t] = c.execute(f"DELETE FROM {t}").rowcount
            c.execute("DELETE FROM meta WHERE key = 'last_seen'")
        self._tx(w)
        self._reset_memory()
        try:
            self.conn.execute("PRAGMA incremental_vacuum")
        except sqlite3.Error:
            pass
        log.warning("기록 초기화: %s", out)
        return out

    def close(self, ts: float | None = None) -> None:
        if ts is not None:
            self.flush(ts)
        self.conn.close()
