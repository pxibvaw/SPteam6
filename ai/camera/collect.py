"""카메라만으로 상체 좌우 기울기 데이터 수집 — 바른 자세(none) / 왼쪽(left) / 오른쪽(right)

압력·거리센서 없이 카메라만 연결된 상태에서 쓴다. 한 번 실행 = 한 자세 = 세션 1개.
SSH 터미널에서 쓰도록 영상 창 없이 글자로만 안내한다.

흐름 (한 번 실행)
    준비 카운트다운 (바르게 앉기, 사람 인식 확인)        저장 안 함
    → 기준 자세 baseline 10초 (바른 자세)               phase=baseline, tilt_label=none
    → 자세 바꾸기 3초 (left/right일 때만)               저장 안 함 (라벨이 애매한 순간)
    → 녹화 record 30초 (지정한 자세 유지)               phase=record,  tilt_label=지정한 값

사용 예 (저장소 최상위, .venv 켠 상태)
    python -m ai.camera.collect --check                        # 카메라·인식·속도 확인만 (저장 안 함)
    python -m ai.camera.collect --check --snapshot             # + 구도 확인용 사진 1장 (점 표시)
    python -m ai.camera.collect --subject p1 --tilt none       # 바른 자세
    python -m ai.camera.collect --subject p1 --tilt left       # 사람 기준 왼쪽으로 기울이기
    python -m ai.camera.collect --subject p1 --tilt right
    python -m ai.camera.collect --source webcam --subject p1 --tilt left   # 맥 웹캠으로
    python -m ai.camera.collect --source mock --subject test --tilt left --seconds 5 --baseline 3
                                                                # 카메라 없이 코드만 확인

저장 (data/raw/는 git에 올라가지 않음)
    data/raw/camera/{subject}_tilt-{tilt}_{YYYYMMDD_HHMMSS}.csv   한 줄 = 카메라 한 프레임
    data/raw/camera/{같은 이름}.json                              세션 정보 (common/recorder.py의 SessionInfo)

CSV 컬럼 (36칸)
    ts, session_id, subject, phase, head_label, tilt_label, frame, pose_detected,
    {부위}_x, {부위}_y, {부위}_z, {부위}_vis  (부위 7개: common.schema.LANDMARK_NAMES)
    - 좌표는 MediaPipe 원래 값(0~1 비율). 각도를 계산하려면 세션 정보의 해상도로 픽셀로 바꾼다.
    - 열 이름·좌표 형식은 전체 수집 CSV(common/recorder.py)의 카메라 열과 같다.
    - head_label은 항상 normal (이 수집에서는 목을 바르게 유지).
    - 이미지는 저장하지 않는다 (--snapshot으로 찍은 확인용 사진 1장만 예외).

방향은 사람 기준: --tilt left = 앉은 사람이 자기 왼쪽으로 기울임 (카메라 화면에서는 오른쪽).

종료 코드: 0 정상 / 1 설정·입력 오류 / 2 카메라 오류 / 3 저장 오류 / 130 Ctrl+C
"""
from __future__ import annotations

import argparse
import csv
import logging
import math
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from common.config import ConfigError, load_config, repo_path
from common.recorder import SUBJECT_RE, RecorderError, SessionInfo
from common.schema import (
    LANDMARK_NAMES, HeadState, LabelError, Phase, TiltState, TILT_LABELS,
)
from common.sensors_base import PoseReading, SensorError

log = logging.getLogger("sitsense.collect")

FORMAT = "camera_tilt_v1"            # 세션 정보 extra.format (형식이 바뀌면 올린다)
LANDMARK_FIELDS = ("x", "y", "z", "vis")
TILT_KO = {"none": "바른 자세", "left": "왼쪽으로 기울이기", "right": "오른쪽으로 기울이기"}

