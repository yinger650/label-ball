"""Selected frame order and declared 30fps must agree after decimation."""
import os
from pathlib import Path
import shutil
import sys
import tempfile
from unittest.mock import patch

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from producer.derive import decimate_clip


def test_decimated_clip_timing():
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        import imageio_ffmpeg  # Optional test-only binary, not a runtime dependency.
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root/'ffmpeg').symlink_to(ffmpeg)
        source = root/'source.mp4'
        writer = cv2.VideoWriter(str(source), cv2.VideoWriter_fourcc(*'mp4v'), 60000/1001, (64, 64))
        assert writer.isOpened()
        for i in range(600):
            frame = np.empty((64, 64, 3), np.uint8)
            frame[:] = ((17*i)%220+16, (47*i)%220+16, (79*i)%220+16)
            writer.write(frame)
        writer.release()
        with patch.dict(os.environ, {'PATH': str(root)+os.pathsep+os.environ.get('PATH', '')}):
            decimate_clip(source, root/'selected.mp4', 2)
        output = cv2.VideoCapture(str(root/'selected.mp4'))
        assert abs(output.get(cv2.CAP_PROP_FPS)-30) < .001
        assert int(output.get(cv2.CAP_PROP_FRAME_COUNT)) == 300
        original = cv2.VideoCapture(str(source))
        for selected_index in [0, 1, 73, 149, 299]:
            original.set(cv2.CAP_PROP_POS_FRAMES, selected_index*2)
            output.set(cv2.CAP_PROP_POS_FRAMES, selected_index)
            ok_a, a = original.read()
            ok_b, b = output.read()
            assert ok_a and ok_b
            error = np.abs(a.astype(float)-b.astype(float)).mean()
            assert error < 4, (selected_index, error)
        output.release()
        original.release()


if __name__ == '__main__':
    test_decimated_clip_timing()
    print('ok decimate_timing: 600 source frames -> 300 selected frames at 30fps/10s; frame order verified')
