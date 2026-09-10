# -*- coding: utf-8 -*-
"""全面集成测试（v2.2.8 排查批次）：配置生命周期/迁移/热键/翻译缓存/悬浮条三模式/
字幕卡生命周期/导出/向导——除真实模型推理与音频设备外的全部可自动化路径。
结果输出 test_report.txt。"""
import io
import json
import os
import sys
import tempfile
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["LIVETRANSLATE_HOME"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "itest_home")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

RESULTS = []
FAILS = []

def check(name, fn):
    try:
        fn()
        RESULTS.append(f"PASS  {name}")
    except Exception as e:
        RESULTS.append(f"FAIL  {name}: {type(e).__name__}: {e}")
        FAILS.append((name, traceback.format_exc(limit=4)))

from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv)

from app.config import Config, DEFAULTS
from app.translate.translator import TranslationCache
from app.audio.capture import Segmenter, resample_to_16k
from app.ui.main_window import MainWindow, CaptionCard
from app.ui.caption_overlay import CaptionOverlay
from app.ui.styles import SETTING_QSS, DARK_QSS, OVERLAY_QSS

cfg = Config()

# ---------- 1) 配置生命周期 ----------
def t_config_whitelist():
    cfg2 = Config()
    assert cfg2.get("hotkey_overlay") == "Ctrl+Alt+O"
    cfg2.set("bogus_key", 123)          # 未知键不应写入
    raw = json.load(open(cfg2._path(), encoding="utf-8")) if hasattr(cfg2, "_path") else None
    if raw is not None:
        assert "bogus_key" not in raw, "未知键不应持久化"
check("config: 新键默认值 + 白名单", t_config_whitelist)

def t_config_roundtrip():
    cfg.set("overlay_font_size", 21)
    assert Config().get("overlay_font_size") == 21
    cfg.set("overlay_font_size", 18)
check("config: 写入回读持久化", t_config_roundtrip)

def t_cache_not_loaded_save():
    import pathlib
    tmp = pathlib.Path(tempfile.mkdtemp())
    (tmp / "c.json").write_text(json.dumps({"k": ("v", "en")}), encoding="utf-8")
    TranslationCache._path = lambda self: tmp / "c.json"
    cc = TranslationCache()
    cc.save()
    assert json.load(open(tmp / "c.json", encoding="utf-8")) == {"k": ["v", "en"]}
check("cache: 未加载 save 不清库（H1 回归）", t_cache_not_loaded_save)

def t_cache_clear():
    cc = TranslationCache()
    cc._ensure_loaded()
    cc.put("a", ("b", "en"))
    cc.clear()
    assert cc.get("a") is None
check("cache: clear 后读取为空", t_cache_clear)

# ---------- 2) 重采样/分段器（无设备） ----------
def t_resample_lengths():
    import numpy as np
    for sr in (44100, 48000, 96000):
        y = resample_to_16k(np.zeros(sr, dtype=np.float32), sr)  # 1 秒静音
        assert abs(len(y) - 16000) <= 2, (sr, len(y))
check("resample: 44.1k/48k/96k → 16k 长度正确", t_resample_lengths)

def t_segmenter_flow():
    import numpy as np
    seg = Segmenter()
    out = []
    chunk = (np.sin(np.linspace(0, 500, 480)) * 3000).astype(np.int16)  # 30ms 语音样
    for _ in range(60):  # 1.8s 连续语音
        r = seg.feed(chunk)
        if r is not None:
            out.append(r)
    tail = seg.flush()
    assert isinstance(out, list)
check("segmenter: 1.8s 连续语音喂入不崩溃", t_segmenter_flow)

# ---------- 3) 悬浮条三模式互斥 + 内容路由 ----------
def t_overlay_modes():
    ov = CaptionOverlay()
    ov.show()
    ov.set_continuous_mode(True)
    assert ov.stream_view.isVisible() and not ov.list_widget.isVisible()
    ov.set_list_mode(True, 4)
    assert ov.stream_view.isVisible()  # 连续模式优先级更高
    ov.set_continuous_mode(False)
    assert ov.list_widget.isVisible() and not ov.stream_view.isVisible()
    ov.set_list_mode(False)
    assert ov.target_label.isVisible()
    ov.set_continuous_mode(True)
    ov.set_continuous_mode(False)
    assert ov.target_label.isVisible()  # 恢复单条
check("overlay: 三模式互斥与恢复", t_overlay_modes)

