"""v2.4.0 字幕面板（用户裁决：旧字幕条退役，工具条+历史滚动面板上位）。

形态：深灰圆角不透明面板。顶部工具条 = 🌐目标语言▾ | 关闭原文 | Aa字号▾ |
状态 | ↓最新 | ⋯ | 收起 | ✕；正文 = "原文(灰) + 译文(白加粗)" 成对左对齐的
历史滚动区，自动跟随最新，上滚暂停跟随。

交互（对旧字幕条的全面翻案）：
- 面板是不透明的板——整板任意处可拖（工具条是显式把手），不再玩"透明区
  哪里能点"的捉迷藏；旧点击穿透/紧凑带/描边体系（P25a/P28/P30 补丁层）随
  旧形态一并退役。
- 右缘拖宽（高度永远贴内容自动收放）；双击工具条 = 顶/底贴边循环。
- 右键或 ⋯ = 同一菜单：设置/切源/复制最近一句/纠正识别/纠正译文/导出 SRT/
  贴边四向/置顶/隐藏。

主窗接口保持兼容：show_pending / show_pending_result / show_caption /
clear_caption / set_status / apply_style(去描边参数)。
"""

from PySide6.QtCore import Qt, QPoint, QTimer, QSize
from PySide6.QtGui import QColor, QGuiApplication, QPainter
from PySide6.QtWidgets import (
    QWidget, QLabel, QMenu, QToolButton, QScrollArea, QApplication,
    QVBoxLayout, QHBoxLayout, QSizePolicy,
)


