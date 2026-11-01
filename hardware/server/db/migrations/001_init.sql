-- 001: 처음 테이블 (hardware/docs/db_design.md v1)
-- 시각은 모두 앱 기준 epoch 초(REAL). day('YYYY-MM-DD')·hour(0~23)는 Asia/Seoul로 계산해서 쓴다.

CREATE TABLE schema_migrations (
    version     INTEGER PRIMARY KEY,
    applied_at  REAL NOT NULL,
    description TEXT NOT NULL
);

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE day_jobs (
    day             TEXT PRIMARY KEY,
    summarized_at   REAL,
    summary_checked INTEGER NOT NULL DEFAULT 0 CHECK (summary_checked IN (0, 1)),
    raw_deleted_at  REAL,
    note            TEXT
);

CREATE TABLE sensor_layouts (
    id          INTEGER PRIMARY KEY,
    n_channels  INTEGER NOT NULL CHECK (n_channels BETWEEN 1 AND 8),
    channel_map TEXT NOT NULL,              -- JSON: 읽는 MCP3008 채널 번호 (순서 = p0, p1, …)
    positions   TEXT NOT NULL,              -- JSON: [[x, y], …]
    created_at  REAL NOT NULL,
    note        TEXT,
    UNIQUE (channel_map, positions)
);

CREATE TABLE sessions (
    id         TEXT PRIMARY KEY,            -- s_20261015_235950
    started_at REAL NOT NULL,
    ended_at   REAL,
    end_reason TEXT CHECK (end_reason IN ('stop', 'crash', 'baseline_failed')),
    boot_id    TEXT NOT NULL,
    timezone   TEXT NOT NULL,
    settings   TEXT NOT NULL,               -- JSON: 이 세션에 적용한 설정
    layout_id  INTEGER REFERENCES sensor_layouts(id)
);
CREATE INDEX idx_sessions_started ON sessions(started_at);

CREATE TABLE session_events (
    id         INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    ts         REAL NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('start', 'pause', 'resume', 'stop', 'time_sync', 'recovered')),
    detail     TEXT                         -- JSON
);
CREATE INDEX idx_events_session ON session_events(session_id, ts);

CREATE TABLE seating_segments (
    id                 INTEGER PRIMARY KEY,
    session_id         TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    start_ts           REAL NOT NULL,
    end_ts             REAL,                -- 진행 중이면 NULL
    day                TEXT NOT NULL,       -- 시작한 날
    end_reason         TEXT CHECK (end_reason IN ('away', 'pause', 'stop', 'crash')),
    first_abnormal_sec REAL,
    normal_sec         REAL NOT NULL DEFAULT 0,
    abnormal_sec       REAL NOT NULL DEFAULT 0,
    unknown_sec        REAL NOT NULL DEFAULT 0
);
CREATE INDEX idx_segments_start ON seating_segments(start_ts);
CREATE INDEX idx_segments_day ON seating_segments(day);

CREATE TABLE posture_hour (
    day     TEXT NOT NULL,
    hour    INTEGER NOT NULL CHECK (hour BETWEEN 0 AND 23),
    seat    TEXT NOT NULL,                  -- normal / lean_* / cross_* / away / unknown
    head    TEXT NOT NULL,                  -- normal / forward / unknown
    tilt    TEXT NOT NULL,                  -- none / left / right / unknown
    seconds REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (day, hour, seat, head, tilt)
) WITHOUT ROWID;

CREATE TABLE hour_metrics (
    day                TEXT NOT NULL,
    hour               INTEGER NOT NULL CHECK (hour BETWEEN 0 AND 23),
    seated_sec         REAL NOT NULL DEFAULT 0,
    distance_sum_mm    REAL NOT NULL DEFAULT 0,
    distance_n         INTEGER NOT NULL DEFAULT 0,
    distance_known_sec REAL NOT NULL DEFAULT 0,
    closer_sec         REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (day, hour)
) WITHOUT ROWID;