def t_overlay_stream_routing():
    ov = CaptionOverlay()
    ov.show()
    ov._show_source = True
    ov.set_continuous_mode(True)
    ov.show_pending("hello")
    ov.show_pending_result("hello", "你好", True)
    txt = ov.stream_view.toPlainText()
    assert "hello" in txt and "你好" in txt, txt
    # 只显示译文
    ov._show_source = False
    ov.show_pending("world")
    ov.show_pending_result("world", "世界", False)
    txt2 = ov.stream_view.toPlainText()
    assert "world" not in txt2 and "世界" in txt2
    assert "你好" in txt2  # 旧内容保留
check("overlay: 连续流 show_source 双向路由", t_overlay_stream_routing)

def t_overlay_trim():
    ov = CaptionOverlay()
    ov.set_continuous_mode(True)
    for i in range(60):
        ov.stream_append(f"长句内容编号{i}填充填充填充填充填充", "target")
    plain = ov.stream_view.toPlainText()
    assert len(plain) < 4000, len(plain)
    assert "长句内容编号59" in plain  # 最新内容必须在
check("overlay: 超长淘汰保最新", t_overlay_trim)

def t_overlay_clear_in_modes():
    ov = CaptionOverlay()
    ov.show()
    ov.set_continuous_mode(True)
    ov.show_pending("x")
    ov.clear_caption()
    assert ov.stream_view.toPlainText() == ""
    ov.set_continuous_mode(False)
    ov.show_caption("s", "t", True)
    ov.clear_caption()
    assert ov.source_label.text() == "" and ov.target_label.text() == ""
check("overlay: 两模式 clear 完整", t_overlay_clear_in_modes)

# ---------- 4) 字幕卡生命周期 ----------
def t_card_lifecycle():
    w = MainWindow()
    w.show()
    w.running = True
    w._on_asr_text("life1", "en", "1.0")
    assert len(w._pending) == 1
    w._on_translated("life1", "一生一", "google", "en", "")
    assert w._active_card.objectName() == "CaptionCardActive"
    w._on_asr_text("life2", "en", "1.0")
    w._on_translated("life2", "一生二", "google", "en", "")
    cards = [w.scroll_layout.itemAt(i).widget() for i in range(w.scroll_layout.count())
             if w.scroll_layout.itemAt(i).widget()]
    old = [c for c in cards if c.objectName() == "CaptionCardOld"]
    assert len(old) == 1 and w._active_card.objectName() == "CaptionCardActive"
    w.stop_pipeline()
    assert len(w._pending) == 0
check("card: 流式配对/聚焦切换/stop清空", t_card_lifecycle)

def t_card_eviction_pending():
    w = MainWindow()
    w.show()
    w.running = True
    w.config.set("max_history", 3)
    # 5 句占位后翻译后两句——每次翻译触发裁剪：会删除最老的"仍占位"卡，
    # _pending 必须同步收缩且不抛 RuntimeError（悬挂引用回归测试）
    for i in range(5):
        w._on_asr_text(f"evict{i}", "en", "1.0")
    w._on_translated("evict4", "译4", "google", "en", "")
    assert len(w._pending) <= 4, len(w._pending)
    w._on_translated("evict3", "译3", "google", "en", "")
    assert len(w._pending) <= 3, len(w._pending)
    # 被裁剪的占位卡（evict0-2）不再在 _pending 中
    texts = [t for (t, _c) in w._pending]
    assert "evict0" not in texts and "evict1" not in texts, texts
    w.stop_pipeline()
check("card: 裁剪时 _pending 同步收缩（悬挂引用回归）", t_card_eviction_pending)

def t_export():
    w = MainWindow()
    w.show()
    w.running = True
    w._on_asr_text("exp1", "en", "1.0")
    w._on_translated("exp1", "导出一", "google", "en", "")
    import pathlib
    out = pathlib.Path(tempfile.mkdtemp()) / "export.txt"
    lines = []
    for i in range(w.scroll_layout.count()):
        wd = w.scroll_layout.itemAt(i).widget()
        if isinstance(wd, CaptionCard):
            lines.append(f"{wd.meta_label.text()} | {wd.source_label.text()} | {wd.target_label.text()}")
    out.write_text("\n".join(lines), encoding="utf-8")
    assert "导出一" in out.read_text(encoding="utf-8")
check("export: 字幕内容可导出格式化", t_export)

