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
- 右缘拖宽、底缘拖高（高度默认贴内容自动收放）。
  v2.20.1（用户点名）：**贴边能力整族退役**——菜单「贴到屏幕四向」、双击工具条
  顶/底贴边循环、拖近边缘自动磁吸全部删除，面板停在哪就是哪，位置只由拖动手决定。
- 右键或 ⋯ = 同一菜单：设置/切源/攒句开关/复制最近一句/纠正识别/纠正译文/
  导出 SRT/清空/置顶/隐藏。

主窗接口保持兼容：show_pending / show_pending_result / show_caption /
clear_caption / set_status / apply_style(去描边参数)。
"""

from PySide6.QtCore import Qt, QPoint, QTimer, QSize, QEvent
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QFont
from PySide6.QtWidgets import (
    QWidget, QLabel, QMenu, QToolButton, QScrollArea, QApplication,
    QVBoxLayout, QHBoxLayout, QSizePolicy, QFrame,
)


def opacity_to_alpha(val):
    """透明度档位（30~100）→ alpha（0~255），**唯一换算入口**。

    v2.19.2：旧写法 `int(v * 2.55)` 在浮点下 100 → 254.999… → **254**，
    "100% 不透明"常年漏 1/255 的桌面进来；且面板菜单/滚轮与重启后
    `apply_style` 各写一份，同一档位前后两副面孔。统一按 255/100 取整并封顶。
    """
    v = max(30, min(100, int(val)))
    return min(255, int(round(v * 255 / 100.0)))


def alpha_to_opacity(alpha):
    """alpha → 档位（`opacity_to_alpha` 的逆，用于把当前 alpha 显示回菜单/滑条）。"""
    return int(round(max(0, min(255, int(alpha))) * 100.0 / 255))


class _DualSepHandle(QWidget):
    """v2.16.1：原文/译文分割把手的自绘本体。

    视觉（用户实测 8px 实心灰带"太生硬、难看"后的重设计）：
    - 平时：仅中央一条**极淡**的 1px 细线（几乎隐形，不破坏空态观感）
    - 悬停：细线提亮 + 中央浮现 40×4 圆角小胶囊（可拖暗示）
    - 拖拽中：胶囊提亮至最明显
    抓取带高度仍为 8px（命中判定不变），只是视觉收敛到中心线。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("DualSep")
        self.setFixedHeight(8)
        self.setMouseTracking(True)
        self._hover = False
        self._drag = False

    def set_hover(self, on):
        if self._hover != bool(on):
            self._hover = bool(on)
            self.update()

    def set_drag(self, on):
        if self._drag != bool(on):
            self._drag = bool(on)
            self.update()

    def paintEvent(self, event):
        from PySide6.QtGui import QPainter, QColor
        p = QPainter(self)
        active = self._hover or self._drag
        # 全宽中央细线：平时淡但**看得见**，悬停/拖拽提亮。
        # v2.18.1：静息 alpha 由 26 提到 70——用户裁决"面板只留一条分割线"后，
        # 真机截图实测这条线在 87% 不透明面板上几乎不可辨（Δ亮度仅约 9%），
        # 等于"留了一条看不见的线"。70 档约 24% 提亮：一眼能找到，仍是一根细线，
        # 不会回到 v2.16.0 那种 8px 实心灰带"太生硬、难看"的老问题。
        line = QColor(255, 255, 255, 130 if active else 70)
        p.fillRect(0, self.height() // 2, self.width(), 1, line)
        # 中央胶囊：仅悬停/拖拽时浮现（平时完全隐形）
        if active:
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 170 if self._drag else 110))
            p.drawRoundedRect(int(self.width() / 2) - 20,
                              int(self.height() / 2) - 2, 40, 4, 2, 2)