# 녹화 중 안내용 (판단 기준이 아니라 수집이 제대로 되는지 보는 값)
HINT_MIN_DEG = 2.0                   # 기울기 자세인데 어깨 변화가 이보다 작으면 '더 기울여 보세요'
HINT_NONE_MAX_DEG = 5.0              # 바른 자세인데 어깨 변화가 이보다 크면 '바르게 앉으세요'
LOW_DETECT_RATIO = 0.7               # 인식률이 이보다 낮으면 다시 수집 권장


def csv_columns() -> list[str]:
    cols = ["ts", "session_id", "subject", "phase", "head_label", "tilt_label",
            "frame", "pose_detected"]
    cols += [f"{name}_{f}" for name in LANDMARK_NAMES for f in LANDMARK_FIELDS]
    return cols


def _num(v: float, fmt: str) -> str:
    return format(v, fmt) if v is not None and math.isfinite(v) else ""


def pose_to_row(pose: PoseReading) -> list:
    row = []
    for name in LANDMARK_NAMES:
        lm = pose.points.get(name) if pose.detected else None
        if lm is None or not lm.is_finite():
            row += ["", "", "", ""]
        else:
            row += [_num(lm.x, ".5f"), _num(lm.y, ".5f"), _num(lm.z, ".5f"),
                    _num(lm.visibility, ".3f")]
    return row


# ---------------------------------------------------------------------------
# 카메라 → 상체 점
# ---------------------------------------------------------------------------
class PoseStream:
    """카메라 + MediaPipe를 하나로. read() → (PoseReading | None, 이미지 | None)"""

    width: int
    height: int

    def read(self):
        raise NotImplementedError

    def set_tilt(self, tilt: str) -> None:
        """mock에서만 의미 있음 (가짜 사람이 자세를 바꾼다)"""

    def close(self) -> None:
        pass


class CameraPoseStream(PoseStream):
    def __init__(self, camera, model_complexity: int):
        from ai.camera.pose import PoseExtractor
        self.camera = camera
        self.width, self.height = camera.width, camera.height
        try:
            self.extractor = PoseExtractor(model_complexity)
        except ImportError as e:
            camera.close()
            raise SensorError(f"MediaPipe를 불러오지 못함 — .venv를 켰는지, "
                              f"'pip install -r requirements.txt'를 했는지 확인 ({e})") from None
        except Exception:
            camera.close()
            raise

    def read(self):
        frame = self.camera.read()
        if frame is None:
            return None, None
        return self.extractor.extract(frame), frame.image

    def close(self) -> None:
        for obj in (self.extractor, self.camera):
            try:
                obj.close()
            except Exception as e:  # noqa: BLE001
                log.warning("%s 닫기 실패: %s", type(obj).__name__, e)


class MockPoseStream(PoseStream):
    """카메라 없이 코드 흐름만 확인 (가짜 좌표, 실제 데이터로 쓰면 안 됨)"""

    def __init__(self, width: int, height: int, fps: float):
        from common.mock_sensors import MockPoseSource
        self.src = MockPoseSource(width, height)
        self.width, self.height = width, height
        self.period = 1.0 / fps

    def set_tilt(self, tilt: str) -> None:
        self.src.tilt = tilt

    def read(self):
        time.sleep(self.period * 0.5)                  # 카메라처럼 약간 기다림
        return self.src.read(), None


def open_stream(args, cfg: dict) -> PoseStream:
    w, h = cfg["camera"]["width"], cfg["camera"]["height"]
    if args.source == "mock":
        return MockPoseStream(w, h, args.fps)
    if args.source == "picam":
        from ai.camera.picam import PiCamera
        cam = PiCamera(w, h, rotate180=args.rotate180)
    else:
        from ai.camera.pose import Webcam
        index = args.index if args.index is not None else cfg["camera"]["index"]
        cam = Webcam(index, w, h)
    return CameraPoseStream(cam, args.model)


