import os
import sys
import time
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
from PySide6.QtWidgets import QApplication

from app.config import CONFIG_FILE
from app.ui.main_window import MainWindow
from app.audio.capture import Segmenter, TARGET_SR


def main():
    fixtures = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
    track = np.load(fixtures / "video_track.npy")
    long_track = np.concatenate([track] * 3)

    app = QApplication([])
    w = MainWindow()
    w.show()
    w.start_pipeline()
    w.capture_thread.error_occurred.disconnect()
    w.capture_thread.stop()
    w.capture_thread.wait(2000)

    seg = Segmenter()
    chunk = 480
    done = [0]
    errors = []
    start_times = {}

    def observer(src, dst, engine, det, err):
        if err:
            errors.append(err)
        done[0] += 1
        print(f"DEEP caption {done[0]}: [{det}] {dst[:40]}", flush=True)

    w.translate_thread.result_ready.connect(observer)

    def feeder():
        for i in range(0, len(long_track), chunk):
            got = seg.feed(long_track[i:i + chunk].astype(np.float32))
            if got is not None:
                start_times[len(start_times)] = time.time()
                while w.asr_thread.queue_in.qsize() >= 2 and time.time() < t0 + 800:
                    time.sleep(1.0)
                w.asr_thread.submit(got)
                print(f"DEEP segment {len(start_times)} submitted ({len(got)/TARGET_SR:.1f}s)", flush=True)
            time.sleep(0.05)

    t0 = time.time()
    threading.Thread(target=feeder, daemon=True).start()

    deadline = t0 + 1500
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.2)
        if done[0] >= 8 and time.time() - t0 > 60:
            break

    w.overlay_check.setChecked(True)
    overlay_ok = w.overlay.isVisible()
    config_ok = CONFIG_FILE.exists()
    w.stop_pipeline()

    print(f"DEEP: captions={done[0]} (expected>=8), errors={len(errors)}, overlay={overlay_ok}, config_saved={config_ok}")
    print(f"DEEP: total wall time {time.time() - t0:.0f}s")
    if done[0] >= 8 and not errors and overlay_ok and config_ok:
        print("DEEP PASS")
        sys.exit(0)
    print("DEEP FAIL")
    sys.exit(1)


if __name__ == "__main__":
    main()
