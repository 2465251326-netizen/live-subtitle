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
from app.i18n import ui_text, ui_fmt
from app.audio.capture import CaptureThread
from app.asr.engine import AsrThread
from app.asr.preview import StreamPreview
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


# v2.20.6（i18n）：卡片"译文未落地"的内部标记。这三个串**不参与界面翻译**——
# 它们同时是显示文本和判据（右键可用性、导出过滤、待决队列），一旦按语言翻译掉，
# 散在各处的 ==/in 判断会同时失灵，导出又会把失败卡混进去（v2.20.4 刚修过的老问题）。
# 规则：比较一律走 `_is_untranslated()`，只有写进 QLabel 的那一刻才过 ui_text()。
TEXT_PENDING = "..."
TEXT_PENDING_STREAM = "⟳ …"
TEXT_FAILED = "[翻译失败]"
_PENDING = (TEXT_PENDING, TEXT_PENDING_STREAM)
_UNTRANSLATED = _PENDING + (TEXT_FAILED,)


def _is_one_of(target, marks):
    """既认原始形态，也认按当前界面语言显示后的形态——比较点因此与 tr()
    的调用时机无关（模块导入早于语言初始化也不会错位）。"""
    return target in marks or target in tuple(ui_text(m) for m in marks)


def _is_untranslated(target):
    """译文是否"还没落地"（占位 / 失败 / 已并入的空文本）。"""
    return (not target) or _is_one_of(target, _UNTRANSLATED)


