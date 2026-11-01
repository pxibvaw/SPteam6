# Pi 카메라로 좌우 기울기 데이터 수집하기

카메라 모듈만 연결된 Pi에서 **상체 좌우 기울기 3가지**(바른 자세 / 왼쪽 / 오른쪽)를 수집한다.
압력·거리센서는 없어도 된다. 코드: `ai/camera/collect.py` (+ `ai/camera/picam.py`)

## 0. 무엇을 넘기나

파일을 따로 보내지 않고 **GitHub `ai` 브랜치**로 넘긴다. Pi에서 필요한 파일은 이것뿐이다.

| 파일 | 역할 |
|---|---|
| `ai/camera/collect.py` | 수집 스크립트 (새로 추가) |
| `ai/camera/picam.py` | Pi 카메라 모듈 읽기 (새로 추가) |
| `ai/camera/pose.py` | MediaPipe로 상체 점 7개 추출 |
| `ai/features/pose_features.py` | 기울기 계산 (녹화 중 안내용) |
| `common/` | 데이터 형식, 설정 읽기 |
| `config.yaml`, `requirements.txt` | 설정, 설치 목록 |

- AI 담당: 위 파일을 `ai` 브랜치에 커밋·push 한다.
- Pi 담당: Pi에서 `ai` 브랜치를 받아 **실행만** 한다. Pi에서 코드를 고치지 않는다 (고칠 게 있으면 AI 담당에게).

## 1. Pi 준비 (처음 한 번)

SSH로 접속한 터미널에서 한 줄씩 실행한다.

```bash
# (1) OS 확인 — 64-bit Bookworm + Python 3.11이어야 MediaPipe가 설치됨
uname -m                         # aarch64 가 나와야 함 (armv7l이면 32-bit라 안 됨)
grep VERSION_CODENAME /etc/os-release   # bookworm
python3 --version                # Python 3.11.x

# (2) 카메라 확인
rpicam-hello --list-cameras      # 카메라 1개가 보여야 함 (예전 OS면 libcamera-hello --list-cameras)

# (3) 코드 받기
git clone https://github.com/pxibvaw/SPteam6.git     # 이미 있으면 생략
cd SPteam6
git fetch origin
git checkout ai
git pull

# (4) 설치
sudo apt install -y python3-picamera2                 # pip가 아니라 apt로
python3 -m venv --system-site-packages .venv           # picamera2가 보이게 하는 옵션 필수
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt                       # MediaPipe 때문에 몇 분 걸림

# (5) 설치 확인
python -c "import mediapipe, cv2, picamera2; print(mediapipe.__version__, cv2.__version__)"
#  → 0.10.18 4.10.0
```

- (1)에서 `trixie`나 Python 3.13이 나오면 MediaPipe 0.10.18이 설치되지 않는다. AI 담당에게 알려 주기.
- 터미널을 새로 열 때마다 `cd SPteam6 && source .venv/bin/activate`를 다시 한다. 줄 앞에 `(.venv)`가 붙으면 켜진 것.
- 코드가 바뀌었다고 하면 `git pull`만 다시 하면 된다.

## 2. 카메라 설치와 확인

카메라는 **최종 설치 위치와 같게** 모니터 위 가운데, 정면을 보게 둔다.

```bash
python -m ai.camera.collect --check              # 10초 동안 인식률·속도 확인 (저장 안 함)
python -m ai.camera.collect --check --snapshot   # + 구도 확인용 사진 1장 (data/raw/camera/check_*.jpg)
```

- 결과에서 **판단 가능 90% 이상**, **2 fps 이상**이면 OK.
- 사진은 MobaXterm 왼쪽 파일 창(SFTP)에서 내려받아 본다. 얼굴·두 어깨가 화면 안에 다 들어와야 한다.
  확인 후 사진은 지운다 (얼굴 사진이라 공유하지 않기).
- 카메라가 거꾸로 달렸으면 모든 명령에 `--rotate180`을 붙인다.
- 2 fps보다 느리면 `--model 0`을 붙인다 (가벼운 모델, 처음 실행 때 인터넷 필요).

## 3. 수집

한 번 실행 = 한 자세. 사람 ID는 `p1`, `p2` … (실명 금지).