# ---------------------------------------------------------------------------
# 진행 상황 (터미널 안내용 계산)
# ---------------------------------------------------------------------------
@dataclass
class PhaseStats:
    frames: int = 0
    detected: int = 0
    shoulder: list = field(default_factory=list)   # 기준 대비 어깨 각도 변화 (도)
    eye: list = field(default_factory=list)
    nose: list = field(default_factory=list)       # 기준 대비 코 좌우 이동 (어깨 너비 비율)
    ear_vis: list = field(default_factory=list)

    def add(self, pose: PoseReading | None, feat, base) -> None:
        self.frames += 1
        if pose is None or not pose.detected:
            return
        self.detected += 1
        for k in ("left_ear", "right_ear"):
            lm = pose.points.get(k)
            if lm is not None and math.isfinite(lm.visibility):
                self.ear_vis.append(lm.visibility)
        if feat is None:
            return
        if base is None:                            # 기준 측정 중: 원래 값을 모은다
            self.shoulder.append(feat.shoulder_angle)
            self.eye.append(feat.head_angle)
            self.nose.append(feat.head_offset)
        else:
            self.shoulder.append(feat.shoulder_angle - base["shoulder"])
            self.eye.append(feat.head_angle - base["eye"])
            self.nose.append(feat.head_offset - base["nose"])

    @property
    def detect_ratio(self) -> float:
        return self.detected / self.frames if self.frames else 0.0


def _med(values: list) -> float:
    return statistics.median(values) if values else math.nan


def _features(pose, w, h, min_vis):
    from ai.features.pose_features import upper_features
    return upper_features(pose, w, h, min_vis)


def hint(tilt: str, shoulder_delta: float) -> str:
    """녹화 중 방향·크기 안내. + 가 사람 왼쪽 (ai/features/pose_features.py 기준)"""
    if not math.isfinite(shoulder_delta):
        return ""
    if tilt == TiltState.NONE.value:
        return "  ⚠ 바르게 앉아 주세요" if abs(shoulder_delta) > HINT_NONE_MAX_DEG else ""
    sign = 1 if tilt == TiltState.LEFT.value else -1
    if sign * shoulder_delta < -HINT_MIN_DEG:
        return "  ⚠ 반대 방향 같아요 (사람 기준 방향 확인)"
    if abs(shoulder_delta) < HINT_MIN_DEG:
        return "  ⚠ 조금 더 기울여 보세요"
    return ""


# ---------------------------------------------------------------------------
# 저장
# ---------------------------------------------------------------------------
class CameraSessionWriter:
    def __init__(self, out_dir: Path, info: SessionInfo):
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise RecorderError(f"저장 폴더를 만들 수 없음: {out_dir} ({e})") from None
        base, k = info.session_id, 1
        while (out_dir / f"{info.session_id}.csv").exists() or (out_dir / f"{info.session_id}.json").exists():
            k += 1
            info.session_id = f"{base}_{k}"         # 기존 파일 덮어쓰기 방지
        self.info = info
        self.csv_path = out_dir / f"{info.session_id}.csv"
        self.json_path = out_dir / f"{info.session_id}.json"
        self.count = 0
        try:
            self._f = open(self.csv_path, "x", newline="", encoding="utf-8")
        except OSError as e:
            raise RecorderError(f"CSV 파일을 만들 수 없음: {self.csv_path} ({e})") from None
        self._w = csv.writer(self._f)
        self._w.writerow(csv_columns())
        self._f.flush()
        info.save(self.json_path)
        self._closed = False

    def write(self, ts: float, phase: str, tilt: str, frame_idx: int, pose: PoseReading) -> None:
        row = [f"{ts:.3f}", self.info.session_id, self.info.subject, phase,
               HeadState.NORMAL.value, tilt, frame_idx, int(pose.detected)]
        row += pose_to_row(pose)
        try:
            self._w.writerow(row)
            self._f.flush()                          # 매 줄 바로 저장 (중간에 꺼져도 남게)
        except OSError as e:
            raise RecorderError(f"CSV 저장 실패 (디스크 공간 확인): {e}") from None
        self.count += 1

    def close(self, status: str, extra: dict) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._f.flush()
            os.fsync(self._f.fileno())
        except OSError as e:
            log.warning("CSV 마무리 저장 실패: %s", e)
        finally:
            self._f.close()
        self.info.end_date = datetime.now().isoformat(timespec="seconds")
        self.info.rows = self.count
        self.info.status = status
        self.info.extra.update(extra)
        try:
            self.info.save(self.json_path)
        except OSError as e:
            log.warning("세션 정보 저장 실패: %s", e)


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------
def say(msg: str) -> None:
    print(msg, flush=True)


