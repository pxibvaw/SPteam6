"""SQLite 연결 + 설정 (hardware/docs/db_design.md 3장)

쓰기 연결은 서버 실행 루프 하나만 쓴다. API는 읽기 연결(query_only)을 따로 연다.
WAL이라 읽기와 쓰기가 서로 막지 않는다. 트랜잭션은 직접 BEGIN/COMMIT 한다 (isolation_level=None).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

WRITE_PRAGMAS = (
    "PRAGMA journal_mode = WAL",            # 읽기·쓰기 동시, 순차 쓰기라 SD카드에 유리
    "PRAGMA synchronous = NORMAL",          # WAL에서는 정전 때 마지막 트랜잭션만 잃고 파일은 안 깨짐
    "PRAGMA wal_autocheckpoint = 1000",     # 약 4MB마다 본 파일에 반영
    "PRAGMA journal_size_limit = 67108864",
    "PRAGMA temp_store = MEMORY",
)


def connect(path: str | Path, *, readonly: bool = False) -> sqlite3.Connection:
    path = Path(path)
    new = not path.exists()
    if new and not readonly:
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if readonly:
        conn.execute("PRAGMA query_only = ON")
        return conn
    if new:
        conn.execute("PRAGMA auto_vacuum = INCREMENTAL")    # 테이블을 만들기 전에만 정할 수 있음
    for p in WRITE_PRAGMAS:
        conn.execute(p)
    return conn