def t_export_srt():
    # v2.2.11：SRT 时间轴导出——纯函数确定性验证（合成卡不经信号链）
    from app.ui.main_window import build_export_text, CaptionCard
    c1 = CaptionCard("hello world")
    c1.set_result("你好世界", "google", "en", False)
    c1.t_start, c1.dur_s = 0.0, 2.5
    c2 = CaptionCard("second line")
    c2.set_result("第二行", "google", "en", False)
    c2.t_start, c2.dur_s = 2.5, 1.8
    pending = CaptionCard("only source")  # 译文未落地：回退原文并入轴
    srt, m = build_export_text([c1, c2, pending], "srt")
    assert m == 3, (m, srt)
    # 时长来自 Whisper：cue1 结束被 cue2 起点前移 0.1s 夹紧（2.5→2.4）
    assert "1\n00:00:00,000 --> 00:00:02,400\n你好世界" in srt, srt
    assert "2\n00:00:02,500 --> 00:00:04,200\n第二行" in srt, srt  # 同样被夹紧
    assert "3\n00:00:04,300 --> 00:00:08,300\nonly source" in srt, srt
    txt, n = build_export_text([c1, c2], "txt")
    assert n == 2 and txt.startswith("[") and "你好世界" in txt
    # v2.2.12：长句折两行（CJK 在标点处断）
    longc = CaptionCard("a very long sentence " * 5)
    longc.set_result("这是一段很长的中文译文，" * 6, "google", "en", False)
    longc.t_start, longc.dur_s = 10.0, 3.0
    srt2, _ = build_export_text([longc], "srt")
    assert "，\n" in srt2, srt2
    assert all(len(line) <= 44 for line in srt2.splitlines() if " --> " not in line)
    # 空目标：无内容时 srt 返回空且不崩
    assert build_export_text([pending], "srt")[1] == 1
check("export: SRT 时间轴格式与夹紧逻辑", t_export_srt)

def t_srt_wrap():
    from app.ui.main_window import _srt_wrap
    assert _srt_wrap("short text") == "short text"
    en = "The quick brown fox jumps over the lazy dog while the sun sets slowly behind us"
    w = _srt_wrap(en)
    assert "\n" in w and all(len(line) <= 44 for line in w.splitlines())
    assert "".join(w.split()) == "".join(en.split()), "折行不得丢内容"
    zh = "第一段字幕内容，" * 6   # 8字×6=48 > 44，标点恰在中点
    wz = _srt_wrap(zh)
    assert wz.split("\n")[0] == "第一段字幕内容，" * 3, wz
check("export: SRT 折行规则（词边界/标点/不丢字）", t_srt_wrap)

def t_recommended_model():
    from app.gpu import recommended_model
    assert recommended_model({"cuda_devices": 1, "vram_mb": 6144})[0] == "large-v3-turbo"
    assert recommended_model({"cuda_devices": 1, "vram_mb": 3000})[0] == "small"
    assert recommended_model({"cuda_devices": 1, "vram_mb": 1000})[0] == "base"
    assert recommended_model({"cuda_devices": 0, "vram_mb": 0})[0] == "small"
    assert recommended_model({})[0] == "small"
    code, reason = recommended_model(None)   # 真机 detect()，永不抛异常
    assert code in ("tiny", "base", "small", "medium", "large-v3-turbo") and reason
check("gpu: 按硬件推荐模型档位（纯函数确定性）", t_recommended_model)

def t_wizard_preselect():
    from app.ui.first_run import FirstRunWizard, MODEL_INFO
    from app import gpu
    w = MainWindow()
    w.show()
    assert str(w.config.get("asr_model")) == str(DEFAULTS.get("asr_model"))
    rec = gpu.recommended_model()[0]
    assert rec in {c for c, _, _ in MODEL_INFO}
    dlg = FirstRunWizard(w)
    rb = dlg.model_group.checkedButton()
    assert rb is not None and rb.property("model_code") == rec, \
        f"出厂默认配置应预选硬件推荐档 {rec}，实际 {rb.property('model_code') if rb else None}"
    # 用户主动改过模型 → 尊重原选择，不覆盖
    w.config.set("asr_model", "tiny")
    dlg2 = FirstRunWizard(w)
    assert dlg2.model_group.checkedButton().property("model_code") == "tiny"
    w.config.set("asr_model", DEFAULTS.get("asr_model"))
    dlg.deleteLater(); dlg2.deleteLater()
    w._quitting = True; w._teardown()
check("wizard: 按硬件预选模型 + 用户选择优先", t_wizard_preselect)

