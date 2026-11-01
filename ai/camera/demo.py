"""웹캠 상체 판단 데모 — 직접 앉아서 확인하고 threshold를 조정할 때 사용

실행 (저장소 최상위, .venv 켠 상태):
    python -m ai.camera.demo                    # 맥 웹캠
    python -m ai.camera.demo --list-cameras     # 카메라 번호 확인 (아이폰이 잡힐 때)
    python -m ai.camera.demo --index 1          # 1번 카메라로
    python -m ai.camera.demo --source 영상.mp4  # 녹화한 영상으로
    python -m ai.camera.demo --no-window --seconds 30   # 화면 없이 1초마다 결과만 출력

사용법
    1. 처음 10초는 바른 자세로 앉기 (기준 측정)
    2. 거북목, 좌우로 기울이기를 해 보면서 화면의 값과 판단을 확인
    3. 키:  b = 기준 다시 측정,  q 또는 ESC = 종료

화면은 거울처럼 좌우 반전해서 보여준다 (판단은 원본 영상 기준, left/right는 사람 기준).
화면 글자는 OpenCV가 한글을 못 써서 영어로 표시한다.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

import cv2
import numpy as np

from ai.camera.pose import PoseExtractor, Webcam
from ai.engine.upper_body import Baseline, FeatureSmoother, StateFilter, UpperRules, judge
from ai.features.pose_features import to_pixels, upper_features
from common.config import ConfigError, load_config
from common.sensors_base import SensorError

log = logging.getLogger("sitsense.demo")

GREEN, RED, YELLOW, WHITE, GRAY = (80, 200, 80), (60, 60, 230), (0, 200, 255), (255, 255, 255), (160, 160, 160)


def _mx(x: float, w: int) -> int:
    """원본 x → 거울 화면 x"""
    return int(w - x)


def draw(disp: np.ndarray, pose, w: int, h: int, feat, base: Baseline, j, head_state, tilt_state,
         fps: float, ts: float, pending) -> None:
    # 점과 선
    if pose is not None and pose.detected:
        px = to_pixels(pose, w, h)
        pts = {k: (_mx(v[0], w), int(v[1])) for k, v in px.items()}
        for a, b in (("left_shoulder", "right_shoulder"), ("left_eye", "right_eye")):
            if a in pts and b in pts:
                cv2.line(disp, pts[a], pts[b], YELLOW, 2)
        if "left_shoulder" in pts and "right_shoulder" in pts and "nose" in pts:
            neck = ((pts["left_shoulder"][0] + pts["right_shoulder"][0]) // 2,
                    (pts["left_shoulder"][1] + pts["right_shoulder"][1]) // 2)
            cv2.line(disp, neck, pts["nose"], YELLOW, 2)
            cv2.circle(disp, neck, 7, WHITE, -1)
        for name, p in pts.items():
            vis = pose.points[name].visibility
            cv2.circle(disp, p, 6, GREEN if vis >= 0.5 else GRAY, -1)

    # 글자 영역
    cv2.rectangle(disp, (0, 0), (430, 250), (0, 0, 0), -1)
    y = 28

    def put(text, color=WHITE, scale=0.6):
        nonlocal y
        cv2.putText(disp, text, (12, y), cv2.FONT_HERSHEY_SIMPLEX, scale, color, 2, cv2.LINE_AA)
        y += int(30 * scale / 0.6)

    put(f"FPS {fps:4.1f}   {w}x{h}", GRAY, 0.5)
    if feat is None:
        put("NO PERSON / points hidden", RED)
    if not base.ready:
        put(f"BASELINE: sit straight  {base.remaining(ts):4.1f}s  (n={base.count})", YELLOW)
        return
    hs = head_state.value.upper() if head_state else "-"
    ts_ = tilt_state.value.upper() if tilt_state else "-"
    put(f"HEAD: {hs}", RED if hs == "FORWARD" else GREEN, 0.8)
    put(f"TILT: {ts_}", RED if ts_ in ("LEFT", "RIGHT") else GREEN, 0.8)
    if pending:
        put(f"  changing to {pending[0].value} in {pending[1]:.1f}s", GRAY, 0.5)
    if j is not None and j.deltas:
        d, c = j.deltas, j.cues

        def col(k):
            return RED if c.get(k) else WHITE
        put(f"neck drop  {d['neck_drop']*100:+5.1f}%", col("neck_drop"), 0.5)
        put(f"face grow  {d['face_grow']*100:+5.1f}%", col("face_grow"), 0.5)
        put(f"shoulder   {d['shoulder_angle']:+5.1f} deg", col("shoulder_angle"), 0.5)
        put(f"eye line   {d['head_angle']:+5.1f} deg", col("head_angle"), 0.5)
        put(f"nose shift {d['head_offset']*100:+5.1f}%", col("head_offset"), 0.5)
    cv2.putText(disp, "b: re-baseline  q: quit", (12, h - 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                GRAY, 1, cv2.LINE_AA)


def list_cameras(max_index: int = 5) -> None:
    """열리는 카메라 번호와 해상도를 보여준다 (맥은 아이폰 연속성 카메라가 섞일 수 있음)"""
    print("카메라 번호 확인 중… (각 번호마다 1~2초)")
    found = False
    for i in range(max_index):
        cap = cv2.VideoCapture(i)
        if cap.isOpened():
            ok, frame = cap.read()
            size = f"{frame.shape[1]}x{frame.shape[0]}" if ok and frame is not None else "프레임 없음"
            print(f"  {i}번: 열림 ({size})")
            found = True
        cap.release()
    if not found:
        print("  열리는 카메라가 없음 — 카메라 권한 확인")
    print("맥북 내장 카메라 번호로  python -m ai.camera.demo --index 번호  실행, "
          "맞으면 config.yaml의 camera.index를 그 번호로 바꾸기")


def run(args) -> None:
    cfg = load_config()
    cam_cfg = dict(cfg["camera"])
    if args.index is not None:
        cam_cfg["index"] = args.index
    rules = UpperRules.from_config(cfg)
    base_sec = args.baseline if args.baseline is not None else cfg["thresholds"]["baseline_seconds"]
    hold = cfg["thresholds"]["short_filter_sec"]

    with Webcam(cam_cfg["index"], cam_cfg["width"], cam_cfg["height"], source=args.source) as cam, \
            PoseExtractor(args.model if args.model is not None else cam_cfg.get("model_complexity", 1)) as ext:
        w, h = cam.width, cam.height
        base = Baseline(seconds=base_sec)
        smoother = FeatureSmoother(rules.smooth_frames)
        head_f, tilt_f = StateFilter(hold), StateFilter(hold)
        t_start, t_last_print, fps, misses = time.time(), 0.0, 0.0, 0
        log.info("카메라 %dx%d. 처음 %g초는 바른 자세로 앉아 주세요", w, h, base_sec)

        while args.seconds is None or time.time() - t_start < args.seconds:
            t0 = time.time()
            frame = cam.read()
            if frame is None:
                if cam.is_file:
                    log.info("영상 끝")
                    break
                misses += 1
                if misses >= 30:
                    raise SensorError("카메라에서 프레임이 30번 연속 안 들어옴 — 카메라 연결 확인")
                continue
            misses = 0

            pose = ext.extract(frame)
            feat = smoother.update(upper_features(pose, w, h, rules.min_visibility))
            j = head_state = tilt_state = pending = None
            if not base.ready:
                if base.add(feat, frame.ts):
                    log.info("기준 측정 완료 (%d프레임)", base.count)
            else:
                j = judge(feat, base, rules)
                head_state = head_f.update(j.head, frame.ts)
                tilt_state = tilt_f.update(j.tilt, frame.ts)
                pending = head_f.pending(frame.ts) or tilt_f.pending(frame.ts)

            dt = time.time() - t0
            fps = 0.9 * fps + 0.1 * (1.0 / dt) if dt > 0 else fps

            if args.no_window:
                if time.time() - t_last_print >= 1.0:
                    t_last_print = time.time()
                    if not base.ready:
                        print(f"기준 측정 중 {base.remaining(frame.ts):.1f}초 남음 (프레임 {base.count})")
                    elif feat is None:
                        print("사람 없음 / 점이 가려짐")
                    else:
                        d = j.deltas
                        print(f"목={head_state.value:8s} 기울기={tilt_state.value:6s}  "
                              f"목높이 {d['neck_drop']*100:+.1f}%  얼굴 {d['face_grow']*100:+.1f}%  "
                              f"어깨 {d['shoulder_angle']:+.1f}°  눈 {d['head_angle']:+.1f}°  "
                              f"코 {d['head_offset']*100:+.1f}%  fps {fps:.1f}")
                continue

            disp = cv2.flip(frame.image, 1)
            draw(disp, pose, w, h, feat, base, j, head_state, tilt_state, fps, frame.ts, pending)
            cv2.imshow("SitSense camera demo", disp)
            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27):
                break
            if key == ord("b"):
                base.reset()
                smoother.reset()
                head_f, tilt_f = StateFilter(hold), StateFilter(hold)
                log.info("기준 다시 측정: %g초 동안 바른 자세로", base_sec)
    if not args.no_window:
        cv2.destroyAllWindows()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="[SitSense] %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="웹캠 상체 판단 데모")
    ap.add_argument("--index", type=int, default=None,
                    help="카메라 번호 (기본: config.yaml camera.index). 아이폰이 잡히면 1로")
    ap.add_argument("--list-cameras", action="store_true", help="열리는 카메라 번호 목록만 보여주기")
    ap.add_argument("--source", default=None, help="웹캠 대신 쓸 동영상 파일")
    ap.add_argument("--model", type=int, choices=[0, 1, 2], default=None,
                    help="MediaPipe 모델 (기본: config.yaml camera.model_complexity)")
    ap.add_argument("--baseline", type=float, default=None, help="기준 측정 시간(초), 기본 config")
    ap.add_argument("--seconds", type=float, default=None, help="실행 시간 (없으면 q까지)")
    ap.add_argument("--no-window", action="store_true", help="화면 없이 1초마다 결과만 출력")
    args = ap.parse_args()
    if args.list_cameras:
        list_cameras()
        return 0
    try:
        run(args)
    except KeyboardInterrupt:
        return 130
    except (ConfigError, ValueError) as e:
        log.error("설정 오류: %s", e)
        return 1
    except SensorError as e:
        log.error("%s", e)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
