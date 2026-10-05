# SitSense SQLite 저장 설계

> **상태: 설계 확정 v1 (하드웨어 담당, 코드 구현 전).** 구현은 아래 순서로 단계마다 계획을 확인받고 진행한다.
> ① 시간 동기화 + 세션 제어 → ② 스키마·마이그레이션 + 저장 경로 → ③ 자정 요약 + 원본 삭제(밀린 날짜) → ④ 읽기 API
> → ⑤ 설정·알림·기기 상태 (설정 변경 API 포함)

구조: 외부 서버 없이 Pi 한 대. **센서 → 판단 → SQLite → FastAPI**, 앱은 같은 Wi-Fi로 접속한다.

## 0. 확정된 결정

| # | 항목 | 결정 |
|---|---|---|
| 1 | 원본 보관 | **날짜 단위 삭제.** 자정에 요약 → 요약 확인 → 30분 뒤 전날 원본 삭제 (최대 24시간 30분 보관) |
| 2 | 정상 비율 | **정상 ÷ (총 착석 − unknown).** 화면의 "총 착석"은 unknown 포함 |
| 3 | 자정을 넘는 구간의 무너짐 시간 | **시작한 날**에 넣는다 |
| 4 | 앱에서 바꾼 설정 | **다음 세션부터** 적용. 앱 설정이 `config.yaml`보다 우선 |
| 5 | 기준 자세 보정 결과 | AI가 준 값을 **저장만** 한다. 재측정이 실패하면 현재 기준 유지 |
| 6 | 원본의 카메라 좌표 | **기본 저장 안 함** (설정 `raw_store_pose`로 켜기). `pose_detected`만 저장 |
| 7 | 시각 재동기화 | 세션 중 **2초 넘게 앞으로 가는 변경은 거절**, **뒤로 가는 변경은 항상 적용 안 함** (0.5초 이내는 200 `applied:false`, 넘으면 409). 판단은 **한 번 바뀌는 양**만 본다 |
| 8 | 기록 초기화 | **기록만** 삭제. 설정·기준 자세·센서 배치는 유지 |
| 9 | 정전 손실 | 최대 1분 손실 허용 |
| 10 | 시간대 | **Asia/Seoul 고정** |
| 11 | 사용자 | 1명 (테이블에 user_id 없음) |
| 12 | CSV 내보내기 | **요약 전부 + 남은 원본(옵션)** |

## 1. 기본 방침

| 항목 | 내용 |
|---|---|
| 파일 | Pi의 `data/sitsense.db` (`.gitignore`의 `*.db`로 git 제외) |
| 코드 위치 | `hardware/server/db/` — 연결·PRAGMA, `migrations/NNN_*.sql` |
| 시각 | 모두 **앱 기준 epoch 초(REAL)** (`hardware/server/clock.py`). `day`(`'YYYY-MM-DD'`)·`hour`(0~23)는 **쓸 때 Asia/Seoul로 계산해 같이 저장** |
| 동기화 전 | 아무것도 쓰지 않는다 (동기화 전에는 세션 시작도 거절) |
| 쓰는 쪽 | 서버 실행 루프 **하나만** 쓴다. API는 읽기 연결을 따로 연다 (WAL이라 서로 막지 않음) |
| 시간 계산 | `posture_hour`·`hour_metrics`는 **착석 구간 안의 시간만**. 하루 합계 = 총 착석 |

## 2. 테이블

### 관리

**`schema_migrations`** — 스키마 버전 이력

| 열 | 형식 | 설명 |
|---|---|---|
| `version` | INTEGER PK | 1, 2, 3 … |
| `applied_at` | REAL | |
| `description` | TEXT | |

- 현재 버전은 `PRAGMA user_version`에도 둔다.
- 서버 시작 때 아직 적용 안 된 `migrations/NNN_*.sql`을 **한 트랜잭션**으로 적용. 적용 전에 DB 파일을 `.bak`으로 복사.
- 원본 BLOB 형식은 따로 `raw_chunks.format_version`, 요약 계산 방식은 `daily_summary.calc_version`으로 관리.

