"""FSR + HC-SR04 동시 데이터 수집 (Raspberry Pi에서 실행)

사용법:
    python3 collect_all.py            # 자세당 180초, 쉬는 시간 10초
    python3 collect_all.py 10 3       # 시험용: 자세당 10초, 쉬는 시간 3초

필요: spidev, gpiozero (Raspberry Pi OS에 보통 설치돼 있음. 없으면 sudo apt install python3-spidev python3-gpiozero)
      SPI 활성화(raspi-config), HC-SR04 Echo는 분압 회로로 3.3V로 낮춰서 연결.

배치 (앞 -> 뒤):  화면에 이 모양으로 FSR 값이 표시됩니다 (CH2도 표시되지만 CSV에는 저장 안 함)
    CH4   CH2  CH1        (맨 앞)
    CH6        CH3        (중간)
    CH7   CH5  CH0        (맨 뒤)

구조와 동기화:
    메인(시계)이 0.5초마다 "틱"을 만들어 두 스레드에 똑같이 보냅니다.
    각 틱에는 같은 timestamp / 자세 / 샘플 번호가 들어 있어서, 두 스레드가 같은 순간에 각자 센서를 읽고
    자기 CSV에 한 줄씩 저장합니다. 그래서 두 CSV는 줄 수와 timestamp가 정확히 같습니다.
    FSR 스레드      -> posture_data_<실행시각>.csv
    HC-SR04 스레드  -> distance_data_<실행시각>.csv
    자세마다 [쉬는 시간 10초 -> 수집 180초]를 반복합니다. 쉬는 시간에는 둘 다 저장하지 않습니다.
    (HC-SR04는 쉬는 시간에도 gpiozero가 내부적으로 거리를 계속 재지만, 파일에는 쓰지 않습니다.)

CSV 열:
    posture_data_*.csv : timestamp, label, sample, elapsed_s,
                         CH0(맨 뒤 오른쪽), CH1(맨 앞 오른쪽), CH3(중간 오른쪽), CH4(맨 앞 왼쪽),
                         CH5(맨 뒤 가운데), CH6(중간 왼쪽), CH7(맨 뒤 왼쪽)      (CH2는 저장 안 함)
    distance_data_*.csv: timestamp, sample, distance_cm
        sample = 그 자세 안에서 몇 번째 측정인지 (0부터 시작, 자세가 바뀌면 다시 0). 전체 행 번호가 아닙니다.
        distance_cm이 비어 있으면 측정 범위(MAX_DIST_M) 밖이거나 반사가 없어 측정 안 된 것입니다.

두 CSV 합치기 (pandas). timestamp가 정확히 같으므로 그대로 붙습니다:
    fsr  = pd.read_csv("posture_data_X.csv",  encoding="utf-8-sig")
    dist = pd.read_csv("distance_data_X.csv", encoding="utf-8-sig")
    m = pd.merge(fsr, dist, on=["timestamp", "sample"])

timestamp는 Pi의 시계를 따릅니다. 실행 전에 Pi의 날짜·시간이 맞는지 확인하세요 (date 명령).
Ctrl+C로 끝내도 그때까지 저장된 데이터는 파일에 남아 있습니다 (한 줄씩 바로 저장).
"""
import csv
import queue
import sys
import threading
import time
import warnings
from datetime import datetime

import spidev
from gpiozero import DistanceSensor, DistanceSensorNoEcho

# ---------------- 설정 ----------------
DURATION_SEC = 180    # 자세당 수집 시간(초)
REST_SEC = 10         # 자세가 바뀔 때 쉬는 시간(초)
INTERVAL = 0.5        # FSR, HC-SR04 공통 수집 간격(초)
SPEED_HZ = 500_000    # SPI 속도 (3.3V에서 안전한 속도)

# HC-SR04 핀 (BCM 번호). 실제로 연결한 핀에 맞게 고치세요.
TRIG_PIN = 23         # 물리 핀 16
ECHO_PIN = 24         # 물리 핀 18 (분압 회로를 거쳐서 연결)
MAX_DIST_M = 2.0      # 이 거리(m) 이상이면 "측정 안 됨"(빈 칸)
QUEUE_LEN = 5         # gpiozero가 최근 N번 측정값의 중앙값을 냄 (노이즈 줄이기)

if len(sys.argv) >= 2:
    DURATION_SEC = float(sys.argv[1])
if len(sys.argv) >= 3:
    REST_SEC = float(sys.argv[2])