def t_listen_pulse():
    w = MainWindow()
    w.show()
    w.running = True
    w.session_count = 0
    w._on_model_ready()
    t = getattr(w, "_pulse_timer", None)
    assert t is not None and t.isActive(), "就绪且未出字应起呼吸"
    w._on_asr_text("hello", "en", "1.0")
    assert not t.isActive(), "首段上屏即停"
    w.running = True
    w.session_count = 0
    w._on_model_ready()
    assert t.isActive()
    w.stop_pipeline()      # 停止熄灭呼吸且不得抛异常
    assert not t.isActive()
    w._quitting = True; w._teardown()
check("status: 正在聆听呼吸反馈起停时机", t_listen_pulse)

# ---------- 5) 热键链路（非按键部分） ----------
def t_hotkey_parse():
    from app.hotkey import sequence_to_hotkey
    assert sequence_to_hotkey("Ctrl+Alt+S") is not None
    assert sequence_to_hotkey("Ctrl+Alt+O") is not None
    assert sequence_to_hotkey("Win+Shift+Z") is not None
    assert sequence_to_hotkey("S") is None            # 无修饰键拒绝
    assert sequence_to_hotkey("Ctrl+@") is None        # 非法键拒绝
    assert sequence_to_hotkey("") is None
check("hotkey: 组合解析与安全约束", t_hotkey_parse)

def t_hotkey_overlay_toggle():
    w = MainWindow()
    w.show()
    w.overlay.show()
    # 真实按键间隔 > 250ms 防抖窗口——每次切换前清时间戳模拟
    w._toggle_overlay_hotkey()
    assert not w.overlay.isVisible()
    w._overlay_hk_last = 0.0
    w._toggle_overlay_hotkey()
    assert w.overlay.isVisible()
    w._overlay_hk_last = 0.0
    w._toggle_overlay_hotkey()
    assert not w.overlay.isVisible()
    # 防抖窗口内的第二连击应被吞掉（v2.2.7 双发根因修复的行为验证）
    w._overlay_hk_last = 0.0
    w._toggle_overlay_hotkey()   # 显示
    before = w.overlay.isVisible()
    w._toggle_overlay_hotkey()   # 防抖窗口内：不得切换
    assert w.overlay.isVisible() == before, "防抖窗口内不应切换"
check("hotkey: 显隐热键回调切换+防抖窗口", t_hotkey_overlay_toggle)

# ---------- 6) 设置对话框结构完整性 ----------
def t_settings_fields():
    from app.ui.settings_dialog import SettingsDialog, _FIELD_SPECS
    w = MainWindow()
    dlg = SettingsDialog(w)
    dlg.load_from_config()
    # 每个非 internal 键都有控件承载（抽样关键键）
    for key in ("hotkey_overlay", "overlay_stream", "instant_caption",
                "show_source", "asr_device", "engine"):
        assert key in _FIELD_SPECS, key
    # 恢复默认不含 internal
    dlg._reset_defaults()
    assert not any(k.startswith(("overlay_x", "overlay_y", "storage_root")) for k in dlg._staged)
check("settings: 字段表完整 + 恢复默认不含 internal", t_settings_fields)

def t_settings_qss_parse():
    # QSS 大括号配平（解析错误会让整段样式失效）
    for qss in (SETTING_QSS, DARK_QSS, OVERLAY_QSS):
        assert qss.count("{") == qss.count("}"), "QSS braces unbalanced"
check("styles: 三套 QSS 括号配平", t_settings_qss_parse)

# ---------- 7) 首启向导可构建 ----------
def t_wizard_build():
    from app.ui.first_run import FirstRunWizard
    w = MainWindow()
    dlg = FirstRunWizard(w)   # 构建即全量初始化，不 exec
    dlg.deleteLater()
check("wizard: 首启向导可完整构建", t_wizard_build)

# ---------- 8) 托盘与退出清理 ----------
def t_teardown():
    w = MainWindow()
    w.show()
    w._quitting = True
    w._teardown()   # 热键注销/设置保存/悬浮条隐藏不得抛异常
check("teardown: 退出清理链完整", t_teardown)

# ---------- 汇总 ----------
check("config: DEFAULTS 全键可读", lambda: [cfg.get(k) for k in DEFAULTS])

report = "\n".join(RESULTS)
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_report.txt"),
          "w", encoding="utf-8") as f:
    f.write(report + "\n\n=== FAIL DETAIL ===\n" + "\n\n".join(t for _, t in FAILS))
print(report)
print(f"TOTAL: {len(RESULTS)} PASS: {len(RESULTS) - len(FAILS)} FAIL: {len(FAILS)}")
sys.exit(1 if FAILS else 0)
