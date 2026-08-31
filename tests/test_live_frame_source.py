from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.live import frame_source


class FakeCapture:
    def __init__(
        self,
        reads: list[tuple[bool, object | None]],
        *,
        opened: bool = True,
        properties: dict[int, float] | None = None,
    ) -> None:
        self._reads = iter(reads)
        self._opened = opened
        self._properties = properties or {}
        self.release_calls = 0
        self.set_calls: list[tuple[int, int]] = []

    def isOpened(self) -> bool:
        return self._opened

    def get(self, property_id: int) -> float:
        return self._properties.get(property_id, 0.0)

    def read(self) -> tuple[bool, object | None]:
        return next(self._reads, (False, None))

    def set(self, property_id: int, value: int) -> bool:
        self.set_calls.append((property_id, value))
        return True

    def release(self) -> None:
        self.release_calls += 1
        self._opened = False


class FakeTime:
    def __init__(self, monotonic_values: list[float]) -> None:
        self._monotonic_values = iter(monotonic_values)
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return next(self._monotonic_values)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def fake_cv2_with(
    captures: list[FakeCapture],
) -> tuple[SimpleNamespace, list[object]]:
    capture_iterator = iter(captures)
    capture_arguments: list[object] = []

    def video_capture(argument: object) -> FakeCapture:
        capture_arguments.append(argument)
        return next(capture_iterator)

    fake_cv2 = SimpleNamespace(
        CAP_PROP_FPS=1,
        CAP_PROP_FRAME_WIDTH=2,
        CAP_PROP_FRAME_HEIGHT=3,
        CAP_PROP_OPEN_TIMEOUT_MSEC=4,
        CAP_PROP_READ_TIMEOUT_MSEC=5,
        VideoCapture=video_capture,
    )
    return fake_cv2, capture_arguments


def test_open_source_routes_webcam_urls_and_video_file(monkeypatch):
    webcam_result = object()
    camera_result = object()
    video_result = object()
    webcam_calls: list[int] = []
    camera_calls: list[tuple[str, dict[str, float]]] = []
    video_calls: list[str] = []

    def fake_webcam(index: int) -> object:
        webcam_calls.append(index)
        return webcam_result

    def fake_camera(url: str, **kwargs: float) -> object:
        camera_calls.append((url, kwargs))
        return camera_result

    def fake_video(path: str) -> object:
        video_calls.append(path)
        return video_result

    monkeypatch.setattr(frame_source, "WebcamSource", fake_webcam)
    monkeypatch.setattr(frame_source, "CameraStreamSource", fake_camera)
    monkeypatch.setattr(frame_source, "VideoFileSource", fake_video)

    assert frame_source.open_source("007") is webcam_result
    for url in (
        "http://camera.local/stream",
        "https://camera.local/stream",
        "rtsp://camera.local/live",
    ):
        assert (
            frame_source.open_source(url, read_timeout_s=2.5) is camera_result
        )
    assert frame_source.open_source("fixtures/dinner.mp4") is video_result

    assert webcam_calls == [7]
    assert camera_calls == [
        ("http://camera.local/stream", {"read_timeout_s": 2.5}),
        ("https://camera.local/stream", {"read_timeout_s": 2.5}),
        ("rtsp://camera.local/live", {"read_timeout_s": 2.5}),
    ]
    assert video_calls == ["fixtures/dinner.mp4"]


def test_video_file_source_has_deterministic_timestamps_and_releases(
    monkeypatch,
):
    frames = [object(), object(), object()]
    capture = FakeCapture(
        [(True, frame) for frame in frames],
        properties={1: 25.0, 2: 640.0, 3: 360.0},
    )
    fake_cv2, capture_arguments = fake_cv2_with([capture])
    monkeypatch.setattr(frame_source, "cv2", fake_cv2)

    source = frame_source.VideoFileSource("meal.mp4")
    emitted = list(source.frames())

    assert capture_arguments == ["meal.mp4"]
    assert source.fps == 25.0
    assert (source.width, source.height) == (640, 360)
    assert [pts_ms for pts_ms, _ in emitted] == pytest.approx(
        [0.0, 40.0, 80.0]
    )
    assert [frame for _, frame in emitted] == frames
    assert capture.release_calls == 0

    source.close()
    assert capture.release_calls == 1


def test_webcam_source_uses_wall_clock_timestamps_and_releases(monkeypatch):
    frames = [object(), object()]
    capture = FakeCapture(
        [(True, frame) for frame in frames],
        properties={2: 1280.0, 3: 720.0},
    )
    fake_cv2, capture_arguments = fake_cv2_with([capture])
    fake_time = FakeTime([50.0, 50.125])
    monkeypatch.setattr(frame_source, "cv2", fake_cv2)
    monkeypatch.setattr(frame_source, "time", fake_time)

    source = frame_source.WebcamSource(3)
    emitted = list(source.frames())

    assert capture_arguments == [3]
    assert (source.width, source.height) == (1280, 720)
    assert [pts_ms for pts_ms, _ in emitted] == pytest.approx([0.0, 125.0])
    assert [frame for _, frame in emitted] == frames

    source.close()
    assert capture.release_calls == 1


def test_camera_stream_yields_a_wall_clock_frame_and_close_stops_it(
    monkeypatch,
):
    frame = object()
    capture = FakeCapture([(True, frame)])
    fake_cv2, capture_arguments = fake_cv2_with([capture])
    fake_time = FakeTime([20.0, 20.25])
    monkeypatch.setattr(frame_source, "cv2", fake_cv2)
    monkeypatch.setattr(frame_source, "time", fake_time)

    source = frame_source.CameraStreamSource(
        "http://camera.local/stream",
        read_timeout_s=1.75,
    )
    iterator = source.frames()

    pts_ms, emitted_frame = next(iterator)
    assert pts_ms == pytest.approx(0.0)
    assert emitted_frame is frame
    assert capture_arguments == ["http://camera.local/stream"]
    assert capture.set_calls == [(4, 1750), (5, 1750)]

    source.close()
    assert capture.release_calls == 1
    assert source._cap is None
    with pytest.raises(StopIteration):
        next(iterator)


def test_camera_stream_reconnects_with_bounded_backoff_and_releases_old_caps(
    monkeypatch,
):
    frame = object()
    captures = [
        FakeCapture([(False, None)]),
        FakeCapture([(False, None)]),
        FakeCapture([(True, frame)]),
    ]
    fake_cv2, capture_arguments = fake_cv2_with(captures)
    fake_time = FakeTime([100.0, 100.1, 100.2, 100.3])
    monkeypatch.setattr(frame_source, "cv2", fake_cv2)
    monkeypatch.setattr(frame_source, "time", fake_time)

    source = frame_source.CameraStreamSource(
        "rtsp://camera.local/live",
        reconnect_backoff_s=0.25,
        max_backoff_s=0.4,
        read_timeout_s=2.0,
    )
    pts_ms, emitted_frame = next(source.frames())

    assert pts_ms == pytest.approx(0.0)
    assert emitted_frame is frame
    assert capture_arguments == ["rtsp://camera.local/live"] * 3
    assert fake_time.sleeps == [0.25, 0.4]
    assert [capture.release_calls for capture in captures] == [1, 1, 0]
    for capture in captures:
        assert capture.set_calls == [(4, 2000), (5, 2000)]

    source.close()
    assert [capture.release_calls for capture in captures] == [1, 1, 1]