**`meta`** — `key` TEXT PK, `value` TEXT
- `last_seen`: 마지막 저장 시각 (비정상 종료 복구, 시각이 뒤로 갔는지 확인)
- `device_id`, `created_at`

**`day_jobs`** — 자정 작업 상태 (날짜별 한 줄)

| 열 | 설명 |
|---|---|
| `day` PK | |
| `summarized_at` | 요약 만든 시각 |
| `summary_checked` | 요약을 다시 읽어 확인했는지 (0/1) |
| `raw_deleted_at` | 원본 삭제 시각 |
| `note` | `midnight` / `catchup` |

### 센서 배치 (8 → 6채널 대응)

**`sensor_layouts`**

| 열 | 설명 |
|---|---|
| `id` PK | |
| `n_channels` | 6 또는 8 |
| `channel_map` | JSON — 읽는 **MCP3008 채널 번호 목록** (순서 = p0, p1, …). 예: 8개 중 6개 선별 `[0, 1, 2, 3, 6, 7]` |
| `positions` | JSON `[[x, y], …]` (길이 = n_channels) |
| `created_at`, `note` | |

- 서버 시작 때 `config.yaml`의 배치(`n_channels`, `positions`, 채널 목록)와 같은 줄이 없으면 새로 만든다.
- 원본·기준 자세는 이 `id`를 가리킨다 → 채널 수·배치가 바뀌어도 예전 기록을 해석할 수 있다.

### 세션

**`sessions`**

| 열 | 설명 |
|---|---|
| `id` TEXT PK | `s_20261015_235950` |
| `started_at`, `ended_at` | 진행 중이면 `ended_at` NULL |
| `end_reason` | `stop` / `crash` (켤 때 열린 채 남은 세션을 복구) |
| `boot_id`, `timezone` | |
| `settings` | JSON — **이 세션에 적용한 설정 스냅샷** (결정 4: 다음 세션부터 적용) |
| `layout_id` FK | |

**`session_events`** — 일시정지 이력 포함

| 열 | 설명 |
|---|---|
| `id` PK, `session_id` FK, `ts` | |
| `kind` | `start` / `pause` / `resume` / `stop` / `time_sync` / `recovered` |
| `detail` | JSON (예: 재동기화 변화량) |

### 착석 구간

**`seating_segments`** (SeatingTracker 결과)

| 열 | 설명 |
|---|---|
| `id` PK, `session_id` FK | |
| `start`, `end` | 진행 중이면 `end` NULL |
| `day` | **시작한 날** (결정 3) |
| `end_reason` | `away` / `pause` / `stop` / `crash` |
| `first_abnormal_sec` | 첫 비정상 확정까지 초 (없으면 NULL) |
| `normal_sec`, `abnormal_sec`, `unknown_sec` | |

- 구간이 **시작될 때** 한 줄 넣고 1분마다 갱신 → 정전 손실 최대 1분.
- 켤 때 `end`가 NULL인 줄은 `end = meta.last_seen`, `end_reason = 'crash'`로 닫는다.

### 시간대별 (영구 보관)

**`posture_hour`** — 자세 조합별 시간 (우선순위 합산 전 원래 값)

| 열 | 설명 |
|---|---|
| `day`, `hour` | |
| `seat` | `normal` / `lean_left` / `lean_right` / `cross_left` / `cross_right` / `away`(5초 미만 비움) / `unknown` |
| `head` | `normal` / `forward` / `unknown` |
| `tilt` | `none` / `left` / `right` / `unknown` |
| `seconds` | REAL |

- PK (day, hour, seat, head, tilt). 정각을 넘는 순간은 두 시간대로 나눠 더한다.
- 메모리에서 모아 1분마다 upsert(`seconds = seconds + ?`).
- 겹치지 않는 값(대표 자세)은 우선순위(거북목 > 다리 꼬기 > 체중 편향 > 기울어진 자세)로 계산한다.

**`hour_metrics`** — 화면 거리

