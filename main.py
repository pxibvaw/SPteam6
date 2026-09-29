"""SitSense 전체 실행 (센서 → 판단 → 저장)

사용 예 (저장소 최상위에서 실행):
    python main.py --mode mock --seconds 5          # 가짜 센서로 5초
    python main.py --mode mock --record --seat cross_left --head forward --tilt none --seconds 40
        # data/raw에 세션 CSV + JSON 저장 (처음 10초는 기준 자세, 이후 지정한 자세)
    python main.py --mode replay --file data/raw/p1_20261015_143012.csv

지금은 뼈대 단계라 '판단' 자리는 비어 있고, 센서 값만 출력한다.
    - 판단(엔진): ai/engine/ 완성 후 연결 (AI 담당)
    - 실제 센서: hardware/sensors/ 완성 후 연결 (하드웨어 담당)
    - 저장·서버: hardware/server/ 완성 후 연결 (하드웨어 담당)

종료 코드: 0 정상 / 1 설정·입력 오류 / 2 센서 오류 / 3 저장 오류 / 130 Ctrl+C
"""
from __future__ import annotations

import argparse
import contextlib
import logging
import sys
import time

from common.config import ConfigError, load_config, repo_path
from common.mock_sensors import MockSensorHub
from common.recorder import SUBJECT_RE, RecorderError, SessionInfo, SessionRecorder
from common.replay import ReplayError, ReplayFinished, ReplaySource
from common.schema import HEAD_LABELS, SEAT_LABELS, TILT_LABELS, LabelError, check_labels
from common.sensors_base import SampleSkipped, SensorError

log = logging.getLogger("sitsense")


def build_source(cfg: dict, args):
    if args.mode == "mock":
        hub = MockSensorHub(cfg)
        hub.set_labels(args.seat, args.head, args.tilt)
        return hub
    if args.mode == "replay":
        path = args.file or cfg["replay"]["file"]
        if not path:
            raise ReplayError("재생할 파일을 --file 또는 config.yaml의 replay.file로 지정하세요")
        return ReplaySource(path, loop=cfg["replay"]["loop"],
                            expected_channels=cfg["pressure"]["n_channels"])
    if args.mode == "real":
        # TODO(하드웨어): hardware/sensors/의 실제 센서로 SensorHub(pressure, distance, pose,
        #                 n_channels=…, adc_max=…)를 만들어 반환
        # TODO(AI): pose에는 ai/camera의 MediaPipe 결과를 주는 PoseSource를 연결
        raise SensorError("실제 센서 코드가 아직 없음 (hardware/sensors/, ai/camera/)")
    raise ConfigError(f"알 수 없는 mode: {args.mode}")


def fmt(sample) -> str:
    d = sample.distance
    dist = f"{d.distance_mm}mm" if d.valid else f"실패(status={d.range_status})"
    pose = "없음" if sample.pose is None else ("인식" if sample.pose.detected else "미인식")
    return f"{sample.ts:.1f}  압력={sample.pressure.values}  거리={dist}  카메라={pose}"


def parse_args(cfg: dict) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="SitSense 실행")
    ap.add_argument("--mode", choices=["mock", "replay", "real"], default=cfg["mode"])
    ap.add_argument("--seconds", type=float, default=None, help="실행 시간 (없으면 Ctrl+C까지)")
    ap.add_argument("--file", default=None, help="replay 모드에서 재생할 세션 CSV")
    ap.add_argument("--record", action="store_true", help="세션 CSV + JSON으로 저장")
    ap.add_argument("--subject", default=cfg["subject"], help="사람 ID (p1, p2, …)")
    ap.add_argument("--seat", choices=SEAT_LABELS, default="normal", help="좌면 자세 라벨")
    ap.add_argument("--head", choices=HEAD_LABELS, default="normal", help="목 자세 라벨")
    ap.add_argument("--tilt", choices=TILT_LABELS, default="none", help="좌우 기울기 라벨")
    ap.add_argument("--chair", default="", help="세션 정보: 의자")
    ap.add_argument("--cushion", default="", help="세션 정보: 방석")
    ap.add_argument("--notes", default="", help="세션 정보: 특이사항")
    ap.add_argument("--quiet", action="store_true", help="매 줄 출력하지 않기")
    args = ap.parse_args()
    if args.seconds is not None and args.seconds <= 0:
        ap.error("--seconds는 0보다 커야 함")
    if args.record and args.mode == "replay":
        ap.error("replay 모드에서는 --record를 쓸 수 없음")
    if args.record and not SUBJECT_RE.match(args.subject):
        ap.error(f"--subject '{args.subject}'는 영문·숫자·_- 20자 이내로 (예: p1, 실명 금지)")
    return args


