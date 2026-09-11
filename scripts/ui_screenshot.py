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

# 设置窗口各页
w._open_settings()
dlg = getattr(w, "_settings_dlg", None)
for i, name in enumerate(["ui_settings_audio", "ui_settings_asr",
                          "ui_settings_translate", "ui_settings_display",
                          "ui_settings_general"]):
    dlg.nav.setCurrentRow(i)
    app.processEvents()
    dlg.grab().save(str(out / f"{name}.png"))

# Argos 分组展开（翻译页）
dlg.nav.setCurrentRow(2)
for k in range(dlg.pages.count()):
    pass
app.processEvents()
dlg.grab().save(str(out / "ui_settings_translate.png"))

# 字幕列表状态（模拟 3 条卡片）
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
# v2.4.0 面板：成对历史行（原文灰 + 译文白）
for src, tgt in [
    ("Welcome back to the channel.", "大家好，欢迎回到频道。"),
    ("The browser captures audio from your system.", "浏览器从您的系统捕获音频。"),
    ("These aren't isolated findings.", "这些并非孤立的发现。"),
]:
    ov.show_caption(src, tgt, True)
ov.apply_style(font_size=24, text_color="#ffffff", bg_color="#1c1f26", bg_opacity=92)
ov.show()
app.processEvents()
ov.grab().save(str(out / "ui_overlay.png"))
ov2 = CaptionOverlay()
ov2.show_caption("Styled overlay", "自定义配色面板", True)
ov2.apply_style(font_size=16, text_color="#7dffce", bg_color="#2d1b3d", bg_opacity=80)
ov2.show()
ov2.move(0, 300)
app.processEvents()
ov2.grab().save(str(out / "ui_overlay2.png"))

print("saved:", [p.name for p in sorted(out.glob("ui_*.png"))])
