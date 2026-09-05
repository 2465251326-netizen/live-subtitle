import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtGui import QIcon, QAction
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton,
    QCheckBox, QFrame, QScrollArea, QProgressBar, QSizePolicy,
    QStatusBar, QMessageBox, QApplication, QStackedWidget,
    QSystemTrayIcon, QMenu,
)

from app.config import Config
from app.audio.capture import CaptureThread
from app.asr.engine import AsrThread
from app.translate.translator import TranslateThread
from app.ui.styles import DARK_QSS
from app.ui.caption_overlay import CaptionOverlay

DOCS_URL = "https://github.com/2465251326-netizen/live-subtitle#readme"


def icon_path():
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "app" / "icon.ico"
    return Path(__file__).resolve().parents[2] / "app" / "icon.ico"


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
        self.running = False
        self.session_count = 0
        self.setWindowTitle("LiveSubtitle · 实时字幕翻译")
        self.resize(1150, 760)
        self.setStyleSheet(DARK_QSS)
        self._build_ui()
        self._load_settings()
        if self.config.get("auto_start"):
            QTimer.singleShot(800, self.start_pipeline)

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

        self.session_label = QLabel("本次会话：0 条")
        status.addPermanentWidget(self.session_label)

        self.overlay = CaptionOverlay(on_closed=self.on_overlay_closed)
        self.overlay.hide()
        self._build_tray()

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
        menu.addSeparator()
        menu.addAction(act_quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda r: self._restore_window()
            if r == QSystemTrayIcon.Trigger or r == QSystemTrayIcon.DoubleClick else None)
        self.tray.show()

    def _load_settings(self):
        c = self.config
        self.overlay.move(c.get("overlay_x"), c.get("overlay_y"))
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
            dlg.load_from_config()
            self._settings_dlg = dlg
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

    def set_overlay_enabled(self, checked):
        if checked:
            self.overlay.move(self.config.get("overlay_x"), self.config.get("overlay_y"))
            self.overlay.show()
        else:
            self.overlay.hide()
        self.config.set("overlay_enabled", bool(checked))

    def apply_overlay_from_config(self):
        c = self.config
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
        if self.running:
            return
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
        self.translate_thread.status_changed.connect(
            lambda m: self.engine_status_label.setText(f"翻译: {m}"))
        self.translate_thread.start()

        self.asr_thread = AsrThread(
            c.get("asr_model"),
            c.get("asr_device"),
            c.get("asr_language"),
            self,
        )
        self.asr_thread.text_ready.connect(self._on_asr_text)
        self.asr_thread.status_changed.connect(
            lambda m: self.engine_status_label.setText(f"识别: {m}"))
        self.asr_thread.error_occurred.connect(self._on_pipeline_error)
        self.asr_thread.start()

        self.capture_thread = CaptureThread(
            c.get("source_type"),
            int(c.get("device_index") or -1),
            self,
        )
        self.capture_thread.segment_ready.connect(self.asr_thread.submit)
        self.capture_thread.level_changed.connect(self.level_bar.setValue)
        self.capture_thread.error_occurred.connect(self._on_pipeline_error)
        self.capture_thread.start()

        if c.get("overlay_enabled") and not self.overlay.isVisible():
            self.set_overlay_enabled(True)

    @staticmethod
    def _detach_thread(t):
        """停止超时的残留线程必须与界面断开信号，避免再向 UI 发送过期字幕。"""
        if t is None:
            return
        for name in ("segment_ready", "level_changed", "error_occurred",
                     "text_ready", "status_changed", "result_ready", "model_ready"):
            sig = getattr(t, name, None)
            if sig is not None:
                try:
                    sig.disconnect()
                except Exception:
                    pass

    def stop_pipeline(self):
        if not self.running:
            return
        self.running = False
        self.toggle_button.setText("开始翻译")
        self.toggle_button.setObjectName("PrimaryButton")
        self.toggle_button.style().unpolish(self.toggle_button)
        self.toggle_button.style().polish(self.toggle_button)
        self.status_dot.setStyleSheet("background-color: #3a4152; border-radius: 7px;")
        self.status_text.setText("未启动")
        self.level_bar.setValue(0)
        self.stack.setCurrentIndex(0)

        threads = (self.capture_thread, self.asr_thread, self.translate_thread)
        for t in threads:
            if t:
                t.stop()
        if threads[0]:
            threads[0].wait(2000)
        for t in threads[1:]:
            if t:
                t.wait(15000)
        for t in threads:
            if t and t.isRunning():
                self._detach_thread(t)
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
        self.engine_status_label.setText(f"引擎：{engine} · 源语言: {detected or '?'}")
        sb = self.scroll.verticalScrollBar()
        sb.setValue(sb.maximum())
        if self.overlay.isVisible():
            self.overlay.show_caption(source_text, translated or ("[" + engine + " 翻译失败]"),
                                      show_source)
        while self.scroll_layout.count() - 1 > self.config.get("max_history"):
            item = self.scroll_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def closeEvent(self, event):
        if getattr(self, "_quitting", False):
            self._save_settings()
            self.stop_pipeline()
            self.overlay.close()
            if getattr(self, "tray", None):
                self.tray.hide()
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
            self.close()
            return
        box = QMessageBox(self)
        box.setWindowTitle("关闭 LiveSubtitle")
        box.setText("关闭软件后要做什么？")
        box.setInformativeText("隐藏到托盘后，字幕悬浮窗继续显示，可从右下角托盘图标重新打开主窗口。")
        tray_btn = box.addButton("隐藏到托盘", QMessageBox.AcceptRole)
        exit_btn = box.addButton("退出程序", QMessageBox.DestructiveRole)
        cancel_btn = box.addButton("取消", QMessageBox.RejectRole)
        box.setCheckBox(QCheckBox("记住我的选择，下次不再询问"))
        box.exec()
        remember = box.checkBox().isChecked()
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
            self.close()

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
