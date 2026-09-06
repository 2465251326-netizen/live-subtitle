from PySide6.QtCore import Qt, QPointF
from PySide6.QtGui import (
    QPainter, QPainterPath, QPen, QBrush, QColor, QTextOption,
    QTextLayout,
)
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QMenu

from app.ui.styles import OVERLAY_QSS


class OutlinedLabel(QLabel):
    """支持描边的 QLabel：描边宽度 > 0 时走自绘路径，否则走原生绘制。"""

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self._outline_w = 0
        self._outline_color = QColor("#000000")
        self._text_color = QColor("#ffffff")

    def set_outline(self, width, color):
        self._outline_w = int(width)
        self._outline_color = QColor(color)
        self.update()

    def set_fill_color(self, color):
        self._text_color = QColor(color)
        self.update()

    def _paint_outline(self, painter):
        painter.setRenderHint(QPainter.Antialiasing)
        layout = QTextLayout(self.text(), self.font())
        opt = QTextOption()
        opt.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        opt.setAlignment(self.alignment())
        layout.setTextOption(opt)
        line_width = max(1.0, float(self.width()))
        layout.beginLayout()
        y = 0.0
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(line_width)
            line.setPosition(QPointF(0.0, y))
            y += line.height()
        layout.endLayout()

        for i in range(layout.lineCount()):
            line = layout.lineAt(i)
            start = line.textStart()
            length = line.textLength()
            if length <= 0:
                continue
            text = self.text()[start:start + length]
            x = line.position().x()
            if self.alignment() & Qt.AlignHCenter:
                x += (line_width - line.naturalTextWidth()) / 2.0
            y = line.position().y() + line.ascent()
            path = QPainterPath()
            path.addText(QPointF(x, y), self.font(), text)
            pen = QPen(
                self._outline_color,
                max(1.0, float(self._outline_w)),
                Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin,
            )
            painter.strokePath(path, pen)
            painter.fillPath(path, QBrush(self._text_color))
        painter.end()

    def paintEvent(self, event):
        if self._outline_w <= 0:
            super().paintEvent(event)
            return
        painter = QPainter(self)
        self._paint_outline(painter)


class CaptionOverlay(QWidget):
    def __init__(self, on_closed=None, on_moved=None):
        super().__init__(None)
        # 背景参数必须先于任何可能触发 paintEvent 的调用（setStyleSheet 等）
        self._bg_color = QColor("#0c0e14")
        self._bg_alpha = int(78 * 2.55)
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setObjectName("OverlayRoot")
        self.setStyleSheet(OVERLAY_QSS)
        self.setFixedWidth(460)
        self._drag_pos = None
        self._on_closed = on_closed
        self._on_moved = on_moved

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 12, 22, 14)
        layout.setSpacing(4)

        self.source_label = OutlinedLabel("")
        self.source_label.setObjectName("OverlaySource")
        self.source_label.setWordWrap(True)
        self.source_label.setAlignment(Qt.AlignCenter)

        self.target_label = OutlinedLabel("悬浮字幕已开启")
        self.target_label.setObjectName("OverlayTarget")
        self.target_label.setWordWrap(True)
        self.target_label.setAlignment(Qt.AlignCenter)

        layout.addWidget(self.source_label)
        layout.addWidget(self.target_label)
        self.adjustSize()

    def paintEvent(self, event):
        # setStyleSheet 会触发提前重绘，属性缺失时跳过本帧
        if not hasattr(self, "_bg_color"):
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        bg = QColor(self._bg_color)
        bg.setAlpha(max(0, min(255, self._bg_alpha)))
        p.setPen(QPen(QColor(255, 255, 255, 24), 1))
        p.setBrush(QBrush(bg))
        p.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 14, 14)

    def apply_style(self, font_size, text_color, bg_color, bg_opacity,
                    outline, outline_width, outline_color):
        """按配置应用外观：字号/颜色/背景/描边。背景走 paintEvent 自绘。"""
        opacity = max(0, min(100, int(bg_opacity)))
        self._bg_color = QColor(bg_color)
        self._bg_alpha = int(opacity * 2.55)
        qss = f"""
        QLabel#OverlaySource {{
            color: rgba(255, 255, 255, 150);
            font-size: {max(10, int(font_size * 0.72))}px;
            background: transparent;
        }}
        QLabel#OverlayTarget {{
            color: {text_color};
            font-size: {font_size}px;
            font-weight: 700;
            background: transparent;
        }}
        """
        self.setStyleSheet(qss)
        outline_w = outline_width if outline else 0
        self.source_label.set_outline(outline_w, outline_color)
        self.target_label.set_outline(outline_w, outline_color)
        self.target_label.set_fill_color(text_color)
        self.source_label.set_fill_color(QColor(255, 255, 255, 150))
        self.update()
        self.updateGeometry()
        self.adjustSize()

    def show_caption(self, source_text, target_text, show_source=True):
        self.source_label.setText(source_text if show_source else "")
        self.source_label.setVisible(show_source and bool(source_text))
        self.target_label.setText(target_text)
        self.adjustSize()
        self.updateGeometry()

    def clear_caption(self):
        self.source_label.setText("")
        self.target_label.setText("")
        self.adjustSize()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        if self._drag_pos is not None and self._on_moved:
            from PySide6.QtCore import QTimer

            def _save_final():
                # 字幕刷新可能触发 adjustSize 微调几何，稳定后再存一次最终位置
                if self._on_moved:
                    self._on_moved(self.x(), self.y())
            self._on_moved(self.x(), self.y())
            QTimer.singleShot(400, _save_final)
        self._drag_pos = None

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        act_close = menu.addAction("关闭悬浮字幕")
        chosen = menu.exec(event.globalPos())
        if chosen == act_close:
            self.hide()
            if self._on_closed:
                self._on_closed()