class CaptionOverlay(QWidget):
    MIN_W = 360
    MAX_ROWS = 40
    LANGS = [("zh-CN", "中文"), ("en", "英语"), ("ja", "日语"), ("ko", "韩语"),
             ("fr", "法语"), ("de", "德语"), ("ru", "俄语"), ("es", "西班牙语")]
    FONTS = [("小号", 16), ("中号", 22), ("大号", 30), ("特大", 40)]

    def __init__(self, on_closed=None, on_moved=None,
                 on_open_settings=None, on_toggle_source=None,
                 on_toggle_translation_only=None, on_resized=None,
                 on_correct=None, on_export_srt=None, on_language=None,
                 on_font_size=None, on_pin_changed=None, on_collapsed=None):
        super().__init__(None)
        self.setObjectName("SubtitlePanel")
        self._bg_color = QColor("#1c1f26")
        self._bg_alpha = int(92 * 2.55)
        self._font_size = 22
        self._text_color = QColor("#ffffff")
        self._show_source = True
        self._target_lang = "zh-CN"
        self._collapsed = False
        self._pinned = True
        self._follow = True
        self._rows = []
        self._pending_row = None
        self._last_result = ("", "")
        self._drag_pos = None
        self._resizing = False
        self._resize_start = None
        self._resize_start_w = 0
        self._user_resized = False
        self._on_closed = on_closed
        self._on_moved = on_moved
        self._on_open_settings = on_open_settings
        self._on_toggle_source = on_toggle_source
        self._on_toggle_translation_only = on_toggle_translation_only
        self._on_resized = on_resized
        self._on_correct = on_correct
        self._on_export_srt = on_export_srt
        self._on_language = on_language
        self._on_font_size = on_font_size
        self._on_pin_changed = on_pin_changed
        self._on_collapsed = on_collapsed

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 10)
        outer.setSpacing(6)
        # v2.4.0：解除布局最小宽钳制——否则 resize(用户450) 会被按钮 sizeHint
        # 总和顶回 ~580（实机插桩实证）；窄时尾部按钮裁切，宽度主权归用户。
        outer.setSizeConstraint(QVBoxLayout.SizeConstraint.SetNoConstraint)

        # ---------- 工具条（显式把手 + 高频四件套） ----------
        self._bar = QWidget(self)
        self._bar.setObjectName("PanelToolbar")
        self._bar.setFixedHeight(30)
        bl = QHBoxLayout(self._bar)
        bl.setContentsMargins(4, 0, 2, 0)
        bl.setSpacing(2)

        self._lang_btn = QToolButton()
        self._lang_btn.setPopupMode(QToolButton.InstantPopup)
        self._lang_btn.setMenu(self._build_lang_menu())
        bl.addWidget(self._lang_btn)

        self._src_btn = QToolButton()
        self._src_btn.clicked.connect(self._toggle_src)
        bl.addWidget(self._src_btn)

        self._font_btn = QToolButton()
        self._font_btn.setPopupMode(QToolButton.InstantPopup)
        self._font_btn.setMenu(self._build_font_menu())
        bl.addWidget(self._font_btn)

        self.status_lbl = QLabel("")
        self.status_lbl.setObjectName("PanelStatus")
        # 状态文字不参与最小宽度（长提示只省略不撑板）——宽度主权归用户
        self.status_lbl.setSizePolicy(QSizePolicy.Policy.Ignored,
                                      QSizePolicy.Policy.Preferred)
        bl.addWidget(self.status_lbl, 1)

        self._jump_btn = QToolButton()
        self._jump_btn.setText("↓ 最新")
        self._jump_btn.clicked.connect(self._scroll_bottom)
        self._jump_btn.hide()
        bl.addWidget(self._jump_btn)

        self._more_btn = QToolButton()
        self._more_btn.setText("⋯")
        self._more_btn.clicked.connect(self._show_more_menu)
        bl.addWidget(self._more_btn)

        self._collapse_btn = QToolButton()
        self._collapse_btn.setText("收起")
        self._collapse_btn.clicked.connect(self._toggle_collapse)
        bl.addWidget(self._collapse_btn)

        self._close_btn = QToolButton()
        self._close_btn.setText("✕")
        self._close_btn.setObjectName("PanelClose")
        self._close_btn.clicked.connect(self._request_close)
        bl.addWidget(self._close_btn)
        outer.addWidget(self._bar)

        # ---------- 历史滚动正文 ----------
        self._scroll = QScrollArea(self)
        self._scroll.setObjectName("PanelScroll")
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._body = QWidget()
        self._rows_lay = QVBoxLayout(self._body)
        self._rows_lay.setContentsMargins(8, 4, 14, 4)
        self._rows_lay.setSpacing(10)
        self._rows_lay.addStretch(1)
        self._scroll.setWidget(self._body)
        outer.addWidget(self._scroll, 1)
        self._scroll.verticalScrollBar().valueChanged.connect(self._on_scroll)

        self._sync_bar_texts()
        self._apply_qss()
        self.resize(560, 150)

    # ---------- 内容 ----------

    def _add_row(self, src, tgt, pending):
        row = QWidget(self._body)
        v = QVBoxLayout(row)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(2)
        s = QLabel(src)
        s.setObjectName("PanelSrc")
        s.setWordWrap(True)
        t = QLabel(tgt)
        t.setObjectName("PanelTgt")
        t.setWordWrap(True)
        s.setVisible(bool(src) and self._show_source)
        v.addWidget(s)
        v.addWidget(t)
        self._rows_lay.insertWidget(self._rows_lay.count() - 1, row)
        item = {"row": row, "src": s, "tgt": t,
                "src_text": src, "tgt_text": tgt, "pending": pending}
        self._rows.append(item)
        while len(self._rows) > self.MAX_ROWS:
            old = self._rows.pop(0)
            old["row"].setParent(None)
            old["row"].deleteLater()
        self._relayout()
        return item

    def show_pending(self, source_text):
        """识别文本先上屏，译文占位（两段式，兼容旧主窗调用）。"""
        if self._pending_row is not None and self._pending_row["pending"]:
            r = self._pending_row
            r["src_text"] = source_text
            r["src"].setText(source_text)
            r["src"].setVisible(bool(source_text) and self._show_source)
            r["tgt"].setText("⟳ …")
            self._relayout()
            return
        self._pending_row = self._add_row(source_text, "⟳ …", True)

    def show_pending_result(self, source_text, target_text, show_source=True):
        """译文就绪：补齐占位行或新建完成行。"""
        self._show_source = bool(show_source)
        self._last_result = (source_text or "", target_text or "")
        r = self._pending_row
        if r is not None and r["pending"]:
            r["pending"] = False
            r["src_text"], r["tgt_text"] = source_text or "", target_text or ""
            r["src"].setText(source_text or "")
            r["src"].setVisible(bool(source_text) and self._show_source)
            r["tgt"].setText(target_text or "")
            self._pending_row = None
            self._relayout()
        else:
            self._add_row(source_text or "", target_text or "", False)
        self._sync_bar_texts()

    def show_caption(self, source_text, target_text, show_source=True):
        """一次性上屏（无占位）。"""
        self._show_source = bool(show_source)
        self._last_result = (source_text or "", target_text or "")
        self._add_row(source_text or "", target_text or "", False)
        self._sync_bar_texts()

    def clear_caption(self):
        for it in self._rows:
            it["row"].setParent(None)
            it["row"].deleteLater()
        self._rows = []
        self._pending_row = None
        self._last_result = ("", "")
        self._relayout()

    def set_status(self, text, is_error=False):
        # 状态列宽度主权让位：截短 + tooltip 全文（520px 面板实测长文案会盖住 ⋯）
        t = (text or "").strip()
        if len(t) > 10:
            t = t[:9] + "…"
        self.status_lbl.setText(t)
        self.status_lbl.setToolTip(text or "")
        self.status_lbl.setStyleSheet(
            "color: #fbbf24;" if is_error else "color: rgba(255,255,255,120);")

    # ---------- 尺寸与跟随 ----------

    def minimumSizeHint(self):
        # v2.4.0 实机验收：布局最小宽度（按钮 sizeHint 总和）会把 resize 钳到
        # ~590px，用户 450px 的宽度"恢复成功后又被内容顶开"。最小宽度只认
        # MIN_W——窄于按钮总和时尾部按钮裁切，可接受（宽度主权归用户）。
        return QSize(self.MIN_W, 40)

    def _relayout(self):
        if self._collapsed:
            self._scroll.setVisible(False)
        else:
            self._scroll.setVisible(True)
            want = self._body.sizeHint().height() + 8
            cap = int((QGuiApplication.primaryScreen().availableGeometry().height()
                       or 800) * 0.55)
            self._scroll.setFixedHeight(min(max(want, 46), cap))
            self._scroll.setVerticalScrollBarPolicy(
                Qt.ScrollBarAlwaysOff if want <= cap else Qt.ScrollBarAsNeeded)
        # v2.4.0 实机验收抓到的宽度跳变：adjustSize 会按内容重排宽度（722→432→698）。
        # 契约修正：宽度只认用户（初值/右缘拖拽/持久化恢复），高度才跟内容走。
        w0 = self.width()
        self.adjustSize()
        if w0 >= self.MIN_W:
            self.resize(w0, self.height())
        if self._follow:
            QTimer.singleShot(0, self._scroll_bottom)

    def _on_scroll(self, v):
        sb = self._scroll.verticalScrollBar()
        self._follow = (v >= sb.maximum() - 4)
        self._jump_btn.setVisible(not self._follow and not self._collapsed)

    def _scroll_bottom(self):
        sb = self._scroll.verticalScrollBar()
        sb.setValue(sb.maximum())
        self._follow = True
        self._jump_btn.setVisible(False)

    # ---------- 工具条动作 ----------

    def _sync_bar_texts(self):
        # 文案从紧（520px 实测：长文案挤没 ⋯/收起/✕）
        self._src_btn.setText("原文 开" if self._show_source else "原文 关")
        self._font_btn.setText(f"Aa {self._font_size} ▾")
        lang = dict(self.LANGS).get(self._target_lang, self._target_lang)
        self._lang_btn.setText(f"🌐 {lang}")
        self._collapse_btn.setText("展开" if self._collapsed else "收起")

    def _toggle_src(self):
        # 与旧"只显示译文"开关同源：回调收到的是 show_source 取反
        if self._on_toggle_translation_only:
            self._on_toggle_translation_only(self._show_source)
        else:
            self.set_show_source(not self._show_source)

    def set_show_source(self, on):
        """外部（设置页/回调）同步原文开关的显示态。"""
        self._show_source = bool(on)
        for it in self._rows:
            it["src"].setVisible(bool(it["src_text"]) and self._show_source)
        self._sync_bar_texts()
        self._relayout()

    def _build_lang_menu(self):
        m = QMenu(self)
        for code, name in self.LANGS:
            a = m.addAction(name)
            a.setCheckable(True)
            a.setChecked(code == self._target_lang)
            a.triggered.connect(lambda _c=False, c=code: self._pick_lang(c))
        return m

    def _pick_lang(self, code):
        self.set_target_lang(code)
        if self._on_language:
            self._on_language(code)

    def set_target_lang(self, code):
        self._target_lang = code
        self._lang_btn.setMenu(self._build_lang_menu())
        self._sync_bar_texts()

    def _build_font_menu(self):
        m = QMenu(self)
        for name, px in self.FONTS:
            a = m.addAction(f"{name}（{px}px）")
            a.setCheckable(True)
            a.setChecked(abs(px - self._font_size) <= 3)
            a.triggered.connect(lambda _c=False, p=px: self._pick_font(p))
        return m

    def _pick_font(self, px):
        if self._on_font_size:
            self._on_font_size(int(px))
        else:
            self._font_size = int(px)
            self._apply_qss()
            self._sync_bar_texts()
            self._relayout()

    def _toggle_collapse(self):
        self.set_collapsed(not self._collapsed)
        if self._on_collapsed:
            self._on_collapsed(self._collapsed)

    def set_collapsed(self, on):
        self._collapsed = bool(on)
        self._sync_bar_texts()
        self._jump_btn.setVisible(not self._follow and not self._collapsed)
        self._relayout()

    def set_pinned(self, on):
        on = bool(on)
        if on == self._pinned:
            return
        self._pinned = on
        vis = self.isVisible()
        flags = Qt.FramelessWindowHint | Qt.Tool
        if on:
            flags |= Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        if vis:
            self.show()

    def is_pinned(self):
        return self._pinned

    def _request_close(self):
        if self._on_closed:
            self._on_closed()
        else:
            self.hide()

    # ---------- 样式 ----------

    def apply_style(self, font_size, text_color, bg_color, bg_opacity):
        self._font_size = max(10, int(font_size))
        self._text_color = QColor(text_color)
        self._bg_color = QColor(bg_color)
        self._bg_alpha = int(max(0, min(100, int(bg_opacity))) * 2.55)
        self._apply_qss()
        self._sync_bar_texts()
        self._relayout()

    def _apply_qss(self):
        self.setStyleSheet(f"""
            QWidget#SubtitlePanel {{ background: transparent; }}
            QWidget#PanelToolbar {{ background: rgba(255,255,255,16); border-radius: 8px; }}
            QToolButton {{ color: #cfd6e4; background: transparent; border: none;
                           padding: 2px 5px; font-size: 12px; border-radius: 6px; }}
            QToolButton:hover {{ background: rgba(255,255,255,30); }}
            QToolButton#PanelClose {{ color: #ff8f8f; }}
            QScrollArea#PanelScroll {{ background: transparent; border: none; }}
            QScrollBar:vertical {{ width: 8px; background: transparent; }}
            QScrollBar::handle:vertical {{ background: rgba(255,255,255,70); border-radius: 4px; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
        """)

    def paintEvent(self, event):
        if not hasattr(self, "_bg_color"):
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        bg = QColor(self._bg_color)
        bg.setAlpha(self._bg_alpha)
        p.setPen(Qt.NoPen)
        p.setBrush(bg)
        r = self.rect().adjusted(0, 0, -1, -1)
        p.drawRoundedRect(r, 12, 12)

    # ---------- 鼠标：整板拖移 + 右缘调宽 + 双击贴边 ----------

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            if event.position().x() >= self.width() - 10 and not self._collapsed:
                self._resizing = True
                self._resize_start = event.globalPosition().toPoint()
                self._resize_start_w = self.width()
            else:
                self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._resizing and event.buttons() & Qt.LeftButton:
            w = max(self.MIN_W, self._resize_start_w
                    + (event.globalPosition().x() - self._resize_start.x()))
            self.resize(w, self.height())
            self._user_resized = True
            event.accept()
            return
        if self._drag_pos is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()
            return
        self.setCursor(Qt.SizeHorCursor
                       if (event.position().x() >= self.width() - 10 and not self._collapsed)
                       else Qt.ArrowCursor)

    def mouseReleaseEvent(self, event):
        if self._resizing:
            self._resizing = False
            if self._on_resized:
                self._on_resized(self.width())
        elif self._drag_pos is not None:
            QTimer.singleShot(0, self._save_final_pos)
            QTimer.singleShot(400, self._save_final_pos)
        self._drag_pos = None

    def _save_final_pos(self):
        if self._on_moved:
            self._on_moved(self.x(), self.y())

    def mouseDoubleClickEvent(self, event):
        # 双击工具条 = 顶/底贴边循环（肌肉记忆：像所有软件的标题栏）
        if event.position().y() <= self._bar.geometry().bottom() + 6:
            self._snap_cycle()

    def _snap_cycle(self):
        self._snap_to_edge("bottom" if self.y() - (
            QGuiApplication.primaryScreen().availableGeometry().top()
        ) < QGuiApplication.primaryScreen().availableGeometry().height() / 2 else "top")

    def _snap_to_edge(self, edge):
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

    # ---------- 菜单（⋯ 与右键同源） ----------

    def _build_menu(self):
        menu = QMenu(self)
        acts = {}
        acts["settings"] = menu.addAction("打开设置…")
        acts["source"] = menu.addAction("切换输入来源")
        menu.addSeparator()
        acts["copy"] = menu.addAction("复制最近一句")
        acts["fix_asr"] = menu.addAction("纠正最近识别…")
        acts["fix_tr"] = menu.addAction("纠正最近译文…")
        acts["export"] = menu.addAction("导出 SRT…")
        menu.addSeparator()
        acts["pin"] = menu.addAction("置顶显示")
        acts["pin"].setCheckable(True)
        acts["pin"].setChecked(self._pinned)
        acts["snap_top"] = menu.addAction("贴到屏幕顶部")
        acts["snap_bottom"] = menu.addAction("贴到屏幕底部")
        acts["snap_left"] = menu.addAction("贴到屏幕左侧")
        acts["snap_right"] = menu.addAction("贴到屏幕右侧")
        menu.addSeparator()
        acts["hide"] = menu.addAction("隐藏字幕面板")
        src, tgt = self._last_result
        acts["copy"].setEnabled(bool(src.strip() or tgt.strip()))
        acts["fix_asr"].setEnabled(bool(src.strip()))
        acts["fix_tr"].setEnabled(bool(tgt.strip()))
        self._menu_acts = acts
        self._menu_last = (src, tgt)
        return menu

    def _menu_dispatch(self, chosen):
        acts = getattr(self, "_menu_acts", {})
        src, tgt = getattr(self, "_menu_last", ("", ""))
        if chosen is None:
            return
        if chosen == acts.get("settings"):
            if self._on_open_settings:
                self._on_open_settings()
        elif chosen == acts.get("source"):
            if self._on_toggle_source:
                self._on_toggle_source()
        elif chosen == acts.get("copy"):
            QApplication.clipboard().setText(f"{src}\n{tgt}".strip())
        elif chosen == acts.get("fix_asr"):
            if self._on_correct:
                self._on_correct("mishear_map", src)
        elif chosen == acts.get("fix_tr"):
            if self._on_correct:
                self._on_correct("translate_fix_map", tgt)
        elif chosen == acts.get("export"):
            if self._on_export_srt:
                self._on_export_srt()
        elif chosen == acts.get("pin"):
            self.set_pinned(acts["pin"].isChecked())
            if self._on_pin_changed:
                self._on_pin_changed(acts["pin"].isChecked())
        elif chosen == acts.get("snap_top"):
            self._snap_to_edge("top")
        elif chosen == acts.get("snap_bottom"):
            self._snap_to_edge("bottom")
        elif chosen == acts.get("snap_left"):
            self._snap_to_edge("left")
        elif chosen == acts.get("snap_right"):
            self._snap_to_edge("right")
        elif chosen == acts.get("hide"):
            self._request_close()

    def _show_more_menu(self):
        menu = self._build_menu()
        self._menu_dispatch(menu.exec(self._more_btn.mapToGlobal(
            QPoint(0, self._more_btn.height()))))
        menu.deleteLater()

    def contextMenuEvent(self, event):
        menu = self._build_menu()
        self._menu_dispatch(menu.exec(event.globalPos()))
        menu.deleteLater()
