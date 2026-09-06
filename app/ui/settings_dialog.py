"""独立设置窗口（微信 PC 版风格：左侧分类导航 + 右侧内容区，改动即时生效保存）"""
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QVBoxLayout, QLabel, QPushButton, QComboBox,
    QCheckBox, QFrame, QGridLayout, QProgressBar, QSpinBox, QSlider,
    QListWidget, QListWidgetItem, QStackedWidget, QWidget, QMessageBox,
    QScrollArea,
)

from app.config import LANGUAGES, TARGET_LANGS
from app.translate.translator import ArgosEngine, _cache
from app.translate.offline_pack import cleanup_temp_files
from app.audio.capture import list_input_devices, list_output_devices
from app.ui.styles import SETTING_QSS

MODELS = [("tiny", "tiny · 最快 · 延迟约 2s"),
          ("base", "base · 流畅 · 中文较弱"),
          ("small", "small · 推荐（4 核以上）"),
          ("medium", "medium · 高精度 · 需好 CPU")]


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
            self.progress_text.emit("正在获取语言包索引...")
            packs = ArgosEngine.available_packages()
            match = [p for p in packs if p.from_code == self.from_code and p.to_code == self.to_code]
            if not match:
                self.failed.emit(
                    f"未找到 {self.from_code} -> {self.to_code} 的离线语言包（该方向暂无离线包，可改用在线引擎）")
                return
            pkg = match[0]

            def cb(pct):
                self.progress_pct.emit(pct)

            self.progress_text.emit(f"正在下载语言包 {pkg.from_name} -> {pkg.to_name}（约 70MB）...")
            ArgosEngine.install(pkg, progress_cb=cb)
            self.progress_pct.emit(100)
            self.finished_ok.emit(f"语言包 {pkg.from_name} -> {pkg.to_name} 安装成功，可离线使用")
        except Exception as e:
            self.failed.emit(f"语言包安装失败: {e}")


def _nav_item(text):
    item = QListWidgetItem(text)
    item.setSizeHint(item.sizeHint().__class__(0, 44))
    return item


