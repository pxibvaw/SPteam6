# SPteam6

**SitSense** — 카메라·압력센서 융합 기반 비착용형 자세 습관 분석 및 맞춤 피드백 시스템
(2026 가을 센서 프로그래밍 캡스톤디자인)

모니터에 단 카메라·거리센서와 의자 방석의 FSR 압력센서로 앉은 자세를 판단하고,
Raspberry Pi 서버를 거쳐 안드로이드 앱에서 자세 리포트를 보여준다.

```
센서 → 숫자 → 판단 → 기록 → 앱 화면
[하드웨어]    [AI]   [하드웨어]  [앱]
```

## 폴더 구조

```
SPteam6/
├── app/                    # [앱] Android Studio 프로젝트 (Kotlin)
│
├── hardware/               # [하드웨어]
│   ├── sensors/            #   실제 센서 읽기 코드 (Picamera2, MCP3008+FSR, 거리센서)
│   ├── server/             #   FastAPI + SQLite 서버 (앱에 데이터 전달)
│   └── docs/               #   배선도, FSR 방석 배치도, 거치대 설계
│
├── ai/                     # [AI]
│   ├── camera/             #   MediaPipe로 목 전방·어깨 기울기 판단
│   ├── features/           #   압력·거리 값에서 특징(feature) 추출
│   ├── classify/           #   RF/SVM/KNN 학습·평가 (사람 단위 평가)
│   ├── engine/             #   카메라+압력+거리 결과 합치기, 시간 필터(3초/30초)
│   ├── models/             #   학습된 모델 파일 (최종 모델만 git에 올림)
│   └── notebooks/          #   실험·분석용 노트북
│
├── common/                 # [하드웨어+AI 공동] 수정 시 서로 PR 확인 후 합치기
│   ├── sensors_base.py     #   센서 인터페이스 + 센서 값 형식 (압력 8칸/거리 mm/카메라 점 + 시간)
│   ├── schema.py           #   판단 결과 형식 + 자세 라벨 목록 (normal, cross_left …)
│   ├── mock_sensors.py     #   가짜 센서 (하드웨어 없이 테스트용)
│   ├── recorder.py         #   센서 값을 CSV로 저장 (수집 파일 형식 통일)
│   ├── replay.py           #   저장한 CSV를 센서처럼 재생 (맥북에서 반복 테스트)
│   └── config.py           #   config.yaml 읽기
│
├── data/                   # [공동] 수집 데이터 (git 제외, 원본은 구글 드라이브 공유)
│   ├── raw/                #   수집 원본 CSV, 수정 금지 (예: p1_cross_left_20261015_1430.csv)
│   ├── synthetic/          #   합성 데이터 (하드웨어 오기 전 테스트용)
│   ├── processed/          #   특징 추출 결과 (코드로 다시 만들 수 있음)
│   └── sample/             #   동작 확인용 작은 CSV (이것만 git에 올림)
│
├── docs/                   # [공동] API 명세, 회의록
├── config.yaml             # 센서 주기, 채널 번호, threshold 등 설정값
├── main.py                 # Pi에서 전체 시스템 실행 (센서 → 판단 → 저장)
├── requirements.txt        # 공통 라이브러리 (맥·Pi 모두)
├── requirements-pi.txt     # Pi 전용 라이브러리 (Picamera2, spidev)
├── .gitignore              # git에 안 올릴 파일 목록
└── README.md               # 프로젝트 소개 + 환경 세팅 방법
```

## 브랜치 규칙

```
main          ← 항상 돌아가는 버전. PR로만 합치기
├─ ai         ← AI 담당
├─ hardware   ← 하드웨어 담당
└─ app        ← 앱 담당
```

1. 기능 하나 완성되면 바로 PR로 `main`에 합치기 (최소 주 1회)
2. 작업 시작 전 `git pull origin main`으로 최신 내용 받기
3. `common/` 수정은 상대방이 PR 확인 후 합치기
4. 발표 전에는 태그 붙이기 (예: `v0.1-midterm`)

처음 한 번만 자기 브랜치 만들기:

```bash
git checkout main && git pull
git checkout -b hardware        # 또는 ai, app
git push -u origin hardware
```

## 환경 세팅

Python은 **3.11**로 통일한다 (Raspberry Pi OS Bookworm 64-bit 기본 버전).

### 맥 / 윈도우 (개발용)

```bash
# 맥: brew install python@3.11
python3.11 -m venv .venv
source .venv/bin/activate          # 윈도우: .venv\Scripts\activate
pip install -r requirements.txt
```

### Raspberry Pi 4 (64-bit Bookworm)

```bash
sudo apt update
sudo apt install -y python3-picamera2        # Picamera2는 apt로 설치
python3 -m venv --system-site-packages .venv # 시스템 패키지(picamera2, gpiozero) 사용
source .venv/bin/activate
pip install -r requirements-pi.txt
```

`sudo raspi-config` → Interface Options에서 **SPI**(MCP3008용)와 **I2C**(VL53L1X용)를 켠다.

### 설치 확인

저장소 최상위에서 실행:

```bash
python main.py --mode mock --seconds 3     # 가짜 센서 값이 출력되면 성공
```

## 하드웨어 구성

| 센서 | 연결 | 설치 위치 |
|---|---|---|
| FSR 406 × 8 (+ 여분 1~2) | MCP3008 1개 → SPI (CE0) | 방석, 센서 위에 단단한 원판 |
| VL53L1X (적외선 ToF 거리) | I2C | 모니터 위, 카메라와 함께 |
| 카메라 (정면, 1280×720) | CSI (Picamera2) | 모니터 위 |

## 데이터 수집

```bash
python main.py --record --subject p1 --seat cross_left --head normal --tilt none --seconds 40
```

- 한 번 실행 = 세션 1개 → `data/raw/p1_날짜_시간.csv` + 같은 이름의 `.json`(세션 정보)
- **처음 10초는 바른 자세로 앉기** (`phase=baseline`, 기준 측정). 그다음 지정한 자세 유지
- 라벨은 3개를 따로 정한다 (값은 `common/schema.py`에 있는 것만)
  - `--seat`: `normal`, `lean_left`, `lean_right`, `cross_left`, `cross_right`, `empty`
  - `--head`: `normal`, `forward`(거북목)
  - `--tilt`: `none`, `left`, `right` (사람 기준 방향)
- 저장한 세션 재생: `python main.py --mode replay --file data/raw/파일이름.csv`
- 필드 설명은 [`docs/data_format.md`](docs/data_format.md)

## 데이터 규칙

- `data/raw/` 파일은 수정하지 않는다. 원본은 팀 구글 드라이브에 올려서 공유
- `subject`는 `p1`, `p2`처럼 번호로. 실명 쓰지 않기
- 카메라 이미지는 저장하지 않고, MediaPipe 좌표만 저장
- 카메라나 모니터 위치가 바뀌면 새 세션으로 시작 (기준 자세를 다시 재야 함)
- 최종 모델만 `ai/models/final_*` 이름으로 git에 올린다
