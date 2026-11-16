"""Exercise desktop buttons offscreen without connecting to physical USB."""
from argparse import Namespace
import importlib.util
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import numpy as np

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
HAS_QT = importlib.util.find_spec('PySide6') is not None
if HAS_QT:
    from PySide6 import QtWidgets, QtCore, QtTest
    from thermal_viewer.gui import Viewer


@unittest.skipUnless(HAS_QT, 'Qt not installed')
class DesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.window = Viewer(Namespace(port=None, demo=True, test_pattern=False, i2c_khz=400,
                                       fps=2, emissivity=0.95, reflected_temperature=None))
        self.window.show()
        self.until(lambda: self.window.latest is not None)

    def tearDown(self):
        self.window.close()
        self.until(lambda: not self.window.isVisible())
        self.window.deleteLater()
        self.app.processEvents()

    def until(self, condition, timeout=8):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            QtTest.QTest.qWait(20)
        self.assertTrue(condition(), 'Desktop operation did not complete')

    def click(self, widget):
        QtTest.QTest.mouseClick(widget, QtCore.Qt.MouseButton.LeftButton)

    def test_presets_and_display(self):
        self.assertEqual(self.window.profile.currentIndex(), 0)
        self.window.profile.setCurrentIndex(1)
        self.assertEqual(self.window.clock.currentText(), '100')
        self.assertEqual(self.window.fps.currentText(), '1')
        self.click(self.window.apply_button)
        self.until(lambda: not self.window.restart_pending and self.window.latest is not None)
        self.window.tabs.setCurrentIndex(1)
        self.window.palette.setCurrentText('viridis')
        self.window.auto_range.setChecked(False)
        self.window.low.setValue(900)
        self.window.high.setValue(7000)
        self.assertEqual(self.window.canvas.palette, 'viridis')
        self.assertEqual(self.window.canvas.scale, (900, 7000))
        self.assertEqual(self.window.canvas.units, 'counts')

    def pixel_center(self, x, y):
        rect = self.window.canvas.image_rect()
        return QtCore.QPointF(rect.left()+(31-x+0.5)*rect.width()/32,
                             rect.top()+(y+0.5)*rect.height()/24).toPoint()

    def test_webcam_buttons_disconnect_restart_and_close(self):
        from thermal_viewer.webcam import VirtualWebcam
        from test_webcam import FakeCamera
        cameras = []

        def factory(device):
            def open_fake(**kwargs):
                camera = FakeCamera(**kwargs)
                cameras.append(camera)
                return camera
            return VirtualWebcam(device, camera_factory=open_fake)

        with patch('thermal_viewer.gui.VirtualWebcam', side_effect=factory):
            self.window.tabs.setCurrentIndex(1)
            self.window.webcam_device.setText('/dev/video10')
            self.click(self.window.webcam_button)
            self.until(lambda: cameras and len(cameras[0].frames) >= 2)
            self.assertEqual(cameras[0].options['device'], '/dev/video10')
            self.assertFalse(self.window.webcam_device.isEnabled())
            self.click(self.window.webcam_button)
            self.until(lambda: self.window.webcam.finished.is_set() and self.window.webcam_device.isEnabled())
            self.assertTrue(cameras[0].closed)
            self.click(self.window.webcam_button)
            self.until(lambda: len(cameras) == 2)
            self.window.apply_settings()
            self.until(lambda: cameras[1].closed and not self.window.restart_pending)
            self.until(lambda: self.window.latest is not None and self.window.webcam_button.isEnabled())
            self.click(self.window.webcam_button)
            self.until(lambda: len(cameras) == 3)
            self.window.disconnect_source()
            self.until(lambda: cameras[2].closed)
            self.window.connect_source()
            self.until(lambda: self.window.latest is not None and self.window.webcam_button.isEnabled())
            self.click(self.window.webcam_button)
            self.until(lambda: len(cameras) == 4)
            self.window.close()
            self.until(lambda: not self.window.isVisible())
            self.assertTrue(cameras[3].closed)

    def test_webcam_failure_keeps_viewer_running(self):
        from thermal_viewer.webcam import VirtualWebcam

        def fail(**kwargs):
            raise RuntimeError('test missing backend')

        with patch('thermal_viewer.gui.VirtualWebcam',
                   side_effect=lambda device: VirtualWebcam(device, camera_factory=fail)):
            self.window.tabs.setCurrentIndex(1)
            self.click(self.window.webcam_button)
            self.until(lambda: 'test missing backend' in self.window.webcam_status.text())
            self.assertEqual(self.window.active_mode, 'Demo')
            self.assertTrue(self.window.webcam_button.isEnabled())
            self.assertTrue(self.window.webcam_device.isEnabled())

    def set_probe_image(self, values):
        self.window.timer.stop()
        self.window.active_settings['mode'] = 'Camera'
        self.window.latest = (values, 100, 1)
        self.window.redraw()

    def test_horizontal_orientation_matches_readouts_and_recording(self):
        from thermal_viewer.rendering import recording_image
        values = np.tile(np.arange(32, dtype=float), (24, 1))
        self.set_probe_image(values)
        self.window.palette.setCurrentText('gray')
        canvas = self.window.canvas
        rect = canvas.image_rect()
        left = QtCore.QPointF(rect.left()+rect.width()/64, rect.top()+rect.height()/48)
        right = QtCore.QPointF(rect.right()-rect.width()/64, left.y())
        self.assertEqual(canvas.pixel_at(left), (31, 0))
        self.assertEqual(canvas.pixel_at(right), (0, 0))
        np.testing.assert_array_equal(canvas.rgb[0, 0], [255, 255, 255])
        np.testing.assert_array_equal(canvas.rgb[0, -1], [0, 0, 0])
        QtTest.QTest.mouseClick(canvas, QtCore.Qt.MouseButton.LeftButton, pos=left.toPoint())
        self.assertEqual(canvas.points, [(31, 0)])
        self.assertIn('31.00 °C', self.window.point_readout.text())
        image, _ = recording_image(values, 'gray', None, 'synthetic orientation check')
        self.assertEqual(image.getpixel((0, 0)), (255, 255, 255))
        self.assertEqual(image.getpixel((639, 0)), (0, 0, 0))
        np.testing.assert_array_equal(canvas.values, values)

    def test_hover_and_pinned_temperature_follow_frames(self):
        values = np.arange(768, dtype=float)
        self.set_probe_image(values)
        canvas = self.window.canvas
        position = self.pixel_center(5, 7)
        QtTest.QTest.mouseMove(canvas, position)
        self.assertEqual(canvas.hover, (5, 7))
        self.assertIn('229.00 °C', self.window.hover_readout.text())
        QtTest.QTest.mouseClick(canvas, QtCore.Qt.MouseButton.LeftButton, pos=position)
        self.assertEqual(canvas.points, [(5, 7)])
        self.assertIn('P1 (5, 7): 229.00 °C', self.window.point_readout.text())
        self.window.latest = (values+10, 200, 2)
        self.window.redraw()
        self.assertIn('239.00 °C', self.window.hover_readout.text())
        self.assertIn('239.00 °C', self.window.point_readout.text())
        QtTest.QTest.mouseMove(canvas, QtCore.QPoint(1, 1))
        self.assertIsNone(canvas.hover)
        self.assertIn('239.00 °C', self.window.point_readout.text())
        self.click(self.window.reset_points_button)
        self.assertEqual(canvas.points, [])
        self.assertFalse(self.window.reset_points_button.isEnabled())

    def test_five_distinct_points_edges_and_resize(self):
        self.set_probe_image(np.arange(768, dtype=float))
        canvas = self.window.canvas
        points = [(0,0), (31,0), (0,23), (31,23), (16,12)]
        for x, y in points:
            QtTest.QTest.mouseClick(canvas, QtCore.Qt.MouseButton.LeftButton, pos=self.pixel_center(x,y))
        self.assertEqual(canvas.points, points)
        QtTest.QTest.mouseClick(canvas, QtCore.Qt.MouseButton.LeftButton, pos=self.pixel_center(5,5))
        QtTest.QTest.mouseClick(canvas, QtCore.Qt.MouseButton.LeftButton, pos=self.pixel_center(0,0))
        self.assertEqual(canvas.points, points)
        self.assertIn('P5 (16, 12): 400.00 °C', self.window.point_readout.text())
        self.window.resize(1350,900)
        self.app.processEvents()
        self.assertEqual(canvas.pixel_at(QtCore.QPointF(self.pixel_center(31,23))), (31,23))
        self.assertEqual(canvas.points, points)
        canvas.clear_frame()
        QtTest.QTest.mouseClick(canvas, QtCore.Qt.MouseButton.LeftButton, pos=self.pixel_center(5,5))
        self.assertEqual(canvas.points, [])

    def test_margins_and_invalid_pixels(self):
        values = np.arange(768, dtype=float)
        values[0] = np.nan
        self.set_probe_image(values)
        canvas = self.window.canvas
        for position in (QtCore.QPoint(1,1), QtCore.QPoint(canvas.width()-65, canvas.height()//2)):
            QtTest.QTest.mouseClick(canvas, QtCore.Qt.MouseButton.LeftButton, pos=position)
            self.assertIsNone(canvas.pixel_at(QtCore.QPointF(position)))
        self.assertEqual(canvas.points, [])
        QtTest.QTest.mouseClick(canvas, QtCore.Qt.MouseButton.LeftButton, pos=self.pixel_center(0,0))
        self.assertIn('unavailable', self.window.point_readout.text())

    @unittest.skipUnless(importlib.util.find_spec('imageio_ffmpeg'), 'MP4 encoder not installed')
    def test_record_stop_export_buttons(self):
        with tempfile.TemporaryDirectory() as directory:
            self.window.tabs.setCurrentIndex(2)
            self.window.folder.setText(directory)
            self.window.interval.setValue(0.1)
            self.click(self.window.record_button)
            self.until(lambda: self.window.recorder is not None and self.window.recorder.count >= 3)
            self.assertFalse(self.window.apply_button.isEnabled())
            self.click(self.window.record_button)
            self.until(lambda: self.window.recorder.finished and self.window.export_button.isEnabled())
            self.assertTrue(self.window.apply_button.isEnabled())
            self.click(self.window.export_button)
            self.until(lambda: self.window.export_thread is not None and not self.window.export_thread.is_alive())
            self.assertTrue((self.window.last_recording/'timelapse-24fps.mp4').exists())
            self.assertTrue((self.window.last_recording/'summary.json').exists())