class SettingsDialog(QDialog):
    settings_saved = Signal()

    def __init__(self, main):
        super().__init__(None)
        self.main = main
        self.c = main.config
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
        # 自动保存指示：本产品设置即改即生效，没有确定按钮；
        # 用这个标签告诉用户「改动已落盘」，消除不确定感
        self.saved_label = QLabel("✓ 设置已自动保存")
        self.saved_label.setObjectName("SavedHint")
        self.saved_label.setAlignment(Qt.AlignCenter)
        self.saved_label.hide()
        outer.addWidget(self.saved_label)

        self.nav = QListWidget()
        self.nav.setObjectName("NavList")
        self.nav.setFixedWidth(190)
        for t in ("音频输入", "语音识别", "翻译", "显示", "通用"):
            self.nav.addItem(_nav_item(t))
        root.addWidget(self.nav)

        self.pages = QStackedWidget()
        self.pages.setObjectName("SettingPages")
        root.addWidget(self.pages, 1)

        self.pages.addWidget(self._page_audio())
        self.pages.addWidget(self._page_asr())
        self.pages.addWidget(self._page_translate())
        self.pages.addWidget(self._page_display())
        self.pages.addWidget(self._page_general())

        self.nav.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.nav.setCurrentRow(0)

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
        return lab

    def _row(self, page, title, desc, widget):
        box = QVBoxLayout()
        box.setSpacing(4)
        h = QHBoxLayout()
        t = QLabel(title)
        t.setObjectName("SettingTitle")
        h.addWidget(t)
        h.addStretch()
        if widget is not None:
            widget.setMinimumWidth(230)
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
        self.device_combo.currentIndexChanged.connect(
            lambda _i: self._save_combo("device_index", self.device_combo))
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
            "medium": "1.5GB · 高精度 · 需较新多核 CPU 或 GPU",
        }
        for code, label in [("tiny", "tiny · 最快 · 延迟约 2s"),
                            ("base", "base · 流畅 · 中文较弱"),
                            ("small", "small · 推荐（4 核以上）"),
                            ("medium", "medium · 高精度 · 需好 CPU")]:
            self.model_combo.addItem(label, code)
            self.model_combo.setItemData(self.model_combo.count() - 1, tips[code], Qt.ToolTipRole)
        self._row(page, "识别模型",
                  "全部在本地运行，语音不出电脑。首次选择后自动下载模型（一次性），之后永久离线可用。中文内容建议 small。",
                  self.model_combo)

        self._section(page, "语言与计算")
        self.asr_lang_combo = QComboBox()
        self.asr_lang_combo.addItem("自动检测", "auto")
        for code, name in LANGUAGES.items():
            if code in ("zh-CN", "zh-TW"):
                continue
            self.asr_lang_combo.addItem(name, code)
        self._row(page, "识别语言",
                  "「自动检测」会在第一句话后锁定说话语言，换语言视频无感切换；已知语言时手动锁定更快更稳。",
                  self.asr_lang_combo)
        self.compute_combo = QComboBox()
        self.compute_combo.addItem("CPU 模式（通用）", "cpu")
        self.compute_combo.addItem("自动（优先 GPU）", "auto")
        self._row(page, "计算方式",
                  "有 NVIDIA 显卡并配置 CUDA 环境时选「自动」可用 GPU 加速；普通电脑保持 CPU 模式即可实时。",
                  self.compute_combo)

        for w, key in ((self.model_combo, "asr_model"), (self.asr_lang_combo, "asr_language"),
                       (self.compute_combo, "asr_device")):
            w.currentIndexChanged.connect(lambda _i, w=w, k=key: self._save_combo(k, w))
        page._inner_layout.addStretch()
        return page

    # ---------- 页面：翻译 ----------

    def _page_translate(self):
        page = self._page()
        self._section(page, "翻译方向")
        self.engine_combo = QComboBox()
        self.engine_combo.addItem("自动探测（推荐）", "auto")
        self.engine_combo.addItem("Google 免费接口（在线）", "google")
        self.engine_combo.addItem("MyMemory（在线备援）", "mymemory")
        self.engine_combo.addItem("Argos 离线语言包", "argos")
        self._row(page, "翻译引擎",
                  "全部免费无需密钥。自动模式启动时探测在线接口并选用可达者；Argos 为完全离线方案，需先在下方下载语言包。",
                  self.engine_combo)
        self.target_combo = QComboBox()
        for code in TARGET_LANGS:
            self.target_combo.addItem(LANGUAGES.get(code, code), code)
        self._row(page, "翻译目标语言",
                  "支持简繁中文、英、日、韩、法、德、西、俄、葡、意、泰、越、阿、印尼、印地共 16 种。",
                  self.target_combo)

        self.argos_section_label = self._section(page, "离线语言包")
        try:
            cleanup_temp_files()
        except Exception:
            pass
        self.argos_hint = QLabel("选择 Argos 引擎后在此下载语言包（约 70MB / 包，一次下载永久离线使用）。")
        self.argos_hint.setObjectName("SettingDesc")
        self.argos_hint.setWordWrap(True)
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
        self.argos_progress = QProgressBar()
        self.argos_progress.setRange(0, 100)
        self.argos_progress.setVisible(False)
        grid.addWidget(self.argos_progress, 1, 0, 1, 2)
        page._inner_layout.addLayout(grid)

        self._refresh_argos_section()
        self.engine_combo.currentIndexChanged.connect(self._on_engine_changed)
        self.target_combo.currentIndexChanged.connect(self._on_engine_changed)
        page._inner_layout.addStretch()
        return page

    # ---------- 页面：显示 ----------

    def _page_display(self):
        page = self._page()
        self._section(page, "字幕显示")
        self.overlay_check = QCheckBox()
        self._row(page, "启用悬浮字幕条（置顶）",
                  "屏幕上方的独立字幕条，可拖动到任意位置；最小化主窗口后继续显示。右键字幕条可关闭。",
                  self.overlay_check)
        self.show_source_check = QCheckBox()
        self._row(page, "同时显示原文",
                  "开启后字幕与悬浮字幕同时保留原语言文本。",
                  self.show_source_check)

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
        self.bg_opacity_slider = QSlider(Qt.Horizontal)
        self.bg_opacity_slider.setRange(0, 95)
        self.bg_opacity_label = QLabel("78%")
        self.bg_opacity_label.setObjectName("SettingDesc")
        slider_row.addWidget(self.bg_opacity_slider)
        slider_row.addWidget(self.bg_opacity_label)
        grid.addLayout(slider_row, 3, 1)

        self.outline_check = QCheckBox()
        grid.addWidget(self._gl("字体描边"), 4, 0)
        grid.addWidget(self.outline_check, 4, 1)
        grid.addWidget(self._gl("描边宽度"), 5, 0)
        self.outline_width_spin = QSpinBox()
        self.outline_width_spin.setRange(1, 6)
        self.outline_width_spin.setSuffix(" px")
        grid.addWidget(self.outline_width_spin, 5, 1)
        grid.addWidget(self._gl("描边颜色"), 6, 0)
        self.outline_color_button = QPushButton("选择")
        self.outline_color_button.setObjectName("ColorPickButton")
        grid.addWidget(self.outline_color_button, 6, 1)
        page._inner_layout.addLayout(grid)

        self.overlay_check.toggled.connect(self._on_overlay_toggle)
        self.show_source_check.toggled.connect(lambda v: self._save("show_source", bool(v)))
        self.overlay_font_spin.valueChanged.connect(self._apply_overlay_style)
        self.outline_width_spin.valueChanged.connect(self._apply_overlay_style)
        self.bg_opacity_slider.valueChanged.connect(
            lambda v: (self.bg_opacity_label.setText(f"{v}%"), self._apply_overlay_style()))
        self.outline_check.toggled.connect(self._apply_overlay_style)
        self.text_color_button.clicked.connect(lambda: self._pick_color("text"))
        self.bg_color_button.clicked.connect(lambda: self._pick_color("bg"))
        self.outline_color_button.clicked.connect(lambda: self._pick_color("outline"))
        self._text_color = QColor(self.c.get("overlay_text_color"))
        self._bg_color = QColor(self.c.get("overlay_bg_color"))
        self._outline_color = QColor(self.c.get("overlay_outline_color"))
        self._update_color_button(self.text_color_button, self._text_color)
        self._update_color_button(self.bg_color_button, self._bg_color)
        self._update_color_button(self.outline_color_button, self._outline_color)
        page._inner_layout.addStretch()
        return page

    # ---------- 页面：通用 ----------

    def _page_general(self):
        page = self._page()
        self._section(page, "窗口行为")
        self.close_combo = QComboBox()
        self.close_combo.addItem("每次询问", "ask")
        self.close_combo.addItem("隐藏到托盘（字幕继续）", "tray")
        self.close_combo.addItem("直接退出程序", "exit")
        self._row(page, "点击关闭按钮时",
                  "「隐藏到托盘」后主窗口消失，识别与翻译在后台继续，悬浮字幕正常显示，可从任务栏右下角托盘图标重新打开主窗口。",
                  self.close_combo)
        self.auto_start_check = QCheckBox()
        self._row(page, "启动后自动开始翻译",
                  "打开软件后自动按上次配置开始识别与翻译，适合固定场景挂机使用。",
                  self.auto_start_check)

        self._section(page, "字幕记录")
        self.max_history_spin = QSpinBox()
        self.max_history_spin.setRange(50, 500)
        self.max_history_spin.setSingleStep(50)
        self._row(page, "主窗口最多保留字幕条数",
                  "超出后自动清理最早的记录，避免长时间运行占用内存。",
                  self.max_history_spin)
        clear_btn = QPushButton("清空翻译缓存")
        self._row(page, "翻译缓存",
                  "相同文本的翻译结果会本地缓存以加速显示；清空后下次重新翻译。不影响字幕记录。",
                  clear_btn)
        clear_btn.clicked.connect(self._clear_cache)

        self.close_combo.currentIndexChanged.connect(
            lambda _i: self._save("close_action", self.close_combo.currentData()))
        self.auto_start_check.toggled.connect(lambda v: self._save("auto_start", bool(v)))
        self.max_history_spin.valueChanged.connect(lambda v: self._save("max_history", int(v)))
        page._inner_layout.addStretch()
        return page

    def _gl(self, text):
        lab = QLabel(text)
        lab.setObjectName("SettingTitle")
        return lab

    # ---------- 保存与应用 ----------

    def _save(self, key, value):
        self.c.set(key, value)
        self.settings_saved.emit()
        self._flash_saved()

    def _flash_saved(self):
        self.saved_label.setText("✓ 设置已自动保存")
        self.saved_label.show()
        # 连续调整时只保留最后一次计时，避免闪烁
        timer = getattr(self, "_saved_timer", None)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self.saved_label.hide)
            self._saved_timer = timer
        timer.start(1600)

    def _save_combo(self, key, combo):
        self._save(key, combo.currentData())

    def _on_source_changed(self):
        self._save_combo("source_type", self.source_combo)
        # 切换采集来源时重置设备，避免把系统声音的回环设备索引带进麦克风模式（反之亦然）
        self.c.set("device_index", -1)
        self._load_devices()

    def _on_engine_changed(self):
        self._save_combo("engine", self.engine_combo)
        self._save_combo("target_lang", self.target_combo)
        self._refresh_argos_section()

    def _on_overlay_toggle(self, checked):
        self._save("overlay_enabled", bool(checked))
        self.main.set_overlay_enabled(checked)

    def _apply_overlay_style(self, *_):
        self._save("overlay_font_size", int(self.overlay_font_spin.value()))
        self._save("overlay_text_color", self._text_color.name())
        self._save("overlay_bg_color", self._bg_color.name())
        self._save("overlay_bg_opacity", int(self.bg_opacity_slider.value()))
        self._save("overlay_outline", bool(self.outline_check.isChecked()))
        self._save("overlay_outline_width", int(self.outline_width_spin.value()))
        self._save("overlay_outline_color", self._outline_color.name())
        self.main.apply_overlay_from_config()

    def _pick_color(self, which):
        from PySide6.QtWidgets import QColorDialog
        cur = {"text": self._text_color, "bg": self._bg_color, "outline": self._outline_color}[which]
        color = QColorDialog.getColor(cur, self, "选择颜色")
        if not color.isValid():
            return
        if which == "text":
            self._text_color = color
            self._update_color_button(self.text_color_button, color)
        elif which == "bg":
            self._bg_color = color
            self._update_color_button(self.bg_color_button, color)
        else:
            self._outline_color = color
            self._update_color_button(self.outline_color_button, color)
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
        """悬浮字幕在设置窗口之外被开关（如右键关闭字幕条）时，同步本页复选框。"""
        self.overlay_check.blockSignals(True)
        self.overlay_check.setChecked(bool(checked))
        self.overlay_check.blockSignals(False)

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
        seen = set()
        for i in range(self.device_combo.count() - 1, -1, -1):
            key = self.device_combo.itemText(i).replace(" ", "").lower()[:20]
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
        idx = self.device_combo.findData(self.c.get("device_index"))
        if idx >= 0:
            self.device_combo.setCurrentIndex(idx)
        else:
            # 找不到已存设备（如麦克风索引混入了系统声音列表）时落到安全的默认项
            if source_type == "system":
                self.device_combo.setCurrentIndex(0)
            else:
                self.device_combo.setCurrentIndex(self.device_combo.count() - 1)
            self.c.set("device_index", self.device_combo.currentData())
        self.device_combo.blockSignals(False)

    def _refresh_argos_section(self):
        is_argos = self.engine_combo.currentData() == "argos"
        section_label = getattr(self, "argos_section_label", None)
        for w in (self.argos_combo, self.argos_download_button, self.argos_hint, section_label):
            if w is not None:
                w.setVisible(is_argos)
        downloading = bool(getattr(self, "argos_worker", None) and self.argos_worker.isRunning())
        self.argos_progress.setVisible(is_argos and downloading)
        if not is_argos:
            return
        tgt = self.target_combo.currentData() or "zh-CN"
        argos_tgt = "zh" if tgt.startswith("zh") else tgt
        tgt_name = LANGUAGES.get(tgt, argos_tgt)
        self.argos_combo.clear()
        common = ["en", "ja", "ko", "ru", "fr", "de", "es", "pt", "it", "th", "vi", "ar", "id", "hi"]
        installed = set(ArgosEngine.installed_pairs())
        for code in common:
            name = LANGUAGES.get(code, code)
            if (code, argos_tgt) in installed:
                name += "（已安装）"
            self.argos_combo.addItem(name, code)
        self.argos_download_button.setText(f"下载所选 → {tgt_name} 语言包")
        if installed:
            self.argos_hint.setText(
                f"已安装 {len(installed)} 个语言包；请下载与「识别语言 → 翻译目标」一致的方向，一次下载永久离线使用。")
        else:
            self.argos_hint.setText(
                "请下载与「识别语言 → 翻译目标」一致的语言包（约 70MB，一次下载永久离线使用）。下载优先走本项目镜像，失败自动回退官方源。")

    def _download_argos(self):
        code = self.argos_combo.currentData()
        if not code:
            return
        if getattr(self, "argos_worker", None) and self.argos_worker.isRunning():
            return
        tgt = self.target_combo.currentData() or "zh-CN"
        argos_tgt = "zh" if tgt.startswith("zh") else tgt
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
        if getattr(self, "argos_worker", None) and self.argos_worker.isRunning():
            self.hide()
            event.ignore()
            return
        event.accept()

    def load_from_config(self):
        c = self.c

        def set_combo(combo, key):
            idx = combo.findData(c.get(key))
            if idx >= 0:
                combo.setCurrentIndex(idx)

        self.source_combo.blockSignals(True)
        set_combo(self.source_combo, "source_type")
        self.source_combo.blockSignals(False)
        self._load_devices()
        set_combo(self.model_combo, "asr_model")
        set_combo(self.asr_lang_combo, "asr_language")
        set_combo(self.compute_combo, "asr_device")
        set_combo(self.engine_combo, "engine")
        set_combo(self.target_combo, "target_lang")
        self._refresh_argos_section()
        self.overlay_check.blockSignals(True)
        self.overlay_check.setChecked(bool(c.get("overlay_enabled")))
        self.overlay_check.blockSignals(False)
        self.show_source_check.setChecked(bool(c.get("show_source")))
        self.overlay_font_spin.blockSignals(True)
        self.overlay_font_spin.setValue(int(c.get("overlay_font_size")))
        self.overlay_font_spin.blockSignals(False)
        self.bg_opacity_slider.blockSignals(True)
        self.bg_opacity_slider.setValue(int(c.get("overlay_bg_opacity")))
        self.bg_opacity_slider.blockSignals(False)
        self.bg_opacity_label.setText(f"{int(c.get('overlay_bg_opacity'))}%")
        self.outline_check.blockSignals(True)
        self.outline_check.setChecked(bool(c.get("overlay_outline")))
        self.outline_check.blockSignals(False)
        self.outline_width_spin.blockSignals(True)
        self.outline_width_spin.setValue(int(c.get("overlay_outline_width")))
        self.outline_width_spin.blockSignals(False)
        self._text_color = QColor(c.get("overlay_text_color"))
        self._bg_color = QColor(c.get("overlay_bg_color"))
        self._outline_color = QColor(c.get("overlay_outline_color"))
        self._update_color_button(self.text_color_button, self._text_color)
        self._update_color_button(self.bg_color_button, self._bg_color)
        self._update_color_button(self.outline_color_button, self._outline_color)
        set_combo(self.close_combo, "close_action")
        self.auto_start_check.setChecked(bool(c.get("auto_start")))
        self.max_history_spin.setValue(int(c.get("max_history")))
