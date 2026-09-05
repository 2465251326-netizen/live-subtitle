import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["QT_QPA_PLATFORM"] = "offscreen"
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PySide6.QtWidgets import QApplication

from app.ui.main_window import MainWindow
from app.ui.caption_overlay import CaptionOverlay

out = Path("/tmp/opencode")
out.mkdir(parents=True, exist_ok=True)

app = QApplication([])
w = MainWindow()
w.resize(1150, 760)
w.show()
app.processEvents()
w.grab().save(str(out / "ui_empty.png"))

# Argos 分组展开
idx = w.engine_combo.findData("argos")
w.engine_combo.setCurrentIndex(idx)
app.processEvents()
w.grab().save(str(out / "ui_argos.png"))

# 字幕列表状态（模拟 3 条卡片）
w.engine_combo.setCurrentIndex(max(0, w.engine_combo.findData("auto")))
w.stack.setCurrentIndex(1)
for src, tgt, det, eng in [
    ("Welcome back to the channel.", "大家好，欢迎回到频道", "en", "google"),
    ("The browser captures audio from your system.", "浏览器从您的系统中捕获音频流", "en", "mymemory"),
    ("今天我们讨论一下实时字幕的处理方式。", "今天我们讨论一下实时字幕的处理方式。", "zh", "argos"),
]:
    w._on_translated(src, tgt, eng, det, None)
app.processEvents()
w.grab().save(str(out / "ui_captions.png"))

ov = CaptionOverlay()
ov.show_caption("The quick brown fox jumps over the lazy dog.", "敏捷的棕色狐狸跳过了懒狗。", True)
ov.show()
app.processEvents()
ov.grab().save(str(out / "ui_overlay.png"))

print("saved:", [p.name for p in sorted(out.glob("ui_*.png"))])
