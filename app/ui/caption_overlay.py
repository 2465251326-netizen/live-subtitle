from PySide6.QtCore import Qt, QPointF, QTimer
from PySide6.QtGui import (
    QPainter, QPainterPath, QPen, QBrush, QColor, QTextOption,
    QTextLayout,
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QMenu, QPushButton,
    QListWidget, QListWidgetItem, QScrollArea,
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
    def __init__(self, on_closed=None, on_moved=None,
                 on_open_settings=None, on_toggle_source=None):
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
        self.setFixedWidth(460)
        self._drag_pos = None
        self._on_closed = on_closed
        self._on_moved = on_moved
        self._on_open_settings = on_open_settings
        self._on_toggle_source = on_toggle_source

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

        # 连续输出模式（v2.1.6）：字幕墙样式——逐句块状累积，旧句渐隐，
        # 最新句全亮，满了自动滚底并淘汰远古句
        self.stream_view = QScrollArea()
        self.stream_view.setObjectName("OverlayStream")
        self.stream_view.setWidgetResizable(True)
        self.stream_view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.stream_view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.stream_view.setFrameShape(QScrollArea.NoFrame)
        self.stream_view.hide()
        self._stream_host = QWidget()
        self._stream_lay = QVBoxLayout(self._stream_host)
        self._stream_lay.setContentsMargins(4, 2, 8, 6)
        self._stream_lay.setSpacing(6)
        self._stream_lay.addStretch()
        self._stream_host.setObjectName("OverlayStreamHost")
        self.stream_view.setWidget(self._stream_host)
        self._continuous = False
        self._stream_blocks = []  # [(原文, 译文block)]，配对补齐用
        self._STREAM_MAX = 6      # 幕上最多保留的句子数

        layout.addWidget(self.source_label)
        layout.addWidget(self.target_label)
        layout.addWidget(self.list_widget)
        layout.addWidget(self.stream_view)
        self.adjustSize()

    def set_continuous_mode(self, enabled):
        """连续输出模式（v2.1.6 样式 v2.1.7 重做）：字幕墙——逐句块状累积，
        旧句渐隐、最新句全亮，满了自动滚底并淘汰远古句。"""
        self._continuous = bool(enabled)
        if self._continuous:
            self.list_widget.hide()
            self.stream_view.show()
            self.source_label.hide()
            self.target_label.hide()
            self._stream_restyle()
        else:
            self.stream_view.hide()
            self.stream_view.setVisible(False)
            self.source_label.setVisible(not self._list_mode and bool(self.source_label.text()))
            self.target_label.setVisible(not self._list_mode)
            self.list_widget.setVisible(self._list_mode)
        self.adjustSize()

    # ---------- 连续输出（字幕墙） ----------

    STREAM_MAX = 6  # 幕上保留的句子数（旧句渐隐后淘汰）

    def _stream_new_block(self, source_text):
        """新建一个句块：原文小字 + 译文大字（v2.1.7 字幕墙）。"""
        src = QLabel(source_text)
        src.setObjectName("StreamSource")
        src.setWordWrap(True)
        tgt = QLabel("")
        tgt.setObjectName("StreamTarget")
        tgt.setWordWrap(True)
        w = QWidget()
        w.setAttribute(Qt.WA_TransparentForMouseEvents)
        v = QVBoxLayout(w)
        v.setContentsMargins(2, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(src)
        v.addWidget(tgt)
        self._stream_lay.insertWidget(self._stream_lay.count() - 1, w)
        self._stream_blocks.append((src, tgt, w))
        return w

    def _stream_restyle(self):
        """按"距最新句的距离"给各句块设置渐隐透明度 + 自动滚底 + 淘汰。"""
        n = len(self._stream_blocks)
        base = getattr(self, "_stream_text_color", QColor("#ffffff"))
        fs = getattr(self, "_font_size", 18)
        for i, (src, tgt, _w) in enumerate(self._stream_blocks):
            dist = n - 1 - i
            a = max(40, 255 - dist * 56)
            c = QColor(base); c.setAlpha(a)
            tgt.setStyleSheet(
                f"color: rgba({c.red()},{c.green()},{c.blue()},{c.alpha()});"
                f" font-size: {fs}px; font-weight: {700 if dist == 0 else 500};"
                " background: transparent;")
            sc = QColor(255, 255, 255); sc.setAlpha(max(26, 150 - dist * 42))
            src.setStyleSheet(
                f"color: rgba({sc.red()},{sc.green()},{sc.blue()},{sc.alpha()});"
                f" font-size: {max(11, int(fs * 0.68))}px; background: transparent;")
        while len(self._stream_blocks) > self.STREAM_MAX:
            _s, _t, w = self._stream_blocks.pop(0)
            w.setParent(None)
            w.deleteLater()
        sb = self.stream_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def stream_append(self, text, kind="target"):
        """连续输出追加（v2.1.7）：原文先建句块，译文就绪填入并高亮。"""
        if not text:
            return
        if kind == "source":
            self._stream_new_block(text)
        else:
            tgt = None
            for _s, t, _w in reversed(self._stream_blocks):
                if not t.text():
                    tgt = t
                    break
            if tgt is None:
                self._stream_new_block("")
                tgt = self._stream_blocks[-1][1]
            tgt.setText(text)
        self._stream_restyle()

    def set_list_mode(self, enabled, max_items=5):
        """切换 单条字幕 / 最近N条列表 两种内容形态。"""
        self._list_mode = bool(enabled)
        self._list_max = max(2, min(10, int(max_items)))
        if getattr(self, "_continuous", False):
            # v2.1.5：连续输出模式优先级高于列表模式
            self.list_widget.hide()
            self.stream_view.show()
            self.source_label.hide()
            self.target_label.hide()
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

    # ---------- 拖动与悬停 ----------

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
        if self._continuous:
            # 连续输出模式（v2.1.7）：固定高度滚动区 + 句块渐隐样式
            self._stream_text_color = QColor(text_color)
            self.stream_view.setFixedHeight(int(self._font_size * 7.5))
            self.stream_view.setStyleSheet(
                "QScrollArea#OverlayStream { background: transparent; border: none; }"
                "QWidget#OverlayStreamHost { background: transparent; }"
                "QScrollArea#OverlayStream > QWidget > QWidget { background: transparent; }")
            self._stream_restyle()
        self.update()
        self.updateGeometry()
        self.adjustSize()

    def show_caption(self, source_text, target_text, show_source=True):
        if self._continuous:
            self.stream_append(target_text, kind="target")
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
        """流式两段式（v2.1.4）：识别文本先上屏（译文位显示转圈占位）。

        队列里还有待翻句时不重复刷占位——等上一句译文落地后自然刷新。"""
        if self._continuous:
            self.stream_append(source_text, kind="source")
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
        """译文就绪：连续模式填入最新句块；两段式原地补齐占位。"""
        if self._continuous:
            self.stream_append(target_text, kind="target")
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
        if self._continuous:
            for _s, _t, w in list(self._stream_blocks):
                w.setParent(None)
                w.deleteLater()
            self._stream_blocks.clear()
            self._stream_restyle()
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

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        act_settings = menu.addAction("打开设置…")
        act_source = menu.addAction("切换输入来源")
        act_hide = menu.addAction("隐藏字幕条")
        chosen = menu.exec(event.globalPos())
        if chosen == act_settings:
            if self._on_open_settings:
                self._on_open_settings()
        elif chosen == act_source:
            if self._on_toggle_source:
                self._on_toggle_source()
        elif chosen == act_hide:
            self._request_close()
