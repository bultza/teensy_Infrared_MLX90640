"""Bounded, asynchronous timelapse recording and MP4 export."""
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import queue
import subprocess
import threading
import uuid

import numpy as np
from .rendering import recording_image


class Timelapse:
    def __init__(self, parent, interval=10.0, palette='inferno', limits=None):
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError('Capture interval must be positive and finite')
        self.interval, self.palette, self.limits = interval, palette, limits
        name = datetime.now(timezone.utc).strftime('timelapse-%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:6]
        self.path = Path(parent).expanduser() / name
        self.path.mkdir(parents=True, exist_ok=False)
        (self.path/'frames').mkdir()
        (self.path/'data').mkdir()
        self.pending = queue.Queue(maxsize=16)
        self.stopping = threading.Event()
        self.lock = threading.Lock()
        self.count = self.skipped = 0
        self.error = None
        self.stop_reason = None
        self.next_due = None
        self.last_key = None
        self.origin = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def submit(self, image, context):
        """Select only a newly acquired image on a monotonic schedule; never wait for disk."""
        with self.lock:
            if self.stopping.is_set():
                return
            now = context['received_monotonic']
            key = (context['source'], image[1], image[2])
            if key == self.last_key:
                return
            self.last_key = key
            if self.next_due is None:
                self.next_due = self.origin = now
            if now < self.next_due:
                return
            slots = int((now - self.next_due) // self.interval) + 1
            self.next_due += slots * self.interval
            self.skipped += slots - 1
            # Snapshot mutable values before returning to acquisition.
            item = (np.asarray(image[0], dtype=np.float32).reshape(24, 32).copy(), image[1], image[2], dict(context))
            try:
                self.pending.put_nowait(item)
            except queue.Full:
                self.skipped += 1

    def stop(self, reason='user'):
        """Request an orderly drain without blocking the desktop thread."""
        with self.lock:
            if self.stop_reason is None:
                self.stop_reason = reason
            self.stopping.set()

    @property
    def finished(self):
        return not self.thread.is_alive()

    def _write_frame(self, item, manifest):
        values, stamp, pair, context = item
        number = self.count
        name = f'{number:06d}'
        if number == 0:
            metadata = {k: v for k, v in context.items() if k not in ('subpages', 'received_monotonic')}
            metadata.update(format='mlx90640-timelapse-v1', interval_seconds=self.interval,
                            palette=self.palette, limits=self.limits, created_utc=context['received_utc'])
            (self.path/'session.json').write_text(json.dumps(metadata, indent=2)+'\n')
        label = f'{context["received_utc"]} | {context["units"]}'
        image, scale = recording_image(values, self.palette, self.limits, label)
        image.save(self.path/'frames'/f'{name}.tmp', format='PNG')
        (self.path/'frames'/f'{name}.tmp').replace(self.path/'frames'/f'{name}.png')
        raw = context.get('subpages', ())
        with (self.path/'data'/f'{name}.tmp').open('wb') as file:
            np.savez_compressed(file, values=values,
                                subpages=np.array([np.frombuffer(p, dtype=np.uint8) for p in raw], dtype=np.uint8))
        (self.path/'data'/f'{name}.tmp').replace(self.path/'data'/f'{name}.npz')
        record = dict(index=number, pair_id=pair, device_millis=stamp,
                      received_utc=context['received_utc'], elapsed_seconds=context['received_monotonic']-self.origin,
                      scale=scale, skipped_slots_total=self.skipped,
                      packet_errors=context.get('packet_errors', 0), sequence_gaps=context.get('sequence_gaps', 0))
        manifest.write(json.dumps(record)+'\n')
        manifest.flush()
        self.count += 1

    def _run(self):
        try:
            with (self.path/'frames.jsonl').open('x') as manifest:
                while not self.stopping.is_set() or not self.pending.empty():
                    try:
                        item = self.pending.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    self._write_frame(item, manifest)
        except Exception as exc:
            self.skipped += 1 + self.pending.qsize()
            self.error = str(exc)
            self.stopping.set()
        finally:
            try:
                (self.path/'summary.json').write_text(json.dumps(dict(
                    saved_frames=self.count, skipped_slots=self.skipped, error=self.error,
                    stop_reason='write error' if self.error else self.stop_reason), indent=2)+'\n')
            except OSError as exc:
                self.error = str(exc)


def export_mp4(session, playback_fps=24):
    """Export a finished recording; capture interval and playback FPS are independent."""
    import imageio_ffmpeg
    if not math.isfinite(playback_fps) or not 1 <= playback_fps <= 60:
        raise ValueError('Playback FPS must be between 1 and 60')
    session = Path(session)
    if not (session/'summary.json').exists():
        raise ValueError('Stop recording and wait for finalization before export')
    records = [json.loads(line) for line in (session/'frames.jsonl').read_text().splitlines()]
    if not records:
        raise ValueError('The recording contains no saved frames')
    if [r['index'] for r in records] != list(range(len(records))):
        raise ValueError('Recording manifest is not sequential')
    if any(not (session/'frames'/f'{index:06d}.png').is_file() for index in range(len(records))):
        raise ValueError('A recorded PNG frame is missing')
    output = session/f'timelapse-{playback_fps:g}fps.mp4'
    if output.exists():
        raise FileExistsError(f'Video already exists: {output.name}')
    temporary = session/f'.export-{uuid.uuid4().hex}.mp4'
    try:
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-nostdin', '-loglevel', 'error', '-n',
                        '-framerate', str(playback_fps), '-i', str(session/'frames'/'%06d.png'),
                        '-frames:v', str(len(records)), '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                        '-movflags', '+faststart', str(temporary)], check=True, capture_output=True)
        temporary.replace(output)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(exc.stderr.decode(errors='replace')[-2000:]) from exc
    finally:
        temporary.unlink(missing_ok=True)
    return output
