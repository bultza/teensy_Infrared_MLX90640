"""Check virtual output independently of kernel modules and physical cameras."""
import threading
import time
import unittest

import numpy as np

from thermal_viewer.webcam import VirtualWebcam


class FakeCamera:
    device = 'test-output'

    def __init__(self, **kwargs):
        self.options = kwargs
        self.frames = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def send(self, frame):
        self.frames.append(frame.copy())


class WebcamTests(unittest.TestCase):
    def until(self, predicate):
        deadline = time.monotonic()+3
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(predicate())

    def test_latest_image_copy_repeat_stale_and_cleanup(self):
        camera = FakeCamera()
        output = VirtualWebcam('/dev/video10', camera_factory=lambda **kwargs: camera)
        self.addCleanup(output.stop)
        try:
            rgb = np.zeros((24, 32, 3), dtype=np.uint8)
            rgb[:, :16] = (255, 0, 0)
            output.submit(rgb)
            rgb[:] = 0
            self.until(lambda: len(camera.frames) >= 3)
            self.assertEqual(camera.frames[-1].shape, (480, 640, 3))
            np.testing.assert_array_equal(camera.frames[-1][0, 0], [255, 0, 0])
            np.testing.assert_array_equal(camera.frames[-1][-1, -1], [0, 0, 0])
            np.testing.assert_array_equal(camera.frames[-1], camera.frames[-2])
            output.submit(np.full((24, 32, 3), 100, np.uint8),
                          received_at=time.monotonic()-11)
            self.until(lambda: not camera.frames[-1].any())
        finally:
            output.stop()
            self.assertTrue(output.finished.wait(2))
        self.assertTrue(camera.closed)
        self.assertIsNone(output.error)

    def test_slow_backend_does_not_block_submit_and_latest_wins(self):
        entered, release = threading.Event(), threading.Event()
        camera = FakeCamera()

        def factory(**kwargs):
            entered.set()
            release.wait(3)
            return camera

        output = VirtualWebcam(camera_factory=factory)
        try:
            self.assertTrue(entered.wait(2))
            for value in range(200):
                output.submit(np.full((24, 32, 3), value, np.uint8))
            release.set()
            self.until(lambda: len(camera.frames) >= 1)
            self.assertTrue((camera.frames[0] == 199).all())
        finally:
            release.set()
            output.stop()
            self.assertTrue(output.finished.wait(2))

    def test_send_failure_closes_backend_and_reports_error(self):
        camera = FakeCamera()

        def fail(frame):
            raise OSError('output disconnected')

        camera.send = fail
        output = VirtualWebcam(camera_factory=lambda **kwargs: camera)
        self.assertTrue(output.finished.wait(2))
        self.assertIn('output disconnected', output.error)
        self.assertTrue(camera.closed)

    def test_open_failure_is_reported(self):
        def fail(**kwargs):
            raise RuntimeError('no loopback device')

        output = VirtualWebcam(camera_factory=fail)
        self.assertTrue(output.finished.wait(2))
        self.assertIn('no loopback device', output.status)