| 열 | 설명 |
|---|---|
| `day`, `hour` | PK |
| `seated_sec` | |
| `distance_sum_mm`, `distance_n` | 평균 = 합 ÷ 개수 |
| `distance_known_sec` | 거리를 잰 시간 |
| `closer_sec` | 기준보다 10cm 이상 가까웠던 시간 |

### 하루 요약 (자정 생성, 영구 보관)

**`daily_summary`**

| 열 | 설명 |
|---|---|
| `day` PK | |
| `seated_sec` | 총 착석 (unknown 포함, 화면 표시용) |
| `normal_sec`, `abnormal_sec`, `unknown_sec` | 정상 비율 = `normal_sec ÷ (seated_sec − unknown_sec)` (결정 2) |
| `forward_head_sec`, `cross_left_sec`, `cross_right_sec`, `lean_left_sec`, `lean_right_sec`, `tilt_left_sec`, `tilt_right_sec` | **겹치지 않는 값** (우선순위 합산) |
| `raw_forward_head_sec` … `raw_tilt_right_sec` | **겹치는 원래 값** (목표 카드 누락 방지) |
| `max_continuous_sec` | 최대 연속 착석 (**자정에서 자름**) |
| `segment_count` | |
| `collapse_avg_sec`, `collapse_n` | 무너짐 평균 (그날 **시작한** 구간 기준) |
| `avg_distance_mm`, `closer_sec` | |
| `calc_version`, `priority` | 계산 방식·우선순위가 바뀌면 다시 계산 가능 |
| `created_at`, `source` | `midnight` / `catchup` / `recompute` |

모두 `posture_hour`·`hour_metrics`·`seating_segments`에서 계산되므로 원본이 지워져도 다시 만들 수 있다.
오늘 값은 요약 없이 같은 계산을 실시간으로 한다.

### 기준 자세 (이력)

**`baselines`**

| 열 | 설명 |
|---|---|
| `id` PK | |
| `kind` | `initial` / `recalibration` / `adjusted`(AI 보정 결과) |
| `status` | `ok` / `failed` |
| `fail_reason` | `no_person` / `sensor_lost` / `too_much_motion` |
| `measured_at`, `session_id`, `seconds` | |
| `layout_id` FK | |
| `pressure` | JSON 채널별 평균 |
| `distance_mm` | |
| `pose` | JSON 부위 7개 × [x, y, z, vis] (기준값이라 좌표 저장) |
| `based_on_id` | 보정 결과면 원래 기준 id |
| `is_current` | 현재 값 (부분 unique 인덱스로 하나만) |

- `adjusted`는 AI가 준 값을 **저장만** 한다 (결정 5). 재측정이 `failed`면 `is_current`는 그대로.
- 앱에는 `is_current = 1` 하나만 보인다.

### 설정

**`settings`** — `key` TEXT PK, `value` JSON, `updated_at`

| key | 기본값 | 설명 |
|---|---|---|
| `goals` | `[]` | 개선 목표 (4종 중 최대 2개) |
| `posture_hold_sec` | 3 | 자세 인정 기준 |
| `alert_after_sec` | 300 | 알림 기준 |
| `stand_reminder_sec` | 3000 | 일어나기 알림 |
| `alert_enabled`, `stand_reminder_enabled` | true | 켜짐 여부 |
| `raw_store_pose` | false | 원본에 카메라 좌표 저장 (결정 6) |

- 값 검사는 코드에서 (목표 최대 2개, 범위).
- **앱 설정이 `config.yaml`보다 우선**하고, **다음 세션부터** 적용 (세션 시작 때 `sessions.settings`에 스냅샷).
- ②에서는 **테이블과 기본값만** 만든다. 설정을 바꾸는 API는 **⑤(설정·알림·기기 상태)**에서.

### 원본 샘플 (최대 24시간 30분)

**`raw_chunks`** — 1분에 한 줄

| 열 | 설명 |
|---|---|
| `id` PK, `session_id` | |
| `start_ts`, `end_ts`, `n_samples` | |
| `day` | 삭제 대상 고르기 (인덱스) |
| `layout_id` FK | |
| `has_pose` | 카메라 좌표 포함 여부 |
| `codec` | `npz-zlib` |
| `format_version` | |
| `data` BLOB | 아래 배열을 압축 |

