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

from app.ui.main_window import MainWindow
from app.audio.capture import Segmenter, TARGET_SR


def build_track():
    """加载预生成的模拟英语视频音轨(含中文插播, 见 tests/fixtures)."""
    p = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "video_track.npy"
    if p.exists():
        return np.load(p)
    return None


def main():
    from app.config import ensure_hf_endpoint_ready, Config
    ensure_hf_endpoint_ready(6.0)
    # 首次运行向导是模态对话框，会在 processEvents 循环里永久阻塞冒烟测试；
    # 这里预先标记为已完成，保证无人值守 CI 不被引导页卡住
    try:
        Config().set("wizard_done", True)
    except Exception:
        pass
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
        print("SMOKE PASS (pipeline start/stop)", flush=True)
        sys.stdout.flush()
        os._exit(0)

    seg = Segmenter()
    chunk = 480
    asr_times = []
    done = [0]

    def observer(src, dst, engine, det, err):
        i = done[0]
        done[0] = i + 1
        print(f"SMOKE caption {i + 1}: [{det}] {engine}: {src[:40]} -> {dst[:40]}", flush=True)

    w.translate_thread.result_ready.connect(observer)

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
    w.set_overlay_enabled(True)
    overlay_ok = w.overlay.isVisible()

    # 设置窗口实例化 + 暂存/应用状态机冒烟（建议8 回归哨兵）
    from app.ui.settings_dialog import SettingsDialog
    dlg = SettingsDialog(w)
    dlg.load_from_config()
    staged_ok = (dlg._staged == {})
    dlg._stage("max_history", 250)
    staged_ok = staged_ok and (dlg._staged.get("max_history") == 250)
    dlg._apply_staged()
    applied_ok = (dlg._staged == {}) and (w.config.get("max_history") == 250)
    dlg._stage("max_history", 200)
    dlg._apply_staged()  # 还原默认值，不污染配置
    print(f"SMOKE settings dialog: staged={staged_ok}, applied={applied_ok}")

    w.stop_pipeline()
    # v2.0.12：Qt 应用退出阶段存在 QApplication 析构与后台线程销毁竞态，
    # CI 上连 os._exit 前的 flush 都可能 abort（本地复现 0xC0000409）。
    # 断言结果先落盘（结果文件为准），CI 步骤读文件判定，退出码仅作参考
    result_txt = ("PASS" if (done[0] >= expect and overlay_ok and staged_ok and applied_ok)
                  else "FAIL")
    detail = f"captions={done[0]}, overlay={overlay_ok}, staged={staged_ok}, applied={applied_ok}"
    try:
        Path(__file__).resolve().parents[1].joinpath("smoke_result.txt").write_text(
            f"{result_txt} ({detail})\n", encoding="utf-8")
    except Exception:
        pass
    if result_txt == "PASS":
        print(f"SMOKE PASS ({detail})", flush=True)
        os._exit(0)
    print(f"SMOKE FAIL ({detail})", flush=True)
    os._exit(1)


if __name__ == "__main__":
    main()