class CaptionCard(QFrame):
    def __init__(self, source_text, parent=None, on_menu=None):
        super().__init__(parent)
        self.setObjectName("CaptionCard")
        # v2.18.1：卡片状态用动态属性表达（""=占位/未激活、"active"=最新句、
        # "old"=已渐隐历史），objectName 恒为 CaptionCard——基础卡面样式不再
        # 被状态切换踩掉（原因见 set_active）
        self.setProperty("state", "")
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
        self.target_label = QLabel(ui_text(TEXT_PENDING))
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
        # v2.7.6（A）：推测式增量翻译——当前译文是"整句还没攒完"的中间版本。
        # 终版到达后由 set_result 置回 False（导出/终态化据此区分完整句子）。
        self.spec = False

    def set_result(self, translated, engine, detected, show_source):
        self.spec = False      # v2.7.6（A）：终版覆盖，脱离推测态
        if translated:
            self.target_label.setText(translated)
        else:
            self.target_label.setText(ui_text(TEXT_FAILED))
        note = ui_fmt("{time} · {det} · 引擎: {eng}",
                      time=datetime.now().strftime('%H:%M:%S'),
                      det=detected or '?', eng=engine)
        self.meta_label.setText(note)
        self.source_label.setStyleSheet("")   # v2.3.17（P22）撤下占位弱化色
        self.source_label.setVisible(show_source)

    def set_spec_result(self, translated, engine, detected, show_source):
        """v2.7.6（A）：推测式中间版译文上屏——只刷新译文文本，**不做终态化**
        （不摘 _pending、不计会话条数、不切聚焦态）；整句终版随后走 set_result
        原地覆盖。收益：连续语流中译文不再干等攒句冲刷（实测 hold_p50≈4.1s），
        碎片一到就上屏并随句子生长。中间版译文可能不完整（半句），meta 标"攒句中"。"""
        if not translated:
            return                     # 推测失败静默——终版会来，中间版是增益
        self.spec = True
        self.target_label.setText(translated)
        self.source_label.setStyleSheet("")
        self.source_label.setVisible(show_source)
        self.meta_label.setText(
            ui_fmt("{time} · {det} · 引擎: {eng} · 攒句中",
                   time=datetime.now().strftime('%H:%M:%S'),
                   det=detected or '?', eng=engine))

    def finalize_spec(self):
        """v2.7.6（A）：停止/收尾时把推测中间版就地终态化。

        **保留已上屏的译文**——它虽然可能只是半句，但比覆盖成"未完成翻译"
        的失败文案有用得多（用户已经在看这行字）。只解除推测态并把 meta
        标注为"可能不完整"。返回 True 表示本卡原是推测态、已就地收口。"""
        if not self.spec:
            return False
        self.spec = False
        self.meta_label.setText(
            f"{datetime.now().strftime('%H:%M:%S')}{ui_text(' · 已停止 · 译文可能不完整')}")
        return True

    def is_pending(self):
        """是否仍处于"译文未落地"占位态（流式两段式，v2.1.4）。
        v2.7.6（A）：推测态（spec=True）虽已有译文，但仍算"未完成"——
        _take_pending 与终态化逻辑据此把它留在待决队列里等终版覆盖。"""
        if self.spec:
            return True
        return _is_one_of(self.target_label.text(), _PENDING)

    def translated_text(self):
        """当前译文；占位/失败/已并入态返回空串（右键菜单据此决定可用性）。
        v2.7.6（A）：推测态返回已有译文——用户看到就能复制，语义上"当前译文"。"""
        if self.spec:
            t = self.target_label.text()
            return "" if (not t or _is_one_of(t, (TEXT_FAILED,))) else t
        if self.is_pending():
            return ""
        t = self.target_label.text()
        return "" if (not t or _is_one_of(t, (TEXT_FAILED,))) else t

    def contextMenuEvent(self, event):
        # v2.3.13（P14）：卡片右键 → 复制原文/译文、一键加入修正词典
        if self._on_menu is None:
            return
        menu = self._on_menu(self)
        if menu is not None:
            menu.exec(event.globalPos())
            menu.deleteLater()

    def set_failed(self, msg):
        self.target_label.setText(ui_text(TEXT_FAILED))
        self.meta_label.setText(f"{datetime.now().strftime('%H:%M:%S')} · {msg}")

    def set_merged_away(self):
        """v2.3.6（P9）：低延迟组内前段碎片卡——译文并入末卡整句呈现，
        本卡只留原文（原文本就隐藏时整卡收起，不留孤零时间戳）。"""
        self.target_label.setText("")
        self.target_label.setVisible(False)
        if not self.source_label.isVisible():
            self.setVisible(False)

    def export_row(self):
        """导出用的一行快照 (meta, source, target, t_start, dur_s)。

        v2.20.3：卡片会被 `max_history` 淘汰并 `deleteLater`，而卡片是这场字幕
        **唯一的副本**（历史区早在 v2.20.0 删除，面板与主窗都不再另存）——淘汰即
        永久丢失。淘汰前先把这一行抄进导出台账。
        原文一律取标签文本、**不看可见性**：用户为清爽关掉「同时显示原文」，不该
        让 30 分钟后拿到的文件里原文永久消失（显示设置决定存档内容）。"""
        return (self.meta_label.text(), self.source_label.text(),
                self.target_label.text(),
                getattr(self, "t_start", None), getattr(self, "dur_s", None))

    def set_active(self, active):
        """聚焦态切换（v2.2.5）：active=True 换强调边框样式，False 渐隐。

        v2.18.1 根因修复：状态改走**动态属性 state**，不再覆写 objectName。
        旧实现把卡片唯一的 objectName 改成 CaptionCardActive/CaptionCardOld，
        而 styles.py 写的是 `QFrame#CaptionCard#CaptionCardActive`——Qt 里
        `#A#B` 意为「祖先名 A 且自身名 B」，卡片之间互为兄弟不是祖孙，
        该选择器**从 v2.2.5 上线起从未命中**。更糟的是改名让基础规则
        `QFrame#CaptionCard`（卡面底色 + 边框）一并失效：像素实测
        聚焦卡采样 = #0f1115（窗口底色透出，卡片"没有脸"），
        未激活卡 = #161a22。既有集成锁只断言 objectName 字符串，
        把这个死机制当成了"正确行为"，所以 102 项全绿也照不出来。"""
        state = "active" if active else ("old" if not self.is_pending() else "")
        self.setProperty("state", state)
        self.style().unpolish(self)
        self.style().polish(self)
        # 子控件必须逐个重 polish：Qt 在父控件动态属性变化时只重排父自身，
        # 依赖父属性的**后代规则**（`QFrame#CaptionCard[state="old"]
        # QLabel#CaptionSource` 等文字渐隐）不会自动重算——实测 old=448 与
        # idle 同色即此因（像素锁 t_card_focus_style_pixels 抓到）
        for ch in (self.meta_label, self.source_label, self.target_label):
            ch.style().unpolish(ch)
            ch.style().polish(ch)


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
    返回 (文本, 有效条数)。

    v2.20.3：入参元素可以是 `CaptionCard`，也可以是 `CaptionCard.export_row()`
    的五元组——被 `max_history` 淘汰的卡片以快照形式留在导出台账里，导出不再
    只剩"最近 N 条"。"""
    rows = [c if isinstance(c, tuple) else _card_row(c) for c in cards]
    if fmt == "srt":
        cues = []
        for meta, source, target, t_start, dur_s in rows:
            # v2.20.4：不再"译文缺失就回退原文"。失败卡、被并入的前片卡、仍在
            # 等待的占位卡都因此混进 SRT——实测一次导出 3 条 cue 里两条是英文
            # 原文，而提示写着"已导出 3 条"，用户拿到的是半英半中的字幕。
            if _is_untranslated(target):
                continue
            text = target
            cues.append([t_start, dur_s, _srt_wrap(text)])
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
    for meta, source, target, _t0, _dur in rows:
        # v2.7.4（B-12）说"占位/失败卡不得混进导出"，旧实现只丢了译文行，
        # `[时间] + 原文` 照样写、条数照样加——失败一整场的会话仍导出 N 条。
        # 现在与 SRT 用同一过滤集，计数即真实可看的条数。
        if _is_untranslated(target):
            continue
        lines.append(f"[{meta}]")
        if source:
            lines.append(source)
        lines.append(target)
        lines.append("")
        n += 1
    return "\n".join(lines), n


def _card_row(card):
    """`CaptionCard.export_row()` 的兜底版：鸭子类型的假卡片（测试桩）也能导出。"""
    try:
        return card.export_row()
    except AttributeError:
        return (card.meta_label.text(), card.source_label.text(),
                card.target_label.text(),
                getattr(card, "t_start", None), getattr(card, "dur_s", None))

def _lcs_ratio(a, b):
    """两条文本按**字符**的有序公共占比：LCS 长度 ÷ 较短一条的长度（0~1）。

    为什么不用词：中日韩文本 `split()` 出来只有一个 token，任何词级判据都退化成
    "全等或全不等"。为什么不用集合：集合会把近义改写的两句误判成同一句
    （面板 v2.20.1 就栽过，见 `CaptionOverlay._dual_same_sentence` 的注释）。"""
    a = (a or "").strip()
    b = (b or "").strip()
    if not a or not b:
        return 0.0
    if len(a) * len(b) > 400000:          # 超长文本不抖 O(n·m)，按"不像同一句"处理
        return 0.0
    prev = [0] * (len(b) + 1)
    for ca in a:
        cur = [0]
        for k, cb in enumerate(b):
            cur.append(prev[k] + 1 if ca == cb else max(prev[k + 1], cur[k]))
        prev = cur
    return prev[-1] / min(len(a), len(b))


class MainWindow(QMainWindow):
    start_requested = Signal()

    def _asr_timing(self, duration):
        """v2.2.11：当前时刻的 (会话相对秒, 语音时长) 时间轴快照。"""
        t0 = getattr(self, "_session_t0", None)
        try:
            dur = max(0.6, float(str(duration)))
        except (TypeError, ValueError):
            dur = None
        # v2.20.4：`_session_t0` 每次开始翻译都归零，而卡片与导出台账**不会**被
        # 清掉——停止→开始后导出的 SRT 时间轴会倒回去（实测第 151 条 cue 落在
        # 0.0s，前一条在 596.0s），播放器直接丢帧/乱序。这里给每条盖上一场累计
        # 的偏移，让整份文件的时间轴始终单调。
        off = getattr(self, "_t_axis_offset", 0.0)
        return ((time.time() - t0 + off) if t0 else None), dur

    def __init__(self):
        super().__init__()
        self.config = Config()
        self.capture_thread = None
        self.asr_thread = None
        self.translate_thread = None
        self.running = False
        self.session_count = 0
        # v2.20.3：被 max_history 淘汰的卡片文本留在台账里供导出（见 export_row）
        self._export_ledger = []
        self.setWindowTitle(ui_text("LiveSubtitle · 实时字幕翻译"))
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
        # v2.7.4（QA-02 活体实锤）：向导会改 source/model/engine——
        # 完成后必须刷速览卡，否则仪表盘停在旧值（实测向导选 turbo，
        # 卡片仍显示 small(CPU)，直到下次保存设置才跟正）
        try:
            self._refresh_quick_panel()
        except Exception:
            pass

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
        subtitle = QLabel(ui_text("实时语音识别 · 自动语言检测 · 多语翻译（在线 / 离线）"))
        subtitle.setObjectName("HeaderSub")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()

        self.status_dot = QLabel()
        self.status_dot.setObjectName("StatusDot")
        self.status_dot.setFixedSize(14, 14)
        self.status_dot.setAlignment(Qt.AlignCenter)
        self.status_dot.setToolTip(ui_text("未启动：点击「开始翻译」开始"))
        self.status_text = QLabel(ui_text("未启动"))
        self.status_text.setObjectName("HeaderSub")
        header.addWidget(self.status_dot)
        header.addWidget(self.status_text)

        self.help_button = QPushButton(ui_text("使用说明"))
        self.help_button.setObjectName("GhostButton")
        self.help_button.setCursor(Qt.PointingHandCursor)
        self.help_button.setToolTip(ui_text("打开浏览器查看详细使用说明"))
        self.help_button.clicked.connect(self._open_docs)
        header.addWidget(self.help_button)

        self.settings_button = QPushButton(ui_text("设置"))
        self.settings_button.setObjectName("GhostButton")
        self.settings_button.setCursor(Qt.PointingHandCursor)
        self.settings_button.setFixedWidth(64)
        self.settings_button.setToolTip(ui_text("打开设置窗口"))
        self.settings_button.clicked.connect(self._open_settings)
        header.addWidget(self.settings_button)

        self.toggle_button = QPushButton(ui_text("开始翻译"))
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
        # v2.19.4：跟底状态做成**粘性标志**而不是当场比 value/maximum——字幕卡的
        # 高度要等布局与换行算完才落定（offscreen 实测滚动范围 0→130→794 分几步
        # 长起来），当场判定"是否贴底"必然误判：正常跟随会被记成"上滚了"、
        # 角标乱跳。改为只在用户真的把滚动条离开底部时取消跟随，并在滚动范围
        # 每次变化后重贴一次。
        self._follow_latest = True
        self._new_since_scroll = 0
        self.scroll.verticalScrollBar().valueChanged.connect(self._on_list_scrolled)
        self.scroll.verticalScrollBar().rangeChanged.connect(
            lambda _mn, _mx: self._settle_bottom())

        self.empty_hint = QLabel(
            ui_text("点击右上角「开始翻译」\n\n播放任意视频或说话，字幕将实时出现在这里\n\n"
            "系统声音模式可直接抓取网页视频 / 播放器 / 会议的声音")
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
        qtitle = QLabel(ui_text("当前配置"))
        qtitle.setObjectName("PanelTitle")
        qv.addWidget(qtitle)
        qgrid = _QGrid()
        qgrid.setHorizontalSpacing(14)
        qgrid.setVerticalSpacing(8)
        self._quick_labels = {}
        # v2.20.6：行名与显示文案解耦——此前中文字面量同时充当 `_quick_labels` 的
        # 键和 QLabel 显示文字，一旦显示文字走 i18n，按键回查就 KeyError。
        for i, (lkey, title) in enumerate((("model", ui_text("识别模型")),
                                           ("engine", ui_text("翻译引擎")),
                                           ("source", ui_text("音频来源")))):
            k = QLabel(title)
            k.setObjectName("PanelTitle")
            k.setMinimumHeight(18)  # v2.2.9：行高按字体下限给足，不裁字
            v = QLabel("—")
            v.setObjectName("EmptyHint")
            # v2.2.13：值标签不换行——热键已拆两行，无超长值；卡片按最长
            # 单行自适应（≤520 上限），从根上消除"word-wrap 高度不上传父
            # 布局导致裁字/重叠"这一类问题（v2.2.9 的 adjustSize 从未真正生效）
            v.setMinimumHeight(20)
            self._quick_labels[lkey] = v
            qgrid.addWidget(k, i, 0, Qt.AlignTop)
            qgrid.addWidget(v, i, 1, Qt.AlignTop)
        # v2.3.2（G1）：悬浮字幕条状态行——用户关了悬浮条后软件从不提醒，
        # "关了都忘了"是模拟用户报告的真实痛点；空页面仪表盘常驻显示状态与开启方法
        ov_key = QLabel(ui_text("字幕面板"))
        ov_key.setObjectName("PanelTitle")
        ov_key.setMinimumHeight(18)
        ov_val = QLabel("—")
        ov_val.setObjectName("EmptyHint")
        ov_val.setMinimumHeight(20)
        self._quick_labels["overlay"] = ov_val
        qgrid.addWidget(ov_key, 3, 0, Qt.AlignTop)
        qgrid.addWidget(ov_val, 3, 1, Qt.AlignTop)
        # v2.2.13（用户实拍"别扭"修正）：热键拆两行显示——单行拼接必换行，
        # 而 word-wrap 标签的换行高度不通知父布局，卡片高度冻结导致第二行
        # 被提示行压住（v2.2.9 的 adjustSize 修复实际从未生效）。两行短文本
        # 永不换行，从结构上消除该问题；"热键"键名跨两行居左对齐。
        hk_key = QLabel(ui_text("热键"))
        hk_key.setObjectName("PanelTitle")
        hk_key.setMinimumHeight(18)
        qgrid.addWidget(hk_key, 4, 0, 2, 1, Qt.AlignTop | Qt.AlignLeft)
        for r, lkey in ((4, "hotkey"), (5, "hotkey_o")):
            v = QLabel("—")
            v.setObjectName("EmptyHint")
            v.setMinimumHeight(20)
            self._quick_labels[lkey] = v
            qgrid.addWidget(v, r, 1, Qt.AlignTop)
        qv.addLayout(qgrid)
        qtip = QLabel(ui_text("提示：托盘图标右键可显隐字幕面板、快速切换输入来源；"
                     "热键可在「设置-通用」修改"))
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
        self.engine_status_label = QLabel(ui_text("引擎：待启动"))
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
        # 空页面指引让用户"看音量条有无波动"判断有没有抓到声音，可这根条
        # 全应用无名无 tooltip（v2.19.4）
        self.level_bar.setToolTip(ui_text("实时输入音量（开始翻译后无波动 = 没抓到声音，"
                                  "请到「设置-音频输入」更换设备）"))
        status.addPermanentWidget(self.level_bar)

        # v2.19.4：回看提示——用户上滚读旧句时，新句不再把他拽回底部（面板早就是
        # 这套判据，主窗此前无条件 setValue(maximum)），改为挂一个可点的计数角标。
        self.jump_new_button = QPushButton(ui_text("↓ 新字幕"))
        self.jump_new_button.setObjectName("GhostButton")
        self.jump_new_button.setCursor(Qt.PointingHandCursor)
        self.jump_new_button.setToolTip(ui_text("你正在回看旧字幕，点此跳回最新一条"))
        self.jump_new_button.clicked.connect(self._jump_to_latest)
        self.jump_new_button.hide()
        status.addPermanentWidget(self.jump_new_button)

        self.clear_button = QPushButton(ui_text("清空"))
        self.clear_button.setObjectName("GhostButton")
        self.clear_button.setCursor(Qt.PointingHandCursor)
        self.clear_button.setToolTip(ui_text("清空当前会话的字幕记录"))
        self.clear_button.clicked.connect(self._clear_captions)
        self.clear_button.setEnabled(False)  # v2.6.5（R7-L3）：无字幕时禁用，有卡再启用
        status.addPermanentWidget(self.clear_button)

        self.export_button = QPushButton(ui_text("导出"))
        self.export_button.setObjectName("GhostButton")
        self.export_button.setCursor(Qt.PointingHandCursor)
        self.export_button.setToolTip(ui_text("把当前会话的双语字幕导出为文本文件"))
        self.export_button.clicked.connect(self._export_captions)
        self.export_button.setEnabled(False)  # v2.6.5（R7-L3）：同上
        status.addPermanentWidget(self.export_button)

        self.session_label = QLabel(ui_text("本次会话：0 条"))
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
                                       on_opacity=self._on_panel_opacity,
                                        on_height_changed=self._on_panel_height,
                                        # v2.11.0：面板 ⋯ 菜单切换布局 → 回调落盘
                                        on_layout_changed=self._on_panel_layout_changed,
                                        # v2.16.0：分割线拖拽 → 原文区高度落盘
                                        on_dual_split=self._on_panel_dual_split,
                                        # v2.20.0：⋯ 菜单切换攒句 → 落盘
                                        on_grouping_toggled=self._on_panel_grouping_toggled,
                                        # v2.20.1：面板上的开始/停止把手
                                        on_toggle_running=self.toggle_running)
        self.overlay.hide()
        self._build_tray()
        self._install_global_hotkey()
        self._maybe_prewarm()
        # v2.10.0：悬浮条启动即常驻（主逻辑在 _load_settings 的无条件 show）——
        # 此处保留兜底，防构建顺序变更时"常驻"承诺失效
        if not self.overlay.isVisible():
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
                                          str(self.config.get("asr_device")), self,
                                          turbo=bool(self.config.get("perf_turbo")))
            self._prewarm.start()
        except Exception:
            pass

    def _stop_prewarm(self):
        """v2.6.1（P0-2）：退出前收预热线程——此前 _prewarm 游离在
        stop_pipeline 的孤儿机制外（其只收三个管线线程），预热中退出时
        MainWindow 析构会销毁运行中的 QThread → qFatal 崩溃
        （prewarm_model 默认开，启动后立刻退出是高频路径）。"""
        pw = getattr(self, "_prewarm", None)
        if pw is None:
            return
        self._prewarm = None
        try:
            if not pw.isRunning():
                return
            pw.request_stop()
            pw.wait(500)
            if pw.isRunning():
                # 已进入模型构造（不可中断）→ 移交孤儿容器：摘除 parent
                # 保引用，finished 后 deleteLater 自清理（与 stop_pipeline
                # 的孤儿模式一致），MainWindow 析构不再触及它
                pw.setParent(None)
                _orphan_threads().append(pw)
                pw.finished.connect(pw.deleteLater)
                from app import log as app_log
                app_log.log("asr.prewarm_orphaned")
        except RuntimeError:
            # C++ 对象已销毁（预热已结束），无需处理
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
                # v2.18.1：原调 dlg.reload_values()——该方法不存在，异常被
                # except 吞掉，设置页目标语言从未跟随面板同步
                dlg.sync_target_lang(code)
            except Exception:
                pass
        if self.running:
            self._set_alert(f"{ui_text('目标语言已切换为 ')}{code}{ui_text('，重启翻译后对新字幕生效')}")

    def _on_panel_font_size(self, px):
        self.config.set("overlay_font_size", int(px))
        self.apply_overlay_from_config()
        self._sync_settings_overlay(overlay_font_size=int(px))

    def _sync_settings_overlay(self, **values):
        """v2.19.2：面板侧改了外观键 → 同步**已打开**的设置页控件。

        旧状只有 overlay_enabled / target_lang 有窄同步，字号/透明度/布局/
        历史区四项漏网：面板切完设置页仍显旧值，用户把控件拨到实际值时被
        `_stage` 判成"改回原值"静默吞掉。设置页未打开时什么都不做。"""
        dlg = getattr(self, "_settings_dlg", None)
        if dlg is None:
            return
        try:
            dlg.sync_overlay_keys(values)
        except Exception:
            pass

    def _on_panel_height(self, h):
        """v2.5.3：面板手动高度落盘（拖底缘/恢复自动均经此）。"""
        self.config.set("overlay_h", int(h or 0))

    def _on_panel_opacity(self, val):
        """v2.5.0：面板滚轮/菜单档调透明度——落盘并本地重放（细调仍走设置页）。"""
        self.config.set("overlay_bg_opacity", int(val))
        self.overlay.set_bg_opacity(int(val))   # v2.19.2：换算收口在面板侧
        self._sync_settings_overlay(overlay_bg_opacity=int(val))

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
            # v2.20.2：回写设置页勾选必须**先屏蔽信号**。这个勾选框是
            # toggled→_stage 的，裸 setChecked 会自己塞一条暂存、又被 _stage 的
            # "改回原值"分支 pop 掉——用户尚未保存的「同时显示原文」改动被静默丢弃，
            # 「保存并应用」按钮重新置灰（同族的 sync_target_lang / sync_overlay_keys
            # 都屏蔽了信号，只有这条漏网）。
            _w = dlg.show_source_check
            _w.blockSignals(True)
            _w.setChecked(bool(show_source))
            _w.blockSignals(False)

    # ---------- 全局热键 ----------

    def _install_global_hotkey(self):
        """安装原生事件过滤器并按当前配置注册热键（进程生命周期内一次过滤器）。"""
        hotkey.install(QApplication.instance(), self.toggle_running)
        hotkey.install_overlay(QApplication.instance(), self._toggle_overlay_hotkey)
        status = self.apply_hotkey_config()
        # v2.0.0：启动时注册失败不再静默（组合被占用/不支持时用户毫无感知）
        if status.startswith("✗"):
            self.tray.showMessage(ui_text("LiveSubtitle 全局热键"), status[2:], QSystemTrayIcon.Warning, 4000)

    def _toggle_overlay_hotkey(self):
        """显隐字幕面板——全局热键与托盘右键菜单**共用**这一条路径（v2.2.6 起）。

        v2.20.1（用户裁决）：面板改**常驻实时显示**，设置页的「启用字幕面板」
        勾选与 `overlay_enabled` 配置键一并删除；此后开/关只有两个入口——热键
        （默认 Ctrl+Alt+O）与托盘右键菜单「显隐字幕面板」，外加面板自身的 ✕。
        显隐**不落盘**：只在本次运行内有效，下次启动一律恢复显示（v2.10.0 起
        就是这个语义，本轮把残留的持久键清掉）。250ms 业务级防抖与
        `toggle_running` 同款（双保险，防热键重复投递）。"""
        import time as _t
        now = _t.monotonic()
        if now - getattr(self, "_overlay_hk_last", 0.0) < 0.25:
            return
        self._overlay_hk_last = now
        if self.overlay.isVisible():
            self.overlay.hide()      # 与面板 ✕ 同路径（on_overlay_closed 收尾）
            self.on_overlay_closed()
        else:
            self.overlay.show()
            self.update_overlay_status()
            self._refresh_quick_panel()  # v2.3.2（G1）：仪表盘同步悬浮条状态

    def apply_hotkey_config(self):
        """按配置注册/注销全局热键；返回给设置页展示的状态文本。"""
        c = self.config
        hotkey.unregister()
        if not c.get("hotkey_enabled"):
            self._update_tray_hotkey_text("")
            return ui_text("全局热键已关闭")
        seq = str(c.get("hotkey_sequence") or "Ctrl+Alt+S")
        ok = hotkey.register(int(self.winId()), seq)
        # v2.2.6：显隐悬浮条热键（默认 Ctrl+Alt+O，可留空禁用）
        # v2.2.7：注册失败不再静默——组合被占用时用户按键"毫无反应"即 BUG 观感，
        # 必须托盘气泡 + 设置页状态行双重提示
        oseq = str(c.get("hotkey_overlay") or "").strip()
        if oseq:
            if oseq.upper() == seq.upper():
                self._set_alert(ui_text("⚠ 悬浮条显隐热键与开始/停止热键相同，已忽略——请在「设置-通用」改键"))
            elif not hotkey.register_overlay(int(self.winId()), oseq):
                self.tray.showMessage(
                    ui_text("LiveSubtitle 全局热键"),
                    ui_fmt("悬浮条显隐热键 {seq} 注册失败（已被其他程序占用或不被支持），"
                           "该热键未生效——请在「设置-通用」换一个组合。", seq=oseq),
                    QSystemTrayIcon.Warning, 5000)
        self._update_tray_hotkey_text(seq if ok else "")
        base = (ui_fmt("✓ 全局热键 {seq} 已生效（托盘菜单同步显示）", seq=seq) if ok
                else ui_fmt("✗ 热键 {seq} 注册失败：组合不被支持或已被其他程序占用，"
                            "请在「设置-通用」换一个组合", seq=seq))
        # v2.2.7：悬浮条显隐热键注册结果同样透传到设置页状态行
        if oseq and oseq.upper() != seq.upper() and not hotkey.overlay_text():
            base += "\n" + ui_fmt("✗ 悬浮条显隐热键 {seq} 注册失败：已被其他程序占用"
                                  "或组合不受支持，请换一个组合或清空禁用", seq=oseq)
        # v2.2.11：注册状态变化后同步速览卡（否则启动早期刷新会停留在旧值）
        self._refresh_quick_panel()
        return base

    def _update_tray_hotkey_text(self, seq):
        act = getattr(self, "_tray_toggle_action", None)
        if act is not None:
            act.setText(f"{ui_text('开始 / 停止翻译（')}{seq}）" if seq else ui_text("开始 / 停止翻译"))

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
            # v2.6.2（P1-5）：stop_pipeline 返回前已把线程引用置 None，旧的
            # `for t in (self.capture_thread, ...)` 循环拿到全 None——"等旧
            # 线程退出"承诺从未兑现，切源时新旧 CaptureThread 并存抢音频
            # 设备、新旧 AsrThread 并发加载双份模型。改为停止前快照引用再等
            old = [t for t in (self.capture_thread, self.asr_thread,
                               self.translate_thread) if t is not None]
            self.stop_pipeline()
            for t in old:
                try:
                    if t.isRunning():
                        t.wait(1500)
                except RuntimeError:
                    pass   # C++ 对象已销毁（线程早已终结）
            self.start_pipeline()
        name = ui_text("麦克风") if new == "microphone" else ui_text("系统声音")
        self._set_engine_status(f"{ui_text('已切换输入来源：')}{name}")
        # v2.19.4：托盘菜单里切来源时主窗往往是隐藏的，那行状态字看不见；
        # 而「当前配置」速览卡的"音频来源"也停在旧值（同 v2.7.4 QA-02 那类谎报）。
        self._refresh_quick_panel()
        self.update_overlay_status()
        if not self.isVisible() and self.tray is not None:
            try:
                self.tray.showMessage("LiveSubtitle", f"{ui_text('输入来源已切换：')}{name}",
                                      QSystemTrayIcon.Information, 2500)
            except Exception:
                pass

    def update_overlay_status(self):
        """把运行状态/来源/引擎/模型同步到悬浮条状态行。"""
        if not hasattr(self, "overlay"):
            return
        # v2.20.1：面板「开始 / 停止翻译」把手跟着真态走——热键、托盘、主窗按钮
        # 三条路径都改得动 running，面板自己翻转就会与真态不一致
        self.overlay.set_running(bool(self.running))
        if getattr(self, "_muted_warn", False):
            # v2.20.3：面板状态行只有 10 字符的额度（`set_status` 截断），长句一律
            # 被腰斩成"系统静音中 · 不会…"这种半截话。这里只放状态词，完整解释在
            # 主窗状态栏与横幅里。
            self.overlay.set_status(ui_text("系统静音中"), is_error=True)
            return
        if getattr(self, "_low_input_warn", False):
            self.overlay.set_status(ui_text("信号弱"), is_error=True)
            return
        if not self.running:
            self.overlay.set_status(ui_text("已停止 · 待机中"))
            return
        src = ui_text("麦克风") if self.config.get("source_type") == "microphone" else ui_text("系统声音")
        model = self.config.get("asr_model")
        eng = getattr(self, "_last_engine_name", "") or ui_text("自动")
        # v2.20.3：面板状态行 `set_status` 只留 10 个字符（长提示截成"运行中 · 系统声…"），
        # 引擎与模型名**从来没能看见过**——那串长文本是给主窗状态栏写的。面板这里
        # 只报"在不在跑、听的是哪路声音"，详情归主窗与设置页。
        self.overlay.set_status(f"{ui_text('运行中 · ')}{src}")
        self._engine_status_detail = f"{ui_text('运行中 · ')}{src} · {eng} · {model}{ui_text(' 模型')}"

    def set_overlay_caption_error(self, failed):
        """字幕翻译失败时让状态行变橙红提醒。"""
        if hasattr(self, "overlay"):
            self.overlay.set_status(ui_text("翻译失败 · 检查网络或切换引擎"), is_error=failed)
            if not failed:
                self.update_overlay_status()

    def _build_tray(self):
        self.tray = QSystemTrayIcon(QIcon(str(icon_path())), self)
        self.tray.setToolTip(ui_text("LiveSubtitle · 实时字幕翻译"))
        menu = QMenu(self)
        act_show = QAction(ui_text("显示主窗口"), self)
        act_show.triggered.connect(self._restore_window)
        act_toggle = QAction(ui_text("开始 / 停止翻译"), self)
        act_toggle.triggered.connect(self.toggle_running)
        # v2.2.8：速览卡承诺过的托盘快捷操作补齐（此前文案撒谎）
        act_overlay = QAction(ui_text("显隐字幕面板"), self)
        act_overlay.triggered.connect(self._toggle_overlay_hotkey)
        self._tray_overlay_action = act_overlay
        act_source = QAction(ui_text("切换输入来源"), self)
        act_source.triggered.connect(self._toggle_source)
        act_settings = QAction(ui_text("打开设置…"), self)
        act_settings.triggered.connect(self._open_settings)
        act_quit = QAction(ui_text("退出"), self)
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
        # v2.10.0 立、v2.20.1 收尾：字幕面板**启动即常驻实时显示**（用户裁决）。
        # 显隐只由热键 / 托盘右键菜单 / 面板 ✕ 决定，且**不跨会话记忆**——
        # `overlay_enabled` 与设置页勾选已随之删除，这里不再回写任何开关值。
        # 必须先 show 再刷仪表盘——速览卡按 isVisible() 取文案（顺序反了会
        # 显示"已关闭"谎报常驻实况）
        self.overlay.show()
        # v2.20.2：显示即回灌真态——面板把手/状态行在启动那一刻还没人同步过
        # （只有 start/stop 与显隐热键会调），于是"没开始翻译"的面板写着
        # 绿色「⏸ 暂停」。
        self.update_overlay_status()
        self._refresh_quick_panel()

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
        elif getattr(dlg, "_staged", None):
            # v2.7.4（B-4）：对话框开着且有未保存改动时重入（面板⋯菜单"打开设置"、
            # 双击快捷键等都会走这里）——无条件 reload 会静默吞掉暂存改动并谎报
            # "所有改动已保存"；closeEvent 有 _confirm_discard 守卫，重入此前没有。
            # 现只把窗口带到前台，不碰内容
            pass
        else:
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
        engine_names = {"google": ui_text("Google（在线）"), "mymemory": ui_text("MyMemory（在线）"),
                        "argos": ui_text("离线翻译包（直译）"), "auto": ui_text("自动（在线优先，失败切离线）")}
        eng = engine_names.get(c.get("engine"), str(c.get("engine")))
        src = ui_text("系统声音") if c.get("source_type") == "system" else ui_text("麦克风")
        hk_cfg = str(c.get("hotkey_sequence") or "Ctrl+Alt+S")
        # v2.2.6：显隐悬浮条热键同步显示
        oseq_cfg = str(c.get("hotkey_overlay") or "").strip()
        labels["model"].setText(f"{model}（{'GPU' if self._quick_gpu_hint() else 'CPU'}）")
        labels["engine"].setText(eng)
        labels["source"].setText(src)
        # v2.3.2（G1）：悬浮条状态常驻仪表盘——关闭时明说怎么再打开
        # （文案刻意短：值列不换行，长句会撑爆卡片 520px 上限）
        if self.overlay.isVisible():
            labels["overlay"].setText(ui_text("已开启（可拖动位置）"))
            labels["overlay"].setStyleSheet("")
        else:
            # v2.7.4（B-9）：不再硬编码 Ctrl+Alt+O——改键/禁用/注册失败时谎报指引；
            # 用配置真值 + 注册实况组合文案（文案不许承诺做不到的事）
            o_cfg = str(c.get("hotkey_overlay") or "").strip()
            if not o_cfg:
                # v2.20.1：设置页「启用字幕面板」勾选已删——指引改指托盘右键菜单
                labels["overlay"].setText(ui_text("已关闭 · 托盘右键可重新显示"))
            elif hotkey.overlay_text():
                labels["overlay"].setText(f"{ui_text('已关闭 · 按 ')}{o_cfg}{ui_text(' 打开')}")
            else:
                labels["overlay"].setText(f"{ui_text('已关闭 · 显隐热键未生效（可在设置-通用改键）')}")
            labels["overlay"].setStyleSheet("color: #fbbf24;")
        # v2.2.11：热键行以“实际注册成功”为准显示——配置了但被占用未注册时
        # 标红“（未生效）”，不再拿配置值谎称可用（文案不许承诺做不到的事）
        hk_live = hotkey.current_text()
        o_live = hotkey.overlay_text()
        failed = False
        hk_failed = o_failed = False
        if not c.get("hotkey_enabled"):
            hk_disp, o_disp = ui_text("全局热键已关闭（设置-通用）"), ""
        else:
            hk_failed = not hk_live
            o_failed = bool(oseq_cfg) and not o_live
            failed = hk_failed or o_failed
            hk_disp = (f"{hk_cfg}{ui_text(' 开始/停止（未生效）')}" if hk_failed
                       else f"{hk_live}{ui_text(' 开始/停止')}")
            if not oseq_cfg:
                o_disp = ui_text("未设 显隐悬浮条")
            elif o_failed:
                o_disp = f"{oseq_cfg}{ui_text(' 显隐悬浮条（未生效）')}"
            else:
                o_disp = f"{o_live}{ui_text(' 显隐悬浮条')}"
        # v2.2.13：两行分别落位；哪行未生效哪行标红（不再拼接换行）
        labels["hotkey"].setText(hk_disp)
        labels["hotkey_o"].setText(o_disp)
        labels["hotkey"].setStyleSheet("color: #ff8a5c;"
                                    if (failed and hk_failed) else "")
        labels["hotkey_o"].setStyleSheet("color: #ff8a5c;"
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
        """切换面板显隐。v2.20.1：面板改常驻实时显示后这是唯一的显隐原语，
        **不落盘**（`overlay_enabled` 已删）——热键/托盘菜单/✕ 都经它或直接经
        `overlay.show()/hide()`，下次启动一律恢复显示。"""
        if checked:
            x, y = self._clamp_overlay_pos(self.config.get("overlay_x"),
                                           self.config.get("overlay_y"))
            self.overlay.move(x, y)
            self.overlay.show()
        else:
            self.overlay.hide()
        self._refresh_quick_panel()  # v2.3.2（G1）

    def _on_panel_layout_changed(self, mode):
        """v2.11.0：面板 ⋯ 菜单切换布局 → 同步 overlay（幂等）+ 落盘 overlay_layout
        （overlay 组键，即时生效；运行中切换即切即用，无需重启管线）。
        v2.13.0：切换后按新布局热启/暂停流式预览通道。"""
        m = "dual" if str(mode) == "dual" else "list"
        self.overlay.set_layout_mode(m)      # 幂等：面板内部切换后此为 no-op
        self.config.set("overlay_layout", m)
        self._sync_settings_overlay(overlay_layout=m)
        self._maybe_start_stream_preview()

    def _on_panel_dual_split(self, mode, value):
        """v2.16.0：分割线拖拽结束 → 原文区高度落盘 overlay_dual_src_h
        （0=回自动贴内容；internal 键，恢复默认归零）。
        v2.18.0：历史区分割交互已移除（历史自动吃剩余），仅 src 一种。"""
        try:
            v = int(value or 0)
        except (TypeError, ValueError):
            v = 0
        self.config.set("overlay_dual_src_h", v)

    def _on_panel_grouping_toggled(self, on):
        """v2.20.0：面板 ⋯ 菜单切「攒句合并」→ 落盘 translate_grouping。
        幂等双保险：面板已就地生效（勾选态），这里只写配置、把状态再同步一次，
        并推给**已打开**的设置页勾选（与 _on_panel_layout_changed 同一套路）。
        攒句判据是 `_grouping_enabled()` 逐片段实时读配置，无需重启管线。"""
        on = bool(on)
        self.config.set("translate_grouping", on)
        self.overlay.set_grouping_enabled(on)
        self._sync_settings_overlay(translate_grouping=on)
        # 即时生效的非错误反馈 → 走状态行（`_set_alert` 是常驻橙黄横幅，
        # 一次随手切换不该在主窗挂一条"警告"）
        self._set_engine_status(ui_text("攒句合并已") + (ui_text("开启：攒成整句再翻译，译文更连贯")
                                                if on else
                                                ui_text("关闭：每个识别片段一到就送翻译，观感最实时")))

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
        # v2.11.0：面板布局（list=历史列表 / dual=上下双语）随配置恢复——
        # **必须最先**（后续分割高度恢复与 _relayout 都依赖布局模式）
        self.overlay.set_layout_mode(str(c.get("overlay_layout") or "list"))
        # v2.20.0：dual 只剩一种形态（上原文 / 可拖分割线 / 下译文），
        # overlay_dual_hist 键随历史区一并删除（旧配置里的该键由 Config.load
        # 的"只认 DEFAULTS 键"规则自然丢弃）
        # v2.16.0：原文区高度（拖原文/译文分割线）随配置恢复（0=自动）
        self.overlay.set_dual_src_h_user(int(c.get("overlay_dual_src_h") or 0))
        # v2.20.0：⋯ 菜单「攒句合并」勾选态跟随配置
        self.overlay.set_grouping_enabled(bool(c.get("translate_grouping")))
        self._maybe_start_stream_preview()
        # 缺键由 Config.load 按 DEFAULTS 合并补齐，这里不再传默认值
        self.overlay.set_pinned(bool(c.get("overlay_pin")))
        self.overlay.set_collapsed(bool(c.get("overlay_collapsed")))
        self.overlay.set_user_height(int(c.get("overlay_h") or 0))

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
        """面板 ✕ / 热键隐藏的收尾。

        v2.20.1：不再写 `overlay_enabled`、不再同步设置页勾选（该开关已随
        "面板常驻实时显示"删除）——隐藏只在本次运行内有效，重新打开走热键或
        托盘右键菜单，下次启动一律恢复显示。位置照旧落盘。"""
        if getattr(self, "_quitting", False):
            return
        self._save_settings()
        self._refresh_quick_panel()  # v2.3.2（G1）

    def _follow_bottom(self):
        """主窗字幕列表的跟底判据（v2.19.4，与悬浮面板同一套语义）。

        直播场景下用户常上滚重读刚说过的一句；旧实现每来一张卡就无条件
        `setValue(maximum)`，2~6 秒后新片段把他拽回底部，回看根本完不成——
        而悬浮面板早有 `_follow` 守卫 + 「↓ 最新」按钮，主窗
        却没有（两入口行为不一致）。现在只在已贴底时跟底，否则累计条数挂角标。
        """
        sb = self.scroll.verticalScrollBar()
        if self._follow_latest:
            self._settle_bottom()
            QTimer.singleShot(0, self._settle_bottom)   # 布局落定后再贴一次
            return True
        self._new_since_scroll += 1
        self._sync_new_badge()
        return False

    def _on_list_scrolled(self, value):
        sb = self.scroll.verticalScrollBar()
        self._follow_latest = value >= sb.maximum() - 4
        if self._follow_latest:
            self._new_since_scroll = 0
        self._sync_new_badge()

    def _settle_bottom(self):
        if not getattr(self, "_follow_latest", True):
            return                      # 用户在上轮回看，不拽回
        sb = self.scroll.verticalScrollBar()
        if sb.value() < sb.maximum():
            sb.setValue(sb.maximum())

    def _sync_new_badge(self):
        n = getattr(self, "_new_since_scroll", 0)
        if n <= 0:
            self.jump_new_button.hide()
            return
        self.jump_new_button.setText(ui_text("↓ %d 条新字幕") % n)
        self.jump_new_button.show()

    def _jump_to_latest(self):
        self._new_since_scroll = 0
        self._sync_new_badge()
        sb = self.scroll.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _clear_captions(self):
        while self.scroll_layout.count() > 1:
            item = self.scroll_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self.session_count = 0
        self.session_label.setText(ui_text("本次会话：0 条"))
        # v2.20.3：主窗「清空」以前只清主窗卡片——面板上还留着全部字幕，用户以为
        # 没清掉；而卡片是这场字幕的唯一副本，清掉就没了。台账一并清空，保持
        # "清空 = 这场真的作废" 的语义。
        self._export_ledger = []
        try:
            self.overlay.clear_caption()
        except Exception:
            pass
        self.stack.setCurrentIndex(0)
        # v2.6.1（P0-1）：在途状态随卡片一起清——此前运行中清空只删卡片，
        # 迟到译文经 _pending 配对打到已删 C++ 对象上 → qFatal 崩溃
        # （PySide6>=6.6 槽内未处理异常直接终止进程）。清理项与 stop_pipeline
        # 对齐（_tgroup 攒句、_tgroup_by_src/_submit_ts 配对簿记）
        if getattr(self, "_pending", None):
            self._pending.clear()
        self._tgroup = []
        if getattr(self, "_tgroup_by_src", None):
            self._tgroup_by_src.clear()
        self._submit_ts = getattr(self, "_submit_ts", {})
        self._submit_ts.clear()
        # v2.7.6（A）：推测式簿记同步清——否则清空后仍有中间版回复来更新
        # 已删卡片（与 v2.6.1 P0-1 同源风险：迟到译文打到已删 C++ 对象）
        if getattr(self, "_spec_inflight", None):
            self._spec_inflight.clear()
        if getattr(self, "_spec_ts", None):
            self._spec_ts.clear()
        tg = getattr(self, "_tgroup_timer", None)
        if tg is not None:
            tg.stop()
        self._sync_export_actions()  # v2.6.5（R7-L3）：清空后回禁用态

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
        # v2.20.3：台账（已被历史上限淘汰的旧卡快照）排在前面，导出=整场而非最近 N 条
        cards = list(getattr(self, "_export_ledger", [])) + cards
        if not cards:
            QMessageBox.information(self, ui_text("导出字幕"), ui_text("当前会话还没有可导出的字幕。"))
            return
        ext = "srt" if fmt == "srt" else "txt"
        default_name = f"LiveSubtitle_{datetime.now():%Y%m%d_%H%M%S}.{ext}"
        # 默认落到用户文档目录：安装目录（Program Files）对标准权限用户不可写
        docs = QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation) or str(Path.home())
        # v2.4.4（BUG-6）：默认过滤器与菜单入口一致——SRT 入口首选 SRT
        filt = (ui_text("SRT 字幕 (*.srt);;文本文件 (*.txt);;所有文件 (*)") if fmt == "srt"
                else ui_text("文本文件 (*.txt);;SRT 字幕 (*.srt);;所有文件 (*)"))
        path, _ = QFileDialog.getSaveFileName(
            self, ui_text("导出字幕"), str(Path(docs) / default_name), filt)
        if not path:
            return
        # v2.2.11：按扩展名选格式——.srt 生成带时间轴的标准字幕（播放器/剪映
        # 可直接加载）；其余维持历史纯文本格式
        fmt = "srt" if str(path).lower().endswith(".srt") else "txt"
        content, count = build_export_text(cards, fmt)
        if fmt == "srt" and count == 0:
            QMessageBox.information(self, ui_text("导出字幕"), ui_text("没有已完成的字幕可导出为 SRT。"))
            return
        try:
            # v2.20.4（P0，实测）：`Path.write_text` 是文本模式 open("w")——
            # **打开即截断**，编码或写盘一失败（卡片里混进孤立代理对、磁盘满、
            # 目标在已被弹出的移动盘上），用户选的那个已有文件就变成 0 字节空壳，
            # 界面上只留一句"写入文件失败"。改成：先内存编码 → 写 .tmp → fsync →
            # os.replace，任何一步失败都不碰原文件（与 config.save 同一套）。
            data = content.encode("utf-8", "replace")
            import os as _os
            tmp = str(path) + ".tmp"
            with open(tmp, "wb") as fp:
                fp.write(data)
                fp.flush()
                _os.fsync(fp.fileno())
            _os.replace(tmp, path)
        except Exception as e:
            QMessageBox.warning(self, ui_text("导出字幕"), f"{ui_text('写入文件失败：')}{e}")
            return
        # v2.4.4（BUG-8）：完成提示路径规范化为 Windows 反斜杠——QFileDialog
        # 返回正斜杠路径，用户复制到资源管理器打不开
        QMessageBox.information(
            self, ui_text("导出字幕"),
            ui_fmt("已导出 {n} 条字幕到：\n{path}", n=count, path=os.path.normpath(path)))

    def toggle_running(self):
        # v2.0.4：热键连按防抖——界面被 stop_pipeline 短暂阻塞时按下的热键
        # 会在事件队列里排队，恢复后被逐条当作 toggle 处理，造成 start/stop
        # 毫秒级反复翻转（实机日志实证：pipeline.start 后 4~6ms 即 pipeline.stop），
        # 最终停在"运行"档、加载线程成孤儿，状态栏被迟到的加载消息永久覆盖
        now = time.monotonic()
        if now - getattr(self, "_last_toggle_at", 0.0) < 0.25:
            return
        # v2.7.4（A-3）：teardown 阻塞期排队的热键不得在退出前把管线重拉起来
        # （重拉的线程逃过孤儿清理，随进程终结被硬杀）
        if getattr(self, "_quitting", False):
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
                    ui_text("已停止翻译") if was_running else ui_text("已开始翻译（首次使用会先下载模型）"),
                    QSystemTrayIcon.Information, 2000)

    def start_pipeline(self):
        if self.running or getattr(self, "_quitting", False):
            # v2.7.4（A-3）：_quitting 守卫同 toggle_running
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
        self._lat_reco, self._lat_tr, self._lat_hold, self._submit_ts = [], [], [], {}
        # v2.7.6（A）：推测式增量翻译的簿记随会话清零——gen 递增使上一会话
        # 迟到的推测回复自动失效，_spec_inflight 不跨会话残留
        self._tgroup_gen = getattr(self, "_tgroup_gen", 0) + 1
        self._spec_inflight = {}
        self._lat_spec = []
        # v2.12.0/v2.14.0：dual 流式原文状态随会话清零
        self._dual_base = ""
        self._dual_current = ""
        self._dual_last_piece = ""
        self._dual_draft = None          # v2.13.0：在飞草稿译文一并作废
        # v2.7.2：榨干模式——翻译运行期提升进程优先级（停止后恢复），
        # 让采集/转写线程在系统负载下不被普通进程抢时间片
        self._apply_process_priority(True)
        self.toggle_button.setText(ui_text("停止翻译"))
        self.toggle_button.setObjectName("StopButton")
        self.toggle_button.style().unpolish(self.toggle_button)
        self.toggle_button.style().polish(self.toggle_button)
        self.status_dot.setStyleSheet("background-color: #2ecc71; border-radius: 7px;")
        self.status_dot.setToolTip(ui_text("运行中：正在识别并翻译"))
        self.status_text.setText(ui_text("运行中"))
        self.stack.setCurrentIndex(1)
        self.session_count = 0
        self._last_asr_lang = ""      # v2.18.2（D-3）：会话级语言记忆随新会话清零

        c = self.config
        engine = c.get("engine")
        # v2.6.0：注入全词匹配开关与离线质量档快照（运行中可经 apply_pipeline_hotfix 热更）
        # v2.7.5（R-3）：expected_src=识别侧配置语言，离线包预载只装该方向
        self.translate_thread = TranslateThread(engine, c.get("target_lang"), self,
                                                translate_fix_map=dict(c.get("translate_fix_map") or {}),
                                                fix_whole_word=bool(c.get("fix_whole_word")),
                                                offline_quality=str(c.get("offline_quality") or "high"),
                                                auto_fallback=bool(c.get("engine_auto_fallback")),
                                                expected_src=str(c.get("asr_language") or "auto"))
        self.translate_thread.result_ready.connect(self._on_translated)
        # v2.7.6（A）：推测式中间版译文走独立信号（终版通路零改动）
        self.translate_thread.spec_result_ready.connect(self._on_spec_translated)
        # v2.0.4：状态改走带守卫的槽——lambda 无 running 守卫，停止后已入队的
        # 迟到状态（如孤儿加载线程的"正在加载模型"）会覆盖"已停止"
        self.translate_thread.status_changed.connect(self._on_translate_status)
        # v2.20.6（i18n 前置改造）：主引擎恢复改信号驱动
        self.translate_thread.primary_recovered.connect(self._on_primary_recovered)
        # v2.3.2（G2）：在线引擎启动即不可达的事前横幅
        self.translate_thread.engine_fallback.connect(self._on_engine_fallback)
        self.translate_thread.start()
        self._sid_tr = self.translate_thread   # v2.6.2（P1-6）：会话身份引用

        self._asr_ready = False  # v2.0.4：模型加载期停止时缩短等待（见 stop_pipeline）
        # v2.20.4：新会话开始，把上一场已经走过的秒数垫进时间轴偏移（见 _asr_timing）
        _prev_t0 = getattr(self, "_session_t0", None)
        if _prev_t0:
            self._t_axis_offset = getattr(self, "_t_axis_offset", 0.0) +                 max(0.0, time.time() - _prev_t0)
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
            accuracy=str(c.get("asr_accuracy") or "fast"),
            mishear_whole_word=bool(c.get("fix_whole_word")),
            hotwords=str(c.get("asr_hotwords") or ""),
            lang_recheck=bool(c.get("lang_recheck")),
            turbo=bool(c.get("perf_turbo")),
        )
        self.asr_thread.text_ready.connect(self._on_asr_text)
        self.asr_thread.status_changed.connect(self._on_asr_status)
        # v2.20.6（i18n 前置改造）：积压预警改信号驱动，不再 from 状态文本猜中文
        self.asr_thread.backlog.connect(self._on_asr_backlog)
        self.asr_thread.error_occurred.connect(self._on_pipeline_error)
        self.asr_thread.model_ready.connect(self._on_model_ready)
        self.asr_thread.recheck_dropped.connect(self._on_recheck_dropped)
        # v2.7.3：识别线程退场（含排水）→ 冲刷攒句+关闭翻译输入门（治"每次停止必孤儿"）
        self.asr_thread.finished.connect(self._on_asr_finished)
        self.asr_thread.start()
        self._sid_asr = self.asr_thread   # v2.6.2（P1-6）：会话身份引用

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
            # v2.7.2：榨干模式——连续语流强制切段上限 6s→4s
            turbo=bool(c.get("perf_turbo")),
            # v2.7.6（C）：分段上限独立可调（>0 覆盖模式内置值）——句长超过上限
            # 即被强制切段，hold_p50 实测恒等于该周期（译文迟到的直接来源）
            cap_s=c.get("segment_cap_s"),
            # v2.9.0：神经 VAD 实验开关（默认关）——判定逻辑与实测留档
            # 全部在 capture.py，这里只透传配置不做决策
            neural_vad=bool(c.get("neural_vad")),
            # v2.12.0：原始音频旁路（90ms 聚合）。v2.13.0：**常开**——
            # 无接收者的 emit 成本可忽略（µs 级），换来"运行中从列表切到
            # dual 也能立刻热启流式通道"（此前 tap 在构造期按布局一次性判定，
            # 中途切换 = 流式永远断粮，用户实测感知就是"原文攒句"）
            tap_enabled=True,
        )
        # v2.12.0：流式预览通道——model_ready 后启动（需要 asr 的模型实例）；
        # 此处先创建并接好进料/出料（feed 与 partial_ready 都是排队连接）。
        # v2.13.0：创建闸门**不含布局**（只看开关+cuda）——运行中切到 dual
        # 即可热启动；启动/暂停闸门在 _maybe_start_stream_preview（含布局）
        self._stream_preview = None
        if (bool(c.get("stream_preview")) and str(c.get("asr_device")) == "cuda"
                and self.capture_thread is not None):
            self._stream_preview = StreamPreview(None, str(c.get("asr_language") or ""))
            self.capture_thread.raw_chunk.connect(self._stream_preview.feed)
            self._stream_preview.partial_ready.connect(self._on_partial_preview)
        self.capture_thread.segment_ready.connect(self.asr_thread.submit)
        self.capture_thread.level_changed.connect(self._on_level)
        self.capture_thread.error_occurred.connect(self._on_capture_error)
        self.capture_thread.low_input.connect(self._on_low_input)
        self.capture_thread.muted.connect(self._on_muted)
        self.capture_thread.start()
        self._sid_cap = self.capture_thread   # v2.6.2（P1-6）：会话身份引用

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

        # v2.20.1：面板常驻实时显示——启动后若被上次运行/异常路径藏了，这里补回
        if not self.overlay.isVisible():
            self.set_overlay_visible(True)
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
                ui_text("模型就绪 25 秒仍无识别结果：请确认所选设备正在播放声音（音量条应有波动），"
                "系统音量/应用音量未静音，或到「设置-音频输入」更换设备"))
            self.update_overlay_status()

    def _start_model_download_feedback(self, model_size):
        self._model_dl_model = model_size
        self._model_dl_total = MODEL_SIZES_MB.get(model_size, 480)
        self._stop_model_download_feedback()
        self._dl_start = None  # v2.2.11：慢速探测基线（首次 tick 建立）
        self._model_dl_timer = QTimer(self)
        self._model_dl_timer.setInterval(1500)   # v2.7.4（C-10）：600ms→1.5s，
        # 反馈要 rglob+stat 整个缓存树，下载大模型期间高频扫盘白耗 IO
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
                # v2.20.3：HF 快照里的 model.bin 是指向 blobs 的符号链接，`f.stat()` 会穿透
                # 计一次、blob 本体再计一次 → 字节翻倍（实测磁盘 100MB 显示 200MB/41%，
                # 真到一半时已经"480/480MB 99%"，ETA 消失且慢速提示被抑制，看着像卡死）。
                # 与 v2.0.1 修过的 `dir_size_mb` 同一判据：跳过符号链接。
                from app.translate.offline_pack import dir_size_mb
                mb = dir_size_mb(d)
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
        hint = ui_text("· 速度慢？到「设置-翻译」配置代理可显著提速") if slow else ""
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
                    eta = ui_fmt("，{rate}，剩余约{t}", rate=f"{sp:.1f}MB/s", t=t)
        # v2.2.5：模型下载进度走彩色横幅（下载是当前最重要的事，别挤状态行）
        self._set_engine_status(ui_text("正在下载识别模型…"))
        self._set_alert(ui_fmt("⬇ 正在下载识别模型（{mb}/{total}MB，{pct}%{eta}，"
                               "仅首次；完成前请保持网络畅通{hint}）…",
                               mb=f"{mb:.0f}", total=total, pct=pct, eta=eta, hint=hint))

    def _set_engine_status(self, text):
        self._engine_status_text = text
        # v2.0.8：积压警示置顶——"识别积压"出现后任何后续状态都追加提醒
        # （此前一句话即被"识别完成/就绪"覆盖，用户从未看到丢段原因）
        # v2.2.5：积压/连续失败等关键提示改走独立彩色横幅（不再挤常规状态行）
        if getattr(self, "_backlog_warn", False):
            self._set_alert(ui_text("⚠ 积压丢段中：CPU 转写跟不上，"
                            "建议到「设置-语音识别」换 small/tiny 模型"))
        elif getattr(self, "_heavy_cpu_warn", False):
            # v2.3.1：重模型+CPU 预警（用户实测"非常不好用"根因之一：medium/CPU
            # 每 10s 音频要 10~15s 转写，字幕越拖越晚永远追不上，且毫无提示）
            self._set_alert(ui_text("⚠ 当前为重模型且运行在 CPU：字幕会明显滞后。建议到"
                            "「设置-语音识别」换 small，或安装 GPU 加速后选「强制 GPU」"))
        elif getattr(self, "_engine_fallback_warn", None):
            # v2.3.2（G2）：在线引擎不可达预警持续展示（后续常规状态不覆盖）
            self._set_alert(self._engine_fallback_warn, error=True)
        else:
            # v2.20.6（i18n 前置改造）：这里原本写 `elif not ("识别积压" in text or
            # "积压" in text or "跳过" in text)`——拿中文子串当控制流。积压预警现在由
            # AsrThread.backlog 信号置位（见 _on_asr_backlog），而上面三条 if/elif 已经
            # 覆盖了所有"有警报警告"的情形，走到这里必然三个标志都为假，
            # 文本探针已成冗余，直接收起横幅即可。
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

    def _session_ok(self, session_thread):
        """v2.6.2（P1-6）：槽的会话身份守卫——sender 与当前会话线程匹配才
        放行。sender() 为 None（代码直接调用）放行；停止后 _sid_* 仍指向
        旧线程（尾句排水链放行上屏），下次 start_pipeline 覆盖——旧线程
        迟到信号从此被拦（修复旧会话"音频错误"误停新会话）。"""
        s = self.sender()
        return s is None or s is session_thread

    def _active_translate(self):
        """v2.6.2（P1-4）：翻译线程解析——运行中用当前线程；排水期
        （stop_pipeline 已置 None）用会话身份引用，asr 尾句仍能转发翻译。"""
        if self.translate_thread is not None:
            return self.translate_thread
        return getattr(self, "_sid_tr", None)

    def _on_asr_status(self, text):
        # v2.0.4：幽灵回调守卫 + 过期线程守卫——停止后已入队的迟到状态、
        # 或重启管线后旧 AsrThread 的残余状态，都不得覆盖当前 UI。
        # 此前该信号是全项目唯一没有 running 守卫的后端回调（v2.0.1 只补了
        # low_input/muted/result 三类），热键连按后状态栏永久卡在
        # "正在加载tiny模型"的根因之一：孤儿加载线程的加载消息在
        # stop_pipeline 写完"已停止"之后才送达
        if not self.running or self.sender() is not self.asr_thread:
            return
        self._set_engine_status(f"{ui_text('识别: ')}{text}")

    def _on_asr_backlog(self):
        """v2.0.8：积压提示置顶常驻——此前一句话即被后续状态覆盖，用户
        从未看到丢段原因（实测 8 段提交 0 条字幕的根因提示）。
        v2.20.6：改由 AsrThread.backlog 信号显式告知，不再 `"识别积压" in text`
        猜中文子串——那样一旦界面语言切英文就静默失效。"""
        if not self.running or not self._session_ok(getattr(self, "_sid_asr", None)):
            return
        self._backlog_warn = True

    def _on_translate_status(self, text):
        if not self.running or self.sender() is not self.translate_thread:
            return
        self._set_engine_status(f"{ui_text('翻译: ')}{text}")

    def _on_primary_recovered(self):
        """v2.3.2（G2）：主引擎恢复切回 → 撤销不可达预警横幅。
        v2.20.6：同上，改信号驱动（原来靠 `"已恢复" in text and "切回" in text`）。"""
        if not self.running or not self._session_ok(getattr(self, "_sid_tr", None)):
            return
        self._engine_fallback_warn = None

    def _on_engine_fallback(self, engine_desc, reason):
        """v2.3.2（G2）：在线引擎不可达的事前横幅——此前只有事后日志，
        代理=直连的用户整场翻译频繁失败也不知道为什么（模拟用户报告缺口）。"""
        # v2.6.2（P1-6）：停止后/新会话中旧翻译线程的迟到预警不再写入
        if not self.running or not self._session_ok(getattr(self, "_sid_tr", None)):
            return
        self._engine_fallback_warn = ui_fmt(
            "⚠ 在线翻译引擎不可达（{eng}）：{reason}。"
            "译文频繁出错请到「设置-翻译」配置代理，或改用「自动」引擎",
            eng=engine_desc, reason=reason)
        if self.running:
            self._set_alert(self._engine_fallback_warn, error=True)

    def _on_model_ready(self):
        # v2.0.4：模型就绪标记 + 停止下载进度反馈（原直连拆槽）
        # v2.6.2（P1-6）：新会话中旧线程迟到的"就绪"不得误置 _asr_ready
        if not self.running or not self._session_ok(getattr(self, "_sid_asr", None)):
            return
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
            self._set_engine_status(ui_text("模型就绪，正在聆听…（播放声音或说话即可出字幕）"))
            self._set_listen_pulse(True)
        # v2.12.0：模型就绪 → 启动流式原文预览（dual+GPU 时）
        self._maybe_start_stream_preview()

    # ---------- v2.12.0：流式原文预览通道（dual"实时不能停"） ----------

    def _stream_preview_enabled(self):
        """流式原文三重闸：①开关开 ②dual 布局（列表模式不需要草稿）
        ③识别计算方式为 cuda——预览通道每 0.9s 重识别一次最近 4s 音频，
        CPU 上单次要数秒、反而拖垮正式识别；GPU 下单次 ~0.4s，与正式
        识别分时复用可行（CTranslate2 模型只读、推理线程安全）。"""
        return (bool(self.config.get("stream_preview"))
                and self.overlay.is_dual()
                and str(self.config.get("asr_device")) == "cuda")

    def _maybe_start_stream_preview(self):
        """按闸门启停预览线程（共享 asr 已加载的模型实例）。
        v2.13.0：设置页/⋯菜单切到 dual 后调用即可**中途热启动**（此前只在
        model_ready 一次性判定，中途切布局流式永远断粮）；切回列表/关开关
        则暂停（不白烧 GPU）。幂等：已运行 no-op，未 running 不启动。"""
        p = getattr(self, "_stream_preview", None)
        if p is None:
            return
        if not self._stream_preview_enabled() or not getattr(self, "running", False):
            self._pause_stream_preview()
            return
        if p.isRunning():
            return
        asr = getattr(self, "asr_thread", None)
        model = getattr(asr, "_model", None)
        if model is None:
            return
        p.set_model(model)
        self._dual_current = ""
        self._dual_last_piece = ""
        self._dual_draft = None
        p.restart()          # 重置停止标志与陈旧音频缓冲（stop 后热重启）
        p.start()
        try:
            from app import log as app_log
            app_log.log("preview.started")
        except Exception:
            pass

    def _pause_stream_preview(self):
        """v2.13.0：中途暂停（不销毁对象——切回 dual 时可热重启）。"""
        p = getattr(self, "_stream_preview", None)
        if p is not None and p.isRunning():
            p.stop()
            p.wait(600)

    def _stop_stream_preview(self):
        p = getattr(self, "_stream_preview", None)
        if p is not None:
            p.stop()
            p.wait(2000)
            self._stream_preview = None

    @staticmethod
    def _strip_overlapped_prefix(base, text):
        """流式草稿增量：从 text 中剥掉与 base 尾部重叠的已确认部分。

        base 与 text 是**同一段音频的两次转写**（正式 beam 档 vs 预览 beam=1），
        标点/大小写/个别词必然有差异——严格字符串对齐实测整句重复
        （base="...bank." vs text="...bank" 从首字符就失配）。改用词级锚点：
        取 base 尾部 5/4/3/2/1 个词（lower+去尾标点）在 text 前部找最后出现
        位置，其后即新增；CJK 源用字符锚（split 分词对中文无效）。锚全失配
        （转写差异过大）返回全量——宁可少量重复下一拍自愈，也不丢新话。"""
        text = (text or "").strip()
        if not base or not text:
            return text
        if any("\u4e00" <= ch <= "\u9fff" for ch in base[-8:]):
            anchor = base[-6:]                      # CJK：字符锚
            pos = text.rfind(anchor)
            if 0 <= pos <= len(text) // 2:          # 锚须落在已确认区（前半）
                return text[pos + len(anchor):].strip()
            return text
        # v2.19.3：**规范化前缀相等快判**。预览草稿最常见的形态就是"基线整句 +
        # 少量新词"（4s 滑窗把同一段音频重转写一遍、再往前多听几个词）。此时 base 的
        # 尾部锚落在 text 的 2/3 限制之外 → 旧实现判"无重叠"→ **整句被当成新话返回**；
        # 旧幕墙态据此另起一行 = 同一句原文+译文重复两遍、尾巴碎片单独成行
        # （真机 60s 合成新闻实测：27 词基线只多 1 个词即触发，多 9 个词才正常）。
        # 逐词去标点+小写后比较，绕开"chunk 两端才去标点"的盲区。
        def _toks(s):
            out = []
            for w in s.split():
                n = w.strip(".,!?;:\"'()[]{}").lower()
                if n:
                    out.append((w, n))
            return out

        bt, tt = _toks(base), _toks(text)
        if bt and len(tt) >= len(bt) and [n for _w, n in bt] == [n for _w, n in tt[:len(bt)]]:
            return " ".join(w for w, _n in tt[len(bt):]).strip()
        base_words = base.split()
        text_words = text.split()
        best_end = None
        for n in (5, 4, 3, 2, 1):
            if len(base_words) < n:
                continue
            anchor = " ".join(base_words[-n:]).lower().strip(".,!?;:")
            if not anchor:
                continue
            limit = min(len(text_words), max(4, len(text_words) * 2 // 3))
            # v2.18.1：取**最后一次**出现，不是第一次。短锚（n=1/2 的 "and"/"told"
            # 这类常用词）常在草稿前段就撞上一次巧合匹配，旧代码找到就 break →
            # 剥离量不足 → 已显示的那段被当成新话整段追加（真机 BBC 新闻实测：
            # 原文与译文各出现同一句两遍）。取最后一次才是"已显示到此为止"。
            for i in range(limit):
                if i + n > len(text_words):
                    break
                chunk = " ".join(text_words[i:i + n]).lower().strip(".,!?;:")
                if chunk == anchor:
                    best_end = i + n
            if best_end is not None:
                break
        if best_end:
            return " ".join(text_words[best_end:]).strip()
        return text

    @staticmethod
    def _word_pairs(s):
        """(原词, 规范化词) 列表：去首尾标点 + 小写，空串丢弃。
        v2.19.3：剥离与合并两处都要"忽略标点/大小写比词"，共用一份实现。"""
        out = []
        for w in (s or "").split():
            n = w.strip(".,!?;:\"'()[]{}").lower()
            if n:
                out.append((w, n))
        return out

    @staticmethod
    def _merge_stream(current, diff):
        """v2.14.0：当前句显示文本与流式增量合并。

        diff 是预览窗口内相对上一句的新增话音，current 是当前句已显示文本：
        a) diff 以 current 为前缀延伸 → 取更长的 diff（窗口重写更准）
        b) current 包含 diff（窗口滑动后 partial 变短）→ 保留 current
        c) current 尾部与 diff 头部重叠（≥2 字符）→ 拼接去重叠
        d) 无重叠 → 空格拼接（转写差异过大：宁可重复下一拍自愈）
        e) v2.19.3：**词级回跳去重**。真实网页新闻实测（DW 直播 150s）出现同一
           短语在**同一行内**被拼进三遍——预览转写在句子中段就与已显示文本分叉
           （"…Lauren, now in a" vs "One of those people is Lauren, now in her
           mid-30s…"），(c) 类"后缀==前缀"根本对不齐 → 落到 (d) 整段追加。
           改为：若 diff 开头若干词（≥3，忽略标点/大小写）已在 current 中连续出现，
           就丢掉这段复读、只追加真正的新词。3 词护栏避免误吞 "very very good"
           这类合法叠词。
        f) v2.20.0：**尾部整块重转写**。用户实拍另一形态——复读块在 diff 的**结尾**
           而不是开头（"…my guest tonight. Oh, my God! and Tom Cruise is my guest
           tonight."）：滑窗重新听见同一段音频、给出措辞不同的另一版，(e) 的
           "diff 头部命中"判据完全不认，于是照 (d) 整段追加 = 同一句在屏上两遍。
           判据改为**两端都对齐**：current 与 diff 的规范化词尾有 ≥3 词公共后缀，
           说明这一拍没有往前走。取"更完整的那一份"——谁把另一方整块包住就留谁
           （本例 diff 包住 current → 收 diff，句首新听到的词一个不丢）；两都
           不包则保持 current（本拍无新增，等终版收口校准；宁可少一拍也不复读）。"""
        if not current:
            return diff
        if not diff:
            return current
        if diff.startswith(current):
            return diff
        if current.startswith(diff):
            return current
        # v2.20.0：(c) 类重叠至少 2 个字符。旧写法 k 一路降到 1，只要"current
        # 末字符 == diff 首字符"就当成重叠并把它吃掉——真机是逐词生长的增量，
        # 这种巧合天天有：("Tom Cruise is my guest", "tonight and we talk…")
        # 被拼成 "guest**onight** and we talk…"、("the market closed at",
        # "the price rose") 被拼成 "closed**athe** price"，都是看得见的糊字。
        max_k = min(len(current), len(diff))
        for k in range(max_k, 1, -1):
            if current.endswith(diff[:k]):
                return current + diff[k:]
        nc = [n for _o, n in MainWindow._word_pairs(current)]
        dp = MainWindow._word_pairs(diff)
        nd = [n for _o, n in dp]
        for ln in range(min(len(nd), 12), 2, -1):
            head = nd[:ln]
            for i in range(len(nc) - ln, -1, -1):
                if nc[i:i + ln] == head:
                    tail = " ".join(o for o, _n in dp[ln:]).strip()
                    return (current + " " + tail).strip() if tail else current
        # ---------- (f) 公共词尾 ≥3：这一拍是对旧音频的重转写 ----------
        tail_common = 0
        while (tail_common < min(len(nc), len(nd))
               and nc[-1 - tail_common] == nd[-1 - tail_common]):
            tail_common += 1
        if tail_common >= 3:
            if MainWindow._contains_block(nd, nc):
                return diff
            if MainWindow._contains_block(nc, nd):
                return current
            return current
        # v2.20.4：走到这里就要整段追加了，而实测有两类形态会被追加成"同一句在
        # 屏上两遍"，必须在 (d) 之前拦住：
        # ① 词级内嵌重复——diff 只是开头多听到一个词，把 current 整块包住
        #    （"the market closed at four" vs "and the market closed at four
        #    o'clock today"）。旧写法只在 `tail_common>=3` 那个分支里查
        #    `_contains_block`，而这类形态的公共词尾只有 2 个，根本进不去。
        if MainWindow._contains_block(nd, nc):
            return diff
        if MainWindow._contains_block(nc, nd):
            return current
        # ② CJK/假名整句重转写：这类文本没有空格，词表恒为 1 个 token，上面所有
        #    词级判据（(c) 的 6 字锚点、(e)、(f)）全部失效，实测
        #    merge("他说今天天气不错", "他说今天的天气不错还有雨") 直接拼成两遍。
        #    改按**字符级有序 LCS 占比**判（阈值与面板同句判据同源：短的一侧
        #    几乎原序被长的一侧包住＝同一句）。真·新句（"这个方案今天开会讨论"
        #    vs "这个方案明天开始实施"）实测只有 0.6，不会被误并。
        if _lcs_ratio(current, diff) >= 0.85:
            return diff if len(diff) >= len(current) else current
        return (current + " " + diff).strip()

    @staticmethod
    def _contains_block(hay, needle):
        """规范化词表 `needle` 是否作为**连续块**出现在 `hay` 里（空 needle 算在）。"""
        if not needle:
            return True
        if len(needle) > len(hay):
            return False
        for i in range(len(hay) - len(needle) + 1):
            if hay[i:i + len(needle)] == needle:
                return True
        return False

    def _on_partial_preview(self, text):
        """预览草稿上屏：确认区 + 增量 → 原文区整体刷新（每 ~0.9s 一拍）。
        译文不跟进草稿（草稿反复改写不值得送译；推测式翻译已覆盖实时性）。"""
        if not getattr(self, "running", False) or not self.overlay.is_dual():
            return
        t = (text or "").strip()
        if not t:
            return
        # v2.14.0：流式模型拆分——_dual_base（最后终版句，剥离基线）
        # 与 _dual_current（当前句显示文本）分离，终版句收口后当前句从
        # 零开始，不再混句
        base = getattr(self, "_dual_base", "") or ""
        cur_txt = getattr(self, "_dual_current", "") or ""
        # v2.18.1：剥离基线必须是"屏幕上已经显示的全部"（上一终版句 + 当前句），
        # 而不是只有上一终版句。预览窗口每拍都会把上一拍的尾部再转写一遍，
        # 只按 base 剥离时，与 current 的重叠就只剩 `_merge_stream` 的严格后缀
        # 匹配在兜——而 whisper 两次转写措辞必有差异（实测 "and told..." vs
        # "and told me more about"），后缀对不齐就走"无重叠"分支 → **整句重复
        # 上屏**（真机 BBC 新闻截图实证：原文与译文各出现同一句两遍）。
        shown = (base + " " + cur_txt).strip() if cur_txt else base
        diff = self._strip_overlapped_prefix(shown, t)
        if not diff:
            return
        cur = self._merge_stream(cur_txt, diff)
        self._dual_current = cur
        self.overlay.update_partial(cur)
        # v2.13.0：**草稿也送推测翻译**——译文区跟着原文一起实时生长。
        # 此前草稿不送译，译文只在正式片段到达（分段周期 2.5~4s）才刷新，
        # 用户实测反馈"译文还那种攒句"。仅离线引擎闸内生效（argos 0.06s/次、
        # 无额度，0.9s 一拍毫无压力；在线引擎请求量 ×4.4 保持不送）。
        # 配对走"最新草稿全文"（_dual_draft）而非 _spec_inflight 簿记，
        # 迟到草稿（非最新）自动作废——下一拍马上会有更新的。
        self._dual_draft = cur
        if self._spec_enabled():
            tr = self._active_translate()
            if tr is not None and tr.isRunning():
                # v2.13.0a：语言必须回退到配置项——首片到达前 _tgroup_lang 是
                # 空串，argos 找不到 ""→zh 的语言包直接抛错（spec 不走备援链），
                # 草稿译文全灭（真机密集拍实证：译文 4.2s 才随首片出现）
                # v2.18.2（D-3）：回退链升级为"攒句语言→会话最近识别语言→配置"，
                # 且 **"auto" 不再当语言用**（translator 会挡成 source=None 抛错）；
                # 仍解析不出就本拍不送，下一拍（0.9s 后）语言通常已锁定
                lang = self._spec_source_lang()
                if lang:
                    tr.submit(cur, lang, spec=True)

    def _on_level(self, value):
        """电平槽。契约：value 为 capture 的 0~1 比例（见 CaptureThread
        .level_changed v2.3.16 注释）——有声判据 3%、音量条换算百分数。"""
        # v2.0.4：停止后迟到的电平事件不再点亮音量条
        # v2.3.15（P19）：capture 发的 value 是 0~1 的比例（min(1, level*8)），
        # 旧条件 value>=3 恒假——"最近有声"时间戳永不更新（P8 电平守卫与 P16
        # 静默巡查双双形同虚设，实测尾句 1s 抢送/跨片不合并）、音量条 setValue
        # 收小数恒 0（界面让用户"看音量条波动"是空话）。统一换算成百分比。
        if not self.running or not self._session_ok(getattr(self, "_sid_cap", None)):
            return
        if value >= 0.03:
            # v2.3.6（P8）：记录"最近有声"时刻（3% 噪声地板之上算有声）
            self._last_level_sound = time.monotonic()
        self.level_bar.setValue(int(value * 100))

    def _on_low_input(self, quiet):
        """采集线程报告输入信号持续过弱/恢复正常。"""
        from app import log as app_log
        app_log.log("capture.low_input", quiet=bool(quiet))
        if not self.running or not self._session_ok(getattr(self, "_sid_cap", None)):
            # v2.0.1：幽灵回调守卫——停止后仍可能收到已入队的 Queued 信号
            # v2.6.2（P1-6）：新会话中旧采集线程的迟到告警不再污染状态行
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
                ui_text("⚠ 输入信号过弱：字幕可能无法识别，请检查系统音量或音频设备"))
        else:
            self.engine_status_label.setText(getattr(self, "_engine_status_text", ""))
        self.update_overlay_status()

    def _on_muted(self, m):
        """系统静音盲区提示（补充5）：静音且抓系统声音时给出确定性指引。"""
        if not self.running or not self._session_ok(getattr(self, "_sid_cap", None)):
            return  # v2.0.1：幽灵回调守卫（迟到的 muted 曾覆盖"已停止"状态）
        if m and self.running:
            self._muted_warn = True
            self.engine_status_label.setText(
                ui_text("⚠ 系统已静音：正在抓取系统声音，静音期间不会有字幕；取消静音后自动恢复"))
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

    def apply_pipeline_hotfix(self):
        """v2.6.0（R5）：设置保存后把词典/质量档热更新到运行中的线程。

        线程成员为纯 Python 引用替换（GIL 下原子），读侧天然一致——
        无需锁、无需重启管线。热更失败仅记日志，管线沿用旧值继续。"""
        from app import log as app_log
        c = self.config
        whole_word = bool(c.get("fix_whole_word"))
        beam = 5 if str(c.get("offline_quality") or "high") == "high" else 2
        at = self.asr_thread
        if at is not None and at.isRunning():
            at.update_mishear_map(dict(c.get("mishear_map") or {}), whole_word)
        tt = self.translate_thread
        if tt is not None and tt.isRunning():
            tt.update_fix_map(dict(c.get("translate_fix_map") or {}))
            tt.update_whole_word(whole_word)
            tt.update_beam_size(beam)
        app_log.log("pipeline.hotfix_applied", whole_word=whole_word, beam=beam)

    def stop_pipeline(self):
        if not self.running:
            return
        from app import log as app_log
        app_log.log("pipeline.stop")
        self.running = False
        self.toggle_button.setText(ui_text("开始翻译"))
        self.toggle_button.setObjectName("PrimaryButton")
        self.toggle_button.style().unpolish(self.toggle_button)
        self.toggle_button.style().polish(self.toggle_button)
        self._set_listen_pulse(False)  # v2.2.12：停止熄灭呼吸
        self._heavy_cpu_warn = False   # v2.3.1：撤重模型CPU预警
        self._engine_fallback_warn = None  # v2.3.2（G2）：撤引擎不可达预警
        self.status_dot.setStyleSheet("background-color: #3a4152; border-radius: 7px;")
        self.status_dot.setToolTip(ui_text("未启动：点击「开始翻译」开始"))
        self.status_text.setText(ui_text("未启动"))
        self.level_bar.setValue(0)
        self._low_input_warn = False
        self._muted_warn = False  # v2.0.1：漏复位曾让悬浮条停止后仍显示"系统静音中"
        self._fail_streak = 0  # v2.0.2：会话结束时清零连续失败计数
        self._backlog_warn = False  # v2.0.8：积压警示随会话结束复位
        self._set_alert(None)  # v2.2.5：停止时清掉提示横幅
        if getattr(self, "_pending", None):
            # v2.2.0：停止时清空流式占位配对——队列里未及翻译的卡片不再等
            # 迟到译文（下次会话不复用旧卡片）
            # v2.7.4（B-12）：先终态化再清配对——此前"⟳ …"永久悬挂在屏，
            # 且 txt 导出会把占位行一起带出去（SRT 侧已过滤，两出口分叉）
            for _txt, _card in list(self._pending):
                try:
                    # v2.7.6（A）：推测态卡片已有可用译文 → 就地终态化保留译文，
                    # 不能覆盖成"未完成翻译"（用户正在看那行字，半句也胜过失败文案）
                    finalize = getattr(_card, "finalize_spec", None)
                    if not (finalize and finalize()):
                        _card.set_failed(ui_text("已停止 · 该句未完成翻译"))
                except Exception:
                    pass
            self._pending.clear()
        # v2.3.6（P9）：低延迟攒句缓冲随会话清零（未送出的碎片不等迟到译文）
        self._tgroup = []
        if getattr(self, "_tgroup_by_src", None):
            self._tgroup_by_src.clear()
        # v2.7.6（A）：推测簿记清零——排水期到达的中间版回复按 gen 失效自动丢弃
        if getattr(self, "_spec_inflight", None):
            self._spec_inflight.clear()
        if getattr(self, "_spec_ts", None):
            self._spec_ts.clear()
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
        # v2.12.0：流式预览通道先停——它引用 asr 的模型实例，且 stop 后
        # raw_chunk 进料已无意义（放行 _schedule 排期链不受影响）
        self._stop_stream_preview()
        # v2.6.2（P1-4）：排水式停止——旧顺序"先断全部信号再 stop"使
        # capture 的 flush 尾段无人接收，asr/translate 清空队列又丢掉已入队
        # 内容，数据槽的 running 守卫再拦一层——三层必死，"说完立刻停丢最后
        # 一句"（v2.2.1 修复实际无效）。改为：stop（各线程排水语义）→
        # capture 尾段同步直塞 asr → wait → 孤儿化 → 只对已退出线程摘信号。
        # 迟到状态信号由各槽 running+会话身份守卫拦截，尾句经身份守卫放行
        for t in threads:
            if t:
                t.stop()
        # capture 先收尾（循环粒度 10ms）：同步取 flush 尾段直塞 asr——
        # 跨线程信号要经主线程事件循环中转，停止流程阻塞期间无人消费
        if threads[0]:
            threads[0].wait(1500)
            tail = threads[0].pop_tail_seg()
            if tail is not None and threads[1]:
                threads[1].submit(tail)
        # v2.0.4：模型加载期的 AsrThread 阻塞在 WhisperModel() 构造里，
        # 响应不了 _stop 标志也到不了队列哨兵，等满 3 秒只会白白冻结 GUI
        # （阻塞期间按下的热键全部排队，恢复后被逐条当作新 toggle，
        # start/stop 毫秒级翻转——实机日志实证的根因推手）。
        # 加载未完成（_asr_ready 为假）时缩短等待，线程交孤儿容器收尾
        for i, t in enumerate(threads[1:], start=1):
            if t:
                if i == 2:
                    # v2.7.3：翻译线程不再阻塞等待——它的尾段来自"主循环转发 asr 排队
                    # 信号"，主线程停在 wait 上反而掐死自己的转发源（死锁结构）。
                    # 退出时机改由 _on_asr_finished→close_input 决定（尾句落地即退，
                    # 通常 <1s；异常时 15s 宽限兜底）。此处继续往下走不阻塞 GUI
                    continue
                timeout = 2500
                if i == 1 and not getattr(self, "_asr_ready", False):
                    timeout = 500
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
                if t is threads[2]:
                    # v2.7.3：翻译线程停止时仍在跑=设计内"停止后台排水"
                    # （close_input 后秒级自退），单列标签，与真孤儿区分
                    app_log.log("pipeline.translate_draining")
                else:
                    app_log.log("pipeline.orphan_thread", cls=type(t).__name__)
                if t is threads[1] and not getattr(self, "_asr_ready", False):
                    # v2.0.4：加载期停止的专项记录——加载线程随后台完成即静默退出
                    app_log.log("pipeline.stop_during_model_load",
                                model=self.config.get("asr_model"))
        # v2.6.2（P1-4）：只对已退出线程摘信号——排水中的孤儿线程保持连接，
        # 尾句链（text_ready→submit→result_ready）要走完上屏；迟到状态与
        # 新会话串台由各槽会话身份守卫拦截
        for t in threads:
            if t:
                try:
                    if not t.isRunning():
                        self._detach_thread(t)
                except RuntimeError:
                    pass   # C++ 对象已销毁（线程早已终结），无需摘信号
        self.capture_thread = None
        self.asr_thread = None
        self.translate_thread = None
        self.engine_status_label.setText(ui_text("引擎：已停止"))
        self._log_latency_summary()   # v2.3.20（P26）：会话延迟摘要入日志
        self._apply_process_priority(False)   # v2.7.2：榨干模式停止后归还优先级
        self.update_overlay_status()

    def _apply_process_priority(self, high):
        """v2.7.2：榨干模式的进程优先级——HIGH(0x80)/NORMAL(0x20)。
        仅开关开启时动手；非 Windows/权限失败静默不影响功能。"""
        if not bool(self.config.get("perf_turbo")):
            return
        try:
            import ctypes
            k = ctypes.windll.kernel32
            k.SetPriorityClass(k.GetCurrentProcess(), 0x00000080 if high else 0x00000020)
        except Exception:
            pass

    def _on_recheck_dropped(self, text, duration):
        """v2.7.5（R-2）：语言复检丢弃留痕——engine 侧低置信不一致整段 return，
        此前用户视角"字幕无预警跳过一大段"。弱化卡上屏（导出过滤集已含失败态，
        不会混进 TXT/SRT），会话身份守卫与管线槽同策略。"""
        s = self.sender()
        if s is not None and s is not getattr(self, "_sid_asr", None):
            return
        card = self._new_card(text)
        card.set_failed(ui_text("语言复检与当前锁定语言不一致且置信度不足 · 该段保守丢弃"))
        self._insert_card(card)
        if self.stack.currentIndex() == 0:
            self.stack.setCurrentIndex(1)

    def _on_asr_finished(self):
        """v2.7.3：识别线程已退出=所有 text_ready 已发出，且本槽在主线程按序处理
        （排在全部尾句转发之后）——此刻把主窗攒句残组冲刷给翻译、再关闭翻译输入门，
        翻译线程排空队列即退，不再空等 15s 排水宽限。
        会话身份守卫：管线重启后旧 asr 的 finished 不得冲刷新会话残组/关新门。"""
        s = self.sender()
        if s is not None and s is not getattr(self, "_sid_asr", None):
            return
        try:
            if getattr(self, "_tgroup", None):
                self._flush_tgroup()
            tr = self._active_translate()
            if tr is not None:
                tr.close_input()
        except RuntimeError:
            pass

    def _on_capture_error(self, msg):
        """采集线程的错误**按定义就是音频类**——此前靠 `"采集" in msg or "音频" in msg`
        这类中文子串猜，界面语言一旦切英文，"要不要拆管线"这个决定就会静默失效
        （v2.20.6 i18n 前置改造）。改由信号来源显式告知。"""
        self._on_pipeline_error(msg, audio=True)

    def _on_pipeline_error(self, msg, audio=False):
        from app.errors import friendly_message
        from app import log as app_log
        app_log.log("pipeline.error", detail=str(msg)[:200])
        s = self.sender()
        if not self.running or (
                s is not None and s is not getattr(self, "_sid_asr", None)
                and s is not getattr(self, "_sid_cap", None)):
            # v2.0.4：停止后迟到的管线错误不再覆盖"已停止"（幽灵回调守卫，
            # 与 _on_asr_status/_on_translate_status 同一策略）
            # v2.6.2（P1-6）：会话身份守卫——新会话中旧线程（capture/asr）
            # 迟到的"音频错误"不得误停新会话（sender 为 None=直接调用，放行）
            return
        msg = friendly_message(str(msg))
        if self.running and audio:
            self.stop_pipeline()
            # stop_pipeline 会把状态重置为"已停止"，错误信息要在其后显示才能被看到
            self.engine_status_label.setText(f"{ui_text('错误：')}{msg}")
            self._show_info(ui_text("音频错误"), msg)
        else:
            # v2.20.3（离屏实测）：模型加载失败这类非音频错误此前只写一行状态字——
            # running 仍是 True、面板仍写"运行中 · 系统声音"、把手仍是"⏸ 暂停"、
            # 音量条继续跳，而 ASR 线程其实已经死了；下载横幅还会永久冻在 99%
            # （`_stop_model_download_feedback` 只在 ready/stop 两条路调）。
            # 改走常驻告警（`_set_alert` 不会被下一条状态覆盖）并收掉下载横幅。
            self.engine_status_label.setText(msg)
            self._set_alert(msg, error=True)
            self._stop_model_download_feedback()

    def _on_asr_text(self, text, detected, duration, t_flush=-1.0):
        # v2.6.2（P1-4/P1-6）：会话身份守卫替代 running 守卫——停止后旧
        # 会话的尾句仍要放行上屏（排水链最后一步，"说完立刻停"不再丢句），
        # 新会话开始后旧线程迟到信号按身份拦截
        s = self.sender()
        if s is not None and s is not getattr(self, "_sid_asr", None):
            return
        # v2.18.2（D-3）：记录本会话"最近一次识别到的语言"——推测式送译在
        # 攒句尚未拿到语言时（草稿早于首个终版片段）用它兜底，见 _spec_source_lang
        self._last_asr_lang = detected or getattr(self, "_last_asr_lang", "")
        if getattr(self, "_caption_seen", False) is False:
            self._caption_seen = True
            # v2.7.5（R-8）：首片上屏同时复位低电平告警标志——v2.4.4 BUG-3
            # 契约"字幕上屏即撤告警"此前只兑现显示层（横幅撤了），标志位仍
            # True 会被下次 _on_low_input 的静音期判定重复引用
            self._low_input_warn = False
            self._set_engine_status(getattr(self, "_engine_status_text", ""))
            self.update_overlay_status()
        self._caption_seen = True
        # v2.7.0（T2）：暂存末片"段内尾静音/置信度"，供攒句提前冲判据使用
        # v2.7.5（R-4）：tail_q/last_lp 已随 T2 审计证伪拆除（见 engine 信号注释）
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
        # v2.18.2（D-2）：本方法内**只此一次**面板占位调用。此处曾有第二处调用
        # （建卡之后，v2.4.0 遗留），同一片段两次进 dual 原文区：延续片段
        # （小写开头=_starts_new_sentence 判为续接）被 _dual_join 拼接两遍，
        # 实测原文区出现 "Hello everyone and welcome to the show and welcome
        # to the show"。GPU+dual 下被流式预览每拍整体覆盖而掩盖，**CPU 用户
        # （预览按闸门关闭）直接可见**；列表模式因 _find_pending 幂等不受影响。
        if self.overlay.isVisible():
            self.overlay.show_pending(text)
        # v2.1.5：instant_caption 开关——开（默认）为流式两段式（原文先上屏、
        # 译文占位、就绪后原地补齐）；关 = 旧行为（识别+翻译都完成后一次性上屏）
        if not bool(self.config.get("instant_caption")):
            self._set_engine_status(f"{ui_text('识别完成 [')}{detected or '?'}] ({duration}{ui_text('s)，翻译中…')}")
            if self._active_translate() is not None:   # v2.6.2（P1-4）：排水期解析
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
        self._insert_card(card)
        if self.stack.currentIndex() == 0:
            self.stack.setCurrentIndex(1)
        if not hasattr(self, "_pending") or self._pending is None:
            self._pending = []
        self._pending.append((text, card))
        self._set_engine_status(f"{ui_text('识别完成 [')}{detected or '?'}] ({duration}{ui_text('s)，翻译中…')}")
        # v2.2.5：占位卡即刻聚焦（原文先出时用户视线在此）
        prev = getattr(self, "_active_card", None)
        if prev is not None and prev is not card:
            try:
                prev.set_active(False)
            except RuntimeError:
                pass
        self._active_card = card
        card.set_active(True)
        # v2.18.2（D-2）：此处第二次 overlay.show_pending(text) 已删除——
        # 面板占位统一在方法开头（同一 text 调两遍会让 dual 原文区重复拼接）
        if self._follow_bottom():
            sb = self.scroll.verticalScrollBar()
            sb.setValue(sb.maximum())
        if self._active_translate() is not None:   # v2.6.2（P1-4）：排水期解析
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
        if not bool(self.config.get("low_latency_mode")) or not self._grouping_enabled():
            # v2.19.0：`translate_grouping=False`（关攒句）走同一条逐片直送通路——
            # 与"低延迟关"的区别只在于**分段仍由 low_latency_mode 决定**，
            # 所以关掉攒句不会把切段退回 14s 慢档（用户要的是"实时"，不是"更慢"）。
            tr = self._active_translate()
            if tr is None:
                return
            if not tr.isRunning():
                # v2.20.4：与攒句路径 `_flush_tgroup` 的 B-1 守卫同一件事，此前
                # 只有那条路有守卫——关掉攒句时翻译线程若已自然退出，文本投进死
                # 队列永不回来，占位卡永久停在 "⟳ …"，且没有任何日志。
                self._drop_translation(text, ui_text("翻译已停止，本句未翻译"))
                return
            self._submit_ts = getattr(self, "_submit_ts", {})
            self._submit_ts[text] = time.monotonic()  # v2.3.20（P26）
            # v2.6.2（P1-7）：被队列挤掉的句子占位卡立即终态化，不再悬挂
            # （or []：submit 契约新加了 dropped 返回值，兼容旧测试 stub）
            for d_text, _d_lang in (tr.submit(text, detected) or []):
                self._drop_translation(d_text)
            return
        grp = getattr(self, "_tgroup", None)
        if grp is None:
            grp = self._tgroup = []
            self._tgroup_lang = ""
        if grp and self._starts_new_sentence(text):
            self._flush_tgroup()
            grp = self._tgroup
        grp.append(text)
        # v2.12.0：dual 流式原文的"已确认基线"随片段生长（预览草稿的剥离基准）
        # v2.14.0：正式片段覆盖当前句显示文本（权威性高于草稿 merge）
        self._dual_last_piece = text
        self._dual_current = self._combine_pieces(grp)
        self._tgroup_last_at = time.monotonic()   # v2.7.0（T2）：攒住时长遥测起点
        if len(grp) == 1:
            # v2.3.18（P23）：组寿命起点——绝对上限用它算，续片无法续命
            self._tgroup_start = time.monotonic()
        self._tgroup_lang = detected or self._tgroup_lang
        # v2.7.6（A）推测式增量翻译：碎片一到达就把"当前已攒文本"送翻译上屏，
        # 下一片到达再送更长版本，译文在同一张卡/同一面板行上原地生长覆盖。
        # 连续语流下译文等待从 hold_p50≈4.1s 降到 ≈0.06s（离线引擎单次耗时）。
        self._maybe_spec_submit(grp)
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
            t.setInterval(250)   # v2.7.0（T2）：1s→250ms，配合提前冲把判定粒度做细
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
        # v2.7.0（T2）：提前冲句——静默地板 3.5s→2.0s。实测修正：审计设想用
        # "段内尾静音(whisper seg.end)"做额外证据，但真机验证 whisper 会把末片
        # 结束时间拉伸补齐到音频尾（tail_q 恒≈0，12:57/13:00 两轮 hold_p50
        # 5.0/6.0 铁证），故只保留时间地板一档。收益定位=末句抢救：连续语流中
        # 本句译文由"下一句到达"冲刷（两轨制固有，hold≈6s 不变）；说话结束/
        # 场景切换后的最后一句，此前要干等 3.5s 静默才冲，现 2.0s。
        # v2.7.2：榨干模式再压一档 2.0→1.2（配合 4s 分段上限，切短风险自担已文案告知）
        if bool(self.config.get("perf_turbo")):
            floor = 1.2
        else:
            floor = 2.0 if bool(self.config.get("early_flush")) else 3.5
        if (quiet_for >= floor and final_looking) or now >= getattr(self, "_tgroup_deadline", 0.0):
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
        hold_at = getattr(self, "_tgroup_last_at", None)   # v2.7.0（T2）遥测
        self._tgroup_last_at = None
        self._tgroup = []
        self._tgroup_start = None      # v2.3.18（P23）：组起点随组清空
        # v2.7.6（A）：组代数递增 + 清空本组的推测簿记——终版已提交、马上
        # 会带完整译文到达，仍在飞行中的"中间版"回复一律作废（不清空的话，
        # 迟到的半句译文可能盖在已经终态化的卡上）。gen 另作双保险：
        # _on_spec_translated 只接受与当前攒句组同代的回复。
        self._tgroup_gen = getattr(self, "_tgroup_gen", 0) + 1
        if getattr(self, "_spec_inflight", None):
            self._spec_inflight.clear()
        if getattr(self, "_spec_ts", None):
            self._spec_ts.clear()
        t = getattr(self, "_tgroup_timer", None)
        if t is not None:
            t.stop()
        tr = self._active_translate()   # v2.6.2（P1-4）：排水期解析到会话身份线程
        if not grp or tr is None:
            return
        if not tr.isRunning():
            # v2.7.4（B-1）：翻译线程已自然退出（asr 排水超过 15s 宽限等场景）——
            # 投进死队列的尾组会让占位卡永久悬挂"⟳ …"，直接整组终态化
            for piece in grp:
                self._drop_translation(piece, ui_text("翻译已停止，本句未翻译"))
            return
        # v2.7.0（T2）："末片到达→整句冲送"的攒住时长入遥测——提前冲是否
        # 起效，看日志 hold_p50 一行即证（此前"慢"的大头恰好不在任何遥测里）
        if hold_at is not None:
            self._lat_add("_lat_hold", max(0.0, time.monotonic() - hold_at))
        combined = self._combine_pieces(grp)
        # v2.12.0：终版收口 = 流式原文的确认基线更新（预览草稿从整句尾部续接）
        # v2.14.0：整句音频已完，后续 partial 相对它剥离出"新句增量"；
        # _dual_current 保留显示，终版翻译到达时清空（v2.20.0：历史区已删，
        # 面板大字区把这句留到下一句开始）
        self._dual_base = combined
        # v2.13.0：冲刷即作废在飞草稿译文（防迟到草稿盖住新句开头）
        self._dual_draft = None
        self._tgroup_by_src = getattr(self, "_tgroup_by_src", {})
        self._tgroup_by_src[combined] = grp
        self._submit_ts = getattr(self, "_submit_ts", {})
        self._submit_ts[combined] = time.monotonic()  # v2.3.20（P26）
        # v2.6.2（P1-7）：合并句被队列挤掉时整组碎片卡立即终态化
        for d_text, _d_lang in (tr.submit(combined, getattr(self, "_tgroup_lang", "")) or []):
            self._drop_translation(d_text)

    @staticmethod
    def _combine_pieces(grp):
        """碎片列表 → 送翻译的整句键（v2.7.6（A）提取为函数，冲刷与推测共用，
        保证两侧算出的 combined 完全一致，否则终版回复会配不上推测建的簿记）。
        含 CJK 时无空格直连，纯拉丁按空格连接。"""
        joined = " ".join(grp)
        return "".join(grp) if any("\u4e00" <= c <= "\u9fff" for c in joined) else joined

    # ---------- v2.7.6（A）：推测式增量翻译 ----------
    # 遥测实锤（用户真机会话，n_reco=110）：reco_p50=0.55s、tr_p50=0.06s，
    # 而 hold_p50=4.13s——端到端延迟的 87% 是"攒句等下一片冲刷"的结构性等待，
    # 识别+翻译本身只占约 0.6s。提前冲句（v2.7.0）只救末句、榨干模式（v2.7.2）
    # 只是把等待从 6s 压到 4s，hold 始终等于分段周期；本方案才是根治。

    def _spec_enabled(self):
        """推测式翻译三重闸：①开关开 ②低延迟模式在攒句（否则逐句直送、无可推测）
        ③**仅离线引擎**。在线引擎有额度与限流（MyMemory 每天约 5000 字符免费额度），
        每片都送一份中间版会让请求量成倍增长，故一律退回整句翻译；
        engine=auto 按引擎探测后的实际选用结果（_active_engine）判定。"""
        if not bool(self.config.get("spec_translate")):
            return False
        if not bool(self.config.get("low_latency_mode")):
            return False
        tr = self._active_translate()
        if tr is None:
            return False
        return str(getattr(tr, "_active_engine", "") or "") == "argos"

    def _spec_source_lang(self):
        """推测式送译可用的**源语言**（v2.18.2 D-3）。

        回退链：本攒句语言 → 本会话最近一次识别到的语言 → 配置项
        （**"auto" 视同未知**，与 translator.py:537 的既有闸门口径一致）。
        解析不出真语言时返回空串，调用方**跳过这一拍的推测送译**——
        真机实测：首句草稿在语言锁定前拿配置里的 "auto" 去送 argos，
        translator 挡成 source=None → 抛"缺少源语言信息…"，spec 不走备援链，
        12 条推测回复里 4 条带错，用户观感=原文在长、译文区不动。
        草稿每 0.9s 一拍，下一拍语言通常就有了，跳过比送注定失败的请求更好：
        省一次引擎调用、不产生错误态、也不污染 _spec_inflight 簿记。"""
        lang = (getattr(self, "_tgroup_lang", "")
                or getattr(self, "_last_asr_lang", "")
                or str(self.config.get("asr_language") or ""))
        lang = str(lang or "").strip()
        return "" if lang.lower() == "auto" else lang

    def _grouping_enabled(self):
        """v2.19.0：翻译侧"攒句合并"开关（默认开=两轨制：上屏碎、翻译整句送）。
        关=每个识别片段一到达就立即送翻译（逐片实时）。每片段实时读配置，
        与 low_latency_mode（分段侧）解耦，故设置页保存即时生效、无需重启管线。"""
        return bool(self.config.get("translate_grouping"))

    def _maybe_spec_submit(self, grp):
        """把"当前已攒文本"送一次翻译。中间版失败静默忽略（终版随后到达），
        队列满时 translator.submit(spec=True) 放弃自己、绝不挤掉终版。"""
        if not grp or not self._spec_enabled():
            return
        lang = self._spec_source_lang()
        if not lang:
            # v2.18.2（D-3）：语言未知——本拍不送（终版路径不受影响）
            return
        combined = self._combine_pieces(grp)
        if not combined.strip():
            return
        tr = self._active_translate()
        if tr is None or not tr.isRunning():
            return
        self._spec_inflight = getattr(self, "_spec_inflight", {})
        self._spec_inflight[combined] = (getattr(self, "_tgroup_gen", 0), list(grp))
        self._spec_ts = getattr(self, "_spec_ts", {})
        self._spec_ts[combined] = time.monotonic()
        tr.submit(combined, lang, spec=True)

    def _peek_pending(self, source_text):
        """v2.7.6（A）：按原文查待决卡但**不摘走**——推测版更新必须保住配对，
        整句终版还要靠它找到同一张卡做原地覆盖。"""
        pend = getattr(self, "_pending", None) or []
        for txt, card in pend:
            if txt == source_text and card.is_pending():
                return card
        for txt, card in pend:
            if txt == source_text:
                return card
        return None

    def _on_spec_translated(self, source_text, translated, engine, detected, error):
        """推测中间版译文回调：**只原地更新译文**。不终态化、不摘 pending、
        不计会话条数、不切聚焦态、不动失败横幅——这些全部留给整句终版。
        组已冲刷（gen 变化）则丢弃：终版已提交，马上会带完整译文到达。"""
        if not self._session_ok(getattr(self, "_sid_tr", None)):
            return
        spec = getattr(self, "_spec_inflight", None) or {}
        ent = spec.pop(source_text, None)
        t0 = getattr(self, "_spec_ts", {}).pop(source_text, None)
        if ent is None:
            # v2.13.0：草稿推测翻译的回复不在 _spec_inflight 簿记里——
            # 按"仍是最新草稿全文"配对（一次性消费防迟到同文重复覆盖）；
            # 只更新 dual 译文区淡色草稿态，不碰卡片/配对/计数，
            # 片片段推测版与整句终版随后自然覆盖
            if getattr(self, "_dual_draft", None) == source_text:
                self._dual_draft = None
                if (not error and translated and self.overlay.is_dual()
                        and getattr(self, "running", False)):
                    # v2.19.1：带上草稿原文做配对——该句若已终版收口，迟到回复作废
                    self.overlay.update_dual_draft_tgt(translated, source_text)
            return
        gen, pieces = ent
        if gen != getattr(self, "_tgroup_gen", 0):
            return
        if error or not translated or not pieces:
            return
        if t0 is not None:
            self._lat_add("_lat_spec", max(0.0, time.monotonic() - t0))
        show_source = bool(self.config.get("show_source"))
        card = self._peek_pending(pieces[-1])
        if card is not None:
            card.set_spec_result(translated, engine, detected, show_source)
        if self.overlay.isVisible():
            self.overlay.update_spec_result(source_text, translated, show_source)

    def _drop_translation(self, src_text, reason=ui_text("翻译队列繁忙，本句已跳过")):
        """v2.6.2（P1-7）：翻译队列满被挤掉的句子——占位卡/攒句簿记立即
        终态化，不再永久悬挂 "⟳ …"、不再累积悬挂引用。攒句合并句整组处理：
        末片卡显示终态文案、前片卡保持并入态。

        v2.20.4：`reason` 可传——"线程已死"与"队列挤爆"是两件事，写同一句
        「翻译队列繁忙」会把用户支到错误的下一步（去调队列/换引擎，而真相是
        翻译线程已经退出了）。"""
        if not src_text:
            return
        gmap = getattr(self, "_tgroup_by_src", None)
        pieces = None
        if gmap and src_text in gmap:
            pieces = gmap.pop(src_text)
        ts = getattr(self, "_submit_ts", {})
        ts.pop(src_text, None)
        if pieces:
            for c in pieces:
                ts.pop(c, None)   # 片级延迟簿记一并清
            card = self._take_pending(pieces[-1])
            if card is not None:
                card.set_failed(reason)
            for c in pieces[:-1]:
                pc = self._take_pending(c)
                if pc is not None:
                    pc.set_merged_away()
        else:
            card = self._take_pending(src_text)
            if card is not None:
                card.set_failed(reason)

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
        一行即可，不必再搭仪器（第十三轮三度折腾的教训）。随后清零。
        v2.7.6（A）：新增 spec 段——"碎片到达→推测译文上屏"，这是用户**实际
        感知**的译文延迟；hold 仍照旧记录（整句终版的攒句等待，用于对照）。"""
        reco = getattr(self, "_lat_reco", []) or []
        tr = getattr(self, "_lat_tr", []) or []
        hold = getattr(self, "_lat_hold", []) or []
        spec = getattr(self, "_lat_spec", []) or []
        if reco or tr or hold or spec:
            from app import log as app_log
            app_log.log(
                "pipeline.latency",
                n_reco=len(reco),
                reco_p50=round(_pct(reco, 0.50), 2),
                reco_p95=round(_pct(reco, 0.95), 2),
                n_tr=len(tr),
                tr_p50=round(_pct(tr, 0.50), 2),
                tr_p95=round(_pct(tr, 0.95), 2),
                tr_max=round(max(tr), 2) if tr else 0,
                n_hold=len(hold),
                hold_p50=round(_pct(hold, 0.50), 2) if hold else 0,
                hold_p95=round(_pct(hold, 0.95), 2) if hold else 0,
                n_spec=len(spec),
                spec_p50=round(_pct(spec, 0.50), 2) if spec else 0,
                spec_p95=round(_pct(spec, 0.95), 2) if spec else 0)
        self._lat_reco, self._lat_tr, self._lat_hold, self._submit_ts = [], [], [], {}
        self._lat_spec = []

    # ---------- v2.3.13（P14）：字幕卡右键一键纠错（词典可达性） ----------
    # 第八轮实测：误听词典做了八轮仍空——不是没工具，是"看到错→查原文→
    # 开设置→找词典→手打"链条太长。这里把纠错入口直接放卡片上：错的已
    # 预填，用户只打"对的"。

    def _new_card(self, text):
        """字幕卡工厂：统一挂右键纠错菜单（三处创建点共用）。"""
        return CaptionCard(text, on_menu=self._card_menu)

    def _insert_card(self, card):
        """卡片插入唯一入口 + 导出/清空按钮联动（v2.6.5 R7-L3）。"""
        self.scroll_layout.insertWidget(self.scroll_layout.count() - 1, card)
        self._sync_export_actions()

    def _sync_export_actions(self):
        has = self._has_cards()
        self.clear_button.setEnabled(has)
        self.export_button.setEnabled(has)
        try:
            self.overlay.set_session_has_content(has)   # v2.19.4：面板导出入口同步
        except Exception:
            pass
        tip = "" if has else ui_text("暂无字幕，开始翻译后自动可用")
        self.clear_button.setToolTip(tip or ui_text("清空当前会话的字幕记录"))
        self.export_button.setToolTip(tip or ui_text("把当前会话的双语字幕导出为文本文件"))

    def _card_menu(self, card):
        menu = QMenu(self)
        act = menu.addAction(ui_text("复制原文"))
        act.triggered.connect(lambda: QApplication.clipboard().setText(card.source_text))
        tr = card.translated_text()
        act = menu.addAction(ui_text("复制译文"))
        act.setEnabled(bool(tr))
        act.triggered.connect(lambda: QApplication.clipboard().setText(card.translated_text()))
        menu.addSeparator()
        act = menu.addAction(ui_text("纠正识别（加入误听词典）…"))
        act.triggered.connect(lambda: self._correct_from_card(card, "mishear_map"))
        act = menu.addAction(ui_text("纠正译文（加入译文修正词典）…"))
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
        title = ui_text("纠正识别") if dict_key == "mishear_map" else ui_text("纠正译文")
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
        v.addWidget(QLabel(ui_text("错误片段（已从字幕预填，删改到只剩要纠正的词句即可）：")))
        e_wrong = QLineEdit(wrong_prefill)
        v.addWidget(e_wrong)
        v.addWidget(QLabel(ui_text("正确文本：")))
        e_right = QLineEdit()
        e_right.setPlaceholderText(ui_text("例如：Norfolk"))
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
        # v2.7.4（C-11）：与设置页同一热更路径——此前卡片入口要"下次开始翻译"
        # 才生效，两入口行为分叉；现在运行中管线即时吃到新词条
        try:
            self.apply_pipeline_hotfix()
        except Exception:
            pass
        tip = ui_text("误听词典") if dict_key == "mishear_map" else ui_text("译文修正词典")
        self._set_alert(f"{ui_text('已保存到')}{tip}：「{wrong[:16]}」→「{right[:16]}{ui_text('」· 已即时生效')}")

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
        # v2.6.2（P1-4/P1-6）：running 守卫改为会话身份守卫——停止后排水
        # 链的尾句译文仍上屏；新会话中旧线程迟到结果按身份拦截
        if not self._session_ok(getattr(self, "_sid_tr", None)):
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
        merged_srcs = []
        combined_src = ""
        if gmap and source_text in gmap:
            combined_src = source_text       # v2.7.4（B-8）：面板收整句原文（与主窗卡片一致）
            pieces = gmap.pop(source_text)
            merged_srcs = list(pieces[:-1])   # v2.7.0（T1）：面板同步收编前片占位行
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
            self._insert_card(card)
            card.t_start, card.dur_s = getattr(self, "_last_asr_timing", (None, None))
            # v2.1.5：切回一次性上屏时清掉流式占位队列（防陈旧配对）
            if getattr(self, "_pending", None):
                self._pending.clear()
        else:
            card = self._take_pending(source_text)
            if card is None:
                card = self._new_card(source_text)
                self._insert_card(card)
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
        # v2.20.3：翻译失败的卡不计入"本次会话 N 条"——实测 4 条全 [翻译失败]
        # 时状态栏写"本次会话：4 条"，用户以为有 4 条字幕可导出（导出侧又会跳过
        # 失败卡，两边对不上更让人困惑）。
        if not error:
            self.session_count = getattr(self, "session_count", 0) + 1
            self.session_label.setText(f"{ui_text('本次会话：')}{self.session_count}{ui_text(' 条')}")
        if error:
            self._set_engine_status(
                f"{ui_text('⚠ 翻译失败（连续 ')}{self._fail_streak}{ui_text(' 条）：')}{error}")
            # v2.2.5：连续失败走彩色横幅（排障建议不被截断）
            advice = ui_text("检查网络/代理节点，或到「设置-翻译」测试通道 / 下载离线语言包")
            self._set_alert(
                f"{ui_text('⚠ 翻译连续失败 ')}{self._fail_streak}{ui_text(' 条 · ')}{advice}", error=True)
        else:
            self._set_engine_status(f"{ui_text('引擎：')}{engine}{ui_text(' · 源语言: ')}{detected or '?'}")
            self._set_alert(None)
        self.update_overlay_status()
        if error:
            # 连续 ≥3 条失败：状态行升级为通道级提示 + 悬浮条橙红常驻，
            # 直到有成功译文才恢复正常
            advice = ui_text("检查网络/代理节点，或到「设置-翻译」测试通道 / 下载离线语言包")
            self.overlay.set_status(
                f"{ui_text('翻译连续失败 ')}{self._fail_streak}{ui_text(' 条 · ')}{advice}" if self._fail_streak >= 3
                else ui_text("翻译失败 · 检查网络或切换引擎"),
                is_error=True)
        if self.overlay.isVisible():
            # v2.1.8：三档路由统一由 overlay.show_pending_result 内部分派
            # （跑马灯=淡入最新句；列表=占位补齐；单条=直接刷新）
            # v2.7.0（T1）：merged_from=攒句前片名单，面板据此收编对应占位行
            # v2.7.4（B-8）：source 用合并整句（与主窗卡片一致）——旧实现只给
            # 末片，面板行显示"半句话的原文配整句译文"，与主窗分叉
            self.overlay.show_pending_result(
                combined_src or source_text,
                translated or ("[" + engine + ui_text(" 翻译失败]")), show_source,
                merged_from=merged_srcs)
            # v2.20.0：dual 历史区已删除——终版句就地留在大字区显示，直到下一句
            # 的流式拍/片段触发**原子换句**（面板侧 _dual_cur_open 收口）。
            # 数据侧仍要清空：下一拍草稿从零起点续接，否则整句被再拼一遍
            if self.overlay.is_dual() and not error:
                self._dual_current = ""
        if self._follow_bottom():
            sb = self.scroll.verticalScrollBar()
            sb.setValue(sb.maximum())
        evicted = []
        while self.scroll_layout.count() - 1 > self.config.get("max_history"):
            item = self.scroll_layout.takeAt(0)
            w = item.widget()
            if w:
                # v2.20.3：淘汰前把这一行抄进导出台账。卡片是这场字幕的唯一副本，
                # 实测灌 200 段 / max_history=30 时状态栏写「本次会话：200 条」而
                # 导出只有最近 30 条——出厂 200 条上限配默认 4s 分段，十几分钟就
                # 翻车，前半程没有任何第二份副本。
                if isinstance(w, CaptionCard):
                    ledger = getattr(self, "_export_ledger", None)
                    if ledger is not None:
                        ledger.append(w.export_row())
                evicted.append(w)
                w.deleteLater()
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
        # v2.7.4（A-3）：退出场景主循环不再泵事件，stop 路径的
        # asr.finished→_on_asr_finished→close_input 链永不执行——
        # 翻译线程过去每次退出都被下面的孤儿 terminate 硬杀（持锁强杀
        # 有挂死向量，且攒批缓存 save 丢失）。手动关门+短等待其自然排水
        try:
            tr = self._active_translate()
            if tr is not None and tr.isRunning():
                tr.close_input()
                tr.wait(2000)
                if tr.isRunning():
                    # v2.7.5（R-1）：wait 超时后线程将被 terminate——线程末尾的
                    # _cache.save() 永远执行不到，本次会话新增缓存整批丢失。
                    # 主线程代刷（save 内部持锁，与翻译线程并发安全）再强杀
                    from app.translate.translator import _cache
                    try:
                        _cache.save()
                    except Exception:
                        pass
        except Exception:
            pass
        self._stop_prewarm()  # v2.6.1（P0-2）：预热线程随退出收尾
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
        box.setWindowTitle(ui_text("关闭 LiveSubtitle"))
        box.setText(ui_text("关闭软件后要做什么？"))
        box.setInformativeText(ui_text("隐藏到托盘后，字幕悬浮窗继续显示，可从右下角托盘图标重新打开主窗口。"))
        tray_btn = box.addButton(ui_text("隐藏到托盘"), QMessageBox.AcceptRole)
        exit_btn = box.addButton(ui_text("退出程序"), QMessageBox.DestructiveRole)
        cancel_btn = box.addButton(ui_text("取消"), QMessageBox.RejectRole)
        remember_box = QCheckBox(ui_text("记住我的选择，下次不再询问"))
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
                ui_text("LiveSubtitle 仍在运行"),
                ui_text("字幕悬浮窗继续工作。左键托盘图标恢复窗口，右键可退出/切来源；"
                "关闭行为可在「设置-通用」修改。"),
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
                ui_text("LiveSubtitle 已经在运行中。\n\n"
                "请点击任务栏右下角托盘区的 LiveSubtitle 图标打开主窗口。"))
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