def countdown(stream: PoseStream, seconds: int, title: str, min_vis: float) -> None:
    """카운트다운하면서 사람이 화면에 잘 잡히는지 보여준다 (저장 안 함)"""
    from ai.features.pose_features import usable
    say(f"\n▶ {title}")
    t_end = time.time() + seconds
    last = None
    seen = ok = 0
    while True:
        left = t_end - time.time()
        if left <= 0:
            break
        pose, _ = stream.read()
        seen += 1
        ok += int(usable(pose, min_vis))
        sec = math.ceil(left)
        if sec != last:
            state = "인식 OK" if ok and ok >= seen * 0.5 else "⚠ 사람이 잘 안 보여요 (화면 안에 어깨까지)"
            say(f"  {sec}…  {state}")
            last, seen, ok = sec, 0, 0


def run_check(stream: PoseStream, seconds: float, snapshot: bool, out_dir: Path,
              min_vis: float) -> None:
    """저장 없이 인식률·속도만 확인. --snapshot이면 마지막 프레임에 점을 그려 사진 1장 저장"""
    from ai.features.pose_features import usable
    say(f"카메라 확인 {seconds:g}초 (저장 안 함). 평소처럼 바르게 앉아 주세요.")
    t0 = time.time()
    t_print = t0
    frames = detected = good = 0
    last_pose = last_img = None
    while time.time() - t0 < seconds:
        pose, img = stream.read()
        frames += 1
        detected += int(pose is not None and pose.detected)
        good += int(usable(pose, min_vis))
        if img is not None:
            last_pose, last_img = pose, img
        if time.time() - t_print >= 1.0:
            el = time.time() - t0
            say(f"  {el:4.1f}s  {frames / el:4.1f} fps  인식 {detected}/{frames}  판단 가능 {good}/{frames}")
            t_print = time.time()
    if frames == 0:
        raise SensorError("프레임을 하나도 못 읽음 — 카메라 연결 확인")
    say(f"결과: {frames / (time.time() - t0):.1f} fps, 사람 인식 {detected / frames:.0%}, "
        f"판단 가능(코·눈·어깨가 잘 보임) {good / frames:.0%}")
    if good / frames < LOW_DETECT_RATIO:
        say("⚠ 판단 가능 비율이 낮아요. 조명, 카메라 높이, 어깨까지 화면에 들어오는지 확인하세요.")

    if snapshot:
        if last_img is None:
            say("사진: mock에서는 찍을 이미지가 없음")
            return
        import cv2
        img = last_img.copy()
        h, w = img.shape[:2]
        if last_pose is not None and last_pose.detected:
            for name, lm in last_pose.points.items():
                if lm.is_finite():
                    c = (0, 200, 0) if lm.visibility >= min_vis else (0, 0, 255)
                    cv2.circle(img, (int(lm.x * w), int(lm.y * h)), 6, c, -1)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"check_{datetime.now():%Y%m%d_%H%M%S}.jpg"
        if not cv2.imwrite(str(path), img):
            raise RecorderError(f"사진 저장 실패: {path}")
        say(f"사진 저장: {path.relative_to(repo_path('.'))}  (초록 점 = 잘 보임, 빨강 = 안 보임)")
        say("  구도 확인용이에요. 확인 후 지워 주세요 (얼굴 사진이라 공유하지 않기).")


