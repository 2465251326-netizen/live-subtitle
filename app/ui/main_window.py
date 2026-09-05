import time
from datetime import datetime

import requests
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton,
    QComboBox, QCheckBox, QFrame, QScrollArea, QGridLayout, QProgressBar,
    QStatusBar, QSizePolicy, QMessageBox, QApplication, QStackedWidget,
)

from app.config import Config, LANGUAGES
from app.audio.capture import CaptureThread, list_input_devices, list_output_devices
from app.asr.engine import AsrThread
from app.translate.translator import TranslateThread, ArgosEngine
from app.ui.styles import DARK_QSS
from app.ui.caption_overlay import CaptionOverlay

MODELS = [("tiny", "tiny · 最快 · 延迟约 2s"),
          ("base", "base · 流畅 · 中文较弱"),
          ("small", "small · 推荐（4 核以上）"),
          ("medium", "medium · 高精度 · 需好 CPU")]


class ArgosWorker(QThread):
    progress_text = Signal(str)
    progress_pct = Signal(int)
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(self, from_code, parent=None):
        super().__init__(parent)
        self.from_code = from_code

    def run(self):
        try:
            self.progress_text.emit("正在获取语言包索引...")
            packs = ArgosEngine.available_packages()
            zh_pkgs = [p for p in packs if p.from_code == self.from_code and p.to_code.startswith("zh")]
            if not zh_pkgs:
                self.failed.emit(f"未找到 {self.from_code} -> 中文 的离线语言包")
                return
            pkg = zh_pkgs[0]

            def cb(pct):
                self.progress_pct.emit(pct)

            self.progress_text.emit(f"正在下载语言包 {pkg.from_name} -> {pkg.to_name}（约 70MB）...")
            ArgosEngine.install(pkg, progress_cb=cb)
            self.progress_pct.emit(100)
            self.finished_ok.emit(f"语言包 {pkg.from_name} -> {pkg.to_name} 安装成功，可离线使用")
        except Exception as e:
            self.failed.emit(f"语言包安装失败: {e}")


class CaptionCard(QFrame):
    def __init__(self, source_text, parent=None):
        super().__init__(parent)
        self.setObjectName("CaptionCard")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
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

        layout.addWidget(self.meta_label)
        layout.addWidget(self.source_label)
        layout.addWidget(self.target_label)

    def set_result(self, translated, engine, detected, show_source):
        if translated:
            self.target_label.setText(translated)
        else:
            self.target_label.setText("[翻译失败]")
        note = f"{datetime.now().strftime('%H:%M:%S')} · {detected or '?'} · 引擎: {engine}"
        self.meta_label.setText(note)
        self.source_label.setVisible(show_source)

    def set_failed(self, msg):
        self.target_label.setText("[翻译失败]")
        self.meta_label.setText(f"{datetime.now().strftime('%H:%M:%S')} · {msg}")


