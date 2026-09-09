"""首次运行向导（补充4）：选音频源 → 选识别模型 → 完成。

新用户面对空窗口无引导、首次模型下载黑盒感强，这里用三步向导解决。
完成后写入 wizard_done=True，之后不再弹出。
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QWidget,
    QStackedWidget, QRadioButton, QButtonGroup, QFrame,
)

from app.ui.styles import SETTING_QSS

MODEL_INFO = [
    ("tiny", "tiny · 极速", "75MB · 延迟约 2s · 中文易误判，适合纯英文内容"),
    ("base", "base · 流畅", "145MB · 延迟约 2.5s · 中文较弱"),
    ("small", "small · 推荐", "480MB · 延迟约 3s · 中文良好，4 核以上 CPU 流畅实时"),
    ("medium", "medium · 高精度", "1.5GB · 延迟约 6s · 需高配 CPU 或 GPU"),
    # v2.0.3：与设置页同步（此前向导缺此模型，已选该模型时向导会静默降级成 small）
    ("large-v3-turbo", "large-v3-turbo · 顶级", "约 1.6GB · 需 GPU 或高配 CPU"),
]


class FirstRunWizard(QDialog):
    def __init__(self, main, parent=None):
        super().__init__(parent or main)
        self.main = main
        self.setWindowTitle("欢迎使用 LiveSubtitle")
        self.setModal(True)
        self.resize(560, 440)
        self.setStyleSheet(SETTING_QSS)

        v = QVBoxLayout(self)
        v.setContentsMargins(28, 24, 28, 20)
        v.setSpacing(16)

        self.title_label = QLabel("欢迎使用 LiveSubtitle")
        self.title_label.setObjectName("AboutAppName")
        v.addWidget(self.title_label)
        self.step_hint = QLabel("第 1 步 / 共 3 步")
        self.step_hint.setObjectName("SettingDesc")
        v.addWidget(self.step_hint)

        self.stack = QStackedWidget()
        v.addWidget(self.stack, 1)

        self.stack.addWidget(self._page_source())
        self.stack.addWidget(self._page_model())
        self.stack.addWidget(self._page_done())

        nav = QHBoxLayout()
        self.back_button = QPushButton("上一步")
        self.back_button.setObjectName("GhostButton")
        self.next_button = QPushButton("下一步")
        self.next_button.setObjectName("PrimaryButton")
        nav.addStretch()
        nav.addWidget(self.back_button)
        nav.addWidget(self.next_button)
        v.addLayout(nav)

        self.back_button.clicked.connect(self._go_back)
        self.next_button.clicked.connect(self._go_next)
        self._page = 0
        self._sync_nav()

    # ---------- 页面 ----------

    def _page_source(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(10)
        q = QLabel("你主要用哪种方式生成字幕？")
        q.setObjectName("SettingTitle")
        v.addWidget(q)
        d = QLabel("看视频 / 听会议 → 选「系统声音」，直接抓取电脑播放的一切声音，无需任何声卡设置；\n"
                   "翻译别人对你说话 → 选「麦克风」。之后可随时在设置里更改。")
        d.setObjectName("SettingDesc")
        d.setWordWrap(True)
        v.addWidget(d)
        self.radio_system = QRadioButton("🎧  系统声音（推荐）——网页视频 / 播放器 / 会议的声音")
        self.radio_system.setChecked(True)
        self.radio_mic = QRadioButton("🎙  麦克风——采集外部人声")
        for r in (self.radio_system, self.radio_mic):
            v.addWidget(r)
        v.addStretch()
        return w

    def _page_model(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(8)
        q = QLabel("选择语音识别模型")
        q.setObjectName("SettingTitle")
        v.addWidget(q)
        d = QLabel("模型在本地运行，语音不出电脑。首次选择后自动下载（一次性），之后永久离线可用。\n"
                   "中文内容建议 small；配置一般、只识别英文可先选 tiny/base。")
        d.setObjectName("SettingDesc")
        d.setWordWrap(True)
        v.addWidget(d)
        self.model_group = QButtonGroup(self)
        current = str(self.main.config.get("asr_model") or "small")
        first = True
        for code, title, desc in MODEL_INFO:
            rb = QRadioButton(f"{title}\n    {desc}")
            rb.setProperty("model_code", code)
            self.model_group.addButton(rb)
            if code == current or (first and code == "small"):
                rb.setChecked(True)
            v.addWidget(rb)
            first = False
        v.addStretch()
        return w

    def _page_done(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(10)
        q = QLabel("全部就绪！")
        q.setObjectName("SettingTitle")
        v.addWidget(q)
        tips = QLabel(
            "· 点主窗口右上角「开始翻译」，播放视频即可看到字幕逐句出现\n"
            "· 翻译目标语言、翻译引擎（含 Google 通道代理解锁）在「设置 → 翻译」\n"
            "· 勾选「启用悬浮字幕条」可获得独立置顶字幕\n"
            "· 有 NVIDIA 显卡可在「设置 → 语音识别」选择 GPU 加速\n\n"
            "三分钟即可上手，遇到问题请查看「使用说明」。")
        tips.setObjectName("SettingDesc")
        tips.setWordWrap(True)
        v.addWidget(tips)
        v.addStretch()
        return w

    # ---------- 导航 ----------

    def _sync_nav(self):
        self.stack.setCurrentIndex(self._page)
        self.step_hint.setText(f"第 {self._page + 1} 步 / 共 3 步")
        self.back_button.setEnabled(self._page > 0)
        self.next_button.setText("完成" if self._page == 2 else "下一步")

    def _go_back(self):
        if self._page > 0:
            self._page -= 1
            self._sync_nav()

    def _go_next(self):
        if self._page < 2:
            self._page += 1
            self._sync_nav()
            return
        self._finish()

    def _finish(self):
        c = self.main.config
        running = bool(getattr(self.main, "running", False))
        c.set("source_type", "microphone" if self.radio_mic.isChecked() else "system")
        # v2.0.3：仅在仍是默认设备时才写 -1——重跑向导不再覆盖用户已选的指定设备
        try:
            was_default = int(c.get("device_index") or -1) == -1
        except (TypeError, ValueError):
            was_default = True
        if was_default:
            c.set("device_index", -1)
        checked = self.model_group.checkedButton()
        if checked is not None:
            c.set("asr_model", checked.property("model_code"))
        c.set("wizard_done", True)
        dlg = getattr(self.main, "_settings_dlg", None)
        if dlg is not None:
            dlg.load_from_config()
        # v2.0.3：向导可从设置页在翻译运行中重跑——写完配置要重启管线让
        # 新 source/model 真正生效（对齐设置页 _PIPELINE_KEYS 语义）
        if running:
            self.main.stop_pipeline()
            self.main.start_pipeline()
        self.accept()