def run_collect(stream: PoseStream, args, cfg: dict, out_dir: Path) -> None:
    tilt = args.tilt
    w, h = stream.width, stream.height
    min_vis = cfg["upper_rules"]["min_visibility"]
    period = 1.0 / args.fps

    info = SessionInfo(
        session_id=f"{args.subject}_tilt-{tilt}_{datetime.now():%Y%m%d_%H%M%S}",
        subject=args.subject,
        date=datetime.now().isoformat(timespec="seconds"),
        camera_width=w, camera_height=h,
        fsr_channels_used=[],                      # 카메라만 수집
        baseline_seconds=args.baseline,
        notes=args.notes,
        extra={"format": FORMAT, "sensors": ["camera"], "label_field": "tilt_label",
               "tilt": tilt, "camera_source": args.source, "model_complexity": args.model,
               "rotate180": args.rotate180, "record_seconds": args.seconds,
               "transition_seconds": args.transition if tilt != "none" else 0,
               "max_fps": args.fps},
    )

    say("=" * 60)
    say(f"사람 {args.subject} · 자세: {TILT_KO[tilt]} ({tilt}) · 녹화 {args.seconds:g}초")
    say("방향은 앉은 사람 기준이에요. 목은 바르게, 엉덩이는 그대로 두고 상체만 기울여 주세요.")
    say("=" * 60)
    countdown(stream, args.countdown, "바르게 앉아 정면을 봐 주세요. 곧 기준 자세 측정을 시작해요.",
              min_vis)

    writer = CameraSessionWriter(out_dir, info)
    stats = {Phase.BASELINE.value: PhaseStats(), Phase.RECORD.value: PhaseStats()}
    base = None
    status = "error"
    frame_idx = 0
    t_start = time.time()
    try:
        # ── 기준 자세 ──
        stream.set_tilt("none")
        say(f"\n● 기준 자세 측정 {args.baseline:g}초 — 바른 자세 그대로")
        _loop(stream, writer, stats, Phase.BASELINE.value, "none", args.baseline, period,
              w, h, min_vis, None, tilt, frame_idx_start=frame_idx)
        frame_idx = writer.count
        b = stats[Phase.BASELINE.value]
        if b.shoulder:
            base = {"shoulder": _med(b.shoulder), "eye": _med(b.eye), "nose": _med(b.nose)}
        else:
            say("⚠ 기준 자세에서 판단 가능한 프레임이 없어요. 녹화는 계속하지만 안내 값은 표시되지 않아요.")

        # ── 자세 바꾸기 (저장 안 함) ──
        if tilt != "none" and args.transition > 0:
            say(f"\n▶ 지금 {TILT_KO[tilt]}! ({args.transition:g}초 뒤 녹화 시작)")
            stream.set_tilt(tilt)
            t_end = time.time() + args.transition
            while time.time() < t_end:
                stream.read()                          # 카메라 추적은 계속 (저장 안 함)
        stream.set_tilt(tilt)

        # ── 녹화 ──
        say(f"\n● 녹화 {args.seconds:g}초 — {TILT_KO[tilt]} 유지")
        _loop(stream, writer, stats, Phase.RECORD.value, tilt, args.seconds, period,
              w, h, min_vis, base, tilt, frame_idx_start=frame_idx)
        status = "completed"
    except KeyboardInterrupt:
        status = "interrupted"
        raise
    finally:
        r, b = stats[Phase.RECORD.value], stats[Phase.BASELINE.value]
        elapsed = max(time.time() - t_start, 1e-6)
        summary = {
            "rows_baseline": b.frames, "rows_record": r.frames,
            "detect_ratio_baseline": round(b.detect_ratio, 3),
            "detect_ratio_record": round(r.detect_ratio, 3),
            "fps_mean": round(writer.count / elapsed, 2),
            "record_median_shoulder_delta_deg": None if not r.shoulder else round(_med(r.shoulder), 2),
            "record_median_eye_delta_deg": None if not r.eye else round(_med(r.eye), 2),
            "ear_vis_mean": None if not (b.ear_vis + r.ear_vis) else
            round(statistics.fmean(b.ear_vis + r.ear_vis), 3),
        }
        writer.close(status, summary)
        _print_summary(writer, summary, tilt, status)


