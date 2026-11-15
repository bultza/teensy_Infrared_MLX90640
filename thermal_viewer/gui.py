"""Qt desktop viewer with safe acquisition controls and asynchronous timelapse."""
from datetime import datetime, timezone
from pathlib import Path
import queue
import threading
import time

import numpy as np
from matplotlib import colormaps
from PySide6 import QtCore, QtGui, QtWidgets

from .receiver import run_serial
from .rendering import PALETTES, colorize
from .settings import configuration_payload
from .timelapse import Timelapse, export_mp4

STYLE = '''
QWidget { background:#10151f; color:#e5eaf3; font-family:"DejaVu Sans"; font-size:12px; }
QMainWindow { background:#10151f; }
QLabel#heading { font-size:25px; font-weight:700; }
QLabel#subheading { color:#8d9bb0; }
QLabel#stats { background:#182130; border:1px solid #2b374b; border-radius:8px; padding:13px; font-size:16px; }
QGroupBox { border:1px solid #2b374b; border-radius:8px; margin-top:16px; padding:12px 10px 8px; font-weight:600; }
QGroupBox::title { subcontrol-origin:margin; left:12px; padding:0 5px; color:#a5b3c7; }
QPushButton { background:#263348; border:1px solid #36475f; border-radius:5px; padding:8px; }
QPushButton:hover { background:#34465e; }
QPushButton#primary { background:#e3a33c; color:#111827; font-weight:700; border:0; }
QPushButton:disabled { color:#667388; background:#1b2534; }
QComboBox, QDoubleSpinBox, QSpinBox, QLineEdit { background:#182130; border:1px solid #354258; border-radius:4px; padding:5px; min-height:19px; }
QComboBox QAbstractItemView { background:#182130; selection-background-color:#384961; }
QScrollArea { border:0; }
QTabWidget::pane { border:0; }
QTabBar::tab { background:#182130; color:#9cabc1; padding:9px 13px; border-bottom:2px solid #263348; }
QTabBar::tab:selected { color:#f0b95b; border-bottom:2px solid #e3a33c; }
QCheckBox { spacing:7px; }
QToolTip { background:#263348; color:white; border:1px solid #6a7d96; }
'''


