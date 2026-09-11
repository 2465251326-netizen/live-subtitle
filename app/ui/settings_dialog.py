"""独立设置窗口（微信 PC 版风格：左侧分类导航 + 右侧内容区）

「保存并应用」模式：改动先暂存（dirty tracking），底部操作栏
「保存并应用」统一落盘并按层生效——悬浮字幕样式实时预览；
管线类设置（模型/引擎/音频源）保存后自动重启管线。"""
import os
import re
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, QTimer, QPointF, QEvent, QUrl
from PySide6.QtGui import QColor, QMouseEvent, QIcon, QDesktopServices
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QVBoxLayout, QLabel, QPushButton, QComboBox,
    QCheckBox, QFrame, QGridLayout, QProgressBar, QSpinBox, QSlider,
    QListWidget, QListWidgetItem, QStackedWidget, QWidget, QMessageBox,
    QScrollArea, QStyle, QStyleOptionSlider, QLineEdit, QKeySequenceEdit,
    QFileDialog, QPlainTextEdit,
)

from app.config import LANGUAGES, TARGET_LANGS, APP_VERSION, DEFAULTS
from app.translate.translator import ArgosEngine, _cache
from app.translate.offline_pack import cleanup_temp_files
from app.translate import offline_pack as offline_pack_mod
from app.audio.capture import list_input_devices, list_output_devices
from app.ui.styles import SETTING_QSS

REPO_URL = "https://github.com/2465251326-netizen/live-subtitle"
DOCS_URL = REPO_URL + "#readme"

MODELS = [("tiny", "tiny · 最快 · 延迟约 2s"),
          ("base", "base · 流畅 · 中文较弱"),
          ("small", "small · 推荐（4 核以上）"),
          ("medium", "medium · 高精度 · 需好 CPU"),
          ("large-v3-turbo", "large-v3-turbo · 顶级精度 · 需 GPU/高配 CPU（约 6s+）")]


class ClickableSlider(QSlider):
    """点击轨道直接定位的滑条。

    Qt 默认点击轨道是 page step，不符合直觉。这里把左键点击换算为具体值，
    并合成一次 handle 上的按压事件交给 QSlider 原生逻辑，因此定位之后
    按住鼠标继续移动仍可正常拖动。仅水平方向启用，垂直方向走默认行为。
    """

    def mousePressEvent(self, event):
        if (event.button() == Qt.LeftButton and self.isEnabled()
                and self.orientation() == Qt.Horizontal
                and self.maximum() > self.minimum()):
            opt = QStyleOptionSlider()
            self.initStyleOption(opt)
            groove = self.style().subControlRect(
                QStyle.CC_Slider, opt, QStyle.SC_SliderGroove, self)
            handle = self.style().subControlRect(
                QStyle.CC_Slider, opt, QStyle.SC_SliderHandle, self)
            pos = event.position().toPoint()
            if groove.contains(pos) and not handle.contains(pos):
                span = max(1, groove.width() - handle.width())
                x = pos.x() - groove.x() - handle.width() / 2.0
                ratio = min(1.0, max(0.0, x / span))
                self.setValue(self.minimum()
                              + round(ratio * (self.maximum() - self.minimum())))
                # 把本次按下位置平移到 handle 中心，交给原生逻辑接管后续拖动
                synthetic = QMouseEvent(
                    QEvent.MouseButtonPress,
                    QPointF(handle.center()), event.globalPosition(),
                    Qt.LeftButton, Qt.LeftButton, event.modifiers())
                super().mousePressEvent(synthetic)
                event.accept()
                return
        super().mousePressEvent(event)


class ModelDownloadWorker(QThread):
    """faster-whisper 模型下载/取消 worker（v2.0.5）。

    执行体在 app.asr.engine.download_model_files：逐文件下载、文件之间
    检查取消标志，使用与运行时加载完全一致的标准 HF 缓存布局
    （HF_HOME/hub），完成后 model_cached 判定即通过。
    取消粒度 = 当前文件下载完成后停止（单文件内部无法中断；
    hf_hub 断点续传保证已下载部分下次继续有效）。
    """

    progress_text = Signal(str)
    progress_pct = Signal(int)
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(self, model_size, parent=None):
        super().__init__(parent)
        self.model_size = model_size
        self._cancel = False
        self._cancelled = False

    def cancel(self):
        self._cancel = True

    def was_cancelled(self):
        return self._cancelled

    def run(self):
        from app.asr.engine import AsrThread, download_model_files
        if AsrThread.model_cached(self.model_size):
            self.finished_ok.emit(f"{self.model_size} 模型已就绪")
            return
        try:
            # 与管线路径同款前置：HF 端点（镜像探测）+ 代理环境变量同步
            from app.config import ensure_hf_endpoint_ready
            ensure_hf_endpoint_ready()
            from app import net as _net
            _net.apply_proxy_env()
        except Exception:
            pass

        def _progress(i, total, name):
            self.progress_text.emit(f"正在下载模型文件（{i}/{total}）：{name}")
            self.progress_pct.emit(int(i * 100 / max(total, 1)))

        try:
            outcome = download_model_files(
                self.model_size, should_stop=lambda: self._cancel,
                progress=_progress)
        except Exception as e:
            if self._cancel:
                self._cancelled = True
                self.finished_ok.emit("下载已取消（已下载部分保留，下次继续）")
                return
            from app.errors import friendly_error
            self.failed.emit(f"模型下载失败：{friendly_error(e)}")
            return
        if outcome == "stopped" or self._cancel:
            self._cancelled = True
            self.finished_ok.emit("下载已取消（已下载部分保留，下次继续）")
            return
        self.progress_pct.emit(100)
        self.finished_ok.emit(f"{self.model_size} 模型下载完成——下次开始翻译即离线可用")


# ---------- 设置字段声明表（v2.0.6，单一登记处） ----------
# key: (group, kind)
#   group:   pipeline=保存后重启管线 | overlay=悬浮字幕外观统一重放 |
#            instant=直接生效 | internal=不出现在恢复默认/暂存清单
#   kind:    仅为文档标注控件类型（combo/check/spin/slider/text/keyseq/hidden）
# 此前 _PIPELINE_KEYS / _OVERLAY_KEYS / _STAGE_ORDER 三处手写清单彼此漂移，
# v2.0.1"恢复默认漏 4 键"即此根因；现改为一处登记、其余派生：
# 新增设置项 = 在 DEFAULTS 加键 + 在此加一行，三类清单自动同步。
_FIELD_SPECS = {
    "source_type":          ("pipeline", "combo"),
    "device_index":         ("pipeline", "hidden"),
    "device_name":          ("pipeline", "hidden"),
    "asr_model":            ("pipeline", "combo"),
    "asr_device":           ("pipeline", "combo"),
    "asr_language":         ("pipeline", "combo"),
    "hallucination_filter": ("pipeline", "check"),
    "silero_vad":           ("pipeline", "check"),
    "low_latency_mode":     ("pipeline", "check"),
    "prewarm_model":        ("instant", "check"),
    "mishear_map":          ("pipeline", "mishear"),
    "translate_fix_map":    ("pipeline", "mishear"),
    "engine":               ("pipeline", "combo"),
    "target_lang":          ("pipeline", "combo"),
    "proxy_mode":           ("instant", "combo"),
    "proxy_url":            ("instant", "text"),
    "hotkey_enabled":       ("instant", "check"),
    "hotkey_sequence":      ("instant", "keyseq"),
    "hotkey_overlay":       ("instant", "keyseq"),
    "overlay_enabled":      ("overlay", "check"),
    "show_source":          ("overlay", "check"),
    "overlay_font_size":    ("overlay", "spin"),
    "overlay_text_color":   ("overlay", "color"),
    "overlay_bg_color":     ("overlay", "color"),
    "overlay_bg_opacity":   ("overlay", "slider"),
    # v2.4.0 退役：outline 三件套 / list_mode / list_max / stream / click_through
    "close_action":         ("instant", "combo"),
    "auto_start":           ("instant", "check"),
    "max_history":          ("instant", "spin"),
    "translate_zh_from_zh": ("instant", "hidden"),
    "instant_caption":      ("instant", "check"),
    "overlay_x":            ("internal", "hidden"),
    "overlay_y":            ("internal", "hidden"),
    "storage_root":         ("internal", "hidden"),
    "wizard_done":          ("internal", "hidden"),
}

# ---------------------------------------------------------------------------
# v2.3.0 声明式标准设置行：新增一个"键→单控件"型设置 = 三处数据登记
#   ① config.DEFAULTS  ② _FIELD_SPECS  ③ 本表
# 控件构建、信号接线（默认 _stage / 指定 on_change）、同步点回显
# （_set_widgets_from 与 load_from_config）全部由本表驱动。
# 复合控件保持手写：模型下拉+管理按钮、颜色选择、透明度滑条、代理行、
# 热键输入、误听词典、设备选择。历史上"两处同步清单漏一处"是 bug 高发区
# （v2.2.12 加一个开关要改 5 处），本表把这类键收敛到 1 处。
# 字段：key=配置键 attr=控件属性名 page/section=落位 title/desc=行文案
#       kind=check|spin|combo  opts=range/items/on_change
_STD_ROW_ITEMS = {
    "asr_lang": [("自动检测", "auto")] + [
        (name, code) for code, name in LANGUAGES.items()
        if code not in ("zh-CN", "zh-TW", "auto")],
    # v2.3.0 顺带修正：原手写版循环未排除 "auto"，下拉里"自动检测"出现两次
    "compute": [("CPU 模式（通用）", "cpu"),
                ("强制 GPU（需先装好 CUDA 运行时）", "cuda"),
                ("自动（安全档 = 优先 CPU 稳定运行）", "auto")],
    "engine": [("自动探测（推荐）", "auto"), ("Google 免费接口（在线）", "google"),
               ("MyMemory（在线备援）", "mymemory"), ("Argos 离线语言包", "argos")],
    "target": [(LANGUAGES.get(code, code), code) for code in TARGET_LANGS],
    "close": [("每次询问", "ask"), ("隐藏到托盘（字幕继续）", "tray"),
              ("直接退出程序", "exit")],
}

