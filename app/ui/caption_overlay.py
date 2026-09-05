from PySide6.QtCore import Qt, QPoint
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QMenu

from app.ui.styles import OVERLAY_QSS


class CaptionOverlay(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
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

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 12, 22, 14)
        layout.setSpacing(4)

        self.source_label = QLabel("")
        self.source_label.setObjectName("OverlaySource")
        self.source_label.setWordWrap(True)
        self.source_label.setAlignment(Qt.AlignCenter)

        self.target_label = QLabel("悬浮字幕已开启")
        self.target_label.setObjectName("OverlayTarget")
        self.target_label.setWordWrap(True)
        self.target_label.setAlignment(Qt.AlignCenter)

        layout.addWidget(self.source_label)
        layout.addWidget(self.target_label)
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
        self._drag_pos = None

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        act_close = menu.addAction("关闭悬浮字幕")
        chosen = menu.exec(event.globalPos())
        if chosen == act_close:
            self.hide()
            if self.parent() and hasattr(self.parent(), "on_overlay_closed"):
                self.parent().on_overlay_closed()