def _loop(stream, writer, stats, phase, label, seconds, period, w, h, min_vis, base, tilt,
          frame_idx_start: int) -> None:
    st = stats[phase]
    t0 = time.time()
    t_print = t0
    idx = frame_idx_start
    win_shoulder: list[float] = []
    while True:
        t_frame = time.time()
        el = t_frame - t0
        if el >= seconds:
            break
        pose, _ = stream.read()
        ts = time.time()
        if pose is None:                               # 프레임 자체를 못 읽음
            pose = PoseReading.empty(ts)
        feat = _features(pose, w, h, min_vis)
        st.add(pose, feat, base)
        writer.write(ts, phase, label, idx, pose)
        idx += 1
        if base is not None and feat is not None:
            win_shoulder.append(feat.shoulder_angle - base["shoulder"])
        if ts - t_print >= 1.0:
            line = f"  {min(el, seconds):4.0f}/{seconds:g}s  인식 {st.detected}/{st.frames}"
            if base is not None and win_shoulder:
                d = _med(win_shoulder)
                line += f"  어깨 {d:+5.1f}°" + hint(tilt, d)
            elif feat is None:
                line += "  ⚠ 판단 불가 프레임 (얼굴·어깨가 잘 보이게)"
            say(line)
            t_print, win_shoulder = ts, []
        rest = period - (time.time() - t_frame)
        if rest > 0:
            time.sleep(rest)


def _print_summary(writer: CameraSessionWriter, s: dict, tilt: str, status: str) -> None:
    say("\n" + "-" * 60)
    say(f"저장: {writer.csv_path.relative_to(repo_path('.'))}  (+ .json, 상태={status})")
    say(f"  기준 {s['rows_baseline']}줄 (인식 {s['detect_ratio_baseline']:.0%}) · "
        f"녹화 {s['rows_record']}줄 (인식 {s['detect_ratio_record']:.0%}) · 평균 {s['fps_mean']} fps")
    d = s["record_median_shoulder_delta_deg"]
    if d is not None:
        say(f"  녹화 중 어깨 기울기 변화 (중앙값): {d:+.1f}°  (+ 사람 왼쪽 / - 오른쪽)")
    problems = []
    if status != "completed":
        problems.append("끝까지 녹화되지 않음")
    if s["rows_record"] and s["detect_ratio_record"] < LOW_DETECT_RATIO:
        problems.append(f"녹화 인식률 {s['detect_ratio_record']:.0%}")
    if d is not None and hint(tilt, d):
        problems.append(hint(tilt, d).strip("⚠ ").strip())
    if problems:
        say("  ⚠ 다시 수집을 권장해요: " + ", ".join(problems))
        say("    (파일은 지우지 말고 두세요. 학습할 때 세션 정보로 걸러낼 수 있어요.)")
    say("-" * 60)