```bash
python -m ai.camera.collect --subject p1 --tilt none    # 바른 자세
python -m ai.camera.collect --subject p1 --tilt left    # 사람 기준 왼쪽
python -m ai.camera.collect --subject p1 --tilt right   # 사람 기준 오른쪽
```

실행하면 화면 안내대로 진행된다 (한 번에 약 50초).

| 단계 | 시간 | 하는 것 | 저장 |
|---|---|---|---|
| 준비 | 5초 | 바르게 앉아 정면 보기 | ✗ |
| 기준 자세 | 10초 | 바른 자세 그대로 | ✓ `baseline`, 라벨 `none` |
| 자세 바꾸기 | 3초 | 안내된 방향으로 기울이기 (left/right만) | ✗ |
| 녹화 | 30초 | 그 자세 유지 | ✓ `record`, 라벨 = `--tilt` |

**자세 규칙**
- 방향은 **앉은 사람 기준**. `left` = 내 왼쪽으로 기울임 (카메라 화면에서는 오른쪽).
- 엉덩이는 의자에 그대로 두고 **상체만** 기울인다. 목은 바르게 (거북목 X).
- 녹화 중 줄마다 `어깨 +7.5°` 같은 값이 나온다. **왼쪽이면 +, 오른쪽이면 −** 가 정상.
  `⚠ 반대 방향 같아요`가 나오면 방향을 다시 확인한다.

**권장 순서 (한 사람당)** — none → left → right를 **3세트** (약 8분)

| 세트 | 기울기 정도 | `--notes` |
|---|---|---|
| 1 | 살짝 (평소에 무심코 기대는 정도) | `--notes small` |
| 2 | 보통 | `--notes medium` |
| 3 | 확실하게 | `--notes large` |

기울기 정도를 다르게 모아야 나중에 기준값(몇 도부터 기울어진 자세인지)을 데이터로 정할 수 있다.
사람은 **5명 이상**이 목표 (새 사람으로 평가하려면 학습에 4명 이상 필요).

## 4. 데이터 넘기기

저장 위치: `data/raw/camera/` — 세션마다 `.csv`(좌표) + `.json`(세션 정보) 2개.

- `data/raw/`는 git에 올라가지 않는다. **커밋하지 말 것.**
- MobaXterm 왼쪽 파일 창에서 `SPteam6/data/raw/camera` 폴더를 통째로 내려받아 AI 담당에게 보낸다.
- 끝에 `⚠ 다시 수집을 권장해요`가 나온 파일도 **지우지 말고** 같이 보낸다 (학습 때 걸러냄).

## 5. 문제 해결

| 메시지 | 해결 |
|---|---|
| `picamera2를 찾을 수 없음` | `sudo apt install -y python3-picamera2`, 가상환경을 `--system-site-packages`로 다시 만들기 (`rm -rf .venv` 후 1-(4)) |
| `Pi가 카메라를 찾지 못함` | 전원 끄고 리본 케이블 방향·꽂힘 확인 → 재부팅 → `rpicam-hello --list-cameras` |
| `Pi 카메라를 시작하지 못함` | 다른 프로그램(rpicam-hello 등)이 카메라를 쓰고 있음 → 종료 후 다시 |
| `MediaPipe를 불러오지 못함` | `source .venv/bin/activate` 했는지, `pip install -r requirements.txt` 했는지 |
| `⚠ 사람이 잘 안 보여요` | 조명을 밝게, 카메라 높이·각도 조정, 어깨까지 화면에 들어오게 |
| 너무 느림 (2 fps 미만) | `--model 0` |
| 중간에 멈추고 싶음 | `Ctrl+C` — 그때까지는 저장되고 세션 상태가 `interrupted`로 남음 |

## 데이터 형식 (AI 담당 참고)

CSV 36칸: `ts, session_id, subject, phase, head_label, tilt_label, frame, pose_detected`
\+ 점 7개 × `x, y, z, vis` (열 이름·좌표 형식은 전체 수집 CSV의 카메라 열과 같음).
세션 JSON의 `extra`에 `tilt`, `camera_source`, `model_complexity`, 인식률, 평균 fps,
녹화 중 어깨 기울기 변화 중앙값이 들어간다. 자세 바꾸는 3초는 저장하지 않는다.
