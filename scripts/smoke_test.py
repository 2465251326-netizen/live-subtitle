import os
import sys
import time
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtWidgets import QApplication

from app.ui.main_window import MainWindow
from app.audio.capture import Segmenter, TARGET_SR


def build_track():
    """加载预生成的模拟英语视频音轨(含中文插播, 见 tests/fixtures)."""
    p = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "video_track.npy"
    if p.exists():
        return np.load(p)
    return None


def main():
    from app.config import ensure_hf_endpoint_ready
    ensure_hf_endpoint_ready(6.0)
    from faster_whisper import WhisperModel

    app = QApplication([])
    w = MainWindow()
    w.show()
    w.start_pipeline()
    w.capture_thread.error_occurred.disconnect()
    w.capture_thread.stop()
    w.capture_thread.wait(2000)

    track = build_track()
    if track is None:
        print("SMOKE: 未找到预生成音轨, 仅测试管线启停")
        w.stop_pipeline()
        print("SMOKE PASS (pipeline start/stop)")
        sys.exit(0)

    seg = Segmenter()
    chunk = 480
    asr_times = []
    done = [0]
    w.translate_thread.result_ready.connect(
        lambda s, d, e, l, err: print(
            f"SMOKE caption {done[0] + 1}: [{l}] {e}: {s[:40]} -> {d[:40]}",
            flush=True) or done.__setitem__(0, done[0] + 1)
    )

    def feeder():
        for i in range(0, len(track), chunk):
            got = seg.feed(track[i:i + chunk].astype(np.float32))
            if got is not None:
                asr_times.append(time.time())
                w.asr_thread.submit(got)
            time.sleep(0.004)

    threading.Thread(target=feeder, daemon=True).start()

    expect = 3
    deadline = time.time() + 240
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.1)
        if done[0] >= expect and time.time() > deadline - 235:
            break

    print(f"SMOKE: captions={done[0]} expected>={expect}")
    w.overlay_check.setChecked(True)
    overlay_ok = w.overlay.isVisible() and w.overlay.target_label.text() != ""
    w.stop_pipeline()
    if done[0] >= expect and overlay_ok:
        print(f"SMOKE PASS (captions={done[0]}, overlay OK)")
        sys.exit(0)
    print(f"SMOKE FAIL (captions={done[0]}, overlay={overlay_ok})")
    sys.exit(1)


if __name__ == "__main__":
    main()