def parse_args(cfg: dict) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="카메라만으로 상체 좌우 기울기 데이터 수집")
    ap.add_argument("--tilt", choices=TILT_LABELS, help="자세 라벨: none(바른 자세) / left / right (사람 기준)")
    ap.add_argument("--subject", default=None, help="사람 ID (p1, p2 …, 실명 금지)")
    ap.add_argument("--seconds", type=float, default=30, help="녹화 시간(초), 기본 30")
    ap.add_argument("--baseline", type=float, default=cfg["thresholds"]["baseline_seconds"],
                    help="기준 자세 측정 시간(초), 기본 config")
    ap.add_argument("--transition", type=float, default=3, help="자세 바꾸는 시간(초, 저장 안 함), 기본 3")
    ap.add_argument("--countdown", type=int, default=5, help="시작 전 카운트다운(초), 기본 5")
    ap.add_argument("--source", choices=["picam", "webcam", "mock"], default="picam",
                    help="picam: Pi 카메라 모듈(기본) / webcam: 맥 웹캠 / mock: 카메라 없이 코드 확인")
    ap.add_argument("--index", type=int, default=None, help="webcam 번호 (기본 config)")
    ap.add_argument("--model", type=int, choices=[0, 1, 2],
                    default=cfg["camera"].get("model_complexity", 1),
                    help="MediaPipe 모델 0: lite(빠름, 처음에 인터넷 필요) / 1: full / 2: heavy")
    ap.add_argument("--fps", type=float, default=cfg["camera"]["fps"],
                    help="최대 저장 속도(초당 프레임), 기본 config")
    ap.add_argument("--rotate180", action="store_true", help="카메라를 거꾸로 달았을 때 (좌우는 바뀌지 않음)")
    ap.add_argument("--notes", default="", help="세션 정보: 특이사항 (예: 안경, 조명 어두움)")
    ap.add_argument("--check", action="store_true", help="저장 없이 인식률·속도만 확인")
    ap.add_argument("--check-seconds", type=float, default=10)
    ap.add_argument("--snapshot", action="store_true", help="--check 때 구도 확인용 사진 1장 저장")
    args = ap.parse_args()

    if args.snapshot and not args.check:
        ap.error("--snapshot은 --check와 함께 쓰세요")
    if not args.check:
        if args.tilt is None:
            ap.error("--tilt를 지정하세요 (none / left / right)")
        if args.subject is None:
            ap.error("--subject를 지정하세요 (예: --subject p1, 실명 금지)")
        if not SUBJECT_RE.match(args.subject):
            ap.error(f"--subject '{args.subject}'는 영문·숫자·_- 20자 이내로 (예: p1)")
    for name in ("seconds", "fps", "check_seconds"):
        if getattr(args, name) <= 0:
            ap.error(f"--{name.replace('_', '-')}는 0보다 커야 함")
    for name in ("baseline", "transition"):
        if getattr(args, name) < 0:
            ap.error(f"--{name}는 0 이상이어야 함")
    if args.countdown < 0:
        ap.error("--countdown은 0 이상이어야 함")
    if args.source == "mock" and not args.check and args.subject not in ("test", "mock"):
        ap.error("--source mock은 가짜 데이터라 --subject test로만 저장할 수 있어요")
    return args


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="[collect] %(levelname)s %(message)s")
    stream = None
    try:
        cfg = load_config()
        args = parse_args(cfg)
        out_dir = repo_path(cfg["paths"]["raw"]) / "camera"
        say("카메라를 여는 중… (MediaPipe 준비에 몇 초 걸려요)")
        stream = open_stream(args, cfg)
        say(f"카메라 {args.source} {stream.width}x{stream.height}, MediaPipe 모델 {args.model}")
        if args.check:
            run_check(stream, args.check_seconds, args.snapshot, out_dir,
                      cfg["upper_rules"]["min_visibility"])
        else:
            run_collect(stream, args, cfg, out_dir)
    except KeyboardInterrupt:
        log.info("Ctrl+C로 종료 (저장 중이던 세션은 status=interrupted로 남음)")
        return 130
    except (ConfigError, LabelError) as e:
        log.error("%s", e)
        return 1
    except SensorError as e:
        log.error("%s", e)
        return 2
    except RecorderError as e:
        log.error("%s", e)
        return 3
    finally:
        if stream is not None:
            stream.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
