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
        if singleline and need_h > have_h + 1:
            issues.append((type(child).__name__, child.text()[:24],
                           f"need_h={need_h} have_h={have_h}"))

walk(w)
walk(w.overlay) if w.overlay.isVisible() else None

for cls, text, detail in issues:
    print(f"CLIP  [{cls}] {text!r}  {detail}")
print(f"TOTAL CLIPPED: {len(issues)}")