`data` 안의 배열:
- 시각: 시작 기준 ms (int32)
- 압력: uint16 **[샘플 수 × n_channels]** — 열 이름(`p0~p7`)이 아니라 배열이라 채널 수가 바뀌어도 테이블이 그대로
- 거리 mm (int16, 실패는 −1), 상태 (int8)
- `pose_detected` (uint8)
- `has_pose`일 때만: 새 카메라 프레임마다 시각 + 7점 × (x, y, z, vis)

CSV로 내보낼 때는 덩어리마다 `layout_id`의 `n_channels`를 보고 `common/recorder.py`와 같은 열로 푼다. **영상은 저장하지 않는다.**

## 3. 쓰기 방식과 SQLite 설정

- **1분마다 한 트랜잭션:** 원본 덩어리 1줄 + `posture_hour`·`hour_metrics` 더하기 + 진행 중 구간 갱신 + `meta.last_seen`.
- **즉시 쓰기 (드묾):** 세션 시작·일시정지·재개·정지, 구간 종료, 기준 자세, 설정 변경, 시각 재동기화.
- 일시정지·정지·재동기화·서버 종료 때는 모아 둔 것도 **바로 한 번 더** 쓴다.

```
PRAGMA journal_mode = WAL;          -- 읽기·쓰기 동시, 순차 쓰기라 SD카드에 유리
PRAGMA synchronous = NORMAL;        -- WAL에서는 정전 때 마지막 트랜잭션만 잃고 파일은 안 깨짐
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
PRAGMA wal_autocheckpoint = 1000;   -- 약 4MB마다 본 파일에 반영
PRAGMA journal_size_limit = 67108864;
PRAGMA auto_vacuum = INCREMENTAL;   -- DB를 처음 만들 때만. 원본 삭제 후 incremental_vacuum으로 조금씩 반환
PRAGMA temp_store = MEMORY;
```

## 4. 시각 규칙 (결정 7)

| 경우 | 동작 |
|---|---|
| 서버가 켜진 뒤 첫 동기화 | 받아들인다. 단 **DB의 `meta.last_seen`보다 이전 시각이면 거절** (②에서 오류 코드·복구 방법 확정) |
| 세션 밖에서 재동기화, 앞으로 | 받아들인다 |
| 세션 중 재동기화, 앞으로 2초 이내 | 받아들인다 |
| 세션 중 재동기화, 2초 넘게 앞으로 | **409 `CLOCK_JUMP_IN_SESSION`** |
| 뒤로 0.5초 이내 (세션 안팎) | **적용하지 않고 200** (`applied: false`) — 네트워크 지연 오차 |
| 뒤로 0.5초 넘게 (세션 안팎) | **409 `CLOCK_BACKWARD`** |
| Asia/Seoul이 아닌 시간대 | **422 `INVALID_TIMEZONE`** (결정 10) |

변화량은 **한 번 바뀌는 양**(직전 동기화 대비)만 본다. 거절·미적용이면 기존 시각을 그대로 쓴다.

## 5. 자정 작업 (밀린 날짜 포함)

1분마다, 그리고 **서버가 켜진 뒤 첫 시각 동기화 직후** 확인한다 (동기화 전에는 오늘 날짜를 모른다).

1. 오늘보다 이전이면서 `summarized_at`이 없는 날을 **오래된 날부터** 요약 → 다시 읽어 확인 → `summary_checked = 1`.
2. `summary_checked = 1`이고 `summarized_at + 30분`이 지났고 `raw_deleted_at`이 없는 날 → 그날 `raw_chunks` 삭제 → `raw_deleted_at` 기록.
3. Pi가 꺼져 있었으면 1, 2를 밀린 날짜 순서대로 처리 (`note = catchup`).
4. 자정을 넘겨 측정 중이면 시간대별 표는 이미 나뉘어 있고, 최대 연속 착석은 진행 중 구간을 24:00에서 잘라 계산한다.

## 6. 기록 초기화와 내보내기