CREATE TABLE daily_summary (
    day                  TEXT PRIMARY KEY,
    seated_sec           REAL NOT NULL,
    normal_sec           REAL NOT NULL,
    abnormal_sec         REAL NOT NULL,
    unknown_sec          REAL NOT NULL,
    forward_head_sec     REAL NOT NULL,     -- 겹치지 않는 값 (우선순위 합산)
    cross_left_sec       REAL NOT NULL,
    cross_right_sec      REAL NOT NULL,
    lean_left_sec        REAL NOT NULL,
    lean_right_sec       REAL NOT NULL,
    tilt_left_sec        REAL NOT NULL,
    tilt_right_sec       REAL NOT NULL,
    raw_forward_head_sec REAL NOT NULL,     -- 겹치는 원래 값
    raw_cross_left_sec   REAL NOT NULL,
    raw_cross_right_sec  REAL NOT NULL,
    raw_lean_left_sec    REAL NOT NULL,
    raw_lean_right_sec   REAL NOT NULL,
    raw_tilt_left_sec    REAL NOT NULL,
    raw_tilt_right_sec   REAL NOT NULL,
    max_continuous_sec   REAL NOT NULL,
    segment_count        INTEGER NOT NULL,
    collapse_avg_sec     REAL,
    collapse_n           INTEGER NOT NULL,
    avg_distance_mm      REAL,
    closer_sec           REAL NOT NULL,
    calc_version         INTEGER NOT NULL,
    priority             TEXT NOT NULL,
    created_at           REAL NOT NULL,
    source               TEXT NOT NULL CHECK (source IN ('midnight', 'catchup', 'recompute'))
);

CREATE TABLE baselines (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('initial', 'session', 'recalibration', 'adjusted')),
    status      TEXT NOT NULL CHECK (status IN ('ok', 'failed')),
    fail_reason TEXT,                       -- no_person / sensor_lost / too_much_motion
    measured_at REAL NOT NULL,
    session_id  TEXT REFERENCES sessions(id) ON DELETE SET NULL,
    seconds     REAL,
    layout_id   INTEGER REFERENCES sensor_layouts(id),
    pressure    TEXT,                       -- JSON 채널별 평균
    distance_mm REAL,
    pose        TEXT,                       -- JSON 부위 7개 × [x, y, z, vis]
    based_on_id INTEGER REFERENCES baselines(id),
    is_current  INTEGER NOT NULL DEFAULT 0 CHECK (is_current IN (0, 1))
);
CREATE UNIQUE INDEX idx_baselines_current ON baselines(is_current) WHERE is_current = 1;

CREATE TABLE settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,               -- JSON
    updated_at REAL NOT NULL                -- 0 = 기본값 그대로
);
INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES
    ('goals', '[]', 0),
    ('posture_hold_sec', '3', 0),
    ('alert_after_sec', '300', 0),
    ('stand_reminder_sec', '3000', 0),
    ('alert_enabled', 'true', 0),
    ('stand_reminder_enabled', 'true', 0),
    ('raw_store_pose', 'false', 0);

CREATE TABLE raw_chunks (
    id             INTEGER PRIMARY KEY,
    session_id     TEXT REFERENCES sessions(id) ON DELETE CASCADE,
    start_ts       REAL NOT NULL,
    end_ts         REAL NOT NULL,
    n_samples      INTEGER NOT NULL,
    day            TEXT NOT NULL,
    layout_id      INTEGER NOT NULL REFERENCES sensor_layouts(id),
    has_pose       INTEGER NOT NULL CHECK (has_pose IN (0, 1)),
    codec          TEXT NOT NULL,           -- npz-zlib
    format_version INTEGER NOT NULL,
    data           BLOB NOT NULL
);
CREATE INDEX idx_raw_day ON raw_chunks(day);
CREATE INDEX idx_raw_start ON raw_chunks(start_ts);
