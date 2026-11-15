from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

from thermal_viewer.timelapse import Timelapse, export_mp4


def context(now):
    return dict(source='camera', units='celsius', received_monotonic=now,
                received_utc=datetime.now(timezone.utc).isoformat(), eeprom_words=[1]*832,
                subpages=(bytes(1690), bytes([1])*1690), packet_errors=0, sequence_gaps=0)


class TimelapseTests(unittest.TestCase):
    def finish(self, recorder):
        recorder.stop()
        recorder.thread.join(5)
        self.assertTrue(recorder.finished)
        self.assertIsNone(recorder.error)

    def test_schedule_no_duplicates_and_original_data(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = Timelapse(directory, interval=1)
            values = np.linspace(15, 35, 768)
            for index, now in enumerate((0, 0.5, 1, 3.4)):
                recorder.submit((values, int(now*1000), index), context(now))
            # The same image must not be saved again just because time advances.
            recorder.submit((values, 3400, 3), context(5))
            self.finish(recorder)
            self.assertEqual(recorder.count, 3)
            self.assertEqual(recorder.skipped, 1)
            manifest = [json.loads(line) for line in (recorder.path/'frames.jsonl').read_text().splitlines()]
            self.assertEqual([record['elapsed_seconds'] for record in manifest], [0, 1, 3.4])
            with np.load(recorder.path/'data/000000.npz', allow_pickle=False) as npz:
                np.testing.assert_allclose(npz['values'], values.reshape(24,32), rtol=1e-6)
                self.assertEqual(npz['subpages'].shape, (2,1690))
                self.assertEqual(int(npz['subpages'][1,0]), 1)
            with Image.open(recorder.path/'frames/000000.png') as png:
                self.assertEqual(png.size, (640,512))
            self.assertEqual(json.loads((recorder.path/'session.json').read_text())['eeprom_words'], [1]*832)
            self.assertEqual(json.loads((recorder.path/'summary.json').read_text())['saved_frames'], 3)
            recorder.submit((values, 6000, 9), context(6))
            self.assertEqual(recorder.count, 3)

    def test_disk_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = Timelapse(directory)
            with patch.object(recorder, '_write_frame', side_effect=OSError('disk full')):
                recorder.submit(([20]*768, 1, 1), context(0))
                recorder.thread.join(3)
            self.assertTrue(recorder.finished)
            self.assertIn('disk full', recorder.error)
            self.assertEqual(recorder.count, 0)
            self.assertGreaterEqual(recorder.skipped, 1)

    @unittest.skipUnless(importlib.util.find_spec('imageio_ffmpeg'), 'MP4 encoder not installed')
    def test_mp4_export_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = Timelapse(directory, interval=1)
            for index in range(3):
                recorder.submit(([20+index]*768, index*1000, index), context(index))
            self.finish(recorder)
            output = export_mp4(recorder.path, 12)
            self.assertGreater(output.stat().st_size, 1000)
            with self.assertRaises(FileExistsError):
                export_mp4(recorder.path, 12)

    def test_slow_disk_keeps_queue_bounded_and_reports_drops(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = Timelapse(directory, interval=1)
            started, release = threading.Event(), threading.Event()
            original = recorder._write_frame
            def slow_write(item, manifest):
                started.set()
                release.wait(3)
                original(item, manifest)
            with patch.object(recorder, '_write_frame', side_effect=slow_write):
                recorder.submit(([20]*768, 0, 0), context(0))
                self.assertTrue(started.wait(2))
                for i in range(1, 21):
                    recorder.submit(([20]*768, i*1000, i), context(i))
                self.assertLessEqual(recorder.pending.qsize(), 16)
                self.assertEqual(recorder.skipped, 4)
                recorder.stop()
                release.set()
                recorder.thread.join(5)
                self.assertTrue(recorder.finished)
                self.assertEqual(recorder.count, 17)

    def test_invalid_interval(self):
        for interval in (0, -1, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                Timelapse('/tmp', interval=interval)
