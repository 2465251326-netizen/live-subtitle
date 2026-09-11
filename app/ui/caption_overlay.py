from PySide6.QtCore import (
    Qt, QPointF, QTimer,
)
from PySide6.QtGui import (
    QPainter, QPainterPath, QPen, QBrush, QColor, QTextOption,
    QTextLayout,
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QMenu, QPushButton,
    QListWidget, QListWidgetItem, QTextBrowser,
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

    RESIZE_MARGIN = 16   # v2.3.19（P25b）：10→16px 命中带 + hover 亮边提示
    MIN_W = 320
    MIN_H = 120

    def __init__(self, on_closed=None, on_moved=None,
                 on_open_settings=None, on_toggle_source=None,
                 on_toggle_translation_only=None, on_resized=None,
                 on_click_through=None, on_correct=None):
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
        self._on_click_through = on_click_through
        # v2.3.21（P29）：纠错入口与主窗卡片同源——on_correct(词典键, 预填文本)
        self._on_correct = on_correct
        self._last_result = ("", "")
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

        # 连续文本流（v2.2.0/v2.2.2）：译文不断追加进同一段富文本，自动换行、
        # 自动滚到最新、超长自动裁掉最旧内容——真正的"连续不间断输出"
        # v2.2.2 修复：此控件必须加入主布局（此前遗漏 addWidget，QTextBrowser
        # 无父控件时成为独立顶层窗口——用户看到"莫名其妙的 Windows 窗口"，
        # 而连续输出内容全写进了这个没显示在悬浮条里的孤儿控件）
        self.stream_view = QTextBrowser(self)
        self.stream_view.setObjectName("OverlayStream")
        self.stream_view.setReadOnly(True)
        self.stream_view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.stream_view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.stream_view.setFrameShape(QTextBrowser.NoFrame)
        self.stream_view.setOpenExternalLinks(False)
        self.stream_view.hide()
        self.stream_view.setContextMenuPolicy(Qt.NoContextMenu)
        self._continuous = False
        self._stream_parts = []        # [(kind, text)] 追加序列（kind: source/target）
        self._STREAM_MAX_CHARS = 1200  # 纯文本超过则从头部淘汰旧句
        self._STREAM_KEEP_CHARS = 700  # 淘汰后保留的尾部字符量
        layout.addWidget(self.stream_view, 1)  # 连续模式占满正文区，悬浮条高度由它撑起
        self.adjustSize()

        # v2.3.19（P25）：空白区鼠标点击穿透——把玩报告头号痛点：单条/跑马灯
        # 模式下文字只占中间一条，上下大片透明区却吞掉鼠标（挡住视频播放器的
        # 进度条/暂停键）。光标落在"文字/状态行/边缘把手/关闭键"之外时，给
        # HWND 加 WS_EX_TRANSPARENT 让点击穿到下层应用（Qt 属性做不到，见
        # _apply_os_transparency）；光标探回即恢复交互。穿透后本窗收不到任何
        # 鼠标事件，只能靠全局光标轮询判断"是否该醒"。
        self._click_through_enabled = True   # 右键菜单可切换，并持久化到配置
        self._transparent_now = False
        self._hover_edges = []
        self._pt_timer = QTimer(self)
        self._pt_timer.setInterval(140)
        self._pt_timer.timeout.connect(self._poll_transparency)
        self._pt_timer.start()

    # ---------- v2.3.19（P25）：点击穿透 + 缩放可发现性 ----------

    def set_click_through(self, enabled):
        """开/关"空白区点击穿透"。关闭时立即恢复整窗可交互。"""
        self._click_through_enabled = bool(enabled)
        if not self._click_through_enabled and self._transparent_now:
            self._apply_os_transparency(False)
            self._transparent_now = False

    def _apply_os_transparency(self, on):
        """v2.3.19（P25a）OS 级点击穿透。实机取证：Qt 的
        WA_TransparentForMouseEvents 在 Windows 上**不设 WS_EX_TRANSPARENT**
        ——只改 Qt 内部事件路由，顶层窗口照样吞掉点击、穿不到下层应用。
        真穿透必须直改 HWND 扩展样式：本窗因 WA_TranslucentBackground 已带
        WS_EX_LAYERED，只需增删 WS_EX_TRANSPARENT 位并以 SWP_FRAMECHANGED 刷新。"""
        import ctypes
        hwnd = int(self.winId())
        GWL_EXSTYLE = -20
        WS_EX_TRANSPARENT = 0x00000020
        WS_EX_LAYERED = 0x00080000
        user32 = ctypes.windll.user32
        ex = ctypes.c_long(user32.GetWindowLongW(hwnd, GWL_EXSTYLE)).value
        if on:
            ex |= (WS_EX_TRANSPARENT | WS_EX_LAYERED)
        else:
            ex &= ~WS_EX_TRANSPARENT
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex)
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                            0x1 | 0x2 | 0x4 | 0x20)   # NOSIZE|NOMOVE|NOZORDER|FRAMECHANGED

    def _text_band(self, label):
        """标签内**真实文字**的紧凑包围带（居中排版 → 以 label 垂直中心展开；
        用 QFontMetrics 算行数高）。不能直接用 label.geometry()——布局拉伸后
        它的 widget 矩形远大于文字本身，会把大片死区误标为可交互，穿透形同虚设。"""
        from PySide6.QtCore import QRect
        fm = label.fontMetrics()
        text = label.text()
        w = max(40, label.width() - 16)
        br = fm.boundingRect(0, 0, w, 100000, Qt.TextWordWrap, text or " ")
        h = br.height() + fm.lineSpacing()   # 一行余量，防边缘擦到字
        cy = label.geometry().center().y()
        return QRect(0, cy - h // 2, self.width(), h)

    def _interactive_rects(self):
        """交互区（局部坐标）：列表模式整窗可交互（它是唯一真有滚动条的形态）；
        连续流/单条/跑马灯只保留 状态行 + **文字紧凑带** + 四边缩放把手带。
        v2.3.21（P28）：连续流旧版"整窗可交互"保护的是一个不存在的手势——
        只读 QTextBrowser 关着滚动条，用户滚不动它，却要整块吞掉视频上的点击。
        现按已渲染文档高度压缩交互带：流式内容贴底渲染，文字不满时上方透明区
        回归穿透；文档铺满视口时自然回到全窗，行为连续无跳变。"""
        from PySide6.QtCore import QRect
        R = self.rect()
        if self._list_mode:
            return [R]  # 可滚动列表，整块可交互
        m = self.RESIZE_MARGIN
        rects = [
            QRect(0, 0, R.width(), m),            # 四边把手带（缩放/抓取入口）
            QRect(0, R.height() - m, R.width(), m),
            QRect(0, 0, m, R.height()),
            QRect(R.width() - m, 0, m, R.height()),
            self.status_label.geometry(),         # 状态行（其右侧即 X 键区）
        ]
        if self._continuous:
            vp = self.stream_view.viewport()
            th = int(self.stream_view.document().size().height())
            if th <= 0 or vp.height() <= 0:
                return [R]                        # 文档未布局（首帧/隐藏）→ 保守全窗
            band_h = min(th + 8, vp.height())
            top = self.stream_view.geometry().top() + max(0, vp.height() - band_h)
            rects.append(QRect(0, top, R.width(), min(band_h, R.height() - top)))
            return rects
        if self.source_label.isVisible():
            rects.append(self._text_band(self.source_label))
        if self.target_label.isVisible():
            rects.append(self._text_band(self.target_label))
        return rects

    def _cursor_interactive(self, local_pt):
        from PySide6.QtCore import QPoint
        p = QPoint(int(local_pt.x()), int(local_pt.y()))
        for r in self._interactive_rects():
            if r.adjusted(-6, -6, 6, 6).contains(p):
                return True
        return False

    def _poll_transparency(self):
        """140ms 轮询全局光标：不在交互区且窗口可见→置穿透；否则撤销。
        拖移/缩放进行中绝不切换（会打断抓取）。"""
        if not self.isVisible():
            return
        if self._drag_pos is not None or self._resizing:
            return
        from PySide6.QtGui import QCursor
        g = QCursor.pos()
        local = self.mapFromGlobal(g)
        inside = (0 <= local.x() < self.width() and 0 <= local.y() < self.height())
        want_transparent = bool(self._click_through_enabled) and not (
            inside and self._cursor_interactive(local))
        if want_transparent != self._transparent_now:
            self._transparent_now = want_transparent
            self._apply_os_transparency(want_transparent)
            if want_transparent:
                self.close_button.hide()
                self._hover_edges = []
                self.setCursor(Qt.ArrowCursor)
                self.update()

    # ---------- 连续文本流（v2.2.0/v2.2.3） ----------

    def _stream_visible_kinds(self):
        """连续流当前应显示的段落类型（v2.2.3）：
        "只显示译文"（show_source=False）时原文段不再进入流，只保留译文。"""
        return ("target",) if not bool(self._show_source) else ("source", "target")

    def _stream_refresh(self):
        """按 _stream_parts 重建整段富文本并滚到最新（v2.2.3：按 _show_source
        过滤段落；增量追加替代全量 setHtml，长文档不再卡顿）。"""
        import html as _html
        kinds = self._stream_visible_kinds()
        base = getattr(self, "_stream_text_color", None) or QColor("#ffffff")
        body = []
        for kind, text in self._stream_parts:
            if kind not in kinds:
                continue
            esc = _html.escape(text)
            if kind == "source":
                body.append(
                    f"<span style='color: rgba(255,255,255,145);"
                    f" font-size: {max(11, int(self._font_size * 0.66))}px;'>"
                    f"{esc} </span>")
            else:
                body.append(
                    f"<span style='color: {base.name()};"
                    f" font-size: {self._font_size}px; font-weight: 600;'>"
                    f"{esc} </span>")
        self.stream_view.setHtml("".join(body))
        sb = self.stream_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def stream_append(self, text, kind="target"):
        """连续输出追加（v2.2.3）：只显示译文模式下原文段直接不入流；
        增量 appendHtml（不全量重建），超长时整体重建一次完成头部淘汰。"""
        if not text:
            return
        if kind not in self._stream_visible_kinds():
            # "只显示译文"模式下原文段不入流（此前会漏显示）
            return
        import html as _html
        esc = _html.escape(text)
        if kind == "source":
            seg = (f"<span style='color: rgba(255,255,255,145);"
                   f" font-size: {max(11, int(self._font_size * 0.66))}px;'>"
                   f"{esc} </span>")
        else:
            seg = (f"<span style='color: {getattr(self, '_stream_text_color', QColor('#ffffff')).name()};"
                   f" font-size: {self._font_size}px; font-weight: 600;'>"
                   f"{esc} </span>")
        self._stream_parts.append((kind, text))
        plain = sum(len(t) for _k, t in self._stream_parts)
        if plain > self._STREAM_MAX_CHARS:
            # 超长：整体重建一次完成头部淘汰（罕见路径）
            while plain > self._STREAM_KEEP_CHARS and len(self._stream_parts) > 2:
                _k, t = self._stream_parts.pop(0)
                plain -= len(t)
            self._stream_refresh()
        else:
            # 常规路径：增量追加（v2.2.3：QTextBrowser 无 appendHtml，改用
            # 移动光标到文末 insertHtml——避免全量 setHtml 的长文档卡顿）
            from PySide6.QtGui import QTextCursor
            cur = self.stream_view.textCursor()
            cur.movePosition(QTextCursor.End)
            cur.insertHtml(seg)
            self.stream_view.setTextCursor(cur)
            sb = self.stream_view.verticalScrollBar()
            sb.setValue(sb.maximum())

    # ---------- 模式切换 ----------

    def set_continuous_mode(self, enabled):
        """连续文本流模式（v2.2.0）：译文/原文不断累积追加到同一段文字，
        自动换行、自动滚到最新、超长自动裁剪最旧——真正不间断的连续输出。"""
        self._continuous = bool(enabled)
        self.list_widget.setVisible(self._list_mode and not self._continuous)
        self.source_label.setVisible(not self._continuous and not self._list_mode)
        self.target_label.setVisible(not self._continuous and not self._list_mode)
        self.stream_view.setVisible(self._continuous)
        self._stream_refresh()
        self.adjustSize()

    def _clear_stream(self):
        self._stream_parts.clear()
        self.stream_view.setHtml("")

    def set_list_mode(self, enabled, max_items=5):
        """切换单条字幕 / 最近N条列表两种内容形态（连续流开启时优先级更高）。"""
        self._list_mode = bool(enabled)
        self._list_max = max(2, min(10, int(max_items)))
        if self._continuous:
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

    def show_caption(self, source_text, target_text, show_source=True):
        self._show_source = bool(show_source)
        self._last_result = (source_text or "", target_text or "")  # v2.3.21（P29）
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
        """流式两段式（v2.1.4）：识别文本先上屏（译文稍后补齐）。

        连续流模式：按 _show_source 决定原文是否入流——
        "只显示译文"时原文段直接跳过（v2.2.3：此前会漏进流里）。"""
        if self._continuous:
            if bool(self._show_source):
                self.stream_append(source_text, kind="source")
            return
        if self._list_mode:
            # 列表模式：占位行只在最末条是旧占位时复用
            # v2.2.1：占位文本按 _show_source 过滤——"只显示译文"时原文不闪现
            it = self.list_widget.item(self.list_widget.count() - 1) if self.list_widget.count() else None
            if it is None or "⟳" not in it.text():
                self.list_widget.addItem(QListWidgetItem(
                    f"⟳ {source_text}" if self._show_source else "⟳ …"))
                self._trim_list()
            self.adjustSize()
            self.updateGeometry()
            return
        # v2.2.1：单条路径同样按 _show_source 过滤（"只显示译文"下原文不闪现）
        self.source_label.setText(source_text if self._show_source else "")
        self.source_label.setVisible(bool(self._show_source) and bool(source_text))
        if "⟳" not in self.target_label.text():
            self.target_label.setText("⟳ …")
            self.adjustSize()
            self.updateGeometry()

    def show_pending_result(self, source_text, target_text, show_source=True):
        """译文就绪：连续流模式追加译文（白色主文）；两段式原地补齐占位。"""
        self._show_source = bool(show_source)
        self._last_result = (source_text or "", target_text or "")  # v2.3.21（P29）
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
        self._clear_stream()
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
        # v2.3.19（P25b）：hover 到的边缘画 2px 亮蓝线，提示可拖拽缩放
        if getattr(self, "_hover_edges", []):
            hl = QPen(QColor(120, 170, 255, 210), 2)
            p.setPen(hl)
            r = self.rect()
            for e in self._hover_edges:
                if e == "n":
                    p.drawLine(r.left() + 8, r.top() + 1, r.right() - 8, r.top() + 1)
                elif e == "s":
                    p.drawLine(r.left() + 8, r.bottom() - 2, r.right() - 8, r.bottom() - 2)
                elif e == "w":
                    p.drawLine(r.left() + 1, r.top() + 8, r.left() + 1, r.bottom() - 8)
                elif e == "e":
                    p.drawLine(r.right() - 2, r.top() + 8, r.right() - 2, r.bottom() - 8)

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
        if self._continuous:
            # v2.2.3 连续文本流：高度=内容高度钳制到 [3行, 用户调整值或 8 行]，
            # 内容超出自动出滚动条——不再"必须手动拉大悬浮窗往下滑"
            self._stream_text_color = QColor(text_color)
            doc = self.stream_view.document()
            doc.setDefaultStyleSheet(
                f"body {{ color: {text_color}; font-size: {self._font_size}px;"
                " font-family: 'Microsoft YaHei UI'; }")
            line_h = self._font_size * 1.45
            max_h = int(max(self.MIN_H - 90, line_h * 8))
            # 用户手动调整过悬浮窗时尊重其高度；否则按内容自适应
            if getattr(self, "_user_resized", False):
                avail = max(int(self.height() * 0.7), int(line_h * 3))
                self.stream_view.setFixedHeight(min(avail, max_h))
            else:
                self.stream_view.setFixedHeight(min(int(line_h * 5), max_h))
            self.stream_view.setStyleSheet(
                "QTextBrowser#OverlayStream { background: transparent; border: none; }")
            self._stream_refresh()
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
            edges = self._edge_at(event.position().toPoint())
            if edges != getattr(self, "_hover_edges", []):
                # v2.3.19（P25b）：悬停边缘画亮线——"可缩放"从隐藏功能变可见
                self._hover_edges = edges
                self.update()
            cur = self._cursor_for(edges)
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
        if getattr(self, "_hover_edges", []):
            self._hover_edges = []
            self.update()

    def _snap_to_edge(self, edge):
        """v2.3.3（P2）：一键贴屏幕顶/底；v2.3.19（P25c）扩展左/右缘磁吸。
        悬浮条默认压着网页播放器控制条——模拟用户把玩报告实锤。"""
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtCore import QPoint
        scr = (QGuiApplication.screenAt(QPoint(self.frameGeometry().center()))
               or QGuiApplication.primaryScreen())
        g = scr.availableGeometry()
        if edge in ("top", "bottom"):
            x = min(max(self.x(), g.left()), max(g.left(), g.right() - self.width() + 1))
            y = g.top() + 8 if edge == "top" else max(g.top(), g.bottom() - self.height() - 7)
        elif edge == "left":
            y = min(max(self.y(), g.top()), max(g.top(), g.bottom() - self.height() + 1))
            x = g.left() + 8
        else:  # right
            y = min(max(self.y(), g.top()), max(g.top(), g.bottom() - self.height() + 1))
            x = max(g.left(), g.right() - self.width() + 1 - 8)
        self.move(x, y)
        if self._on_moved:
            self._on_moved(x, y)

    def _build_menu(self):
        """v2.3.21（P29）：菜单构建抽方法（可测）；新增悬浮条纠错入口——
        第十三轮吐槽：看字幕的人整天盯悬浮条不开主窗，P14 的卡片右键纠错
        永远够不着；最该长把手的地方没把手。"""
        menu = QMenu(self)
        acts = {}
        acts["settings"] = menu.addAction("打开设置…")
        acts["source"] = menu.addAction("切换输入来源")
        acts["transonly"] = menu.addAction("只显示译文")
        acts["transonly"].setCheckable(True)
        acts["transonly"].setChecked(not self._show_source)
        acts["autosize"] = menu.addAction("恢复自动大小")
        acts["autosize"].setEnabled(self._user_resized)
        menu.addSeparator()
        # v2.3.3（P2）：快捷归位；v2.3.19（P25c）：补左/右缘磁吸
        acts["snap_top"] = menu.addAction("贴到屏幕顶部")
        acts["snap_bottom"] = menu.addAction("贴到屏幕底部")
        acts["snap_left"] = menu.addAction("贴到屏幕左侧")
        acts["snap_right"] = menu.addAction("贴到屏幕右侧")
        menu.addSeparator()
        # v2.3.19（P25a）：空白处点击穿透开关（把玩报告头号痛点）
        acts["through"] = menu.addAction("空白处点击穿透")
        acts["through"].setCheckable(True)
        acts["through"].setChecked(self._click_through_enabled)
        menu.addSeparator()
        # v2.3.21（P29）：纠错入口——无内容时置灰，有内容预填最近一句
        last_src, last_tgt = getattr(self, "_last_result", ("", ""))
        acts["fix_asr"] = menu.addAction("纠正最近识别…")
        acts["fix_asr"].setEnabled(bool((last_src or "").strip()))
        acts["fix_tr"] = menu.addAction("纠正最近译文…")
        acts["fix_tr"].setEnabled(bool((last_tgt or "").strip()))
        menu.addSeparator()
        acts["hide"] = menu.addAction("隐藏字幕条")
        self._menu_acts = acts
        return menu

    def _menu_dispatch(self, chosen):
        acts = getattr(self, "_menu_acts", {})
        src, tgt = getattr(self, "_last_result", ("", ""))
        if chosen is None:
            return
        if chosen == acts.get("settings"):
            if self._on_open_settings:
                self._on_open_settings()
        elif chosen == acts.get("source"):
            if self._on_toggle_source:
                self._on_toggle_source()
        elif chosen == acts.get("transonly"):
            # v2.1.8：只显示译文 = show_source 取反（立即生效并持久化）
            if self._on_toggle_translation_only:
                self._on_toggle_translation_only(acts["transonly"].isChecked())
        elif chosen == acts.get("autosize"):
            self._user_resized = False
            if self._on_resized:
                self._on_resized(0, 0)  # 0 = 清除持久化尺寸
            self.adjustSize()
        elif chosen == acts.get("snap_top"):
            self._snap_to_edge("top")
        elif chosen == acts.get("snap_bottom"):
            self._snap_to_edge("bottom")
        elif chosen == acts.get("snap_left"):
            self._snap_to_edge("left")
        elif chosen == acts.get("snap_right"):
            self._snap_to_edge("right")
        elif chosen == acts.get("through"):
            self.set_click_through(acts["through"].isChecked())
            if self._on_click_through:
                self._on_click_through(acts["through"].isChecked())
        elif chosen == acts.get("fix_asr"):
            if self._on_correct:
                self._on_correct("mishear_map", src)
        elif chosen == acts.get("fix_tr"):
            if self._on_correct:
                self._on_correct("translate_fix_map", tgt)
        elif chosen == acts.get("hide"):
            self._request_close()

    def contextMenuEvent(self, event):
        menu = self._build_menu()
        self._menu_dispatch(menu.exec(event.globalPos()))
        menu.deleteLater()
