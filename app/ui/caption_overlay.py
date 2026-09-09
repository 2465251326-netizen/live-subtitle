from PySide6.QtCore import (
    Qt, QPointF, QTimer, QPropertyAnimation, QEasingCurve, Property,
)
from PySide6.QtGui import (
    QPainter, QPainterPath, QPen, QBrush, QColor, QTextOption,
    QTextLayout,
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QMenu, QPushButton,
    QListWidget, QListWidgetItem, QGraphicsOpacityEffect,
)

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
    """悬浮字幕条。

    内容形态（互斥）：单条 / 列表 / 跑马灯（连续输出，overlay_stream）。
    跑马灯：只显示最新一句，旧句淡出消失，新句淡入。

    窗口交互（v2.1.8）：可拖动移动；四边/四角拖拽调整大小（像浏览器窗口）；
    手动调整后尺寸固定并持久化，右键可"恢复自动大小"。
    """

    RESIZE_MARGIN = 10
    MIN_W = 320
    MIN_H = 120

    def __init__(self, on_closed=None, on_moved=None,
                 on_open_settings=None, on_toggle_source=None,
                 on_toggle_translation_only=None, on_resized=None):
        super().__init__(None)
        # 背景参数必须先于任何可能触发 paintEvent 的调用（setStyleSheet 等）
        self._bg_color = QColor("#0c0e14")
        self._bg_alpha = int(78 * 2.55)
        self._font_size = 18
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setObjectName("OverlayRoot")
        self.setStyleSheet(OVERLAY_QSS)
        self.resize(460, 180)
        self.setMinimumSize(self.MIN_W, self.MIN_H)
        self._drag_pos = None
        self._resizing = None  # v2.1.8：边缘拖拽的方向集（{"n","s","e","w"} 子集）
        self._user_resized = False
        self._on_closed = on_closed
        self._on_moved = on_moved
        self._on_open_settings = on_open_settings
        self._on_toggle_source = on_toggle_source
        self._on_toggle_translation_only = on_toggle_translation_only
        self._on_resized = on_resized
        self._show_source = True

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 10, 14, 14)

        # 顶部行：状态行 + 悬浮 X 关闭按钮（hover 显现）
        top_row = QHBoxLayout()
        top_row.setSpacing(6)
        self.status_label = QLabel("")
        self.status_label.setObjectName("OverlayStatus")
        self.status_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        top_row.addWidget(self.status_label, 1)
        self.close_button = QPushButton("✕")
        self.close_button.setObjectName("OverlayClose")
        self.close_button.setFixedSize(22, 22)
        self.close_button.setCursor(Qt.PointingHandCursor)
        self.close_button.setToolTip("关闭悬浮字幕")
        self.close_button.clicked.connect(self._request_close)
        self.close_button.hide()  # 悬浮条上才显现，避免误点
        top_row.addWidget(self.close_button, 0, Qt.AlignTop)
        layout.addLayout(top_row)

        # 状态行与正文保持大间距（apply_style 中随字号重算）
        self._status_gap = QWidget()
        self._status_gap.setFixedHeight(int(self._font_size * 1.5))
        layout.addWidget(self._status_gap)

        self.source_label = OutlinedLabel("")
        self.source_label.setObjectName("OverlaySource")
        self.source_label.setWordWrap(True)
        self.source_label.setAlignment(Qt.AlignCenter)

        self.target_label = OutlinedLabel("悬浮字幕已开启")
        self.target_label.setObjectName("OverlayTarget")
        self.target_label.setWordWrap(True)
        self.target_label.setAlignment(Qt.AlignCenter)

        # 列表模式（补充3）：最近 N 条可滚动字幕
        self.list_widget = QListWidget()
        self.list_widget.setObjectName("OverlayList")
        self.list_widget.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.list_widget.hide()
        self._list_mode = False
        self._list_max = 5

        layout.addWidget(self.source_label)
        layout.addWidget(self.target_label)
        layout.addWidget(self.list_widget)
        self.adjustSize()

        # 跑马灯过渡（v2.1.8）：旧句淡出(160ms)→换字→新句淡入(200ms)，
        # 快速连句自动合并只保留最新（"旧句淡出消失"）
        self._src_effect = QGraphicsOpacityEffect(self.source_label)
        self.source_label.setGraphicsEffect(self._src_effect)
        self._tgt_effect = QGraphicsOpacityEffect(self.target_label)
        self.target_label.setGraphicsEffect(self._tgt_effect)
        self._stream_anim = QPropertyAnimation(self, b"marqueeOpacity", self)
        self._stream_anim.setDuration(170)
        self._stream_anim.setStartValue(1.0)
        self._stream_anim.setKeyValueAt(0.5, 0.0)
        self._stream_anim.setEndValue(1.0)
        self._stream_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._stream_anim.valueChanged.connect(self._on_stream_opacity)
        self._stream_anim.finished.connect(self._on_stream_anim_done)
        self._marquee_next = None
        self._marquee_animating = False
        self._continuous = False

    # ---------- Qt 属性：跑马灯透明度（动画驱动两个标签同步淡入淡出） ----------

    def _on_stream_opacity(self, v):
        """动画中间值：同步两个标签的透明度（跑马灯淡出/淡入共享）。"""
        self._src_effect.setOpacity(float(v))
        self._tgt_effect.setOpacity(float(v))

    def _on_stream_anim_done(self):
        """一轮淡出/淡入动画结束：清动画态；若期间又来新句则继续下一轮。"""
        self._marquee_animating = False
        self._src_effect.setOpacity(1.0)
        self._tgt_effect.setOpacity(1.0)
        if self._marquee_next is not None:
            self._marquee_advance()

    def _get_marquee_opacity(self):
        return self._src_effect.opacity()

    def _set_marquee_opacity(self, v):
        self._src_effect.setOpacity(float(v))
        self._tgt_effect.setOpacity(float(v))

    marqueeOpacity = Property(
        float, _get_marquee_opacity, _set_marquee_opacity)

    # ---------- 模式切换 ----------

    def set_continuous_mode(self, enabled):
        """跑马灯模式（v2.1.8）：只显示最新一句，旧句淡出消失，新句淡入。"""
        self._continuous = bool(enabled)
        if self._continuous:
            self.list_widget.hide()
            self.source_label.show()
            self.target_label.show()
        else:
            self.source_label.setVisible(not self._list_mode and bool(self.source_label.text()))
            self.target_label.setVisible(not self._list_mode)
            self.list_widget.setVisible(self._list_mode)
        self.adjustSize()

    def set_list_mode(self, enabled, max_items=5):
        """切换 单条字幕 / 最近N条列表 两种内容形态（跑马灯开启时优先级更高）。"""
        self._list_mode = bool(enabled)
        self._list_max = max(2, min(10, int(max_items)))
        if getattr(self, "_continuous", False):
            self.list_widget.hide()
            self.source_label.show()
            self.target_label.show()
            return
        self.source_label.setVisible(not self._list_mode and bool(self.source_label.text()))
        self.target_label.setVisible(not self._list_mode)
        self.list_widget.setVisible(self._list_mode)
        if self._list_mode:
            self.list_widget.setStyleSheet(
                "QListWidget#OverlayList { background: transparent; border: none;"
                f" color: #ffffff; font-size: {max(11, int(self._font_size * 0.72))}px;"
                "QListWidget#OverlayList::item { padding: 2px 0; }")
            self._trim_list()
        self.adjustSize()

    def _trim_list(self):
        while self.list_widget.count() > self._list_max:
            self.list_widget.takeItem(0)
        if self.list_widget.count():
            self.list_widget.scrollToBottom()

    def _append_list_item(self, source_text, target_text):
        text = target_text if not source_text else f"{target_text}　·　{source_text}"
        self.list_widget.addItem(QListWidgetItem(text))
        self._trim_list()

    # ---------- 字幕内容 ----------

    def _marquee_show(self, source_text, target_text):
        """跑马灯：旧内容淡出→换字→新内容淡入；快速连句自动合并为最新。"""
        self._marquee_next = (source_text, target_text)
        if self._marquee_animating:
            return
        self._marquee_advance()

    def _marquee_advance(self):
        nxt = self._marquee_next
        if nxt is None:
            self._marquee_animating = False
            return
        self._marquee_next = None
        self._marquee_animating = True
        self._apply_marquee(*nxt)
        self._stream_anim.stop()
        self._stream_anim.start()

    def _apply_marquee(self, source_text, target_text):
        show_source = bool(self._show_source) and bool(source_text)
        # 跑马灯永远显示最新一句：译文为主行，原文按 show_source 显示
        self.source_label.setText(source_text if show_source else "")
        self.source_label.setVisible(show_source)
        self.target_label.setText(target_text or "")
        self.adjustSize()

    def show_caption(self, source_text, target_text, show_source=True):
        self._show_source = bool(show_source)
        if self._continuous:
            self._marquee_show(source_text, target_text)
            return
        if self._list_mode:
            self._append_list_item(source_text if show_source else "", target_text)
            self.adjustSize()
            self.updateGeometry()
            return
        self.source_label.setText(source_text if show_source else "")
        self.source_label.setVisible(show_source and bool(source_text))
        self.target_label.setText(target_text)
        self.adjustSize()
        self.updateGeometry()

    def show_pending(self, source_text):
        """流式两段式（v2.1.4）：识别文本先上屏（译文稍后补齐）。

        跑马灯模式：直接显示最新识别文本（旧句淡出消失）。
        不改动 _show_source（此处未知，沿用 show_caption/_result 已知值）。"""
        if self._continuous:
            self._marquee_show(source_text, "")
            return
        if self._list_mode:
            # 列表模式：占位行只在最末条是旧占位时复用
            it = self.list_widget.item(self.list_widget.count() - 1) if self.list_widget.count() else None
            if it is None or "⟳" not in it.text():
                self.list_widget.addItem(QListWidgetItem(f"⟳ {source_text}"))
                self._trim_list()
            self.adjustSize()
            self.updateGeometry()
            return
        self.source_label.setText(source_text)
        self.source_label.setVisible(True)
        if "⟳" not in self.target_label.text():
            self.target_label.setText("⟳ …")
            self.adjustSize()
            self.updateGeometry()

    def show_pending_result(self, source_text, target_text, show_source=True):
        """译文就绪：跑马灯淡入最新译文；两段式原地补齐占位。"""
        self._show_source = bool(show_source)
        if self._continuous:
            self._marquee_show(source_text, target_text)
            return
        if self._list_mode:
            it = self.list_widget.item(self.list_widget.count() - 1) if self.list_widget.count() else None
            if it is not None and "⟳" in it.text():
                it.setText(f"{target_text}　·　{source_text if show_source else ''}".rstrip("　·"))
            else:
                self._append_list_item(source_text if show_source else "", target_text)
            self._trim_list()
            self.adjustSize()
            self.updateGeometry()
            return
        if "⟳" in self.target_label.text():
            self.show_caption(source_text, target_text, show_source)
        else:
            self.show_caption(source_text, show_source and source_text or "", show_source)

    def clear_caption(self):
        self.source_label.setText("")
        self.target_label.setText("")
        self.adjustSize()

    # ---------- 状态行 ----------

    def set_status(self, text, is_error=False):
        """更新状态行：运行状态 · 输入来源 · 引擎 · 模型；错误时变橙红。"""
        self.status_label.setText(text)
        if is_error:
            self.status_label.setStyleSheet("color: #ff8a5c; font-size: 11px;")
        else:
            self.status_label.setStyleSheet("color: rgba(255, 255, 255, 150); font-size: 11px;")

    def _request_close(self):
        self.hide()
        if self._on_closed:
            self._on_closed()

    # ---------- 拖动 / 边缘缩放 / 悬停 ----------

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
        self._font_size = max(10, int(font_size))
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
        # 拼接基础 OVERLAY_QSS（保住 X 关闭按钮/状态行样式）再覆盖正文规则；
        # 直接 setStyleSheet(qss) 会整体替换，X 按钮退化为系统默认样式（v2.0.1）
        self.setStyleSheet(OVERLAY_QSS + qss)
        outline_w = outline_width if outline else 0
        self.source_label.set_outline(outline_w, outline_color)
        self.target_label.set_outline(outline_w, outline_color)
        self.target_label.set_fill_color(text_color)
        self.source_label.set_fill_color(QColor(255, 255, 255, 150))
        # 状态行与正文的间距随字号走，保持"空三格"的宽松观感
        self._status_gap.setFixedHeight(int(self._font_size * 1.5))
        if self._list_mode:
            # 列表模式高度随字号与保留条数走
            self.list_widget.setFixedHeight(
                int(self._list_max * (self._font_size * 0.72 + 14)))
            # v2.0.1：拼进整体样式表而不是独立赋值（此前的双大括号笔误会让
            # 整段 QSS 解析失败，列表模式字号/颜色全部不生效）
            self.list_widget.setStyleSheet(
                "QListWidget#OverlayList { background: transparent; border: none;"
                f" color: #ffffff; font-size: {max(11, int(self._font_size * 0.72))}px;"
                "QListWidget#OverlayList::item { padding: 2px 0; }")
            self._trim_list()
        self.update()
        self.updateGeometry()
        self.adjustSize()

    def adjustSize(self):
        # v2.1.8：用户手动调整过大小后，字幕刷新不得改变窗口尺寸
        # （文本超宽时由 QLabel wordwrap 折行消化）
        if getattr(self, "_user_resized", False):
            return
        super().adjustSize()

    def _edge_at(self, pos):
        """返回鼠标所在边缘方向集（空=非边缘）。"""
        r = self.rect()
        m = self.RESIZE_MARGIN
        edges = []
        if pos.x() <= m:
            edges.append("w")
        if pos.x() >= r.width() - m:
            edges.append("e")
        if pos.y() <= m:
            edges.append("n")
        if pos.y() >= r.height() - m:
            edges.append("s")
        return edges

    def _cursor_for(self, edges):
        es = set(edges)
        if es == {"n", "e"} or es == {"s", "w"}:
            return Qt.SizeFDiagCursor
        if es == {"n", "w"} or es == {"s", "e"}:
            return Qt.SizeBDiagCursor
        if "n" in es or "s" in es:
            return Qt.SizeVerCursor
        if "e" in es or "w" in es:
            return Qt.SizeHorCursor
        return None

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            edges = self._edge_at(event.position().toPoint())
            if edges:
                # v2.1.8：边缘拖拽 = 调整大小（优先于移动）
                self._resizing = edges
                self._resize_start_geo = self.frameGeometry()
                self._resize_start = event.globalPosition().toPoint()
                event.accept()
                return
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._resizing and event.buttons() & Qt.LeftButton:
            g = event.globalPosition().toPoint()
            delta = g - self._resize_start
            geo = self._resize_start_geo
            left, top = geo.left(), geo.top()
            width, height = geo.width(), geo.height()
            if "w" in self._resizing:
                left = geo.left() + delta.x()
                width = geo.width() - delta.x()
            if "e" in self._resizing:
                width = geo.width() + delta.x()
            if "n" in self._resizing:
                top = geo.top() + delta.y()
                height = geo.height() - delta.y()
            if "s" in self._resizing:
                height = geo.height() + delta.y()
            width = max(self.MIN_W, width)
            height = max(self.MIN_H, height)
            if "w" in self._resizing:
                left = geo.right() - width + 1
            if "n" in self._resizing:
                top = geo.bottom() - height + 1
            self._user_resized = True
            self.setGeometry(left, top, width, height)
            event.accept()
            return
        if self._drag_pos is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()
            return
        if not self._resizing:
            cur = self._cursor_for(self._edge_at(event.position().toPoint()))
            self.setCursor(cur if cur is not None else Qt.ArrowCursor)

    def mouseReleaseEvent(self, event):
        if self._resizing and event.button() == Qt.LeftButton:
            self._resizing = None
            # v2.1.8：调整结束后持久化尺寸（位置沿用 _on_moved 通道）
            if self._on_resized:
                self._on_resized(self.width(), self.height())
            self._save_final_pos()
            event.accept()
            return
        if self._drag_pos is not None and event.button() == Qt.LeftButton:
            # 释放时最后一批 move 事件可能仍在 Qt 事件队列中，x()/y() 读到旧值。
            # singleShot(0) 在全部待处理事件落地后触发，根治落点偏差；
            # 400ms 后再存一次，兜底字幕刷新触发 adjustSize 的几何微调。
            QTimer.singleShot(0, self._save_final_pos)
            QTimer.singleShot(400, self._save_final_pos)
        self._drag_pos = None

    def _save_final_pos(self):
        if self._on_moved:
            self._on_moved(self.x(), self.y())

    def enterEvent(self, event):
        # 悬停时显现右上角 X 按钮；离开时隐藏
        self.close_button.show()

    def leaveEvent(self, event):
        self.close_button.hide()
        if not self._resizing:
            self.setCursor(Qt.ArrowCursor)

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        act_settings = menu.addAction("打开设置…")
        act_source = menu.addAction("切换输入来源")
        act_transonly = menu.addAction("只显示译文")
        act_transonly.setCheckable(True)
        act_transonly.setChecked(not self._show_source)
        act_auto_size = menu.addAction("恢复自动大小")
        act_auto_size.setEnabled(self._user_resized)
        menu.addSeparator()
        act_hide = menu.addAction("隐藏字幕条")
        chosen = menu.exec(event.globalPos())
        if chosen == act_settings:
            if self._on_open_settings:
                self._on_open_settings()
        elif chosen == act_source:
            if self._on_toggle_source:
                self._on_toggle_source()
        elif chosen == act_transonly:
            # v2.1.8：只显示译文 = show_source 取反（立即生效并持久化）
            if self._on_toggle_translation_only:
                self._on_toggle_translation_only(act_transonly.isChecked())
        elif chosen == act_auto_size:
            self._user_resized = False
            if self._on_resized:
                self._on_resized(0, 0)  # 0 = 清除持久化尺寸
            self.adjustSize()
        elif chosen == act_hide:
            self._request_close()
