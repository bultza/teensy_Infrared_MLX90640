"""Optional virtual-camera output with a bounded latest-frame mailbox."""
import threading
import time

import numpy as np
from PIL import Image


def open_camera(**kwargs):
    """Load the optional backend only when virtual-camera output starts."""
    try:
        import pyvirtualcam
    except ImportError as exc:
        raise RuntimeError('Install webcam support: python -m pip install -r requirements-webcam.txt') from exc
    try:
        return pyvirtualcam.Camera(**kwargs)
    except Exception as exc:
        raise RuntimeError(f'{exc}. On Linux, load v4l2loopback and check device permissions; '
                           'see the README virtual webcam setup.') from exc


class VirtualWebcam:
    """Send the latest RGB image at 30 FPS without blocking acquisition or Qt."""
    WIDTH, HEIGHT, FPS = 640, 480, 30
    STALE_SECONDS = 10

    def __init__(self, device=None, camera_factory=None):
        self.device = device or None
        self.camera_factory = camera_factory or open_camera
        self.stop_event = threading.Event()
        self.finished = threading.Event()
        self.lock = threading.Lock()
        self.latest = None
        self.error = None
        self.status = 'Starting virtual webcam…'
        self.thread = threading.Thread(target=self._run, name='virtual-webcam', daemon=True)
        self.thread.start()

    def submit(self, rgb, received_at=None):
        """Copy one sensor-sized image; replace any unconsumed image."""
        rgb = np.asarray(rgb)
        if rgb.shape != (24, 32, 3) or rgb.dtype != np.uint8:
            raise ValueError('Webcam input must be a 24 × 32 uint8 RGB image')
        with self.lock:
            if not self.stop_event.is_set():
                self.latest = (rgb.copy(), time.monotonic() if received_at is None else received_at)

    def stop(self):
        """Request shutdown; the GUI polls finished instead of joining its thread."""
        with self.lock:
            self.stop_event.set()
            self.latest = None

    def _run(self):
        try:
            with self.camera_factory(width=self.WIDTH, height=self.HEIGHT, fps=self.FPS,
                                     device=self.device) as camera:
                connected = f'{camera.device} · 640 × 480 / 30 FPS'
                blank = np.zeros((self.HEIGHT, self.WIDTH, 3), dtype=np.uint8)
                frame, rendered_input = blank, None
                deadline = time.monotonic()
                while not self.stop_event.is_set():
                    with self.lock:
                        latest = self.latest
                    if latest is None or time.monotonic()-latest[1] > self.STALE_SECONDS:
                        frame, rendered_input = blank, None
                        self.status = f'Waiting for fresh images · {connected}'
                    elif latest is not rendered_input:
                        frame = np.array(Image.fromarray(latest[0]).resize(
                            (self.WIDTH, self.HEIGHT), Image.Resampling.NEAREST))
                        rendered_input = latest
                        self.status = f'Live · {connected}'
                    camera.send(frame)
                    # Use an interruptible monotonic schedule and skip missed output slots.
                    deadline += 1/self.FPS
                    now = time.monotonic()
                    if deadline < now:
                        deadline = now + 1/self.FPS
                    self.stop_event.wait(max(0, deadline-now))
        except Exception as exc:
            self.error = str(exc)
            self.status = f'Webcam failed: {exc}'
        finally:
            if self.error is None:
                self.status = 'Virtual webcam stopped'
            self.finished.set()