LABELS = [
    "바른자세(앞)",
    "바른자세(중)",
    "바른자세(뒤)",
    "왼쪽편향(앞)",
    "왼쪽편향(뒤)",
    "오른쪽편향(앞)",
    "오른쪽편향(뒤)",
    "왼다리꼬기(앞)",
    "왼다리꼬기(중)",
    "왼다리꼬기(뒤)",
    "오른다리꼬기(앞)",
    "오른다리꼬기(중)",
    "오른다리꼬기(뒤)",
]

# 읽을 FSR 채널과 위치 이름
POSITIONS = {
    4: "맨 앞 왼쪽",
    2: "맨 앞 가운데",
    1: "맨 앞 오른쪽",
    6: "중간 왼쪽",
    3: "중간 오른쪽",
    7: "맨 뒤 왼쪽",
    5: "맨 뒤 가운데",
    0: "맨 뒤 오른쪽",
}
CHANNELS = list(POSITIONS)                                   # 화면에 표시할 채널 (8개)
CSV_CHANNELS = sorted(ch for ch in CHANNELS if ch != 2)      # CSV에 저장할 채널 (CH2 제외)

warnings.simplefilter("ignore", DistanceSensorNoEcho)        # 반사 없음 경고가 화면을 어지럽히지 않게


class Shared:
    """메인과 스레드들이 같이 쓰는 상태."""

    def __init__(self):
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.recording = False
        self.fsr = None         # 가장 최근 FSR 값 (채널 -> 값)
        self.dist_cm = None     # 가장 최근 거리(cm), 없으면 None
        self.errors = []


class Tick:
    """0.5초마다 두 스레드에 똑같이 전달되는 한 번의 측정 지시."""

    def __init__(self, ts, label, recording, sample, elapsed):
        self.ts = ts                # 두 CSV에 똑같이 들어가는 timestamp
        self.label = label
        self.recording = recording  # False면 쉬는 시간 (저장 안 함)
        self.sample = sample        # 이 구간 안에서 몇 번째인지 (0부터)
        self.elapsed = elapsed      # 구간 시작 후 경과 시간(초)


def read_adc(spi, ch):
    """MCP3208 한 채널을 읽어 0~4095를 돌려준다."""
    r = spi.xfer2([0x06 | (ch >> 2), (ch & 3) << 6, 0])
    return ((r[1] & 0x0F) << 8) | r[2]


def now_iso():
    return datetime.now().isoformat(timespec="milliseconds")


def fsr_worker(sh, q, spi, path, counts):
    """틱이 올 때마다 FSR를 읽고, 수집 중이면 CSV에 한 줄 저장한다."""
    try:
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "label", "sample", "elapsed_s"]
                       + [f"CH{ch}({POSITIONS[ch]})" for ch in CSV_CHANNELS])
            f.flush()
            while True:
                tick = q.get()
                if tick is None:
                    break
                vals = {ch: read_adc(spi, ch) for ch in CHANNELS}
                with sh.lock:
                    sh.fsr = vals
                if tick.recording:
                    w.writerow([tick.ts, tick.label, tick.sample, tick.elapsed]
                               + [vals[ch] for ch in CSV_CHANNELS])
                    f.flush()
                    counts[tick.label] = counts.get(tick.label, 0) + 1
    except Exception as e:
        sh.errors.append(f"FSR 스레드 오류: {e!r}")
        sh.stop.set()


def dist_worker(sh, q, sensor, path, counts):
    """틱이 올 때마다(수집 중일 때만) HC-SR04 거리를 읽고 CSV에 한 줄 저장한다."""
    try:
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "sample", "distance_cm"])
            f.flush()
            while True:
                tick = q.get()
                if tick is None:
                    break
                if not tick.recording:
                    with sh.lock:
                        sh.dist_cm = None
                    continue
                d = sensor.distance                  # 미터 단위
                cm = None if d >= MAX_DIST_M - 1e-6 else round(d * 100, 1)
                with sh.lock:
                    sh.dist_cm = cm
                w.writerow([tick.ts, tick.sample, cm])
                f.flush()
                counts[tick.label] = counts.get(tick.label, 0) + 1
    except Exception as e:
        sh.errors.append(f"HC-SR04 스레드 오류: {e!r}")
        sh.stop.set()


drawn = False


