-- 002: 요약한 뒤 늦게 바뀐 날을 다시 계산하도록 표시 (hardware/docs/db_design.md 5장)
-- 요약을 만든 날에 걸친 착석 구간이 나중에 닫히거나(자정을 넘긴 구간), 비정상 종료 복구로 닫히면 1

ALTER TABLE day_jobs ADD COLUMN needs_recompute INTEGER NOT NULL DEFAULT 0 CHECK (needs_recompute IN (0, 1));
