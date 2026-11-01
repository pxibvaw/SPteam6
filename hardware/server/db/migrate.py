"""스키마 버전 관리 — migrations/NNN_설명.sql을 순서대로 한 번씩 적용

- 현재 버전 = PRAGMA user_version (이력은 schema_migrations 테이블)
- 아직 적용 안 된 파일을 하나씩 한 트랜잭션으로 적용 (중간에 실패하면 그 파일은 통째로 취소)
- 기존 데이터가 있는 DB를 바꿀 때는 먼저 같은 폴더에 .bak으로 복사
- DB 버전이 코드보다 높으면(새 코드로 만든 DB를 옛 코드로 열면) 열지 않는다
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

log = logging.getLogger("sitsense.db")
MIGRATIONS_DIR = Path(__file__).with_name("migrations")


class MigrationError(RuntimeError):
    pass


def migration_files() -> list[tuple[int, Path]]:
    files = sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))
    return [(int(f.name[:3]), f) for f in files]


def latest_version() -> int:
    files = migration_files()
    return files[-1][0] if files else 0


def migrate(conn: sqlite3.Connection, db_path: str | Path | None = None) -> list[int]:
    """적용한 버전 목록을 돌려준다 (이미 최신이면 [])"""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    latest = latest_version()
    if current > latest:
        raise MigrationError(f"DB 스키마 버전({current})이 코드({latest})보다 높음 — 코드를 업데이트하세요")
    pending = [(v, f) for v, f in migration_files() if v > current]
    if not pending:
        return []
    if current > 0 and db_path is not None:
        backup = Path(db_path).with_suffix(Path(db_path).suffix + ".bak")
        with sqlite3.connect(backup) as dst:
            conn.backup(dst)
        log.info("마이그레이션 전 백업: %s", backup)
    applied = []
    for version, f in pending:
        sql = f.read_text(encoding="utf-8")
        desc = f.stem[4:]
        try:
            conn.executescript(
                "BEGIN IMMEDIATE;\n" + sql +
                f"\nINSERT INTO schema_migrations (version, applied_at, description) "
                f"VALUES ({version}, {time.time()}, '{desc}');\n"
                f"PRAGMA user_version = {version};\nCOMMIT;")
        except sqlite3.Error as e:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise MigrationError(f"{f.name} 적용 실패: {e}") from e
        log.info("스키마 %d 적용: %s", version, desc)
        applied.append(version)
    return applied
