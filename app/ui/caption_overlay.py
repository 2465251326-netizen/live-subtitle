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

from PySide6.QtCore import Qt, QPoint, QTimer, QSize, QEvent
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QFont
from PySide6.QtWidgets import (
    QWidget, QLabel, QMenu, QToolButton, QScrollArea, QApplication,
    QVBoxLayout, QHBoxLayout, QSizePolicy, QFrame,
)


class CaptionOverlay(QWidget):
    MIN_W = 360
    RESIZE_EDGE = 14   # v2.4.1：右缘调宽命中带（10px 太窄且无光标反馈→普通人找不到）
    MAX_ROWS = 40
    MAX_DUAL_HIST = 30  # v2.14.0：dual 历史区上限行数（超出删最老，防无限增长）
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
                 on_first_show=None, on_opacity=None, on_height_changed=None,
                 on_layout_changed=None, on_dual_split=None):
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
        self._last_result = ("", "")
        # v2.11.0：面板布局模式——"list"=历史滚动列表（v2.4.0 起的形态），
        # "dual"=上下双语（用户要求新增，对标豆包 PC 实时翻译：上半原文随识别
        # 流式生长、下半译文随推测式翻译就地更新，无历史行）。主窗调用接口
        # （show_pending/show_pending_result/update_spec_result/…）零改动。
        self._layout_mode = "list"
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
        self._on_height_changed = on_height_changed
        self._on_layout_changed = on_layout_changed
        self._on_dual_split = on_dual_split
        # v2.5.3：手动高度（用户裁决回归——面板支持上下拉长）。None=自动贴内容；
        # 拖底缘/主窗配置恢复后锁定手动高度，⋯ 菜单可恢复自动
        self._user_height = None
        self._v_resizing = False
        self._v_resize_start = None
        self._v_resize_start_h = 0

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
        self._lang_btn.setToolTip("翻译目标语言")
        self._lang_btn.setMenu(self._build_lang_menu())
        bl.addWidget(self._lang_btn)

        self._src_btn = QToolButton()
        self._src_btn.clicked.connect(self._toggle_src)
        self._src_btn.setToolTip("切换：只看译文 / 译文+原文")
        bl.addWidget(self._src_btn)

        self._font_btn = QToolButton()
        self._font_btn.setPopupMode(QToolButton.InstantPopup)
        self._font_btn.setToolTip("面板字号")
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
        self._jump_btn.setToolTip("滚动到最新一条字幕")
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
        self._more_btn.setToolTip("更多操作（导出、置顶、贴边、透明度等）")
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
        self._collapse_btn.setToolTip("收起为迷你条（只显示最新一句）")
        self._collapse_btn.clicked.connect(self._toggle_collapse)

        self._close_btn = QToolButton()
        self._close_btn.setText("✕")
        self._close_btn.setObjectName("PanelClose")
        self._close_btn.setToolTip("关闭面板（可从主窗重新打开）")
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

        # ---------- v2.11.0：上下双语正文（dual 布局模式） ----------
        # 对标豆包 PC 实时翻译：上半=原文（识别片段流式生长，淡色小字），
        # 下半=译文（推测式翻译就地更新、终版收口，主字号加粗）。
        # 与列表模式互斥显示；工具条/拖动/置顶/透明度/收起全部复用。
        # v2.14.0：加**历史区**——终版句自动沉入历史（原文小灰+译文小白成对），
        # 滚轮上下回看；当前句大字区保持流式实时。用户需求："原文和译文不是
        # 都应该保留吗，可以用滚轮进行滚上滚下查看"。
        self._dual_hist = QScrollArea(self)
        self._dual_hist.setObjectName("PanelDualHist")
        self._dual_hist.setWidgetResizable(True)
        self._dual_hist.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._dual_hist.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._dual_hist.viewport().setAutoFillBackground(False)
        self._dual_hist.viewport().setAttribute(Qt.WA_TranslucentBackground, True)
        self._dual_hist_body = QWidget()
        self._dual_hist_body.setAutoFillBackground(False)
        self._dual_hist_body.setAttribute(Qt.WA_TranslucentBackground, True)
        self._dual_hist_lay = QVBoxLayout(self._dual_hist_body)
        self._dual_hist_lay.setContentsMargins(10, 2, 14, 2)
        self._dual_hist_lay.setSpacing(7)
        self._dual_hist_lay.addStretch(1)
        self._dual_hist.setWidget(self._dual_hist_body)
        outer.addWidget(self._dual_hist, 1)
        self._dual_hist.hide()
        self._dual_hist_rows = 0           # 历史行数（上限 MAX_DUAL_HIST）
        self._hist_follow = True           # 用户上滚回看时不自动滚底
        self._dual_hist.verticalScrollBar().valueChanged.connect(self._on_hist_scroll)
        # v2.15.0：分割线拖拽——历史区/当前句区的高度比例由用户拖 hist 底缘
        # （分隔把手）自由分配；None=自动分配。用户需求："中间的分割线依然
        # 不能自由的上下拉长"
        self._dual_hist_h_user = None
        self._dual_src_h_user = None       # v2.16.0：原文区用户拖出的高度（px）
        self._dual_split_drag = False
        self._dual_split_mode = "hist"     # "hist"=历史区底缘 / "src"=原文译文分割线
        self._dual_split_start_y = None
        self._dual_split_start_h = 0

        self._dual_body = QWidget(self)
        self._dual_body.setObjectName("PanelDual")
        self._dual_body.setAutoFillBackground(False)
        self._dual_body.setAttribute(Qt.WA_TranslucentBackground, True)
        dl = QVBoxLayout(self._dual_body)
        dl.setContentsMargins(10, 6, 14, 8)
        dl.setSpacing(5)
        # v2.16.0：原文区/译文区改为**各自可滚动的 QScrollArea**（用户需求：
        # "原文和译文不是都应该保留吗，可以用滚轮进行滚上滚下查看"——长句
        # 超出区域高度时不再被裁切，滚轮即可看全）；中间 `_dual_sep` 从装饰
        # 线升级为**可拖分割把手**（"原文的显示范围太小了，改成可以自由上下
        # 拉长"——拖动分配原文/译文两区高度，原文区想多大拖多大）。
        self._dual_src_wrap = QScrollArea(self._dual_body)
        self._dual_src_wrap.setObjectName("DualSrcWrap")
        self._dual_src_wrap.setWidgetResizable(True)
        self._dual_src_wrap.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._dual_src_wrap.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._dual_src_wrap.viewport().setAutoFillBackground(False)
        self._dual_src_wrap.viewport().setAttribute(Qt.WA_TranslucentBackground, True)
        self._dual_src = QLabel("", self._dual_src_wrap)
        self._dual_src.setObjectName("DualSrc")
        self._dual_src.setWordWrap(True)
        self._dual_src.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self._dual_src.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._dual_src_wrap.setWidget(self._dual_src)
        self._dual_sep = QFrame(self._dual_body)
        self._dual_sep.setObjectName("DualSep")
        self._dual_sep.setFixedHeight(8)     # 抓取带：内画 1px 线，好抓
        self._dual_sep.setCursor(Qt.SizeVerCursor)
        self._dual_tgt_wrap = QScrollArea(self._dual_body)
        self._dual_tgt_wrap.setObjectName("DualTgtWrap")
        self._dual_tgt_wrap.setWidgetResizable(True)
        self._dual_tgt_wrap.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._dual_tgt_wrap.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._dual_tgt_wrap.viewport().setAutoFillBackground(False)
        self._dual_tgt_wrap.viewport().setAttribute(Qt.WA_TranslucentBackground, True)
        self._dual_tgt = QLabel("", self._dual_tgt_wrap)
        self._dual_tgt.setObjectName("DualTgt")
        self._dual_tgt.setWordWrap(True)
        self._dual_tgt.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self._dual_tgt.setTextInteractionFlags(Qt.TextSelectableByMouse)
        # v2.16.0：空态占位标记初始化——此前 property 未设时 _sync_dual_visibility
        # 误判"有译文"，导致空态下分隔线可见（用户截图圈出的那条"神秘线"）
        self._dual_tgt.setProperty("empty", True)
        self._dual_tgt_wrap.setWidget(self._dual_tgt)
        dl.addWidget(self._dual_src_wrap, 1)
        dl.addWidget(self._dual_sep)
        dl.addWidget(self._dual_tgt_wrap, 2)
        outer.addWidget(self._dual_body, 1)
        self._dual_body.hide()
        # v2.15.2：分割线交互改走**事件过滤器**。为什么必须用它：
        # ① mouse move/hover 在子控件忽略后**不会传播给父控件**（只有 press
        #    会重新投递）→ 悬停光标反馈永远失效，用户无从发现可拖；
        # ② 命中带上半落在 QScrollArea 内，press 会被它直接消费；
        # ③ 传播到面板的事件 position 不重映射（相对原接收者），相对坐标
        #    判定必然错位（v2.15.1 已实证）。
        # 过滤器在子控件层面拦截全部相关事件，坐标全用 globalPosition。
        # 注意：必须放在全部控件创建之后（否则引用未创建属性）。
        self._dual_split_watch = [
            self._dual_hist, self._dual_hist.viewport(), self._dual_hist_body,
            self._dual_body, self._dual_src, self._dual_sep, self._dual_tgt,
            self._dual_src_wrap, self._dual_src_wrap.viewport(),
            self._dual_tgt_wrap, self._dual_tgt_wrap.viewport(),
        ]
        for _w in self._dual_split_watch:
            _w.installEventFilter(self)

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
        # v2.6.6：修"每句闪一个无题小窗"瞬窗——v2.5.0 行卡片化时 s/t 无父构造，
        # 紧随的 s.setVisible(True)（开了"同时显示原文"时每句必执行）让 Qt 把
        # 尚未收编的标签当顶层窗口建出原生窗口，下一行 addWidget 收编为子控件
        # 又立刻销毁该窗口——用户看到字幕面板同款小窗闪现 ~40ms 消失。
        # 修：构造即传父 + 可见性切换挪到收编之后（双保险，杜绝裸顶层窗口）。
        s = QLabel(src, row)
        s.setObjectName("PanelSrc")
        s.setWordWrap(True)
        t = QLabel(tgt, row)
        t.setObjectName("PanelTgt")
        t.setWordWrap(True)
        v.addWidget(s)
        v.addWidget(t)
        s.setVisible(bool(src) and self._show_source)
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

    # ---------- v2.7.0（T1）：待决行按原文配对，杜绝"译文配错原文" ----------
    # 旧设计全板只有一个"待决槽"：下一句 show_pending 会覆盖槽内容，上一句
    # 迟到的译文就落到新句的原文行上（用户看到"A 的原文配 B 的翻译"）。
    # 现允许多条待决行并存，结果只填匹配行；攒句合并的前片由 merged 名单收编。

    @staticmethod
    def _starts_new_sentence(text):
        """新句判据（与主窗 _starts_new_sentence 同源）：首字符大写拉丁/CJK/数字。"""
        t = (text or "").lstrip()
        if not t:
            return False
        ch = t[0]
        return ch.isupper() or ch.isdigit() or "\u4e00" <= ch <= "\u9fff"

    def _find_pending(self, source_text):
        key = (source_text or "").strip()
        if not key:
            return None
        suffix_hit = None
        for it in reversed(self._rows):
            if not it["pending"]:
                continue
            s = (it["src_text"] or "").strip()
            if s == key:
                return it
            # v2.7.4（B-8）：合并整句→末片占位行的后缀匹配（combined 以末片
            # 结尾），命中后由补齐路径把行文本升级为整句，与主窗卡片对齐
            if s and key.endswith(s) and suffix_hit is None:
                suffix_hit = it
        return suffix_hit

    def _merge_pending(self, texts):
        """收编被并入整句的前片占位行（与主窗卡片 set_merged_away 同语义，
        面板行直接移除——旧单槽设计里它们本就被覆盖消失，观感一致）。"""
        if not texts:
            return
        tset = {(t or "").strip() for t in texts if (t or "").strip()}
        if not tset:
            return
        for it in list(self._rows):
            if it["pending"] and (it["src_text"] or "").strip() in tset:
                self._rows.remove(it)
                it["row"].setParent(None)
                it["row"].deleteLater()

    def show_pending(self, source_text):
        """识别文本先上屏，译文占位（两段式）。同句重复调用幂等（主窗每段双发）。

        v2.11.0：dual（上下双语）模式下原文区**流式生长**——新句起始（首字符
        大写/CJK/数字）即重置原文区为该片段；延续片段按 CJK 邻接规则拼进当前
        句，用户看到的是"整句在打字机式生长"，与推测式翻译的译文区同步演进。"""
        if self._layout_mode == "dual":
            self._dual_show_pending(source_text)
            return
        r = self._find_pending(source_text)
        if r is not None:
            return
        last = None
        for it in reversed(self._rows):
            if it["pending"]:
                last = it
                break
        if (last is not None and source_text
                and not self._starts_new_sentence(source_text)):
            # 延续片：同一句在长行上生长（保持旧"单行生长"观感）
            last["src_text"] = source_text
            last["src"].setText(source_text)
            self._relayout()
            self._schedule_relayout()
            return
        # v2.7.5（R-4）：_pending_row 死变量移除——T1 多待决并存后匹配走
        # _find_pending（按原文精确/后缀），单槽指针已无读方
        self._add_row(source_text, "⟳ 识别中…", True)

    def show_pending_result(self, source_text, target_text, show_source=True,
                            merged_from=None):
        """译文就绪：补齐**原文匹配**的占位行；无匹配则新建完成行（迟到旧句
        排到队尾，好过配错行）。merged_from=攒句合并的前片名单（主窗传入）。
        v2.11.0：dual 模式下"收口"=译文区从推测态转正式样式，原文区校准为
        整句；无行概念，merged_from 直接忽略。"""
        if self._layout_mode == "dual":
            self._dual_show_result(source_text, target_text, show_source)
            return
        self._show_source = bool(show_source)
        self._last_result = (source_text or "", target_text or "")
        self._merge_pending(merged_from)
        r = self._find_pending(source_text)
        if r is not None:
            r["pending"] = False
            r["spec"] = False    # v2.7.6（A）：整句终版覆盖推测中间版，脱离推测态
            r["src_text"], r["tgt_text"] = source_text or "", target_text or ""
            r["src"].setText(source_text or "")
            r["src"].setVisible(bool(source_text) and self._show_source)
            r["tgt"].setText(target_text or "")
            if self._collapsed:
                self._update_mini()   # v2.5.0：精简条正在显示这句占位时同步成译文
            self._relayout()
            self._schedule_relayout()
        else:
            self._add_row(source_text or "", target_text or "", False)
        self._count_unread()
        self._sync_bar_texts()

    def update_spec_result(self, source_text, target_text, show_source=True):
        """v2.7.6（A）推测式中间版译文：在匹配的待决行上**原地生长覆盖**。

        与 show_pending_result 的区别（三条都是刻意的）：
        ① 行保持 pending=True —— 整句终版随后到达还要走补齐路径收口；
        ② 不动 _last_result / 不计未读 —— 中间版不是"完成了一句"；
        ③ 原文行同步生长为整句（combined）—— 否则会出现"半句原文配整句
           译文"，正是 v2.7.4（B-8）在主窗侧修过的同款分叉。
        配对复用 _find_pending（精确 + 后缀匹配）：combined 键以末片结尾，
        所以能命中末片占位行；找不到行说明已收编/已终态，静默丢弃。"""
        if self._layout_mode == "dual":
            self._dual_spec(source_text, target_text, show_source)
            return
        r = self._find_pending(source_text)
        if r is None:
            return
        self._show_source = bool(show_source)
        r["spec"] = True
        r["tgt_text"] = target_text or ""
        r["tgt"].setText(target_text or "")
        if source_text:
            r["src_text"] = source_text
            r["src"].setText(source_text)
            r["src"].setVisible(self._show_source)
        if self._collapsed:
            self._update_mini()
        self._relayout()
        self._schedule_relayout()

    def update_partial(self, text_full):
        """v2.12.0：流式草稿上屏（dual 专属）——主窗把「已确认 + 预览增量」
        拼成整句传入，原文区整体刷新（每 ~0.9s 一拍，实现"主持人讲到哪、
        原文跟到哪"）。终版收口（show_pending_result）会以正式文本覆盖；
        列表模式忽略。样式与常态原文一致（草稿的未定稿感由高频刷新自证）。"""
        if self._layout_mode != "dual":
            return
        t = (text_full or "").strip()
        if not t:
            return
        self._dual_src.setText(t)
        self._sync_dual_visibility()
        self._schedule_relayout()

    def update_dual_draft_tgt(self, translated):
        """v2.13.0：草稿推测译文——**只更新译文区**（spec 淡样式），不碰原文/
        行簿记/_last_result/未读计数；片段级推测版（_dual_spec）与整句终版
        （_dual_show_result）随后自然覆盖。dual 专属（调用方已按布局闸门过滤，
        这里再防一道）。效果：译文区与原文区同节奏实时生长（0.9s 级）。"""
        if self._layout_mode != "dual" or not translated:
            return
        self._dual_tgt.setProperty("spec", True)
        self._dual_tgt.setProperty("empty", False)
        self._dual_tgt.setText(translated)
        self._restyle_dual_tgt()
        self._sync_dual_visibility()
        self._schedule_relayout()

    def show_caption(self, source_text, target_text, show_source=True):
        """一次性上屏（无占位）。v2.11.0：dual 模式同终态收口路径。"""
        if self._layout_mode == "dual":
            self._dual_show_result(source_text, target_text, show_source)
            return
        self._show_source = bool(show_source)
        self._last_result = (source_text or "", target_text or "")
        self._add_row(source_text or "", target_text or "", False)
        self._count_unread()
        self._sync_bar_texts()

    def clear_caption(self):
        if self._layout_mode == "dual":
            # v2.14.0：当前句 + 历史区一并清空
            while self._dual_hist_rows > 0:
                it = self._dual_hist_lay.itemAt(0)
                if it and it.widget():
                    it.widget().deleteLater()
                    self._dual_hist_lay.removeItem(it)
                self._dual_hist_rows -= 1
            self._hist_follow = True
            self._dual_src.setText("")
            self._dual_tgt.setProperty("spec", False)
            self._dual_tgt.setProperty("empty", True)
            self._dual_tgt.setText(
                self.HINT_GUIDE if self._hint_guide else self.HINT_IDLE)
            self._restyle_dual_tgt()
            self._sync_dual_visibility()
            self._clear_btn.setEnabled(False)
            self._relayout()
            return
        for it in self._rows:
            it["row"].setParent(None)
            it["row"].deleteLater()
        self._rows = []
        self._last_result = ("", "")
        self._unread = 0
        self._sync_unread_btn()
        self._update_empty_hint()
        self._update_mini()
        self._relayout()
        self._schedule_relayout()

    # ---------- v2.4.3：空状态占位 / 未读计数 ----------

    # ---------- v2.11.0：dual（上下双语）布局模式 ----------

    def is_dual(self):
        """当前是否处于上下双语布局（主窗/测试用）。"""
        return self._layout_mode == "dual"

    def _dual_want_height(self):
        """dual 当前句区的内容需求高度。

        QLabel 带 wordWrap 时 sizeHint 是**单行**值，直接用会在长句下裁切
        （真机截图实证）；heightForWidth(可用宽度) 才是换行后的真实高度。
        v2.16.0：src/tgt 已各自进 QScrollArea（wrap 内含 2px frame 余量），
        常数 = sep 抓取带 8 + dl 纵向 margins 14 + spacing×2 10。"""
        avail = max(80, self._dual_body.width() - 24)
        src_h = (self._dual_src.heightForWidth(max(40, avail - 2)) + 2
                 if self._dual_src.isVisible() else 0)
        tgt_h = self._dual_tgt.heightForWidth(max(40, avail - 2)) + 2
        return src_h + tgt_h + 8 + 24

    def _on_hist_scroll(self, v):
        """v2.14.0：历史区滚动跟随——用户上滚回看时不自动滚底，回底恢复。"""
        sb = self._dual_hist.verticalScrollBar()
        self._hist_follow = (v >= sb.maximum() - 4)

    def _dual_hist_bottom_y(self, global_coords=False):
        """v2.15.0：历史区底边的 y。global_coords=True 时返回**全局屏幕** y——
        鼠标事件从子控件（QLabel/QScrollArea）传播到面板时 position 不重映射
        （相对原接收者），相对坐标判定会永远错位失配（用户实测"还是不行"
        的根因），必须用全局坐标比对。"""
        try:
            if global_coords:
                return self._dual_hist.mapToGlobal(
                    QPoint(0, self._dual_hist.height())).y()
            return self._dual_hist.mapTo(self, QPoint(
                0, self._dual_hist.height())).y()
        except RuntimeError:
            return -1

    def _dual_split_hit(self, global_y):
        """v2.15.0：分割把手命中判定，仅 dual+可见+未收起。参数必须是
        **全局屏幕 y**（见 _dual_hist_bottom_y 的坐标说明）。
        v2.15.2：命中中心 = 历史区底边本身（布局缝隙的视觉中心，±9px）——
        该点可能落在布局缝隙（无子控件，事件直达 overlay）或 src_wrap 顶部
        （事件过滤器处理），双通道都按同一中心判定。"""
        return (self._layout_mode == "dual" and not self._collapsed
                and self._dual_hist.isVisible()
                and abs(global_y - self._dual_hist_bottom_y(global_coords=True)) <= 9)

    def set_dual_hist_h_user(self, h):
        """v2.15.0：历史区用户高度入口（主窗配置恢复/0=回自动分配）。
        拖分割线改的就是这个值——决定历史区与当前句区的高度比例。"""
        self._dual_hist_h_user = int(h) if h and int(h) >= 40 else None
        if self._layout_mode == "dual" and not self._collapsed:
            self._relayout()

    def set_dual_src_h_user(self, h):
        """v2.16.0：原文区用户高度入口（主窗配置恢复/0=回自动贴内容）。
        拖原文/译文之间的分割线改的就是这个值——原文区想多大拖多大，
        译文区吃剩余（两者各自可滚动）。"""
        self._dual_src_h_user = int(h) if h and int(h) >= 30 else None
        if self._layout_mode == "dual" and not self._collapsed:
            self._relayout()

    def _dual_hist_scroll_bottom(self):
        sb = self._dual_hist.verticalScrollBar()
        sb.setValue(sb.maximum())

    def dual_push_history(self, src, tgt):
        """v2.14.0：终版句沉入历史区（原文小灰 + 译文小白成对行）。
        超上限删最老；跟随状态下自动滚底（用户上滚回看时不打断）。
        主窗在整句终版翻译到达时调用。"""
        if self._layout_mode != "dual":
            return
        src = (src or "").strip()
        tgt = (tgt or "").strip()
        if not src and not tgt:
            return
        w = QWidget()
        w.setAttribute(Qt.WA_TranslucentBackground, True)
        l = QVBoxLayout(w)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(1)
        lab_src = QLabel(src)
        lab_src.setObjectName("DualHistSrc")
        lab_src.setWordWrap(True)
        f = QFont()
        f.setPixelSize(max(10, int(self._font_size * 0.55)))
        lab_src.setFont(f)
        lab_src.setVisible(bool(src) and self._show_source)
        lab_tgt = QLabel(tgt or "…")
        lab_tgt.setObjectName("DualHistTgt")
        lab_tgt.setWordWrap(True)
        f2 = QFont()
        f2.setPixelSize(max(11, int(self._font_size * 0.66)))
        f2.setWeight(QFont.DemiBold)
        lab_tgt.setFont(f2)
        l.addWidget(lab_src)
        l.addWidget(lab_tgt)
        self._dual_hist_lay.insertWidget(self._dual_hist_lay.count() - 1, w)
        self._dual_hist_rows += 1
        while self._dual_hist_rows > self.MAX_DUAL_HIST:
            it = self._dual_hist_lay.itemAt(0)
            if it and it.widget():
                it.widget().deleteLater()
                self._dual_hist_lay.removeItem(it)
            self._dual_hist_rows -= 1
        if self._dual_hist_rows and self._hist_follow:
            QTimer.singleShot(0, self._dual_hist_scroll_bottom)
        self._schedule_relayout()

    def dual_clear_current(self):
        """v2.14.0：当前句区清空（终版句已沉历史，等待下一句草稿/片段；
        空态由 _update_empty_hint 写占位/引导）。"""
        if self._layout_mode != "dual":
            return
        self._dual_src.setText("")
        self._dual_tgt.setProperty("empty", True)
        self._dual_tgt.setProperty("spec", False)
        self._dual_tgt.setText("")
        self._restyle_dual_tgt()
        self._sync_dual_visibility()
        self._update_empty_hint()
        self._schedule_relayout()

    def set_layout_mode(self, mode):
        """切换面板布局："list"=历史滚动列表，"dual"=上下双语（豆包风）。
        幂等；可见性与高度收放统一交给 _relayout。dual→list 切回时列表
        保留既有历史行（dual 的当前句不回填——历史真相在主窗与导出里）。"""
        m = "dual" if str(mode or "").strip().lower() == "dual" else "list"
        if m == self._layout_mode:
            return
        self._layout_mode = m
        # v2.11.0：__init__ 的空态占位写在列表区 _hint 里，构造后经配置切到
        # dual 的实例会错过它（真机截图实证：dual 空面板一片空白）——
        # 切换即补一次空态刷新（dual 分支只在原文区为空时写占位，幂等）
        self._update_empty_hint()
        self._relayout()

    def _restyle_dual_tgt(self):
        """property 变更（spec/empty）后重刷 QSS——Qt 不会自动感知属性态样式。"""
        w = self._dual_tgt
        w.style().unpolish(w)
        w.style().polish(w)

    def _sync_dual_visibility(self):
        """原文行与分隔线随「同时显示原文」开关与内容有无显隐。
        关掉原文 = 纯译文大字模式（分隔线一并隐藏）。"""
        has_tgt = bool(self._dual_tgt.text().strip()) and not self._dual_tgt.property("empty")
        show = self._show_source and bool(self._dual_src.text().strip())
        self._dual_src.setVisible(show)
        self._dual_sep.setVisible(show and has_tgt)

    @staticmethod
    def _dual_join(cur, piece):
        """延续片段拼接：CJK 邻接直连、否则补空格（与主窗 _combine_pieces 同规）。"""
        if not cur:
            return piece
        if ("\u4e00" <= cur[-1] <= "\u9fff"
                or (piece and "\u4e00" <= piece[0] <= "\u9fff")):
            return cur + piece
        return cur + " " + piece

    def _dual_new_sentence(self, src_text):
        """新句起点：原文区重置为该片段，译文区进入占位态（推测版淡样式）。"""
        self._dual_src.setText(src_text)
        self._dual_tgt.setProperty("spec", True)
        self._dual_tgt.setProperty("empty", False)
        self._dual_tgt.setText("…")
        self._restyle_dual_tgt()
        self._sync_dual_visibility()
        self._update_empty_hint()
        self._schedule_relayout()

    def _dual_show_pending(self, source_text):
        t = (source_text or "").strip()
        if not t:
            return
        cur = self._dual_src.text().strip()
        if not cur or self._starts_new_sentence(t):
            self._dual_new_sentence(t)
        else:
            self._dual_src.setText(self._dual_join(cur, t))
            self._sync_dual_visibility()
            self._schedule_relayout()

    def _dual_spec(self, source_text, target_text, show_source):
        """推测中间版：原文校准为整句、译文淡色就地更新（失败静默等终版）。"""
        if not target_text:
            return
        self._show_source = bool(show_source)
        if source_text:
            self._dual_src.setText(source_text)
        self._dual_tgt.setProperty("spec", True)
        self._dual_tgt.setProperty("empty", False)
        self._dual_tgt.setText(target_text)
        self._restyle_dual_tgt()
        self._sync_dual_visibility()
        self._schedule_relayout()

    def _dual_show_result(self, source_text, target_text, show_source):
        """终版收口：译文转正式样式（主字号加粗纯色），原文校准为整句。"""
        self._show_source = bool(show_source)
        self._last_result = (source_text or "", target_text or "")
        if source_text:
            self._dual_src.setText(source_text)
        self._dual_tgt.setProperty("spec", False)
        self._dual_tgt.setProperty("empty", not bool(target_text))
        self._dual_tgt.setText(target_text or "…")
        self._restyle_dual_tgt()
        self._sync_dual_visibility()
        self._update_empty_hint()
        self._schedule_relayout()

    def _update_empty_hint(self):
        """B/D：无行时显示占位（或首次手势引导），来字即隐；顺带门控清空按钮。
        v2.11.0：dual 模式空态时译文区**常驻占位文案**（真机截图实证：不写的话
        空面板是一片空白，用户不知道这里是干嘛的）；有内容则不动。"""
        if self._layout_mode == "dual":
            if not self._dual_src.text().strip():
                self._dual_tgt.setProperty("empty", True)
                self._dual_tgt.setProperty("spec", False)
                self._dual_tgt.setText(
                    self.HINT_GUIDE if self._hint_guide else self.HINT_IDLE)
                self._restyle_dual_tgt()
            self._clear_btn.setEnabled(bool(self._dual_src.text().strip()))
            return
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
        # v2.11.0：dual 模式收敛判定看 _dual_want_height（sizeHint 对 wordWrap
        # label 是单行值，不可靠）
        # v2.11.0（关键修复）：链结束必须把 _relayout_pending 置回 False——
        # 原版（v2.4.3 起）收敛后 pending 永久残留 True，之后所有 _schedule_
        # relayout 被守卫吞掉：列表模式有滚动条兜底视觉无感（历史未暴露），
        # dual 模式无滚动 → 第二句起高度永远停在首句值、长终版底部裁切
        # （真机截图+探针轨迹实证：pending=True 恒真、want=124 而 body=74）。
        if self._layout_mode == "dual" and not self._collapsed:
            want0 = self._dual_want_height()
            h0 = self._dual_body.height()
            h1 = self._dual_hist.height()
            self._relayout()
            if ((self._dual_want_height() != want0
                 or self._dual_body.height() != h0
                 or self._dual_hist.height() != h1) and self._relayout_passes < 12):
                self._relayout_passes += 1
                self._relayout_pending = True
                QTimer.singleShot(0, self._consume_relayout)
            else:
                self._relayout_pending = False
            return
        want0 = self._body.sizeHint().height()
        h0 = self._scroll.height()
        self._relayout()
        if ((self._body.sizeHint().height() != want0
             or self._scroll.height() != h0) and self._relayout_passes < 12):
            self._relayout_passes += 1
            self._relayout_pending = True
            QTimer.singleShot(0, self._consume_relayout)
        else:
            self._relayout_pending = False

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
            self._dual_body.setVisible(False)
            self._mini.setVisible(True)
            self._mini.setFixedHeight(min(self._mini.sizeHint().height(), 120))
        elif self._layout_mode == "dual":
            # v2.11.0：上下双语——当前句区贴内容；v2.14.0：历史区吃剩余
            # 空间（有内容才显示）
            # v2.14.1（关键修复）：**用户拉高的空间优先给当前句原文区**——
            # v2.14.0 把增量全分给历史区，历史无行时 hist_h 强制 0、body 固定
            # 内容高 → 拖底缘面板弹回原高，用户实测"完全拉不了"（底缘命中与
            # 拖拽逻辑本身正常，是高度分配把它吃了）。现语义：拖底缘 = 原文
            # 显示区变大（内容顶部对齐，多余空间留白），历史区吃剩余。
            self._mini.setVisible(False)
            self._scroll.setVisible(False)
            self._dual_body.setVisible(True)
            self._jump_btn.hide()
            # 先解除上一轮 setFixedHeight 的钳制——否则 wordWrap 文本变长时
            # label 被压在旧高度里，终版长译文底部裁切（真机截图实证）
            self._dual_body.setMinimumHeight(0)
            self._dual_body.setMaximumHeight(16777215)
            chrome = 54 + 12                          # 工具条 + outer margins/spacing
            sep_h = 8                                 # v2.16.0：分割把手抓取带
            avail_w = max(80, self._dual_body.width() - 24)
            src_want = ((self._dual_src.heightForWidth(max(40, avail_w - 2)) + 2)
                        if self._dual_src.isVisible() else 0)
            tgt_want = self._dual_tgt.heightForWidth(max(40, avail_w - 2)) + 2
            body_want = src_want + tgt_want + sep_h + 24   # + dl 纵向 margins/spacing
            if self._user_height:
                # v2.14.1：拉高 = **锁定面板总高**——当前句区吃"总高-工具条-
                # 历史"的全部剩余（拖动即时反馈，绝不弹回）；历史区最多占
                # 总高 55%、保底 56px 可滚。历史无行时当前句区独占整板。
                # v2.15.0：分割线拖出的历史高度（_dual_hist_h_user）优先——
                # 用户显式拖过的比例不应被自动分配覆盖。
                total = self._user_height
                hist_h = 0
                if self._dual_hist_rows:
                    if self._dual_hist_h_user:
                        hist_h = max(40, min(self._dual_hist_h_user,
                                             max(40, total - chrome - 46)))
                    else:
                        # 有行即保底 56px（可滚）——单行内容需求 ~38px，若按
                        # "内容需求≥40 才显示"判定会把单行历史整条杀掉
                        # （集成锁与独立调试双双实证）
                        hist_h = max(56, min(self._dual_hist_body.sizeHint().height(),
                                             int(total * 0.55),
                                             max(56, total - chrome - 46)))
                body_h = max(46, total - chrome - hist_h)
                self._dual_body.setFixedHeight(body_h)
                if hist_h:
                    self._dual_hist.setVisible(True)
                    self._dual_hist.setFixedHeight(hist_h)
                else:
                    self._dual_hist.setVisible(False)
                    self._dual_hist.setFixedHeight(0)
            else:
                body_h = min(max(body_want, 46), 300)
                self._dual_body.setFixedHeight(body_h)
                avail = int((QGuiApplication.primaryScreen().availableGeometry().height()
                             or 800) * 0.68)
                hist_want = (self._dual_hist_body.sizeHint().height()
                             if self._dual_hist_rows else 0)
                if self._dual_hist_rows:
                    # v2.15.0：分割线拖过的历史高度优先（自动总高模式下拖大
                    # 历史区 → 面板随之长高）
                    if self._dual_hist_h_user:
                        hist_h = max(40, self._dual_hist_h_user)
                    else:
                        # v2.15.1：自动分支同样保底 56px——旧 40px 门槛会把
                        # 单行历史（内容需求 ~38px）整条隐藏（与锁定总高分
                        # 支同源，v2.14.1 只修了一处漏了这里）
                        hist_h = max(56, min(hist_want, avail - chrome - body_h))
                else:
                    hist_h = 0
                if self._dual_hist_rows and hist_h >= 40:
                    self._dual_hist.setVisible(True)
                    self._dual_hist.setFixedHeight(hist_h)
                else:
                    self._dual_hist.setVisible(False)
                    self._dual_hist.setFixedHeight(0)
            # v2.16.0：当前句区内部——原文区用户高度（拖 sep 分割线得出）
            # 固定生效，译文区吃剩余（两者各自可滚动，永不互相裁切）
            if self._dual_src_h_user:
                self._dual_src_wrap.setFixedHeight(
                    max(30, min(self._dual_src_h_user,
                                max(30, body_h - sep_h - 24 - 40))))
            else:
                self._dual_src_wrap.setMinimumHeight(0)
                self._dual_src_wrap.setMaximumHeight(16777215)
        else:
            self._mini.setVisible(False)
            self._dual_body.setVisible(False)
            self._scroll.setVisible(True)
            want = self._body.sizeHint().height() + 8
            cap = int((QGuiApplication.primaryScreen().availableGeometry().height()
                       or 800) * 0.55)
            if self._user_height:
                # v2.5.3：手动高度锁定——用户拖底缘拉长后不再自动贴内容，
                # 新句在固定高度内滚动（⋯ 菜单可恢复自动）
                self._scroll.setFixedHeight(max(46, self._user_height - 54))
            else:
                self._scroll.setFixedHeight(min(max(want, 46), cap))
            self._scroll.setVerticalScrollBarPolicy(
                Qt.ScrollBarAlwaysOff if want <= self._scroll.height() else Qt.ScrollBarAsNeeded)
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
        self._sync_font_checks()   # v2.5.3：字号菜单勾选随字号互斥同步

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
        self._font_actions = []
        for name, px in self.FONTS:
            a = m.addAction(f"{name}（{px}px）")
            a.setCheckable(True)
            a.triggered.connect(lambda _c=False, p=px: self._pick_font(p))
            self._font_actions.append((a, px))
        self._sync_font_checks()
        return m

    def _sync_font_checks(self):
        """v2.5.3：勾选态随当前字号同步互斥——此前菜单只在构造时 setChecked
        一次且非互斥，换档/滚轮调节后旧勾永不消失（用户实测四档全勾）。"""
        for a, px in getattr(self, "_font_actions", []):
            a.setChecked(abs(px - self._font_size) <= 3)

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
        """v2.5.0：精简条内容 = 最新一句（尊重原文开关）；无字幕给引导占位。
        v2.11.0：dual 模式无行区，从双语区的当前句取值。"""
        if self._layout_mode == "dual":
            if self._dual_src.text().strip():
                src = self._dual_src.text() if self._show_source else ""
                self._mini_src.setText(src)
                self._mini_src.setVisible(bool(src))
                self._mini_tgt.setText(
                    self._dual_tgt.text()
                    if not self._dual_tgt.property("empty") else "⟳ 识别中…")
            else:
                self._mini_src.setVisible(False)
                self._mini_tgt.setText("暂无字幕 · 单击展开")
            return
        if self._rows:
            it = self._rows[-1]
            src = it["src_text"] if self._show_source else ""
            self._mini_src.setText(src)
            self._mini_src.setVisible(bool(src))
            self._mini_tgt.setText(it["tgt_text"] or "⟳ 识别中…")
        else:
            self._mini_src.setVisible(False)
            self._mini_tgt.setText("暂无字幕 · 单击展开")

    def set_user_height(self, h):
        """v2.5.3：手动高度入口（主窗配置恢复/恢复自动传 0）。"""
        self._user_height = int(h) if h and int(h) >= 80 else None
        if not self._collapsed:
            self._relayout()

    def _reset_user_height(self):
        """v2.5.3：恢复自动高度（贴内容）。"""
        self._user_height = None
        if self._on_height_changed:
            self._on_height_changed(0)
        self._relayout()

    def set_collapsed(self, on):
        self._collapsed = bool(on)
        self._sync_bar_texts()
        dual = self._layout_mode == "dual"
        self._jump_btn.setVisible(not self._follow and not self._collapsed and not dual)
        self._scroll.setVisible(not self._collapsed and not dual)
        self._dual_body.setVisible(not self._collapsed and dual)
        # v2.14.0：历史区可见性与 body 同步（_relayout 按行数精调高度）
        self._dual_hist.setVisible(not self._collapsed and dual)
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
            QWidget#PanelDual {{ background: transparent; }}
            QScrollArea#PanelDualHist {{ background: transparent; border: none;
                                          border-bottom: 1px solid rgba(255,255,255,20); }}
            QScrollArea#PanelDualHist > QWidget {{ background: transparent; }}
            QScrollArea#PanelDualHist > QWidget > QWidget {{ background: transparent; }}
            QLabel#DualHistSrc {{ color: #7d8794; }}
            QLabel#DualHistTgt {{ color: rgba(255,255,255,218); }}
            QLabel#DualSrc {{ color: #98a2b3; }}
            QLabel#DualTgt {{ color: {self._text_color.name()}; }}
            QLabel#DualTgt[spec="true"] {{ color: rgba(255,255,255,205); }}
            QLabel#DualTgt[empty="true"] {{ color: rgba(255,255,255,72); }}
            QFrame#DualSep {{ background: rgba(255,255,255,14);
                              border-top: 1px solid rgba(255,255,255,30);
                              border-bottom: 1px solid rgba(255,255,255,30); }}
            QScrollArea#DualSrcWrap {{ background: transparent; border: none; }}
            QScrollArea#DualSrcWrap > QWidget {{ background: transparent; }}
            QScrollArea#DualSrcWrap > QWidget > QWidget {{ background: transparent; }}
            QScrollArea#DualTgtWrap {{ background: transparent; border: none; }}
            QScrollArea#DualTgtWrap > QWidget {{ background: transparent; }}
            QScrollArea#DualTgtWrap > QWidget > QWidget {{ background: transparent; }}
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
        # v2.11.0：dual 区字号用 setFont 落地（原因见 _apply_dual_fonts）
        self._apply_dual_fonts()

    def _apply_dual_fonts(self):
        """dual 区字号/字重必须走 setFont——QSS 的 font-size **不会写回
        widget.font()**，QLabel 的 heightForWidth/sizeForWidth 按默认 12px
        字体度量，长句需求高度被严重算小 → 终版译文底部裁切（真机 grab
        实证两次）。颜色等外观仍走 QSS（_apply_qss），度量归 setFont。"""
        fs = max(10, int(self._font_size))
        f_src = QFont()
        f_src.setPixelSize(max(11, int(fs * 0.78)))
        f_src.setWeight(QFont.DemiBold)
        self._dual_src.setFont(f_src)
        self._restyle_dual_tgt()

    def _restyle_dual_tgt(self):
        """按 spec/empty 属性态落地译文字号/字重（setFont，原因见
        _apply_dual_fonts），并重刷 QSS 颜色（Qt 不自动感知属性态样式）。"""
        w = self._dual_tgt
        empty = bool(w.property("empty"))
        f = QFont()
        if empty:
            f.setPixelSize(12)
            f.setWeight(QFont.Normal)
        else:
            f.setPixelSize(max(10, int(self._font_size)))
            f.setWeight(QFont.Bold if not w.property("spec") else QFont.DemiBold)
        w.setFont(f)
        w.style().unpolish(w)
        w.style().polish(w)

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
            # v2.14.1：底缘"⋯"横向三点把手——底缘拉伸此前是隐形手势（右缘
            # 有 ⋮ 而底缘没有），用户实测"连最下边的边都拉不了"的观感之一；
            # 拖拽中提亮给出反馈
            grip2 = QColor(255, 255, 255, 90 if not self._v_resizing else 180)
            p.setBrush(grip2)
            cy2 = self.height() - self.RESIZE_EDGE // 2 - 1
            cx2 = self.width() // 2
            for dx in (-7, 0, 7):
                p.drawEllipse(QPoint(cx2 + dx - 1, cy2 - 1), 1, 1)
            # v2.15.0：分割把手三点（历史区底边中央）——"中间的分割线"此前
            # 是纯装饰不可拖，现可拖动分配历史/当前句高度；拖拽中提亮
            if (self._layout_mode == "dual" and self._dual_hist.isVisible()
                    and not self._collapsed):
                grip3 = QColor(255, 255, 255, 150 if self._dual_split_drag else 90)
                p.setBrush(grip3)
                by = self._dual_hist_bottom_y()
                cx3 = self.width() // 2
                for dx in (-7, 0, 7):
                    p.drawEllipse(QPoint(cx3 + dx - 1, by - 1), 1, 1)

    # ---------- 鼠标：整板拖移 + 右缘调宽 + 双击贴边 ----------

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            pos = event.position()
            pos = pos.toPoint() if hasattr(pos, "toPoint") else pos
            if (event.position().x() >= self.width() - self.RESIZE_EDGE
                    and not self._collapsed):
                self._resizing = True
                self._resize_start = event.globalPosition().toPoint()
                self._resize_start_w = self.width()
            elif (not self._collapsed
                  and pos.y() >= self.height() - self.RESIZE_EDGE):
                # v2.5.3：底缘拖高——用户裁决"上下也要能拉长"；拖后锁定手动高度
                self._v_resizing = True
                self._v_resize_start = event.globalPosition().toPoint()
                self._v_resize_start_h = self.height()
            # v2.15.2：分割拖拽由事件过滤器处理（子控件层面拦截，否则
            # QScrollArea/占位控件会先消费事件）。
            # v2.15.3（兜底）：分割线可能落在 hist 与 body 之间的**布局缝隙**
            # （无子控件，widgetAt 返回面板自己、事件直达本方法）——此命中
            # 分支必须保留（v2.15.2 清理时误删导致缝隙处拖拽失效，用户实测
            # "还是不行"）；过滤器路径与本路径按同一命中中心双保险。
            elif self._dual_split_hit(event.globalPosition().toPoint().y()):
                self._dual_split_drag = True
                self._dual_split_mode = "hist"
                self._dual_split_start_y = event.globalPosition().toPoint()
                self._dual_split_start_h = self._dual_hist.height()
                event.accept()
                return
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
        if self._v_resizing and event.buttons() & Qt.LeftButton:
            new_h = max(80, self._v_resize_start_h
                        + (event.globalPosition().y() - self._v_resize_start.y()))
            self._user_height = new_h
            self.resize(self.width(), new_h)
            self._relayout()
            event.accept()
            return
        # v2.15.2：分割拖拽由事件过滤器处理（见 eventFilter/_dual_split_watch）——
        # mouse move 在子控件忽略后不传播给父级，此路径收不到拖拽事件
        if self._dual_split_drag and event.buttons() & Qt.LeftButton:
            # v2.15.3（兜底路径）：分割线落在布局缝隙时 press/move 直达本方法
            # （事件过滤器覆盖不到）——与过滤器路径共用 _dual_split_apply_drag
            self._dual_split_apply_drag(event.globalPosition().toPoint())
            event.accept()
            return
        if self._drag_pos is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()
            return
        pos = event.position()
        pos = pos.toPoint() if hasattr(pos, "toPoint") else pos
        near_right = (pos.x() >= self.width() - self.RESIZE_EDGE
                      and not self._collapsed)
        near_bottom = (pos.y() >= self.height() - self.RESIZE_EDGE
                       and not self._collapsed)
        # v2.15.1：分割命中同样用全局坐标（传播事件坐标不重映射）
        near_split = self._dual_split_hit(event.globalPosition().toPoint().y())
        if near_right:
            self.setCursor(Qt.SizeHorCursor)
        elif near_bottom or near_split:
            self.setCursor(Qt.SizeVerCursor)
        else:
            self.setCursor(Qt.ArrowCursor)

    def _dual_split_apply_drag(self, g):
        """v2.16.0：按分割模式应用拖拽。
        "src"（原文/译文分割线）：上移原文区变小、下移变大（译文区让位，
        各自可滚动不怕挤没）；"hist"（历史区底缘）：上移历史变小、下移变大。"""
        dy = g.y() - self._dual_split_start_y.y()
        if self._dual_split_mode == "src":
            body_avail = max(120, self._dual_body.height() - 8 - 24)
            new_h = max(30, min(self._dual_split_start_h + dy,
                                max(30, body_avail - 40)))
            self._dual_src_h_user = new_h
        else:
            new_h = max(40, min(self._dual_split_start_h + dy,
                                max(40, self.height() - 54 - 46)))
            self._dual_hist_h_user = new_h
        self._relayout()

    def eventFilter(self, obj, ev):
        """v2.15.2：dual 分割线交互的事件过滤器（挂在历史区/当前句区全部
        子控件上，见 __init__ 的 _dual_split_watch）。

        为什么必须用过滤器而不是 overlay 自身的 mouseEvent：
        ① mouse move/hover 在子控件忽略后**不会传播给父控件**（只有 press
           会重新投递）→ 悬停光标反馈在旧实现下永远失效，用户无从发现可拖；
        ② 命中带上半落在 QScrollArea 内，press 会被它直接消费；
        ③ 传播/过滤的事件 position 相对**原接收者**（不重映射），一律取
           globalPosition 比对。"""
        if (getattr(self, "_dual_split_watch", None) and obj in self._dual_split_watch
                and self._layout_mode == "dual" and not self._collapsed):
            t = ev.type()
            if t in (QEvent.Type.MouseMove, QEvent.Type.HoverMove):
                g = ev.globalPosition().toPoint()
                if self._dual_split_drag:
                    self._dual_split_apply_drag(g)
                    return True
                if hasattr(obj, "setCursor"):
                    if (self._dual_split_hit(g.y())
                            or (obj is self._dual_sep and not self._collapsed)):
                        obj.setCursor(Qt.SizeVerCursor)
                    else:
                        obj.setCursor(Qt.ArrowCursor)
            elif t == QEvent.Type.MouseButtonPress and ev.button() == Qt.LeftButton:
                g = ev.globalPosition().toPoint()
                # v2.16.0：两个分割目标——sep 本体（原文/译文高度分配）、
                # 历史区底边（历史/当前句高度分配）
                mode = None
                if obj is self._dual_sep:
                    mode = "src"
                elif self._dual_split_hit(g.y()):
                    mode = "hist"
                if mode:
                    self._dual_split_drag = True
                    self._dual_split_mode = mode
                    self._dual_split_start_y = g
                    self._dual_split_start_h = (self._dual_hist.height()
                                                if mode == "hist"
                                                else self._dual_src_wrap.height())
                    return True           # 拦下：防止 QScrollArea 抢走拖拽
            elif t == QEvent.Type.MouseMove and self._dual_split_drag:
                self._dual_split_apply_drag(ev.globalPosition().toPoint())
                return True
            elif t == QEvent.Type.MouseButtonRelease and self._dual_split_drag:
                self._dual_split_drag = False
                self._dual_split_apply_drag(ev.globalPosition().toPoint())
                if getattr(self, "_on_dual_split", None):
                    if self._dual_split_mode == "src":
                        self._on_dual_split("src", self._dual_src_h_user or 0)
                    else:
                        self._on_dual_split("hist", self._dual_hist_h_user or 0)
                return True
        return super().eventFilter(obj, ev)

    def mouseReleaseEvent(self, event):
        if self._resizing:
            self._resizing = False
            if self._on_resized:
                self._on_resized(self.width())
        elif self._v_resizing:
            # v2.5.3：底缘拖高结束——手动高度落盘
            self._v_resizing = False
            if self._on_height_changed:
                self._on_height_changed(self._user_height)
        elif self._dual_split_drag:
            # v2.15.0：分割拖拽结束——经专用回调落盘（下次启动保持比例）
            self._dual_split_drag = False
            if getattr(self, "_on_dual_split", None):
                self._on_dual_split(self._dual_hist_h_user or 0)
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
        pos = event.position()
        pos = pos.toPoint() if hasattr(pos, "toPoint") else pos
        # 双击工具条 = 顶/底贴边循环（肌肉记忆：像所有软件的标题栏）
        if pos.y() <= self._bar.geometry().bottom() + 6:
            self._snap_cycle()
        elif pos.y() >= self.height() - self.RESIZE_EDGE:
            # v2.5.3：双击底缘 = 恢复自动高度（与 ⋯ 菜单项同源）
            self._reset_user_height()

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
        # v2.11.0：面板布局快捷切换（与设置页「面板布局」同源落盘，主窗回调）
        acts["layout"] = menu.addAction(
            "切换为列表历史布局" if self._layout_mode == "dual"
            else "切换为上下双语布局（豆包风）")
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
        # v2.5.3：手动高度恢复入口（拖底缘拉长后可回到自动贴内容）
        act_auto_h = menu.addAction("恢复自动高度")
        act_auto_h.setEnabled(bool(self._user_height))
        act_auto_h.triggered.connect(lambda _c=False: self._reset_user_height())
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
        elif chosen == acts.get("layout"):
            # v2.11.0：即时切换 + 经主窗回调落盘 overlay_layout（下次启动保持）
            new_mode = "list" if self._layout_mode == "dual" else "dual"
            self.set_layout_mode(new_mode)
            if self._on_layout_changed:
                self._on_layout_changed(new_mode)
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