class ThermalCanvas(QtWidgets.QWidget):
    readoutsChanged = QtCore.Signal()
    MAX_POINTS = 5

    def __init__(self):
        super().__init__()
        self.setMinimumSize(480, 390)
        self.rgb = None
        self.scale = (0, 1)
        self.palette = 'inferno'
        self.units = '°C'
        self.values = None
        self.points = []
        self.hover = None
        self.mouse_position = None
        self.setMouseTracking(True)
        self.setCursor(QtCore.Qt.CursorShape.CrossCursor)

    def image_rect(self):
        # Share exactly the same aspect-ratio geometry between drawing and hit testing.
        available = QtCore.QRectF(18, 18, self.width()-105, self.height()-36)
        width = min(available.width(), available.height()*4/3)
        height = width*3/4
        return QtCore.QRectF(available.x()+(available.width()-width)/2,
                             available.y()+(available.height()-height)/2, width, height)

    def pixel_at(self, position):
        rect = self.image_rect()
        if self.rgb is None or self.values is None:
            return None
        # Right/bottom edges are exclusive; margins and the color bar are not sensor pixels.
        if not (rect.left() <= position.x() < rect.right() and rect.top() <= position.y() < rect.bottom()):
            return None
        return (int((position.x()-rect.left())*32/rect.width()),
                int((position.y()-rect.top())*24/rect.height()))

    def pixel_text(self, point):
        x, y = point
        if self.values is None:
            return f'({x}, {y}): waiting'
        value = self.values[y, x]
        if not np.isfinite(value):
            return f'({x}, {y}): unavailable'
        return f'({x}, {y}): {value:.2f} {self.units}'

    def set_values(self, values, units):
        self.values = np.asarray(values, dtype=float).reshape(24, 32).copy()
        self.units = units
        if self.mouse_position is not None:
            self.hover = self.pixel_at(self.mouse_position)
        self.readoutsChanged.emit()
        self.update()

    def reset_points(self):
        self.points.clear()
        self.readoutsChanged.emit()
        self.update()

    def clear_frame(self):
        # A restarted source must not inherit probe readings or locations from the previous source.
        self.rgb = self.values = None
        self.hover = self.mouse_position = None
        self.reset_points()

    def mouseMoveEvent(self, event):
        self.mouse_position = event.position()
        self.hover = self.pixel_at(self.mouse_position)
        self.readoutsChanged.emit()

    def leaveEvent(self, event):
        self.hover = self.mouse_position = None
        self.readoutsChanged.emit()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            point = self.pixel_at(event.position())
            if point is not None and point not in self.points and len(self.points) < self.MAX_POINTS:
                self.points.append(point)
                self.readoutsChanged.emit()
                self.update()
        super().mousePressEvent(event)

    def resizeEvent(self, event):
        self.hover = None
        self.mouse_position = None
        self.readoutsChanged.emit()
        super().resizeEvent(event)

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), QtGui.QColor('#080d15'))
        if self.rgb is None:
            painter.setPen(QtGui.QColor('#8290a6'))
            painter.drawText(self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, 'Connect a camera or start the demo')
            return
        height, width, _ = self.rgb.shape
        image = QtGui.QImage(self.rgb.data, width, height, self.rgb.strides[0], QtGui.QImage.Format.Format_RGB888).copy()
        rect = self.image_rect()
        painter.drawImage(rect, image)
        # Draw numbered white crosses at sensor-cell centers, with a dark halo for contrast.
        for index, (x, y) in enumerate(self.points, 1):
            center = QtCore.QPointF(rect.left()+(x+0.5)*rect.width()/32,
                                   rect.top()+(y+0.5)*rect.height()/24)
            radius = 6
            for color, thickness in (('#080d15', 4), ('white', 1.5)):
                painter.setPen(QtGui.QPen(QtGui.QColor(color), thickness))
                painter.drawLine(center+QtCore.QPointF(-radius, 0), center+QtCore.QPointF(radius, 0))
                painter.drawLine(center+QtCore.QPointF(0, -radius), center+QtCore.QPointF(0, radius))
            text_x = center.x()+8 if x < 30 else center.x()-18
            text_y = center.y()-8 if y > 0 else center.y()+19
            painter.setPen(QtGui.QColor('#080d15'))
            painter.drawText(QtCore.QPointF(text_x+1, text_y+1), str(index))
            painter.setPen(QtGui.QColor('white'))
            painter.drawText(QtCore.QPointF(text_x, text_y), str(index))
        gradient = QtGui.QLinearGradient(0, rect.bottom(), 0, rect.top())
        cmap = colormaps[self.palette]
        for point in np.linspace(0, 1, 20):
            r, g, b, _ = cmap(point)
            gradient.setColorAt(float(point), QtGui.QColor.fromRgbF(r, g, b))
        bar = QtCore.QRectF(self.width()-72, rect.top(), 13, rect.height())
        painter.fillRect(bar, gradient)
        painter.setPen(QtGui.QColor('#c3cede'))
        for fraction in (0, 0.5, 1):
            value = self.scale[1] - fraction*(self.scale[1]-self.scale[0])
            painter.drawText(QtCore.QRectF(bar.right()+5, bar.top()+fraction*(bar.height()-18), 53, 20), f'{value:.1f}')
        painter.drawText(QtCore.QRectF(bar.left()-5, rect.bottom()+5, 65, 22), self.units)


