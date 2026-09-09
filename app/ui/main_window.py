import ctypes
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
    def __init__(self, source_text, parent=None):
        super().__init__(parent)
        self.setObjectName("CaptionCard")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        self.created_at = datetime.now()
        self.source_text = source_text
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
        self.running = False
        self.session_count = 0
        self.setWindowTitle("LiveSubtitle · 实时字幕翻译")
        self.resize(1150, 760)
        self.setStyleSheet(DARK_QSS)
        self._build_ui()
        self._load_settings()
        if not self.config.get("wizard_done"):
            QTimer.singleShot(400, self._show_first_run_wizard)
        if self.config.get("auto_start"):
            # v2.0.1：改为可撤销的成员定时器——启动后 800ms 内手动开始又停止，
            # 旧 singleShot 到点会把管线再次拉起，与用户操作相反
            self._auto_start_timer = QTimer(self)
            self._auto_start_timer.setSingleShot(True)
            self._auto_start_timer.timeout.connect(self.start_pipeline)
            self._auto_start_timer.start(800)

    def _show_first_run_wizard(self):
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
        body.addWidget(self.stack, 1)
        root.addLayout(body)

        status = QStatusBar()
        self.setStatusBar(status)
        self.engine_status_label = QLabel("引擎：待启动")
        status.addWidget(self.engine_status_label)

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
                                      on_toggle_source=self._toggle_source)
        self.overlay.hide()
        self._build_tray()
        self._install_global_hotkey()

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

    # ---------- 全局热键 ----------

    def _install_global_hotkey(self):
        """安装原生事件过滤器并按当前配置注册热键（进程生命周期内一次过滤器）。"""
        hotkey.install(QApplication.instance(), self.toggle_running)
        status = self.apply_hotkey_config()
        # v2.0.0：启动时注册失败不再静默（组合被占用/不支持时用户毫无感知）
        if status.startswith("✗"):
            self.tray.showMessage("LiveSubtitle 全局热键", status[2:], QSystemTrayIcon.Warning, 4000)

    def apply_hotkey_config(self):
        """按配置注册/注销全局热键；返回给设置页展示的状态文本。"""
        c = self.config
        hotkey.unregister()
        if not c.get("hotkey_enabled"):
            self._update_tray_hotkey_text("")
            return "全局热键已关闭"
        seq = str(c.get("hotkey_sequence") or "Ctrl+Alt+S")
        ok = hotkey.register(int(self.winId()), seq)
        self._update_tray_hotkey_text(seq if ok else "")
        if ok:
            return f"✓ 全局热键 {seq} 已生效（托盘菜单同步显示）"
        return f"✗ 热键 {seq} 注册失败：组合不被支持或已被其他程序占用，请在「设置-通用」换一个组合"

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
        act_quit = QAction("退出", self)
        act_quit.triggered.connect(self._quit_app)
        menu.addAction(act_show)
        menu.addAction(act_toggle)
        self._tray_toggle_action = act_toggle
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
        self.apply_overlay_from_config()
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
        dlg.load_from_config()
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

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

    def apply_overlay_from_config(self):
        c = self.config
        self.overlay.set_list_mode(bool(c.get("overlay_list_mode")),
                                   int(c.get("overlay_list_max")))
        self.overlay.apply_style(
            font_size=int(c.get("overlay_font_size")),
            text_color=c.get("overlay_text_color"),
            bg_color=c.get("overlay_bg_color"),
            bg_opacity=int(c.get("overlay_bg_opacity")),
            outline=bool(c.get("overlay_outline")),
            outline_width=int(c.get("overlay_outline_width")),
            outline_color=c.get("overlay_outline_color"),
        )

    def on_overlay_closed(self):
        if getattr(self, "_quitting", False):
            return
        self._save_settings()
        self.config.set("overlay_enabled", False)
        dlg = getattr(self, "_settings_dlg", None)
        if dlg is not None:
            dlg.sync_overlay_check(False)

    def _clear_captions(self):
        while self.scroll_layout.count() > 1:
            item = self.scroll_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self.session_count = 0
        self.session_label.setText("本次会话：0 条")
        self.stack.setCurrentIndex(0)

    def _export_captions(self):
        cards = []
        for i in range(self.scroll_layout.count()):
            w = self.scroll_layout.itemAt(i).widget()
            if isinstance(w, CaptionCard):
                cards.append(w)
        if not cards:
            QMessageBox.information(self, "导出字幕", "当前会话还没有可导出的字幕。")
            return
        default_name = f"LiveSubtitle_{datetime.now():%Y%m%d_%H%M%S}.txt"
        # 默认落到用户文档目录：安装目录（Program Files）对标准权限用户不可写
        docs = QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation) or str(Path.home())
        path, _ = QFileDialog.getSaveFileName(
            self, "导出字幕", str(Path(docs) / default_name), "文本文件 (*.txt);;所有文件 (*)")
        if not path:
            return
        lines = []
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
        try:
            Path(path).write_text("\n".join(lines), encoding="utf-8")
        except Exception as e:
            QMessageBox.warning(self, "导出字幕", f"写入文件失败：{e}")
            return
        QMessageBox.information(self, "导出字幕", f"已导出 {len(cards)} 条字幕到：\n{path}")

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
        self.translate_thread = TranslateThread(engine, c.get("target_lang"), self)
        self.translate_thread.result_ready.connect(self._on_translated)
        # v2.0.4：状态改走带守卫的槽——lambda 无 running 守卫，停止后已入队的
        # 迟到状态（如孤儿加载线程的"正在加载模型"）会覆盖"已停止"
        self.translate_thread.status_changed.connect(self._on_translate_status)
        self.translate_thread.start()

        self._asr_ready = False  # v2.0.4：模型加载期停止时缩短等待（见 stop_pipeline）
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
        )
        self.capture_thread.segment_ready.connect(self.asr_thread.submit)
        self.capture_thread.level_changed.connect(self._on_level)
        self.capture_thread.error_occurred.connect(self._on_pipeline_error)
        self.capture_thread.low_input.connect(self._on_low_input)
        self.capture_thread.muted.connect(self._on_muted)
        self.capture_thread.start()

        # v2.0.7：30 秒零产出指引——"一直显示正在聆听"时给用户明确抓手
        # （音量条是否有波动 / 设备是否在放声音），而不是干等
        self._no_segment_hint_done = False
        self._no_segment_timer = QTimer(self)
        self._no_segment_timer.setSingleShot(True)
        self._no_segment_timer.timeout.connect(self._no_segment_hint)
        self._no_segment_timer.start(30000)

        if c.get("overlay_enabled") and not self.overlay.isVisible():
            self.set_overlay_enabled(True)
        self.update_overlay_status()

    def _no_segment_hint(self):
        """管线运行 30 秒仍零字幕时的一次性指引（v2.0.7）。"""
        if not self.running or getattr(self, "_no_segment_hint_done", True):
            return
        self._no_segment_hint_done = True
        if getattr(self, "_asr_ready", False) and getattr(self, "session_count", 0) == 0:
            from app import log as app_log
            app_log.log("pipeline.no_segments_30s", source=self.config.get("source_type"))
            self._set_engine_status(
                "已开始 30 秒仍无识别结果：请确认所选设备正在播放声音（音量条应有波动），"
                "系统音量/应用音量未静音，或到「设置-音频输入」更换设备")
            self.update_overlay_status()

    def _start_model_download_feedback(self, model_size):
        self._model_dl_model = model_size
        self._model_dl_total = MODEL_SIZES_MB.get(model_size, 480)
        self._stop_model_download_feedback()
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
        self._set_engine_status(
            f"正在下载识别模型（{mb:.0f}/{total}MB，{pct}%，仅首次；完成前请保持网络畅通）...")

    def _set_engine_status(self, text):
        self._engine_status_text = text
        if not getattr(self, "_low_input_warn", False) and not getattr(self, "_muted_warn", False):
            self.engine_status_label.setText(text)

    def _on_asr_status(self, text):
        # v2.0.4：幽灵回调守卫 + 过期线程守卫——停止后已入队的迟到状态、
        # 或重启管线后旧 AsrThread 的残余状态，都不得覆盖当前 UI。
        # 此前该信号是全项目唯一没有 running 守卫的后端回调（v2.0.1 只补了
        # low_input/muted/result 三类），热键连按后状态栏永久卡在
        # "正在加载tiny模型"的根因之一：孤儿加载线程的加载消息在
        # stop_pipeline 写完"已停止"之后才送达
        if not self.running or self.sender() is not self.asr_thread:
            return
        self._set_engine_status(f"识别: {text}")

    def _on_translate_status(self, text):
        if not self.running or self.sender() is not self.translate_thread:
            return
        self._set_engine_status(f"翻译: {text}")

    def _on_model_ready(self):
        # v2.0.4：模型就绪标记 + 停止下载进度反馈（原直连拆槽）
        self._asr_ready = True
        self._stop_model_download_feedback()

    def _on_level(self, value):
        # v2.0.4：停止后迟到的电平事件不再点亮音量条
        if not self.running:
            return
        self.level_bar.setValue(value)

    def _on_low_input(self, quiet):
        """采集线程报告输入信号持续过弱/恢复正常。"""
        from app import log as app_log
        app_log.log("capture.low_input", quiet=bool(quiet))
        if not self.running:
            # v2.0.1：幽灵回调守卫——停止后仍可能收到已入队的 Queued 信号
            return
        self._low_input_warn = quiet
        if quiet and self.running:
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
        self.status_dot.setStyleSheet("background-color: #3a4152; border-radius: 7px;")
        self.status_text.setText("未启动")
        self.level_bar.setValue(0)
        self._low_input_warn = False
        self._muted_warn = False  # v2.0.1：漏复位曾让悬浮条停止后仍显示"系统静音中"
        self._fail_streak = 0  # v2.0.2：会话结束时清零连续失败计数
        self._stop_model_download_feedback()
        timer = getattr(self, "_no_segment_timer", None)
        if timer is not None:
            timer.stop()
        self._no_segment_hint_done = True
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

    def _on_asr_text(self, text, detected, duration):
        self._set_engine_status(f"识别完成 [{detected or '?'}] ({duration}s)，翻译中...")
        if self.translate_thread:
            self.translate_thread.submit(text, detected)

    def _on_translated(self, source_text, translated, engine, detected, error):
        # v2.0.1：幽灵回调守卫——停止后仍会收到已入队的翻译结果，
        # 此前会新增字幕卡片、把界面翻回列表页、悬浮条显示"运行中"
        if not self.running:
            return
        self._last_engine_name = engine
        # v2.0.2：连续失败升级提示——备援链全灭（如 Google 全通道被封 +
        # MyMemory 配额尽 + 无离线包）时，不能只让每条字幕各自报错
        if error:
            self._fail_streak = getattr(self, "_fail_streak", 0) + 1
        else:
            self._fail_streak = 0
        show_source = bool(self.config.get("show_source"))
        card = CaptionCard(source_text)
        self.scroll_layout.insertWidget(self.scroll_layout.count() - 1, card)
        if self.stack.currentIndex() == 0:
            self.stack.setCurrentIndex(1)
        if error:
            card.set_failed(error)
        else:
            card.set_result(translated, engine, detected, show_source)
        self.session_count = getattr(self, "session_count", 0) + 1
        self.session_label.setText(f"本次会话：{self.session_count} 条")
        if error:
            self._set_engine_status(
                f"⚠ 翻译失败（连续 {self._fail_streak} 条）：{error}")
        else:
            self._set_engine_status(f"引擎：{engine} · 源语言: {detected or '?'}")
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
            self.overlay.show_caption(source_text, translated or ("[" + engine + " 翻译失败]"),
                                      show_source)
        sb = self.scroll.verticalScrollBar()
        sb.setValue(sb.maximum())
        while self.scroll_layout.count() - 1 > self.config.get("max_history"):
            item = self.scroll_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

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
                "字幕悬浮窗继续工作。点击托盘图标可重新打开主窗口。",
                QSystemTrayIcon.Information, 2500)


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
