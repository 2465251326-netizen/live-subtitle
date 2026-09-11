import ctypes
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, QTimer, QStandardPaths
from PySide6.QtGui import QIcon, QAction, QGuiApplication
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton,
    QCheckBox, QFrame, QScrollArea, QProgressBar, QSizePolicy,
    QStatusBar, QMessageBox, QApplication, QStackedWidget,
    QSystemTrayIcon, QMenu, QFileDialog,
    QDialog, QLineEdit, QDialogButtonBox,
)

from app.config import Config
from app.audio.capture import CaptureThread
from app.asr.engine import AsrThread
from app.translate.translator import TranslateThread
from app.ui.styles import DARK_QSS
from app.ui.caption_overlay import CaptionOverlay
from app import hotkey

DOCS_URL = "https://github.com/2465251326-netizen/live-subtitle#readme"

# 各识别模型的近似下载体积（MB），用于把缓存目录增量换算成下载进度
MODEL_SIZES_MB = {"tiny": 75, "base": 145, "small": 480, "medium": 1536,
                  "large-v3-turbo": 1600}

# v2.0.3：停止超时的孤儿线程容器——保住 Python 引用防 GC，
# finished 后 deleteLater 自清理；进程退出前对仍存活的 terminate 兜底
_ORPHANS: list = []


def _orphan_threads() -> list:
    alive = []
    for t in _ORPHANS:
        try:
            if t.isRunning():
                alive.append(t)
        except RuntimeError:
            # v2.0.7：deleteLater 销毁 C++ 对象后，Python 壳上调用 isRunning
            # 会抛 "Internal C++ object already deleted"——线程已终结，跳过。
            # 此前该异常会从 stop_pipeline 一路炸出去，中断后续清理
            pass
    _ORPHANS[:] = alive
    return _ORPHANS


def icon_path():
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "app" / "icon.ico"
    return Path(__file__).resolve().parents[2] / "app" / "icon.ico"


class CaptionCard(QFrame):
    def __init__(self, source_text, parent=None, on_menu=None):
        super().__init__(parent)
        self.setObjectName("CaptionCard")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        self.created_at = datetime.now()
        self.source_text = source_text
        # v2.3.13（P14）：右键纠错菜单回调（MainWindow 注入）——
        # "看到错的→查原文→开设置→找词典→手打"五步链，压缩成"右键→打正解"一步
        self._on_menu = on_menu
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(4)

        self.meta_label = QLabel(datetime.now().strftime("%H:%M:%S"))
        self.meta_label.setObjectName("CaptionMeta")
        self.source_label = QLabel(source_text)
        self.source_label.setObjectName("CaptionSource")
        self.source_label.setWordWrap(True)
        self.source_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.target_label = QLabel("...")
        self.target_label.setObjectName("CaptionTarget")
        self.target_label.setWordWrap(True)
        self.target_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        # v2.4.4（BUG-5）：两个大文本标签占满整卡，右键落在文字上被 QLabel
        # 自带英文菜单（Copy/Select All）拦截，P14 纠错菜单实机永远弹不出来
        # （集成测试直接调内部菜单构建，从未覆盖真实右键路由）。NoContextMenu
        # 把右键交还父级（卡片菜单含"复制原文/译文"，选中 Ctrl+C 仍可用）
        self.source_label.setContextMenuPolicy(Qt.NoContextMenu)
        self.target_label.setContextMenuPolicy(Qt.NoContextMenu)

        layout.addWidget(self.meta_label)
        layout.addWidget(self.source_label)
        layout.addWidget(self.target_label)
        # v2.2.11：SRT 时间轴数据——t_start=会话起算秒，dur_s=语音时长（Whisper 给）
        self.t_start = None
        self.dur_s = None

    def set_result(self, translated, engine, detected, show_source):
        if translated:
            self.target_label.setText(translated)
        else:
            self.target_label.setText("[翻译失败]")
        note = f"{datetime.now().strftime('%H:%M:%S')} · {detected or '?'} · 引擎: {engine}"
        self.meta_label.setText(note)
        self.source_label.setStyleSheet("")   # v2.3.17（P22）撤下占位弱化色
        self.source_label.setVisible(show_source)

    def is_pending(self):
        """是否仍处于"译文未落地"占位态（流式两段式，v2.1.4）。"""
        return self.target_label.text() in ("...", "⟳ …")

    def translated_text(self):
        """当前译文；占位/失败/已并入态返回空串（右键菜单据此决定可用性）。"""
        if self.is_pending():
            return ""
        t = self.target_label.text()
        return "" if (not t or t == "[翻译失败]") else t

    def contextMenuEvent(self, event):
        # v2.3.13（P14）：卡片右键 → 复制原文/译文、一键加入修正词典
        if self._on_menu is None:
            return
        menu = self._on_menu(self)
        if menu is not None:
            menu.exec(event.globalPos())
            menu.deleteLater()

    def set_failed(self, msg):
        self.target_label.setText("[翻译失败]")
        self.meta_label.setText(f"{datetime.now().strftime('%H:%M:%S')} · {msg}")

    def set_merged_away(self):
        """v2.3.6（P9）：低延迟组内前段碎片卡——译文并入末卡整句呈现，
        本卡只留原文（原文本就隐藏时整卡收起，不留孤零时间戳）。"""
        self.target_label.setText("")
        self.target_label.setVisible(False)
        if not self.source_label.isVisible():
            self.setVisible(False)

    def set_active(self, active):
        """聚焦态切换（v2.2.5）：active=True 换强调边框样式，False 渐隐。"""
        self.setObjectName("CaptionCardActive" if active else "CaptionCardOld"
                           if not self.is_pending() else "CaptionCard")
        self.style().unpolish(self)
        self.style().polish(self)


def _srt_ts(sec):
    """秒 → SRT 时间戳 HH:MM:SS,mmm。"""
    ms = int(round(max(0.0, float(sec)) * 1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _pct(values, p):
    """v2.3.20（P26）：近邻法分位数，空列返回 0。仅供延迟日志摘要。"""
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p * (len(s) - 1))))]


def _srt_wrap(text, limit=44):
    """v2.2.12：SRT 长句折两行——优先中间附近的词边界（英文），其次 CJK
    标点之后，都没有就硬切中点。播放器与剪辑软件通用习惯：单行 ≤44 字。"""
    if len(text) <= limit:
        return text
    mid = len(text) // 2
    for off in range(0, 13):
        for i in (mid + off, mid - off):
            if 0 < i < len(text):
                if text[i] == " " or text[i - 1] == " ":
                    return text[:i].rstrip() + "\n" + text[i:].lstrip()
                if text[i - 1] in "，。！？、；：,.!?;:":
                    return text[:i] + "\n" + text[i:].lstrip()
    return text[:mid] + "\n" + text[mid:]


def build_export_text(cards, fmt="txt"):
    """字幕卡列表 → 导出文本（v2.2.11：提取为纯函数 + SRT 时间轴支持）。

    fmt="txt"：维持历史格式 `[meta]/原文/译文`（逐字节不变，不破坏既有习惯）；
    fmt="srt"：标准 SRT 编号+时间轴，cue 文本用译文（缺失时回退原文）；
    时间优先用卡片记录的会话相对秒 t_start + Whisper 时长 dur_s，
    缺失时按累计时长/5 秒槽位近似；占位与翻译失败卡跳过。
    返回 (文本, 有效条数)。"""
    if fmt == "srt":
        cues = []
        for card in cards:
            target = card.target_label.text()
            source = card.source_label.text()
            if target in ("...", "⟳ …", "", "[翻译失败]"):
                target = ""
            text = target or source
            if not text:
                continue
            cues.append([getattr(card, "t_start", None),
                         getattr(card, "dur_s", None), _srt_wrap(text)])
        for i, c in enumerate(cues):
            if c[0] is None:
                prev = cues[i - 1]
                c[0] = (prev[0] + (prev[1] or 4.0)) if i and prev[0] is not None else float(i * 5)
        out = []
        for i, (st, dur, text) in enumerate(cues):
            end = st + (dur or 4.0)
            if i + 1 < len(cues) and cues[i + 1][0] > st:
                end = max(st + 1.0, min(end, cues[i + 1][0] - 0.1))
            out.append(f"{i + 1}\n{_srt_ts(st)} --> {_srt_ts(end)}\n{text}\n")
        return "\n".join(out), len(cues)
    lines = []
    n = 0
    for card in cards:
        meta = card.meta_label.text()
        source = card.source_label.text() if card.source_label.isVisibleTo(card) else ""
        target = card.target_label.text()
        lines.append(f"[{meta}]")
        if source:
            lines.append(source)
        if target and target != "...":
            lines.append(target)
        lines.append("")
        n += 1
    return "\n".join(lines), n