class MainWindow(QMainWindow):
    start_requested = Signal()

    def __init__(self):
        super().__init__()
        self.config = Config()
        self.capture_thread = None
        self.asr_thread = None
        self.translate_thread = None
        self.argos_worker = None
        self.running = False
        self.device_map = {}
        self.session_count = 0
        self.setWindowTitle("LiveSubtitle · 实时字幕翻译")
        self.resize(1150, 760)
        self.setStyleSheet(DARK_QSS)
        self._build_ui()
        self._load_devices()
        self._load_settings()

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
        subtitle = QLabel("实时语音识别 · 自动语言检测 · 中文翻译（在线 / 离线）")
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

        self.toggle_button = QPushButton("开始翻译")
        self.toggle_button.setObjectName("PrimaryButton")
        self.toggle_button.setCursor(Qt.PointingHandCursor)
        self.toggle_button.clicked.connect(self.toggle_running)
        header.addWidget(self.toggle_button)
        root.addLayout(header)

        body = QHBoxLayout()
        body.setSpacing(14)

        side = QFrame()
        side.setObjectName("SidePanel")
        side.setFixedWidth(290)
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(16, 14, 16, 14)
        side_layout.setSpacing(10)

        def panel_title(text):
            lab = QLabel(text)
            lab.setObjectName("PanelTitle")
            return lab

        side_layout.addWidget(panel_title("音频输入"))
        self.source_combo = QComboBox()
        self.source_combo.addItem("系统声音（正在播放的内容）", "system")
        self.source_combo.addItem("麦克风", "microphone")
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)
        side_layout.addWidget(self.source_combo)

        self.device_combo = QComboBox()
        side_layout.addWidget(self.device_combo)
        self.refresh_button = QPushButton("刷新设备列表")
        self.refresh_button.clicked.connect(self._load_devices)
        side_layout.addWidget(self.refresh_button)

        self.level_bar = QProgressBar()
        self.level_bar.setObjectName("LevelBar")
        self.level_bar.setRange(0, 100)
        self.level_bar.setTextVisible(False)
        self.level_bar.setFixedHeight(10)
        side_layout.addWidget(self.level_bar)

        side_layout.addSpacing(6)
        side_layout.addWidget(panel_title("语音识别（本地）"))
        self.model_combo = QComboBox()
        for code, label in MODELS:
            self.model_combo.addItem(label, code)
        side_layout.addWidget(self.model_combo)

        self.asr_lang_combo = QComboBox()
        for code, name in LANGUAGES.items():
            if code in ("zh-CN", "zh-TW"):
                continue
            self.asr_lang_combo.addItem(name, code)
        side_layout.addWidget(self.asr_lang_combo)

        self.compute_combo = QComboBox()
        self.compute_combo.addItem("CPU 模式（通用）", "cpu")
        self.compute_combo.addItem("自动（优先 GPU）", "auto")
        side_layout.addWidget(self.compute_combo)

        side_layout.addSpacing(6)
        side_layout.addWidget(panel_title("翻译引擎"))
        self.engine_combo = QComboBox()
        self.engine_combo.addItem("自动探测（推荐）", "auto")
        self.engine_combo.addItem("Google 免费接口（在线）", "google")
        self.engine_combo.addItem("MyMemory（在线备援）", "mymemory")
        self.engine_combo.addItem("Argos 离线语言包", "argos")
        self.engine_combo.currentIndexChanged.connect(self._refresh_argos_section)
        side_layout.addWidget(self.engine_combo)

        self.target_combo = QComboBox()
        self.target_combo.addItem("简体中文", "zh-CN")
        self.target_combo.addItem("繁体中文", "zh-TW")
        side_layout.addWidget(self.target_combo)

        self.argos_hint = QLabel("选择 Argos 引擎后，请先下载所需语言包（下载一次即可永久离线使用）")
        self.argos_hint.setObjectName("CaptionMeta")
        self.argos_hint.setWordWrap(True)
        self.argos_combo = QComboBox()
        self.argos_download_button = QPushButton("下载该语言 -> 中文 语言包")
        self.argos_download_button.clicked.connect(self._download_argos)
        self.argos_progress = QProgressBar()
        self.argos_progress.setRange(0, 100)
        self.argos_progress.setVisible(False)
        for w in (self.argos_combo, self.argos_download_button, self.argos_progress):
            w.setVisible(False)
            side_layout.addWidget(w)
        side_layout.addWidget(self.argos_hint)
        self._refresh_argos_section()

        side_layout.addSpacing(6)
        side_layout.addWidget(panel_title("显示"))
        self.overlay_check = QCheckBox("启用悬浮字幕条（置顶）")
        self.overlay_check.toggled.connect(self._on_overlay_toggle)
        side_layout.addWidget(self.overlay_check)
        self.show_source_check = QCheckBox("同时显示原文")
        side_layout.addWidget(self.show_source_check)

        side_layout.addStretch()
        self.clear_button = QPushButton("清空字幕记录")
        self.clear_button.clicked.connect(self._clear_captions)
        side_layout.addWidget(self.clear_button)

        body.addWidget(side)

        captions_column = QVBoxLayout()
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
        empty_page = QWidget()
        empty_layout = QVBoxLayout(empty_page)
        empty_layout.addStretch()
        empty_layout.addWidget(self.empty_hint)
        empty_layout.addStretch()
        list_page = QWidget()
        list_layout = QVBoxLayout(list_page)
        list_layout.setContentsMargins(0, 0, 0, 0)
        list_layout.addWidget(self.scroll)

        self.stack = QStackedWidget()
        self.stack.addWidget(empty_page)
        self.stack.addWidget(list_page)
        self.stack.setCurrentIndex(0)
        captions_column.addWidget(self.stack)
        body.addLayout(captions_column, 1)
        root.addLayout(body)

        status = QStatusBar()
        self.setStatusBar(status)
        self.engine_status_label = QLabel("引擎：待启动")
        status.addWidget(self.engine_status_label)
        self.session_label = QLabel("本次会话：0 条")
        status.addPermanentWidget(self.session_label)

        self.overlay = CaptionOverlay(self)
        self.overlay.hide()

    def _load_devices(self):
        self.device_combo.clear()
        self.device_map = {}
        source_type = self.source_combo.currentData()
        devices = list_output_devices() if source_type == "system" else list_input_devices()
        for d in devices:
            tag = " [系统声音]" if d.get("loopback") else ""
            label = f"{d['name']}{tag}"
            self.device_map[label] = d["index"]
            self.device_combo.addItem(label, d["index"])
        if not devices and source_type == "system":
            self.device_combo.addItem("默认输出设备（自动）", -1)

    def _on_source_changed(self):
        self._load_devices()

    def _load_settings(self):
        c = self.config
        idx = self.source_combo.findData(c.get("source_type"))
        if idx >= 0:
            self.source_combo.setCurrentIndex(idx)
        dev_idx = self.device_combo.findData(c.get("device_index"))
        if dev_idx >= 0:
            self.device_combo.setCurrentIndex(dev_idx)
        model_idx = self.model_combo.findData(c.get("asr_model"))
        if model_idx >= 0:
            self.model_combo.setCurrentIndex(model_idx)
        lang_idx = self.asr_lang_combo.findData(c.get("asr_language"))
        if lang_idx >= 0:
            self.asr_lang_combo.setCurrentIndex(lang_idx)
        dev_idx = self.compute_combo.findData(c.get("asr_device"))
        if dev_idx >= 0:
            self.compute_combo.setCurrentIndex(dev_idx)
        eng_idx = self.engine_combo.findData(c.get("engine"))
        if eng_idx >= 0:
            self.engine_combo.setCurrentIndex(eng_idx)
        tgt_idx = self.target_combo.findData(c.get("target_lang"))
        if tgt_idx >= 0:
            self.target_combo.setCurrentIndex(tgt_idx)
        self.overlay_check.setChecked(bool(c.get("overlay_enabled")))
        self.show_source_check.setChecked(bool(c.get("show_source")))

    def _save_settings(self):
        c = self.config
        c.set("source_type", self.source_combo.currentData())
        c.set("device_index", self.device_combo.currentData())
        c.set("asr_model", self.model_combo.currentData())
        c.set("asr_language", self.asr_lang_combo.currentData())
        c.set("asr_device", self.compute_combo.currentData())
        c.set("engine", self.engine_combo.currentData())
        c.set("target_lang", self.target_combo.currentData())
        c.set("overlay_enabled", self.overlay_check.isChecked())
        c.set("show_source", self.show_source_check.isChecked())
        c.set("overlay_x", self.overlay.x())
        c.set("overlay_y", self.overlay.y())

    def _refresh_argos_section(self):
        is_argos = self.engine_combo.currentData() == "argos"
        for w in (self.argos_combo, self.argos_download_button):
            w.setVisible(is_argos)
        self.argos_hint.setVisible(is_argos)
        downloading = bool(self.argos_worker and self.argos_worker.isRunning())
        self.argos_progress.setVisible(is_argos and downloading)
        if is_argos:
            self._populate_argos_langs()

    def _populate_argos_langs(self):
        self.argos_combo.clear()
        common = ["en", "ja", "ko", "ru", "fr", "de", "es", "pt", "it", "th", "vi", "ar", "id", "hi"]
        installed = set(ArgosEngine.installed_pairs())
        for code in common:
            name = LANGUAGES.get(code, code)
            if (code, "zh") in installed:
                name += "（已安装）"
            self.argos_combo.addItem(name, code)
        if installed:
            self.argos_hint.setText(
                f"已安装 {len(installed)} 个语言包，选中即可离线翻译；下方可继续补充其他语言。")
        else:
            self.argos_hint.setText(
                "请先下载所需语言包（下载一次即可永久离线使用），下载期间请勿切换翻译引擎。")

    def _download_argos(self):
        code = self.argos_combo.currentData()
        if not code:
            return
        if self.argos_worker and self.argos_worker.isRunning():
            return
        self.argos_download_button.setEnabled(False)
        self.argos_combo.setEnabled(False)
        self.engine_combo.setEnabled(False)
        self.argos_progress.setValue(0)
        self.argos_progress.setVisible(True)
        self.engine_status_label.setText(f"正在安装离线语言包: {code} -> 中文")
        self.argos_worker = ArgosWorker(code, self)
        self.argos_worker.progress_text.connect(
            lambda m: self.engine_status_label.setText(m))
        self.argos_worker.progress_pct.connect(self.argos_progress.setValue)
        self.argos_worker.finished_ok.connect(self._on_argos_done)
        self.argos_worker.failed.connect(self._on_argos_failed)
        self.argos_worker.start()

    def _on_argos_done(self, msg):
        self.argos_download_button.setEnabled(True)
        self.argos_combo.setEnabled(True)
        self.engine_combo.setEnabled(True)
        self.argos_progress.setVisible(False)
        self.engine_status_label.setText(msg)
        self._populate_argos_langs()
        self._show_info("完成", msg)

    def _on_argos_failed(self, msg):
        self.argos_download_button.setEnabled(True)
        self.argos_combo.setEnabled(True)
        self.engine_combo.setEnabled(True)
        self.argos_progress.setVisible(False)
        self.engine_status_label.setText(msg)
        self._show_info("失败", f"{msg}\n\n请检查网络，或改用在线翻译引擎。")

    def _show_info(self, title, text):
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setText(text)
        box.setModal(False)
        box.setAttribute(Qt.WA_DeleteOnClose, True)
        box.show()

    def _on_overlay_toggle(self, checked):
        if checked:
            self.overlay.move(self.config.get("overlay_x"), self.config.get("overlay_y"))
            self.overlay.show()
        else:
            self.overlay.hide()

    def on_overlay_closed(self):
        self.overlay_check.blockSignals(True)
        self.overlay_check.setChecked(False)
        self.overlay_check.blockSignals(False)

    def _clear_captions(self):
        while self.scroll_layout.count() > 1:
            item = self.scroll_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self.session_count = 0
        self.session_label.setText("本次会话：0 条")
        self.stack.setCurrentIndex(0)

    def toggle_running(self):
        if self.running:
            self.stop_pipeline()
        else:
            self.start_pipeline()

    def start_pipeline(self):
        self._save_settings()
        self.running = True
        self.toggle_button.setText("停止翻译")
        self.toggle_button.setObjectName("StopButton")
        self.toggle_button.style().unpolish(self.toggle_button)
        self.toggle_button.style().polish(self.toggle_button)
        self.status_dot.setStyleSheet("background-color: #2ecc71; border-radius: 7px;")
        self.status_text.setText("运行中")
        self.stack.setCurrentIndex(1)
        self.session_count = 0

        engine = self.engine_combo.currentData()
        self.translate_thread = TranslateThread(engine, self.target_combo.currentData(), self)
        self.translate_thread.result_ready.connect(self._on_translated)
        self.translate_thread.status_changed.connect(
            lambda m: self.engine_status_label.setText(f"翻译: {m}"))
        self.translate_thread.start()

        self.asr_thread = AsrThread(
            self.model_combo.currentData(),
            self.compute_combo.currentData(),
            self.asr_lang_combo.currentData(),
            self,
        )
        self.asr_thread.text_ready.connect(self._on_asr_text)
        self.asr_thread.status_changed.connect(
            lambda m: self.engine_status_label.setText(f"识别: {m}"))
        self.asr_thread.error_occurred.connect(self._on_pipeline_error)
        self.asr_thread.start()

        self.capture_thread = CaptureThread(
            self.source_combo.currentData(),
            self.device_combo.currentData() or -1,
            self,
        )
        self.capture_thread.segment_ready.connect(self.asr_thread.submit)
        self.capture_thread.level_changed.connect(self.level_bar.setValue)
        self.capture_thread.error_occurred.connect(self._on_pipeline_error)
        self.capture_thread.start()

        if self.overlay_check.isChecked() and not self.overlay.isVisible():
            self._on_overlay_toggle(True)

    def stop_pipeline(self):
        self.running = False
        self.toggle_button.setText("开始翻译")
        self.toggle_button.setObjectName("PrimaryButton")
        self.toggle_button.style().unpolish(self.toggle_button)
        self.toggle_button.style().polish(self.toggle_button)
        self.status_dot.setStyleSheet("background-color: #3a4152; border-radius: 7px;")
        self.status_text.setText("未启动")
        self.level_bar.setValue(0)
        self.stack.setCurrentIndex(0)

        for t in (self.capture_thread, self.asr_thread, self.translate_thread):
            if t:
                t.stop()
        if self.capture_thread:
            self.capture_thread.wait(2000)
        for t in (self.asr_thread, self.translate_thread):
            if t:
                t.wait(15000)
        self.capture_thread = None
        self.asr_thread = None
        self.translate_thread = None
        self.engine_status_label.setText("引擎：已停止")

    def _on_pipeline_error(self, msg):
        self.engine_status_label.setText(msg)
        if self.running and ("采集" in msg or "回环" in msg or "音频" in msg):
            self.stop_pipeline()
            self._show_info("音频错误", msg)

    def _on_asr_text(self, text, detected, duration):
        self.engine_status_label.setText(f"识别完成 [{detected or '?'}] ({duration}s)，翻译中...")
        if self.translate_thread:
            self.translate_thread.submit(text, detected)

    def _on_translated(self, source_text, translated, engine, detected, error):
        card = CaptionCard(source_text)
        self.scroll_layout.insertWidget(self.scroll_layout.count() - 1, card)
        if self.stack.currentIndex() == 0:
            self.stack.setCurrentIndex(1)
        if error:
            card.set_failed(error)
        else:
            card.set_result(translated, engine, detected, self.show_source_check.isChecked())
        self.session_count = getattr(self, "session_count", 0) + 1
        self.session_label.setText(f"本次会话：{self.session_count} 条")
        self.engine_status_label.setText(f"引擎：{engine} · 源语言: {detected or '?'}")
        sb = self.scroll.verticalScrollBar()
        sb.setValue(sb.maximum())
        if self.overlay.isVisible():
            self.overlay.show_caption(source_text, translated or ("[" + engine + " 翻译失败]"),
                                      self.show_source_check.isChecked())
        while self.scroll_layout.count() - 1 > self.config.get("max_history"):
            item = self.scroll_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def closeEvent(self, event):
        self._save_settings()
        self.stop_pipeline()
        event.accept()


def run_app():
    import sys
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName("LiveSubtitle")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
