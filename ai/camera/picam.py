"""Raspberry Pi 카메라 모듈 (CSI, Picamera2) → FrameReading

맥의 Webcam(ai/camera/pose.py)과 같은 틀(CameraSensor)이라 PoseExtractor를 그대로 쓴다.

    cam = PiCamera(width=1280, height=720)
    frame = cam.read()                    # FrameReading (BGR 이미지)
    pose = PoseExtractor(...).extract(frame)

Pi 준비 (자세한 내용: docs/pi_camera_collect.md)
    sudo apt install -y python3-picamera2            # pip로 설치하지 않음
    python3 -m venv --system-site-packages .venv     # 가상환경에서 picamera2가 보이게

좌우 주의
    - 화면을 거울처럼 좌우 반전(hflip)하면 MediaPipe의 왼쪽·오른쪽이 뒤바뀌어 라벨이 전부 틀어진다.
      그래서 좌우 반전 옵션은 두지 않았다.
    - 카메라를 거꾸로 달았으면 rotate180=True (180도 회전은 좌우가 바뀌지 않는다).
"""
from __future__ import annotations

import logging
import time

from common.sensors_base import CameraSensor, FrameReading, SensorError, now

log = logging.getLogger(__name__)

WARMUP_SEC = 1.0          # 켠 직후 자동 노출이 자리 잡는 시간


class PiCamera(CameraSensor):
    def __init__(self, width: int = 1280, height: int = 720, rotate180: bool = False):
        try:
            from libcamera import Transform
            from picamera2 import Picamera2
        except ImportError:
            raise SensorError(
                "picamera2를 찾을 수 없음 — Pi에서 'sudo apt install -y python3-picamera2'를 하고, "
                "가상환경은 'python3 -m venv --system-site-packages .venv'로 만들어야 함 "
                "(맥에서는 --source webcam 사용)") from None

        if not Picamera2.global_camera_info():
            raise SensorError("Pi가 카메라를 찾지 못함 — 리본 케이블 방향·꽂힘 확인 후 "
                              "'rpicam-hello --list-cameras'로 확인 (재부팅 필요할 수 있음)")
        try:
            self.cam = Picamera2()
            # RGB888 = 메모리에 B, G, R 순서 → OpenCV(BGR)에서 그대로 쓴다
            config = self.cam.create_video_configuration(
                main={"size": (width, height), "format": "RGB888"},
                transform=Transform(hflip=rotate180, vflip=rotate180),
            )
            self.cam.configure(config)
            self.cam.start()
        except Exception as e:  # noqa: BLE001 — libcamera 오류 종류가 다양함
            raise SensorError(f"Pi 카메라를 시작하지 못함 (다른 프로그램이 쓰고 있지 않은지 확인): {e}") from e
        time.sleep(WARMUP_SEC)
        self.width, self.height = self.cam.camera_configuration()["main"]["size"]
        if (self.width, self.height) != (width, height):
            log.warning("요청한 해상도 %dx%d 대신 %dx%d로 열림 (세션 정보에는 실제 값이 저장됨)",
                        width, height, self.width, self.height)

    def read(self) -> FrameReading | None:
        try:
            image = self.cam.capture_array("main")   # 다음 프레임이 나올 때까지 기다림
        except Exception as e:  # noqa: BLE001
            log.warning("Pi 카메라 프레임 읽기 실패: %s", e)
            return None
        if image is None or image.ndim != 3:
            return None
        if image.shape[2] == 4:                      # 혹시 XBGR8888로 열리면 알파 채널 제거
            image = image[:, :, :3]
        return FrameReading(ts=now(), image=image)

    def close(self) -> None:
        try:
            self.cam.stop()
            self.cam.close()
        except Exception as e:  # noqa: BLE001
            log.warning("Pi 카메라 닫기 실패: %s", e)