- **기록 초기화 (결정 8):** `sessions`, `session_events`, `seating_segments`, `posture_hour`, `hour_metrics`, `daily_summary`, `raw_chunks`, `day_jobs` 삭제.
  `settings`, `baselines`, `sensor_layouts`, `meta`, `schema_migrations`는 유지. 앱의 확인 절차를 거친 요청만 받는다.
- **CSV 내보내기 (결정 12):** 요약 테이블 전부 (`daily_summary`, `posture_hour`, `hour_metrics`, `seating_segments`, `sessions`) + 옵션으로 남은 원본.

## 7. 화면별 값 계산 확인

| 화면 | 값 | 계산 출처 |
|---|---|---|
| 홈 | 하루 평균 자세 (오늘 비정상 누적 최대), 정상 비율, 총 착석, 정상 시간 | `posture_hour` 오늘 + 우선순위 |
| 홈 | 기준 거리 | `baselines` 현재 값 |
| 홈 | 내 목표 오늘·어제·증감 | `settings.goals` + `posture_hour`·`daily_summary` |
| 홈 | 측정 바 | `sessions`·`session_events` + 실시간 |
| 일간 | 총 착석, 정상 비율, 자세별 누적(겹치지 않음), 가장 많이 나타난 습관 | `daily_summary` (오늘은 실시간 계산) |
| 일간 | 최대 연속 착석 (자정에서 자름), 무너짐 평균 | `seating_segments` |
| 일간 | 좌우 편향 비교 | `raw_lean_left/right_sec` |
| 일간 | 거리 평균, 기준보다 가까웠던 시간 | `hour_metrics` |
| 주간 | 주간 정상 비율 (합 ÷ 합), 지난주 대비 %p, 요일별 착석 | `daily_summary` 두 주 |
| 주간 | 지난주 비교 5개 (기록 있는 날만 평균) | `daily_summary` (기울기 방향은 `raw_tilt_*` — 9장의 가정, 팀 확인 필요) |
| 주간 | 이번 주 패턴 (시간대) | `posture_hour` |
| 피드백 | 목표 카드 (오늘·어제·지난주 평균·7일 막대·추세·주된 방향) | `daily_summary` 겹치는/안 겹치는 값 + 방향별 값 |
| 설정 | 측정·알림 설정, CSV 내보내기, 기록 초기화 | `settings`, 위 테이블 |

실시간 값(지금 자세, 압력 비율, 알림 배너, 센서 상태, 일어나기 알림)은 DB가 아니라 서버 메모리에서 준다.

## 8. 예상 용량 (어림)

| 항목 | 크기 |
|---|---|
| 원본, 좌표 없음 (기본) | 샘플당 약 24B(8채널) → 분당 약 14KB, 압축 후 약 8KB → **8시간 약 4MB**, 최대 보관(24.5시간) 약 12MB |
| 원본, 좌표 저장 켬 | 분당 약 36KB (압축 후) → 8시간 약 17MB, 최대 약 53MB |
| `posture_hour` | 하루 보통 100줄 안팎 (최악 720줄) → 1년 2~13MB |
| `seating_segments` | 하루 20~50개 → 1년 약 1.5MB |
| `hour_metrics`·`daily_summary`·세션·기준 자세 | 1년 1MB 미만 |
| WAL | 최대 64MB (`journal_size_limit`) |

원본은 하루치만 남고 지운 공간을 다시 쓰므로 DB 크기는 **1년에 약 5~15MB + 원본 하루치**에서 멈춘다.
SD카드 쓰기는 1분에 한 번 (하루 약 1,440번).

## 9. 앞으로 정할 것

- **기울어진 자세 방향 지표 (주간 비교) — ⚠️ 팀 확인 필요.** 지금 가정:
  1. 이번 주 `raw_tilt_left_sec` 합과 `raw_tilt_right_sec` 합 중 **큰 쪽이 이번 주 주된 방향**
  2. 그 방향의 **하루 평균 시간**(기록 있는 날만)을 **지난주 같은 방향**의 하루 평균과 비교
  3. 두 합이 같거나 둘 다 0이면 "방향 없음"
- 설정 변경 API → **⑤로 결정** (②는 테이블·기본값만)