_STD_ROWS = [
    {"key": "asr_language", "attr": "asr_lang_combo", "page": "asr", "section": "语言与计算",
     "kind": "combo", "title": "识别语言",
     "desc": "「自动检测」会在第一句话后锁定说话语言，换语言视频无感切换；已知语言时手动锁定更快更稳。",
     "opts": {"items": _STD_ROW_ITEMS["asr_lang"]}},
    {"key": "asr_device", "attr": "compute_combo", "page": "asr", "section": "语言与计算",
     "kind": "combo", "title": "计算方式",
     "desc": "「强制 GPU」= 只用 NVIDIA 显卡加速（需先「检测 GPU 环境」并安装 CUDA 版 PyTorch；"
             "不可用时自动回落 CPU 并在就绪提示中说明原因）。"
             "「自动」= 稳定优先的 CPU 模式。无独显或打包版保持 CPU 即可实时。",
     "opts": {"items": _STD_ROW_ITEMS["compute"]}},
    {"key": "hallucination_filter", "attr": "hallucination_check", "page": "asr", "section": "语言与计算",
     "kind": "check", "title": "幻觉抑制",
     "desc": "自动丢弃音乐/噪声段的胡言乱语字幕（推荐开启；若发现正常语音被误丢可关闭）。"},
    {"key": "silero_vad", "attr": "silero_check", "page": "asr", "section": "语言与计算",
     "kind": "check", "title": "Silero VAD（段内净化）",
     "desc": "在识别前用 Silero 模型过滤段内非语音（背景音乐/噪声更干净），与切句 VAD 双保险。"},
    {"key": "low_latency_mode", "attr": "low_latency_check", "page": "asr", "section": "语言与计算",
     "kind": "check", "title": "低延迟模式（直播/新闻推荐）",
     "desc": "字幕更快上屏：分段上限 14 秒→6 秒、静音判停收紧。v2.3.7 起翻译自动攒整句"
             "（上屏快、译文仍是完整句子，不再半截话各翻各的）；显示上句子可能切短。",
     "opts": {}},
    {"key": "prewarm_model", "attr": "prewarm_check", "page": "asr", "section": "语言与计算",
     "kind": "check", "title": "启动时预热模型",
     "desc": "打开软件即在后台把已下载的模型加载好，点「开始翻译」几乎秒就绪——"
             "否则每次冷启动要等 GPU 初始化近 1 分钟。仅预热已下载模型，不会自动下载；关闭可省显存占用。",
     "opts": {}},
    {"key": "engine", "attr": "engine_combo", "page": "translate", "section": "翻译方向",
     "kind": "combo", "title": "翻译引擎",
     "desc": "全部免费无需密钥。自动模式启动时探测在线接口并选用可达者、断网自动回退离线包；"
             "Argos 为完全离线方案，逐句直译、多义词易翻错（通顺度低于在线），需先在下方下载语言包。",
     "opts": {"items": _STD_ROW_ITEMS["engine"], "on_change": "_on_engine_changed"}},
    {"key": "target_lang", "attr": "target_combo", "page": "translate", "section": "翻译方向",
     "kind": "combo", "title": "翻译目标语言",
     "desc": "在线引擎支持简繁中文、英、日、韩、法、德、西、俄、葡、意、泰、越、阿、印尼、印地共 16 种；"
             "离线语言包支持其中 15 种（暂缺繁体中文），选 Argos 引擎后可下载。",
     "opts": {"items": _STD_ROW_ITEMS["target"], "on_change": "_on_engine_changed"}},
    {"key": "overlay_enabled", "attr": "overlay_check", "page": "display", "section": "字幕显示",
     "kind": "check", "title": "启用字幕面板（置顶）",
     "desc": "悬浮在所有窗口之上的字幕面板：顶部工具条（目标语言/原文开关/字号/收起），"
             "正文是原文+译文成对的历史滚动区，上滚暂停自动跟随。整板可拖、右缘拖宽、"
             "双击工具条贴顶/底。托盘「显隐字幕面板」或热键（默认 Ctrl+Alt+O）随时可切。",
     "opts": {"on_change": "_on_overlay_toggle"}},
    {"key": "show_source", "attr": "show_source_check", "page": "display", "section": "字幕显示",
     "kind": "check", "title": "同时显示原文",
     "desc": "开启后字幕与字幕面板同时保留原语言文本（面板上也可一键开关）。"},
    {"key": "instant_caption", "attr": "instant_caption_check", "page": "display", "section": "上屏行为",
     "kind": "check", "title": "字幕流式上屏（原文先出）",
     "desc": "开启：识别文本立刻上屏（译文位置显示占位），译文就绪后原地补齐——听到哪看到哪。"
             "关闭：等识别+翻译都完成后一次性显示整条字幕（旧行为）。"},
    {"key": "close_action", "attr": "close_combo", "page": "general", "section": "窗口行为",
     "kind": "combo", "title": "点击关闭按钮时",
     "desc": "「隐藏到托盘」后主窗口消失，识别与翻译在后台继续，悬浮字幕正常显示，"
             "可从任务栏右下角托盘图标重新打开主窗口。",
     "opts": {"items": _STD_ROW_ITEMS["close"]}},
    {"key": "auto_start", "attr": "auto_start_check", "page": "general", "section": "窗口行为",
     "kind": "check", "title": "启动后自动开始翻译",
     "desc": "打开软件后自动按上次配置开始识别与翻译，适合固定场景挂机使用。"},
    {"key": "max_history", "attr": "max_history_spin", "page": "general", "section": "字幕记录",
     "kind": "spin", "title": "主窗口最多保留字幕条数",
     "desc": "超出后自动清理最早的记录，避免长时间运行占用内存。",
     "opts": {"range": (50, 500, 50)}},
]