class Viewer(QtWidgets.QMainWindow):
    def __init__(self, args):
        super().__init__()
        self.args = args
        self.setWindowTitle('MLX90640 · Thermal Studio')
        self.resize(1210, 820)
        self.setMinimumSize(1000, 720)
        self.stop_event = threading.Event()
        self.worker = None
        self.images, self.messages = queue.Queue(1), queue.Queue(1)
        self.recorder = None
        self.last_recording = None
        self.export_thread = None
        self.export_messages = queue.Queue()
        self.restart_pending = self.closing = False
        self.latest = None
        self.active_mode = None
        self.last_image_at = None
        self.active_settings = None
        self.demo_start = 0
        self.demo_pair = 0
        root = QtWidgets.QWidget()
        self.setCentralWidget(root)
        layout = QtWidgets.QHBoxLayout(root)
        layout.setContentsMargins(24, 22, 22, 20)
        layout.setSpacing(22)
        left = QtWidgets.QVBoxLayout()
        heading = QtWidgets.QLabel('THERMAL STUDIO')
        heading.setObjectName('heading')
        left.addWidget(heading)
        subtitle = QtWidgets.QLabel('MLX90640  /  32 × 24 radiometric imaging')
        subtitle.setObjectName('subheading')
        left.addWidget(subtitle)
        self.stats = QtWidgets.QLabel('MIN  —             MAX  —             MEAN  —')
        self.stats.setObjectName('stats')
        left.addWidget(self.stats)
        self.canvas = ThermalCanvas()
        left.addWidget(self.canvas, 1)
        probes = QtWidgets.QHBoxLayout()
        readings = QtWidgets.QVBoxLayout()
        self.hover_readout = QtWidgets.QLabel('Hover over the image to inspect a pixel')
        self.point_readout = QtWidgets.QLabel('Click up to 5 pixels to track their temperature')
        self.point_readout.setWordWrap(True)
        self.point_readout.setMinimumHeight(36)
        readings.addWidget(self.hover_readout)
        readings.addWidget(self.point_readout)
        probes.addLayout(readings, 1)
        self.reset_points_button = QtWidgets.QPushButton('Reset points')
        self.reset_points_button.setEnabled(False)
        self.reset_points_button.clicked.connect(self.canvas.reset_points)
        probes.addWidget(self.reset_points_button)
        left.addLayout(probes)
        self.canvas.readoutsChanged.connect(self.update_pixel_readouts)
        self.status = QtWidgets.QLabel('Ready · select a source and connect')
        self.status.setWordWrap(True)
        self.status.setMinimumHeight(42)
        left.addWidget(self.status)
        layout.addLayout(left, 1)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFixedWidth(330)
        sidebar = QtWidgets.QWidget()
        self.side_layout = QtWidgets.QVBoxLayout(sidebar)
        self.side_layout.setContentsMargins(0, 0, 8, 0)
        scroll.setWidget(sidebar)
        layout.addWidget(scroll)
        self.panel_layout = self.side_layout
        self._connection_controls()
        self.tabs = QtWidgets.QTabWidget()
        self.side_layout.addWidget(self.tabs, 1)
        for title, builder in (('Camera', self._camera_controls), ('Display', self._display_controls), ('Timelapse', self._recording_controls)):
            panel = QtWidgets.QWidget()
            self.panel_layout = QtWidgets.QVBoxLayout(panel)
            self.panel_layout.setContentsMargins(0, 4, 0, 0)
            builder()
            self.panel_layout.addStretch()
            self.tabs.addTab(panel, title)
        self.panel_layout = self.side_layout
        self.refresh_ports()
        if args.port:
            self.port.setCurrentText(args.port)
        self.mode.setCurrentText('Demo' if args.demo else 'Test pattern' if args.test_pattern else 'Camera')
        self.clock.setCurrentText(str(args.i2c_khz))
        self.fps.setCurrentText(f'{args.fps:g}')
        self.emissivity.setValue(args.emissivity)
        self.reflected_auto.setChecked(args.reflected_temperature is None)
        if args.reflected_temperature is not None:
            self.reflected.setValue(args.reflected_temperature)
        index = {(400, 2): 0, (100, 1): 1, (100, 0.5): 2}.get((args.i2c_khz, args.fps), 3)
        self.profile.setCurrentIndex(index)
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(50)
        self.setStyleSheet(STYLE)
        if args.port or args.demo:
            QtCore.QTimer.singleShot(0, self.connect_source)

    def group(self, title):
        group = QtWidgets.QGroupBox(title)
        form = QtWidgets.QFormLayout(group)
        form.setSpacing(8)
        self.panel_layout.addWidget(group)
        return form

    def combo(self, items):
        combo = QtWidgets.QComboBox()
        combo.addItems(items)
        return combo

    def spin(self, low, high, value, decimals=2):
        spin = QtWidgets.QDoubleSpinBox()
        spin.setDecimals(decimals)
        spin.setRange(low, high)
        spin.setValue(value)
        return spin

    def _connection_controls(self):
        form = self.group('CONNECTION')
        self.port = QtWidgets.QComboBox()
        self.port.setEditable(True)
        refresh = QtWidgets.QPushButton('Refresh ports')
        refresh.clicked.connect(self.refresh_ports)
        self.mode = self.combo(['Camera', 'Test pattern', 'Demo'])
        self.connect_button = QtWidgets.QPushButton('Connect')
        self.connect_button.setObjectName('primary')
        self.connect_button.clicked.connect(self.toggle_connection)
        form.addRow('USB port', self.port)
        form.addRow('', refresh)
        form.addRow('Source', self.mode)
        form.addRow(self.connect_button)

    def _camera_controls(self):
        form = self.group('CAMERA SETTINGS')
        self.profile = self.combo(['Balanced · 2 FPS', 'Robust cable · 1 FPS', 'Slow · 0.5 FPS', 'Custom'])
        self.clock = self.combo(['400', '100'])
        self.fps = self.combo(['2', '1', '0.5', '0.25'])
        self.emissivity = self.spin(0.01, 1, 0.95)
        self.emissivity.setSingleStep(0.01)
        self.reflected_auto = QtWidgets.QCheckBox('Auto: ambient − 8°C')
        self.reflected_auto.setChecked(True)
        self.reflected = self.spin(-273, 1000, 20, 1)
        self.reflected.setEnabled(False)
        self.reflected_auto.toggled.connect(lambda checked: self.reflected.setEnabled(not checked))
        self.apply_button = QtWidgets.QPushButton('Apply & restart')
        self.apply_button.clicked.connect(self.apply_settings)
        self.profile.currentIndexChanged.connect(self.apply_profile)
        form.addRow('Preset', self.profile)
        form.addRow('I2C · kHz', self.clock)
        form.addRow('Complete FPS', self.fps)
        form.addRow('Emissivity', self.emissivity)
        form.addRow(self.reflected_auto)
        form.addRow('Reflected · °C', self.reflected)
        form.addRow(self.apply_button)
        hint = QtWidgets.QLabel('Changes apply on restart. Stop timelapse before changing acquisition settings.')
        hint.setWordWrap(True)
        hint.setObjectName('subheading')
        form.addRow(hint)

    def _display_controls(self):
        form = self.group('DISPLAY')
        self.palette = self.combo(list(PALETTES))
        self.auto_range = QtWidgets.QCheckBox('Automatic range')
        self.auto_range.setChecked(True)
        self.low = self.spin(-32768, 65535, 15, 1)
        self.high = self.spin(-32768, 65535, 40, 1)
        self.low.setEnabled(False)
        self.high.setEnabled(False)
        self.auto_range.toggled.connect(lambda value: (self.low.setEnabled(not value), self.high.setEnabled(not value), self.redraw()))
        self.palette.currentTextChanged.connect(self.redraw)
        self.low.valueChanged.connect(self.redraw)
        self.high.valueChanged.connect(self.redraw)
        form.addRow('Palette', self.palette)
        form.addRow(self.auto_range)
        form.addRow('Minimum', self.low)
        form.addRow('Maximum', self.high)

    def _recording_controls(self):
        form = self.group('TIMELAPSE')
        self.folder = QtWidgets.QLineEdit(str(Path('captures').absolute()))
        browse = QtWidgets.QPushButton('Choose folder…')
        browse.clicked.connect(self.choose_folder)
        self.interval = self.spin(0.1, 86400, 10, 1)
        self.playback = QtWidgets.QSpinBox()
        self.playback.setRange(1, 60)
        self.playback.setValue(24)
        self.record_button = QtWidgets.QPushButton('Start timelapse')
        self.record_button.setEnabled(False)
        self.record_button.clicked.connect(self.toggle_recording)
        self.export_button = QtWidgets.QPushButton('Export MP4')
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self.export_video)
        self.record_status = QtWidgets.QLabel('PNG + temperatures + original subpages')
        self.record_status.setWordWrap(True)
        form.addRow('Save under', self.folder)
        form.addRow('', browse)
        form.addRow('Interval · s', self.interval)
        form.addRow('Playback FPS', self.playback)
        form.addRow(self.record_button)
        form.addRow(self.export_button)
        form.addRow(self.record_status)

    def refresh_ports(self):
        from serial.tools.list_ports import comports
        current = self.port.currentText()
        ports = sorted(comports(), key=lambda p: p.vid != 0x16c0)
        self.port.clear()
        self.port.addItems([p.device for p in ports])
        if current:
            self.port.setCurrentText(current)

    def apply_profile(self, index):
        presets = [(400, 2), (100, 1), (100, 0.5)]
        if index < len(presets):
            clock, fps = presets[index]
            self.clock.setCurrentText(str(clock))
            self.fps.setCurrentText(f'{fps:g}')

    def selected_settings(self):
        clock, fps = int(self.clock.currentText()), float(self.fps.currentText())
        configuration_payload(clock, fps)
        if self.mode.currentText() != 'Demo' and not self.port.currentText().strip():
            raise ValueError('Select a USB port')
        return dict(mode=self.mode.currentText(), port=self.port.currentText().strip(), i2c_khz=clock, fps=fps,
                    emissivity=self.emissivity.value(),
                    reflected_temperature=None if self.reflected_auto.isChecked() else self.reflected.value())

    def connect_source(self):
        try:
            settings = self.selected_settings()
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        if self.worker is not None and self.worker.is_alive():
            return
        self.stop_event = threading.Event()
        self.images, self.messages = queue.Queue(1), queue.Queue(1)
        self.latest = None
        self.canvas.clear_frame()
        self.stats.setText('MIN  —             MAX  —             MEAN  —')
        self.canvas.update()
        self.last_image_at = None
        self.active_settings = settings
        self.active_mode = settings['mode']
        self.connect_button.setText('Disconnect')
        self.status.setText('Connecting…' if self.active_mode != 'Demo' else 'Demo · synthetic data, not °C')
        if self.active_mode == 'Demo':
            self.demo_start = time.monotonic()
            self.demo_pair = 0
        else:
            self.worker = threading.Thread(target=run_serial, kwargs=dict(
                port=settings['port'], stop=self.stop_event, images=self.images, messages=self.messages,
                real=self.active_mode == 'Camera', emissivity=settings['emissivity'],
                reflected_temperature=settings['reflected_temperature'], i2c_khz=settings['i2c_khz'],
                fps=settings['fps'], frame_sink=self.on_frame), daemon=True)
            self.worker.start()

    def on_frame(self, image, context):
        # Runs in the acquisition thread; recording.submit never waits for disk or calls Qt.
        recorder = self.recorder
        if recorder is not None:
            recorder.submit(image, context)

    def toggle_connection(self):
        if self.active_mode is not None:
            self.disconnect_source()
        else:
            self.connect_source()

    def disconnect_source(self):
        self.stop_event.set()
        self.active_mode = None
        if self.recorder is not None:
            self.recorder.stop('window closed' if self.closing else 'disconnected')
        self.connect_button.setText('Connect')
        self.status.setText('Stopping acquisition…' if self.worker and self.worker.is_alive() else 'Disconnected')
        self.record_button.setEnabled(False)

    def apply_settings(self):
        if self.recorder and not self.recorder.finished:
            self.status.setText('Stop timelapse before applying camera settings')
            return
        try:
            self.selected_settings()
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        self.disconnect_source()
        self.restart_pending = True

    def limits(self):
        return None if self.auto_range.isChecked() else (self.low.value(), self.high.value())

    def redraw(self, *_):
        if self.latest is None:
            return
        try:
            rgb, scale = colorize(self.latest[0], self.palette.currentText(), self.limits())
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        self.canvas.rgb, self.canvas.scale = rgb, scale
        self.canvas.palette = self.palette.currentText()
        self.canvas.set_values(self.latest[0], '°C' if self.active_settings['mode'] == 'Camera' else 'counts')
        values = np.asarray(self.latest[0])
        self.stats.setText(f'MIN  {np.nanmin(values):.1f}     MAX  {np.nanmax(values):.1f}     MEAN  {np.nanmean(values):.1f}  {self.canvas.units}')
        self.canvas.update()

    def update_pixel_readouts(self):
        canvas = self.canvas
        self.hover_readout.setText('Hover · '+canvas.pixel_text(canvas.hover) if canvas.hover is not None
                                  else 'Hover over the image to inspect a pixel')
        if canvas.points:
            readouts = [f'P{i} {canvas.pixel_text(point)}' for i, point in enumerate(canvas.points, 1)]
            self.point_readout.setText('\n'.join('   ·   '.join(readouts[i:i+2]) for i in range(0, len(readouts), 2)))
        else:
            self.point_readout.setText('Click up to 5 pixels to track their temperature')
        self.reset_points_button.setEnabled(bool(canvas.points))

    def choose_folder(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, 'Timelapse folder', self.folder.text())
        if folder:
            self.folder.setText(folder)

    def toggle_recording(self):
        if self.recorder and not self.recorder.finished:
            self.recorder.stop()
            self.record_button.setEnabled(False)
            self.record_status.setText('Finishing queued frames…')
            return
        if self.latest is None or self.active_mode is None:
            return
        try:
            if self.limits() is not None and self.low.value() >= self.high.value():
                raise ValueError('Minimum must be lower than maximum')
            self.recorder = Timelapse(self.folder.text(), self.interval.value(), self.palette.currentText(), self.limits())
        except (OSError, ValueError) as exc:
            self.record_status.setText(str(exc))
            return
        self.last_recording = self.recorder.path
        self.record_button.setText('Stop timelapse')
        self.export_button.setEnabled(False)
        self.lock_recording_controls(True)

    def lock_recording_controls(self, locked):
        for widget in (self.apply_button, self.mode, self.port, self.profile, self.clock, self.fps,
                       self.emissivity, self.reflected_auto, self.palette, self.auto_range,
                       self.folder, self.interval):
            widget.setEnabled(not locked)
        self.reflected.setEnabled(not locked and not self.reflected_auto.isChecked())
        self.low.setEnabled(not locked and not self.auto_range.isChecked())
        self.high.setEnabled(not locked and not self.auto_range.isChecked())

    def export_video(self):
        if self.last_recording is None or (self.recorder and not self.recorder.finished):
            return
        if self.export_thread and self.export_thread.is_alive():
            return
        session, fps = self.last_recording, self.playback.value()
        self.export_button.setEnabled(False)
        self.record_status.setText('Exporting MP4…')
        def export():
            try:
                self.export_messages.put(f'Saved video: {export_mp4(session, fps)}')
            except Exception as exc:
                self.export_messages.put(f'Export failed: {exc}')
        self.export_thread = threading.Thread(target=export, daemon=True)
        self.export_thread.start()

    def tick(self):
        if self.active_mode == 'Demo':
            now = time.monotonic()
            if self.last_image_at is None or now-self.last_image_at >= 0.1:
                y, x = np.mgrid[:24, :32]
                t = now-self.demo_start
                pixels = (1000+5000*np.exp(-((x-16-8*np.sin(t))**2+(y-12)**2)/25)).ravel()
                self.latest = (pixels, int(t*1000), self.demo_pair)
                self.demo_pair += 1
                self.last_image_at = now
                self.on_frame(self.latest, dict(source='demo', units='synthetic counts', received_monotonic=now,
                                               received_utc=datetime.now(timezone.utc).isoformat(), subpages=()))
                self.redraw()
        else:
            try:
                self.latest = self.images.get_nowait()
                self.last_image_at = time.monotonic()
                self.redraw()
            except queue.Empty:
                pass
        try:
            self.status.setText(self.messages.get_nowait())
        except queue.Empty:
            pass
        alive = self.worker is not None and self.worker.is_alive()
        if self.active_mode not in (None, 'Demo') and not alive:
            self.active_mode = None
            self.connect_button.setText('Connect')
            if self.recorder:
                self.recorder.stop('source stopped: '+self.status.text())
        if self.restart_pending and not alive:
            self.restart_pending = False
            self.connect_source()
        recording = self.recorder is not None and not self.recorder.finished
        if recording:
            self.record_status.setText(f'{self.recorder.count} saved · {self.recorder.skipped} skipped\n{self.recorder.path.name}')
        elif self.recorder is not None:
            if self.record_button.text() != 'Start timelapse':
                self.record_button.setText('Start timelapse')
                self.lock_recording_controls(False)
                self.record_status.setText(f'{self.recorder.count} saved · {self.recorder.skipped} skipped\n'+
                                          (f'Error: {self.recorder.error}' if self.recorder.error else str(self.recorder.path)))
            self.export_button.setEnabled(self.recorder.count > 0 and not (self.export_thread and self.export_thread.is_alive()))
        self.record_button.setEnabled(self.active_mode is not None and self.latest is not None and
                                      not (recording and self.recorder.stopping.is_set()))
        try:
            self.record_status.setText(self.export_messages.get_nowait())
        except queue.Empty:
            pass
        if self.closing and not alive and not recording and not (self.export_thread and self.export_thread.is_alive()):
            self.close()

    def closeEvent(self, event):
        alive = self.worker is not None and self.worker.is_alive()
        recording = self.recorder is not None and not self.recorder.finished
        exporting = self.export_thread is not None and self.export_thread.is_alive()
        if alive or recording or exporting:
            self.closing = True
            self.restart_pending = False
            self.disconnect_source()
            event.ignore()
        else:
            self.timer.stop()
            event.accept()


def run_gui(args):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = Viewer(args)
    window.show()
    return app.exec()
