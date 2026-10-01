"""웹캠 + MediaPipe Pose → 상체 점 7개 (PoseReading)

사용 예:
    cam = Webcam(index=0, width=1280, height=720)
    extractor = PoseExtractor(model_complexity=1)
    frame = cam.read()
    pose = extractor.extract(frame)      # PoseReading (common/sensors_base.py)

- 점 7개와 MediaPipe 번호는 common/schema.py의 LANDMARKS를 따른다.
- 좌표는 MediaPipe 원래 값(0~1 비율)을 그대로 담는다. 픽셀 변환은 ai/features에서.
- left/right는 사람 기준이다. 웹캠 원본 영상(좌우 반전 전)에서는 사람 왼쪽이 화면 오른쪽에 찍힌다.

Pi에서는 Webcam 대신 Picamera2로 FrameReading을 만드는 클래스를 hardware/sensors에 두고,
PoseExtractor는 그대로 쓰면 된다.
"""
from __future__ import annotations

import logging

import cv2

from common.schema import LANDMARKS
from common.sensors_base import CameraSensor, FrameReading, Landmark, PoseReading, SensorError, now

log = logging.getLogger(__name__)


class Webcam(CameraSensor):
    """맥 웹캠 (또는 --source로 동영상 파일)"""

    def __init__(self, index: int = 0, width: int = 1280, height: int = 720,
                 source: str | None = None):
        self.is_file = source is not None
        self.cap = cv2.VideoCapture(source if self.is_file else index)
        if not self.cap.isOpened():
            hint = ("파일 경로를 확인하세요" if self.is_file else
                    "맥: 시스템 설정 → 개인정보 보호 및 보안 → 카메라에서 터미널(또는 VS Code) 허용, "
                    "다른 앱이 카메라를 쓰고 있지 않은지 확인")
            raise SensorError(f"카메라를 열 수 없음 ({source or f'index {index}'}) — {hint}")
        if not self.is_file:
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if not self.is_file and (self.width, self.height) != (width, height):
            log.warning("요청한 해상도 %dx%d 대신 %dx%d로 열림 (세션 정보에는 실제 값을 써야 함)",
                        width, height, self.width, self.height)

    def read(self) -> FrameReading | None:
        ok, frame = self.cap.read()
        if not ok or frame is None:
            return None
        return FrameReading(ts=now(), image=frame)

    def close(self) -> None:
        self.cap.release()


class PoseExtractor:
    """MediaPipe Pose로 한 프레임에서 점 7개를 뽑는다.

    model_complexity: 0 = lite (가장 빠름, Pi용, 처음 실행 때 모델 다운로드 필요)
                      1 = full (기본, 설치 파일에 포함)
                      2 = heavy (가장 정확, 느림, 다운로드 필요)
    """

    def __init__(self, model_complexity: int = 1, min_detection_confidence: float = 0.5,
                 min_tracking_confidence: float = 0.5):
        if model_complexity not in (0, 1, 2):
            raise ValueError(f"model_complexity는 0, 1, 2 중 하나 (지금: {model_complexity})")
        import mediapipe as mp          # 무거워서 필요할 때만 불러온다
        try:
            self._pose = mp.solutions.pose.Pose(
                static_image_mode=False,         # 영상: 앞 프레임 결과로 추적 → 빠르고 안정적
                model_complexity=model_complexity,
                smooth_landmarks=True,           # 프레임 사이 떨림 완화
                enable_segmentation=False,
                min_detection_confidence=min_detection_confidence,
                min_tracking_confidence=min_tracking_confidence,
            )
        except OSError as e:
            raise SensorError(f"MediaPipe 모델을 불러오지 못함 (model_complexity={model_complexity}"
                              f"는 처음 실행 때 인터넷이 필요): {e}") from e

    def extract(self, frame: FrameReading) -> PoseReading:
        rgb = cv2.cvtColor(frame.image, cv2.COLOR_BGR2RGB)
        rgb.flags.writeable = False              # MediaPipe가 복사하지 않고 읽게 (속도)
        result = self._pose.process(rgb)
        if result.pose_landmarks is None:
            return PoseReading.empty(frame.ts)
        lms = result.pose_landmarks.landmark
        points = {
            name: Landmark(x=lms[idx].x, y=lms[idx].y, z=lms[idx].z, visibility=lms[idx].visibility)
            for name, idx in LANDMARKS.items()
        }
        return PoseReading(ts=frame.ts, detected=True, points=points)

    def close(self) -> None:
        self._pose.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
