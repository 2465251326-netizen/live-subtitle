# -*- coding: utf-8 -*-
"""程序化文字裁剪探测：主窗口 1152x792 下逐控件比对 文字所需尺寸 vs 实际尺寸。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import (
    QApplication, QLabel, QPushButton, QCheckBox, QComboBox, QSpinBox,
    QScrollArea, QToolButton,
)
from PySide6.QtCore import Qt

app = QApplication(sys.argv)
from app.ui.main_window import MainWindow
w = MainWindow()
w.resize(1152, 792)
w.show()
app.processEvents()

issues = []

def widget_visible(wdg):
    p = wdg
    while p is not None:
        if not p.isVisible():
            return False
        p = p.parentWidget()
    return True

def walk(widget):
    children = list(widget.findChildren(QLabel)) + list(widget.findChildren(QPushButton)) \
        + list(widget.findChildren(QCheckBox))
    for child in children:
        if not widget_visible(child) or not child.text():
            continue
        fm = child.fontMetrics()
        need_w = fm.horizontalAdvance(child.text())
        need_h = fm.height()
        have_w = child.width()
        have_h = child.height()
        wrap = isinstance(child, QLabel) and bool(child.wordWrap())
        singleline = "\n" not in child.text()
        if singleline and not wrap and need_w > have_w + 1:
            issues.append((type(child).__name__, child.text()[:24],
                           f"need_w={need_w} have_w={have_w}"))
        if wrap or not singleline:
            # v2.2.13 盲区补强：word-wrap/多行标签必须按"当前宽度换行后的
            # 需要高度"比对——此前只比单行高度，漏掉"换行了但行高未上传父
            # 布局"的裁字类（速览卡热键行被提示行压住：need=36 have=20 仍报 0）
            need_h2 = fm.boundingRect(0, 0, have_w, 10000,
                                      Qt.TextWordWrap, child.text()).height()
            if need_h2 > have_h + 1:
                issues.append((type(child).__name__, child.text()[:24],
                               f"wrap need_h={need_h2} have_h={have_h} (w={have_w})"))
        elif need_h > have_h + 1:
            issues.append((type(child).__name__, child.text()[:24],
                           f"need_h={need_h} have_h={have_h}"))

walk(w)
walk(w.overlay) if w.overlay.isVisible() else None

for cls, text, detail in issues:
    print(f"CLIP  [{cls}] {text!r}  {detail}")
plat = os.environ.get("QT_QPA_PLATFORM", "")
if plat == "offscreen":
    print("WARN: offscreen 无字体，测量不可靠——请用 QT_QPA_PLATFORM=windows 运行本探测")
print(f"TOTAL CLIPPED: {len(issues)}")