class _ModelDetailDialog(QDialog):
    """单个识别模型的详情/操作弹窗（v2.0.5）。

    内容：模型介绍、当前状态（未下载/不完整/已下载+体积）、
    下载（逐文件可取消、断点续传、进度条）、删除。
    下载进行中禁止关闭窗口——worker 以本窗口为 parent，
    强行关闭会销毁运行中的 QThread（qFatal 风险）。
    """

    def __init__(self, parent, code, current_model, running):
        super().__init__(parent)
        self.code = code
        self.current_model = current_model
        self.running = running
        self.worker = None
        from app.ui.first_run import MODEL_INFO
        intro = next((f"{t}：{d}" for c, t, d in MODEL_INFO if c == code), code)
        self.setWindowTitle(f"模型详情 · {code}")
        self.resize(480, 250)
        v = QVBoxLayout(self)
        title = QLabel(intro)
        title.setObjectName("SettingTitle")
        title.setWordWrap(True)
        v.addWidget(title)
        self.state_lbl = QLabel()
        self.state_lbl.setObjectName("SettingDesc")
        self.state_lbl.setWordWrap(True)
        v.addWidget(self.state_lbl)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setVisible(False)
        v.addWidget(self.bar)
        self.dl_status = QLabel("")
        self.dl_status.setObjectName("SettingDesc")
        self.dl_status.setWordWrap(True)
        self.dl_status.setVisible(False)
        v.addWidget(self.dl_status)
        btns = QHBoxLayout()
        self.dl_btn = QPushButton("下载模型")
        self.dl_btn.clicked.connect(self._start_download)
        self.cancel_btn = QPushButton("取消下载")
        self.cancel_btn.setVisible(False)
        self.cancel_btn.clicked.connect(self._cancel_download)
        self.rm_btn = QPushButton("删除模型")
        self.rm_btn.clicked.connect(self._remove)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.close)
        btns.addWidget(self.dl_btn)
        btns.addWidget(self.cancel_btn)
        btns.addWidget(self.rm_btn)
        btns.addStretch()
        btns.addWidget(close_btn)
        v.addLayout(btns)
        self._refresh()

    def closeEvent(self, event):
        if self.worker is not None and self.worker.isRunning():
            event.ignore()
            QMessageBox.information(self, "下载进行中",
                                    "模型正在下载，请先「取消下载」或等待完成后再关闭。")
            return
        event.accept()

    def reject(self):
        # v2.2.1：Esc/系统关闭走 reject→done，完全不经过 closeEvent（Qt 官方
        # 文档确认该路径不可被 closeEvent 拦截）——下载中直接关窗会让运行中
        # 的 QThread 存活到程序退出时被销毁（qFatal 崩溃），且可双开下载。
        # 故在 reject 层同样拦截。
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.information(self, "下载进行中",
                                    "模型正在下载，请先「取消下载」或等待完成后再关闭。")
            return
        super().reject()

    def _refresh(self):
        from app.asr.engine import AsrThread
        state, mb = AsrThread.model_state(self.code)
        text = {
            "missing": "状态：未下载（可点「下载模型」提前下载；首次使用也会自动下载）",
            "partial": f"状态：下载不完整 · 已占 {mb:.0f} MB（残留半截文件，可删除或重新下载续传）",
            "full": f"状态：已下载 · {mb:.0f} MB",
        }[state]
        if self.code == self.current_model:
            text += " · 当前使用"
        self.state_lbl.setText(text)
        busy = self.worker is not None and self.worker.isRunning()
        self.dl_btn.setEnabled(state != "full" and not busy)
        self.rm_btn.setEnabled(state != "missing" and not busy)

    def _start_download(self):
        if self.worker is not None and self.worker.isRunning():
            return
        self.bar.setValue(0)
        self.bar.setVisible(True)
        self.dl_status.setText("正在连接下载源（首次可能需探测镜像）...")
        self.dl_status.setVisible(True)
        self.cancel_btn.setVisible(True)
        self.dl_btn.setEnabled(False)
        self.rm_btn.setEnabled(False)
        self.worker = ModelDownloadWorker(self.code, self)
        self.worker.progress_text.connect(self.dl_status.setText)
        self.worker.progress_pct.connect(self.bar.setValue)
        self.worker.finished_ok.connect(self._dl_done)
        self.worker.failed.connect(self._dl_fail)
        self._refresh()
        self.worker.start()

    def _cancel_download(self):
        if self.worker is not None and self.worker.isRunning():
            self.worker.cancel()
            self.dl_status.setText("正在取消（等待当前文件下载完成，最长可能数十秒）...")

    def _finish_dl(self):
        self.cancel_btn.setVisible(False)
        self._refresh()

    def _dl_done(self, msg):
        self.dl_status.setText(msg)
        self._finish_dl()

    def _dl_fail(self, msg):
        self.dl_status.setText(msg)
        self._finish_dl()

    def _remove(self):
        from app.asr.engine import AsrThread
        if self.code == self.current_model and self.running:
            QMessageBox.warning(self, "无法删除",
                                "该模型正在使用中，请先停止翻译再删除。")
            return
        box = QMessageBox(self)
        box.setWindowTitle("删除模型")
        box.setText(f"确定删除 {self.code} 模型的缓存文件（含未完成的下载残留）吗？")
        b_yes = box.addButton("删除", QMessageBox.DestructiveRole)
        box.addButton("取消", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() != b_yes:
            return
        AsrThread.remove_model(self.code)
        self.bar.setVisible(False)
        self.dl_status.setVisible(False)
        self._refresh()


class ArgosWorker(QThread):
    progress_text = Signal(str)
    progress_pct = Signal(int)
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(self, from_code, to_code, parent=None):
        super().__init__(parent)
        self.from_code = from_code
        self.to_code = to_code

    def run(self):
        try:
            # v2.0.7：worker 侧兜底——进入下载前再查一次已装方向，防 UI 层
            # 刷新时序窗口内的重复安装（实测同一包 22 秒内装了两次）
            if (self.from_code, self.to_code) in set(ArgosEngine.installed_pairs()):
                self.finished_ok.emit("该方向语言包已安装，无需重复下载")
                return
            self.progress_text.emit("正在获取语言包索引...")
            packs = ArgosEngine.available_packages()
            match = [p for p in packs if p.from_code == self.from_code and p.to_code == self.to_code]
            if not match:
                self.failed.emit(
                    f"未找到 {self.from_code} -> {self.to_code} 的离线语言包（该方向暂无离线包，可改用在线引擎）")
                return
            pkg = match[0]

            import time as _t
            from app.fmt import eta_text
            t0 = [None]

            def cb(pct):
                self.progress_pct.emit(pct)
                # v2.2.14：按 pct 时间差估算剩余（≥5% 才显示，起步误差大）
                now = _t.monotonic()
                if t0[0] is None:
                    t0[0] = now
                    return
                if 5 <= pct < 95:
                    el = now - t0[0]
                    t = eta_text(el * (100 - pct) / pct)
                    if t:
                        self.progress_text.emit(
                            f"正在下载语言包 {pkg.from_name} -> {pkg.to_name}"
                            f"（约 80MB，{pct}%，剩余约{t}）...")

            self.progress_text.emit(f"正在下载语言包 {pkg.from_name} -> {pkg.to_name}（约 80MB）...")
            ArgosEngine.install(pkg, progress_cb=cb)
            self.progress_pct.emit(100)
            self.finished_ok.emit(f"语言包 {pkg.from_name} -> {pkg.to_name} 安装成功，可离线使用")
        except Exception as e:
            self.failed.emit(f"语言包安装失败: {e}")


def _nav_item(text):
    item = QListWidgetItem(text)
    item.setSizeHint(item.sizeHint().__class__(0, 44))
    return item


class ProxyProbeWorker(QThread):
    """后台探测 Google 免费翻译通道连通性（避免卡 UI 线程）。"""
    done = Signal(bool, str)

    def run(self):
        from app.translate.translator import probe_engine
        from app import net
        try:
            ok, detail = probe_engine("google", timeout=4.0)
        except Exception as e:
            ok, detail = False, str(e)[:120]
        self.done.emit(bool(ok), detail)


class _StorageMigrateWorker(QThread):
    """后台迁移数据目录（模型可能数 GB，不能卡 UI）。"""
    done = Signal(str)
    fail = Signal(str)
    progress = Signal(str)

    def __init__(self, new_root, parent=None):
        super().__init__(parent)
        self.new_root = new_root

    def run(self):
        try:
            from app import storage
            new_root = storage.migrate_root(
                self.new_root, progress_cb=lambda m: self.progress.emit(m))
            self.done.emit(new_root)
        except Exception as e:
            self.fail.emit(str(e))


class _CudaInstallWorker(QThread):
    """后台安装 CUDA 推理运行时（仅源码运行模式提供）。

    v2.1.3：按 Python 版本智能选择方案——
    - Python ≤ 3.13：装 CUDA 版 PyTorch（约 2GB，官方 cu121 源）；
    - Python ≥ 3.14：PyTorch 官方源无对应轮子（用户实测 from versions:
      none），改装 NVIDIA 独立运行时包 nvidia-cublas-cu12 +
      nvidia-cudnn-cu12（约 700MB，PyPI 纯二进制轮子不挑 Python 版本）。
    两者都提供 CTranslate2 需要的 cuBLAS/cuDNN 动态库。
    """
    done = Signal(bool, str)

    def run(self):
        try:
            import subprocess
            py_minor = sys.version_info[1]
            if py_minor <= 13:
                cmd = [sys.executable, "-m", "pip", "install", "torch",
                       "--index-url", "https://download.pytorch.org/whl/cu121",
                       # 显式重装：CPU 版已存在时 pip 不会自行升级换 CUDA 版
                       "--force-reinstall"]
            else:
                cmd = [sys.executable, "-m", "pip", "install",
                       "nvidia-cublas-cu12==12.1.3.1", "nvidia-cudnn-cu12==9.1.1.17",
                       "--no-deps"]
            env = os.environ.copy()
            r = subprocess.run(
                cmd, capture_output=True, text=True, timeout=3600,
                encoding="utf-8", errors="replace", env=env)
            if r.returncode == 0:
                self.done.emit(True, "")
            else:
                tail = (r.stderr or r.stdout or "")[-400:]
                self.done.emit(False, tail)
        except Exception as e:
            self.done.emit(False, str(e))


def _version_tuple(s):
    """'1.8.1' -> (1, 8, 1)；解析失败返回空元组（视为最旧）。"""
    try:
        return tuple(int(x) for x in re.findall(r"\d+", str(s))[:3])
    except Exception:
        return ()


class _UpdateCheckWorker(QThread):
    """「版本与更新」页的三类在线检查：软件 / 模型 / 语言包。"""
    done = Signal(object, str)  # result, error

    def __init__(self, kind, model_size="", parent=None):
        super().__init__(parent)
        self.kind = kind
        self.model_size = model_size or "small"

    def run(self):
        import requests
        from app import net
        try:
            if self.kind == "app":
                r = requests.get(
                    "https://api.github.com/repos/2465251326-netizen/live-subtitle/releases/latest",
                    timeout=8, proxies=net.proxies(),
                    headers={"User-Agent": "LiveSubtitle-UpdateCheck"})
                if r.status_code == 404:
                    self.done.emit("", "")
                    return
                r.raise_for_status()
                tag = r.json().get("tag_name", "")
                self.done.emit(str(tag).lstrip("vV"), "")
            elif self.kind == "model":
                # v2.3.1：检查更新必须走 model_repo_id() 统一解析——large-v3-turbo
                # 在 Systran 下不存在（HF 对不存在仓库回 401，用户点"检查更新"
                # 必失败；v2.0.9 修下载/加载时漏了这条路径）。同时像下载一样做
                # 镜像回退：主站不可达时走 hf-mirror，不让用户干等超时
                from app.asr.engine import model_repo_id
                repo = model_repo_id(self.model_size)
                last_err = None
                for host in ("https://huggingface.co", "https://hf-mirror.com"):
                    try:
                        r = requests.get(f"{host}/api/models/{repo}", timeout=8,
                                         proxies=net.proxies(),
                                         headers={"User-Agent": "LiveSubtitle-UpdateCheck"})
                        r.raise_for_status()
                        self.done.emit(str(r.json().get("sha", ""))[:7], "")
                        last_err = None
                        break
                    except Exception as e:
                        last_err = e
                if last_err is not None:
                    raise last_err
            elif self.kind == "pack":
                from app.translate.offline_pack import fetch_index
                packs = fetch_index(timeout=8)
                self.done.emit(packs, "")
        except Exception as e:
            self.done.emit(None, str(e))


class SettingsDialog(QDialog):
    settings_saved = Signal()

    def __init__(self, main):
        super().__init__(None)
        self.main = main
        self.c = main.config
        self._staged = {}     # 待应用的改动 key -> value
        self._loading = False  # 界面重绘期间挂起暂存记录
        self.setWindowTitle("设置 · LiveSubtitle")
        self.setModal(False)
        self.resize(860, 580)
        self.setMinimumSize(760, 500)
        self.setStyleSheet(SETTING_QSS)

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        central = QWidget()
        central.setLayout(root)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(central, 1)

        # 底部操作栏：保存并应用模式的核心
        bar = QFrame()
        bar.setObjectName("ActionBar")
        bar.setFixedHeight(56)
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(18, 8, 18, 8)
        self.dirty_hint = QLabel("所有改动已保存")
        self.dirty_hint.setObjectName("DirtyHint")
        bar_layout.addWidget(self.dirty_hint)
        bar_layout.addStretch()
        self.reset_button = QPushButton("恢复默认")
        self.reset_button.setObjectName("GhostButton")
        self.reset_button.setCursor(Qt.PointingHandCursor)
        self.reset_button.clicked.connect(self._reset_defaults)
        bar_layout.addWidget(self.reset_button)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setObjectName("GhostButton")
        self.cancel_button.setCursor(Qt.PointingHandCursor)
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._discard_staged)
        bar_layout.addWidget(self.cancel_button)
        self.apply_button = QPushButton("保存并应用")
        self.apply_button.setObjectName("PrimaryButton")
        self.apply_button.setCursor(Qt.PointingHandCursor)
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self._apply_staged)
        bar_layout.addWidget(self.apply_button)
        outer.addWidget(bar)

        self.nav = QListWidget()
        self.nav.setObjectName("NavList")
        self.nav.setFixedWidth(174)
        for t in ("🎤 音频输入", "🧠 语音识别", "🌐 翻译", "🖥 显示", "⚙ 通用",
                  "ℹ 版本与更新"):
            self.nav.addItem(_nav_item(t))

        # 建议6：导航列顶部搜索——输入关键词直接跳转对应页
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("🔍 搜索设置…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setObjectName("SearchBox")
        self.search_edit.setFixedHeight(34)
        self.search_edit.textChanged.connect(self._on_search_changed)
        self.search_hint = QLabel("")
        self.search_hint.setObjectName("SettingDesc")
        self.search_hint.setWordWrap(True)
        self.search_hint.hide()
        nav_wrap = QWidget()
        nav_wrap.setObjectName("NavWrap")
        nav_wrap.setFixedWidth(190)
        nav_box = QVBoxLayout(nav_wrap)
        nav_box.setContentsMargins(8, 8, 8, 8)
        nav_box.setSpacing(6)
        nav_box.addWidget(self.search_edit)
        nav_box.addWidget(self.nav, 1)
        nav_box.addWidget(self.search_hint)
        root.addWidget(nav_wrap)

        self.pages = QStackedWidget()
        self.pages.setObjectName("SettingPages")
        root.addWidget(self.pages, 1)

        self.pages.addWidget(self._page_audio())
        self.pages.addWidget(self._page_asr())
        self.pages.addWidget(self._page_translate())
        self.pages.addWidget(self._page_display())
        self.pages.addWidget(self._page_general())
        self.pages.addWidget(self._page_about())

        self.nav.currentRowChanged.connect(self._on_nav_changed)
        self.nav.setCurrentRow(0)
        self._build_search_index()

    def _on_nav_changed(self, index):
        self.pages.setCurrentIndex(index)

    def _fade_page(self):
        """页面切换动效占位：原 QGraphicsOpacityEffect 方案会破坏 Qt 可访问性
        子树（UIA/读屏器读不到页面内容），已移除；保留接口兼容。"""
        pass

    def _build_search_index(self):
        self._search_index = []
        for idx in range(self.pages.count()):
            page = self.pages.widget(idx)
            for t, d in getattr(page, "_rows_meta", []):
                self._search_index.append((idx, t, d))

    def _on_search_changed(self, text):
        q = (text or "").strip().lower()
        if not q:
            self.search_hint.hide()
            return
        hits = [(i, t) for (i, t, d) in self._search_index
                if q in t.lower() or q in d.lower()]
        if hits:
            first_page = hits[0][0]
            self.nav.setCurrentRow(first_page)
            names = "、".join(t for _, t in hits[:4])
            self.search_hint.setText(f"找到 {len(hits)} 项：{names}"
                                     + ("…" if len(hits) > 4 else ""))
        else:
            self.search_hint.setText("没有匹配的设置项")
        self.search_hint.show()

    # ---------- 通用小组件 ----------

    def _page(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setObjectName("SettingScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        inner.setObjectName("SettingInner")
        v = QVBoxLayout(inner)
        v.setContentsMargins(32, 26, 32, 26)
        v.setSpacing(18)
        scroll.setWidget(inner)
        outer.addWidget(scroll)
        page._inner_layout = v
        return page

    def _section(self, page, text):
        lab = QLabel(text)
        lab.setObjectName("SettingGroup")
        page._inner_layout.addWidget(lab)
        if not hasattr(page, "_rows_meta"):
            page._rows_meta = []
        page._rows_meta.append((text, ""))
        return lab

    def _row(self, page, title, desc, widget):
        if not hasattr(page, "_rows_meta"):
            page._rows_meta = []
        page._rows_meta.append((title, desc or ""))
        box = QVBoxLayout()
        box.setSpacing(4)
        h = QHBoxLayout()
        t = QLabel(title)
        t.setObjectName("SettingTitle")
        h.addWidget(t)
        h.addStretch()
        if widget is not None:
            widget.setMinimumWidth(230)
            # v2.2.4：紧凑控件统一 26px 高并垂直居中——勾选框/数值框/下拉/
            # 按钮与标题行对齐（此前参差，勾选框尤其违和）
            from PySide6.QtWidgets import QComboBox, QSpinBox, QCheckBox, QPushButton
            if isinstance(widget, (QComboBox, QSpinBox, QCheckBox, QPushButton)):
                widget.setFixedHeight(26)
                h.addWidget(widget, 0, Qt.AlignVCenter)
            else:
                h.addWidget(widget)
        box.addLayout(h)
        if desc:
            d = QLabel(desc)
            d.setObjectName("SettingDesc")
            d.setWordWrap(True)
            box.addWidget(d)
        sep = QFrame()
        sep.setObjectName("SettingSep")
        sep.setFixedHeight(1)
        page._inner_layout.addLayout(box)
        page._inner_layout.addWidget(sep)

    # ---------- 页面：音频输入 ----------

    def _page_audio(self):
        page = self._page()
        self._section(page, "音频来源")
        self.source_combo = QComboBox()
        self.source_combo.addItem("系统声音（正在播放的内容）", "system")
        self.source_combo.addItem("麦克风", "microphone")
        self._row(page, "音频来源",
                  "看视频 / 听会议选「系统声音」，直接抓取电脑播放的一切声音；翻译别人说话选「麦克风」。",
                  self.source_combo)
        self.device_combo = QComboBox()
        device_row = QHBoxLayout()
        device_row.setSpacing(6)
        device_row.addWidget(self.device_combo)
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.setFixedWidth(64)
        self.refresh_button.clicked.connect(self._load_devices)
        device_row.addWidget(self.refresh_button)
        wrap = QWidget()
        wrap.setLayout(device_row)
        # 只连接一次；_load_devices 会被反复调用，在其中连接会累积重复信号
        self.device_combo.currentIndexChanged.connect(self._on_device_changed)
        self._row(page, "输入设备",
                  "选择具体设备；更换耳机等设备后点「刷新」重新加载。蓝牙耳机的部分虚拟输出不支持抓取系统声音。",
                  wrap)
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)

        tip = QLabel("开始翻译后，主窗口底部会显示实时音量条；音量条不动说明抓错了设备。")
        tip.setObjectName("SettingDesc")
        tip.setWordWrap(True)
        page._inner_layout.addWidget(tip)
        page._inner_layout.addStretch()
        return page

    # ---------- 页面：语音识别 ----------

    def _page_asr(self):
        page = self._page()
        self._section(page, "识别模型")
        self.model_combo = QComboBox()
        tips = {
            "tiny": "75MB · 延迟约 2s · 中文易误判，适合纯英文+老电脑",
            "base": "145MB · 延迟约 2.5s · 中文较弱",
            "small": "480MB · 延迟约 3s · 中文良好，推荐 4 核以上 CPU",
            "medium": "1.5GB · 高精度 · 需较新多核 CPU 或 GPU（纯 CPU 慢机实时吃力，建议 small）",
            "large-v3-turbo": "约1.6GB · 顶级精度 · 需 GPU 或高配 CPU（约 6s+）",
        }
        # 与模块顶部 MODELS 常量保持同一来源，避免新增模型时漏进下拉（v1.9.2 修复）
        for code, label in MODELS:
            self.model_combo.addItem(label, code)
            self.model_combo.setItemData(self.model_combo.count() - 1, tips[code], Qt.ToolTipRole)
        model_row = QHBoxLayout()
        model_row.setSpacing(6)
        model_row.addWidget(self.model_combo, 1)
        self.model_manage_button = QPushButton("管理")
        self.model_manage_button.setFixedWidth(64)
        self.model_manage_button.clicked.connect(self._manage_models)
        model_row.addWidget(self.model_manage_button)
        model_wrap = QWidget()
        model_wrap.setLayout(model_row)
        self._row(page, "识别模型",
                  "全部在本地运行，语音不出电脑。首次选择后自动下载模型（一次性），之后永久离线可用。中文内容建议 small。",
                  model_wrap)

        self._section(page, "语言与计算")
        # v2.3.0：语言/计算/幻觉/Silero 四行改 _STD_ROWS 表驱动；
        # GPU 检测按钮为复合控件保持手写，插在两组之间
        self._std_rows(page, "asr", "语言与计算",
                       keys=("asr_language", "asr_device"))
        self.gpu_check_button = QPushButton("检测 GPU 环境")
        self.gpu_check_button.setFixedWidth(140)
        self.gpu_check_button.clicked.connect(self._show_gpu_guidance)
        self._row(page, "GPU / CUDA 配置",
                  "一键检测显卡、驱动与 CUDA 可用性，附配置教程与注意事项。",
                  self.gpu_check_button)
        self._std_rows(page, "asr", "语言与计算",
                       keys=("hallucination_filter", "silero_vad", "low_latency_mode",
                             "prewarm_model"))
        self._section(page, "识别质量调优")
        self.mishear_edit = QPlainTextEdit()
        self.mishear_edit.setPlaceholderText(
            "每行一条误听修正：错误文本=正确文本\n例如：\nfeline=feel in\n芯片组=新奇点")
        self.mishear_edit.setMaximumHeight(90)
        self._row(page, "误听修正词典",
                  "对识别结果做精确替换（建议2/建议5 质量调优）；保存并应用后对后续字幕生效。",
                  self.mishear_edit)
        self.mishear_edit.textChanged.connect(self._mishear_text_changed)

        # v2.3.0：model_combo 仍手写接线（复合控件）；asr_lang/compute/
        # hallucination/silero 四行的接线已由 _std_wire 完成，此处不得重复挂
        self.model_combo.currentIndexChanged.connect(
            lambda _i: self._stage_combo("asr_model", self.model_combo))
        page._inner_layout.addStretch()
        return page

    # ---------- 页面：翻译 ----------

    def _page_translate(self):
        page = self._page()
        self._section(page, "翻译方向")
        # v2.3.0：引擎/目标语言两行改 _STD_ROWS 表驱动（on_change=_on_engine_changed）
        self._std_rows(page, "translate", "翻译方向")

        self._section(page, "译文质量")
        # v2.3.6（P7）：译文修正词典——误听词典的对偶，识别侧修"听错"，这里修"翻错/翻反"
        self.tfix_edit = QPlainTextEdit()
        self.tfix_edit.setPlaceholderText(
            "每行一条译文修正：错误译文=正确译文\n例如：\n"
            "加快人工智能的发展速度=控制人工智能的发展节奏")
        self.tfix_edit.setMaximumHeight(90)
        self._row(page, "译文修正词典",
                  "对翻译结果做精确替换——引擎翻错/翻反的多义词可人工纠偏；"
                  "保存并应用后对后续字幕生效（含缓存里的旧译文也会被修正显示）。与识别侧「误听修正词典」对偶。",
                  self.tfix_edit)
        self.tfix_edit.textChanged.connect(self._tfix_text_changed)

        self._section(page, "网络代理")
        proxy_hint = QLabel(
            "「跟随系统」读取 Windows 系统代理（v2rayN / Clash 开启系统代理即可用）。\n"
            "Google 免费接口国内直连不可达，走代理后翻译质量显著提升。")
        proxy_hint.setObjectName("SettingDesc")
        proxy_hint.setWordWrap(True)
        page._inner_layout.addWidget(proxy_hint)

        self.proxy_combo = QComboBox()
        self.proxy_combo.addItem("跟随系统代理（推荐）", "system")
        self.proxy_combo.addItem("手动指定", "manual")
        self.proxy_combo.addItem("不使用代理（直连）", "none")
        self._row(page, "代理模式",
                  "手动指定适合代理软件未开启系统代理、或需要端口转发的场景。",
                  self.proxy_combo)
        self.proxy_url_edit = QLineEdit()
        self.proxy_url_edit.setPlaceholderText("例如 http://127.0.0.1:10808")
        self.proxy_test_button = QPushButton("测试 Google 通道")
        self.proxy_test_button.setFixedWidth(140)
        proxy_row = QHBoxLayout()
        proxy_row.setSpacing(6)
        proxy_row.addWidget(self.proxy_url_edit, 1)
        proxy_row.addWidget(self.proxy_test_button)
        proxy_wrap = QWidget()
        proxy_wrap.setLayout(proxy_row)
        self._row(page, "手动代理地址",
                  "仅「手动指定」模式需要填写；支持 http/https/socks5（socks5 需安装 pysocks）。",
                  proxy_wrap)
        self.proxy_status_label = QLabel("")
        self.proxy_status_label.setObjectName("SettingDesc")
        self.proxy_status_label.setWordWrap(True)
        page._inner_layout.addWidget(self.proxy_status_label)

        self.proxy_combo.currentIndexChanged.connect(self._on_proxy_mode_changed)
        self.proxy_url_edit.editingFinished.connect(self._on_proxy_url_changed)
        self.proxy_test_button.clicked.connect(self._test_proxy)

        self.argos_section_label = self._section(page, "离线语言包")
        try:
            cleanup_temp_files()
        except Exception:
            pass
        self.argos_hint = QLabel("选择 Argos 引擎后在此下载语言包（约 80MB / 包，一次下载永久离线使用）。"
                                 "离线包为机器直译风格，建议先开一条语音试试效果再决定常用。")
        self.argos_hint.setObjectName("SettingDesc")
        self.argos_hint.setWordWrap(True)
        page._inner_layout.addWidget(self.argos_hint)
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        self.argos_combo = QComboBox()
        grid.addWidget(self.argos_combo, 0, 0)
        self.argos_download_button = QPushButton("下载语言包")
        self.argos_download_button.clicked.connect(self._download_argos)
        grid.addWidget(self.argos_download_button, 0, 1)

        # 已安装语言包管理（建议3）：显示占用体积，支持单独卸载
        self._section(page, "已安装语言包管理")
        self.packs_list = QListWidget()
        self.packs_list.setObjectName("PacksList")
        self.packs_list.setMaximumHeight(150)
        page._inner_layout.addWidget(self.packs_list)
        self.pack_remove_button = QPushButton("卸载所选语言包")
        self.pack_remove_button.clicked.connect(self._remove_selected_pack)
        self.pack_remove_button.setEnabled(False)
        page._inner_layout.addWidget(self.pack_remove_button)
        self.packs_list.itemSelectionChanged.connect(
            lambda: self.pack_remove_button.setEnabled(bool(self.packs_list.selectedItems())))
        self.argos_progress = QProgressBar()
        self.argos_progress.setRange(0, 100)
        self.argos_progress.setVisible(False)
        grid.addWidget(self.argos_progress, 1, 0, 1, 2)
        page._inner_layout.addLayout(grid)

        self._refresh_argos_section()
        # v2.3.0：engine/target 接线已由 _std_wire(on_change) 完成，不得重复挂
        page._inner_layout.addStretch()
        return page

    # ---------- 页面：显示 ----------

    def _page_display(self):
        page = self._page()
        self._section(page, "字幕显示")
        # v2.3.0：overlay_enabled(on_change=_on_overlay_toggle)/show_source
        # 改 _STD_ROWS 表驱动
        self._std_rows(page, "display", "字幕显示")

        self._section(page, "悬浮字幕样式")
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)

        grid.addWidget(self._gl("字号"), 0, 0)
        self.overlay_font_spin = QSpinBox()
        self.overlay_font_spin.setRange(12, 48)
        grid.addWidget(self.overlay_font_spin, 0, 1)

        grid.addWidget(self._gl("文字颜色"), 1, 0)
        self.text_color_button = QPushButton("选择")
        self.text_color_button.setObjectName("ColorPickButton")
        grid.addWidget(self.text_color_button, 1, 1)

        grid.addWidget(self._gl("背景颜色"), 2, 0)
        self.bg_color_button = QPushButton("选择")
        self.bg_color_button.setObjectName("ColorPickButton")
        grid.addWidget(self.bg_color_button, 2, 1)

        grid.addWidget(self._gl("背景透明度"), 3, 0)
        slider_row = QHBoxLayout()
        slider_row.setSpacing(8)
        self.bg_opacity_slider = ClickableSlider(Qt.Horizontal)
        self.bg_opacity_slider.setRange(0, 95)
        self.bg_opacity_label = QLabel("92%")
        self.bg_opacity_label.setObjectName("SettingDesc")
        slider_row.addWidget(self.bg_opacity_slider)
        slider_row.addWidget(self.bg_opacity_label)
        grid.addLayout(slider_row, 3, 1)
        # v2.4.0：字体描边三件套退役——面板是不透明板，描边是透明玻璃时代的补丁
        page._inner_layout.addLayout(grid)

        self._section(page, "上屏行为")
        # v2.3.0：stream/instant/list_mode/list_max 四行改表驱动
        # v2.4.0：面板形态下仅剩 instant_caption 一行
        self._std_rows(page, "display", "上屏行为")

        self.overlay_font_spin.valueChanged.connect(self._apply_overlay_style)
        self.bg_opacity_slider.valueChanged.connect(
            lambda v: (self.bg_opacity_label.setText(f"{v}%"), self._apply_overlay_style()))
        self.text_color_button.clicked.connect(lambda: self._pick_color("text"))
        self.bg_color_button.clicked.connect(lambda: self._pick_color("bg"))
        self._text_color = QColor(self.c.get("overlay_text_color"))
        self._bg_color = QColor(self.c.get("overlay_bg_color"))
        self._update_color_button(self.text_color_button, self._text_color)
        self._update_color_button(self.bg_color_button, self._bg_color)
        page._inner_layout.addStretch()
        return page

    # ---------- 页面：通用 ----------

    def _page_general(self):
        page = self._page()
        self._section(page, "窗口行为")
        # v2.3.0：close_action/auto_start/max_history 改 _STD_ROWS 表驱动
        self._std_rows(page, "general", "窗口行为")
        self._section(page, "字幕记录")
        self._std_rows(page, "general", "字幕记录", keys=("max_history",))
        clear_btn = QPushButton("清空翻译缓存")
        self._row(page, "翻译缓存",
                  "相同文本的翻译结果会本地缓存以加速显示；清空后下次重新翻译。不影响字幕记录。",
                  clear_btn)
        clear_btn.clicked.connect(self._clear_cache)

        # v2.0.0：升级/跳过过向导的用户可随时重看三步引导
        wizard_btn = QPushButton("重新运行首次向导")
        self._row(page, "新手引导",
                  "重新打开「音频源 → 模型 → 完成」三步向导（不会改动你的现有配置，确认步骤时可保持原样）。",
                  wizard_btn)
        wizard_btn.clicked.connect(self._rerun_wizard)

        self._section(page, "全局热键")
        self.hotkey_check = QCheckBox()
        self._row(page, "启用全局热键",
                  "看视频/开会时无需切回本窗口，任何界面按热键即可开始/停止翻译。",
                  self.hotkey_check)
        self.hotkey_edit = QKeySequenceEdit()
        self._row(page, "热键组合",
                  "默认 Ctrl+Alt+S，可改键。需含 Ctrl/Alt/Shift/Win 至少一个修饰键，避免影响正常打字。",
                  self.hotkey_edit)
        # v2.2.6：显隐悬浮条热键（可留空禁用）
        self.hotkey_overlay_edit = QKeySequenceEdit()
        self._row(page, "字幕面板显隐热键",
                  "默认 Ctrl+Alt+O，任何界面按键即可显示/隐藏字幕面板；清空后禁用该热键。",
                  self.hotkey_overlay_edit)
        self.hotkey_status = QLabel("")
        self.hotkey_status.setObjectName("SettingDesc")
        self.hotkey_status.setWordWrap(True)
        page._inner_layout.addWidget(self.hotkey_status)

        self._section(page, "存储位置")
        self.storage_hint = QLabel("")
        self.storage_hint.setObjectName("SettingDesc")
        self.storage_hint.setWordWrap(True)
        page._inner_layout.addWidget(self.storage_hint)
        storage_row = QHBoxLayout()
        storage_row.setSpacing(6)
        btn_change = QPushButton("更改位置…")
        self.storage_change_button = btn_change  # v2.0.1：迁移期间禁用
        btn_change.clicked.connect(self._change_storage_root)
        btn_open = QPushButton("打开目录")
        btn_open.clicked.connect(self._open_storage_dir)
        storage_row.addWidget(btn_change)
        storage_row.addWidget(btn_open)
        storage_row.addStretch()
        page._inner_layout.addLayout(storage_row)

        # v2.3.0：close/auto/max 三行接线已由 _std_wire 完成，不再重复挂；
        # 热键三控件为复合控件保持手写
        self.hotkey_check.toggled.connect(self._on_hotkey_enabled_changed)
        self.hotkey_edit.keySequenceChanged.connect(self._on_hotkey_sequence_changed)
        self.hotkey_overlay_edit.keySequenceChanged.connect(self._on_hotkey_overlay_changed)
        self.hotkey_overlay_edit.keySequenceChanged.connect(self._on_hotkey_overlay_changed)
        page._inner_layout.addStretch()
        return page

    # ---------- 全局热键 ----------

    def _rerun_wizard(self):
        """重新打开首启三步向导（默认项即当前配置，一路「下一步」无副作用）。"""
        # v2.4.4（BUG-4）：向导结束后会 load_from_config() 回填控件，未保存的
        # 暂存修改会被静默丢弃且状态栏误导性显示"所有改动已保存"（摸底实测
        # 丢失"强制 GPU"暂存）。先给用户保存/放弃的选择，不再无声吞掉。
        if getattr(self, "_staged", None):
            ret = QMessageBox.question(
                self, "未保存的修改",
                "当前有未保存的设置修改，运行向导会丢弃这些修改。\n\n"
                "要先「保存并应用」再运行向导吗？",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
            if ret == QMessageBox.Cancel:
                return
            if ret == QMessageBox.Yes:
                self._apply_staged()
        from app.ui.first_run import FirstRunWizard
        # v2.0.4：必须传 MainWindow——向导内部访问 self.main.config /
        # stop_pipeline / start_pipeline（first_run.py），传设置对话框自身
        # 会在构建模型页时 AttributeError（「重新运行首次向导」必崩）
        dlg = FirstRunWizard(self.main)
        dlg.exec()
        self.load_from_config()

    def _on_hotkey_enabled_changed(self, v):
        self._stage("hotkey_enabled", bool(v))

    def _on_hotkey_sequence_changed(self, seq):
        self._stage("hotkey_sequence", seq.toString())

    def _on_hotkey_overlay_changed(self, seq):
        # v2.2.6：显隐悬浮条热键——清空 = 禁用（存空串）
        self._stage("hotkey_overlay", seq.toString())

    def _apply_hotkey(self):
        """设置页状态提示：展示"当前配置"的热键状态（不含未保存的暂存值）。"""
        try:
            self.hotkey_status.setText(self.main.apply_hotkey_config())
        except Exception:
            pass

    # ---------- 存储位置（建议1） ----------

    def _refresh_storage_hint(self):
        try:
            from app import storage
            u = storage.usage_summary()
            total = u["hf_mb"] + u["argos_mb"] + u["cache_mb"]
            free = storage.disk_free_mb(u["root"])
            self.storage_hint.setText(
                f"当前：{u['root']}（模型 {u['hf_mb']:.0f} MB · 语言包 {u['argos_mb']:.0f} MB · "
                f"缓存 {u['cache_mb']:.1f} MB · 共 {total:.0f} MB；该盘剩余 {free / 1024:.1f} GB）")
        except Exception as e:
            self.storage_hint.setText(f"占用统计失败：{e}")

    def _open_storage_dir(self):
        from PySide6.QtCore import QUrl
        from app import config as cfg
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(cfg.CONFIG_DIR)))

    def _change_storage_root(self):
        # v2.0.1：迁移期间锁死入口——此前可并发触发第二次迁移/开始翻译，
        # 与迁移线程同时读写同一目录树导致数据错乱
        if getattr(self, "_storage_worker", None) and self._storage_worker.isRunning():
            QMessageBox.warning(self, "正在迁移",
                                "数据迁移正在进行中，请等待完成后再操作。")
            return
        if getattr(self.main, "running", False):
            QMessageBox.warning(self, "无法更改",
                                "翻译运行中不能迁移数据，请先停止翻译。")
            return
        new = QFileDialog.getExistingDirectory(
            self, "选择新的数据根目录（模型/语言包将迁移到此目录下）",
            str(self.c.get("storage_root") or ""))
        if not new:
            return
        from app import config as cfg
        if Path(new).resolve() == Path(cfg.CONFIG_DIR).resolve():
            return
        box = QMessageBox(self)
        box.setWindowTitle("迁移数据")
        box.setText(f"将把识别模型、语言包与缓存整体迁移到：\n{new}\n\n"
                    "迁移期间请勿关闭程序（模型可能数 GB，视磁盘速度需数分钟）。")
        b_go = box.addButton("开始迁移", QMessageBox.AcceptRole)
        box.addButton("取消", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() != b_go:
            return
        self.storage_change_button.setEnabled(False)
        self._storage_progress = QProgressBar()
        self._storage_progress.setRange(0, 0)  # 忙碌指示
        page_widget = self.storage_hint.parentWidget()
        if page_widget is not None:
            page_widget.layout().addWidget(self._storage_progress)
        self._storage_worker = _StorageMigrateWorker(new)
        self._storage_worker.progress.connect(lambda m: self.storage_hint.setText(m))
        self._storage_worker.done.connect(self._on_storage_migrated)
        self._storage_worker.fail.connect(self._on_storage_failed)
        self._storage_worker.start()

    def _on_storage_migrated(self, new_root):
        self._remove_storage_progress()
        self.storage_change_button.setEnabled(True)
        self.c.relocate(new_root)
        self._refresh_storage_hint()
        QMessageBox.information(
            self, "迁移完成",
            f"数据已迁移到：\n{new_root}\n\n重启程序后所有组件将完全使用新位置。")

    def _on_storage_failed(self, msg):
        self._remove_storage_progress()
        self.storage_change_button.setEnabled(True)
        QMessageBox.warning(self, "迁移失败",
                            f"{msg}\n\n已完成部分已尝试搬回原位置；"
                            "如仍提示空间不足，请更换目标盘或清理后重试。")

    def _remove_storage_progress(self):
        bar = getattr(self, "_storage_progress", None)
        if bar is not None:
            bar.setParent(None)
            bar.deleteLater()
            self._storage_progress = None

    # ---------- 识别模型管理 ----------

    def _manage_models(self):
        """模型管理对话框：各模型状态/体积；双击查看详情（下载/删除/进度）。

        v2.0.5：删除按钮仅在所选模型本地确有数据时可用（此前未下载也
        显示"删除所选模型"却删无可删）；新增双击详情弹窗
        （介绍/下载/取消下载/进度条，见 _ModelDetailDialog）。
        """
        from app.asr.engine import AsrThread

        dlg = QDialog(self)
        dlg.setWindowTitle("识别模型管理")
        dlg.resize(500, 360)
        v = QVBoxLayout(dlg)
        hint = QLabel("双击模型查看介绍并可下载/删除；模型只在本机运行，删除后下次选择会自动重新下载。")
        hint.setObjectName("SettingDesc")
        hint.setWordWrap(True)
        v.addWidget(hint)
        lst = QListWidget()
        v.addWidget(lst, 1)
        current = str(self.c.get("asr_model"))

        def status_text(code):
            state, mb = AsrThread.model_state(code)
            base = {
                "missing": "未下载（首次选择时自动下载，也可手动下载）",
                "partial": f"下载不完整 · 残留 {mb:.0f} MB（可删除或续传）",
                "full": f"已下载 · {mb:.0f} MB",
            }[state]
            if code == current:
                base += " · 当前使用"
            return base

        def label_of(code):
            return next(l for c, l in MODELS if c == code)

        for code, label in MODELS:
            item = QListWidgetItem(f"{label}\n    {status_text(code)}")
            item.setData(Qt.UserRole, code)
            lst.addItem(item)

        rm_btn = QPushButton("删除所选模型")
        rm_btn.setEnabled(False)

        def _sync_remove():
            it = lst.currentItem()
            enabled = (it is not None
                       and AsrThread.model_state(it.data(Qt.UserRole))[0] != "missing")
            rm_btn.setEnabled(enabled)

        def open_detail(item=None):
            it = item or lst.currentItem()
            if it is None:
                return
            code = it.data(Qt.UserRole)
            _ModelDetailDialog(dlg, code, current,
                               getattr(self.main, "running", False)).exec()
            # 详情窗关闭后刷新该行状态与按钮可用性
            it.setText(f"{label_of(code)}\n    {status_text(code)}")
            _sync_remove()

        def do_remove():
            it = lst.currentItem()
            if it is None:
                return
            code = it.data(Qt.UserRole)
            if code == current and getattr(self.main, "running", False):
                QMessageBox.warning(dlg, "无法删除",
                                    "该模型正在使用中，请先停止翻译再删除。")
                return
            box = QMessageBox(dlg)
            box.setWindowTitle("删除模型")
            box.setText(f"确定删除 {code} 模型的缓存文件（含未完成的下载残留）吗？")
            b_yes = box.addButton("删除", QMessageBox.DestructiveRole)
            box.addButton("取消", QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() != b_yes:
                return
            if AsrThread.remove_model(code):
                it.setText(f"{label_of(code)}\n    {status_text(code)}")
                _sync_remove()

        lst.itemDoubleClicked.connect(open_detail)
        lst.itemSelectionChanged.connect(_sync_remove)
        rm_btn.clicked.connect(do_remove)
        v.addWidget(rm_btn)
        dlg.exec()

    def _mishear_text_changed(self):
        """QPlainTextEdit 无 editingFinished：textChanged + 400ms 防抖等效。"""
        timer = getattr(self, "_mishear_timer", None)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._stage_mishear)
            self._mishear_timer = timer
        timer.start(400)

    def _parse_dict_text(self, widget):
        """解析"错误=正确"逐行词典文本（误听/译文修正共用），只收合法行。"""
        mapping = {}
        for line in widget.toPlainText().splitlines():
            line = line.strip()
            if not line or "=" not in line:
                continue
            wrong, _, right = line.partition("=")
            wrong = wrong.strip()
            right = right.strip()
            if wrong and right:
                mapping[wrong] = right
        return mapping

    def _stage_mishear(self):
        """解析误听词典文本（每行 错误=正确），只暂存合法行。"""
        mapping = self._parse_dict_text(self.mishear_edit)
        if mapping != (self.c.get("mishear_map") or {}):
            self._stage("mishear_map", mapping)

    def _tfix_text_changed(self):
        timer = getattr(self, "_tfix_timer", None)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._stage_translate_fix)
            self._tfix_timer = timer
        timer.start(400)

    def _stage_translate_fix(self):
        mapping = self._parse_dict_text(self.tfix_edit)
        if mapping != (self.c.get("translate_fix_map") or {}):
            self._stage("translate_fix_map", mapping)

    def _mishear_to_text(self, mapping):
        return "\n".join(f"{k}={v}" for k, v in (mapping or {}).items())

    # ---------- GPU / CUDA 引导（建议4） ----------

    def _show_gpu_guidance(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("GPU / CUDA 环境检测")
        dlg.resize(620, 480)
        v = QVBoxLayout(dlg)
        title = QLabel("检测结果")
        title.setObjectName("SettingTitle")
        v.addWidget(title)
        # v2.0.1：检测挪到后台线程——nvidia-smi 子进程（超时 8s）+ ctranslate2
        # CUDA 枚举此前在 GUI 线程同步执行，驱动异常时界面冻结 8 秒以上
        summary = QLabel("正在检测（显卡 / 驱动 / CUDA 环境，最长约 10 秒）…")
        summary.setObjectName("SettingDesc")
        summary.setWordWrap(True)
        v.addWidget(summary)
        v.addWidget(self._sep())
        t = QLabel("配置教程与注意事项")
        t.setObjectName("SettingTitle")
        v.addWidget(t)
        body = QLabel("检测完成后显示。")
        body.setObjectName("SettingDesc")
        body.setWordWrap(True)
        v.addWidget(body, 1)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        install_btn = QPushButton("一键安装 CUDA 推理运行时")
        install_btn.setObjectName("PrimaryButton")
        install_btn.setVisible(False)
        install_btn.clicked.connect(lambda: self._install_cuda_torch(dlg, install_btn))
        btn_row.addWidget(install_btn)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(dlg.accept)
        btn_row.addWidget(close_btn)
        v.addLayout(btn_row)

        info_box = {}

        def _apply(info):
            info_box.update(info)
            vram = info.get("vram_mb") or 0
            torch_state = info.get("torch_cuda", "unknown")
            state_txt = {"cuda": "已装 CUDA 版（运行时就绪）",
                         "cpu": "已装 CPU 版（缺运行时）",
                         "missing": "未安装",
                         "unknown": "未知"}.get(torch_state, "未知")
            summary.setText(
                f"NVIDIA 显卡：{info['nvidia_gpu'] or '未检测到'}\n"
                f"驱动版本：{info['driver'] or '—'}\n"
                f"显存：{f'{vram} MB' if vram else '—'}\n"
                f"CUDA 可用设备数：{info['cuda_devices']}\n"
                f"CUDA 运行时（PyTorch）：{state_txt}\n"
                f"运行形态：{'打包版（内置 CPU 推理）' if info['frozen'] else '源码运行'}")
            from app import gpu as gpu_mod
            body.setText(gpu_mod.guidance_text(info))
            # v2.1.2：按钮显示条件修正——此前 cuda_devices==0 才显示，而
            # "驱动可见、运行时缺失"（cuda_devices=1，最需要装的机器）恰好
            # 被藏掉。现按运行时三态：missing/cpu 版/未知 → 显示安装按钮
            if not info["frozen"] and info["nvidia_gpu"] and torch_state != "cuda":
                install_btn.setVisible(True)
                install_btn.setText("升级为 CUDA 版运行时" if torch_state == "cpu"
                                    else "一键安装 CUDA 推理运行时")
            else:
                install_btn.setVisible(False)

        class _GpuDetectWorker(QThread):
            done = Signal(dict)

            def run(self):
                from app import gpu as gpu_mod
                self.done.emit(gpu_mod.detect())

        self._gpu_worker = _GpuDetectWorker()
        self._gpu_worker.done.connect(_apply)
        self._gpu_worker.start()
        dlg.exec()

    def _sep(self):
        sep = QFrame()
        sep.setObjectName("SettingSep")
        sep.setFixedHeight(1)
        return sep

    def _install_cuda_torch(self, parent_dlg, btn):
        """仅源码模式提供：后台安装 CUDA 推理运行时（cuDNN/cuBLAS）。"""
        box = QMessageBox(self)
        box.setWindowTitle("确认安装")
        box.setText("将从 PyTorch 官方源下载并安装 CUDA 12.1 版 PyTorch（约 2GB+）。\n"
                    "安装期间请保持网络与电源稳定，完成后需重启本程序生效。")
        b_go = box.addButton("开始安装", QMessageBox.AcceptRole)
        box.addButton("取消", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() != b_go:
            return
        btn.setEnabled(False)
        btn.setText("正在安装…")
        self._cuda_worker = _CudaInstallWorker()
        self._cuda_worker.done.connect(lambda ok, msg, b=btn: self._on_cuda_done(ok, msg, b))
        self._cuda_worker.start()

    def _on_cuda_done(self, ok, msg, btn):
        btn.setEnabled(True)
        btn.setText("一键安装 CUDA 推理运行时")
        if ok:
            QMessageBox.information(self, "安装完成",
                                    "CUDA 推理运行时（cuDNN/cuBLAS）已安装。\n"
                                    "请重启 LiveSubtitle，然后在「计算方式」选择「强制 GPU」。")
        else:
            QMessageBox.warning(self, "安装失败",
                                f"{msg}\n\n可稍后重试，或手动执行：\n"
                                "Python ≤ 3.13：pip install torch --index-url "
                                "https://download.pytorch.org/whl/cu121\n"
                                "Python ≥ 3.14：pip install nvidia-cublas-cu12==12.1.3.1 "
                                "nvidia-cudnn-cu12==9.1.1.17 --no-deps")

    # ---------- GPU / CUDA 引导结束 ----------

    def _page_about(self):
        page = self._page()

        # 品牌区
        brand = QVBoxLayout()
        brand.setSpacing(2)
        icon_label = QLabel()
        icon_label.setAlignment(Qt.AlignCenter)
        try:
            from app.ui.main_window import icon_path
            icon_label.setPixmap(QIcon(str(icon_path())).pixmap(64, 64))
        except Exception:
            pass
        name_label = QLabel("LiveSubtitle")
        name_label.setObjectName("AboutAppName")
        name_label.setAlignment(Qt.AlignCenter)
        ver_label = QLabel(f"实时字幕翻译 · v{APP_VERSION}")
        ver_label.setObjectName("AboutVersion")
        ver_label.setAlignment(Qt.AlignCenter)
        brand.addWidget(icon_label)
        brand.addWidget(name_label)
        brand.addWidget(ver_label)
        page._inner_layout.addLayout(brand)
        page._inner_layout.addSpacing(14)

        self._section(page, "软件更新")
        self.app_update_title = QLabel(f"当前 v{APP_VERSION}")
        self.app_update_title.setObjectName("SettingTitle")
        self.app_update_btn = QPushButton("检查新版本 →")
        self.app_update_btn.setFixedWidth(130)
        app_row = self._about_row("软件更新", "检查 GitHub Releases 上的最新版本", self.app_update_btn)
        page._inner_layout.addLayout(app_row)
        self.app_update_status = QLabel("")
        self.app_update_status.setObjectName("SettingDesc")
        self.app_update_status.setWordWrap(True)
        page._inner_layout.addWidget(self.app_update_status)

        self._section(page, "识别模型")
        self.model_update_btn = QPushButton("检查更新 →")
        self.model_update_btn.setFixedWidth(130)
        model_row = self._about_row("识别模型", "检查 HuggingFace 上模型是否有新版本", self.model_update_btn)
        page._inner_layout.addLayout(model_row)
        self.model_update_status = QLabel("")
        self.model_update_status.setObjectName("SettingDesc")
        self.model_update_status.setWordWrap(True)
        page._inner_layout.addWidget(self.model_update_status)

        self._section(page, "离线语言包")
        self.pack_update_btn = QPushButton("检查更新 →")
        self.pack_update_btn.setFixedWidth(130)
        pack_row = self._about_row("离线语言包", "检查 Argos 语言包索引中的最新版本", self.pack_update_btn)
        page._inner_layout.addLayout(pack_row)
        self.pack_update_status = QLabel("")
        self.pack_update_status.setObjectName("SettingDesc")
        self.pack_update_status.setWordWrap(True)
        page._inner_layout.addWidget(self.pack_update_status)

        self._section(page, "链接")
        self._about_link(page, "更新日志", "查看版本历史与改进内容", DOCS_URL)
        self._about_link(page, "问题反馈 / 源码仓库", "提交 Issue 或 Fork 贡献", REPO_URL)

        self.app_update_btn.clicked.connect(self._check_app_update)
        self.model_update_btn.clicked.connect(self._check_model_update)
        self.pack_update_btn.clicked.connect(self._check_pack_update)
        page._inner_layout.addStretch()
        return page

    def _about_row(self, title, desc, widget):
        box = QVBoxLayout()
        box.setSpacing(4)
        h = QHBoxLayout()
        t = QLabel(title)
        t.setObjectName("SettingTitle")
        h.addWidget(t)
        h.addStretch()
        h.addWidget(widget)
        box.addLayout(h)
        if desc:
            d = QLabel(desc)
            d.setObjectName("SettingDesc")
            d.setWordWrap(True)
            box.addWidget(d)
        return box

    def _about_link(self, page, title, desc, url):
        btn = QPushButton("打开 →")
        btn.setFixedWidth(130)
        btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(url)))
        row = self._about_row(title, desc, btn)
        page._inner_layout.addLayout(row)

    # ---------- 更新检查 ----------

    def _check_app_update(self):
        self.app_update_btn.setEnabled(False)
        self.app_update_status.setText("正在检查 GitHub Releases ...")
        self._app_check = _UpdateCheckWorker("app")
        self._app_check.done.connect(self._on_app_check_done)
        self._app_check.start()

    def _on_app_check_done(self, latest, err):
        self.app_update_btn.setEnabled(True)
        if err:
            self.app_update_status.setText(f"✗ 检查失败：{err}")
            return
        if not latest:
            self.app_update_status.setText("未获取到版本信息。")
            return
        if _version_tuple(latest) > _version_tuple(APP_VERSION):
            self.app_update_status.setText(
                f"🎉 发现新版本 v{latest}！可到 Releases 页下载安装包更新（保留用户数据）。")
            QDesktopServices.openUrl(QUrl(f"{REPO_URL}/releases"))
        else:
            self.app_update_status.setText(f"✓ 已是最新版本（最新 v{latest}）。")

    def _check_model_update(self):
        self.model_update_btn.setEnabled(False)
        self.model_update_status.setText("正在检查 HuggingFace 模型版本 ...")
        self._model_check = _UpdateCheckWorker("model", self.c.get("asr_model"))
        self._model_check.done.connect(self._on_model_check_done)
        self._model_check.start()

    def _on_model_check_done(self, rev, err):
        self.model_update_btn.setEnabled(True)
        cur = str(self.c.get("asr_model"))
        if err:
            self.model_update_status.setText(f"✗ 检查失败：{err}")
            return
        self.model_update_status.setText(
            f"✓ 当前使用 {cur} 模型，线上最新修订 {rev}。模型随首次下载固定，重装才会更新。")

    def _check_pack_update(self):
        self.pack_update_btn.setEnabled(False)
        self.pack_update_status.setText("正在检查 Argos 语言包索引 ...")
        self._pack_check = _UpdateCheckWorker("pack")
        self._pack_check.done.connect(self._on_pack_check_done)
        self._pack_check.start()

    def _on_pack_check_done(self, packs, err):
        self.pack_update_btn.setEnabled(True)
        if err:
            self.pack_update_status.setText(f"✗ 检查失败：{err}")
            return
        try:
            installed = ArgosEngine.installed_pairs()
        except Exception:
            installed = []
        self.pack_update_status.setText(
            f"✓ 索引可访问，共 {len(packs)} 个可用语言包；本机已安装 {len(installed)} 个"
            + ("（索引与本地均在线，无需更新）" if installed else "。"))

    # ---------- 页面：版本与更新结束 ----------

    # ---------- v2.3.0：标准设置行的表驱动构建/接线/回显 ----------

    def _std_rows(self, page, page_key, section, keys=None):
        """按 _STD_ROWS 构建落在 (page_key, section) 的标准控件行。
        keys 可限定子集——与手写复合控件交错的分节使用。"""
        for spec in _STD_ROWS:
            if spec["page"] != page_key or spec["section"] != section:
                continue
            if keys and spec["key"] not in keys:
                continue
            kind = spec["kind"]
            opts = spec.get("opts", {})
            if kind == "check":
                w = QCheckBox()
            elif kind == "spin":
                w = QSpinBox()
                rng = opts["range"]
                w.setRange(rng[0], rng[1])
                if len(rng) > 2:
                    w.setSingleStep(rng[2])
            else:
                w = QComboBox()
                for text, data in opts["items"]:
                    w.addItem(text, data)
            setattr(self, spec["attr"], w)
            self._row(page, spec["title"], spec["desc"], w)
            self._std_wire(spec, w)

    def _std_wire(self, spec, w):
        """默认接线=暂存语义（_stage）；on_change 指定处理器则完全交给它
        （处理器内部自行 _stage，与原手写行为一致）。"""
        key = spec["key"]
        kind = spec["kind"]
        handler = spec.get("opts", {}).get("on_change")
        if handler:
            fn = getattr(self, handler)
            if kind == "combo":
                w.currentIndexChanged.connect(fn)
            elif kind == "check":
                w.toggled.connect(fn)
            else:
                w.valueChanged.connect(fn)
            return
        if kind == "combo":
            w.currentIndexChanged.connect(lambda _i, k=key, c=w: self._stage_combo(k, c))
        elif kind == "spin":
            w.valueChanged.connect(lambda v, k=key: self._stage(k, int(v)))
        else:
            w.toggled.connect(lambda v, k=key: self._stage(k, bool(v)))

    def _std_set(self, spec, values):
        """单个标准控件回显：values 优先、缺省回退配置值。"""
        w = getattr(self, spec["attr"], None)
        if w is None:
            return
        val = values.get(spec["key"], self.c.get(spec["key"]))
        kind = spec["kind"]
        if kind == "combo":
            idx = w.findData(val)
            if idx >= 0:
                w.setCurrentIndex(idx)
        elif kind == "spin":
            w.setValue(int(val))
        else:
            w.setChecked(bool(val))

    def _gl(self, text):
        lab = QLabel(text)
        lab.setObjectName("SettingTitle")
        return lab

    # ---------- 暂存与应用（保存并应用模式） ----------

    # v2.0.6：三类清单全部由模块级 _FIELD_SPECS 派生（单一登记处），
    # 不再手写三份彼此漂移的清单
    _PIPELINE_KEYS = {k for k, (g, _k) in _FIELD_SPECS.items() if g == "pipeline"}
    _OVERLAY_KEYS = {k for k, (g, _k) in _FIELD_SPECS.items() if g == "overlay"}
    _STAGE_ORDER = [k for k, (g, _k) in _FIELD_SPECS.items() if g != "internal"]

    def _stage(self, key, value):
        """暂存改动（不写配置不生效），等用户点「保存并应用」。"""
        if getattr(self, "_loading", False):
            return
        if self.c.get(key) == value:
            # 改回原值：撤销该项的暂存（脏状态如实收敛）
            if key in self._staged:
                self._staged.pop(key)
                self._mark_dirty()
                self._preview_overlay_style()
            return
        self._staged[key] = value
        self._mark_dirty()
        self._preview_overlay_style()

    def _stage_combo(self, key, combo):
        self._stage(key, combo.currentData())

    def _mark_dirty(self):
        dirty = bool(self._staged)
        self.apply_button.setEnabled(dirty)
        self.cancel_button.setEnabled(dirty)
        self.dirty_hint.setText("有未保存的修改" if dirty else "所有改动已保存")
        self.dirty_hint.setStyleSheet(
            "color: #ffb454; font-size: 12px; font-weight: 700;" if dirty
            else "color: #8a91a5; font-size: 12px;")

    def _preview_overlay_style(self):
        """悬浮字幕样式改动实时预览（不落盘），保存时才真正写入配置。"""
        staged = self._staged
        if not any(k.startswith("overlay_") or k == "show_source" for k in staged):
            return

        def g(key):
            return staged.get(key, self.c.get(key))

        self.main.overlay.apply_style(
            font_size=int(g("overlay_font_size")),
            text_color=g("overlay_text_color"),
            bg_color=g("overlay_bg_color"),
            bg_opacity=int(g("overlay_bg_opacity")),
        )

    def _apply_staged(self):
        """把暂存的改动写入配置并按分层生效。"""
        if not self._staged:
            return
        # v2.0.1：误听词典 400ms 防抖与保存竞态——输入后立即点保存时，
        # 此处先 flush 防抖定时器把词典内容补进暂存，否则"已保存并应用"
        # 提示后脏状态又出现（本轮改动未生效）
        timer = getattr(self, "_mishear_timer", None)
        if timer is not None and timer.isActive():
            timer.stop()
            self._stage_mishear()
        # v2.3.6（P7）：译文修正词典同款防抖 flush
        tf = getattr(self, "_tfix_timer", None)
        if tf is not None and tf.isActive():
            tf.stop()
            self._stage_translate_fix()
        order = list(self._STAGE_ORDER) + [k for k in self._staged if k not in self._STAGE_ORDER]
        applied = []
        for k in order:
            if k in self._staged:
                v = self._staged.pop(k)
                self.c.set(k, v)
                applied.append(k)
        self._mark_dirty()
        # 分层生效：悬浮字幕外观统一重放；热键重新注册；管线类改动重启管线
        if any(k.startswith("overlay_") or k == "show_source" for k in applied):
            self.main.apply_overlay_from_config()
        if "overlay_enabled" in applied:
            self.main.set_overlay_enabled(bool(self.c.get("overlay_enabled")))
        if "hotkey_enabled" in applied or "hotkey_sequence" in applied \
                or "hotkey_overlay" in applied:
            self.main.apply_hotkey_config()
        if self._PIPELINE_KEYS & set(applied):
            if self.main.running:
                self.main.stop_pipeline()
                self.main.start_pipeline()
                self.dirty_hint.setText("已保存并应用 · 管线已重启")
            else:
                self.dirty_hint.setText("已保存并应用 · 下次开始翻译时生效")
        else:
            self.dirty_hint.setText("已保存并应用")
        self.settings_saved.emit()

    def _discard_staged(self):
        """放弃暂存改动：重新从配置加载界面 + 还原悬浮条预览。"""
        self._staged.clear()
        self.load_from_config()
        # v2.0.1：还原悬浮条样式预览（docstring 一直承诺、实际漏做）——
        # 否则取消后悬浮条保持未保存的新样式
        try:
            self.main.apply_overlay_from_config()
        except Exception:
            pass
        # v2.2.1：还原悬浮条显隐预览——此前只还原样式不还原显隐，勾过
        # 「启用悬浮字幕条」再取消，悬浮条残留显示与配置相反
        try:
            self.main.set_overlay_visible(bool(self.c.get("overlay_enabled")))
        except Exception:
            pass

    def _reset_defaults(self):
        """全部设置项恢复为默认值（仅暂存，需点「保存并应用」才落盘）。

        v2.0.6：暂存清单由 _FIELD_SPECS 全量遍历——"恢复默认漏键"类 bug
        （v2.0.1 曾漏 4 个键）从结构上消除：新设置项进 specs 即自动被
        恢复默认覆盖，无需再记得改多个清单。
        """
        d = dict(DEFAULTS)
        self._suspend(lambda: self._set_widgets_from(d))
        # v2.0.1：误听词典防抖未触发的输入也要按默认值暂存
        timer = getattr(self, "_mishear_timer", None)
        if timer is not None:
            timer.stop()
        tf = getattr(self, "_tfix_timer", None)
        if tf is not None:
            tf.stop()
        for key, (group, kind) in _FIELD_SPECS.items():
            if group == "internal":
                continue
            cur = self.c.get(key)
            default = d.get(key)
            if kind == "mishear":
                if (cur or {}) != (default or {}):
                    self._staged[key] = dict(default or {})
            elif cur != default:
                self._staged[key] = default
        self._text_color = QColor(d["overlay_text_color"])
        self._bg_color = QColor(d["overlay_bg_color"])
        self._update_color_button(self.text_color_button, self._text_color)
        self._update_color_button(self.bg_color_button, self._bg_color)
        self._load_devices()
        self._refresh_argos_section()
        self._update_proxy_manual_enabled()
        self.hotkey_status.setText("已暂存默认值，点「保存并应用」生效")
        self._mark_dirty()
        self._preview_overlay_style()

    def _suspend(self, fn):
        """挂起暂存记录执行界面重绘。"""
        prev = getattr(self, "_loading", False)
        self._loading = True
        try:
            fn()
        finally:
            self._loading = prev

    def _set_widgets_from(self, values):
        """用 values（缺省回退配置值）刷新全部控件。
        v2.3.0：标准行统一走 _STD_ROWS 表回显，此处仅存复合控件。"""
        c = self.c

        def set_combo(combo, key):
            idx = combo.findData(values.get(key, c.get(key)))
            if idx >= 0:
                combo.setCurrentIndex(idx)

        set_combo(self.source_combo, "source_type")
        set_combo(self.model_combo, "asr_model")
        self.mishear_edit.setPlainText(self._mishear_to_text(values.get("mishear_map",
                                                                        c.get("mishear_map"))))
        self.tfix_edit.setPlainText(self._mishear_to_text(values.get("translate_fix_map",
                                                                     c.get("translate_fix_map"))))
        set_combo(self.proxy_combo, "proxy_mode")
        self.proxy_url_edit.setText(str(values.get("proxy_url", c.get("proxy_url")) or ""))
        self.overlay_font_spin.setValue(int(values.get("overlay_font_size", c.get("overlay_font_size"))))
        self.bg_opacity_slider.setValue(int(values.get("overlay_bg_opacity", c.get("overlay_bg_opacity"))))
        self.bg_opacity_label.setText(f"{self.bg_opacity_slider.value()}%")
        self.hotkey_check.setChecked(bool(values.get("hotkey_enabled", c.get("hotkey_enabled"))))
        self.hotkey_edit.setKeySequence(str(values.get("hotkey_sequence", c.get("hotkey_sequence") or "Ctrl+Alt+S")))
        # v2.2.6：显隐悬浮条热键（空 = 禁用）
        self.hotkey_overlay_edit.setKeySequence(str(values.get("hotkey_overlay", c.get("hotkey_overlay") or "")))
        for spec in _STD_ROWS:
            self._std_set(spec, values)

    def _confirm_discard(self):
        if not self._staged:
            return True
        box = QMessageBox(self)
        box.setWindowTitle("未保存的修改")
        box.setText("有未保存的设置修改，确定放弃并关闭吗？")
        b_discard = box.addButton("放弃修改", QMessageBox.DestructiveRole)
        box.addButton("返回继续编辑", QMessageBox.RejectRole)
        box.exec()
        return box.clickedButton() == b_discard

    def _flash_saved(self):
        # 保存并应用模式下不再使用"自动保存"闪现提示（保留接口兼容）
        pass

    def _on_source_changed(self):
        self._stage_combo("source_type", self.source_combo)
        # 切换采集来源时重置设备，避免把系统声音的回环设备索引带进麦克风模式（反之亦然）
        self._stage("device_index", -1)
        self._stage("device_name", "")
        self._load_devices()

    def _on_device_changed(self):
        """设备选择变更：索引与设备名一起暂存（v2.0.6 按名回查防热插拔漂移）。"""
        combo = self.device_combo
        if combo.currentData() is None:
            return
        self._stage("device_index", combo.currentData())
        self._stage("device_name", combo.currentText().replace(" [系统声音]", ""))

    def _on_engine_changed(self):
        self._stage_combo("engine", self.engine_combo)
        self._stage_combo("target_lang", self.target_combo)
        self._refresh_argos_section()

    # ---------- 网络代理 ----------

    def _on_proxy_mode_changed(self):
        self._stage_combo("proxy_mode", self.proxy_combo)
        self._update_proxy_manual_enabled()

    def _on_proxy_url_changed(self):
        url = self.proxy_url_edit.text().strip()
        if url != (self.c.get("proxy_url") or ""):
            self._stage("proxy_url", url)

    def _update_proxy_manual_enabled(self):
        manual = self.proxy_combo.currentData() == "manual"
        self.proxy_url_edit.setEnabled(manual)
        if not self.proxy_url_edit.text().strip():
            if manual:
                try:
                    from app import net as _net
                    sys_url = _net.system_proxy_url()
                except Exception:
                    sys_url = None
                self.proxy_url_edit.setPlaceholderText(
                    f"如 http://127.0.0.1:10808（检测到系统代理：{sys_url}）"
                    if sys_url else "如 http://127.0.0.1:10808")
            else:
                self.proxy_url_edit.setPlaceholderText("仅「手动指定」模式需要填写")

    def _test_proxy(self):
        self.proxy_test_button.setEnabled(False)
        self.proxy_status_label.setText("正在探测 Google 免费翻译通道（最长 4 秒）...")
        self._proxy_probe = ProxyProbeWorker()
        self._proxy_probe.done.connect(self._on_proxy_probe_done)
        self._proxy_probe.start()

    def _on_proxy_probe_done(self, ok, detail):
        from app import net
        self.proxy_test_button.setEnabled(True)
        if ok:
            self.proxy_status_label.setText(
                f"✓ Google 免费翻译通道可达（{detail} · 出口：{net.describe()}）")
        else:
            self.proxy_status_label.setText(
                f"✗ Google 通道不可达（{detail} · 出口：{net.describe()}）。"
                "不影响 MyMemory / Argos 备援通道。")

    def _on_overlay_toggle(self, checked):
        self._stage("overlay_enabled", bool(checked))
        if not getattr(self, "_loading", False):
            # v2.0.1：预览只切显隐，不落盘——此前 set_overlay_enabled 内
            # config.set 直接写配置，取消/关闭无法还原，绕过"保存并应用"契约
            self.main.set_overlay_visible(checked)

    def _apply_overlay_style(self, *_):
        self._stage("overlay_font_size", int(self.overlay_font_spin.value()))
        self._stage("overlay_text_color", self._text_color.name())
        self._stage("overlay_bg_color", self._bg_color.name())
        self._stage("overlay_bg_opacity", int(self.bg_opacity_slider.value()))

    def _pick_color(self, which):
        from PySide6.QtWidgets import QColorDialog
        cur = {"text": self._text_color, "bg": self._bg_color}[which]
        color = QColorDialog.getColor(cur, self, "选择颜色")
        if not color.isValid():
            return
        if which == "text":
            self._text_color = color
            self._update_color_button(self.text_color_button, color)
        else:
            self._bg_color = color
            self._update_color_button(self.bg_color_button, color)
        self._apply_overlay_style()

    def _update_color_button(self, btn, color):
        btn.setText(color.name().upper())
        btn.setStyleSheet(
            f"QPushButton#ColorPickButton {{ background-color: {color.name()}; "
            f"color: {'#111' if color.lightness() > 150 else '#fff'}; }}")

    def _clear_cache(self):
        _cache.clear()
        QMessageBox.information(self, "完成", "翻译缓存已清空。")

    def sync_overlay_check(self, checked):
        """悬浮字幕在设置窗口之外被开关时，同步本页复选框（外部改动=直接生效）。"""
        self.overlay_check.setChecked(bool(checked))

    def _load_devices(self):
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        source_type = self.source_combo.currentData()
        if source_type == "system":
            devices = list_output_devices()
        else:
            # 麦克风模式只列真实输入设备：回环设备混进来会被误选导致采集失败，
            # MME 的 "Sound Mapper"/"主声音捕获" 是虚拟映射项，对用户没有意义
            raw = [d for d in list_input_devices() if not d.get("loopback")]
            devices = [d for d in raw
                       if "sound mapper" not in d["name"].lower()
                       and "主声音" not in d["name"]]
        self._device_map = {}
        for d in devices:
            tag = " [系统声音]" if d.get("loopback") else ""
            label = f"{d['name']}{tag}"
            self._device_map[label] = d["index"]
            self.device_combo.addItem(label, d["index"])
        # 规范化去重：不同 Host API 对同一设备的命名常有截断/大小写差异
        # v2.0.1：改用 itemData（设备索引）做去重键——此前按文本前 20 字符
        # 截断去重，不同设备名前缀相同会被误合并，用户选不到目标设备
        seen = set()
        for i in range(self.device_combo.count() - 1, -1, -1):
            key = self.device_combo.itemData(i)
            if key in seen:
                self.device_combo.removeItem(i)
            else:
                seen.add(key)
        if source_type == "system":
            if not devices:
                self.device_combo.addItem("默认输出设备（自动）", -1)
        else:
            self.device_combo.addItem("默认麦克风（自动）", -1)
            # 去重：不同 Host API 会暴露同名设备，只保留首个（倒序删除避免索引跳动）
            seen = set()
            for i in range(self.device_combo.count() - 1, -1, -1):
                label = self.device_combo.itemText(i)
                if label in seen:
                    self.device_combo.removeItem(i)
                else:
                    seen.add(label)
        # v2.0.1：回填优先取暂存值——此前用配置值回填，用户改选未保存后点
        # "刷新"会把下拉框拉回旧值，与 _staged 脱节
        # v2.0.6：按名回查——索引会随设备热插拔漂移，先拿暂存/配置里的
        # device_name 匹配；名字不存在（设备已拔出/改名）时回落旧索引
        preferred_name = str(self._staged.get("device_name",
                              self.c.get("device_name") or "") or "")
        preferred = self._staged.get("device_index", self.c.get("device_index"))
        idx = -1
        if preferred_name:
            for i in range(self.device_combo.count()):
                if self.device_combo.itemText(i).replace(" [系统声音]", "") == preferred_name:
                    idx = i
                    break
        if idx < 0:
            idx = self.device_combo.findData(preferred)
        if idx >= 0:
            self.device_combo.setCurrentIndex(idx)
        else:
            # 找不到已存设备（如麦克风索引混入了系统声音列表）时落到安全的默认项
            if source_type == "system":
                self.device_combo.setCurrentIndex(0)
            else:
                self.device_combo.setCurrentIndex(self.device_combo.count() - 1)
            if not self._loading:
                self._stage("device_index", self.device_combo.currentData())
        self.device_combo.blockSignals(False)

    def _refresh_argos_section(self):
        is_argos = self.engine_combo.currentData() == "argos"
        section_label = getattr(self, "argos_section_label", None)
        for w in (self.argos_combo, self.argos_download_button, self.argos_hint, section_label):
            if w is not None:
                w.setVisible(is_argos)
        downloading = bool(getattr(self, "argos_worker", None) and self.argos_worker.isRunning())
        self.argos_progress.setVisible(is_argos and downloading)
        self._refresh_packs_list()
        if not is_argos:
            return
        tgt = self.target_combo.currentData() or "zh-CN"
        argos_tgt = "zh" if tgt.startswith("zh") else tgt
        tgt_name = LANGUAGES.get(tgt, argos_tgt)
        self.argos_combo.clear()
        # v2.0.0：候选源语言从翻译目标列表派生（单一数据源），不再手写副本漏项
        # v2.0.7：已安装的方向不再进下载列表（此前仅标"（已安装）"仍可重复
        # 下载，实测 22 秒内同一语言包被装了两次）——已装项在下方列表管理
        installed = set(ArgosEngine.installed_pairs())
        seen = set()
        for code in TARGET_LANGS:
            argos_src = "zh" if code.startswith("zh") else code
            if argos_src in seen or argos_src == argos_tgt:
                continue
            seen.add(argos_src)
            if (argos_src, argos_tgt) in installed:
                continue
            self.argos_combo.addItem(LANGUAGES.get(code, argos_src), argos_src)
        if self.argos_combo.count() == 0:
            self.argos_combo.addItem("该目标语言的方向均已安装", None)
            self.argos_download_button.setEnabled(False)
        elif not self.argos_download_button.isEnabled():
            self.argos_download_button.setEnabled(True)
        self.argos_download_button.setText(f"下载所选 → {tgt_name} 语言包")
        if installed:
            self.argos_hint.setText(
                f"已安装 {len(installed)} 个语言包；请下载与「识别语言 → 翻译目标」一致的方向，一次下载永久离线使用。"
                "注意：离线包为逐句直译，多义词/专有名词易翻错，追求通顺请用「自动」引擎。")
        else:
            self.argos_hint.setText(
                "请下载与「识别语言 → 翻译目标」一致的语言包（约 80MB，一次下载永久离线使用）。"
                "下载优先走本项目镜像，失败自动回退官方源。")

    def _refresh_packs_list(self):
        """已安装语言包列表（含体积），供卸载管理。"""
        lst = getattr(self, "packs_list", None)
        if lst is None:
            return
        lst.clear()
        try:
            sizes = offline_pack_mod.installed_sizes()
        except Exception:
            sizes = []
        for fc, tc, mb in sizes:
            name = f"{LANGUAGES.get(fc, fc)} → {LANGUAGES.get(tc, tc)}"
            item = QListWidgetItem(f"{name}  ·  {mb:.0f} MB")
            item.setData(Qt.UserRole, (fc, tc))
            lst.addItem(item)

    def _remove_selected_pack(self):
        sel = self.packs_list.currentItem()
        if sel is None:
            return
        fc, tc = sel.data(Qt.UserRole)
        name = sel.text().split("·")[0].strip()
        box = QMessageBox(self)
        box.setWindowTitle("卸载语言包")
        box.setText(f"确定卸载 {name} 语言包吗？\n\n删除后可随时重新下载（约 80MB）。")
        b_yes = box.addButton("卸载", QMessageBox.DestructiveRole)
        box.addButton("取消", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() != b_yes:
            return
        try:
            removed = offline_pack_mod.remove_pack(fc, tc)
        except Exception as e:
            QMessageBox.warning(self, "卸载失败", f"删除目录时出错：{e}")
            return
        if removed:
            self._refresh_packs_list()
            self._refresh_argos_section()
            QMessageBox.information(self, "完成", f"已卸载 {name}（释放 {sel.text().split('·')[-1].strip()}）。")
        else:
            self._refresh_packs_list()

    def _download_argos(self):
        code = self.argos_combo.currentData()
        if not code:
            return
        if getattr(self, "argos_worker", None) and self.argos_worker.isRunning():
            return
        tgt = self.target_combo.currentData() or "zh-CN"
        argos_tgt = "zh" if tgt.startswith("zh") else tgt
        # v2.0.7：硬校验防重复下载（UI 列表已排除已装方向，此处兜底防
        # 刷新时序窗口内的重复点击——实测同一语言包 22 秒内被装了两次）
        if (code, argos_tgt) in set(ArgosEngine.installed_pairs()):
            QMessageBox.information(
                self, "无需重复下载",
                f"{LANGUAGES.get(code, code)} → {LANGUAGES.get(tgt, argos_tgt)} 方向的语言包已安装。")
            self._refresh_argos_section()
            return
        tgt_name = LANGUAGES.get(tgt, argos_tgt)
        self.argos_download_button.setEnabled(False)
        self.argos_combo.setEnabled(False)
        self.argos_progress.setValue(0)
        self.argos_progress.setVisible(True)
        self.argos_hint.setText(f"正在安装离线语言包: {code} -> {tgt_name}")
        self.argos_worker = ArgosWorker(code, argos_tgt, self)
        self.argos_worker.progress_text.connect(
            lambda m: self.argos_hint.setText(m))
        self.argos_worker.progress_pct.connect(self.argos_progress.setValue)
        self.argos_worker.finished_ok.connect(self._on_argos_done)
        self.argos_worker.failed.connect(self._on_argos_failed)
        self.argos_worker.start()

    def _on_argos_done(self, msg):
        self.argos_download_button.setEnabled(True)
        self.argos_combo.setEnabled(True)
        self.argos_progress.setVisible(False)
        self._refresh_argos_section()
        self.argos_hint.setText(msg)
        self.settings_saved.emit()

    def _on_argos_failed(self, msg):
        self.argos_download_button.setEnabled(True)
        self.argos_combo.setEnabled(True)
        self.argos_progress.setVisible(False)
        self._refresh_argos_section()
        self.argos_hint.setText(msg)

    def closeEvent(self, event):
        # v2.0.1：迁移进行中同样只隐藏——进程退出会硬杀迁移线程，留下半迁移状态
        if getattr(self, "_storage_worker", None) and self._storage_worker.isRunning():
            self.hide()
            event.ignore()
            return
        if getattr(self, "argos_worker", None) and self.argos_worker.isRunning():
            self.hide()
            event.ignore()
            return
        if self._staged and not self._confirm_discard():
            event.ignore()
            return
        # 放弃改动时把悬浮条样式还原为已保存配置
        if self._staged:
            self._staged.clear()
            try:
                self.main.apply_overlay_from_config()
            except Exception:
                pass
            # v2.2.1：放弃改动时同步还原悬浮条显隐（与 _discard_staged 同源修复）
            try:
                self.main.set_overlay_visible(bool(self.c.get("overlay_enabled")))
            except Exception:
                pass
        event.accept()

    # ---------- 外部联动 ----------

    def sync_source_type(self, mode):
        """悬浮条切换输入来源后，同步音频来源下拉框并刷新设备列表。"""
        idx = self.source_combo.findData(mode)
        if idx >= 0:
            self.source_combo.blockSignals(True)
            self.source_combo.setCurrentIndex(idx)
            self.source_combo.blockSignals(False)
        self._load_devices()

    def focus_page(self, index):
        """右键「打开设置」时定位到指定页（0 音频 / 1 识别 / 2 翻译 / 3 显示 / 4 通用）。"""
        if 0 <= index < self.pages.count():
            self.nav.setCurrentRow(index)

    def load_from_config(self):
        """从配置刷新全部控件（挂起暂存记录），并把悬浮条预览还原为已保存值。

        v2.3.0：控件回显整体委托 _set_widgets_from（标准行表驱动 + 复合控件），
        消灭此前与它逐行重复的第二份同步清单——历史上"改设置漏同步一处"
        （v2.2.12 加开关要改 5 处）正是这种双清单漂移的产物。"""
        c = self.c
        self._staged.clear()
        self._loading = True
        try:
            self.source_combo.blockSignals(True)
            self._load_devices()
            self.source_combo.blockSignals(False)
            self._update_proxy_manual_enabled()
            self._refresh_argos_section()
            self._text_color = QColor(c.get("overlay_text_color"))
            self._bg_color = QColor(c.get("overlay_bg_color"))
            self._update_color_button(self.text_color_button, self._text_color)
            self._update_color_button(self.bg_color_button, self._bg_color)
            self._set_widgets_from({})
        finally:
            self._loading = False
        self._mark_dirty()
        self._refresh_storage_hint()