class CaptionOverlay(QWidget):
    MIN_W = 360
    RESIZE_EDGE = 14   # v2.4.1：右缘调宽命中带（10px 太窄且无光标反馈→普通人找不到）
    MAX_ROWS = 40
    MAX_DUAL_LINES = 40   # v2.20.1：dual 每栏逐句累积的上限（超出删最老，与列表同规格）
    LANGS = [("zh-CN", "中文"), ("en", "英语"), ("ja", "日语"), ("ko", "韩语"),
             ("fr", "法语"), ("de", "德语"), ("ru", "俄语"), ("es", "西班牙语")]
    FONTS = [("小号", 16), ("中号", 22), ("大号", 30), ("特大", 40)]
    # v2.4.3（B/D）：空状态占位。亮度压在 rgba(255,255,255,72)——混到深底上仍
    # <RGB(120,120,120)，不触碰 v2.4.2"空闲正文无浅灰块"像素回归锁的阈值
    HINT_IDLE = "字幕将在这里逐句显示"
    HINT_GUIDE = ("首次使用小抄：拖工具条移动面板 · 拖右缘改宽度\n"
                  "拖底缘改高度 · 右键或 ⋯ 打开更多操作\n"
                  "字幕将在这里逐句显示")

    def __init__(self, on_closed=None, on_moved=None,
                 on_open_settings=None, on_toggle_source=None,
                 on_toggle_translation_only=None, on_resized=None,
                 on_correct=None, on_export_srt=None, on_language=None,
                 on_font_size=None, on_pin_changed=None, on_collapsed=None,
                 on_first_show=None, on_opacity=None, on_height_changed=None,
                 on_layout_changed=None, on_dual_split=None,
                 on_grouping_toggled=None, on_toggle_running=None):
        super().__init__(None)
        self.setObjectName("SubtitlePanel")
        self._bg_color = QColor("#1c1f26")
        self._bg_alpha = opacity_to_alpha(92)
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
        self._on_grouping_toggled = on_grouping_toggled   # v2.20.0：⋯ 菜单攒句开关落盘
        # v2.20.1：面板上的「开始 / 停止翻译」把手（与全局热键 Ctrl+Alt+S、
        # 托盘「开始 / 停止翻译」同一个动作，回调由主窗注入 = toggle_running）
        self._on_toggle_running = on_toggle_running
        self._running = True
        # v2.20.0：翻译侧「攒句合并」在面板的镜像状态（主窗按 translate_grouping
        # 下发；菜单勾选与设置页勾选同源，set_grouping_enabled 是唯一写入口）
        self._grouping = True
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

        self._run_btn = QToolButton()
        self._run_btn.setObjectName("PanelRun")
        self._run_btn.clicked.connect(self._toggle_running)
        bl.addWidget(self._run_btn)
        self.set_running(True)

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
        self._more_btn.setToolTip("更多操作（导出、置顶、透明度、攒句等）")
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
        self._keep_content_clear(self._body)
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
        # 上半=原文栏（淡灰略小），下半=译文栏（用户点名的卡片样式：圆角底 +
        # 最新一句左侧主题色竖条 + 主字号白色粗体），中间一条可上下拖的分割线
        # 自由分配两栏高度。
        # v2.20.0（用户实拍裁决）：dual **只有这一种形态**——此前按
        # overlay_dual_hist 分成两副面孔（滚动字幕墙 / 顶部历史块），用户指认
        # 幕墙"和列表历史一模一样"，两态一并退役。
        # v2.20.1（用户实拍再裁决）：两栏**逐句累积**——说过、译过的每一句都留在
        # 自己那一栏里可滚轮回看，不再"新句一到就把上一句顶掉"；但**不新增历史区**
        # （回看就是在这两栏里滚），也**不显示滚动条**（"不要搞滚动条"）。
        # v2.19.1：当前句"在说态"——终版收口后置 False（闭合）。流式拍/新片段
        # 在闭合态到来即开**新的一对**条目（原文+译文同刻起行），杜绝
        # "新句原文 配 上一句终版译文"的错配窗口（用户实拍反馈的"攒句感"元凶之一）
        # v2.20.1：累积后"在说哪一行"不再是单值标志——`_dual_rows_closed[i]`
        # 逐行记账（True=该行已收口），`_dual_cur_open` 读它是最新行的状态
        self._dual_rows_closed = []
        # v2.18.1：原文/译文两栏各自的"跟底"状态（用户上滚回看不打断）
        self._dual_follow = {"src": True, "tgt": True}
        self._dual_src_items = []      # [{"lab": QLabel, "text": str}] 逐句累积
        self._dual_tgt_items = []      # [{"card": QWidget, "lab": QLabel}]
        # 当前句的两个标签（无句时 None）——流式生长/推测译/终版收口都只写这一对
        self._dual_src = None
        self._dual_tgt = None
        self._dual_src_h_user = None       # v2.16.0：原文区用户拖出的高度（px）
        self._dual_split_drag = False
        self._dual_split_start_y = None
        self._dual_split_start_h = 0

        self._dual_body = QWidget(self)
        self._dual_body.setObjectName("PanelDual")
        self._dual_body.setAutoFillBackground(False)
        self._dual_body.setAttribute(Qt.WA_TranslucentBackground, True)
        dl = QVBoxLayout(self._dual_body)
        dl.setContentsMargins(10, 6, 14, 8)
        dl.setSpacing(5)
        # v2.16.0：原文区/译文区各自是**独立可滚动的 QScrollArea**；中间
        # `_dual_sep` 是可拖分割把手（拖动分配两栏高度，原文栏想多大拖多大）。
        # v2.20.1：垂直滚动条一律关掉（用户："不要搞滚动条"）——QScrollArea 的
        # 滚轮滚动走 wheel 事件、与滚动条可见性无关，所以回看能力不丢。
        self._dual_src_wrap = QScrollArea(self._dual_body)
        self._dual_src_wrap.setObjectName("DualSrcWrap")
        self._dual_src_wrap.setWidgetResizable(True)
        self._dual_src_wrap.setFrameShape(QFrame.NoFrame)
        self._dual_src_wrap.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._dual_src_wrap.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # v2.16.2：防白底"三保险"——viewport 是独立子控件、**不吃父级 QSS
        # 选择器链**，且真实 Windows 渲染下 QAbstractScrollArea 会用
        # palette.base（白）填充，仅 QSS/属性单层防护真机上仍露白块
        # （用户截图实证）。① NoFrame ② viewport 透明属性 ③ viewport
        # 直接内联 styleSheet（对自身生效，绕过选择器链）
        self._dual_src_wrap.viewport().setAutoFillBackground(False)
        self._dual_src_wrap.viewport().setAttribute(Qt.WA_TranslucentBackground, True)
        self._dual_src_wrap.viewport().setStyleSheet("background: transparent;")
        self._dual_src_body = QWidget()
        self._dual_src_lay = QVBoxLayout(self._dual_src_body)
        self._dual_src_lay.setContentsMargins(0, 0, 0, 0)
        self._dual_src_lay.setSpacing(5)
        self._dual_src_lay.addStretch(1)
        self._dual_src_wrap.setWidget(self._dual_src_body)
        self._keep_content_clear(self._dual_src_body)
        self._dual_sep = _DualSepHandle(self._dual_body)
        self._dual_tgt_wrap = QScrollArea(self._dual_body)
        self._dual_tgt_wrap.setObjectName("DualTgtWrap")
        self._dual_tgt_wrap.setWidgetResizable(True)
        self._dual_tgt_wrap.setFrameShape(QFrame.NoFrame)
        self._dual_tgt_wrap.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._dual_tgt_wrap.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._dual_tgt_wrap.viewport().setAutoFillBackground(False)
        self._dual_tgt_wrap.viewport().setAttribute(Qt.WA_TranslucentBackground, True)
        self._dual_tgt_wrap.viewport().setStyleSheet("background: transparent;")
        self._dual_tgt_body = QWidget()
        self._dual_tgt_lay = QVBoxLayout(self._dual_tgt_body)
        self._dual_tgt_lay.setContentsMargins(0, 0, 0, 0)
        self._dual_tgt_lay.setSpacing(6)
        self._dual_tgt_lay.addStretch(1)
        # 空态占位（与列表区同款 PanelHint）——此前写在译文标签里，译文改逐句
        # 累积后必须有独立标签，否则占位会被当成第一句永久留在栏里
        self._dual_hint = QLabel("")
        self._dual_hint.setObjectName("PanelHint")
        self._dual_hint.setWordWrap(True)
        self._dual_tgt_lay.insertWidget(0, self._dual_hint)
        self._dual_tgt_wrap.setWidget(self._dual_tgt_body)
        self._keep_content_clear(self._dual_tgt_body)
        dl.addWidget(self._dual_src_wrap, 1)
        dl.addWidget(self._dual_sep)
        dl.addWidget(self._dual_tgt_wrap, 2)
        # v2.18.1：两栏各自的"用户上滚回看"检测（与列表区同一语义）
        self._dual_src_wrap.verticalScrollBar().valueChanged.connect(
            lambda v: self._on_dual_wrap_scroll("src", v))
        self._dual_tgt_wrap.verticalScrollBar().valueChanged.connect(
            lambda v: self._on_dual_wrap_scroll("tgt", v))
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
        # v2.20.1：逐句新建的条目标签/卡片在 `_dual_new_slot` 里补挂过滤器。
        self._dual_split_watch = [
            self._dual_body, self._dual_sep, self._dual_hint,
            self._dual_src_body, self._dual_tgt_body,
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
        prefix_hit = None
        reverse_hit = None
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
            # v2.19.1：列表的流式草稿行常是终版的**前缀**（whisper 终版
            # 只是补了句读或尾部一词）——不认这条就会同句多出一行重复字幕。
            # ≥8 字符护栏防短句误配；倒序扫描天然偏向最新待决行（即生长行）
            if s and len(s) >= 8 and key.startswith(s) and prefix_hit is None:
                prefix_hit = it
            # v2.19.3：**反方向**——流式草稿经常比终版**更长**（4s 窗口把下一句
            # 开头也转写进来，或句中分叉导致短语复读），此时终版是草稿的前缀。
            # 旧实现只认上面那个方向，于是同一句的两行并存：膨胀的草稿行挂着
            # 推测译留在屏上，干净的终版另起一行（真机 DW News 直播 150s 实测：
            # 同一句的复读版与干净版各占一行，翻译缓存里送译原文只出现一次）。
            # 命中后由 show_pending_result 把行文本**换成权威终版**，复读随之消失。
            if s and len(key) >= 8 and s.startswith(key) and reverse_hit is None:
                reverse_hit = it
        return suffix_hit or prefix_hit or reverse_hit

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
        if self.is_dual():
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
        if (last is not None and source_text
                and self._dual_same_sentence(last["src_text"], source_text)):
            # v2.19.1：流式草稿已把同句先行上屏（列表路径亦然），随后到达的
            # 正式片段虽大写开头也**必须就地校准该行**而非另起新行——否则同句
            # 两行并存，且草稿行的迟到终版会配错行
            last["src_text"] = source_text
            last["src"].setText(source_text)
            last["src"].setVisible(bool(source_text) and self._show_source)
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
        if self.is_dual():
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
        if self.is_dual():
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
        拼成整句传入，**当前句那一行**整体刷新（每 ~0.9s 一拍，实现"主持人讲到
        哪、原文跟到哪"）。终版收口（show_pending_result）会以正式文本覆盖；
        列表模式忽略。样式与常态原文一致（草稿的未定稿感由高频刷新自证）。
        v2.20.1：刷新范围只有"当前句"这一条——上一句已收口的行原地不动。"""
        if self._layout_mode != "dual":
            return
        t = (text_full or "").strip()
        if not t:
            return
        cur = self._dual_cur_src().strip()
        if cur and self._dual_same_sentence(cur, t):
            # 同一句（还在生长，或终版之后 whisper 又转了一遍的尾重复）：
            # 只刷新**最新一行**的原文，该行译文状态一律不动
            # （v2.19.1 的"不闪白""不重置终版"两条契约）
            # v2.20.1：收口行不许被更短的回声拍打回半句（与 _dual_show_pending
            # 同一条判据）——累积形态下这一行会永久停在残缺文本上
            if len(t) > len(cur) or self._dual_cur_open:
                self._dual_set_src(t)
        else:
            # 不是同一句 → 新句：另起一行（上一句留在栏里，v2.20.1 累积契约）。
            # 旧行为是整块覆盖唯一那一对标签，于是"新句一到、上一句就消失"
            # （用户实拍），并且留下一拍~两拍的"新句原文 + 上一句终版译文"
            # 错配窗口（v2.19.1 元凶）。
            self._dual_new_sentence(t)
        self._sync_dual_visibility()
        self._schedule_relayout()

    def update_dual_draft_tgt(self, translated, source_text=None):
        """v2.13.0：草稿推测译文——**只更新当前句的译文卡片**（spec 淡样式），
        不碰原文/行簿记/_last_result/未读计数；片段级推测版（_dual_spec）与整句终版
        （_dual_show_result）随后自然覆盖。dual 专属（调用方已按布局闸门过滤，
        这里再防一道）。效果：译文与原文同节奏实时生长（0.9s 级）。
        v2.19.1：可选 `source_text` 配对——该句已终版收口时迟到的草稿回复
        直接丢弃，不得把已定稿译文刷回淡色。"""
        if self._layout_mode != "dual" or not translated:
            return
        if self._dual_tgt is None:
            return
        if (source_text and not self._dual_cur_open
                and self._dual_same_sentence(self._dual_cur_src(), source_text)):
            return
        self._dual_tgt.setProperty("spec", True)
        self._dual_tgt.setProperty("empty", False)
        self._dual_tgt.setText(translated)
        self._restyle_dual_tgt()
        self._sync_dual_visibility()
        self._schedule_relayout()

    def show_caption(self, source_text, target_text, show_source=True):
        """一次性上屏（无占位）。v2.11.0：dual 模式同终态收口路径。"""
        if self.is_dual():
            self._dual_show_result(source_text, target_text, show_source)
            return
        self._show_source = bool(show_source)
        self._last_result = (source_text or "", target_text or "")
        self._add_row(source_text or "", target_text or "", False)
        self._count_unread()
        self._sync_bar_texts()

    def clear_caption(self):
        if self.is_dual():
            # v2.19.2：dual 分支漏清"最近一句"——清空后面板空白，但「复制最近一句 /
            # 纠正最近识别 / 纠正译文」仍指向已被清掉的句子（列表分支无此问题）
            self._last_result = ("", "")
            # v2.20.1：两栏逐句累积 → 清空要把**所有**条目删掉（旧实现只有一对
            # 标签，setText("") 就够）。当前句指针一并归 None、逐行收口账目清空，
            # 下一句重新开槽。
            gone_s, gone_t = self._dual_src_items, self._dual_tgt_items
            self._dual_src_items, self._dual_tgt_items = [], []
            self._dual_rows_closed = []
            self._dual_src = None
            self._dual_tgt = None
            for it in gone_s:
                it["lab"].setParent(None)
                it["lab"].deleteLater()
            for it in gone_t:
                it["card"].setParent(None)
                it["card"].deleteLater()
            self._dual_unwatch(*[it["lab"] for it in gone_s],
                               *[w for it in gone_t
                                 for w in (it["card"], it["lab"])])
            self._sync_dual_visibility()
            self._update_empty_hint()
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
        """当前是否处于上下双语布局（主窗/测试用）。

        v2.20.0：dual 只有**一种**形态——上半原文 / 可拖分割线 / 下半译文。
        旧的 `overlay_dual_hist` 两态（滚动字幕墙 vs 顶部历史块）已退役。"""
        return self._layout_mode == "dual"

    # ---------- v2.20.1：两栏逐句累积（原文一行、译文一张卡片） ----------

    def _dual_cur_src(self):
        """当前句原文文本（无槽时空串，供各入口统一读）。"""
        return self._dual_src.text() if self._dual_src is not None else ""

    @property
    def _dual_cur_open(self):
        """最新一行是否还在说（未收口）。单一真相源是 `_dual_rows_closed`。"""
        return bool(self._dual_rows_closed) and not self._dual_rows_closed[-1]

    def _dual_col_has_text(self, col):
        """栏内是否有**真实内容**（译文栏排除 "…" 占位与空态）。"""
        items = self._dual_src_items if col == "src" else self._dual_tgt_items
        for it in items:
            lab = it["lab"]
            if col == "tgt" and bool(lab.property("empty")):
                continue
            if (lab.text() or "").strip():
                return True
        return False

    def _dual_new_slot(self, src_text=""):
        """开一对新条目并把它设为"当前句"。

        译文用卡片壳（`DualTgtRow` / 最新一句 `DualTgtRowNewest` = 圆角底 +
        左侧主题色竖条，用户点名的样式）；原文只是淡灰一行，不做卡片。
        瞬窗防线同 `_add_row`（v2.6.6）：**构造即传父**、可见性切换一律在
        `addWidget` 之后——未收编的 QWidget 被 show 会建成原生顶层窗口。"""
        lab_s = QLabel(src_text, self._dual_src_body)
        lab_s.setObjectName("DualSrc")
        lab_s.setWordWrap(True)
        lab_s.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        lab_s.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._dual_src_lay.insertWidget(self._dual_src_lay.count() - 1, lab_s)
        self._dual_src_items.append({"lab": lab_s, "text": src_text})

        card = QWidget(self._dual_tgt_body)
        card.setObjectName("DualTgtRowNewest")
        card.setAttribute(Qt.WA_TranslucentBackground, True)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(8, 6, 8, 6)
        cl.setSpacing(1)
        lab_t = QLabel("", card)
        lab_t.setObjectName("DualTgt")
        lab_t.setWordWrap(True)
        lab_t.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        lab_t.setTextInteractionFlags(Qt.TextSelectableByMouse)
        # 空态占位标记初始化：property 未设时会被当成"有译文"（v2.16.0 的
        # "神秘分割线"病根），新卡片一律先按空态起
        lab_t.setProperty("empty", True)
        lab_t.setProperty("spec", False)
        cl.addWidget(lab_t)
        # 上一张卡片退出"最新"强调（列表行同款：只有最后一张带蓝条）
        if self._dual_tgt_items:
            prev = self._dual_tgt_items[-1]["card"]
            prev.setObjectName("DualTgtRow")
            prev.style().unpolish(prev)
            prev.style().polish(prev)
        self._dual_tgt_lay.insertWidget(self._dual_tgt_lay.count() - 1, card)
        self._dual_tgt_items.append({"card": card, "lab": lab_t})
        self._dual_rows_closed.append(False)

        self._dual_src = lab_s
        self._dual_tgt = lab_t
        for w in (lab_s, card, lab_t):
            w.installEventFilter(self)
            self._dual_split_watch.append(w)
        self._dual_trim()
        self._apply_dual_fonts()
        return lab_s

    def _dual_slot_has_content(self):
        """当前这一对里是否已经有真内容（译文 "…" 占位不算）。"""
        if self._dual_src is None:
            return False
        if self._dual_src.text().strip():
            return True
        t = self._dual_tgt.text().strip()
        return bool(t) and not bool(self._dual_tgt.property("empty"))

    def _dual_row_for(self, src_text=""):
        """定位 `src_text` 该写进**哪一行**，返回行号。

        为什么必须按原文找而不是永远写最新一行：累积之后，正式片段/终版译文
        可能**迟到**——新句的流式拍已经把下一行开出来了，这时上一句的终版才到，
        写进最新行就等于"A 的译文盖在 B 的原文上"（v2.19.1 花大力气消灭的错配
        窗口，在累积模式下会以更糟的形式复发）。所以：
        ① 从最新往回找**同源且未收口**的行；
        ② 找不到 → 当前行还空就用它，否则另起一行。"""
        key = (src_text or "").strip()
        if key:
            for i in range(len(self._dual_src_items) - 1, -1, -1):
                if self._dual_rows_closed[i]:
                    continue
                if self._dual_same_sentence(self._dual_src_items[i]["text"], key):
                    return i
        if self._dual_src is None or self._dual_slot_has_content():
            self._dual_new_slot(key)
        elif key:
            self._dual_set_src(key)
        return len(self._dual_src_items) - 1

    def _dual_set_row(self, idx, src_text=None, tgt_text=None):
        """把文本写进指定行（同步簿记）。"""
        if src_text is not None and 0 <= idx < len(self._dual_src_items):
            self._dual_src_items[idx]["lab"].setText(src_text)
            self._dual_src_items[idx]["text"] = src_text
            if idx == len(self._dual_src_items) - 1:
                self._dual_src = self._dual_src_items[idx]["lab"]
        if tgt_text is not None and 0 <= idx < len(self._dual_tgt_items):
            self._dual_tgt_items[idx]["lab"].setText(tgt_text)

    def _dual_unwatch(self, *ws):
        """条目销毁后必须从分割线事件过滤器的观察名单里摘掉——否则列表无限增长，
        且 Qt 会对已 delete 的包装对象再发事件。"""
        for w in ws:
            if w in self._dual_split_watch:
                self._dual_split_watch.remove(w)

    def _dual_trim(self):
        """两栏各自裁到上限，**成对删除**保持行号对齐（超出丢最老）。"""
        while len(self._dual_src_items) > self.MAX_DUAL_LINES:
            it = self._dual_src_items.pop(0)
            t = self._dual_tgt_items.pop(0)
            self._dual_rows_closed.pop(0)
            it["lab"].setParent(None)
            it["lab"].deleteLater()
            t["card"].setParent(None)
            t["card"].deleteLater()
            self._dual_unwatch(it["lab"], t["card"], t["lab"])

    def _dual_col_height(self, col, avail_w):
        """某一栏**累积内容**的需求高度：逐条 heightForWidth 求和再加间距。

        QLabel 带 wordWrap 时 sizeHint 是**单行**值，长句必被算小（真机截图
        实证过两次），只有 heightForWidth(可用宽度) 才是换行后的真实高度。
        译文条目外面套了卡片壳（纵向 margins 6+6=12、左右 8+8），要一并算进
        可用宽度与高度，否则卡片底部会被裁。"""
        if col == "src":
            hs = [it["lab"].heightForWidth(max(40, avail_w))
                  for it in self._dual_src_items]
            gap = 5
        else:
            hs = [it["lab"].heightForWidth(max(40, avail_w - 16)) + 12
                  for it in self._dual_tgt_items]
            gap = 6
        if not hs:
            return 0
        return sum(max(16, h) for h in hs) + gap * (len(hs) - 1)

    def _dual_want_height(self):
        """dual 正文区的内容需求高度（两栏累积内容 + 把手 + 边距）。"""
        avail = max(80, self._dual_body.width() - 24)
        src_h = (self._dual_col_height("src", avail - 2)
                 if self._src_col_shown() else 0)
        tgt_h = self._dual_col_height("tgt", avail - 2)
        return src_h + tgt_h + (8 if src_h else 0) + 24

    def _src_col_shown(self):
        """原文栏是否参与布局：「同时显示原文」开着且栏里确实有原文。"""
        return bool(self._show_source) and bool(self._dual_src_items)

    def set_dual_src_h_user(self, h):
        """v2.16.0：原文区用户高度入口（主窗配置恢复/0=回自动贴内容）。
        拖原文/译文之间的分割线改的就是这个值——原文区想多大拖多大，
        译文区吃剩余（两者各自可滚动）。"""
        self._dual_src_h_user = int(h) if h and int(h) >= 30 else None
        if self.is_dual() and not self._collapsed:
            self._relayout()

    def _on_dual_wrap_scroll(self, key, v):
        """v2.18.1：用户在原文/译文区内上滚回看 → 暂停该区自动跟底；滚回底部恢复。"""
        wrap = self._dual_src_wrap if key == "src" else self._dual_tgt_wrap
        sb = wrap.verticalScrollBar()
        self._dual_follow[key] = (v >= sb.maximum() - 4)

    def _dual_follow_bottom(self):
        """v2.18.1：dual 原文/译文区内容超出可视高度时**自动跟底**。

        真机英语新闻实测（BBC Global News Podcast 整集）：长句把最新文字推到可视区
        之外，滚动条 max=49 却停在 value=0——用户看不到刚说出的那几个字，必须自己
        滚轮。流式字幕里这是硬伤：面板存在的意义就是"看到正在说的话"。
        用户主动上滚回看时不打断（`_on_dual_wrap_scroll`）。"""
        for wrap, key in ((self._dual_src_wrap, "src"), (self._dual_tgt_wrap, "tgt")):
            if not self._dual_follow.get(key, True):
                continue
            sb = wrap.verticalScrollBar()
            if sb.maximum() > 0 and sb.value() < sb.maximum():
                sb.setValue(sb.maximum())

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

    # ---------- v2.20.0：攒句开关（面板 ⋯ 菜单 ↔ 设置页 ↔ 配置） ----------

    def set_grouping_enabled(self, on):
        """主窗按配置 `translate_grouping` 下发面板勾选态（唯一写入口）。

        面板只镜像状态：真正的判据在主窗 `_grouping_enabled()`，每个识别片段
        实时读配置，所以这里不需要（也不该）另存一份行为开关。"""
        self._grouping = bool(on)

    def is_grouping_enabled(self):
        return bool(self._grouping)

    def _sync_dual_visibility(self):
        """原文栏与分隔线随「同时显示原文」开关与栏内有无内容显隐。

        关掉原文 = 纯译文模式（分隔线一并隐藏）。v2.20.1：改判**整栏**——
        原文栏是逐句累积的一列标签，"有没有原文"看栏内任意一句，不看当前句。
        （v2.18.1 的教训保留：关原文时必须连 QScrollArea 本体一起收起，
        只隐标签会让它继续占高、把译文栏饿成细条。）"""
        has_src = self._dual_col_has_text("src")
        has_tgt = self._dual_col_has_text("tgt")
        show = self._show_source and has_src
        self._dual_src_wrap.setVisible(show)
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
        """新句起点：另起一对条目（上一句留在栏里），译文进占位态（推测淡样式）。

        v2.20.1：从"整块覆盖当前句"改为**逐句累积**——上一句不再被顶掉。
        调用方已判定"这不是当前句的延伸"，所以 `_dual_row_for` 必然落到
        "另起一行"分支，当前行/行指针即最新一行。"""
        self._dual_row_for(src_text)
        # v2.18.1：新句开始重新跟底——用户在上半句里上滚回看，不应把下一句
        # 的最新文字也一起挡住（回看语义属于过去那句）
        self._dual_follow = {"src": True, "tgt": True}
        self._dual_tgt.setProperty("spec", True)
        self._dual_tgt.setProperty("empty", False)
        self._dual_tgt.setText("…")
        self._restyle_dual_tgt()
        self._sync_dual_visibility()
        self._update_empty_hint()
        self._schedule_relayout()

    @staticmethod
    def _dual_same_sentence(a, b):
        """同句判据（v2.19.1）：流式草稿与随后到达的正式片段是否**同一句**。
        前缀包含关系，或（分词后）前 3 词同源即算同句。
        用途：① 正式片段落在已上屏的同句草稿上时，校准原文而非重置译文区
        （消灭"译文闪白再来一遍"）；② 终版闭合后，流式拍只有**不是当前句
        延伸**才判为新句并原子换句。两句连贯新闻共享前 3 词的概率极低，
        误并的代价远小于误切（闪白）的代价。"""
        a = (a or "").strip()
        b = (b or "").strip()
        if not a or not b:
            return False
        if a.startswith(b) or b.startswith(a):
            return True
        ta = a.lower().split()
        tb = b.lower().split()
        if len(ta) < 3 or len(tb) < 3:
            return False
        if all(x == y for x, y in zip(ta[:3], tb[:3])):
            return True
        # v2.20.1：回声判据——b 的词 ≥85% 已在 a 里出现过，算同一句。真机 91s
        # 英语新闻实测：末句收口后预览缓冲重启，下一拍把已上屏那一段又播了一遍
        # （"…across the board. on the sports desk" 之后跟来 "inflation readings
        # … on the sports desk"）。单行时代它只是覆盖同一块文本无人看见，累积
        # 形态下会变成并排的两句重复。
        sa = set(ta)
        return sum(1 for w in tb if w in sa) / len(tb) >= 0.85

    def _dual_set_src(self, text):
        """写当前句原文，并同步累积条目里的簿记文本（高度/有无内容判据都读它）。"""
        if self._dual_src is None:
            return
        self._dual_src.setText(text)
        if self._dual_src_items:
            self._dual_src_items[-1]["text"] = text

    def _dual_show_pending(self, source_text):
        t = (source_text or "").strip()
        if not t:
            return
        cur = self._dual_cur_src().strip()
        if not cur:
            self._dual_new_sentence(t)
            return
        if not self._starts_new_sentence(t):
            # 延续片段（小写开头）：拼进当前句（D-2：主窗每段只发一次占位）
            self._dual_set_src(self._dual_join(cur, t))
            self._sync_dual_visibility()
            self._schedule_relayout()
            return
        # 大写/CJK/数字开头名义上是"新句起点"——v2.19.1 修正：流式路径下同一句
        # 的草稿往往已在屏上生长（真机时序：draft beat 先行，正式片段晚到），
        # 此时重置会把该句已就位/正生长的推测译打回 "…"（用户实拍"闪白"）。
        # 同句 → 就地校准原文，译文区状态**不动**。
        if self._dual_same_sentence(cur, t):
            if len(t) > len(cur) or self._dual_cur_open and len(t) >= len(cur):
                self._dual_set_src(t)
            self._sync_dual_visibility()
            self._schedule_relayout()
            return
        self._dual_new_sentence(t)

    def _dual_spec(self, source_text, target_text, show_source):
        """推测中间版：原文校准为整句、译文淡色就地更新（失败静默等终版）。

        v2.20.1：按原文定位行——迟到的推测版要写回**它自己那一句**，不能盖在
        已经开出来的下一行上。"""
        if not target_text:
            return
        self._show_source = bool(show_source)
        idx = self._dual_row_for(source_text or "")
        lab_t = self._dual_tgt_items[idx]["lab"]
        lab_t.setProperty("spec", True)
        lab_t.setProperty("empty", False)
        lab_t.setText(target_text)
        self._tgt_label_font(lab_t)
        self._sync_dual_visibility()
        self._schedule_relayout()

    def _dual_show_result(self, source_text, target_text, show_source):
        """终版收口：译文转正式样式（主字号加粗纯色），原文校准为整句，
        并把**该行**记账为已收口（`_dual_rows_closed`）。

        v2.19.1：收口即闭合该句——此后的第一个流式拍/新片段会另起一对条目；
        v2.20.1：上一句不再被顶掉，两句都留在各自栏里。终版按原文找自己的行，
        因此"新句已先行上屏、上一句终版迟到"时不会写错行。"""
        self._show_source = bool(show_source)
        self._last_result = (source_text or "", target_text or "")
        # v2.4.4（BUG-7）同一语义在 dual 的补漏：引导小抄一经真实字幕上屏就
        # 完成使命。旧实现只在 `_add_row`（列表路径）复位，dual 走不到
        # 那里 → 清空后三行小抄反复重弹，违反"每份配置只弹一次"。
        self._hint_guide = False
        idx = self._dual_row_for(source_text or "")
        self._dual_set_row(idx, src_text=source_text or None)
        lab_t = self._dual_tgt_items[idx]["lab"]
        lab_t.setProperty("spec", False)
        lab_t.setProperty("empty", not bool(target_text))
        lab_t.setText(target_text or "…")
        self._tgt_label_font(lab_t)
        self._dual_rows_closed[idx] = True
        self._sync_dual_visibility()
        self._update_empty_hint()
        self._schedule_relayout()

    def _update_empty_hint(self):
        """B/D：无内容时显示占位（或首次手势引导），来字即隐；顺带门控清空按钮。
        v2.11.0：dual 模式空态时译文区**常驻占位文案**（真机截图实证：不写的话
        空面板是一片空白，用户不知道这里是干嘛的）；有内容则不动。
        v2.20.1：dual 的占位从"写进译文标签"改成独立 `_dual_hint`——译文改逐句
        累积后，写进标签会被当成第一句永久留在栏里（列表区 `_hint` 同款做法）。"""
        if self.is_dual():
            has = self._dual_col_has_text("src") or self._dual_col_has_text("tgt")
            if not has:
                self._dual_hint.setText(
                    self.HINT_GUIDE if self._hint_guide else self.HINT_IDLE)
            self._dual_hint.setVisible(not has)
            self._clear_btn.setEnabled(has)
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
        if self.is_dual() and not self._collapsed:
            want0 = self._dual_want_height()
            h0 = self._dual_body.height()
            self._relayout()
            if ((self._dual_want_height() != want0
                 or self._dual_body.height() != h0) and self._relayout_passes < 12):
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
        elif self.is_dual():
            # v2.20.0（用户实拍裁决）：dual 只有一块正文——原文区 / 可拖分割线 /
            # 译文区，面板高度贴内容。旧"顶部历史块吃剩余"的三分分配随历史区
            # 一并删除；拖底缘仍是把整个正文拉高（多余空间归原文区，见下）。
            self._mini.setVisible(False)
            self._scroll.setVisible(False)
            self._dual_body.setVisible(True)
            self._jump_btn.hide()
            # 先解除上一轮 setFixedHeight 的钳制——否则 wordWrap 文本变长时
            # label 被压在旧高度里，终版长译文底部裁切（真机截图实证）
            self._dual_body.setMinimumHeight(0)
            self._dual_body.setMaximumHeight(16777215)
            chrome = 54 + 12                          # 工具条 + outer margins/spacing
            # v2.18.1：关原文时原文区与分隔把手**都不该占高**——旧算法恒加
            # sep_h 8 与"三控件"的 24px 边距/间距，导致译文区被饿到 21px
            # （真机实测，用户配置正是「原文 关」）。
            src_on = self._src_col_shown()
            sep_h = 8 if src_on else 0
            avail_w = max(80, self._dual_body.width() - 24)
            # v2.20.1：两栏都是**累积内容**的高度（逐条 heightForWidth 求和）
            src_want = self._dual_col_height("src", avail_w - 2) if src_on else 0
            tgt_want = self._dual_col_height("tgt", avail_w - 2)
            # v2.18.0：用户拖过的原文区高度优先计入 body 需求——自动高度
            # 模式下拖大原文区 → body 随之撑大（否则用户值被钳回 30 无效）
            if self._dual_src_h_user and src_on:
                src_want = max(30, self._dual_src_h_user)
            # dl 纵向 margins 14 + 可见控件之间的 spacing 5×(n-1)
            body_want = (src_want + tgt_want + sep_h + 14
                         + 5 * (2 if src_on else 0))
            screen_h = int(QGuiApplication.primaryScreen().availableGeometry().height()
                           or 800)
            # body_min＝可拖下限：原文 30 + 把手 8 + 译文 30 + 边距。低于此值时
            # 分割线钳制区间 [30, body_h-sep_h-30] 宽度归零 → 彻底拖不动
            # （v2.19.0 真实事件流实测 0 位移的根因）
            body_min = 92 if src_on else 60
            if self._user_height:
                total = self._user_height          # 用户拖过底缘：总高锁定
            else:
                total = min(chrome + max(body_want, body_min),
                            int(screen_h * 0.68))
            avail = max(46, total - chrome)
            body_h = max(min(body_min, avail), avail)
            self._dual_body.setFixedHeight(body_h)
            # 当前句区内部——原文区用户高度（拖 sep 分割线得出）固定生效，
            # 译文区吃剩余（两者各自可滚动，永不互相裁切）。
            if self._dual_src_h_user:
                self._dual_src_wrap.setFixedHeight(
                    max(30, min(self._dual_src_h_user,
                                max(30, body_h - sep_h - 30))))
            else:
                self._dual_src_wrap.setMinimumHeight(0)
                self._dual_src_wrap.setMaximumHeight(16777215)
            # dual 面板**总高显式锁定**：正文用 setFixedHeight 定高后，adjustSize
            # 仍会按 QScrollArea 的默认 sizeHint（192px）把面板缩回去，故总高自己算
            self._dual_total_h = total
            # v2.18.1：本轮高度分配落地后跟底（等 viewport 尺寸真正变化再算，
            # 否则 maximum 还是旧值）——长句最新文字不得留在可视区之外
            QTimer.singleShot(0, self._dual_follow_bottom)
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
        # v2.17.0a：dual 模式跳过 adjustSize——QScrollArea 默认 sizeHint 高
        # 192 会把面板缩回去（fixed 高度的历史区被裁），总高已显式锁定
        # （_dual_total_h = chrome+历史+当前句+外框）
        w0 = self.width()
        if self.is_dual() and not self._collapsed:
            self.resize(w0, int(getattr(self, "_dual_total_h", self.height())))
        else:
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

    def _toggle_running(self):
        """v2.20.1：面板「开始 / 停止翻译」把手——只转发，不改文案。

        文案/悬停提示一律由主窗回灌（`set_running` 在 `update_overlay_status`
        里被调），因为"点了到底停没停"只有主窗知道：热键、托盘、主窗按钮三条
        路径都能改态，面板自己翻转必然谎报。"""
        if self._on_toggle_running:
            self._on_toggle_running()

    def set_running(self, on):
        """同步运行态显示（主窗 update_overlay_status 是唯一调用方）。"""
        self._running = bool(on)
        self._run_btn.setText("⏸ 暂停" if self._running else "▶ 开始")
        self._run_btn.setStyleSheet(
            "color: #8fd18a;" if self._running else "color: #ffc46b;")
        self._run_btn.setToolTip(
            ("停止翻译（运行中）" if self._running else "开始翻译（已停止）")
            + " · 与全局热键、托盘菜单同一个开关")

    def set_show_source(self, on):
        """外部（设置页/回调）同步原文开关的显示态。"""
        self._show_source = bool(on)
        for it in self._rows:
            it["src"].setVisible(bool(it["src_text"]) and self._show_source)
        if self._collapsed:
            self._update_mini()
        # v2.18.1：dual 区必须一并重算可见性——此前漏调 _sync_dual_visibility，
        # 「原文 关」时原文行与那条可拖分割线**仍留在面板上**（用户截图实证：
        # 关着原文却看得见 sep 线）。用户裁决：关原文=没有两栏要分=面板无可见线。
        self._sync_dual_visibility()
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
        # v2.19.4：首行写真实字号——四档（16/22/30/40）只是"快捷档"，滚轮与
        # 设置页能落在档外的值（18/19/44…），旧菜单在这种值下要么双勾要么无勾，
        # 用户看不出自己现在到底多大。
        hdr = m.addAction("当前字号 %dpx" % int(self._font_size))
        hdr.setEnabled(False)
        self._font_header = hdr
        for name, px in self.FONTS:
            a = m.addAction(f"{name}（{px}px）")
            a.setCheckable(True)
            a.triggered.connect(lambda _c=False, p=px: self._pick_font(p))
            self._font_actions.append((a, px))
        self._sync_font_checks()
        return m

    def _sync_font_checks(self):
        """v2.5.3：勾选态随当前字号同步互斥——此前菜单只在构造时 setChecked
        一次且非互斥，换档/滚轮调节后旧勾永不消失（用户实测四档全勾）。

        v2.19.4：改为**只勾最接近的一档（唯一）**。上一版用 `abs(px-fs) <= 3`
        的半径，档位间距只有 6px，半径彼此重叠 → offscreen 实测：19px 同时勾上
        「小号16」与「中号22」、12px 与 44px 整组无勾、18px 勾的是"16px"——
        菜单显示与实际字号不符（红线）。真实字号现在直接写在菜单首行。"""
        acts = getattr(self, "_font_actions", [])
        if not acts:
            return
        nearest = min(acts, key=lambda ap: abs(ap[1] - self._font_size))[1]
        for a, px in acts:
            a.setChecked(px == nearest)
        hdr = getattr(self, "_font_header", None)
        if hdr is not None:
            hdr.setText("当前字号 %dpx" % int(self._font_size))

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
        if self.is_dual():
            if self._dual_cur_src().strip():
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
        dual = self.is_dual()
        self._jump_btn.setVisible(not self._follow and not self._collapsed and not dual)
        self._scroll.setVisible(not self._collapsed and not dual)
        self._dual_body.setVisible(not self._collapsed and dual)
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

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # v2.19.1（真机截图实证）：WA_TranslucentBackground 无边框窗口 resize 后
        # **新暴露区域不会自动重绘**——面板每来一句长高、启动时宽度从初始值撑到
        # 持久化的 619，右半/下半永远停在壁纸透明（右缘把手三点画在旧宽度处即
        # 铁证）。尺寸变化即全窗 update。
        self.update()

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
        # v2.19.1：地板 30 与面板快捷档/设置页滑条统一——历史配置里已存在的 0
        # 不再能在重启后把面板变成全透明空壳（真机实录）；换算收进
        # opacity_to_alpha（v2.19.2：旧 int(v*2.55) 让 100% 实际是 254）。
        self._bg_alpha = opacity_to_alpha(bg_opacity)
        self._apply_qss()
        self._sync_bar_texts()
        self._relayout()
        self._schedule_relayout()

    def set_session_has_content(self, has):
        """v2.19.4：主窗告知"本会有没有字幕"，用于 ⋯ 菜单「导出 SRT…」置灰。

        主窗的「导出」按钮 v2.6.5（R7-L3）就已无字幕禁用，面板这个入口没门控：
        零字幕时点了只弹一句"当前会话还没有可导出的字幕"——同一动作两入口
        一个亮一个灰，正是 v2.19.2 为「清空」修过的老毛病。"""
        self._session_has = bool(has)

    def _keep_content_clear(self, w):
        """QScrollArea 的内容控件必须在 setWidget **之后**再关一次 autoFill。

        setWidgetResizable(True) 时 Qt 在 setWidget 内部（C++ 侧，Python 层
        追不到这次调用）把内容控件的 autoFillBackground 打开；叠加
        WA_TranslucentBackground 后，该控件每帧把自己整块矩形擦成 alpha 0，
        连面板 paintEvent 画好的圆角底色一起抹掉——正文区因此整片透出桌面
        （真机像素实测：body=壁纸蓝 (15,157,250)，关掉 autoFill 立刻回到
        底色 (28,32,39) 且 std=0；单独关 translucent 或只 repaint() 均无效，
        所以 v2.19.1 那句 resizeEvent→update() 并没有修到这一层）。
        """
        w.setAutoFillBackground(False)
        w.setAttribute(Qt.WA_TranslucentBackground, True)

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
            /* v2.19.4：占位/引导文字对比度 72/255 合成后约 2.5:1，低于可读下限
               （承载的是首启三行小抄）；提到 110 后约 4.6:1。像素锁判的是
               "整行均匀亮像素"，文字占比仅 0.66，不受此改动影响 */
            QLabel#PanelHint {{ color: rgba(255,255,255,110); font-size: 12px; }}
            QLabel#PanelSrc {{ font-size: {src_fs}px; color: #98a2b3; }}
            QLabel#PanelTgt {{ font-size: {fs}px; color: {self._text_color.name()}; font-weight: 600; }}
            QLabel#PanelMiniSrc {{ font-size: {max(11, int(fs * 0.62))}px; color: #98a2b3; }}
            QLabel#PanelMiniTgt {{ font-size: {fs}px; color: {self._text_color.name()}; font-weight: 600; }}
            QWidget#PanelDual {{ background: transparent; }}
            QLabel#DualSrc {{ color: #98a2b3; }}
            /* v2.20.1：译文栏改逐句累积，每句一张卡片（用户实拍点名的样式）——
               与列表行同一套观感：素底圆角 + 3px 左侧竖条，只有最新一句竖条上
               主题色。原文栏按用户裁决**不做卡片**（保持淡灰一行）。 */
            QWidget#DualTgtRow {{ background: rgba(255,255,255,8); border-radius: 8px;
                                  border-left: 3px solid transparent; }}
            QWidget#DualTgtRowNewest {{ background: rgba(79,140,255,26); border-radius: 8px;
                                        border-left: 3px solid #4f8cff; }}
            QLabel#DualTgt {{ color: {self._text_color.name()}; }}
            QLabel#DualTgt[spec="true"] {{ color: rgba(255,255,255,205); }}
            QLabel#DualTgt[empty="true"] {{ color: rgba(255,255,255,110); }}
            /* v2.16.1：DualSep 把手为自绘（_DualSepHandle：平时仅一条极淡
               细线，悬停浮现中央胶囊）——旧的 8px 实心灰带"太生硬、难看"。
               注意：QSS 只认 C 风格块注释，用 # 写注释会让 Qt 从该行起丢弃
               后续全部规则（最小实验实测：井号夹在中间则后面的规则全不生效；
               放在首行则整张表作废）。本条注释此前正是井号写法，其后的
               PanelRow / PanelRowNewest / PanelScroll / QScrollBar 等 12 条
               规则从未生效过。另：注释正文里不得出现块注释的闭合符。 */
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
        实证两次）。颜色等外观仍走 QSS（_apply_qss），度量归 setFont。
        v2.20.1：两栏都逐句累积 → 字号变化要**重刷全部条目**，只刷当前句会
        让旧行的度量停在旧字号（面板高度与文本一起错位）。"""
        fs = max(10, int(self._font_size))
        f_src = QFont()
        f_src.setPixelSize(max(11, int(fs * 0.78)))
        f_src.setWeight(QFont.DemiBold)
        for it in self._dual_src_items:
            it["lab"].setFont(f_src)
        f_fin = QFont()
        f_fin.setPixelSize(fs)
        f_fin.setWeight(QFont.Bold)
        for it in self._dual_tgt_items:
            if it["lab"] is not self._dual_tgt:
                it["lab"].setFont(f_fin)
        if self._dual_tgt is not None:
            self._restyle_dual_tgt()

    def _tgt_label_font(self, w):
        """按 spec/empty 属性态给**指定**译文标签落地字号/字重（setFont，原因见
        _apply_dual_fonts），并重刷 QSS 颜色（Qt 不自动感知属性态样式）。"""
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

    def _restyle_dual_tgt(self):
        """重刷**当前句**译文标签的字体与属性态样式。"""
        w = self._dual_tgt
        if w is None:
            return
        self._tgt_label_font(w)

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
            # v2.18.0：分割把手三点已移除——面板只保留一条可拖分割线
            # （原文/译文之间的 DualSep 把手）+ 右缘/底缘把手

    # ---------- 鼠标：整板拖移 + 右缘调宽 + 底缘调高 ----------

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
            # v2.18.0：分割拖拽只走事件过滤器（sep 本体）
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
        # v2.15.2/v2.18.0：分割拖拽只走事件过滤器（sep 本体），此路径已无分割事件
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
        if near_right:
            self.setCursor(Qt.SizeHorCursor)
        elif near_bottom:
            self.setCursor(Qt.SizeVerCursor)
        else:
            self.setCursor(Qt.ArrowCursor)

    def _dual_split_apply_drag(self, g):
        """v2.16.0：按当前全局鼠标位置应用分割拖拽（原文区高度=起始值+dy，
        上移原文区变小、下移变大；译文区吃剩余，各自可滚动）。
        v2.19.0：上限统一为 body − sep − 30（译文保底）→ 分割线**一对一跟手**
        （旧几何下拖拽区间宽度为 0，实测鼠标 ±60/120px 线位移 0，或反向跳变）。"""
        dy = g.y() - self._dual_split_start_y.y()
        new_h = max(30, min(self._dual_split_start_h + dy,
                            max(30, self._dual_body.height() - 8 - 30)))
        self._dual_src_h_user = new_h
        self._relayout()

    def eventFilter(self, obj, ev):
        """v2.15.2：dual 分割线交互的事件过滤器（挂在原文/译文滚动区与分割
        把手的全部控件上，见 __init__ 的 _dual_split_watch）。

        为什么必须用过滤器而不是 overlay 自身的 mouseEvent：
        ① mouse move/hover 在子控件忽略后**不会传播给父控件**（只有 press
           会重新投递）→ 悬停光标反馈在旧实现下永远失效，用户无从发现可拖；
        ② 命中带上半落在 QScrollArea 内，press 会被它直接消费；
        ③ 传播/过滤的事件 position 相对**原接收者**（不重映射），一律取
           globalPosition 比对。"""
        if (getattr(self, "_dual_split_watch", None) and obj in self._dual_split_watch
                and self.is_dual() and not self._collapsed):
            t = ev.type()
            if t in (QEvent.Type.MouseMove, QEvent.Type.HoverMove):
                g = ev.globalPosition().toPoint()
                if self._dual_split_drag:
                    self._dual_split_apply_drag(g)
                    return True
                if hasattr(obj, "setCursor"):
                    if obj is self._dual_sep:
                        obj.setCursor(Qt.SizeVerCursor)
                    else:
                        obj.setCursor(Qt.ArrowCursor)
                # v2.18.1：悬停态真实落地——_DualSepHandle 的"细线→胶囊→提亮"
                # 三态里，set_hover(True) 此前**全库零调用点**（只挂了 Leave→False
                # 与 HoverEnter 无人处理），所以 v2.16.1 宣称的"悬停浮现胶囊"
                # 从未出现过。MouseMove 已经在过滤器里到达，直接按"是否落在
                # 把手上"给真值，不依赖 WA_Hover/HoverEnter。
                self._dual_sep.set_hover(obj is self._dual_sep)
            elif t == QEvent.Type.Leave and obj is self._dual_sep:
                self._dual_sep.set_hover(False)
            elif t == QEvent.Type.MouseButtonPress and ev.button() == Qt.LeftButton:
                # v2.18.0：唯一可拖分割 = sep（原文/译文两栏的高度分配）
                if obj is self._dual_sep:
                    self._dual_split_drag = True
                    self._dual_split_start_y = ev.globalPosition().toPoint()
                    self._dual_split_start_h = self._dual_src_wrap.height()
                    self._dual_sep.set_drag(True)
                    return True           # 拦下：防止 QScrollArea 抢走拖拽
            elif t == QEvent.Type.MouseButtonRelease and self._dual_split_drag:
                self._dual_split_drag = False
                self._dual_split_apply_drag(ev.globalPosition().toPoint())
                # v2.18.1：拖拽态必须收口——此前只清 _dual_split_drag 标志、
                # 没把手本体 set_drag(False)，结果用户拖过一次后中央胶囊
                # **永久高亮**挂在面板上（真机截图里那条"又粗又亮的线"）。
                self._dual_sep.set_drag(False)
                if getattr(self, "_on_dual_split", None):
                    self._on_dual_split("src", self._dual_src_h_user or 0)
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
                # v2.20.1：松手不再磁吸贴边（用户点名删除贴边能力）——
                # 面板停在拖到的位置，只把坐标落盘
                QTimer.singleShot(0, self._save_final_pos)
                QTimer.singleShot(400, self._save_final_pos)
        self._drag_pos = None
        self._mini_press = False

    def wheelEvent(self, event):
        # v2.5.0：工具条上快捷调节（豆包式顺手性）——Ctrl+滚轮=字号 ±1、
        # Ctrl+Shift+滚轮=透明度 ∓5；正文区滚轮保持历史滚动不受影响
        if event.position().y() <= self._bar.geometry().bottom() + 6:
            mods = event.modifiers()
            up = event.angleDelta().y() > 0
            if mods & Qt.ControlModifier and mods & Qt.ShiftModifier:
                cur = alpha_to_opacity(self._bg_alpha)
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
            self.set_bg_opacity(val)

    def set_bg_opacity(self, val):
        """v2.19.2：透明度**唯一写入口**（面板侧快捷档/滚轮与主窗落盘回放共用）。

        旧实现里主窗 `_on_panel_opacity` 直接改私有属性 `overlay._bg_alpha`
        并自带一份 `int(v*2.55)`——与 `apply_style` 的公式各写一处，
        同一档位"当场 254、重启 255"两副面孔。
        """
        self._bg_alpha = opacity_to_alpha(val)
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
        # v2.20.1（用户点名）：双击工具条的"顶/底贴边循环"已删除——贴边能力
        # 整族退役（菜单四向、松手磁吸一并删），面板停在哪就是哪。
        # 只保留双击底缘 = 恢复自动高度（与 ⋯ 菜单项同源，不是贴边功能）。
        pos = event.position()
        pos = pos.toPoint() if hasattr(pos, "toPoint") else pos
        if pos.y() >= self.height() - self.RESIZE_EDGE:
            self._reset_user_height()

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
        # v2.20.0（用户点名）：攒句开关上面板。此前这个能力只藏在
        # 「设置-识别与翻译」的一堆勾选里，想从"整句连贯译文"临时切到
        # "逐片实时译文"必须开设置页→翻页→保存三步，而它和布局/字号一样
        # 属于"看着字幕效果想立刻试一下"的观感开关
        acts["grouping"] = menu.addAction("攒句合并（整句翻译，译文更连贯）")
        acts["grouping"].setCheckable(True)
        acts["grouping"].setChecked(self._grouping)
        menu.addSeparator()
        acts["copy"] = menu.addAction("复制最近一句")
        acts["fix_asr"] = menu.addAction("纠正最近识别…")
        acts["fix_tr"] = menu.addAction("纠正最近译文…")
        acts["export"] = menu.addAction("导出 SRT…")
        acts["export"].setEnabled(getattr(self, "_session_has", True))
        acts["clear"] = menu.addAction("清空面板字幕")   # v2.4.3（A）：与工具条清空同源
        menu.addSeparator()
        acts["pin"] = menu.addAction("置顶显示")
        acts["pin"].setCheckable(True)
        acts["pin"].setChecked(self._pinned)
        # v2.20.1（用户点名）：「贴到屏幕顶部/底部/左侧/右侧」四项已删除
        # （连同双击贴边、松手磁吸一起，贴边能力整族退役）
        # v2.5.0：透明度常用档直调（滚轮 Ctrl+Shift 的菜单版；细调仍走设置页）
        op_menu = menu.addMenu("背景透明度")
        cur_op = alpha_to_opacity(self._bg_alpha)
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
        # v2.19.2：dual 的正文不在 `_rows`（当前句原文/译文），旧判据让这一项
        # 恒灰，而工具条「清空」同一动作可用——两个入口打架。
        # v2.20.1：dual 两栏逐句累积 → 判据是"任一栏有真内容"，不能只看当前句
        # （译文栏空态常驻占位文案，也不能拿它的文本判有无内容）
        has_content = bool(self._rows) or (
            self.is_dual() and (self._dual_col_has_text("src")
                                 or self._dual_col_has_text("tgt")))
        acts["clear"].setEnabled(has_content)
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
        elif chosen == acts.get("grouping"):
            on = not self._grouping
            self.set_grouping_enabled(on)
            if self._on_grouping_toggled:
                try:
                    self._on_grouping_toggled(on)
                except Exception:
                    pass
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
