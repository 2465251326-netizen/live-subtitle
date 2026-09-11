"""v2.4.0 字幕面板（用户裁决：旧字幕条退役，工具条+历史滚动面板上位）。

形态：深灰圆角不透明面板。顶部工具条 = 🌐目标语言▾ | 关闭原文 | Aa字号▾ |
状态 | ↓最新 | 清空 | ⋯ | 📌 | 收起 | ✕；正文 = "原文(灰) + 译文(白加粗)"
成对左对齐的历史滚动区，自动跟随最新，上滚暂停跟随（v2.4.3 起非跟随时
"↓最新"按新到句数计数，回底归零）。空闲时正文显示占位提示，首次使用升级
为手势引导（每份配置只弹一次，主窗按 overlay_hint_shown 控制）。

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
    RESIZE_EDGE = 14   # v2.4.1：右缘调宽命中带（10px 太窄且无光标反馈→普通人找不到）
    MAX_ROWS = 40
    LANGS = [("zh-CN", "中文"), ("en", "英语"), ("ja", "日语"), ("ko", "韩语"),
             ("fr", "法语"), ("de", "德语"), ("ru", "俄语"), ("es", "西班牙语")]
    FONTS = [("小号", 16), ("中号", 22), ("大号", 30), ("特大", 40)]
    # v2.4.3（B/D）：空状态占位。亮度压在 rgba(255,255,255,72)——混到深底上仍
    # <RGB(120,120,120)，不触碰 v2.4.2"空闲正文无浅灰块"像素回归锁的阈值
    HINT_IDLE = "字幕将在这里逐句显示"
    HINT_GUIDE = ("首次使用小抄：拖工具条移动面板 · 拖右缘改宽度\n"
                  "双击工具条贴屏幕顶/底 · 右键或 ⋯ 打开更多操作\n"
                  "字幕将在这里逐句显示")

    def __init__(self, on_closed=None, on_moved=None,
                 on_open_settings=None, on_toggle_source=None,
                 on_toggle_translation_only=None, on_resized=None,
                 on_correct=None, on_export_srt=None, on_language=None,
                 on_font_size=None, on_pin_changed=None, on_collapsed=None,
                 on_first_show=None, on_opacity=None):
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
        self._unread = 0            # v2.4.3（E）：非跟随时新到句数
        self._hint_guide = False    # v2.4.3（D）：空状态文案是否升级为手势引导
        self._first_show_seen = False
        self._relayout_pending = False
        self._mini_press = False    # v2.5.0：精简条点击展开判定锚
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
        self._on_first_show = on_first_show
        self._on_opacity = on_opacity

        self._pinned = True
        self._apply_window_flags()   # v2.5.1（P2）：统一窗口标志（清 Tool 隐含的拒绝焦点）
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        # v2.4.1：开鼠标追踪——否则不按住鼠标收不到 move 事件，右缘"↔"调宽光标
        # 永不显示（用户实测"无法手动调大小"的直接原因之一：够不着也看不见）。
        self.setMouseTracking(True)
        # v2.5.2（R1）：面板级 tooltip 移除——悬停弹出的原生 tooltip 窗口会拦截
        # 整板点击（拖动/按钮全失灵）；手势说明由首次手势引导（D）承担

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, self.RESIZE_EDGE, 10)
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

        # v2.4.3（A）：一键清空面板历史（⋯/右键菜单同源"清空面板字幕"）；
        # 只动面板行，不碰主窗历史与 SRT 导出
        self._clear_btn = QToolButton()
        self._clear_btn.setText("清空")
        self._clear_btn.setToolTip("清空面板字幕（主窗历史与导出不受影响）")
        self._clear_btn.clicked.connect(self.clear_caption)
        bl.addWidget(self._clear_btn)

        self._more_btn = QToolButton()
        self._more_btn.setText("⋯")
        self._more_btn.clicked.connect(self._show_more_menu)
        bl.addWidget(self._more_btn)

        # v2.4.3（C）：置顶图钉上工具条——高频模式不该藏在 ⋯ 二级里；
        # 与菜单"置顶显示"同源（set_pinned 内双向同步）
        self._pin_btn = QToolButton()
        self._pin_btn.setText("📌")
        self._pin_btn.setCheckable(True)
        self._pin_btn.setToolTip("置顶显示：开 = 面板始终浮在其他窗口之上")
        self._pin_btn.clicked.connect(self._toggle_pin)
        bl.addWidget(self._pin_btn)

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
        # v2.4.1：QScrollArea 设了透明还不够——它的 viewport 是独立子控件，
        # 不被 `QScrollArea{...}` 那条 QSS 命中，默认浅色底会在空闲时露成一块
        # 灰矩形（实机截图实证）。viewport 连父级 QSS 的 `> QWidget` 都不吃，
        # 必须直接给它和正文容器挂 WA_TranslucentBackground。
        self._scroll.viewport().setAutoFillBackground(False)
        self._scroll.viewport().setAttribute(Qt.WA_TranslucentBackground, True)
        self._body = QWidget()
        self._body.setAutoFillBackground(False)
        self._body.setAttribute(Qt.WA_TranslucentBackground, True)
        self._rows_lay = QVBoxLayout(self._body)
        self._rows_lay.setContentsMargins(8, 4, 14, 4)
        self._rows_lay.setSpacing(6)
        # v2.4.3（B）：空状态占位提示——空闲不再是一片空白（index 0 = 恒在行区上方）
        self._hint = QLabel("")
        self._hint.setObjectName("PanelHint")
        self._hint.setWordWrap(True)
        self._rows_lay.addStretch(1)
        self._rows_lay.insertWidget(0, self._hint)
        self._scroll.setWidget(self._body)
        outer.addWidget(self._scroll, 1)
        self._scroll.verticalScrollBar().valueChanged.connect(self._on_scroll)

        # v2.5.0：精简条（对标豆包"收起=最新一句小条"）——收起态不再只剩工具条，
        # 正文区换成最新句的两行速览（原文小灰 + 译文大白），单击展开完整历史
        self._mini = QWidget(self)
        self._mini.setObjectName("PanelMini")
        mini_l = QVBoxLayout(self._mini)
        mini_l.setContentsMargins(10, 2, 10, 4)
        mini_l.setSpacing(0)
        self._mini_src = QLabel("")
        self._mini_src.setObjectName("PanelMiniSrc")
        self._mini_tgt = QLabel("")
        self._mini_tgt.setObjectName("PanelMiniTgt")
        self._mini_tgt.setWordWrap(True)
        mini_l.addWidget(self._mini_src)
        mini_l.addWidget(self._mini_tgt)
        self._mini.setCursor(Qt.PointingHandCursor)
        outer.addWidget(self._mini)
        self._mini.hide()

        self._pin_btn.setChecked(self._pinned)
        self._sync_unread_btn()
        self._update_empty_hint()
        self._sync_bar_texts()
        self._apply_qss()
        self.resize(560, 150)

    # ---------- 内容 ----------

    def _add_row(self, src, tgt, pending):
        # v2.4.4（BUG-7）：字幕到来 = 引导完成使命，复位后空状态回退单行占位——
        # 此前 _hint_guide 一经置位永久生效，"清空"后每次都弹三行小抄，
        # 与"首次只弹一次"的设计语义冲突
        self._hint_guide = False
        # v2.5.0：行卡片化 + 最新行左侧主题色竖条（对标豆包"新句有强调"）
        if self._rows:
            old = self._rows[-1]["row"]
            old.setObjectName("PanelRow")
            old.style().unpolish(old)
            old.style().polish(old)
        row = QWidget(self._body)
        row.setObjectName("PanelRowNewest")
        v = QVBoxLayout(row)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(1)
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
        if self._collapsed:
            self._update_mini()          # v2.5.0：精简条跟随最新句
        self._update_empty_hint()
        self._relayout()
        self._schedule_relayout()
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
            self._schedule_relayout()
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
            if self._collapsed:
                self._update_mini()   # v2.5.0：精简条正在显示这句占位时同步成译文
            self._relayout()
            self._schedule_relayout()
        else:
            self._add_row(source_text or "", target_text or "", False)
        self._count_unread()
        self._sync_bar_texts()

    def show_caption(self, source_text, target_text, show_source=True):
        """一次性上屏（无占位）。"""
        self._show_source = bool(show_source)
        self._last_result = (source_text or "", target_text or "")
        self._add_row(source_text or "", target_text or "", False)
        self._count_unread()
        self._sync_bar_texts()

    def clear_caption(self):
        for it in self._rows:
            it["row"].setParent(None)
            it["row"].deleteLater()
        self._rows = []
        self._pending_row = None
        self._last_result = ("", "")
        self._unread = 0
        self._sync_unread_btn()
        self._update_empty_hint()
        self._update_mini()
        self._relayout()
        self._schedule_relayout()

    # ---------- v2.4.3：空状态占位 / 未读计数 ----------

    def _update_empty_hint(self):
        """B/D：无行时显示占位（或首次手势引导），来字即隐；顺带门控清空按钮。"""
        self._hint.setText(self.HINT_GUIDE if self._hint_guide else self.HINT_IDLE)
        self._hint.setVisible(not self._rows)
        self._clear_btn.setEnabled(bool(self._rows))

    def _count_unread(self):
        """E：非跟随时每完成一句计数 +1（占位行不算，只数出结果的新句）。"""
        if not self._follow:
            self._unread += 1
            self._sync_unread_btn()

    def _sync_unread_btn(self):
        n = self._unread
        self._jump_btn.setText(f"↓ 最新 {n}" if n else "↓ 最新")
        self._jump_btn.setStyleSheet("color: #ff8f8f;" if n else "color: #cfd6e4;")

    def set_status(self, text, is_error=False):
        # 状态列宽度主权让位：截短 + 限宽（520px 面板实测长文案会盖住 ⋯）
        self._cap_status_width()
        t = (text or "").strip()
        if len(t) > 10:
            t = t[:9] + "…"
        self.status_lbl.setText(t)
        # v2.5.2（R1）：不再设 tooltip——悬停 1s 弹出的原生 tooltip 窗口会拦截
        # 光标区域后续所有点击（随机测试实锤：清空/⋯/📌/收起/✕ 全部"点了没反应"
        # = tooltip 窗口吃事件）。全文保留在主窗状态行（同源文本）
        self.status_lbl.setToolTip("")
        self.status_lbl.setStyleSheet(
            "color: #fbbf24;" if is_error else "color: rgba(255,255,255,120);")

    # ---------- 尺寸与跟随 ----------

    def _schedule_relayout(self):
        """v2.4.3：插入/改文本当拍 QLabel 的 sizeHint 还没定型（实机插桩：新行
        读出 8px，事件循环后才是 139px）——立即 _relayout 只能定出过期高度，
        面板"高度贴内容"实际滞后一拍甚至停在空闲高度。排期即消耗（v2.4.1
        教训：守卫防链式重排）补延迟复排；换行宽度跨事件拍还会变，高度未
        收敛就再排一拍（封顶 8 拍防振荡环）。"""
        if self._relayout_pending:
            return
        self._relayout_pending = True
        self._relayout_passes = 0
        QTimer.singleShot(0, self._consume_relayout)

    def _consume_relayout(self):
        # v2.5.0（F2）：收敛判定改看"内容需求高度"——此前只比较 scroll 固定高，
        # 而 scroll 被 min(46) 钳住期间 body.sizeHint 仍在多拍变化，链提前断，
        # 只译文模式实测 5 行塌成 2.5 行
        want0 = self._body.sizeHint().height()
        h0 = self._scroll.height()
        self._relayout()
        if ((self._body.sizeHint().height() != want0
             or self._scroll.height() != h0) and self._relayout_passes < 12):
            self._relayout_passes += 1
            self._relayout_pending = True
            QTimer.singleShot(0, self._consume_relayout)

    def minimumSizeHint(self):
        # v2.4.0 实机验收：布局最小宽度（按钮 sizeHint 总和）会把 resize 钳到
        # ~590px，用户 450px 的宽度"恢复成功后又被内容顶开"。最小宽度只认
        # MIN_W——窄于按钮总和时尾部按钮裁切，可接受（宽度主权归用户）。
        return QSize(self.MIN_W, 40)

    def _relayout(self):
        self._cap_status_width()
        if self._collapsed:
            # v2.5.0：精简条模式——正文区换成最新句速览，高度贴两行内容
            self._scroll.setVisible(False)
            self._mini.setVisible(True)
            self._mini.setFixedHeight(min(self._mini.sizeHint().height(), 120))
        else:
            self._mini.setVisible(False)
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
        if self._follow and self._unread:      # E：回到最新处即清零
            self._unread = 0
            self._sync_unread_btn()
        self._jump_btn.setVisible(not self._follow and not self._collapsed)

    def _scroll_bottom(self):
        sb = self._scroll.verticalScrollBar()
        sb.setValue(sb.maximum())
        self._follow = True
        self._unread = 0
        self._sync_unread_btn()
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
        if self._collapsed:
            self._update_mini()
        self._sync_bar_texts()
        self._relayout()
        self._schedule_relayout()

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

    def _update_mini(self):
        """v2.5.0：精简条内容 = 最新一句（尊重原文开关）；无字幕给引导占位。"""
        if self._rows:
            it = self._rows[-1]
            src = it["src_text"] if self._show_source else ""
            self._mini_src.setText(src)
            self._mini_src.setVisible(bool(src))
            self._mini_tgt.setText(it["tgt_text"] or "⟳ …")
        else:
            self._mini_src.setVisible(False)
            self._mini_tgt.setText("暂无字幕 · 单击展开")

    def set_collapsed(self, on):
        self._collapsed = bool(on)
        self._sync_bar_texts()
        self._jump_btn.setVisible(not self._follow and not self._collapsed)
        self._scroll.setVisible(not self._collapsed)
        self._mini.setVisible(self._collapsed)
        if self._collapsed:
            self._update_mini()
        self._relayout()

    def _toggle_pin(self):
        # v2.4.3（C）：工具条图钉与 ⋯ 菜单"置顶显示"同一落点（set_pinned 双向同步）
        on = not self._pinned
        self.set_pinned(on)
        if self._on_pin_changed:
            self._on_pin_changed(on)

    def set_pinned(self, on):
        on = bool(on)
        if on == self._pinned:
            return
        self._pinned = on
        self._pin_btn.setChecked(on)
        vis = self.isVisible()
        self._apply_window_flags()
        if vis:
            self.show()

    def _apply_window_flags(self):
        """v2.5.1（P2）：清除 Qt.Tool 隐含的 WindowDoesNotAcceptFocus——
        隐含标志让面板点击也不激活（前台始终停在视频/浏览器），系统滚轮
        全部发给焦点窗口，Ctrl+滚轮调节与滚轮滚动在真实使用中永远失效
        （实测：点击面板后前台仍是 Chrome）。允许激活后：点面板=面板前台=
        滚轮可用。构造时也走本函数统一维护。"""
        flags = Qt.FramelessWindowHint | Qt.Tool
        if self._pinned:
            flags |= Qt.WindowStaysOnTopHint
        flags &= ~Qt.WindowType.WindowDoesNotAcceptFocus
        self.setWindowFlags(flags)

    def is_pinned(self):
        return self._pinned

    def _request_close(self):
        # v2.4.1 回归修复：v2.4.0 重写时把 hide() 误塞进 else 分支，而主窗永远
        # 传 on_closed 回调 → else 永不执行 → 点✕只改配置不隐藏（用户实测"没反应"）。
        self.hide()
        if self._on_closed:
            self._on_closed()

    def showEvent(self, event):
        super().showEvent(event)
        # v2.4.3（D）：本进程首次显示只发一次回调；主窗按 overlay_hint_shown
        # 决定是否升级为手势引导（热键/设置预览/启动恢复全走 showEvent，无需逐处补调用）
        if not self._first_show_seen:
            self._first_show_seen = True
            if self._on_first_show:
                self._on_first_show()

    def show_first_hint(self):
        """D：空状态文案升级为手势引导（主窗按配置只调一次）。"""
        self._hint_guide = True
        self._update_empty_hint()
        self._relayout()
        self._schedule_relayout()

    # ---------- 样式 ----------

    def apply_style(self, font_size, text_color, bg_color, bg_opacity):
        self._font_size = max(10, int(font_size))
        self._text_color = QColor(text_color)
        self._bg_color = QColor(bg_color)
        self._bg_alpha = int(max(0, min(100, int(bg_opacity))) * 2.55)
        self._apply_qss()
        self._sync_bar_texts()
        self._relayout()
        self._schedule_relayout()

    def _apply_qss(self):
        # v2.5.0：字号真实生效——v2.4.0 面板化时正文（PanelSrc/PanelTgt）从未
        # 挂过 font-size 规则，设置页/工具条调字号完全无效（深度实测截图矩阵
        # 16/30/40 三档像素级相同才暴露）。同时对齐豆包式双行层次：原文=主字号
        # 72%、更淡的灰，译文=主字号加粗白，扫读时主次分明。
        fs = int(self._font_size)
        src_fs = max(11, int(fs * 0.72))
        self.setStyleSheet(f"""
            QWidget#SubtitlePanel {{ background: transparent; }}
            QWidget#PanelToolbar {{ background: rgba(255,255,255,16); border-radius: 8px; }}
            QToolButton {{ color: #cfd6e4; background: transparent; border: none;
                           padding: 2px 5px; font-size: 12px; border-radius: 6px; }}
            QToolButton:hover {{ background: rgba(255,255,255,30); }}
            QToolButton:checked {{ background: rgba(255,255,255,45); }}
            QToolButton:disabled {{ color: rgba(255,255,255,60); }}
            QToolButton#PanelClose {{ color: #ff8f8f; }}
            QLabel#PanelHint {{ color: rgba(255,255,255,72); font-size: 12px; }}
            QLabel#PanelSrc {{ font-size: {src_fs}px; color: #98a2b3; }}
            QLabel#PanelTgt {{ font-size: {fs}px; color: {self._text_color.name()}; font-weight: 600; }}
            QLabel#PanelMiniSrc {{ font-size: {max(11, int(fs * 0.62))}px; color: #98a2b3; }}
            QLabel#PanelMiniTgt {{ font-size: {fs}px; color: {self._text_color.name()}; font-weight: 600; }}
            QWidget#PanelRow {{ background: rgba(255,255,255,8); border-radius: 8px;
                                border-left: 3px solid transparent; }}
            QWidget#PanelRowNewest {{ background: rgba(79,140,255,26); border-radius: 8px;
                                      border-left: 3px solid #4f8cff; }}
            QWidget#PanelMini {{ background: transparent; }}
            QScrollArea#PanelScroll {{ background: transparent; border: none; }}
            QScrollArea#PanelScroll > QWidget {{ background: transparent; }}
            QScrollArea#PanelScroll > QWidget > QWidget {{ background: transparent; }}
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
        # v2.4.1：右缘"⋮"把手——调宽从隐形手势变看得见（体验报告的"加提示"）
        if not self._collapsed:
            grip = QColor(255, 255, 255, 90 if not self._resizing else 180)
            p.setBrush(grip)
            cx = self.width() - self.RESIZE_EDGE // 2 - 1
            cy = self.height() // 2
            for dy in (-7, 0, 7):
                p.drawEllipse(QPoint(cx - 1, cy + dy - 1), 1, 1)

    # ---------- 鼠标：整板拖移 + 右缘调宽 + 双击贴边 ----------

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            if event.position().x() >= self.width() - self.RESIZE_EDGE and not self._collapsed:
                self._resizing = True
                self._resize_start = event.globalPosition().toPoint()
                self._resize_start_w = self.width()
            else:
                self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                # v2.5.0：精简条上"按住=拖、原地点击=展开"的判定锚点
                self._mini_press = (self._collapsed
                                    and event.position().y() > self._bar.geometry().bottom())
                _p = event.position()
                self._mini_press_at = _p.toPoint() if hasattr(_p, "toPoint") else _p
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
                       if (event.position().x() >= self.width() - self.RESIZE_EDGE and not self._collapsed)
                       else Qt.ArrowCursor)

    def mouseReleaseEvent(self, event):
        if self._resizing:
            self._resizing = False
            if self._on_resized:
                self._on_resized(self.width())
        elif self._drag_pos is not None:
            pos = event.position()
            pos = pos.toPoint() if hasattr(pos, "toPoint") else pos
            # v2.5.0：精简条原地点击（位移<6px）= 展开完整历史；真拖动不触发
            if (getattr(self, "_mini_press", False)
                    and (pos - getattr(self, "_mini_press_at", pos)).manhattanLength() < 6):
                self._mini_press = False
                self.set_collapsed(False)
                if self._on_collapsed:
                    self._on_collapsed(False)
            else:
                self._magnet_snap()
                QTimer.singleShot(0, self._save_final_pos)
                QTimer.singleShot(400, self._save_final_pos)
        self._drag_pos = None
        self._mini_press = False

    def _magnet_snap(self):
        """v2.5.0：拖动松手磁吸——面板边缘距屏幕可用区边缘 <24px 时自动贴齐
        （此前只能靠右键菜单/双击贴边，随手一拖永远对不齐）。"""
        scr = (QGuiApplication.screenAt(QPoint(self.frameGeometry().center()))
               or QGuiApplication.primaryScreen())
        g = scr.availableGeometry()
        x, y, w, h = self.x(), self.y(), self.width(), self.height()
        nx, ny = x, y
        if abs(x - g.left()) < 24:
            nx = g.left()
        elif abs(x + w - g.right()) < 24:
            nx = g.right() - w
        if abs(y - g.top()) < 24:
            ny = g.top()
        elif abs(y + h - g.bottom()) < 24:
            ny = g.bottom() - h
        if (nx, ny) != (x, y):
            self.move(nx, ny)

    def wheelEvent(self, event):
        # v2.5.0：工具条上快捷调节（豆包式顺手性）——Ctrl+滚轮=字号 ±1、
        # Ctrl+Shift+滚轮=透明度 ∓5；正文区滚轮保持历史滚动不受影响
        if event.position().y() <= self._bar.geometry().bottom() + 6:
            mods = event.modifiers()
            up = event.angleDelta().y() > 0
            if mods & Qt.ControlModifier and mods & Qt.ShiftModifier:
                cur = int(round(self._bg_alpha / 2.55))
                self._apply_opacity(int(max(30, min(100, cur + (5 if up else -5)))))
                event.accept()
                return
            if mods & Qt.ControlModifier:
                self._apply_font(int(max(12, min(48, self._font_size + (1 if up else -1)))))
                event.accept()
                return
        event.ignore()

    def _apply_font(self, px):
        if self._on_font_size:
            self._on_font_size(int(px))
        else:
            self._font_size = int(px)
            self._apply_qss()
            self._sync_bar_texts()
            self._relayout()
            self._schedule_relayout()

    def _apply_opacity(self, val):
        """v2.5.0：透明度快捷调节统一出口（滚轮/菜单档）。"""
        val = int(max(30, min(100, val)))
        if self._on_opacity:
            self._on_opacity(val)
        else:
            self._bg_alpha = int(val * 2.55)
            self.update()

    def _cap_status_width(self):
        # v2.5.0（F3）：状态行动态限宽——Ignored 策略下布局可把状态行压没，
        # 但 QLabel 溢出绘制会压到右侧按钮（360px 宽实测与"翻译失败"文字重叠）。
        # 限宽 = 面板宽 − 按钮区（约 240px），超长文字被裁剪，全文在 tooltip。
        # 注意：未 show 的 widget 收不到 Python resizeEvent（实测裸 QWidget 同样），
        # 故挂在 set_status/_relayout 这两个必经路径上而非 resizeEvent
        self.status_lbl.setMaximumWidth(max(60, self.width() - 240))

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
        acts["clear"] = menu.addAction("清空面板字幕")   # v2.4.3（A）：与工具条清空同源
        menu.addSeparator()
        acts["pin"] = menu.addAction("置顶显示")
        acts["pin"].setCheckable(True)
        acts["pin"].setChecked(self._pinned)
        acts["snap_top"] = menu.addAction("贴到屏幕顶部")
        acts["snap_bottom"] = menu.addAction("贴到屏幕底部")
        acts["snap_left"] = menu.addAction("贴到屏幕左侧")
        acts["snap_right"] = menu.addAction("贴到屏幕右侧")
        # v2.5.0：透明度常用档直调（滚轮 Ctrl+Shift 的菜单版；细调仍走设置页）
        op_menu = menu.addMenu("背景透明度")
        cur_op = int(round(self._bg_alpha / 2.55))
        for val in (60, 75, 85, 92, 100):
            a = op_menu.addAction(f"{val}%")
            a.setCheckable(True)
            a.setChecked(cur_op == val)
            a.triggered.connect(lambda _c=False, v=val: self._apply_opacity(v))
        menu.addSeparator()
        acts["hide"] = menu.addAction("隐藏字幕面板")
        src, tgt = self._last_result
        acts["copy"].setEnabled(bool(src.strip() or tgt.strip()))
        acts["fix_asr"].setEnabled(bool(src.strip()))
        acts["fix_tr"].setEnabled(bool(tgt.strip()))
        acts["clear"].setEnabled(bool(self._rows))
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
        elif chosen == acts.get("clear"):
            self.clear_caption()
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