def run(cfg: dict, args: argparse.Namespace) -> None:
    check_labels(args.seat, args.head, args.tilt)
    source = build_source(cfg, args)

    with contextlib.ExitStack() as stack:
        stack.callback(source.close)
        recorder = None
        if args.record:
            info = SessionInfo.new(args.subject, cfg, chair=args.chair,
                                   cushion=args.cushion, notes=args.notes)
            recorder = stack.enter_context(
                SessionRecorder(cfg["paths"]["raw"], info, cfg["pressure"]["n_channels"]))
            log.info("세션 %s: 처음 %g초는 바른 자세로 앉아 주세요 (기준 측정)",
                     info.session_id, info.baseline_seconds)

        period = 1.0 / cfg["sampling"]["hz"]
        t_end = None if args.seconds is None else time.time() + args.seconds
        log.info("mode=%s, %gHz, Ctrl+C로 종료", args.mode, cfg["sampling"]["hz"])

        last_phase, skipped = None, 0
        try:
            while t_end is None or time.time() < t_end:
                t0 = time.time()
                phase = recorder.phase_at(t0) if recorder else None
                if isinstance(source, MockSensorHub):
                    # 기준 측정 중에는 가짜 센서도 바른 자세 값을 내게 함 (실제로는 사람이 바르게 앉음)
                    if phase == "baseline":
                        source.set_labels()
                    else:
                        source.set_labels(args.seat, args.head, args.tilt)

                try:
                    sample = source.read()
                except SampleSkipped as e:       # 이번 순간만 건너뜀 (연속 실패는 SensorError로 멈춤)
                    skipped += 1
                    log.debug("%s", e)
                    time.sleep(period)
                    continue

                # TODO(AI): ai/engine 연결 → PostureEvent 생성
                # TODO(하드웨어): PostureEvent를 DB에 저장

                line = fmt(sample)
                if recorder:
                    phase = recorder.write(sample, args.seat, args.head, args.tilt, phase=phase)
                    if phase != last_phase:
                        log.info("── %s ──", phase)
                        last_phase = phase
                elif args.mode == "replay":
                    line += f"  라벨={source.labels}"
                if not args.quiet:
                    print(line)
                time.sleep(max(0.0, period - (time.time() - t0)))
        except ReplayFinished:
            log.info("재생 끝")
        finally:
            if skipped:
                log.warning("압력 읽기 실패로 %d순간을 건너뜀", skipped)

    if recorder:
        log.info("%d줄 저장: %s (+ .json, 상태=%s)", recorder.count,
                 recorder.csv_path.relative_to(repo_path(".")), recorder.info.status)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="[SitSense] %(levelname)s %(message)s")
    try:
        cfg = load_config()
        args = parse_args(cfg)
        run(cfg, args)
    except KeyboardInterrupt:
        log.info("Ctrl+C로 종료 (저장 중이던 세션은 status=interrupted로 남음)")
        return 130
    except (ConfigError, LabelError, ReplayError) as e:
        log.error("%s", e)
        return 1
    except SensorError as e:
        log.error("%s", e)
        return 2
    except RecorderError as e:
        log.error("%s", e)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