class MainWindow(QMainWindow):
    start_requested = Signal()

    def _asr_timing(self, duration):
        """v2.2.11：当前时刻的 (会话相对秒, 语音时长) 时间轴快照。"""
        t0 = getattr(self, "_session_t0", None)
        try:
            dur = max(0.6, float(str(duration)))
        except (TypeError, ValueError):
            dur = None
        return ((time.time() - t0) if t0 else None), dur

    def __init__(self):
        super().__init__()
        self.config = Config()
        self.capture_thread = None
        self.asr_thread = None
        self.translate_thread = None
        self.running = False
        self.session_count = 0
        self.setWindowTitle("LiveSubtitle · 实时字幕翻译")
        self.resize(1150, 760)
        self.setStyleSheet(DARK_QSS)
        self._build_ui()
        self._load_settings()
        if not self.config.get("wizard_done") and not MainWindow._wizard_scheduled:
            # v2.4.4（BUG-1）：排期标志与"已显示"标志分离——v2.4.1 把置位放在排期时，
            # 而回调守卫查的是同一标志，向导被自己的守卫拦截永不弹出（新用户首启
            # 静默失效，隔离配置实测抓到）。排期即消耗防重复排期；"已显示"只由
            # 回调真正弹窗时置位。
            MainWindow._wizard_scheduled = True
            QTimer.singleShot(400, self._show_first_run_wizard)
        if self.config.get("auto_start"):
            # v2.0.1：改为可撤销的成员定时器——启动后 800ms 内手动开始又停止，
            # 旧 singleShot 到点会把管线再次拉起，与用户操作相反
            self._auto_start_timer = QTimer(self)
            self._auto_start_timer.setSingleShot(True)
            self._auto_start_timer.timeout.connect(self.start_pipeline)
            self._auto_start_timer.start(800)

    _wizard_scheduled = False  # v2.4.4：排期即消耗（进程内只排一次，防多实例重复）
    _wizard_shown = False      # v2.4.4：已真正弹过（回调守卫；原单标志自我拦截见 BUG-1）
    _wizard_defers = 0

    def _show_first_run_wizard(self):
        # 集成测试曾在此递归栈溢出：每个 MainWindow 各挂一个 singleShot，
        # 向导 exec 嵌套泵事件时其余待触发定时器再开新向导，无限套娃。
        if MainWindow._wizard_shown or getattr(self, "_quitting", False):
            return
        if not self.isVisible():
            # v2.4.4：排期实例到点不可见（最小化/多实例隐藏者先到期）时有限次
            # 顺延等主窗真正显示——v2.4.1 在此直接放弃导致向导永不弹；顺延
            # 封顶 5 次防无限定时器（原始挂死风险仍被"进程内只排一个链"挡住）
            if MainWindow._wizard_defers < 5:
                MainWindow._wizard_defers += 1
                QTimer.singleShot(400, self._show_first_run_wizard)
            return
        MainWindow._wizard_shown = True
        from app.ui.first_run import FirstRunWizard
        dlg = FirstRunWizard(self)
        dlg.exec()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(18, 14, 18, 8)
        root.setSpacing(12)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("LiveSubtitle")
        title.setObjectName("HeaderTitle")
        subtitle = QLabel("实时语音识别 · 自动语言检测 · 多语翻译（在线 / 离线）")
        subtitle.setObjectName("HeaderSub")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()

        self.status_dot = QLabel()
        self.status_dot.setObjectName("StatusDot")
        self.status_dot.setFixedSize(14, 14)
        self.status_dot.setAlignment(Qt.AlignCenter)
        self.status_text = QLabel("未启动")
        self.status_text.setObjectName("HeaderSub")
        header.addWidget(self.status_dot)
        header.addWidget(self.status_text)

        self.help_button = QPushButton("使用说明")
        self.help_button.setObjectName("GhostButton")
        self.help_button.setCursor(Qt.PointingHandCursor)
        self.help_button.setToolTip("打开浏览器查看详细使用说明")
        self.help_button.clicked.connect(self._open_docs)
        header.addWidget(self.help_button)

        self.settings_button = QPushButton("设置")
        self.settings_button.setObjectName("GhostButton")
        self.settings_button.setCursor(Qt.PointingHandCursor)
        self.settings_button.setFixedWidth(64)
        self.settings_button.setToolTip("打开设置窗口")
        self.settings_button.clicked.connect(self._open_settings)
        header.addWidget(self.settings_button)

        self.toggle_button = QPushButton("开始翻译")
        self.toggle_button.setObjectName("PrimaryButton")
        self.toggle_button.setCursor(Qt.PointingHandCursor)
        self.toggle_button.clicked.connect(self.toggle_running)
        header.addWidget(self.toggle_button)
        root.addLayout(header)

        body = QHBoxLayout()
        body.setSpacing(14)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll_container = QWidget()
        self.scroll_layout = QVBoxLayout(self.scroll_container)
        self.scroll_layout.setContentsMargins(4, 4, 10, 4)
        self.scroll_layout.setSpacing(10)
        self.scroll_layout.addStretch()
        self.scroll.setWidget(self.scroll_container)

        self.empty_hint = QLabel(
            "点击右上角「开始翻译」\n\n播放任意视频或说话，字幕将实时出现在这里\n\n"
            "系统声音模式可直接抓取网页视频 / 播放器 / 会议的声音"
        )
        self.empty_hint.setObjectName("EmptyHint")
        self.empty_hint.setAlignment(Qt.AlignCenter)
        self.empty_hint.setWordWrap(True)
        # v2.2.5：配置速览卡——当前模型/引擎/设备/热键一目了然（空窗口变仪表盘）
        from PySide6.QtWidgets import QFrame as _QFrame, QGridLayout as _QGrid
        self._quick = _QFrame()
        self._quick.setObjectName("SidePanel")
        self._quick.setMaximumWidth(520)
        self._quick.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        qv = QVBoxLayout(self._quick)
        qv.setContentsMargins(18, 14, 18, 14)
        qv.setSpacing(8)
        qtitle = QLabel("当前配置")
        qtitle.setObjectName("PanelTitle")
        qv.addWidget(qtitle)
        qgrid = _QGrid()
        qgrid.setHorizontalSpacing(14)
        qgrid.setVerticalSpacing(8)
        self._quick_labels = {}
        for i, key in enumerate(("识别模型", "翻译引擎", "音频来源")):
            k = QLabel(key)
            k.setObjectName("PanelTitle")
            k.setMinimumHeight(18)  # v2.2.9：行高按字体下限给足，不裁字
            v = QLabel("—")
            v.setObjectName("EmptyHint")
            # v2.2.13：值标签不换行——热键已拆两行，无超长值；卡片按最长
            # 单行自适应（≤520 上限），从根上消除"word-wrap 高度不上传父
            # 布局导致裁字/重叠"这一类问题（v2.2.9 的 adjustSize 从未真正生效）
            v.setMinimumHeight(20)
            self._quick_labels[key] = v
            qgrid.addWidget(k, i, 0, Qt.AlignTop)
            qgrid.addWidget(v, i, 1, Qt.AlignTop)
        # v2.3.2（G1）：悬浮字幕条状态行——用户关了悬浮条后软件从不提醒，
        # "关了都忘了"是模拟用户报告的真实痛点；空页面仪表盘常驻显示状态与开启方法
        ov_key = QLabel("字幕面板")
        ov_key.setObjectName("PanelTitle")
        ov_key.setMinimumHeight(18)
        ov_val = QLabel("—")
        ov_val.setObjectName("EmptyHint")
        ov_val.setMinimumHeight(20)
        self._quick_labels["字幕面板"] = ov_val
        qgrid.addWidget(ov_key, 3, 0, Qt.AlignTop)
        qgrid.addWidget(ov_val, 3, 1, Qt.AlignTop)
        # v2.2.13（用户实拍"别扭"修正）：热键拆两行显示——单行拼接必换行，
        # 而 word-wrap 标签的换行高度不通知父布局，卡片高度冻结导致第二行
        # 被提示行压住（v2.2.9 的 adjustSize 修复实际从未生效）。两行短文本
        # 永不换行，从结构上消除该问题；"热键"键名跨两行居左对齐。
        hk_key = QLabel("热键")
        hk_key.setObjectName("PanelTitle")
        hk_key.setMinimumHeight(18)
        qgrid.addWidget(hk_key, 4, 0, 2, 1, Qt.AlignTop | Qt.AlignLeft)
        for r, name in ((4, "热键"), (5, "热键o")):
            v = QLabel("—")
            v.setObjectName("EmptyHint")
            v.setMinimumHeight(20)
            self._quick_labels[name] = v
            qgrid.addWidget(v, r, 1, Qt.AlignTop)
        qv.addLayout(qgrid)
        qtip = QLabel("提示：托盘图标右键可显隐字幕面板、快速切换输入来源；"
                     "热键可在「设置-通用」修改")
        qtip.setObjectName("SettingDesc")
        qtip.setAlignment(Qt.AlignCenter)
        qtip.setWordWrap(True)  # v2.2.9：提示自动换行，不截断
        qv.addWidget(qtip)
        empty_page = QWidget()
        empty_layout = QVBoxLayout(empty_page)
        empty_layout.addStretch()
        empty_layout.addWidget(self.empty_hint, 0, Qt.AlignHCenter)
        empty_layout.addSpacing(18)
        empty_layout.addWidget(self._quick, 0, Qt.AlignHCenter)
        empty_layout.addStretch()
        list_page = QWidget()
        list_layout = QVBoxLayout(list_page)
        list_layout.setContentsMargins(0, 0, 0, 0)
        list_layout.addWidget(self.scroll)

        self.stack = QStackedWidget()
        self.stack.addWidget(empty_page)
        self.stack.addWidget(list_page)
        self.stack.setCurrentIndex(0)
        body.addWidget(self.stack, 1)
        root.addLayout(body)

        status = QStatusBar()
        self.setStatusBar(status)
        self.engine_status_label = QLabel("引擎：待启动")
        status.addWidget(self.engine_status_label, 1)
        # v2.2.5：关键提示横幅（积压/连续失败等排障信息）——独立彩色标签，
        # 不再挤进常规状态行被截断；无提示时隐藏不占位
        self.alert_banner = QLabel("")
        self.alert_banner.setObjectName("StatusAlert")
        self.alert_banner.setWordWrap(True)
        self.alert_banner.setVisible(False)
        status.addPermanentWidget(self.alert_banner, 2)

        self.level_bar = QProgressBar()
        self.level_bar.setRange(0, 100)
        self.level_bar.setTextVisible(False)
        self.level_bar.setFixedSize(140, 10)
        self.level_bar.setStyleSheet(
            "QProgressBar { background: #262c38; border: none; border-radius: 5px; }"
            "QProgressBar::chunk { background: #34d399; border-radius: 5px; }")
        status.addPermanentWidget(self.level_bar)

        self.clear_button = QPushButton("清空")
        self.clear_button.setObjectName("GhostButton")
        self.clear_button.setCursor(Qt.PointingHandCursor)
        self.clear_button.setToolTip("清空当前会话的字幕记录")
        self.clear_button.clicked.connect(self._clear_captions)
        status.addPermanentWidget(self.clear_button)

        self.export_button = QPushButton("导出")
        self.export_button.setObjectName("GhostButton")
        self.export_button.setCursor(Qt.PointingHandCursor)
        self.export_button.setToolTip("把当前会话的双语字幕导出为文本文件")
        self.export_button.clicked.connect(self._export_captions)
        status.addPermanentWidget(self.export_button)

        self.session_label = QLabel("本次会话：0 条")
        status.addPermanentWidget(self.session_label)

        self.overlay = CaptionOverlay(on_closed=self.on_overlay_closed,
                                      on_moved=self._on_overlay_moved,
                                      on_open_settings=self._open_overlay_settings,
                                      on_toggle_source=self._toggle_source,
                                      on_toggle_translation_only=self._on_toggle_translation_only,
                                      on_resized=self._on_overlay_resized,
                                       on_correct=self._overlay_correct,
                                       on_export_srt=self._export_srt,
                                       on_language=self._on_panel_language,
                                       on_font_size=self._on_panel_font_size,
                                       on_pin_changed=self._on_panel_pin,
                                       on_collapsed=self._on_panel_collapsed,
                                       on_first_show=self._on_panel_first_show,
                                       on_opacity=self._on_panel_opacity)
        self.overlay.hide()
        self._build_tray()
        self._install_global_hotkey()
        self._maybe_prewarm()
        # v2.5.1（P1）：启动按配置恢复字幕面板显隐——此前 overlay_enabled=true
        # 也要等"开始翻译"才显示，用户"明明开着面板"重启后却没了（设置不兑现）
        if bool(self.config.get("overlay_enabled")) and not self.overlay.isVisible():
            self.set_overlay_visible(True)

    def _maybe_prewarm(self):
        """v2.3.5（P5-A）：启动即后台预热模型——把 GPU 近 1 分钟的冷初始化
        从"点开始翻译之后"挪到启动空闲期；只热已下载模型，绝不触发下载。"""
        if not bool(self.config.get("prewarm_model")):
            return
        # v2.3.6：offscreen（无头测试/CI）不预热——桌面 UX 功能在无真实
        # GPU/音频的headless环境既测不到、也无意义；且每建一个 MainWindow
        # 就起一条 QThread 的线程churn 会诱发集成测试套件的 Qt 随机挂死
        if os.environ.get("QT_QPA_PLATFORM", "").lower() == "offscreen":
            return
        try:
            from app.asr.engine import PrewarmWorker
            self._prewarm = PrewarmWorker(str(self.config.get("asr_model")),
                                          str(self.config.get("asr_device")), self)
            self._prewarm.start()
        except Exception:
            pass

    def _clamp_overlay_pos(self, x, y):
        """把悬浮字幕位置限制在其所在屏幕的可用区域内。

        v2.0.1：此前用主窗口所在屏，多显示器下把字幕条拖到副屏松手即被
        钳回主屏；现按 overlay 中心点定位屏幕，取不到再回退主窗口所在屏。"""
        from PySide6.QtGui import QGuiApplication
        screen = self.overlay.screen() or QGuiApplication.screenAt(
            self.overlay.geometry().center()) or self.screen()
        geo = screen.availableGeometry()
        w = max(80, self.overlay.width())
        h = max(40, self.overlay.height())
        x = max(geo.left(), min(int(x), geo.right() - w))
        y = max(geo.top(), min(int(y), geo.bottom() - h))
        return x, y

    def _on_overlay_moved(self, x, y):
        x, y = self._clamp_overlay_pos(x, y)
        self.overlay.move(x, y)
        self.config.set("overlay_x", x)
        self.config.set("overlay_y", y)

    def _on_overlay_resized(self, w):
        """面板宽度持久化（v2.4.0：高度永远贴内容，不再有手动高度）。"""
        self.config.set("overlay_w", int(w) if w and w > 0 else 0)

    def _on_panel_language(self, code):
        """工具条 🌐 下拉：写 target_lang；运行中提示重启生效（与设置页同语义）。"""
        self.config.set("target_lang", code)
        dlg = getattr(self, "_settings_dlg", None)
        if dlg is not None:
            try:
                dlg.reload_values()
            except Exception:
                pass
        if self.running:
            self._set_alert(f"目标语言已切换为 {code}，重启翻译后对新字幕生效")

    def _on_panel_font_size(self, px):
        self.config.set("overlay_font_size", int(px))
        self.apply_overlay_from_config()

    def _on_panel_opacity(self, val):
        """v2.5.0：面板滚轮/菜单档调透明度——落盘并本地重放（细调仍走设置页）。"""
        self.config.set("overlay_bg_opacity", int(val))
        self.overlay._bg_alpha = int(max(30, min(100, int(val))) * 2.55)
        self.overlay.update()

    def _on_panel_pin(self, on):
        self.config.set("overlay_pin", bool(on))

    def _on_panel_collapsed(self, on):
        self.config.set("overlay_collapsed", bool(on))

    def _on_panel_first_show(self):
        """v2.4.3（D）：面板本进程首次显示时，若这份配置还没看过手势引导，
        把空状态文案升级为操作小抄并落盘 overlay_hint_shown（只弹一次）。"""
        if not bool(self.config.get("overlay_hint_shown")):
            self.overlay.show_first_hint()
            self.config.set("overlay_hint_shown", True)

    def _on_toggle_translation_only(self, translation_only):
        """工具条/菜单"只显示译文"：写 show_source 并同步设置页复选框。"""
        show_source = not bool(translation_only)
        self.config.set("show_source", show_source)
        self.overlay.set_show_source(show_source)
        dlg = getattr(self, "_settings_dlg", None)
        if dlg is not None and hasattr(dlg, "show_source_check"):
            dlg.show_source_check.setChecked(show_source)

    # ---------- 全局热键 ----------

    def _install_global_hotkey(self):
        """安装原生事件过滤器并按当前配置注册热键（进程生命周期内一次过滤器）。"""
        hotkey.install(QApplication.instance(), self.toggle_running)
        hotkey.install_overlay(QApplication.instance(), self._toggle_overlay_hotkey)
        status = self.apply_hotkey_config()
        # v2.0.0：启动时注册失败不再静默（组合被占用/不支持时用户毫无感知）
        if status.startswith("✗"):
            self.tray.showMessage("LiveSubtitle 全局热键", status[2:], QSystemTrayIcon.Warning, 4000)

    def _toggle_overlay_hotkey(self):
        """显隐悬浮条热键回调（v2.2.6）：可见则隐藏（同步配置），不可见则强制
        显示并同步配置（热键即开关，不受 overlay_enabled 当前值约束）。
        v2.2.7：250ms 业务级防抖——与 toggle_running 同款，双保险。"""
        import time as _t
        now = _t.monotonic()
        if now - getattr(self, "_overlay_hk_last", 0.0) < 0.25:
            return
        self._overlay_hk_last = now
        if self.overlay.isVisible():
            self.overlay.hide()  # 与 X 按钮路径一致：先隐藏再同步配置
            self.on_overlay_closed()
        else:
            self.overlay.show()
            self.config.set("overlay_enabled", True)
            dlg = getattr(self, "_settings_dlg", None)
            if dlg is not None and hasattr(dlg, "sync_overlay_check"):
                dlg.sync_overlay_check(True)
            self.update_overlay_status()
            self._refresh_quick_panel()  # v2.3.2（G1）：仪表盘同步悬浮条状态

    def apply_hotkey_config(self):
        """按配置注册/注销全局热键；返回给设置页展示的状态文本。"""
        c = self.config
        hotkey.unregister()
        if not c.get("hotkey_enabled"):
            self._update_tray_hotkey_text("")
            return "全局热键已关闭"
        seq = str(c.get("hotkey_sequence") or "Ctrl+Alt+S")
        ok = hotkey.register(int(self.winId()), seq)
        # v2.2.6：显隐悬浮条热键（默认 Ctrl+Alt+O，可留空禁用）
        # v2.2.7：注册失败不再静默——组合被占用时用户按键"毫无反应"即 BUG 观感，
        # 必须托盘气泡 + 设置页状态行双重提示
        oseq = str(c.get("hotkey_overlay") or "").strip()
        if oseq:
            if oseq.upper() == seq.upper():
                self._set_alert("⚠ 悬浮条显隐热键与开始/停止热键相同，已忽略——请在「设置-通用」改键")
            elif not hotkey.register_overlay(int(self.winId()), oseq):
                self.tray.showMessage(
                    "LiveSubtitle 全局热键",
                    f"悬浮条显隐热键 {oseq} 注册失败（已被其他程序占用或不被支持），"
                    "该热键未生效——请在「设置-通用」换一个组合。",
                    QSystemTrayIcon.Warning, 5000)
        self._update_tray_hotkey_text(seq if ok else "")
        base = (f"✓ 全局热键 {seq} 已生效（托盘菜单同步显示）" if ok
                else f"✗ 热键 {seq} 注册失败：组合不被支持或已被其他程序占用，请在「设置-通用」换一个组合")
        # v2.2.7：悬浮条显隐热键注册结果同样透传到设置页状态行
        if oseq and oseq.upper() != seq.upper() and not hotkey.overlay_text():
            base += (f"\n✗ 悬浮条显隐热键 {oseq} 注册失败：已被其他程序占用"
                     "或组合不受支持，请换一个组合或清空禁用")
        # v2.2.11：注册状态变化后同步速览卡（否则启动早期刷新会停留在旧值）
        self._refresh_quick_panel()
        return base

    def _update_tray_hotkey_text(self, seq):
        act = getattr(self, "_tray_toggle_action", None)
        if act is not None:
            act.setText(f"开始 / 停止翻译（{seq}）" if seq else "开始 / 停止翻译")

    def _open_overlay_settings(self):
        """悬浮条右键「打开设置」：打开设置窗口并定位到「显示」页。"""
        self._open_settings()
        dlg = getattr(self, "_settings_dlg", None)
        if dlg is not None:
            dlg.focus_page(3)

    def _toggle_source(self):
        """悬浮条右键「切换输入来源」：系统声音 ↔ 麦克风，运行中自动重启采集。"""
        cur = self.config.get("source_type")
        new = "microphone" if cur == "system" else "system"
        self.config.set("source_type", new)
        dlg = getattr(self, "_settings_dlg", None)
        if dlg is not None:
            dlg.sync_source_type(new)
        if self.running:
            self.stop_pipeline()
            # v2.0.3：等旧线程真正退出再重启——此前立即 start 会造成新旧
            # CaptureThread 并存抢音频设备、新旧 AsrThread 并发加载双份模型
            for t in (self.capture_thread, self.asr_thread, self.translate_thread):
                if t and t.isRunning():
                    t.wait(5000)
            self.start_pipeline()
        name = "麦克风" if new == "microphone" else "系统声音"
        self._set_engine_status(f"已切换输入来源：{name}")

    def update_overlay_status(self):
        """把运行状态/来源/引擎/模型同步到悬浮条状态行。"""
        if not hasattr(self, "overlay"):
            return
        if getattr(self, "_muted_warn", False):
            self.overlay.set_status("系统静音中 · 不会有字幕", is_error=True)
            return
        if getattr(self, "_low_input_warn", False):
            self.overlay.set_status("信号弱", is_error=True)
            return
        if not self.running:
            self.overlay.set_status("已停止 · 待机中")
            return
        src = "麦克风" if self.config.get("source_type") == "microphone" else "系统声音"
        model = self.config.get("asr_model")
        eng = getattr(self, "_last_engine_name", "") or "自动"
        self.overlay.set_status(f"运行中 · {src} · {eng} · {model} 模型")

    def set_overlay_caption_error(self, failed):
        """字幕翻译失败时让状态行变橙红提醒。"""
        if hasattr(self, "overlay"):
            self.overlay.set_status("翻译失败 · 检查网络或切换引擎", is_error=failed)
            if not failed:
                self.update_overlay_status()

    def _build_tray(self):
        self.tray = QSystemTrayIcon(QIcon(str(icon_path())), self)
        self.tray.setToolTip("LiveSubtitle · 实时字幕翻译")
        menu = QMenu(self)
        act_show = QAction("显示主窗口", self)
        act_show.triggered.connect(self._restore_window)
        act_toggle = QAction("开始 / 停止翻译", self)
        act_toggle.triggered.connect(self.toggle_running)
        # v2.2.8：速览卡承诺过的托盘快捷操作补齐（此前文案撒谎）
        act_overlay = QAction("显隐字幕面板", self)
        act_overlay.triggered.connect(self._toggle_overlay_hotkey)
        self._tray_overlay_action = act_overlay
        act_source = QAction("切换输入来源", self)
        act_source.triggered.connect(self._toggle_source)
        act_settings = QAction("打开设置…", self)
        act_settings.triggered.connect(self._open_settings)
        act_quit = QAction("退出", self)
        act_quit.triggered.connect(self._quit_app)
        menu.addAction(act_show)
        menu.addAction(act_toggle)
        menu.addAction(act_overlay)
        self._tray_toggle_action = act_toggle
        menu.addSeparator()
        menu.addAction(act_source)
        menu.addAction(act_settings)
        menu.addSeparator()
        menu.addAction(act_quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda r: self._restore_window()
            if r == QSystemTrayIcon.Trigger or r == QSystemTrayIcon.DoubleClick else None)
        self.tray.show()

    def _load_settings(self):
        c = self.config
        x, y = self._clamp_overlay_pos(c.get("overlay_x"), c.get("overlay_y"))
        self.overlay.move(x, y)
        # v2.4.0：只恢复宽度（0=自动）；高度永远贴内容，旧 overlay_h 退役
        try:
            ow = int(c.get("overlay_w") or 0)
        except (TypeError, ValueError):
            ow = 0
        if ow >= CaptionOverlay.MIN_W:
            self.overlay.resize(ow, self.overlay.height())
            self.overlay._user_resized = True
        self.apply_overlay_from_config()
        self._refresh_quick_panel()
        if c.get("overlay_enabled"):
            self.overlay.show()

    def _save_settings(self):
        c = self.config
        c.set("overlay_x", self.overlay.x())
        c.set("overlay_y", self.overlay.y())

    def _open_settings(self):
        from app.ui.settings_dialog import SettingsDialog
        dlg = getattr(self, "_settings_dlg", None)
        if dlg is None:
            dlg = SettingsDialog(self)
            self._settings_dlg = dlg
            # v2.2.8：连接保存信号——保存后速览卡/托盘文案实时刷新
            # （此前信号从未被连接，改了模型/引擎/热键速览卡一直显示旧值）
            dlg.settings_saved.connect(self._refresh_quick_panel)
        dlg.load_from_config()
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _refresh_quick_panel(self):
        """空状态页配置速览卡（v2.2.5）：模型/引擎/来源/热键实时汇总。"""
        labels = getattr(self, "_quick_labels", None)
        if not labels:
            return
        c = self.config
        model = c.get("asr_model")
        engine_names = {"google": "Google（在线）", "mymemory": "MyMemory（在线）",
                        "argos": "离线翻译包（直译）", "auto": "自动（在线优先，失败切离线）"}
        eng = engine_names.get(c.get("engine"), str(c.get("engine")))
        src = "系统声音" if c.get("source_type") == "system" else "麦克风"
        hk_cfg = str(c.get("hotkey_sequence") or "Ctrl+Alt+S")
        # v2.2.6：显隐悬浮条热键同步显示
        oseq_cfg = str(c.get("hotkey_overlay") or "").strip()
        labels["识别模型"].setText(f"{model}（{'GPU' if self._quick_gpu_hint() else 'CPU'}）")
        labels["翻译引擎"].setText(eng)
        labels["音频来源"].setText(src)
        # v2.3.2（G1）：悬浮条状态常驻仪表盘——关闭时明说怎么再打开
        # （文案刻意短：值列不换行，长句会撑爆卡片 520px 上限）
        if self.overlay.isVisible():
            labels["字幕面板"].setText("已开启（可拖动位置）")
            labels["字幕面板"].setStyleSheet("")
        else:
            labels["字幕面板"].setText("已关闭 · 按 Ctrl+Alt+O 打开")
            labels["字幕面板"].setStyleSheet("color: #fbbf24;")
        # v2.2.11：热键行以“实际注册成功”为准显示——配置了但被占用未注册时
        # 标红“（未生效）”，不再拿配置值谎称可用（文案不许承诺做不到的事）
        hk_live = hotkey.current_text()
        o_live = hotkey.overlay_text()
        failed = False
        hk_failed = o_failed = False
        if not c.get("hotkey_enabled"):
            hk_disp, o_disp = "全局热键已关闭（设置-通用）", ""
        else:
            hk_failed = not hk_live
            o_failed = bool(oseq_cfg) and not o_live
            failed = hk_failed or o_failed
            hk_disp = (f"{hk_cfg} 开始/停止（未生效）" if hk_failed
                       else f"{hk_live} 开始/停止")
            if not oseq_cfg:
                o_disp = "未设 显隐悬浮条"
            elif o_failed:
                o_disp = f"{oseq_cfg} 显隐悬浮条（未生效）"
            else:
                o_disp = f"{o_live} 显隐悬浮条"
        # v2.2.13：两行分别落位；哪行未生效哪行标红（不再拼接换行）
        labels["热键"].setText(hk_disp)
        labels["热键o"].setText(o_disp)
        labels["热键"].setStyleSheet("color: #ff8a5c;"
                                    if (failed and hk_failed) else "")
        labels["热键o"].setStyleSheet("color: #ff8a5c;"
                                      if (failed and o_failed) else "")

    def _quick_gpu_hint(self):
        """算力档判定：优先读 AsrThread 实际加载设备——v2.4.4（BUG-2）修复
        GPU 静默回落 CPU 时速览卡仍按配置谎报"（GPU）"；线程未跑时回退配置值
        （v2.2.10 修正：此前误读不存在的 compute_type 键，恒为 None）。"""
        t = getattr(self, "asr_thread", None)
        used = getattr(t, "_device_used", None) if t is not None else None
        if used in ("cpu", "cuda"):
            return used == "cuda"
        return str(self.config.get("asr_device") or "auto") == "cuda"

    def _open_docs(self):
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl
        QDesktopServices.openUrl(QUrl(DOCS_URL))

    def _restore_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit_app(self):
        self._quitting = True
        self.close()

    def _show_info(self, title, text):
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setText(text)
        box.setModal(False)
        box.setAttribute(Qt.WA_DeleteOnClose, True)
        box.show()

    def set_overlay_visible(self, checked):
        """仅切换悬浮条显隐（预览用，不落盘）——落盘统一走 set_overlay_enabled。"""
        if checked:
            x, y = self._clamp_overlay_pos(self.config.get("overlay_x"),
                                           self.config.get("overlay_y"))
            self.overlay.move(x, y)
            self.overlay.show()
        else:
            self.overlay.hide()

    def set_overlay_enabled(self, checked):
        self.set_overlay_visible(checked)
        self.config.set("overlay_enabled", bool(checked))
        self._refresh_quick_panel()  # v2.3.2（G1）

    def apply_overlay_from_config(self):
        c = self.config
        # v2.4.0 面板形态：三形态/穿透/描边全部退役，只剩内容相关的外观项
        self.overlay.apply_style(
            font_size=int(c.get("overlay_font_size")),
            text_color=c.get("overlay_text_color"),
            bg_color=c.get("overlay_bg_color"),
            bg_opacity=int(c.get("overlay_bg_opacity")),
        )
        self.overlay.set_show_source(bool(c.get("show_source")))
        self.overlay.set_target_lang(str(c.get("target_lang") or "zh-CN"))
        # 缺键由 Config.load 按 DEFAULTS 合并补齐，这里不再传默认值
        self.overlay.set_pinned(bool(c.get("overlay_pin")))
        self.overlay.set_collapsed(bool(c.get("overlay_collapsed")))

    # ---------- v2.2.12：就绪未出字时的"正在聆听"呼吸反馈 ----------

    def _set_listen_pulse(self, on):
        t = getattr(self, "_pulse_timer", None)
        if on:
            if t is None:
                t = QTimer(self)
                t.setInterval(550)
                t.timeout.connect(self._tick_listen_pulse)
                self._pulse_timer = t
                self._pulse_on = False
            if not t.isActive():
                t.start()
        elif t is not None and t.isActive():
            t.stop()
            self.status_dot.setStyleSheet("background-color: #2ecc71; border-radius: 7px;")

    def _tick_listen_pulse(self):
        self._pulse_on = not getattr(self, "_pulse_on", False)
        col = "#2ecc71" if self._pulse_on else "#14532d"
        self.status_dot.setStyleSheet(f"background-color: {col}; border-radius: 7px;")

    def on_overlay_closed(self):
        if getattr(self, "_quitting", False):
            return
        self._save_settings()
        self.config.set("overlay_enabled", False)
        dlg = getattr(self, "_settings_dlg", None)
        if dlg is not None:
            dlg.sync_overlay_check(False)
        self._refresh_quick_panel()  # v2.3.2（G1）

    def _clear_captions(self):
        while self.scroll_layout.count() > 1:
            item = self.scroll_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self.session_count = 0
        self.session_label.setText("本次会话：0 条")
        self.stack.setCurrentIndex(0)

    def _has_cards(self):
        """v2.4.4（BUG-9）：列表页是否存在字幕卡（layout 里有 stretch 等非卡项，
        不能拿 count() 直接判）。"""
        for i in range(self.scroll_layout.count()):
            if isinstance(self.scroll_layout.itemAt(i).widget(), CaptionCard):
                return True
        return False

    def _export_captions(self):
        self._do_export("txt")

    def _export_srt(self):
        """面板菜单「导出 SRT…」入口（v2.4.4 BUG-6）：此前与通用导出共用，
        默认文件名与过滤器都是 txt——照菜单文案直接点保存得到 txt，承诺落空。"""
        self._do_export("srt")

    def _do_export(self, fmt):
        cards = []
        for i in range(self.scroll_layout.count()):
            w = self.scroll_layout.itemAt(i).widget()
            if isinstance(w, CaptionCard):
                cards.append(w)
        if not cards:
            QMessageBox.information(self, "导出字幕", "当前会话还没有可导出的字幕。")
            return
        ext = "srt" if fmt == "srt" else "txt"
        default_name = f"LiveSubtitle_{datetime.now():%Y%m%d_%H%M%S}.{ext}"
        # 默认落到用户文档目录：安装目录（Program Files）对标准权限用户不可写
        docs = QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation) or str(Path.home())
        # v2.4.4（BUG-6）：默认过滤器与菜单入口一致——SRT 入口首选 SRT
        filt = ("SRT 字幕 (*.srt);;文本文件 (*.txt);;所有文件 (*)" if fmt == "srt"
                else "文本文件 (*.txt);;SRT 字幕 (*.srt);;所有文件 (*)")
        path, _ = QFileDialog.getSaveFileName(
            self, "导出字幕", str(Path(docs) / default_name), filt)
        if not path:
            return
        # v2.2.11：按扩展名选格式——.srt 生成带时间轴的标准字幕（播放器/剪映
        # 可直接加载）；其余维持历史纯文本格式
        fmt = "srt" if str(path).lower().endswith(".srt") else "txt"
        content, count = build_export_text(cards, fmt)
        if fmt == "srt" and count == 0:
            QMessageBox.information(self, "导出字幕", "没有已完成的字幕可导出为 SRT。")
            return
        try:
            Path(path).write_text(content, encoding="utf-8")
        except Exception as e:
            QMessageBox.warning(self, "导出字幕", f"写入文件失败：{e}")
            return
        # v2.4.4（BUG-8）：完成提示路径规范化为 Windows 反斜杠——QFileDialog
        # 返回正斜杠路径，用户复制到资源管理器打不开
        QMessageBox.information(
            self, "导出字幕", f"已导出 {count} 条字幕到：\n{os.path.normpath(path)}")

    def toggle_running(self):
        # v2.0.4：热键连按防抖——界面被 stop_pipeline 短暂阻塞时按下的热键
        # 会在事件队列里排队，恢复后被逐条当作 toggle 处理，造成 start/stop
        # 毫秒级反复翻转（实机日志实证：pipeline.start 后 4~6ms 即 pipeline.stop），
        # 最终停在"运行"档、加载线程成孤儿，状态栏被迟到的加载消息永久覆盖
        now = time.monotonic()
        if now - getattr(self, "_last_toggle_at", 0.0) < 0.25:
            return
        self._last_toggle_at = now
        was_running = self.running
        if was_running:
            self.stop_pipeline()
        else:
            self.start_pipeline()
        # v2.0.4：启停同步执行往往伴随 GUI 线程短暂阻塞（模型加载期冻结），
        # 阻塞期间按下的热键此刻恰好排队待发——完成后再盖一次时间戳，
        # 让这批"冻结期按键"在恢复后的 0.25s 内被吞掉，防止管线被
        # 排队事件反向拉起（一次按键一个动作）
        self._last_toggle_at = time.monotonic()
        # 热键/托盘启动时主窗口往往隐藏在托盘、悬浮条也可能关闭，
        # 状态变化必须用系统气泡显式告知，否则用户感知为"无响应"（v1.9.4）
        if not self.isVisible():
            tray = getattr(self, "tray", None)
            if tray is not None:
                tray.showMessage(
                    "LiveSubtitle",
                    "已停止翻译" if was_running else "已开始翻译（首次使用会先下载模型）",
                    QSystemTrayIcon.Information, 2000)

    def start_pipeline(self):
        if self.running:
            return
        timer = getattr(self, "_auto_start_timer", None)
        if timer is not None:
            timer.stop()  # 用户已手动介入，撤销 auto_start（v2.0.1）
        from app import log as app_log
        app_log.log("pipeline.start", source=self.config.get("source_type"),
                    model=self.config.get("asr_model"), engine=self.config.get("engine"),
                    target=self.config.get("target_lang"))
        self.running = True
        # v2.3.20（P26）：新会话清零延迟样本与提交时刻表（防跨会话混算）
        self._lat_reco, self._lat_tr, self._submit_ts = [], [], {}
        self.toggle_button.setText("停止翻译")
        self.toggle_button.setObjectName("StopButton")
        self.toggle_button.style().unpolish(self.toggle_button)
        self.toggle_button.style().polish(self.toggle_button)
        self.status_dot.setStyleSheet("background-color: #2ecc71; border-radius: 7px;")
        self.status_text.setText("运行中")
        self.stack.setCurrentIndex(1)
        self.session_count = 0

        c = self.config
        engine = c.get("engine")
        self.translate_thread = TranslateThread(engine, c.get("target_lang"), self,
                                                translate_fix_map=dict(c.get("translate_fix_map") or {}))
        self.translate_thread.result_ready.connect(self._on_translated)
        # v2.0.4：状态改走带守卫的槽——lambda 无 running 守卫，停止后已入队的
        # 迟到状态（如孤儿加载线程的"正在加载模型"）会覆盖"已停止"
        self.translate_thread.status_changed.connect(self._on_translate_status)
        # v2.3.2（G2）：在线引擎启动即不可达的事前横幅
        self.translate_thread.engine_fallback.connect(self._on_engine_fallback)
        self.translate_thread.start()

        self._asr_ready = False  # v2.0.4：模型加载期停止时缩短等待（见 stop_pipeline）
        self._session_t0 = time.time()  # v2.2.11：SRT 时间轴零点（本次会话起算）
        # v2.3.1：重模型+CPU 组合预警（常驻横幅，见 _set_engine_status）
        self._heavy_cpu_warn = (str(c.get("asr_model")) in ("medium", "large-v3-turbo")
                                and str(c.get("asr_device")) in ("cpu", "auto"))
        # v2.4.4（BUG-3）：每会话重置"已证明可识别"标记——出过字幕的会话内
        # 低电平不再挂过弱告警
        self._caption_seen = False
        self.asr_thread = AsrThread(
            c.get("asr_model"),
            c.get("asr_device"),
            c.get("asr_language"),
            self,
            hallucination_filter=bool(c.get("hallucination_filter")),
            silero_vad=bool(c.get("silero_vad")),
            mishear_map=dict(c.get("mishear_map") or {}),
        )
        self.asr_thread.text_ready.connect(self._on_asr_text)
        self.asr_thread.status_changed.connect(self._on_asr_status)
        self.asr_thread.error_occurred.connect(self._on_pipeline_error)
        self.asr_thread.model_ready.connect(self._on_model_ready)
        self.asr_thread.start()

        # 首次使用的模型需要下载（可能上百 MB）：轮询缓存目录增量，
        # 在状态栏给出进度，避免用户在一句静态文案里无限等待
        if not self.asr_thread.model_cached(c.get("asr_model")):
            self._start_model_download_feedback(c.get("asr_model"))

        self.capture_thread = CaptureThread(
            c.get("source_type"),
            # v2.0.1：device_index=0 是合法设备（PyAudio 从 0 计数），
            # `or -1` 会把 0 吞成"默认设备"导致静默错配
            c.get("device_index") if c.get("device_index") is not None else -1,
            self,
            # v2.0.6：设备名随行——采集线程按名回查，热插拔索引漂移不再抓错源
            device_name=str(c.get("device_name") or ""),
            # v2.3.3（P1）：低延迟模式——直播/新闻场景缩短分段与判停
            low_latency=bool(c.get("low_latency_mode")),
        )
        self.capture_thread.segment_ready.connect(self.asr_thread.submit)
        self.capture_thread.level_changed.connect(self._on_level)
        self.capture_thread.error_occurred.connect(self._on_pipeline_error)
        self.capture_thread.low_input.connect(self._on_low_input)
        self.capture_thread.muted.connect(self._on_muted)
        self.capture_thread.start()

        # v2.2.11：无产出指引改「模型就绪后」起算（见 _on_model_ready 重挂计时），
        # 此处仅为"模型秒就绪"快路径兜底。
        # v2.3.11：15s→25s——六轮实测里浏览器打开视频到出声普遍要 5~15 秒，
        # 15 秒窗口下这条指引在"开网页马上按 J"的主场景每次必报，成了固定噪音；
        # 25 秒仍能兜住真静音场景（P8 电平守卫逻辑不变）
        self._no_segment_hint_done = False
        self._no_segment_timer = QTimer(self)
        self._no_segment_timer.setSingleShot(True)
        self._no_segment_timer.timeout.connect(self._no_segment_hint)
        self._no_segment_timer.start(25000)

        if c.get("overlay_enabled") and not self.overlay.isVisible():
            self.set_overlay_enabled(True)
        self.update_overlay_status()

    def _no_segment_hint(self):
        """模型就绪 25 秒仍零字幕时的一次性指引（v2.2.11 起算点=就绪后；
        v2.3.11 窗口 15s→25s，浏览器起播要 5~15 秒）。"""
        if not self.running or getattr(self, "_no_segment_hint_done", True):
            return
        # v2.3.6（P8，CBS 实测轮抓到）：视频还在缓冲/音量在跳时别急着怪用户——
        # 近 15 秒有过电平活动就顺延复检，不设置已提示标记
        if time.monotonic() - getattr(self, "_last_level_sound", 0.0) < 15.0:
            t = getattr(self, "_no_segment_timer", None)
            if t is not None:
                t.start(25000)
            return
        self._no_segment_hint_done = True
        if getattr(self, "_asr_ready", False) and getattr(self, "session_count", 0) == 0:
            from app import log as app_log
            app_log.log("pipeline.no_segments_hint", window_s=25,
                        source=self.config.get("source_type"))
            self._set_engine_status(
                "模型就绪 25 秒仍无识别结果：请确认所选设备正在播放声音（音量条应有波动），"
                "系统音量/应用音量未静音，或到「设置-音频输入」更换设备")
            self.update_overlay_status()

    def _start_model_download_feedback(self, model_size):
        self._model_dl_model = model_size
        self._model_dl_total = MODEL_SIZES_MB.get(model_size, 480)
        self._stop_model_download_feedback()
        self._dl_start = None  # v2.2.11：慢速探测基线（首次 tick 建立）
        self._model_dl_timer = QTimer(self)
        self._model_dl_timer.setInterval(600)
        self._model_dl_timer.timeout.connect(self._tick_model_download_feedback)
        self._model_dl_timer.start()
        self._tick_model_download_feedback()

    def _stop_model_download_feedback(self):
        timer = getattr(self, "_model_dl_timer", None)
        if timer is not None:
            timer.stop()
            self._model_dl_timer = None
            self._set_alert(None)  # v2.2.5：下载结束清掉进度横幅

    def _tick_model_download_feedback(self):
        from app.asr.engine import AsrThread
        d = AsrThread.model_cache_dir(getattr(self, "_model_dl_model", ""))
        mb = 0.0
        if d.exists():
            try:
                mb = sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) / 1048576.0
            except Exception:
                pass
        total = self._model_dl_total
        pct = min(99, int(mb * 100 / total))
        # v2.2.11：慢速探测——连续 20 秒不足 5MB 时提示代理入口（B2）
        import time as _t
        now = _t.monotonic()
        if getattr(self, "_dl_start", None) is None:
            self._dl_start = (now, mb)
        slow = (now - self._dl_start[0] > 20) and (mb - self._dl_start[1] < 5)
        if mb >= total * 0.9 or pct >= 99:
            slow = False
        hint = "· 速度慢？到「设置-通用」配置代理可显著提速" if slow else ""
        # v2.2.14：ETA——采样 3 秒后按平均速度估算剩余（起步阶段不显示，
        # 避免"剩余 3 小时"式惊吓；接近完成时也不显示，误差大）
        from app.fmt import eta_text
        eta = ""
        el = now - self._dl_start[0]
        if el > 3 and pct < 95:
            sp = (mb - self._dl_start[1]) / el
            if sp > 0.02:
                t = eta_text((total - mb) / sp)
                if t:
                    eta = f"，{sp:.1f}MB/s，剩余约{t}"
        # v2.2.5：模型下载进度走彩色横幅（下载是当前最重要的事，别挤状态行）
        self._set_engine_status("正在下载识别模型…")
        self._set_alert(f"⬇ 正在下载识别模型（{mb:.0f}/{total}MB，{pct}%{eta}，"
                        f"仅首次；完成前请保持网络畅通{hint}）…")

    def _set_engine_status(self, text):
        self._engine_status_text = text
        # v2.0.8：积压警示置顶——"识别积压"出现后任何后续状态都追加提醒
        # （此前一句话即被"识别完成/就绪"覆盖，用户从未看到丢段原因）
        # v2.2.5：积压/连续失败等关键提示改走独立彩色横幅（不再挤常规状态行）
        if getattr(self, "_backlog_warn", False):
            self._set_alert("⚠ 积压丢段中：CPU 转写跟不上，"
                            "建议到「设置-语音识别」换 small/tiny 模型")
        elif getattr(self, "_heavy_cpu_warn", False):
            # v2.3.1：重模型+CPU 预警（用户实测"非常不好用"根因之一：medium/CPU
            # 每 10s 音频要 10~15s 转写，字幕越拖越晚永远追不上，且毫无提示）
            self._set_alert("⚠ 当前为重模型且运行在 CPU：字幕会明显滞后。建议到"
                            "「设置-语音识别」换 small，或安装 GPU 加速后选「强制 GPU」")
        elif getattr(self, "_engine_fallback_warn", None):
            # v2.3.2（G2）：在线引擎不可达预警持续展示（后续常规状态不覆盖）
            self._set_alert(self._engine_fallback_warn, error=True)
        elif not ("识别积压" in text or "积压" in text or "跳过" in text):
            self._set_alert(None)
        if not getattr(self, "_low_input_warn", False) and not getattr(self, "_muted_warn", False):
            self.engine_status_label.setText(text)

    def _set_alert(self, text, error=False):
        """关键提示横幅（v2.2.5）：text=None 隐藏。error=True 用橙红警示色。"""
        banner = getattr(self, "alert_banner", None)
        if banner is None:
            return
        if not text:
            banner.setVisible(False)
            banner.setText("")
            return
        banner.setText(text)
        banner.setStyleSheet(
            "color: #ff8a5c;" if error else "color: #fbbf24;")
        banner.setVisible(True)

    def _on_asr_status(self, text):
        # v2.0.4：幽灵回调守卫 + 过期线程守卫——停止后已入队的迟到状态、
        # 或重启管线后旧 AsrThread 的残余状态，都不得覆盖当前 UI。
        # 此前该信号是全项目唯一没有 running 守卫的后端回调（v2.0.1 只补了
        # low_input/muted/result 三类），热键连按后状态栏永久卡在
        # "正在加载tiny模型"的根因之一：孤儿加载线程的加载消息在
        # stop_pipeline 写完"已停止"之后才送达
        if not self.running or self.sender() is not self.asr_thread:
            return
        if "识别积压" in text:
            # v2.0.8：积压提示置顶常驻——此前一句话即被后续状态覆盖，用户
            # 从未看到丢段原因（实测 8 段提交 0 条字幕的根因提示）
            self._backlog_warn = True
        self._set_engine_status(f"识别: {text}")

    def _on_translate_status(self, text):
        if not self.running or self.sender() is not self.translate_thread:
            return
        # v2.3.2（G2）：主引擎恢复切回 → 撤销不可达预警横幅
        if "已恢复" in text and "切回" in text and getattr(self, "_engine_fallback_warn", None):
            self._engine_fallback_warn = None
        self._set_engine_status(f"翻译: {text}")

    def _on_engine_fallback(self, engine_desc, reason):
        """v2.3.2（G2）：在线引擎不可达的事前横幅——此前只有事后日志，
        代理=直连的用户整场翻译频繁失败也不知道为什么（模拟用户报告缺口）。"""
        self._engine_fallback_warn = (
            f"⚠ 在线翻译引擎不可达（{engine_desc}）：{reason}。"
            "译文频繁出错请到「设置-通用」配置代理，或改用「自动」引擎")
        if self.running:
            self._set_alert(self._engine_fallback_warn, error=True)

    def _on_model_ready(self):
        # v2.0.4：模型就绪标记 + 停止下载进度反馈（原直连拆槽）
        self._asr_ready = True
        # v2.4.4（BUG-2）：就绪即刷新速览卡——加载后才知道实际设备
        # （GPU 回落 CPU 时"识别模型 xxx（GPU）"的谎报由本行纠正）
        self._refresh_quick_panel()
        self._stop_model_download_feedback()
        # v2.2.11：无产出指引计时改由"模型就绪"起算——此前在
        # start_pipeline 起算单发 30s，模型加载>30s（首次下载/大模型CPU）
        # 时计时器先于就绪到期，指引永不触发（B1 真 bug 修复）
        # v2.3.11：窗口 15s→25s（浏览器起播延迟主场景噪音）
        if (getattr(self, "running", False)
                and getattr(self, "_no_segment_timer", None) is not None
                and not getattr(self, "_no_segment_hint_done", False)):
            self._no_segment_timer.start(25000)
        # v2.2.12：就绪但还没出字——状态灯呼吸 + 明确"正在聆听"状态行（#3）
        if getattr(self, "running", False) and getattr(self, "session_count", 0) == 0:
            self._set_engine_status("模型就绪，正在聆听…（播放声音或说话即可出字幕）")
            self._set_listen_pulse(True)

    def _on_level(self, value):
        """电平槽。契约：value 为 capture 的 0~1 比例（见 CaptureThread
        .level_changed v2.3.16 注释）——有声判据 3%、音量条换算百分数。"""
        # v2.0.4：停止后迟到的电平事件不再点亮音量条
        # v2.3.15（P19）：capture 发的 value 是 0~1 的比例（min(1, level*8)），
        # 旧条件 value>=3 恒假——"最近有声"时间戳永不更新（P8 电平守卫与 P16
        # 静默巡查双双形同虚设，实测尾句 1s 抢送/跨片不合并）、音量条 setValue
        # 收小数恒 0（界面让用户"看音量条波动"是空话）。统一换算成百分比。
        if not self.running:
            return
        if value >= 0.03:
            # v2.3.6（P8）：记录"最近有声"时刻（3% 噪声地板之上算有声）
            self._last_level_sound = time.monotonic()
        self.level_bar.setValue(int(value * 100))

    def _on_low_input(self, quiet):
        """采集线程报告输入信号持续过弱/恢复正常。"""
        from app import log as app_log
        app_log.log("capture.low_input", quiet=bool(quiet))
        if not self.running:
            # v2.0.1：幽灵回调守卫——停止后仍可能收到已入队的 Queued 信号
            return
        self._low_input_warn = quiet
        if quiet and self.running:
            # v2.4.4（BUG-3）：本会话已成功出过字幕 = 信号可识别已被事实证明，
            # 之后的静音期（句间停顿/音频播完）不再挂"信号过弱"——告警服务的
            # 是"还没证明过可识别"的阶段；"有字幕在出却说可能识别不了"和
            # "播完静音仍警示"都是误导（无产出场景另有 25s 无字幕指引兜底）
            if getattr(self, "_caption_seen", False):
                self._low_input_warn = False
                return
            self.engine_status_label.setText(
                "⚠ 输入信号过弱：字幕可能无法识别，请检查系统音量或音频设备")
        else:
            self.engine_status_label.setText(getattr(self, "_engine_status_text", ""))
        self.update_overlay_status()

    def _on_muted(self, m):
        """系统静音盲区提示（补充5）：静音且抓系统声音时给出确定性指引。"""
        if not self.running:
            return  # v2.0.1：幽灵回调守卫（迟到的 muted 曾覆盖"已停止"状态）
        if m and self.running:
            self._muted_warn = True
            self.engine_status_label.setText(
                "⚠ 系统已静音：正在抓取系统声音，静音期间不会有字幕；取消静音后自动恢复")
        else:
            self._muted_warn = False
            self.engine_status_label.setText(getattr(self, "_engine_status_text", ""))
        self.update_overlay_status()

    @staticmethod
    def _detach_thread(t):
        """停止超时的残留线程必须与界面断开信号，避免再向 UI 发送过期字幕。"""
        if t is None:
            return
        for name in ("segment_ready", "level_changed", "error_occurred", "low_input",
                     "text_ready", "status_changed", "result_ready", "model_ready",
                     "muted"):
            sig = getattr(t, name, None)
            if sig is not None:
                try:
                    sig.disconnect()
                except Exception:
                    pass

    def stop_pipeline(self):
        if not self.running:
            return
        from app import log as app_log
        app_log.log("pipeline.stop")
        self.running = False
        self.toggle_button.setText("开始翻译")
        self.toggle_button.setObjectName("PrimaryButton")
        self.toggle_button.style().unpolish(self.toggle_button)
        self.toggle_button.style().polish(self.toggle_button)
        self._set_listen_pulse(False)  # v2.2.12：停止熄灭呼吸
        self._heavy_cpu_warn = False   # v2.3.1：撤重模型CPU预警
        self._engine_fallback_warn = None  # v2.3.2（G2）：撤引擎不可达预警
        self.status_dot.setStyleSheet("background-color: #3a4152; border-radius: 7px;")
        self.status_text.setText("未启动")
        self.level_bar.setValue(0)
        self._low_input_warn = False
        self._muted_warn = False  # v2.0.1：漏复位曾让悬浮条停止后仍显示"系统静音中"
        self._fail_streak = 0  # v2.0.2：会话结束时清零连续失败计数
        self._backlog_warn = False  # v2.0.8：积压警示随会话结束复位
        self._set_alert(None)  # v2.2.5：停止时清掉提示横幅
        if getattr(self, "_pending", None):
            # v2.2.0：停止时清空流式占位配对——队列里未及翻译的卡片不再等
            # 迟到译文（下次会话不复用旧卡片）
            self._pending.clear()
        # v2.3.6（P9）：低延迟攒句缓冲随会话清零（未送出的碎片不等迟到译文）
        self._tgroup = []
        if getattr(self, "_tgroup_by_src", None):
            self._tgroup_by_src.clear()
        tg = getattr(self, "_tgroup_timer", None)
        if tg is not None:
            tg.stop()
        self._stop_model_download_feedback()
        timer = getattr(self, "_no_segment_timer", None)
        if timer is not None:
            timer.stop()
        self._no_segment_hint_done = True
        # v2.4.4（BUG-9）：还有字幕卡时停在列表页——此前停止一律切回速览卡，
        # 用户想回看/导出刚才的会话记录时列表凭空消失（计数还在、内容没了）
        if not self._has_cards():
            self.stack.setCurrentIndex(0)

        threads = (self.capture_thread, self.asr_thread, self.translate_thread)
        # 先断开全部信号再停止：否则停止过程中/停止后仍会收到迟到的状态信号，
        # 把"已停止"覆盖成"就绪，正在聆听..."之类的僵尸状态
        for t in threads:
            if t:
                self._detach_thread(t)
                t.stop()
        if threads[0]:
            threads[0].wait(2000)
        # v2.0.4：模型加载期的 AsrThread 阻塞在 WhisperModel() 构造里，
        # 响应不了 _stop 标志也到不了队列哨兵，等满 3 秒只会白白冻结 GUI
        # （阻塞期间按下的热键全部排队，恢复后被逐条当作新 toggle，
        # start/stop 毫秒级翻转——实机日志实证的根因推手）。
        # 加载未完成（_asr_ready 为假）时缩短等待，线程交孤儿容器收尾
        for i, t in enumerate(threads[1:], start=1):
            if t:
                timeout = 3000
                if i == 1 and not getattr(self, "_asr_ready", False):
                    timeout = 500
                # 不在 GUI 线程长等（模型加载中停止曾最长冻界面 15s）：
                # 信号已断开，线程收尾放后台自行完成（v1.9.4）
                t.wait(timeout)
        # v2.0.3：超时仍未退出的线程不再裸丢引用（只剩 parent 关系，MainWindow
        # 析构时会销毁运行中的 QThread → qFatal 崩溃）。改为摘除 parent、
        # 挂模块级容器保引用，finished 后 deleteLater 自清理
        for t in threads:
            if t and t.isRunning():
                t.setParent(None)
                _orphan_threads().append(t)
                t.finished.connect(t.deleteLater)
                from app import log as app_log
                app_log.log("pipeline.orphan_thread", cls=type(t).__name__)
                if t is threads[1] and not getattr(self, "_asr_ready", False):
                    # v2.0.4：加载期停止的专项记录——加载线程随后台完成即静默退出
                    app_log.log("pipeline.stop_during_model_load",
                                model=self.config.get("asr_model"))
        self.capture_thread = None
        self.asr_thread = None
        self.translate_thread = None
        self.engine_status_label.setText("引擎：已停止")
        self._log_latency_summary()   # v2.3.20（P26）：会话延迟摘要入日志
        self.update_overlay_status()

    def _on_pipeline_error(self, msg):
        from app.errors import friendly_message
        from app import log as app_log
        app_log.log("pipeline.error", detail=str(msg)[:200])
        if not self.running:
            # v2.0.4：停止后迟到的管线错误不再覆盖"已停止"（幽灵回调守卫，
            # 与 _on_asr_status/_on_translate_status 同一策略）
            return
        msg = friendly_message(str(msg))
        if self.running and ("采集" in msg or "回环" in msg or "音频" in msg or "设备" in msg):
            self.stop_pipeline()
            # stop_pipeline 会把状态重置为"已停止"，错误信息要在其后显示才能被看到
            self.engine_status_label.setText(f"错误：{msg}")
            self._show_info("音频错误", msg)
        else:
            self.engine_status_label.setText(msg)

    def _on_asr_text(self, text, detected, duration, t_flush=-1.0):
        if not self.running:
            return
        # v2.4.4（BUG-3）：字幕成功上屏即证明输入信号可识别——撤"输入信号过弱"
        # 告警。此前该告警挂到会话结束，一边出字幕一边说"字幕可能无法识别"，
        # 与事实自相矛盾（摸底实测：10 条字幕在屏、横幅仍警示）
        if getattr(self, "_low_input_warn", False):
            self._low_input_warn = False
            self._set_engine_status(getattr(self, "_engine_status_text", ""))
            self.update_overlay_status()
        self._caption_seen = True
        # v2.3.20（P26）：识别段延迟——"音频切分完成→原文上屏"（含判停、
        # 排队、转写、上屏全程）。t_flush 由 AsrThread 随 text_ready 第 4 参带来。
        if t_flush and t_flush > 0:
            self._lat_add("_lat_reco", time.monotonic() - t_flush)
        self._set_listen_pulse(False)  # v2.2.12：首段文字上屏即停呼吸（#3）
        # v2.2.11：记录本段音频时间轴（会话相对秒 + Whisper 语音时长）；
        # 流式路径直接写上占位卡，一次性路径由 _on_translated 建卡时取快照
        self._last_asr_timing = self._asr_timing(duration)
        # v2.2.3：连续流模式下原文是否入流由 overlay 自行按 show_source 决定
        # （"只显示译文"时原文不入流）
        if self.overlay.isVisible():
            self.overlay.show_pending(text)
        # v2.1.5：instant_caption 开关——开（默认）为流式两段式（原文先上屏、
        # 译文占位、就绪后原地补齐）；关 = 旧行为（识别+翻译都完成后一次性上屏）
        if not bool(self.config.get("instant_caption")):
            self._set_engine_status(f"识别完成 [{detected or '?'}] ({duration}s)，翻译中…")
            if self.translate_thread:
                self._submit_for_translation(text, detected)
            return
        show_source = bool(self.config.get("show_source"))
        card = self._new_card(text)
        # v2.3.17（P22）：占位卡始终显示原文——攒句/翻译等待期最长约 7 秒
        # （R11 直播 11445.71 三条同时占位），show_source=False 时列表只剩
        # 一排"⟳ …"，看着像卡死（系统在工作，界面在装死）。用户偏好"只看
        # 中文"时用弱化灰斜体顶过等待期，译文落地即恢复偏好设置。
        card.source_label.setVisible(True)
        if not show_source:
            card.source_label.setStyleSheet("color: #6b7488; font-style: italic;")
        card.target_label.setText("⟳ …")
        card.t_start, card.dur_s = self._last_asr_timing  # v2.2.11：SRT 时间轴
        self.scroll_layout.insertWidget(self.scroll_layout.count() - 1, card)
        if self.stack.currentIndex() == 0:
            self.stack.setCurrentIndex(1)
        if not hasattr(self, "_pending") or self._pending is None:
            self._pending = []
        self._pending.append((text, card))
        self._set_engine_status(f"识别完成 [{detected or '?'}] ({duration}s)，翻译中…")
        # v2.2.5：占位卡即刻聚焦（原文先出时用户视线在此）
        prev = getattr(self, "_active_card", None)
        if prev is not None and prev is not card:
            try:
                prev.set_active(False)
            except RuntimeError:
                pass
        self._active_card = card
        card.set_active(True)
        if self.overlay.isVisible():
            self.overlay.show_pending(text)   # v2.4.0：面板恒历史滚动，占位直入
        sb = self.scroll.verticalScrollBar()
        sb.setValue(sb.maximum())
        if self.translate_thread:
            self._submit_for_translation(text, detected)

    # ---------- v2.3.6（P9）：低延迟"上屏碎、翻译整句"两轨制 ----------

    def _submit_for_translation(self, text, detected):
        """v2.3.7（P9）低延迟两轨制：碎片立即上屏（占位卡已建），翻译攒整句再送。

        v2.3.8 实测修正：句末标点判据**无效**——whisper 对 6 秒音频切片会
        自行补句号（P9 验收实测每片都以 "." 收尾、逐片即送，攒句形同虚设）。
        真正的句子边界是**首字母大小写**：小写开头=上句延续（并入），
        大写/CJK/数字开头=新句开始（先把已攒的整句送出）。
        辅以 5 片硬上限与 7 秒静默兜底（v2.3.9：兜底窗口必须 > 6s 分片
        周期——2.5s 实测会在前后片之间先行冲出，攒句永不发生）。
        默认模式行为不变。"""
        if not bool(self.config.get("low_latency_mode")):
            self._submit_ts = getattr(self, "_submit_ts", {})
            self._submit_ts[text] = time.monotonic()  # v2.3.20（P26）
            self.translate_thread.submit(text, detected)
            return
        grp = getattr(self, "_tgroup", None)
        if grp is None:
            grp = self._tgroup = []
            self._tgroup_lang = ""
        if grp and self._starts_new_sentence(text):
            self._flush_tgroup()
            grp = self._tgroup
        grp.append(text)
        if len(grp) == 1:
            # v2.3.18（P23）：组寿命起点——绝对上限用它算，续片无法续命
            self._tgroup_start = time.monotonic()
        self._tgroup_lang = detected or self._tgroup_lang
        if len(grp) >= 5:
            self._flush_tgroup()
            return
        # v2.3.14（P16）：兜底从"单发 7 秒"升级为 1 秒巡查——音频已静默 ≥3.5s
        # 且末片收尾完整即立送；噪声环境仍由硬兜底接管。
        # v2.3.18（P23）修正：deadline 旧写法每次到达重置（now+7），链式续片
        # 会让头号句被后面的碎片无限扣住（第十二轮实测 13.4~13.6 秒，5 片链
        # 理论 ~30 秒）。现取"逐片 7s 间距"与"组寿命 10s 绝对上限"的较小值：
        # 单跳真延续（6~7.5s 到达）仍完整合并，多跳长链 10s 剪断，尾句有界。
        now = time.monotonic()
        self._tgroup_deadline = min(
            now + 7.0, getattr(self, "_tgroup_start", now) + 10.0)
        t = getattr(self, "_tgroup_timer", None)
        if t is None:
            t = QTimer(self)
            t.setInterval(1000)
            t.timeout.connect(self._tgroup_tick)
            self._tgroup_timer = t
        t.start()

    def _tgroup_tick(self):
        """v2.3.14（P16+守卫）：攒句巡查——"音频静默"不等于"句子说完"：
        whisper 分片之间天然有 2~4 秒交付间隙，实况新闻实测被 P16 初版误判
        成句尾、当场腰斩攒句（uranium.../were transferred.../before the US
        strikes. 三条各走）。因此静默立送只在末片"长得像说完了"时才允许；
        省略号/逗号/无标点收尾=明显未完，交给 7 秒硬兜底最终送出。"""
        if not getattr(self, "_tgroup", None):
            t = getattr(self, "_tgroup_timer", None)
            if t is not None:
                t.stop()
            return
        now = time.monotonic()
        quiet_for = now - getattr(self, "_last_level_sound", 0.0)
        final_looking = self._looks_final(self._tgroup[-1])
        if (quiet_for >= 3.5 and final_looking) or now >= getattr(self, "_tgroup_deadline", 0.0):
            self._flush_tgroup()
            t = getattr(self, "_tgroup_timer", None)
            if t is not None:
                t.stop()

    @staticmethod
    def _looks_final(text):
        """收尾判据（v2.3.14）：末片是否"看起来完整"。
        省略号（"…"/"..."，whisper 对未完语句的显式标记）→ 未完；
        逗号/无标点 → 未完；句末标点 .!?。！？ 及跟随引号/括号 → 完整。"""
        t = (text or "").rstrip()
        if not t:
            return True
        if t.endswith("...") or t.endswith("…"):
            return False
        return t[-1] in ".!?。！？\"'」』）)"

    @staticmethod
    def _starts_new_sentence(text):
        """新句判据（v2.3.8）：首字符为大写拉丁/CJK/数字；小写开头视为延续。"""
        t = (text or "").lstrip()
        if not t:
            return False
        ch = t[0]
        return ch.isupper() or ch.isdigit() or "\u4e00" <= ch <= "\u9fff"

    def _flush_tgroup(self):
        grp = getattr(self, "_tgroup", None) or []
        self._tgroup = []
        self._tgroup_start = None      # v2.3.18（P23）：组起点随组清空
        t = getattr(self, "_tgroup_timer", None)
        if t is not None:
            t.stop()
        if not grp or not self.translate_thread:
            return
        joined = " ".join(grp)
        combined = "".join(grp) if any("\u4e00" <= c <= "\u9fff" for c in joined) else joined
        self._tgroup_by_src = getattr(self, "_tgroup_by_src", {})
        self._tgroup_by_src[combined] = grp
        self._submit_ts = getattr(self, "_submit_ts", {})
        self._submit_ts[combined] = time.monotonic()  # v2.3.20（P26）
        self.translate_thread.submit(combined, getattr(self, "_tgroup_lang", ""))

    # ---------- v2.3.20（P26）：内置延迟自测 ----------

    def _lat_add(self, name, val):
        """惰性初始化 + 追加一条延迟样本（识别段 _lat_reco / 翻译段 _lat_tr）。"""
        lst = getattr(self, name, None)
        if lst is None:
            lst = []
            setattr(self, name, lst)
        lst.append(val)

    def _log_latency_summary(self):
        """会话结束把识别/翻译两段延迟的 p50/p95 打进日志——想测速看日志
        一行即可，不必再搭仪器（第十三轮三度折腾的教训）。随后清零。"""
        reco = getattr(self, "_lat_reco", []) or []
        tr = getattr(self, "_lat_tr", []) or []
        if reco or tr:
            from app import log as app_log
            app_log.log(
                "pipeline.latency",
                n_reco=len(reco),
                reco_p50=round(_pct(reco, 0.50), 2),
                reco_p95=round(_pct(reco, 0.95), 2),
                n_tr=len(tr),
                tr_p50=round(_pct(tr, 0.50), 2),
                tr_p95=round(_pct(tr, 0.95), 2),
                tr_max=round(max(tr), 2) if tr else 0)
        self._lat_reco, self._lat_tr, self._submit_ts = [], [], {}

    # ---------- v2.3.13（P14）：字幕卡右键一键纠错（词典可达性） ----------
    # 第八轮实测：误听词典做了八轮仍空——不是没工具，是"看到错→查原文→
    # 开设置→找词典→手打"链条太长。这里把纠错入口直接放卡片上：错的已
    # 预填，用户只打"对的"。

    def _new_card(self, text):
        """字幕卡工厂：统一挂右键纠错菜单（三处创建点共用）。"""
        return CaptionCard(text, on_menu=self._card_menu)

    def _card_menu(self, card):
        menu = QMenu(self)
        act = menu.addAction("复制原文")
        act.triggered.connect(lambda: QApplication.clipboard().setText(card.source_text))
        tr = card.translated_text()
        act = menu.addAction("复制译文")
        act.setEnabled(bool(tr))
        act.triggered.connect(lambda: QApplication.clipboard().setText(card.translated_text()))
        menu.addSeparator()
        act = menu.addAction("纠正识别（加入误听词典）…")
        act.triggered.connect(lambda: self._correct_from_card(card, "mishear_map"))
        act = menu.addAction("纠正译文（加入译文修正词典）…")
        act.setEnabled(bool(tr))
        act.triggered.connect(lambda: self._correct_from_card(card, "translate_fix_map"))
        return menu

    def _correct_from_card(self, card, dict_key):
        wrong0 = card.source_text if dict_key == "mishear_map" else card.translated_text()
        self._correct_with_prefill(dict_key, wrong0)

    def _overlay_correct(self, dict_key, wrong0):
        """v2.3.21（P29）：悬浮条右键菜单的纠错入口——与卡片同源同对话框。"""
        self._correct_with_prefill(dict_key, wrong0)

    def _correct_with_prefill(self, dict_key, wrong0):
        wrong0 = (wrong0 or "").strip()
        if not wrong0:
            return                      # 无内容不弹框（菜单项本应置灰，双保险）
        title = "纠正识别" if dict_key == "mishear_map" else "纠正译文"
        wrong, right = self._dict_dialog(title, wrong0)
        if wrong is None:
            return
        wrong, right = wrong.strip(), right.strip()
        if wrong and right:
            self._add_dict_entry(dict_key, wrong, right)

    def _dict_dialog(self, title, wrong_prefill):
        """双字段小对话框：错误片段（预填整句让用户删改）+ 正确文本。"""
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.setMinimumWidth(480)
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel("错误片段（已从字幕预填，删改到只剩要纠正的词句即可）："))
        e_wrong = QLineEdit(wrong_prefill)
        v.addWidget(e_wrong)
        v.addWidget(QLabel("正确文本："))
        e_right = QLineEdit()
        e_right.setPlaceholderText("例如：Norfolk")
        v.addWidget(e_right)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        return (e_wrong.text(), e_right.text()) if dlg.exec() == QDialog.Accepted else (None, None)

    def _add_dict_entry(self, dict_key, wrong, right):
        m = dict(self.config.get(dict_key) or {})
        m[wrong] = right
        self.config.set(dict_key, m)   # Config.set 原子落盘
        tip = "误听词典" if dict_key == "mishear_map" else "译文修正词典"
        self._set_alert(f"已保存到{tip}：「{wrong[:16]}」→「{right[:16]}」· 下次开始翻译生效")

    def _take_pending(self, source_text):
        """按原文取出最早的待补齐卡片（流式两段式配对，v2.1.4）。"""
        pend = getattr(self, "_pending", None) or []
        for i, (txt, card) in enumerate(pend):
            if txt == source_text and card.is_pending():
                pend.pop(i)
                return card
        # 原文被 max_history 裁掉等场景：只清记录
        for i, (txt, card) in enumerate(pend):
            if txt == source_text:
                pend.pop(i)
                return card
        return None

    def _on_translated(self, source_text, translated, engine, detected, error):
        # v2.0.1：幽灵回调守卫——停止后仍会收到已入队的翻译结果，
        # 此前会新增字幕卡片、把界面翻回列表页、悬浮条显示"运行中"
        if not self.running:
            return
        self._last_engine_name = engine
        # v2.3.20（P26）：翻译段延迟——"提交翻译→译文落地"（含攒句等待+引擎耗时）。
        # 用改写前的 source（合并组的 combined 键）取提交时刻。
        t_submit = getattr(self, "_submit_ts", {}).pop(source_text, None)
        if t_submit is not None:
            self._lat_add("_lat_tr", time.monotonic() - t_submit)
        # v2.3.6（P9）：低延迟攒句结果——合并译文落组内末卡，
        # 前面的碎片卡只留原文（整句译文不再被拆成半截话各翻各的）
        gmap = getattr(self, "_tgroup_by_src", None)
        if gmap and source_text in gmap:
            pieces = gmap.pop(source_text)
            for c in pieces[:-1]:
                pc = self._take_pending(c)
                if pc is not None:
                    pc.set_merged_away()
            source_text = pieces[-1]
        # v2.0.2：连续失败升级提示——备援链全灭（如 Google 全通道被封 +
        # MyMemory 配额尽 + 无离线包）时，不能只让每条字幕各自报错
        if error:
            self._fail_streak = getattr(self, "_fail_streak", 0) + 1
        else:
            self._fail_streak = 0
        show_source = bool(self.config.get("show_source"))
        # v2.1.5：流式关（instant_caption=False）时无占位卡，走全新建卡；
        # 流式开（默认）优先原地补齐识别时已上屏的占位卡，找不到
        # （管线重启/被裁剪等）才新建，兜底兼容旧行为
        if not bool(self.config.get("instant_caption")):
            card = self._new_card(source_text)
            self.scroll_layout.insertWidget(self.scroll_layout.count() - 1, card)
            card.t_start, card.dur_s = getattr(self, "_last_asr_timing", (None, None))
            # v2.1.5：切回一次性上屏时清掉流式占位队列（防陈旧配对）
            if getattr(self, "_pending", None):
                self._pending.clear()
        else:
            card = self._take_pending(source_text)
            if card is None:
                card = self._new_card(source_text)
                self.scroll_layout.insertWidget(self.scroll_layout.count() - 1, card)
                card.t_start, card.dur_s = getattr(self, "_last_asr_timing", (None, None))
        if self.stack.currentIndex() == 0:
            self.stack.setCurrentIndex(1)
        if error:
            card.set_failed(error)
        else:
            card.set_result(translated, engine, detected, show_source)
        # v2.2.5：字幕卡聚焦态——新完成的卡强调边框，前一张降级渐隐
        prev = getattr(self, "_active_card", None)
        if prev is not None and prev is not card:
            try:
                prev.set_active(False)
            except RuntimeError:
                pass  # 前卡可能已被 max_history 裁剪销毁
        self._active_card = card
        card.set_active(True)
        self.session_count = getattr(self, "session_count", 0) + 1
        self.session_label.setText(f"本次会话：{self.session_count} 条")
        if error:
            self._set_engine_status(
                f"⚠ 翻译失败（连续 {self._fail_streak} 条）：{error}")
            # v2.2.5：连续失败走彩色横幅（排障建议不被截断）
            advice = "检查网络/代理节点，或到「设置-翻译」测试通道 / 下载离线语言包"
            self._set_alert(
                f"⚠ 翻译连续失败 {self._fail_streak} 条 · {advice}", error=True)
        else:
            self._set_engine_status(f"引擎：{engine} · 源语言: {detected or '?'}")
            self._set_alert(None)
        self.update_overlay_status()
        if error:
            # 连续 ≥3 条失败：状态行升级为通道级提示 + 悬浮条橙红常驻，
            # 直到有成功译文才恢复正常
            advice = "检查网络/代理节点，或到「设置-翻译」测试通道 / 下载离线语言包"
            self.overlay.set_status(
                f"翻译连续失败 {self._fail_streak} 条 · {advice}" if self._fail_streak >= 3
                else "翻译失败 · 检查网络或切换引擎",
                is_error=True)
        if self.overlay.isVisible():
            # v2.1.8：三档路由统一由 overlay.show_pending_result 内部分派
            # （跑马灯=淡入最新句；列表=占位补齐；单条=直接刷新）
            self.overlay.show_pending_result(
                source_text, translated or ("[" + engine + " 翻译失败]"), show_source)
        sb = self.scroll.verticalScrollBar()
        sb.setValue(sb.maximum())
        evicted = []
        while self.scroll_layout.count() - 1 > self.config.get("max_history"):
            item = self.scroll_layout.takeAt(0)
            if item.widget():
                evicted.append(item.widget())
                item.widget().deleteLater()
        # v2.2.0：_pending 悬挂引用清理——被裁剪的占位卡随后 deleteLater，
        # 若仍在 _pending 里，迟到译文调用 set_result 会打在已删 C++ 对象上
        # 崩溃。同步移除配对记录
        if evicted and getattr(self, "_pending", None):
            doomed = {id(w) for w in evicted}
            self._pending = [(t, c) for (t, c) in self._pending if id(c) not in doomed]

    def _teardown(self):
        """退出前的统一清理（v2.0.1）：此前 exit 分支靠 closeEvent 内重入
        self.close() 触发本段，被 Qt 嵌套 close 抑制，导致退出路径整体失效
        （窗口隐藏但管线继续跑、托盘退出失灵）。"""
        hotkey.unregister()
        self._save_settings()
        self.stop_pipeline()
        # v2.0.3：对仍存活的孤儿线程 terminate 兜底——运行中的 QThread 随
        # MainWindow 析构会 qFatal 崩溃，宁可强杀
        for t in _orphan_threads():
            try:
                t.terminate()
            except Exception:
                pass
        self.overlay.close()
        if getattr(self, "tray", None):
            self.tray.hide()

    def closeEvent(self, event):
        if getattr(self, "_quitting", False):
            self._teardown()
            event.accept()
            QApplication.quit()
            return
        action = self.config.get("close_action")
        if action == "tray":
            event.ignore()
            self._hide_to_tray()
            return
        if action == "exit":
            self._quitting = True
            self._teardown()
            event.accept()
            QApplication.quit()
            return
        box = QMessageBox(self)
        box.setWindowTitle("关闭 LiveSubtitle")
        box.setText("关闭软件后要做什么？")
        box.setInformativeText("隐藏到托盘后，字幕悬浮窗继续显示，可从右下角托盘图标重新打开主窗口。")
        tray_btn = box.addButton("隐藏到托盘", QMessageBox.AcceptRole)
        exit_btn = box.addButton("退出程序", QMessageBox.DestructiveRole)
        cancel_btn = box.addButton("取消", QMessageBox.RejectRole)
        remember_box = QCheckBox("记住我的选择，下次不再询问")
        box.setCheckBox(remember_box)
        box.exec()
        # 打包环境下部分 PySide6 版本 box.checkBox() 返回的对象没有 isChecked，
        # 直接调用会抛未捕获异常，导致连"取消"都会关掉窗口；改用自己持有的引用并兜底
        try:
            remember = bool(remember_box.isChecked())
        except Exception:
            remember = False
        clicked = box.clickedButton()
        if clicked == cancel_btn:
            event.ignore()
            return
        if clicked == tray_btn:
            if remember:
                self.config.set("close_action", "tray")
            event.ignore()
            self._hide_to_tray()
        else:
            if remember:
                self.config.set("close_action", "exit")
            self._quitting = True
            self._teardown()
            event.accept()
            QApplication.quit()

    def _hide_to_tray(self):
        self._save_settings()
        self.hide()
        if getattr(self, "tray", None):
            self.tray.showMessage(
                "LiveSubtitle 仍在运行",
                "字幕悬浮窗继续工作。左键托盘图标恢复窗口，右键可退出/切来源；"
                "关闭行为可在「设置-通用」修改。",
                QSystemTrayIcon.Information, 3500)


def run_app():
    import sys
    # 单实例互斥：避免两个进程同时写配置、争抢音频设备
    try:
        ERROR_ALREADY_EXISTS = 183
        _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "LiveSubtitle_SingleInstance_Mutex")
        if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            app = QApplication(sys.argv)
            QMessageBox.warning(
                None, "LiveSubtitle",
                "LiveSubtitle 已经在运行中。\n\n"
                "请点击任务栏右下角托盘区的 LiveSubtitle 图标打开主窗口。")
            return
    except Exception:
        pass  # 互斥检测失败时按原行为启动，不阻塞正常使用
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName("LiveSubtitle")
    app.setWindowIcon(QIcon(str(icon_path())))
    # 主窗口会隐藏到托盘继续工作，不能因"最后一个窗口关闭"而自动退出；
    # 退出统一由 closeEvent 的 quitting 分支显式调用 QApplication.quit()
    app.setQuitOnLastWindowClosed(False)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