def show(sh, status):
    """상태 + FSR 배치 모양 3줄 + 거리 한 줄을 제자리에서 갱신한다."""
    global drawn
    with sh.lock:
        z = sh.fsr
        cm = sh.dist_cm
        rec = sh.recording
    if z is None:
        z = {ch: 0 for ch in CHANNELS}
    if not rec:
        dist_line = "거리: (쉬는 중, 측정 안 함)"
    elif cm is None:
        dist_line = "거리: 측정 안 됨 (범위 밖/반사 없음)"
    else:
        dist_line = f"거리: {cm:6.1f} cm"
    rows = [
        status,
        f"{z[4]:4d}  {z[2]:4d}  {z[1]:4d}",
        f"{z[6]:4d}        {z[3]:4d}",
        f"{z[7]:4d}  {z[5]:4d}  {z[0]:4d}",
        dist_line,
    ]
    if drawn:
        print("\033[5A", end="")
    for r in rows:
        print("\033[K" + r)
    drawn = True


def schedule():
    """(라벨, 수집중 여부, 샘플 번호, 남은 시간, 몇 번째 자세) 를 0.5초 단위로 차례로 내보낸다."""
    for k, label in enumerate(LABELS, 1):
        for recording, seconds in ((False, REST_SEC), (True, DURATION_SEC)):
            n = int(round(seconds / INTERVAL))
            for i in range(n):
                yield label, recording, i, seconds - i * INTERVAL, k


def main():
    sh = Shared()
    spi = spidev.SpiDev()
    spi.open(0, 0)       # SPI0, CE0 (Pi 24번 핀 = CS)
    spi.max_speed_hz = SPEED_HZ
    spi.mode = 0
    sensor = DistanceSensor(echo=ECHO_PIN, trigger=TRIG_PIN, max_distance=MAX_DIST_M, queue_len=QUEUE_LEN)

    threads = []
    queues = []
    fsr_counts, dist_counts = {}, {}
    fsr_path = dist_path = None
    try:
        print(f"자세 {len(LABELS)}개 x {DURATION_SEC:g}초, 자세 사이 쉬는 시간 {REST_SEC:g}초")
        print(f"FSR, HC-SR04 모두 {INTERVAL}초 간격으로 같은 순간에 측정 (수집 중일 때만 저장)")
        print("자세 순서:", " -> ".join(LABELS))
        input("준비되면 Enter를 누르세요 (첫 자세 전에도 쉬는 시간이 있습니다)... ")
        print()

        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        fsr_path = f"posture_data_{run_id}.csv"
        dist_path = f"distance_data_{run_id}.csv"
        q_fsr, q_dist = queue.Queue(), queue.Queue()
        queues = [q_fsr, q_dist]
        threads = [
            threading.Thread(target=fsr_worker, args=(sh, q_fsr, spi, fsr_path, fsr_counts), name="fsr"),
            threading.Thread(target=dist_worker, args=(sh, q_dist, sensor, dist_path, dist_counts), name="dist"),
        ]
        for t in threads:
            t.start()

        total_labels = len(LABELS)
        next_t = time.monotonic()
        completed = True
        for label, recording, i, left, k in schedule():
            wait = next_t - time.monotonic()
            if wait > 0:
                sh.stop.wait(wait)
            if sh.stop.is_set():
                completed = False
                break
            with sh.lock:
                sh.recording = recording
            tick = Tick(now_iso(), label, recording, i, round(i * INTERVAL, 2))
            q_fsr.put(tick)
            q_dist.put(tick)
            next_t += INTERVAL
            sh.stop.wait(0.2)            # 두 스레드가 읽을 시간을 주고 화면 갱신
            if recording:
                status = f"[{k}/{total_labels}] 수집 중 - {label} (남은 시간 {left:5.1f}초)"
            else:
                status = f"[{k}/{total_labels}] 쉬는 중 - 다음 자세: {label} ({left:4.1f}초 후 시작, 저장 안 함)"
            show(sh, status)
        if completed:
            print("\n\n모든 자세 수집 완료")

    except KeyboardInterrupt:
        print("\n\n중단됨 (지금까지 저장된 데이터는 파일에 남아 있습니다)")

    finally:
        for q in queues:
            q.put(None)                  # 스레드에게 끝내라고 알림 (남은 틱을 처리한 뒤 종료)
        for t in threads:
            t.join(timeout=10)
        spi.close()
        sensor.close()
        for e in sh.errors:
            print(e)
        if fsr_path:
            print(f"\n저장 파일:\n  {fsr_path}  (FSR)\n  {dist_path}  (거리)")
            print("자세별 저장 개수 (FSR / 거리):")
            for label in LABELS:
                if label in fsr_counts or label in dist_counts:
                    print(f"  {label}: {fsr_counts.get(label, 0)} / {dist_counts.get(label, 0)}")
        else:
            print("수집을 시작하지 않았습니다")


if __name__ == "__main__":
    main()
