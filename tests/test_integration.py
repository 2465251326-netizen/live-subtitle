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

import faulthandler

RESULTS = []
FAILS = []

# v2.3.8 诊断：崩溃时自动 dump 全部线程栈到 stderr（定位退出 AV 肇事线程）
import faulthandler as _fh
_fh.enable()

# v2.3.20（测试自愈）：与 test_units 同款——mkdtemp 全局记账、每项测试后回收
# （集成侧曾有 2 处 mkdtemp 无清理：%TEMP% 导出 txt + 缓存状态目录树）
_MADE_TMP = []
_orig_mkdtemp = tempfile.mkdtemp


def _tracked_mkdtemp(*a, **k):
    d = _orig_mkdtemp(*a, **k)
    _MADE_TMP.append(d)
    return d


tempfile.mkdtemp = _tracked_mkdtemp


def _purge_tmp():
    import shutil
    while _MADE_TMP:
        try:
            shutil.rmtree(_MADE_TMP.pop(), ignore_errors=True)
        except Exception:
            pass

def check(name, fn):
    # v2.3.6：逐项进度打印——套件曾出现间歇性挂死（Qt 收尾竞态/设备枚举），
    # 无进度时无法定位挂点；卡住时看最后一行 [it] 即嫌疑测试
    print(f"[it] {name} ...", flush=True)
    # v2.3.6：单测级看门狗——任何一项卡住 120 秒即 dump 全部线程栈并杀进程，
    # 把"静默挂 5 分钟"变成"带栈 2 分钟定性失败"
    faulthandler.dump_traceback_later(120, exit=True)
    try:
        fn()
        RESULTS.append(f"PASS  {name}")
    except Exception as e:
        RESULTS.append(f"FAIL  {name}: {type(e).__name__}: {e}")
        FAILS.append((name, traceback.format_exc(limit=4)))
    finally:
        faulthandler.cancel_dump_traceback_later()
        _purge_tmp()

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

# ---------- 3) 字幕面板（v2.4.0）：成对行 / 路由 / 淘汰 / 清空 / 收起 ----------
def t_overlay_pairing():
    ov = CaptionOverlay()
    ov.show()
    ov.set_show_source(True)
    ov.show_pending("hello")
    assert len(ov._rows) == 1 and ov._rows[0]["pending"]
    assert "⟳" in ov._rows[0]["tgt"].text()
    ov.show_pending_result("hello", "你好", True)
    assert len(ov._rows) == 1 and not ov._rows[0]["pending"]   # 原地补齐不新建
    assert ov._rows[0]["src"].text() == "hello" and ov._rows[0]["tgt"].text() == "你好"
    # 原文关：新行原文隐藏，译文照常（旧内容不追溯隐藏已见行——与主窗一致按当下开关）
    ov.show_pending("world")
    ov.show_pending_result("world", "世界", False)
    assert len(ov._rows) == 2
    assert not ov._rows[1]["src"].isVisible() and ov._rows[1]["tgt"].text() == "世界"
    ov.deleteLater()
check("panel: 成对行与占位补齐", t_overlay_pairing)

def t_overlay_row_never_flash_window():
    # v2.6.6 瞬窗回归锁：行标签"构造即父挂载"——曾因 QLabel() 无父构造后先
    # setVisible(True) 再 addWidget，使未收编标签以顶层窗口身份建出原生窗口，
    # 用户看到"每来一句字幕闪一个 40ms 的无题小窗"（开"同时显示原文"必现）。
    # isWindow()==True 的控件一旦被 show 就是独立窗口；锁定该不变式即锁死病根。
    ov = CaptionOverlay()
    ov.show()
    ov.set_show_source(True)
    ov.show_pending("hello")
    it = ov._rows[0]
    assert it["src"].parentWidget() is it["row"], "PanelSrc 必须挂在 PanelRow 下"
    assert it["tgt"].parentWidget() is it["row"], "PanelTgt 必须挂在 PanelRow 下"
    assert not it["src"].isWindow(), "行标签不得是顶层窗口（瞬窗回归）"
    assert not it["tgt"].isWindow(), "行标签不得是顶层窗口（瞬窗回归）"
    ov.show_pending_result("hello", "你好", True)
    assert not it["src"].isWindow()          # 补齐路径同样不得逃逸成顶层窗口
    ov.deleteLater()
check("panel: 行标签永不成顶层窗口（v2.6.6 瞬窗回归）", t_overlay_row_never_flash_window)

def t_overlay_pending_pairing_no_crosstalk():
    # v2.7.0（T1）：旧"单待决槽"设计里，下一句覆盖槽后上一句迟到译文会
    # 配到错误原文行。现多待决并存+按原文匹配；merged_from 收编攒句前片。
    ov = CaptionOverlay()
    ov.show()
    ov.set_show_source(True)
    ov.show_pending("Hello world.")
    ov.show_pending("Another thing happened.")          # 大写开头=新句，并存不覆盖
    ov.show_pending_result("Hello world.", "你好世界", True)
    r1 = [r for r in ov._rows if r["src_text"] == "Hello world."][0]
    r2 = [r for r in ov._rows if r["src_text"] == "Another thing happened."][0]
    assert r1["tgt"].text() == "你好世界" and not r1["pending"]
    assert "⟳" in r2["tgt"].text() and r2["pending"], "B 行不得被 A 的译文填走"
    ov.show_pending_result("Another thing happened.", "另一件事", True)
    assert r2["tgt"].text() == "另一件事" and not r2["pending"]
    # 同句双发幂等（主窗每段调两次 show_pending）
    n0 = len(ov._rows)
    ov.show_pending("Dup check.")
    ov.show_pending("Dup check.")
    assert len(ov._rows) == n0 + 1, "重复调用不得建行"
    # 延续片在当前待决行上生长
    ov.show_pending_result("Dup check.", "重复检查", True)
    ov.show_pending("keep going")
    ov.show_pending("keep going now")                   # 小写开头=延续→生长
    assert sum(1 for r in ov._rows if r["pending"]) == 1
    ov.show_pending_result("keep going now", "继续走", True)
    # merged_from：整句结果落地时收编被并入的前片占位行
    ov.show_pending("Alpha.")
    ov.show_pending("Beta.")
    ov.show_pending_result("Beta.", "贝塔", True, merged_from=["Alpha."])
    assert all(r["src_text"] != "Alpha." for r in ov._rows), "前片占位行应被移除"
    assert not any(r["pending"] for r in ov._rows)
    ov.deleteLater()
check("panel: 待决行按原文配对不串线（v2.7.0 T1）", t_overlay_pending_pairing_no_crosstalk)

def t_registration_bidirectional():
    # v2.7.4：登记完整性双向断言——旧测试只测 SPECS⊇STD 方向，DEFAULTS 里
    # "有读有写却漏登记"的键（曾藏 overlay_w/h/pin/collapsed/hint_shown 5 个）
    # 永远不会被抓出来。现补 DEFAULTS ⊆ SPECS 方向。
    from app.config import DEFAULTS
    from app.ui.settings_dialog import _FIELD_SPECS
    missing = set(DEFAULTS) - set(_FIELD_SPECS)
    assert not missing, f"DEFAULTS 有键未登记 _FIELD_SPECS: {missing}"
check("settings: 登记完整性双向断言（v2.7.4）", t_registration_bidirectional)

def t_config_type_sanitizer():
    # v2.7.4（A-2 活体实锤）：手编配置逐键消毒——"18px"型毒药启动即崩、
    # list 型词典值每段抛全场零字幕，load 后必须回落到可用值
    import json
    from app.config import Config, DEFAULTS
    from app import config as cfgmod
    p = cfgmod.CONFIG_FILE
    backup = p.read_text(encoding="utf-8") if p.exists() else None
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "overlay_font_size": "18px",   # int 原型 + 坏字符串 → 回默认
            "max_history": "200",          # int 原型 + 数字字符串 → 挽救成 200
            "mishear_map": {"a": ["b"]},   # dict[str,str] 原型 + 坏值 → 整键回默认
            "show_source": "yes",          # bool 原型 + 字符串 → 回默认
            "engine": 42,                  # str 原型 + 数字 → 回默认
        }), encoding="utf-8")
        c = Config()
        assert c.get("overlay_font_size") == DEFAULTS["overlay_font_size"]
        assert c.get("max_history") == 200
        assert c.get("mishear_map") == {}
        assert isinstance(c.get("show_source"), bool)
        assert c.get("engine") == DEFAULTS["engine"]
    finally:
        if backup is not None:
            p.write_text(backup, encoding="utf-8")
        elif p.exists():
            p.unlink()
check("config: 手编毒药逐键消毒（v2.7.4 A-2）", t_config_type_sanitizer)

def t_export_filters_placeholder():
    # v2.7.4（B-12）：txt 导出与 SRT 同一过滤集——"⟳ …"占位与失败卡不得混进导出
    from app.ui.main_window import build_export_text

    class FakeLabel:
        def __init__(self, t): self._t = t
        def text(self): return self._t
        def isVisible(self): return bool(self._t)
        def isVisibleTo(self, _w): return bool(self._t)

    class FakeCard:
        def __init__(self, meta, src, tgt):
            self.meta_label, self.source_label, self.target_label = (
                FakeLabel(meta), FakeLabel(src), FakeLabel(tgt))
    cards = [FakeCard("12:00:00 · en", "hello", "你好"),
             FakeCard("12:00:05 · en", "world", "⟳ …"),
             FakeCard("12:00:10 · en", "boom", "[翻译失败]")]
    txt, n = build_export_text(cards, "txt")
    assert "你好" in txt and "⟳" not in txt and "翻译失败" not in txt, txt
    srt, _ = build_export_text(cards, "srt")
    assert "⟳" not in srt
check("export: 占位与失败卡双出口过滤（v2.7.4 B-12）", t_export_filters_placeholder)

def t_early_flush_decision():
    # v2.7.0（T2）：提前冲句判据——开关开时静默 2.0s 地板即送；静默不足不送；
    # 开关关回 3.5s 旧地板。deadline 拉远隔离兜底路径。
    # （审计原设想 1.2s+段内尾静音证据——真机证伪：whisper 末片时间戳拉伸到
    #  音频尾，tail_q 恒≈0；降为纯时间地板一档，定位=末句抢救）
    import time as _t
    w = MainWindow()
    w.config.set("low_latency_mode", True)
    w.config.set("early_flush", True)
    w._tgroup = ["hello there."]
    w._tgroup_start = _t.monotonic()
    w._tgroup_deadline = _t.monotonic() + 99
    w._last_level_sound = _t.monotonic() - 2.2          # 静默 2.2s：>2.0 且 <3.5
    flushed = []
    w._flush_tgroup = lambda: flushed.append(1)
    w._tgroup_tick()
    assert flushed, "开关开+静默 2.2s 应提前冲"
    flushed.clear()
    w._last_level_sound = _t.monotonic() - 1.5          # 静默不足 2.0s
    w._tgroup_tick()
    assert not flushed, "静默未达地板不得冲"
    flushed.clear()
    w._last_level_sound = _t.monotonic() - 2.2
    w.config.set("early_flush", False)                  # 开关关→旧 3.5s 地板
    w._tgroup_tick()
    assert not flushed, "开关关闭必须回到 3.5s 旧行为"
    # v2.7.2：榨干模式再压一档——静默 1.5s 即冲（地板 1.2），早于 early_flush 的 2.0
    w.config.set("early_flush", True)
    w.config.set("perf_turbo", True)
    flushed.clear()
    w._last_level_sound = _t.monotonic() - 1.5
    w._tgroup_tick()
    assert flushed, "榨干模式 1.5s 静默应冲（地板 1.2）"
    w.config.set("perf_turbo", False)
    tt = getattr(w, "_tgroup_timer", None)
    if tt is not None:
        tt.stop()
    w.deleteLater()
check("pipeline: 提前冲句判据与开关（v2.7.0 T2）", t_early_flush_decision)

def t_asr_finished_closes_translate_input():
    # v2.7.3：识别线程退场→冲刷攒句残组+关闭翻译输入门（孤儿线程根治的
    # 主窗半边）；旧会话 asr 的 finished 被身份守卫拦截
    from app.asr.engine import AsrThread
    w = MainWindow()
    events = []

    class FakeTr:
        def submit(self, text, lang):
            events.append(("submit", text))
            return []

        def isRunning(self):
            return True   # v2.7.4（B-1）：_flush_tgroup 新增死队列检查，stub 须可运行

        def close_input(self):
            events.append(("close",))

    asr = AsrThread("tiny", "cpu", "auto")
    w._sid_asr = asr
    w.translate_thread = None
    w._sid_tr = FakeTr()
    w._tgroup = ["tail frag"]
    w._tgroup_lang = "en"
    w._tgroup_start = __import__("time").monotonic()
    asr.finished.connect(w._on_asr_finished)
    asr.finished.emit()
    assert ("submit", "tail frag") in events, "残组必须在关门前冲刷给翻译"
    assert ("close",) in events, "翻译输入门必须关闭"
    assert not w._tgroup
    # 守卫：旧线程 finished 而 _sid_asr 已换新 → 不冲不关
    events.clear()
    old = AsrThread("tiny", "cpu", "auto")
    w._sid_asr = AsrThread("tiny", "cpu", "auto")   # 已是新会话
    w._tgroup = ["new session frag"]
    old.finished.connect(w._on_asr_finished)
    old.finished.emit()
    assert events == [], "旧会话 finished 不得触碰新会话残组/输入门"
    assert w._tgroup == ["new session frag"]
    w.deleteLater()
check("pipeline: asr 退场冲刷残组并关翻译输入门（v2.7.3 孤儿根治）",
      t_asr_finished_closes_translate_input)

def t_overlay_trim():
    ov = CaptionOverlay()
    for i in range(60):
        ov.show_caption(f"src{i}", f"译文内容{i}", True)
    assert len(ov._rows) <= ov.MAX_ROWS, len(ov._rows)
    assert ov._rows[-1]["tgt_text"] == "译文内容59"          # 最新必在
    assert all("src0" != it["src_text"] for it in ov._rows)  # 最旧已淘汰
    ov.deleteLater()
check("panel: 历史淘汰保最新", t_overlay_trim)

def t_overlay_clear():
    ov = CaptionOverlay()
    ov.show_pending("x")
    ov.show_caption("s", "t", True)
    ov.clear_caption()
    assert ov._rows == [] and ov._last_result == ("", "")
    ov.deleteLater()
check("panel: 清空彻底", t_overlay_clear)

def t_overlay_collapse_and_bar():
    # 收起态：正文隐藏、按钮文案翻转、回调持久化；工具条语言/字号回调
    got = {"collapsed": [], "lang": [], "font": []}
    ov = CaptionOverlay(on_collapsed=got["collapsed"].append,
                        on_language=got["lang"].append,
                        on_font_size=got["font"].append)
    ov.show()
    h_full = ov._scroll.height()
    ov._toggle_collapse()
    assert got["collapsed"] == [True] and not ov._scroll.isVisible()
    assert ov._collapse_btn.text() == "展开"
    ov._toggle_collapse()
    assert got["collapsed"] == [True, False] and ov._scroll.isVisible()
    assert ov._scroll.height() >= h_full or ov._scroll.height() >= 46
    # 语言菜单：选英语 → 回调 + 按钮文案更新
    acts = ov._lang_btn.menu().actions()
    en = [a for a in acts if a.text() == "英语"][0]
    en.trigger()
    assert got["lang"] == ["en"] and "英语" in ov._lang_btn.text()
    ov.set_target_lang("zh-CN")   # 回写不重复触发回调
    assert got["lang"] == ["en"]
    # 字号菜单
    facts = ov._font_btn.menu().actions()
    big = [a for a in facts if "大号" in a.text()][0]
    big.trigger()
    assert got["font"] == [30]
    ov.deleteLater()
check("panel: 收起态与工具条回调", t_overlay_collapse_and_bar)

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
    # v2.3.0：复原共享 itest_home 的配置——此前泄漏 max_history=3 污染后续测试
    # （被"声明式行表回显全覆盖"测试当场抓获，正是该测试价值的自证）
    w.config.set("max_history", int(DEFAULTS["max_history"]))
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

def t_overlay_status_row():
    # v2.3.2（G1）：速览卡"悬浮字幕条"状态行跟随显隐（用户痛点：关了没人说）
    # v2.7.4（B-9）：文案改"配置真值+注册实况"三分支——本测试同时锁三态
    from app import hotkey as hk
    w = MainWindow()
    w.show()
    lab = w._quick_labels["字幕面板"]
    w.overlay.hide()
    orig = hk.overlay_text
    try:
        hk.overlay_text = lambda: "Ctrl+Alt+O"          # 已注册：如实给组合键
        w._refresh_quick_panel()
        assert "已关闭" in lab.text() and "按 Ctrl+Alt+O 打开" in lab.text(), lab.text()
        assert "fbbf24" in lab.styleSheet()
        hk.overlay_text = lambda: ""                    # 配置了但没注册成功：不谎称可用
        w._refresh_quick_panel()
        assert "未生效" in lab.text(), lab.text()
        w.config.set("hotkey_overlay", "")              # 压根没配：指路设置页
        w._refresh_quick_panel()
        assert "设置-显示可重新开启" in lab.text(), lab.text()
    finally:
        hk.overlay_text = orig
        w.config.set("hotkey_overlay", "Ctrl+Alt+O")
    w.overlay.show()
    w._refresh_quick_panel()
    assert "已开启" in lab.text() and lab.styleSheet() == "", (lab.text(), lab.styleSheet())
    w.overlay.hide()
    w._quitting = True
    w._teardown()
check("quickpanel: 悬浮字幕条状态行跟随显隐（G1+B-9 三分支）", t_overlay_status_row)

def t_engine_fallback_banner():
    # v2.3.2（G2）：在线引擎不可达→事前横幅，且不被后续常规状态覆盖
    w = MainWindow()
    w.show()
    w.running = True
    w._on_engine_fallback("Google 未通过，已选 mymemory", "google: ConnectTimeout")
    assert getattr(w, "_engine_fallback_warn", None) and "ConnectTimeout" in w._engine_fallback_warn
    assert w.alert_banner.isVisibleTo(w) and "不可达" in w.alert_banner.text()
    w._set_engine_status("翻译: 识别完成")
    assert "不可达" in w.alert_banner.text(), "常规状态不得清掉预警横幅"
    w.running = False
    w._quitting = True
    w._teardown()
check("translate: 在线引擎不可达事前横幅持续（G2）", t_engine_fallback_banner)

def t_select_engine_ex():
    # v2.3.2（G2）：select_engine_ex 返回 (选用, 失败原因列表)——横幅的数据源
    import app.translate.translator as tr
    orig = tr.probe_engine
    try:
        tr.probe_engine = lambda name, timeout=2.5: (
            name == "mymemory", "OK" if name == "mymemory" else "ConnectTimeout: proxy down")
        eng, fails = tr.select_engine_ex()
        assert eng == "mymemory" and len(fails) == 1 and "google" in fails[0], (eng, fails)
    finally:
        tr.probe_engine = orig
check("translate: select_engine_ex 失败原因收集", t_select_engine_ex)

def t_overlay_snap():
    # v2.3.3（P2）：贴边逻辑——v2.3.6 改为纯几何验证（不 show、不泵事件）：
    # offscreen 下真实 show()+processEvents 偶发原生死锁（挂点漂移的元凶），
    # 而 _snap_to_edge 只依赖几何与回调，无需可见性
    from PySide6.QtGui import QGuiApplication
    moved = []
    ov = CaptionOverlay(on_moved=lambda x, y: moved.append((x, y)))
    ov.resize(400, 150)
    g = QGuiApplication.primaryScreen().availableGeometry()
    ov.move(g.center().x(), g.center().y())
    ov._snap_to_edge("top")
    assert ov.y() <= g.top() + 10, (ov.y(), g.top())
    assert moved and moved[-1][1] <= g.top() + 10
    ov._snap_to_edge("bottom")
    assert ov.y() + ov.height() >= g.bottom() - 20, (ov.y(), ov.height(), g.bottom())
    assert moved[-1][1] == ov.y()
    # v2.3.19（P25c）：左右缘磁吸
    ov._snap_to_edge("left")
    assert ov.x() <= g.left() + 12, (ov.x(), g.left())
    ov._snap_to_edge("right")
    assert ov.x() + ov.width() >= g.right() - 12, (ov.x(), ov.width(), g.right())
    ov.deleteLater()
check("overlay: 贴屏幕四缘一键归位（P2/P25c）", t_overlay_snap)

def t_panel_drag_contract():
    # v2.4.0：面板是"诚实的板"——整板可拖、右缘调宽、双击工具条贴边；
    # 穿透/紧凑带/三形态旧 API 必须已随旧字幕条退役（防幽灵回归）
    from PySide6.QtCore import QPoint, QPointF, Qt
    LB = Qt.MouseButton.LeftButton   # PySide6 枚举不与 int 互通，fake 事件必须用真枚举

    class _Ev:
        def __init__(self, x, y, gx=0, gy=0, buttons=LB):
            self._p, self._g, self._btns = QPoint(x, y), QPointF(gx, gy), buttons
        def button(self):
            return LB
        def buttons(self):
            return self._btns
        def position(self):
            return self._p
        def globalPosition(self):
            return self._g
        def accept(self):
            pass

    ov = CaptionOverlay()
    assert not hasattr(ov, "set_click_through"), "穿透 API 应已退役"
    assert not hasattr(ov, "_interactive_rects"), "紧凑带 API 应已退役"
    assert not hasattr(ov, "set_continuous_mode"), "三形态 API 应已退役"
    resized = []
    ov._on_resized = resized.append
    ov.resize(560, 150)
    ov.show()
    app.processEvents()
    p0 = (ov.x(), ov.y())
    ov.mousePressEvent(_Ev(100, 75, 500, 500))          # 板体任意处 = 拖
    assert ov._drag_pos is not None and not ov._resizing
    ov.mouseMoveEvent(_Ev(100, 75, 560, 540))           # 拖 +60/+40
    assert (ov.x(), ov.y()) == (p0[0] + 60, p0[1] + 40), (ov.x(), ov.y(), p0)
    ov.mouseReleaseEvent(_Ev(100, 75, 560, 540))
    assert ov._drag_pos is None
    ov.mousePressEvent(_Ev(ov.width() - 3, 75, 1000, 1000))  # 右缘 = 调宽
    assert ov._resizing and ov._drag_pos is None
    ov.mouseMoveEvent(_Ev(ov.width() - 3, 75, 1100, 1000))
    assert ov.width() >= 560
    ov.mouseReleaseEvent(_Ev(ov.width() - 3, 75, 1100, 1000))
    assert resized and resized[-1] == ov.width()       # 宽度回调持久化通道
    w_before = ov.width()
    ov.show_caption("probe source", "宽度稳定探针：这句较长，用于验证 adjustSize 不再横向改宽度。", True)
    assert ov.width() == w_before, "宽度不得随内容跳变（实机抓到过 722→432→698）"
    from PySide6.QtGui import QGuiApplication
    g = QGuiApplication.primaryScreen().availableGeometry()
    ov.move(g.center())
    ov.mouseDoubleClickEvent(_Ev(100, 10))             # 双击工具条 → 贴更近的缘
    assert ov.y() <= g.top() + 20 or ov.y() + ov.height() >= g.bottom() - 20, ov.y()
    ov.deleteLater()
check("panel: 整板拖移/右缘调宽/双击贴边契约（P31）", t_panel_drag_contract)

def t_panel_close_and_resize_edge():
    # v2.4.1 回归锁：① ✕ 关闭必须隐藏（v2.4.0 把 hide() 误放进 else 分支，主窗
    # 永远传 on_closed → 点了没反应）；② 右缘调宽命中带 = RESIZE_EDGE 且开鼠标追踪
    from PySide6.QtCore import QPoint, QPointF, Qt
    LB = Qt.MouseButton.LeftButton

    class _Ev:
        def __init__(s, x, y, gx=0, gy=0):
            s._p, s._g = QPoint(x, y), QPointF(gx, gy)
        def button(s): return LB
        def buttons(s): return LB
        def position(s): return s._p
        def globalPosition(s): return s._g
        def accept(s): pass

    closed = []
    ov = CaptionOverlay(on_closed=lambda: closed.append(1))
    ov.show(); app.processEvents()
    assert ov.hasMouseTracking(), "右缘调宽需鼠标追踪，否则悬停光标永不显示"
    assert ov.RESIZE_EDGE >= 12, "命中带太窄普通人够不着"
    # 命中带内按下 = 进入调宽（不误判为拖移）
    ov.mousePressEvent(_Ev(ov.width() - 3, 60, 800, 600))
    assert ov._resizing and ov._drag_pos is None
    ov.mouseReleaseEvent(_Ev(ov.width() - 3, 60, 800, 600))
    # ✕ 关闭：隐藏 + 回调都发生（回归点）
    ov._request_close()
    assert not ov.isVisible(), "✕ 必须真正隐藏面板（v2.4.0 回归）"
    assert closed == [1], "✕ 仍须回调主窗落盘 overlay_enabled"
    ov.deleteLater()
check("panel: ✕关闭隐藏+右缘命中带回归锁（v2.4.1）", t_panel_close_and_resize_edge)

def t_panel_body_transparent():
    # v2.4.1：空闲面板正文不得露出浅色矩形——QScrollArea 的 viewport 是独立子控件，
    # 父级 QSS 命中不到，必须直接挂 WA_TranslucentBackground，否则无字幕时是一大块灰
    ov = CaptionOverlay()
    ov.apply_style(22, "#ffffff", "#1c1f26", 92)
    ov.show()
    app.processEvents()
    pm = ov.grab()
    mid = pm.toImage().pixelColor(ov.width() // 2, int(ov.height() * 0.75))
    assert mid.red() < 120 and mid.green() < 120, \
        f"正文中部露出浅色底 {mid.name()}——viewport 未透明"
    ov.deleteLater()
check("panel: 空闲正文透明无灰块（v2.4.1）", t_panel_body_transparent)

def t_panel_clear_button_and_empty_hint():
    # v2.4.3（A/B）：工具条"清空"一键清面板（行/占位/最近一句全清），空状态
    # 占位提示随行数显隐；⋯/右键菜单同源"清空面板字幕"按有无内容门控
    ov = CaptionOverlay()
    ov.show(); app.processEvents()
    assert ov._hint.isVisible() and ov._hint.text().strip(), "空闲时应有占位提示"
    assert not ov._clear_btn.isEnabled(), "无内容时清空按钮置灰"
    ov.show_caption("hello", "你好", True)
    assert not ov._hint.isVisible(), "来字即隐"
    assert ov._clear_btn.isEnabled()
    ov.show_caption("world", "世界", True)
    ov._clear_btn.click()
    assert ov._rows == [], "行与占位必须全清"
    assert ov._last_result == ("", ""), "最近一句也要清（复制/纠错入口同步失效）"
    assert ov._hint.isVisible(), "清空后占位提示回归"
    assert not ov._clear_btn.isEnabled()
    menu = ov._build_menu()
    assert ov._menu_acts["clear"].text() == "清空面板字幕"
    assert not ov._menu_acts["clear"].isEnabled(), "空态时菜单清空置灰"
    menu.deleteLater()
    ov.show_caption("x", "y", True)
    menu = ov._build_menu()
    assert ov._menu_acts["clear"].isEnabled()
    menu.deleteLater()
    ov.deleteLater()
check("panel: 清空按钮+空状态占位（A/B）", t_panel_clear_button_and_empty_hint)

def t_panel_pin_toolbar_button():
    # v2.4.3（C）：📌 图钉上工具条——点击翻转置顶态、回调落盘、与菜单"置顶显示"同源
    pins = []
    ov = CaptionOverlay(on_pin_changed=lambda on: pins.append(bool(on)))
    ov.show(); app.processEvents()
    assert ov._pin_btn.isCheckable(), "图钉必须可勾选"
    assert ov._pin_btn.isChecked() == ov.is_pinned(), "初态与置顶态一致"
    ov._pin_btn.click()
    assert not ov.is_pinned() and pins == [False], "点击翻转 + 回调"
    assert not ov._pin_btn.isChecked()
    menu = ov._build_menu()
    assert not ov._menu_acts["pin"].isChecked(), "菜单勾选态与图钉同源"
    menu.deleteLater()
    ov._pin_btn.click()
    assert ov.is_pinned() and pins == [False, True]
    assert ov._pin_btn.isChecked()
    ov.deleteLater()
check("panel: 置顶图钉上工具条与菜单同源（C）", t_panel_pin_toolbar_button)

def t_panel_first_show_hint_once():
    # v2.4.3（D）：面板本进程首次显示发一次 on_first_show；引导文案含手势提示，
    # 且只在空状态展示（来字即随占位一起隐去）。（回调→文案升级的落盘接线
    # 由主窗级用例覆盖，此处直接调 show_first_hint 验证文案）
    calls = []
    ov = CaptionOverlay(on_first_show=lambda: calls.append(1))
    ov.show(); app.processEvents()
    assert calls == [1], "首次显示应触发一次回调"
    ov.hide(); ov.show(); app.processEvents()
    assert calls == [1], "回调每进程只发一次"
    ov.show_first_hint()
    assert "拖工具条" in ov._hint.text() and "右缘" in ov._hint.text(), \
        "引导文案应覆盖移动/调宽手势"
    ov.show_caption("a", "b", True)
    assert not ov._hint.isVisible()
    ov.deleteLater()
check("panel: 首次手势引导一次性（D）", t_panel_first_show_hint_once)

def t_panel_first_show_flag_persist():
    # v2.4.3（D）：主窗接线——注意首次显示发生在 MainWindow() 构造期内
    # （_load_settings 的启动恢复：overlay_enabled=True 即 show），所以必须在
    # 构造前用裸 Config 重置标记，构造后断言已落盘且文案升级为引导
    from app.config import Config
    cfg = Config()
    cfg.set("overlay_hint_shown", False)
    cfg.set("overlay_enabled", True)
    w = MainWindow()
    assert bool(w.config.get("overlay_hint_shown")), "构造期首次显示就应落盘标记"
    assert "拖工具条" in w.overlay._hint.text(), "空状态应升级为手势引导"
    w.overlay.deleteLater()
    w._quitting = True
    w._teardown()
check("panel: 首次引导标记落盘（D·主窗接线）", t_panel_first_show_flag_persist)

def t_overlay_resident_on_launch():
    """v2.10.0（启动即常驻）：上次退出时藏了面板（overlay_enabled=False 落盘），
    新会话构造 MainWindow 后也必须可见、且回写 True（设置页勾选/仪表盘/托盘
    语义一致）；热键隐藏只在本次运行内有效，toggle 显隐语义不变。"""
    from app.config import Config
    cfg = Config()
    orig = cfg.get("overlay_enabled")
    cfg.set("overlay_enabled", False)      # 模拟上次以"隐藏"状态退出
    try:
        w = MainWindow()
        assert w.overlay.isVisible(), "启动即常驻：上次落盘 False 也必须显示"
        assert bool(w.config.get("overlay_enabled")) is True, \
            "常驻状态应回写 True（设置页勾选/仪表盘/托盘保持一致）"
        ql = getattr(w, "_quick_labels", None)
        if ql:
            assert str(ql["字幕面板"].text()).startswith("已开启"), \
                "仪表盘必须显示'已开启'——先 show 再刷面板的顺序不许反"
        # 热键隐藏：当次生效并落盘 False（下次启动才恢复常驻）
        w._toggle_overlay_hotkey()
        assert not w.overlay.isVisible()
        assert bool(w.config.get("overlay_enabled")) is False
        w._overlay_hk_last = 0.0           # 解除 250ms 防抖（既有机制，非本测试对象）
        w._toggle_overlay_hotkey()
        assert w.overlay.isVisible(), "再按热键应重新显示（toggle 语义不变）"
        assert bool(w.config.get("overlay_enabled")) is True
        w._quitting = True
        w._teardown()
    finally:
        cfg.set("overlay_enabled", orig)
check("panel: 悬浮条启动常驻、热键隐藏当次有效", t_overlay_resident_on_launch)

def t_overlay_dual_layout():
    """v2.11.0：上下双语布局——原文区流式生长（新句重置/延续拼接）、推测版
    就地更新、终版收口校准、clear 占位恢复、纯译文显隐、收起态取值。"""
    from app.ui.caption_overlay import CaptionOverlay
    ov = CaptionOverlay()
    ov.show()
    ov.set_layout_mode("dual")
    assert ov.is_dual()
    assert ov._dual_body.isVisible() and not ov._scroll.isVisible()
    # 新句起点：原文上屏 + 译文占位（淡色推测态）
    ov.show_pending("The quick brown")
    assert ov._dual_src.text() == "The quick brown"
    assert ov._dual_tgt.text() == "…"
    assert ov._dual_tgt.property("spec") is True
    # 延续片段：整句打字机式生长（拉丁补空格）
    ov.show_pending("fox jumps")
    assert ov._dual_src.text() == "The quick brown fox jumps"
    # 推测版：原文校准整句、译文就地更新且保持推测态
    ov.update_spec_result("The quick brown fox jumps", "敏捷的棕色狐狸跳", True)
    assert ov._dual_tgt.text() == "敏捷的棕色狐狸跳"
    assert ov._dual_tgt.property("spec") is True
    # 终版收口：正式样式 + _last_result 落账
    ov.show_pending_result("The quick brown fox jumps", "敏捷的棕色狐狸跳了起来", True)
    assert ov._dual_tgt.text() == "敏捷的棕色狐狸跳了起来"
    assert ov._dual_tgt.property("spec") is False
    assert ov._last_result == ("The quick brown fox jumps", "敏捷的棕色狐狸跳了起来")
    # 下一句（大写开头）重置原文区；CJK 起始字符按既有 _starts_new_sentence
    # 语义同样判"新句"（中文源逐段重置是既有约定）；CJK 邻接直连由
    # _dual_join 兜底（小写起始片段被判延续时，与 CJK 尾字相邻不加空格）
    ov.show_pending("Over the lazy dog")
    assert ov._dual_src.text() == "Over the lazy dog"
    ov.show_pending("今天天气")
    assert ov._dual_src.text() == "今天天气"
    ov.show_pending("is fine")
    assert ov._dual_src.text() == "今天天气is fine"
    # 关原文开关 = 纯译文大字：原文行与分隔线隐藏
    ov._dual_show_result("Hello world", "你好世界", False)
    assert not ov._dual_src.isVisible() and not ov._dual_sep.isVisible()
    assert ov._dual_tgt.text() == "你好世界"
    # 收起态：迷你条从双语区取当前句
    ov._dual_show_result("Stay focused", "保持专注", True)
    ov.set_collapsed(True)
    assert ov._mini_tgt.text() == "保持专注"
    assert ov._mini_src.text() == "Stay focused"
    ov.set_collapsed(False)
    # 清空 → 占位恢复
    ov.clear_caption()
    assert ov._dual_src.text() == ""
    assert ov._dual_tgt.property("empty") is True
    # 切回列表模式：滚动区回归、双语区隐藏
    ov.set_layout_mode("list")
    assert not ov.is_dual()
    assert ov._scroll.isVisible() and not ov._dual_body.isVisible()
    ov.deleteLater()
check("panel: 上下双语布局（dual）全链路", t_overlay_dual_layout)

def t_overlay_dual_user_height_body():
    """v2.17.0：拉高面板 → **历史区吃剩余**（内容多正好多显示），当前句区
    贴内容（上限 45% 总高）——v2.14.1"拉高全给当前句区"在可滚动历史区之后
    严重失衡（用户截图"？？？"：当前句两三行配巨幅留白、历史被挤裁）。"""
    from app.ui.caption_overlay import CaptionOverlay
    ov = CaptionOverlay()
    ov.show()
    ov.set_layout_mode("dual")
    ov._dual_show_pending("The quick brown fox jumps over the lazy dog")
    for _ in range(4):
        app.processEvents()
    body_before = ov._dual_body.height()
    ov.set_user_height(620)
    for _ in range(6):
        app.processEvents()
    # 当前句区：贴内容（不因拉高被强行撑大产生巨幅留白）
    assert abs(ov._dual_body.height() - body_before) <= 8, \
        f"当前句区应贴内容，不随拉高暴涨：{body_before} -> {ov._dual_body.height()}"
    assert abs(ov.height() - 620) <= 14, f"面板总高锁定：{ov.height()} vs 620"
    # 历史行出现：吃剩余空间（显示更多历史）
    for i in range(6):
        ov.dual_push_history(f"Historical sentence number {i}",
                             f"历史句第 {i} 句")
    for _ in range(6):
        app.processEvents()
    assert ov._dual_hist.isVisible()
    assert ov._dual_hist.height() > ov._dual_body.height(), \
        f"拉高后剩余空间应归历史区：hist={ov._dual_hist.height()} body={ov._dual_body.height()}"
    ov.deleteLater()
check("panel: dual 拉高→历史区吃剩余、当前句贴内容", t_overlay_dual_user_height_body)

def t_overlay_dual_split_drag():
    """v2.15.0：分割把手——拖出的历史区高度（_dual_hist_h_user）被
    _relayout 尊重：拖大历史区则 body 让位、拖小则原文区变大；
    锁定总高与自动两种模式都生效。"""
    from app.ui.caption_overlay import CaptionOverlay
    ov = CaptionOverlay()
    ov.show()
    ov.set_layout_mode("dual")
    ov.resize(620, 200)
    for src, tgt in [("Sentence one here", "第一句在这里"),
                     ("Sentence two here", "第二句在这里")]:
        ov._dual_show_result(src, tgt, True)
        ov.dual_push_history(src, tgt)
    ov._dual_show_pending("Current sentence growing")
    for _ in range(6):
        app.processEvents()
    ov.set_user_height(700)
    for _ in range(6):
        app.processEvents()
    h_auto = ov._dual_hist.height()
    assert h_auto >= 56, f"历史区保底 56px：{h_auto}"
    # 模拟分割把手下拖 120px（历史区变大、原文区让位）——700 总高下不触顶
    ov.set_dual_hist_h_user(h_auto + 120)
    for _ in range(6):
        app.processEvents()
    h1 = ov._dual_hist.height()
    # body 保底 46 → 历史上限 = 700-66-46
    expect = min(h_auto + 120, 700 - 66 - 46)
    assert abs(h1 - expect) <= 12, f"分割拖拽应即时生效：{h_auto} -> {h1}（期望 {expect}）"
    assert ov._dual_body.height() <= 700 - 66 - 40 + 8, \
        f"原文区应给历史让位：{ov._dual_body.height()}"
    # 模拟分割把手上拖回保底（历史区变小、原文区变大）
    ov.set_dual_hist_h_user(56)
    for _ in range(6):
        app.processEvents()
    h2 = ov._dual_hist.height()
    assert 56 <= h2 <= 72, f"历史区应回缩到保底附近：{h2}"
    assert ov._dual_body.height() > 520 - 66 - 120, \
        f"历史缩小后原文区应变大：{ov._dual_body.height()}"
    ov.deleteLater()
check("panel: dual 分割线拖拽分配两区高度", t_overlay_dual_split_drag)

def t_dual_split_hit_global():
    """v2.15.1：分割命中必须用**全局坐标**——鼠标事件从子控件（QLabel/
    QScrollArea）传播到面板时 position 不重映射（相对原接收者），相对坐标
    判定永远错位（用户实测"还是不行"的根因）。"""
    from app.ui.caption_overlay import CaptionOverlay
    from PySide6.QtCore import QPoint
    ov = CaptionOverlay()
    ov.set_layout_mode("dual")
    ov.resize(620, 300)
    ov.move(80, 60)
    ov.show()
    app.processEvents()
    ov._dual_show_result("Hello world", "你好世界", True)
    ov.dual_push_history("Hello world", "你好世界")
    for _ in range(4):
        app.processEvents()
    assert ov._dual_hist.isVisible()
    bottom = ov._dual_hist.mapToGlobal(QPoint(0, ov._dual_hist.height())).y()
    assert ov._dual_split_hit(bottom) is True, "底边中心必须命中"
    assert ov._dual_split_hit(bottom - 4) is True, "底边上方必须命中"
    assert ov._dual_split_hit(bottom + 8) is True, "底边下方（body 侧）必须命中"
    assert ov._dual_split_hit(bottom + 40) is False, "远离命中带不得误判"
    assert ov._dual_split_hit(bottom - 60) is False, "历史区内部不得误判"
    ov.deleteLater()
check("panel: 分割命中用全局坐标（传播坐标不重映射）", t_dual_split_hit_global)

def t_overlay_dual_split_real_drag():
    """v2.15.2：**真实事件流**拖拽——QMouseEvent + sendEvent 按真实路径
    （分割点处实际接收的子控件 → 事件过滤器），验证事件路由而非仅分配
    逻辑：press 启动拖拽、move 跟手、release 收口落盘。"""
    from app.ui.caption_overlay import CaptionOverlay
    from PySide6.QtCore import Qt, QPoint, QPointF, QEvent
    from PySide6.QtGui import QMouseEvent
    ov = CaptionOverlay()
    ov.set_layout_mode("dual")
    ov.resize(620, 300)
    ov.move(80, 60)
    ov.show()
    app.processEvents()
    ov._dual_show_result("Hello world", "你好世界", True)
    ov.dual_push_history("Hello world", "你好世界")
    ov._dual_show_pending("Current growing")
    for _ in range(6):
        app.processEvents()
    # 700 总高：拖 90 不触"body 保底 46"的钳制上限
    ov.set_user_height(700)
    for _ in range(6):
        app.processEvents()
    assert ov._dual_hist.isVisible()
    h0 = ov._dual_hist.height()
    g = ov._dual_hist.mapToGlobal(
        QPoint(ov._dual_hist.width() // 2, ov._dual_hist.height() + 5))
    target = QApplication.widgetAt(g)

    def send_mouse(gtype, gpos, button=None):
        t = QApplication.widgetAt(gpos) or target
        local = t.mapFromGlobal(gpos)
        ev = QMouseEvent(QEvent.Type(gtype), QPointF(local), QPointF(gpos),
                         button or Qt.NoButton, button or Qt.NoButton,
                         Qt.NoModifier)
        QApplication.sendEvent(t, ev)

    send_mouse(QEvent.Type.MouseButtonPress, g, Qt.LeftButton)
    assert ov._dual_split_drag, "press 应经事件过滤器启动分割拖拽"
    for dy in (30, 60, 90):
        send_mouse(QEvent.Type.MouseMove, QPoint(g.x(), g.y() + dy), Qt.LeftButton)
    h_mid = ov._dual_hist.height()
    # 700 总高下 hist 上限 = 700-66-46=588（body 保底 46），若 h0 已近上限
    # 则拖动增量被钳制属正常，跟手性由 h_mid >= h0 保证
    expect_mid = min(h0 + 90, 700 - 66 - 46)
    assert h_mid >= h0 and h_mid <= expect_mid + 4, \
        f"拖动中历史区应跟手：{h0} -> {h_mid}（期望上限 {expect_mid}）"
    send_mouse(QEvent.Type.MouseButtonRelease, QPoint(g.x(), g.y() + 90), Qt.LeftButton)
    assert ov._dual_split_drag is False, "release 应收口"
    h1 = ov._dual_hist.height()
    assert abs(h1 - expect_mid) <= 16, f"最终高度应达钳制期望：{h1} vs {expect_mid}"
    ov.deleteLater()
check("panel: 分割线真实事件流拖拽（事件过滤器）", t_overlay_dual_split_real_drag)

def t_overlay_relayout_pending_release():
    """v2.11.0 关键修复锁：_consume_relayout 收敛后必须释放 _relayout_pending。

    原版（v2.4.3 起）pending 收敛后永久残留 True，后续所有 _schedule_relayout
    被守卫吞掉——列表模式有滚动条兜底、视觉无感（历史未暴露），dual 模式无
    滚动，第二句起高度永远停在首句值、长终版底部裁切（真机截图+探针轨迹
    实证：want=124 而 body=74）。本锁钉住"连续多句高度必须持续跟随"。"""
    ov = CaptionOverlay()
    ov.show()
    ov.set_layout_mode("dual")
    texts = [("The quick brown fox", "敏捷的棕色狐狸"),
             ("Our team shipped the new subtitle engine last week and the latency dropped a lot",
              "我们的团队上周发布了新的字幕引擎，延迟大幅下降"),
             ("Neural voice activity detection can tell human speech apart from background music",
              "神经语音活动检测能够把人声与背景音乐区分开来")]
    heights = []
    for src, tgt in texts:
        ov._dual_show_pending(src)
        for _ in range(5):
            app.processEvents()
        ov._dual_show_result(src, tgt, True)
        for _ in range(8):
            app.processEvents()
        heights.append(ov._dual_body.height())
        assert ov._relayout_pending is False, "收敛后 pending 必须释放，否则后续排期被吞"
    assert heights[-1] > heights[0], f"高度未跟随内容演进（停在首句值）：{heights}"
    assert len(set(heights)) >= 2, f"高度应随句长变化：{heights}"
    ov.deleteLater()
check("panel: relayout pending 释放（多句高度跟随）", t_overlay_relayout_pending_release)

def t_overlay_stream_partial():
    """v2.12.0：dual 流式原文——update_partial 整句刷新（每 ~0.9s 一拍）、
    终版收口以正式文本覆盖草稿、列表模式忽略。"""
    from app.ui.caption_overlay import CaptionOverlay
    ov = CaptionOverlay()
    ov.show()
    ov.set_layout_mode("dual")
    ov.show_pending("The quick")
    ov.update_partial("The quick brown fox jumps")
    assert ov._dual_src.text() == "The quick brown fox jumps"
    # 终版收口：正式文本覆盖草稿
    ov.show_pending_result("The quick brown fox jumps over.", "敏捷的狐狸跳了过去。", True)
    assert ov._dual_src.text() == "The quick brown fox jumps over."
    # 列表模式：update_partial 忽略（草稿不进历史区）
    ov.set_layout_mode("list")
    ov.show_pending("list mode piece")
    ov.update_partial("草稿不应进入列表")
    assert "草稿不应进入列表" not in [it["src_text"] for it in ov._rows]
    ov.deleteLater()
check("panel: dual 流式草稿 update_partial", t_overlay_stream_partial)

def t_main_partial_preview_alignment():
    """主窗流式接线：确认基线随片段生长、partial 剥重叠后追加、final 收口
    更新基线、非运行态忽略。"""
    w = MainWindow()
    w.show()
    w.running = True
    w.config.set("overlay_layout", "dual")
    w.apply_overlay_from_config()

    class _Tr(object):                     # _StubTr 定义在文件后段，此处内联
        _active_engine = "argos"

        def isRunning(self):
            return True

        def submit(self, text, lang, spec=False):
            return []

    stub = _Tr()
    w._active_translate = lambda: stub
    old_ll = w.config.get("low_latency_mode")
    w.config.set("low_latency_mode", True)
    try:
        # 片段到达：当前句显示文本随攒句生长（正式权威覆盖草稿）
        w._on_asr_text("The market opened higher today", "en", "1.0")
        assert w._dual_current == "The market opened higher today"
        # partial 到达：窗口文本剥掉与历史基线的重叠后合并进当前句
        w._on_partial_preview("The market opened higher today and stocks rallied")
        assert w.overlay._dual_src.text() == "The market opened higher today and stocks rallied"
        # 冲刷：剥离基线更新为整句
        w._flush_tgroup()
        assert w._dual_hist_base == "The market opened higher today"
        # 终版翻译到达：沉入历史区（原文+译文成对）+ 当前句数据清空
        w._on_translated("The market opened higher today", "今天高开", "argos", "en", "")
        assert w.overlay._dual_hist_rows == 1, "终版句应沉入历史区"
        assert w._dual_current == "", "终版沉历史后当前句数据清空"
        # 非运行态：预览草稿不得改写面板
        w.running = False
        w._on_partial_preview("stale garbage after stop")
        assert w.overlay._dual_src.text() == "The market opened higher today"
        w.stop_pipeline()
    finally:
        w.config.set("low_latency_mode", old_ll)
check("pipeline: dual 流式原文接线与对齐", t_main_partial_preview_alignment)

def t_main_draft_translation_flow():
    """v2.13.0：草稿也送推测翻译——译文区与原文同节奏实时生长；
    回复按"最新草稿全文"配对（不入 _spec_inflight 簿记）、一次性消费
    防迟到同文重复覆盖；冲刷作废在飞草稿防盖新句。"""
    w = MainWindow()
    w.show()
    w.running = True
    w.config.set("overlay_layout", "dual")
    w.config.set("low_latency_mode", True)
    w.apply_overlay_from_config()

    class _Tr(object):
        _active_engine = "argos"

        def __init__(self):
            self.sent = []

        def isRunning(self):
            return True

        def submit(self, text, lang, spec=False):
            self.sent.append((text, lang, bool(spec)))
            return []

    tr = _Tr()
    w._active_translate = lambda: tr
    try:
        # 草稿到达：原文区刷新 + 草稿送推测翻译（spec=True）
        w._dual_current = ""
        w._on_partial_preview("The market opened higher")
        assert w.overlay._dual_src.text() == "The market opened higher"
        assert w._dual_draft == "The market opened higher"
        assert tr.sent[-1] == ("The market opened higher", "auto", True), tr.sent
        # 草稿译文回复：译文区 spec 淡态更新（原文/配对/计数都不动）
        w._on_spec_translated("The market opened higher", "市场高开", "argos", "en", "")
        assert w.overlay._dual_tgt.text() == "市场高开"
        assert w.overlay._dual_tgt.property("spec") is True
        assert w._dual_draft is None, "一次性消费：防迟到同文重复覆盖"
        # 同文迟到回复：忽略（草稿已消费）
        w._on_spec_translated("The market opened higher", "陈旧草稿译文", "argos", "en", "")
        assert w.overlay._dual_tgt.text() == "市场高开"
        # 正式片段到达→攒句提交（spec 簿记通道），冲刷作废在飞草稿
        w._on_partial_preview("The market opened higher today")
        assert w._dual_draft == "The market opened higher today"
        w._on_asr_text("The market opened higher today", "en", "1.0")
        w._flush_tgroup()
        assert w._dual_draft is None, "冲刷必须作废在飞草稿"
        # 冲刷提交带 _tgroup_lang（正式片段锁定的语言），spec=False 终版
        assert tr.sent[-1] == ("The market opened higher today", "en", False), tr.sent[-1]
    finally:
        w.stop_pipeline()
check("pipeline: 草稿送推测翻译与迟到草稿作废", t_main_draft_translation_flow)

def t_overlay_layout_config_roundtrip():
    """overlay_layout 配置经 apply_overlay_from_config 恢复布局；
    面板 ⋯ 菜单切换经主窗回调落盘。"""
    w = MainWindow()
    w.show()
    w.config.set("overlay_layout", "dual")
    w.apply_overlay_from_config()
    assert w.overlay.is_dual(), "配置 dual 应在启动恢复时生效"
    w._on_panel_layout_changed("list")
    assert w.config.get("overlay_layout") == "list"
    assert not w.overlay.is_dual(), "菜单切换应即时生效并落盘"
    w._quitting = True
    w._teardown()
check("panel: 布局配置恢复与菜单切换落盘", t_overlay_layout_config_roundtrip)

def t_panel_unread_badge():
    # v2.4.3（E）：非跟随时新句计数 +1、按钮变红显示"N"；回底两种路径（点按钮/
    # 滚到底）都归零复原。offscreen 字体度量退化（30 行仅 ~2px 溢出、maximum<4
    # 时任何值都判"在底部"），第二轮直接置 _follow 构造非跟随态；_on_scroll 的
    # 非跟随/回底分支由第一轮与收尾断言覆盖
    ov = CaptionOverlay()
    ov.resize(420, 140)
    ov.show(); app.processEvents()
    for i in range(30):
        ov.show_caption(f"source sentence {i} of the live stream", f"译文第 {i} 句", True)
    app.processEvents()
    assert ov._unread == 0 and ov._jump_btn.text() == "↓ 最新", "跟随时不计数"
    ov._on_scroll(-98)                       # 真实处理器路径：人为滚离底部
    assert not ov._follow and ov._jump_btn.isVisible(), "暂停跟随后应浮出 ↓最新"
    ov.show_caption("new one", "新的一句", True)
    assert ov._unread == 1 and "1" in ov._jump_btn.text(), "新句计数 +1"
    ov.show_pending("another")
    ov.show_pending_result("another", "又一句", True)
    assert ov._unread == 2, "占位补齐也计一次（只数完成句）"
    ov._scroll_bottom()
    assert ov._unread == 0 and ov._jump_btn.text() == "↓ 最新", "回底归零复原"
    assert not ov._jump_btn.isVisible()
    ov._follow = False                        # 第二轮：直设非跟随（绕开退化滚动条；
    ov._unread = 3                            # 其间若有 relayout 会经 valueChanged 判回底部）
    ov._sync_unread_btn()
    assert "3" in ov._jump_btn.text(), "计数徽标文案"
    ov._on_scroll(ov._scroll.verticalScrollBar().maximum())   # 滚到底同样归零
    assert ov._unread == 0 and ov._follow and ov._jump_btn.text() == "↓ 最新"
    ov.deleteLater()
check("panel: ↓最新未读计数（E）", t_panel_unread_badge)

def t_panel_height_settles():
    # v2.4.3：高度贴内容必须真实成立——插入当拍 QLabel 的 sizeHint 未定型
    # （实机插桩：新行读出 8px，事件循环后才是 139px），v2.4.0~v2.4.2 面板
    # 高度一直滞后一拍甚至停在空闲高度。延迟复排（排期即消耗）后，scroll
    # 固定高必须等于 want = body.sizeHint+8（限幅内）
    from PySide6.QtGui import QGuiApplication
    ov = CaptionOverlay()
    ov.resize(560, 150)
    ov.show(); app.processEvents()
    for i in range(5):
        ov.show_caption(f"sentence {i} with a bit of length here", f"译文第 {i} 句", True)
    h = -1
    for _ in range(12):            # 放行收敛链（每拍一个零时器复排，高度稳定即止）
        app.processEvents()
        if ov._scroll.height() == h:
            break
        h = ov._scroll.height()
    cap = int((QGuiApplication.primaryScreen().availableGeometry().height() or 800) * 0.55)
    want = min(max(ov._body.sizeHint().height() + 8, 46), cap)
    assert ov._scroll.height() == want, \
        f"高度滞后未复排：scroll={ov._scroll.height()} want={want}"
    ov.deleteLater()
check("panel: 高度贴内容真实成立（延迟复排）", t_panel_height_settles)

def t_wizard_scheduled_vs_shown():
    # v2.4.4（BUG-1）：v2.4.1 在排期时就置"已显示"标志，回调守卫查同一标志
    # → 向导被自己的守卫拦截，首启永不弹出（隔离新配置实测抓到）。回归锁：
    # 构造排期后 scheduled=True 而 shown=False；可见实例回调真正弹窗才置
    # shown 且只弹一次；不可见实例有限顺延、不置位、不弹
    from app.ui import first_run as fr
    from app.ui.main_window import MainWindow
    made = []
    class _FakeWiz:
        def __init__(self, parent): made.append(1)
        def exec(self): return 0
    orig = fr.FirstRunWizard
    fr.FirstRunWizard = _FakeWiz
    try:
        MainWindow._wizard_shown = False
        MainWindow._wizard_scheduled = False
        MainWindow._wizard_defers = 0
        cfg = Config()
        orig_done = bool(cfg.get("wizard_done"))
        cfg.set("wizard_done", False)
        cfg.set("overlay_enabled", False)
        w = MainWindow()
        assert MainWindow._wizard_scheduled, "构造后应已排期"
        assert not MainWindow._wizard_shown, "排期不得置已显标志（BUG-1 回归点）"
        w.show(); app.processEvents()
        w._show_first_run_wizard()
        assert made == [1], "可见实例回调应弹一次向导"
        assert MainWindow._wizard_shown
        w._show_first_run_wizard()
        assert made == [1], "已显守卫：不得重复弹"
        MainWindow._wizard_shown = False
        MainWindow._wizard_defers = 0
        w.hide(); app.processEvents()
        w._show_first_run_wizard()
        assert made == [1] and not MainWindow._wizard_shown, "不可见不得弹"
        assert MainWindow._wizard_defers == 1, "不可见时应顺延一次"
        w.deleteLater()
        w._quitting = True
        w._teardown()
    finally:
        fr.FirstRunWizard = orig
        MainWindow._wizard_shown = False
        MainWindow._wizard_scheduled = False
        MainWindow._wizard_defers = 0
        # itest_home 是测试专用 home：无条件回到"向导已完成"——残留
        # wizard_done=False 会让后续任何 MainWindow 构造后排向导，400ms 后
        # 在别的测试的事件循环里真 exec 阻塞整个套件（本测试首跑踩过）
        Config().set("wizard_done", True)
check("wizard: 排期/已显标志分离+有限顺延（BUG-1）", t_wizard_scheduled_vs_shown)

def t_quick_gpu_hint_actual_device():
    # v2.4.4（BUG-2）：GPU 静默回落 CPU 后，速览卡不得按配置谎报"（GPU）"
    from types import SimpleNamespace
    w = MainWindow()
    try:
        cfg_dev = str(w.config.get("asr_device")) == "cuda"
        w.asr_thread = None
        assert w._quick_gpu_hint() == cfg_dev, "无线程时回退配置"
        w.asr_thread = SimpleNamespace(_device_used="cpu")
        assert not w._quick_gpu_hint(), "实际 CPU 不得显示 GPU"
        w.asr_thread = SimpleNamespace(_device_used="cuda")
        assert w._quick_gpu_hint(), "实际 GPU 显示 GPU"
        w.asr_thread = SimpleNamespace()
        assert w._quick_gpu_hint() == cfg_dev, "线程早期无设备值：回退配置"
    finally:
        w.asr_thread = None
        w._quitting = True
        w._teardown()
check("quickpanel: 速览卡按实际加载设备显示（BUG-2）", t_quick_gpu_hint_actual_device)

def t_low_input_cleared_on_caption():
    # v2.4.4（BUG-3）：字幕成功上屏必须撤"信号过弱"告警（此前挂到会话结束，
    # 一边出字幕一边警示"字幕可能无法识别"，主窗横幅与面板状态双端矛盾）
    w = MainWindow()
    try:
        w.running = True
        w._set_engine_status("识别: 就绪，正在聆听…")
        w._on_low_input(True)
        assert "信号过弱" in w.engine_status_label.text()
        assert w.overlay.status_lbl.text() == "信号弱"
        w._on_asr_text("hello", "en", "1.0", -1.0)
        assert not w._low_input_warn, "字幕上屏应撤告警标志"
        assert "信号过弱" not in w.engine_status_label.text()
        assert w.overlay.status_lbl.text() != "信号弱"
        # 撤回后的静音期（quiet=True 再触发）不得重挂——会话已证明可识别
        w._on_low_input(True)
        assert not w._low_input_warn, "证明过可识别后静音不再挂告警"
        assert "信号过弱" not in w.engine_status_label.text()
    finally:
        w.running = False
        w._quitting = True
        w._teardown()
check("status: 字幕上屏撤低电平告警（BUG-3）", t_low_input_cleared_on_caption)

def t_card_labels_defer_context_menu():
    # v2.4.4（BUG-5）：QLabel 不得拦截卡片右键——此前英文 Copy/Select All
    # 菜单挡死 P14 纠错入口，且集成测试直接调内部构建从未暴露
    from PySide6.QtCore import Qt
    card = CaptionCard("src text", on_menu=lambda c: None)
    try:
        assert card.source_label.contextMenuPolicy() == Qt.ContextMenuPolicy.NoContextMenu
        assert card.target_label.contextMenuPolicy() == Qt.ContextMenuPolicy.NoContextMenu
    finally:
        card.deleteLater()
check("card: 文本标签右键交还父级（BUG-5）", t_card_labels_defer_context_menu)

def t_export_srt_default():
    # v2.4.4（BUG-6）："导出 SRT…"入口的默认文件名与首选过滤器必须是 SRT
    # （此前与通用导出共用，照文案直接保存得到 txt）。注意：PySide6 类型类体
    # 不可靠 monkeypatch，改为替换 main_window 模块命名空间的 QFileDialog 绑定
    import app.ui.main_window as mw
    w = MainWindow()
    try:
        w.running = True
        w._on_translated("a", "一", "google", "en", None)
        got = {}
        class _FakeFD:
            @staticmethod
            def getSaveFileName(parent, title, d, f):
                got["dir"], got["filter"] = d, f
                return ("", "")
        orig_cls = mw.QFileDialog
        mw.QFileDialog = _FakeFD
        try:
            w._export_srt()
            assert got["dir"].endswith(".srt"), f"SRT 入口默认名错误：{got['dir']}"
            assert got["filter"].startswith("SRT"), got["filter"]
            w._export_captions()
            assert got["dir"].endswith(".txt"), "通用导出仍默认 txt"
            assert got["filter"].startswith("文本文件")
        finally:
            mw.QFileDialog = orig_cls
    finally:
        w._quitting = True
        w._teardown()
check("export: SRT 入口默认 SRT（BUG-6）", t_export_srt_default)

def t_hint_guide_resets_on_caption():
    # v2.4.4（BUG-7）：手势引导只陪首段字幕之前——字幕到来即复位，
    # 此后清空回退单行占位（此前引导每次清空都重现）
    ov = CaptionOverlay()
    try:
        ov.show(); app.processEvents()
        ov.show_first_hint()
        assert "首次使用小抄" in ov._hint.text()
        ov.show_caption("a", "b", True)
        ov.clear_caption()
        assert ov._hint.text() == ov.HINT_IDLE, "清空后应回退单行占位"
    finally:
        ov.deleteLater()
check("panel: 引导首句后复位（BUG-7）", t_hint_guide_resets_on_caption)

def t_stop_keeps_cards():
    # v2.4.4（BUG-9）：停止翻译后仍有字幕卡时不得切回速览卡（会话记录要能回看）
    import inspect
    w = MainWindow()
    try:
        w.running = True
        w._on_translated("keep", "保留", "google", "en", None)
        assert w._has_cards()
        src_txt = inspect.getsource(type(w).stop_pipeline)
        assert "_has_cards" in src_txt, "stop_pipeline 应按有无卡片决定切页"
    finally:
        w._quitting = True
        w._teardown()
check("ui: 停止保留字幕列表（BUG-9）", t_stop_keeps_cards)

def t_rerun_wizard_preserves_staged_choice():
    # v2.4.4（BUG-4）：有未保存暂存时运行向导必须先问；取消则不进向导
    # （此前 load_from_config 无条件回填，暂存被静默丢弃）。
    # patch 走 settings_dialog 模块命名空间（PySide6 类型类体不可 monkeypatch）
    import app.ui.settings_dialog as sd
    from app.ui import first_run as fr
    from app.ui.settings_dialog import SettingsDialog
    w = MainWindow()
    try:
        dlg = SettingsDialog(w)
        dlg._staged["auto_start"] = True
        made = []
        class _FakeWiz:
            def __init__(self, p): made.append(1)
            def exec(self): return 0
        class _FakeQMB:
            Cancel = 0x00400000
            Yes = 0x00004000
            No = 0x00010000
            @staticmethod
            def question(*a, **k):
                return _FakeQMB.Cancel
        orig_w = fr.FirstRunWizard
        orig_m = sd.QMessageBox
        fr.FirstRunWizard = _FakeWiz
        sd.QMessageBox = _FakeQMB
        try:
            dlg._rerun_wizard()
            assert made == [], "用户取消时不得运行向导"
        finally:
            fr.FirstRunWizard = orig_w
            sd.QMessageBox = orig_m
        dlg.deleteLater()
    finally:
        w._quitting = True
        w._teardown()
check("settings: 向导前暂存问询（BUG-4）", t_rerun_wizard_preserves_staged_choice)

def t_panel_font_size_actually_applies():
    # v2.5.0（F1 回归锁）：v2.4.0 起正文从未挂 font-size 规则，字号调节完全
    # 失效（截图矩阵 16/30/40 三档像素相同才暴露）。锁：QSS 字号必须随
    # apply_style 变化并回读到译文标签字体
    ov = CaptionOverlay()
    try:
        ov.show(); app.processEvents()
        ov.apply_style(22, "#ffffff", "#1c1f26", 92)
        ov.show_caption("s", "t", True)
        app.processEvents()
        f22 = ov._rows[-1]["tgt"].font().pixelSize()
        ov.apply_style(40, "#ffffff", "#1c1f26", 92)
        app.processEvents()
        f40 = ov._rows[-1]["tgt"].font().pixelSize()
        assert f22 == 22 and f40 == 40, f"字号未生效：22→{f22}, 40→{f40}"
        src_fs = ov._rows[-1]["src"].font().pixelSize()
        assert src_fs == max(11, int(40 * 0.72)), f"原文层次字号未生效：{src_fs}"
    finally:
        ov.deleteLater()
check("panel: 字号真实生效+原文层次（F1）", t_panel_font_size_actually_applies)

def t_panel_height_converges_tgt_only():
    # v2.5.0（F2 回归锁）：只译文模式 5 行不得塌陷（此前收敛判定比较被 min
    # 钳住的 scroll 高度，链提前断，5 行塌成 2.5 行）
    from PySide6.QtGui import QGuiApplication
    ov = CaptionOverlay()
    try:
        ov.resize(560, 150)
        ov.show(); app.processEvents()
        for i in range(5):
            ov.show_caption(f"sentence {i} with a bit of length here", f"译文第 {i} 句", False)
        h = -1
        for _ in range(15):
            app.processEvents()
            if ov._scroll.height() == h:
                break
            h = ov._scroll.height()
        cap = int((QGuiApplication.primaryScreen().availableGeometry().height() or 800) * 0.55)
        want = min(max(ov._body.sizeHint().height() + 8, 46), cap)
        assert ov._scroll.height() == want, \
            f"只译文高度塌陷：scroll={ov._scroll.height()} want={want}"
    finally:
        ov.deleteLater()
check("panel: 只译文高度收敛（F2）", t_panel_height_converges_tgt_only)

def t_panel_status_label_width_capped():
    # v2.5.0（F3 回归锁）：窄面板状态行限宽，长错误文案不得溢出压到按钮。
    # （未 show 的 widget 收不到 Python resizeEvent，限宽挂在 set_status/
    # _relayout 必经路径，测试走 set_status 断言）
    ov = CaptionOverlay()
    try:
        ov.resize(360, 100)
        ov.set_status("翻译失败 · 检查网络或切换引擎", is_error=True)
        assert ov.status_lbl.maximumWidth() == 120, ov.status_lbl.maximumWidth()
        assert ov.status_lbl.width() <= 122, ov.status_lbl.width()
        ov.resize(700, 100)
        ov.set_status("运行中", is_error=False)
        assert ov.status_lbl.maximumWidth() == 460, ov.status_lbl.maximumWidth()
    finally:
        ov.deleteLater()
check("panel: 状态行限宽防重叠（F3）", t_panel_status_label_width_capped)

def t_panel_mini_bar():
    # v2.5.0：精简条——收起显示最新一句（原文+译文），新句跟随，
    # 原地点击展开完整历史，真拖动不触发展开
    from PySide6.QtCore import QPoint, QPointF, Qt
    LB = Qt.MouseButton.LeftButton
    events = {"collapsed": []}
    ov = CaptionOverlay(on_collapsed=lambda on: events["collapsed"].append(on))
    try:
        ov.show(); app.processEvents()
        ov.show_caption("s1", "t1", True)
        ov.show_caption("s2", "t2", True)
        ov.set_collapsed(True)
        app.processEvents()
        assert ov._mini.isVisible() and not ov._scroll.isVisible()
        assert ov._mini_src.text() == "s2" and ov._mini_tgt.text() == "t2"
        ov.show_caption("s3", "t3", True)
        assert ov._mini_tgt.text() == "t3", "精简条应跟随最新句"
        ov.clear_caption()
        assert "展开" in ov._mini_tgt.text(), "空状态应有展开引导"
        ov.show_caption("s4", "t4", True)

        class _Ev:
            def __init__(s, x, y, gx=0, gy=0):
                s._p = QPoint(x, y)
                s._g = QPointF(gx, gy)
                s._b = LB
            def button(s): return s._b
            def buttons(s): return s._b
            def position(s): return s._p
            def globalPosition(s): return s._g
            def accept(s): pass
        bar_bottom = ov._bar.geometry().bottom()
        y = bar_bottom + 20
        ov.mousePressEvent(_Ev(200, y, 200, y))
        ov.mouseReleaseEvent(_Ev(200, y, 200, y))     # 原地点击 → 展开
        assert not ov._collapsed and events["collapsed"][-1] is False
        assert ov._scroll.isVisible() and not ov._mini.isVisible()
        ov.set_collapsed(True)
        ov.mousePressEvent(_Ev(200, y, 200, y))
        ov.mouseMoveEvent(_Ev(260, y + 8, 260, y + 8))   # 真拖动
        ov.mouseReleaseEvent(_Ev(260, y + 8, 260, y + 8))
        assert ov._collapsed, "真拖动不得触发展开"
    finally:
        ov.deleteLater()
check("panel: 精简条模式（v2.5.0）", t_panel_mini_bar)

def t_panel_wheel_shortcuts():
    # v2.5.0：工具条 Ctrl+滚轮=字号 ±1、Ctrl+Shift+滚轮=透明度 ∓5；
    # 无修饰键不触发（正文历史滚动不受影响）
    from PySide6.QtCore import QPoint, Qt

    class _WEv:
        def __init__(s, mods, up):
            s._m, s._d = mods, (120 if up else -120)
            s.accepted = False
            s.ignored = False
        def position(s): return QPoint(20, 10)
        def angleDelta(s): return QPoint(0, s._d)
        def modifiers(s): return s._m
        def accept(s): s.accepted = True
        def ignore(s): s.ignored = True

    fonts, ops = [], []
    ov = CaptionOverlay(on_font_size=lambda px: (fonts.append(px),
                                                 setattr(ov, "_font_size", int(px))),
                        on_opacity=lambda v: (ops.append(v),
                                              setattr(ov, "_bg_alpha", int(v * 2.55))))
    try:
        ov.show(); app.processEvents()
        C = Qt.KeyboardModifier.ControlModifier
        CS = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
        ov.wheelEvent(_WEv(C, True))
        assert fonts == [23], fonts
        ov.wheelEvent(_WEv(C, False))
        assert fonts == [23, 22], fonts
        ov.wheelEvent(_WEv(CS, True))
        assert ops == [97], ops                      # 92 + 5
        ov.wheelEvent(_WEv(CS, True))
        assert ops == [97, 100], ops                 # 钳 100
        ov.wheelEvent(_WEv(CS, False))
        assert ops == [97, 100, 95], ops
        wev = _WEv(Qt.KeyboardModifier.NoModifier, True)
        ov.wheelEvent(wev)
        assert wev.ignored and not wev.accepted, "无修饰键滚轮不得抢占"
        assert fonts == [23, 22] and ops == [97, 100, 95]
    finally:
        ov.deleteLater()
check("panel: 工具条滚轮快捷调节（v2.5.0）", t_panel_wheel_shortcuts)

def t_panel_magnet_snap():
    # v2.5.0：拖动松手近屏幕边缘自动磁吸（松手时面板顶缘距可用区顶 <24px → 贴顶）
    from PySide6.QtCore import QPoint, QPointF, Qt
    LB = Qt.MouseButton.LeftButton

    class _Ev:
        def __init__(s, x, y, gx, gy):
            s._p, s._g = QPoint(x, y), QPointF(gx, gy)
        def button(s): return LB
        def buttons(s): return LB
        def position(s): return s._p
        def globalPosition(s): return s._g
        def accept(s): pass

    moved = []
    ov = CaptionOverlay(on_moved=lambda x, y: moved.append((x, y)))
    try:
        ov.show(); app.processEvents()
        g = ov.screen().availableGeometry()
        # 中部松手（面板顶缘距可用区顶 285px）不吸附
        ov.move(g.left() + 300, g.top() + 285)
        app.processEvents()
        ov.mousePressEvent(_Ev(100, 20, g.left() + 400, g.top() + 305))
        ov.mouseMoveEvent(_Ev(100, 20, g.left() + 400, g.top() + 305))
        ov.mouseReleaseEvent(_Ev(100, 20, g.left() + 400, g.top() + 305))
        assert ov.y() == g.top() + 285, f"中部松手不应吸附：{ov.y()}"
        # 近顶松手（面板顶缘 y=5 <24px）磁吸到顶
        ov.move(g.left() + 300, g.top() + 5)
        app.processEvents()
        ov.mousePressEvent(_Ev(100, 20, g.left() + 400, g.top() + 25))
        ov.mouseMoveEvent(_Ev(100, 20, g.left() + 400, g.top() + 26))
        ov.mouseReleaseEvent(_Ev(100, 20, g.left() + 400, g.top() + 26))
        assert ov.y() == g.top(), f"近顶松手应磁吸到顶：{ov.y()}"
        assert moved, "吸附后应落盘位置"
    finally:
        ov.deleteLater()
check("panel: 拖动磁吸贴边（v2.5.0）", t_panel_magnet_snap)

def t_panel_opacity_menu_preset():
    # v2.5.0：⋯ 菜单透明度常用档，勾选态与当前值同步
    ops = []
    ov = CaptionOverlay(on_opacity=ops.append)
    try:
        menu = ov._build_menu()
        op_menu = None
        for a in menu.actions():
            if a.text() == "背景透明度":
                op_menu = a.menu()
        assert op_menu is not None, "透明度子菜单缺失"
        vals = [a.text() for a in op_menu.actions()]
        assert vals == ["60%", "75%", "85%", "92%", "100%"], vals
        sel = [a for a in op_menu.actions() if a.text() == "75%"][0]
        sel.trigger()
        assert ops == [75], ops
        menu.deleteLater()
    finally:
        ov.deleteLater()
check("panel: 菜单透明度档（v2.5.0）", t_panel_opacity_menu_preset)

def t_panel_font_menu_exclusive_checks():
    # v2.5.3（用户实测四档全勾）：字号菜单勾选必须互斥同步——换档/滚轮后旧勾
    # 不得残留（菜单非互斥 + 只在构造时 setChecked 的累积效应）
    ov = CaptionOverlay()
    try:
        ov.show(); app.processEvents()
        menu = ov._font_btn.menu()
        def checks():
            return {a.text(): a.isChecked() for a in menu.actions()}
        ov.apply_style(22, "#ffffff", "#1c1f26", 92)
        c22 = checks()
        assert c22["中号（22px）"] and sum(c22.values()) == 1, c22
        ov.apply_style(40, "#ffffff", "#1c1f26", 92)
        c40 = checks()
        assert c40["特大（40px）"] and sum(c40.values()) == 1, c40
        ov.apply_style(16, "#ffffff", "#1c1f26", 92)
        c16 = checks()
        assert c16["小号（16px）"] and sum(c16.values()) == 1, c16
    finally:
        ov.deleteLater()
check("panel: 字号菜单勾选互斥同步", t_panel_font_menu_exclusive_checks)

def t_panel_vertical_resize():
    # v2.5.3（用户裁决回归）：面板支持底缘拉高——拖后锁定手动高度且
    # _relayout 不再自动覆盖；恢复自动后回内容高度
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QGuiApplication
    LB = Qt.MouseButton.LeftButton

    class _Ev:
        def __init__(s, x, y, gx, gy):
            s._p, s._g = QPoint(x, y), QPointF(gx, gy)
        def button(s): return LB
        def buttons(s): return LB
        def position(s): return s._p
        def globalPosition(s): return s._g
        def accept(s): pass

    heights = []
    ov = CaptionOverlay(on_height_changed=lambda h: heights.append(int(h or 0)))
    try:
        ov.resize(560, 150)
        ov.show(); app.processEvents()
        ov.show_caption("s", "一段译文内容", True)
        h = -1
        for _ in range(15):
            app.processEvents()
            if ov._scroll.height() == h:
                break
            h = ov._scroll.height()
        auto_total = ov.height()
        # 拖底缘 +120px
        bottom = ov.height()
        gx, gy = 300 + 560, 200 + bottom   # 全局（起点在底缘上）
        ov.mousePressEvent(_Ev(280, bottom - 3, gx, gy))
        assert ov._v_resizing, "底缘按下应进入拉高模式"
        ov.mouseMoveEvent(_Ev(280, bottom - 3, gx, gy + 120))
        ov.mouseReleaseEvent(_Ev(280, bottom - 3, gx, gy + 120))
        app.processEvents()
        assert ov.height() >= auto_total + 100, f"拉高未生效：{ov.height()} vs {auto_total}"
        assert ov._user_height == ov.height(), "手动高度应锁定"
        assert heights and heights[-1] == ov.height()
        # 加新句：手动高度不被自动覆盖
        ov.show_caption("s2", "第二句译文内容", True)
        h = -1
        for _ in range(12):
            app.processEvents()
            if ov._scroll.height() == h:
                break
            h = ov._scroll.height()
        assert ov._user_height and ov._scroll.height() == max(46, ov._user_height - 54)
        # 菜单"恢复自动高度"
        menu = ov._build_menu()
        acts = [a for a in menu.actions() if a.text() == "恢复自动高度"]
        assert acts and acts[0].isEnabled()
        acts[0].trigger()
        app.processEvents()
        assert ov._user_height is None, "恢复自动后应清除手动高度"
        assert heights[-1] == 0
        menu.deleteLater()
    finally:
        ov.deleteLater()
check("panel: 底缘拉高+手动高度契约（v2.5.3）", t_panel_vertical_resize)

def t_settings_overlay_title_no_pin_word():
    # v2.5.3（用户指出双"置顶"逻辑冲突）：设置页开关标题不得再含"置顶"——
    # 置顶语义专属面板 📌 按钮（控制是否压过其他窗口）
    from app.ui.settings_dialog import _STD_ROWS
    row = [r for r in _STD_ROWS if r["key"] == "overlay_enabled"][0]
    assert row["title"] == "启用字幕面板", row["title"]
    assert "置顶" not in row["title"]
check("settings: 面板开关无双置顶字样（v2.5.3）", t_settings_overlay_title_no_pin_word)

def t_overlay_menu_correction():
    # v2.3.21（P29）：悬浮条右键菜单的纠错入口——无内容置灰；派发走
    # on_correct 回调（与主窗卡片纠错同源）
    got = []
    ov = CaptionOverlay(on_correct=lambda k, w0: got.append((k, w0)))
    menu = ov._build_menu()
    acts = ov._menu_acts
    texts = [a.text() for a in menu.actions() if a.text()]
    assert "纠正最近识别…" in texts and "纠正最近译文…" in texts, texts
    assert not acts["fix_asr"].isEnabled() and not acts["fix_tr"].isEnabled(), \
        "无内容时纠错项必须置灰"
    ov.show_caption("HELLO WORLD SOURCE", "你好世界", show_source=True)
    menu2 = ov._build_menu()
    acts2 = ov._menu_acts
    assert acts2["fix_asr"].isEnabled() and acts2["fix_tr"].isEnabled()
    ov._menu_dispatch(acts2["fix_asr"])
    ov._menu_dispatch(acts2["fix_tr"])
    assert got == [("mishear_map", "HELLO WORLD SOURCE"),
                   ("translate_fix_map", "你好世界")], got
    menu.deleteLater()
    menu2.deleteLater()
    ov.deleteLater()
check("overlay: 悬浮条纠错菜单入口（P29）", t_overlay_menu_correction)

def t_prewarm_skip_uncached():
    # v2.3.5（P5）：预热的安全边界——模型未完整下载时必须直接返回，
    # 绝不在后台悄悄触发 1.6GB 下载
    from app.asr.engine import PrewarmWorker, AsrThread
    import app.asr.engine as eng
    assert not AsrThread.model_cached("large-v3-turbo"), "itest_home 不应有该模型"
    wkr = PrewarmWorker("large-v3-turbo", "cuda")
    wkr.start()
    wkr.wait(3000)
    assert not wkr.isRunning(), "未缓存模型的预热必须立即结束"
    for key in list(eng._MODEL_CACHE):
        assert key[0] != "large-v3-turbo", f"预热不该把未下载模型塞进池: {key}"
check("asr: 预热不触发下载（未缓存即跳过）", t_prewarm_skip_uncached)

def t_no_segment_hint_level_guard():
    # v2.3.6（P8）：有电平活动（视频还在缓冲）时"无声音"指引必须顺延，
    # 静默超时后才真正提示——CBS 实测轮抓到指引抢跑冤枉用户
    import time as _t
    w = MainWindow()
    w.show()
    w.running = True
    w._asr_ready = True
    w.session_count = 0
    w._no_segment_hint_done = False   # 真实流程由 start_pipeline 置 False
    w._last_level_sound = _t.monotonic()
    w._no_segment_hint()
    assert not getattr(w, "_no_segment_hint_done", False), "有电平活动时应顺延"
    w._last_level_sound = 0.0
    w._no_segment_hint()
    assert getattr(w, "_no_segment_hint_done", False), "静默超时后应提示"
    assert "无识别结果" in w.engine_status_label.text()
    w.running = False
    w._quitting = True
    w._teardown()
check("pipeline: 无声音指引电平守卫顺延（P8）", t_no_segment_hint_level_guard)

def t_translate_fix_staging():
    # v2.3.6（P7）：译文修正词典的解析/暂存/回显链路
    from app.ui.settings_dialog import SettingsDialog
    w = MainWindow()
    dlg = SettingsDialog(w)
    dlg.load_from_config()
    dlg._loading = False
    dlg.tfix_edit.setPlainText("加快人工智能的发展速度=控制人工智能的发展节奏\n"
                               "空行忽略\n=无效\n无效=\n多行=正确=忽略后续")
    dlg._stage_translate_fix()
    assert dlg._staged.get("translate_fix_map") == {
        "加快人工智能的发展速度": "控制人工智能的发展节奏",
        "多行": "正确=忽略后续"}, dlg._staged
    dlg.deleteLater()
    w._quitting = True
    w._teardown()
check("settings: 译文修正词典解析与暂存（P7）", t_translate_fix_staging)

def t_low_latency_group():
    # v2.3.6（P9）：低延迟"上屏碎、翻译整句"——碎片不独立送翻，
    # 整句译文落末卡，前碎片卡只留原文（走真实配对链路，不伪造键）
    from app.ui.main_window import CaptionCard
    w = MainWindow()
    w.show()
    w.running = True
    w.config.set("low_latency_mode", True)
    w._last_asr_timing = (0.0, 2.0)
    submitted = []

    class FakeT:
        def submit(self, text, detected):
            submitted.append(text)

        def isRunning(self):
            return True   # v2.7.4（B-1）：死队列守卫要求可查询存活
    real_tt = w.translate_thread
    w.translate_thread = FakeT()
    w._on_asr_text("and authorities to understand", "en", "2.0")
    assert submitted == [], "小写开头延续片不应送出"
    w._on_asr_text("what happened.", "en", "2.0")
    assert submitted == [], "whisper 自补句号也不触发（v2.3.8 修正核心）"
    w._flush_tgroup()   # 模拟静默兜底
    assert submitted == ["and authorities to understand what happened."], submitted
    # v2.3.14：7 秒硬兜底改为 _tgroup_deadline（定时器降频为 1s 轮询）——
    # 兜底窗口仍必须 > 6s 分片周期（v2.3.9 教训），断言跟着搬到家法上
    import time as _time
    dl = getattr(w, "_tgroup_deadline", None)
    assert dl is not None and dl - _time.monotonic() > 6.0, \
        f"硬兜底 deadline 不大于分片周期，攒句会失效"
    w._on_translated("and authorities to understand what happened.",
                     "有关部门正在了解发生了什么。", "google", "en", "")
    assert not getattr(w, "_pending", []), "组内占位卡应全部消化"
    cs = [w.scroll_layout.itemAt(i).widget() for i in range(w.scroll_layout.count())]
    cs = [c for c in cs if isinstance(c, CaptionCard)]
    assert len(cs) == 2, len(cs)
    assert cs[0].target_label.text() == "", cs[0].target_label.text()
    assert cs[1].target_label.text() == "有关部门正在了解发生了什么。"
    # 大写开头=新句：先把已攒的送出，自己开新组
    w._on_asr_text("the FAA confirmed it", "en", "1.0")
    w._on_asr_text("The NTSB joined.", "en", "1.0")
    assert submitted[-1] == "the FAA confirmed it", submitted
    w._flush_tgroup()
    assert submitted[-1] == "The NTSB joined.", submitted
    # 5 片硬上限
    for i in range(5):
        w._on_asr_text(f"seg{i}", "en", "1.0")
    assert submitted[-1] == "seg0 seg1 seg2 seg3 seg4", submitted
    w.config.set("low_latency_mode", False)
    w._on_asr_text("direct", "en", "1.0")
    assert submitted[-1] == "direct"                     # 默认模式即时送
    w.translate_thread = real_tt
    w.running = False
    w._quitting = True
    w._teardown()
check("asr: 低延迟攒句两轨制（P9）", t_low_latency_group)

def t_tgroup_silent_early_flush():
    # v2.3.14（P16+收尾守卫）：静默立送需"音频停了 + 末片像说完了"双证据；
    # 未完片（省略号）即使静默也 held，由 7 秒硬兜底最终送出
    import time as _t
    w = MainWindow()
    w.show()
    w.running = True
    w.config.set("low_latency_mode", True)
    w._last_asr_timing = (0.0, 2.0)
    submitted = []

    class FakeT:
        def submit(self, text, detected):
            submitted.append(text)

        def isRunning(self):
            return True   # v2.7.4（B-1）：死队列守卫要求可查询存活
    real_tt = w.translate_thread
    w.translate_thread = FakeT()
    # ① 收尾完整但音频活跃 → 不送
    w._on_asr_text("The market closed higher today.", "en", "2.0")
    w._last_level_sound = _t.monotonic()
    w._tgroup_tick()
    assert submitted == [], "音频仍活跃时不应送出"
    # ② 收尾完整 + 静默 3.5s → 立送（P16 提速主场景）
    w._last_level_sound = _t.monotonic() - 4.0
    w._tgroup_tick()
    assert submitted == ["The market closed higher today."], submitted
    # ③ 省略号未完 + 深度静默 → 仍不送（R9 实况腰斩案例回归锁）
    w._on_asr_text("Iran's state media claims that stockpiles of uranium...", "en", "2.0")
    w._last_level_sound = _t.monotonic() - 5.0
    w._tgroup_tick()
    assert submitted == ["The market closed higher today."], "未完片不得因静默提前冲"
    # ④ 小写延续片到达 → held 并入同组（不即时送）
    w._on_asr_text("were transferred away from Iranian nuclear sites.", "en", "2.0")
    assert submitted == ["The market closed higher today."], "延续片到达即送是倒退"
    # v2.3.18（P23）回归锁：组寿命绝对上限 10s——续片不得给 deadline 续命
    assert w._tgroup_deadline - w._tgroup_start <= 10.05, \
        "deadline 仍可被逐片重置，头号句等待不封顶"
    w._tgroup_start = _t.monotonic() - 12.0     # 伪造"组已活 12 秒"
    w._on_asr_text("and a third late continuation", "en", "2.0")
    w._last_level_sound = _t.monotonic()        # 音频活跃，静默通道排除
    w._tgroup_tick()                            # 只能由绝对上限放行
    assert submitted[-1] == ("Iran's state media claims that stockpiles of uranium... "
                             "were transferred away from Iranian nuclear sites. "
                             "and a third late continuation"), submitted[-1]
    # ⑤ 7 秒硬兜底：无标点未完片最终仍会送出，不会永远卡死
    w._on_asr_text("an open ending without punctuation", "en", "2.0")
    w._tgroup_deadline = _t.monotonic() - 1.0
    w._last_level_sound = _t.monotonic()
    w._tgroup_tick()
    assert submitted[-1] == "an open ending without punctuation", submitted[-1]
    w.translate_thread = real_tt
    w.running = False
    w._quitting = True
    w._teardown()
check("asr: 尾句静默+收尾双证据立送（P16 守卫版）", t_tgroup_silent_early_flush)

def t_level_signal_scale():
    # v2.3.15（P19）：电平量纲接线回归锁——capture 发 0~1 比例，槽必须按
    # 比例消费。旧代码 value>=3 恒假：有声时间戳永不更新（P8/P16 守卫全部
    # 形同虚设）、音量条恒 0。此测试直喂信号源真实量纲，不模拟时间。
    w = MainWindow()
    w.show()
    w.running = True
    w._on_level(0.5)
    assert getattr(w, "_last_level_sound", 0.0) > 0, "0.5 应有声→时间戳更新"
    assert w.level_bar.value() >= 40, w.level_bar.value()   # 百分比换算生效
    mark = w._last_level_sound
    w._on_level(0.02)                      # 低于 3% 噪声地板：不算有声
    assert w._last_level_sound == mark
    w.running = False
    w._quitting = True
    w._teardown()
check("ui: 电平信号量纲接线（P19）", t_level_signal_scale)

def t_placeholder_shows_source_dim():
    # v2.3.17（P22）：show_source=False 时占位卡也必须显示原文（弱化样式）——
    # 攒句/翻译等待期列表只剩"⟳ …"看着像卡死（R11 真人直播实锤）；
    # 译文落地后恢复用户偏好（重新隐藏）
    w = MainWindow()
    w.show()
    w.running = True
    w.config.set("low_latency_mode", False)
    w.config.set("show_source", False)
    w._last_asr_timing = (0.0, 2.0)

    class FakeT:
        def submit(self, text, detected):
            pass
    real_tt = w.translate_thread
    w.translate_thread = FakeT()
    w._on_asr_text("A pending line awaits its translation.", "en", "2.0")
    card = w._take_pending("A pending line awaits its translation.")
    assert card is not None and card.source_label.isVisible(), \
        "占位期原文必须可见（哪怕弱化），不许给用户一排 ⟳"
    assert "italic" in card.source_label.styleSheet()
    card.set_result("一句等待翻译的原文。", "google", "en", False)
    assert not card.source_label.isVisible()       # 落地后回到用户偏好
    assert card.source_label.styleSheet() == ""
    w.translate_thread = real_tt
    w.running = False
    w._quitting = True
    w._teardown()
check("ui: 占位卡弱化原文可见（P22）", t_placeholder_shows_source_dim)

def t_quiet_warn_on_starvation():
    # v2.3.12（P13）：完全无包（Chrome 暂停媒体等）也必须进入静默告警——
    # 旧行为：无包路径直接 continue，_quiet_s 永不累计，用户面对冻住的
    # 电平条与死寂字幕无从判断"应用挂了 or 视频没声"
    from app.audio.capture import CaptureThread
    ct = CaptureThread("system", 0)
    got = []
    ct.low_input.connect(lambda q: got.append(q))
    ct._quiet_s = ct.QUIET_WARN_S          # 模拟 starve 路径已把静默计满
    ct._maybe_warn_quiet()
    assert got == [True], got
    assert ct._warned_quiet
    ct._maybe_warn_quiet()                 # 幂等：不重复告警
    assert got == [True], got
check("capture: 无包静默同样告警（P13）", t_quiet_warn_on_starvation)

def t_card_correction_menu():
    # v2.3.13（P14）：词典可达性——字幕卡右键即纠错，落库核心直测
    w = MainWindow()
    w.show()
    card = w._new_card("THERE WAS ANOTHER GATHERING HOSTED HERE IN DOCUMENTAL")
    card.set_result("纪录片中还在这里举办了另一场聚会", "google", "en", True)
    menu = w._card_menu(card)
    texts = [a.text() for a in menu.actions() if a.text()]
    assert "复制原文" in texts and "复制译文" in texts, texts
    assert any("误听词典" in t for t in texts) and any("译文修正" in t for t in texts), texts
    assert card.translated_text() == "纪录片中还在这里举办了另一场聚会"
    w._add_dict_entry("mishear_map", "DOCUMENTAL", "Norfolk")
    assert (w.config.get("mishear_map") or {}).get("DOCUMENTAL") == "Norfolk"
    w._add_dict_entry("translate_fix_map", "纪录片中", "诺福克")
    assert (w.config.get("translate_fix_map") or {}).get("纪录片中") == "诺福克"
    # 落盘持久性：重读磁盘配置
    import json as _j
    from app.config import CONFIG_FILE
    cfg = _j.load(open(CONFIG_FILE, encoding="utf-8"))
    assert cfg.get("mishear_map", {}).get("DOCUMENTAL") == "Norfolk"
    # 还原 itest 配置，避免污染其他测试
    w.config.set("mishear_map", {})
    w.config.set("translate_fix_map", {})
    menu.deleteLater()
    w._quitting = True
    w._teardown()
check("ui: 卡片右键纠错词典（P14）", t_card_correction_menu)

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
    # 每个非 internal 键都有控件承载（抽样关键键；v2.4.0 面板化后换样）
    for key in ("hotkey_overlay", "overlay_font_size", "instant_caption",
                "show_source", "asr_device", "engine"):
        assert key in _FIELD_SPECS, key
    # 恢复默认不含 internal
    dlg._reset_defaults()
    assert not any(k.startswith(("overlay_x", "overlay_y", "storage_root")) for k in dlg._staged)
check("settings: 字段表完整 + 恢复默认不含 internal", t_settings_fields)

def t_std_rows_coverage():
    # v2.3.0：声明式标准行表——键/属性/回显/暂存语义全覆盖
    from app.ui.settings_dialog import SettingsDialog, _STD_ROWS, _FIELD_SPECS
    from app.config import DEFAULTS
    w = MainWindow()
    dlg = SettingsDialog(w)
    dlg.load_from_config()
    keys = [s["key"] for s in _STD_ROWS]
    assert len(keys) == len(set(keys)), "行表键重复"
    for spec in _STD_ROWS:
        assert spec["key"] in _FIELD_SPECS and spec["key"] in DEFAULTS, spec["key"]
        widget = getattr(dlg, spec["attr"], None)
        assert widget is not None, f"缺控件属性 {spec['attr']}"
        if spec["kind"] == "combo":
            assert widget.currentData() == dlg.c.get(spec["key"]), spec["key"]
        elif spec["kind"] == "spin":
            assert widget.value() == int(dlg.c.get(spec["key"])), spec["key"]
        else:
            assert widget.isChecked() == bool(dlg.c.get(spec["key"])), spec["key"]
    # 暂存语义：改标准控件 → 进 _staged（保存并应用契约不因表驱动而丢）
    dlg._loading = False
    before = bool(dlg.c.get("auto_start"))
    dlg.auto_start_check.setChecked(not before)
    assert dlg._staged.get("auto_start") == (not before), dlg._staged
    dlg._staged.clear()
    dlg.deleteLater()
    w._quitting = True
    w._teardown()
check("settings: 声明式标准行表全覆盖（键/属性/回显/暂存）", t_std_rows_coverage)

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

# ---------- 9) 内置延迟自测（v2.3.20 P26） ----------
def t_latency_telemetry():
    import time as _t
    # 识别段：t_flush 第 4 参 → _lat_reco；提交时刻入 _submit_ts
    w = MainWindow()
    w.show()
    w.running = True
    w.config.set("low_latency_mode", False)

    class FakeTT:
        def __init__(self):
            self.submitted = []

        def submit(self, text, detected):
            self.submitted.append(text)

    real_tt = w.translate_thread
    w.translate_thread = FakeTT()
    w._on_asr_text("Telemetry probe line.", "en", "2.0", _t.monotonic() - 1.25)
    assert len(w._lat_reco) == 1 and 1.0 < w._lat_reco[0] < 1.6, w._lat_reco
    assert w.translate_thread.submitted == ["Telemetry probe line."]
    assert "Telemetry probe line." in w._submit_ts
    # 翻译段：落地取提交时刻 → _lat_tr；无提交记录的迟到结果不误记
    w._on_translated("Telemetry probe line.", "遥测探针行", "test-engine", "en", "")
    assert len(w._lat_tr) == 1 and w._lat_tr[0] >= 0, w._lat_tr
    w._on_translated("ghost line", "无提交记录", "test", "en", "")
    assert len(w._lat_tr) == 1, "无 _submit_ts 的结果不应新增样本"
    # 摘要：打日志后清零
    w._log_latency_summary()
    assert w._lat_reco == [] and w._lat_tr == [] and w._submit_ts == {}
    # _pct 边界
    from app.ui.main_window import _pct
    assert _pct([], 0.5) == 0.0 and _pct([3.0], 0.95) == 3.0
    w.translate_thread = real_tt
    w.running = False
    w._quitting = True
    w._teardown()
check("asr: 内置延迟自测遥测链路（P26）", t_latency_telemetry)

def t_asr_submit_queue_contract():
    # 队元素统一 (ndarray, t_flush)；裸 ndarray 旧格式（smoke/deep_windows）兼容
    import numpy as _np
    from app.asr.engine import AsrThread
    a = AsrThread("tiny", "cpu", "auto")
    arr = _np.zeros(16000, dtype=_np.float32)
    a.submit(arr)
    a.submit((arr, 123.4))
    i1 = a.queue_in.get_nowait()
    i2 = a.queue_in.get_nowait()
    assert isinstance(i1, tuple) and len(i1) == 2 and i1[1] == -1.0, i1
    assert isinstance(i2, tuple) and i2[1] == 123.4, i2
check("asr: 提交队列时间戳契约（P26）", t_asr_submit_queue_contract)

# ---------- v2.6.0 译文质量优化 ----------

def t_quality_new_config_keys():
    # R2/R4：新键进 DEFAULTS 且默认值符合用户裁决（全词匹配开、离线高质量）
    assert cfg.get("fix_whole_word") is True
    assert cfg.get("offline_quality") == "high"
    # R6：死配置已移除
    assert "translate_zh_from_zh" not in DEFAULTS
check("quality: 新配置键默认值 + 死配置移除（R2/R4/R6）", t_quality_new_config_keys)

def t_quality_settings_registered():
    # _FIELD_SPECS/_STD_ROWS 为模块级声明表（v2.3.0 单一登记处），从模块取
    import app.ui.settings_dialog as sd
    specs = sd._FIELD_SPECS
    assert specs["fix_whole_word"] == ("pipeline", "check")
    assert specs["offline_quality"] == ("pipeline", "combo")
    assert "translate_zh_from_zh" not in specs
    rows = {r["key"] for r in sd._STD_ROWS}
    assert {"fix_whole_word", "offline_quality"} <= rows
    assert sd.SettingsDialog._HOTFIX_KEYS == {"mishear_map", "translate_fix_map",
                                              "fix_whole_word", "offline_quality"}
check("quality: 设置页声明表三处登记 + 死配置清除（R2/R4/R6）", t_quality_settings_registered)

def t_quality_thread_hotfix():
    # R4：构造快照映射（high→beam 5）；R5：热更方法生效
    from app.translate.translator import TranslateThread, ArgosEngine
    from app.asr.engine import AsrThread
    tt = TranslateThread("auto", "zh-CN", translate_fix_map={"a": "b"},
                         fix_whole_word=True, offline_quality="fast")
    assert tt._beam_size == 2 and ArgosEngine.beam_size == 2
    assert tt.fix_map == {"a": "b"} and tt._fix_whole_word is True
    tt.update_beam_size(5)
    assert tt._beam_size == 5 and ArgosEngine.beam_size == 5, "beam 热更应同步类级值"
    tt.update_beam_size(0)   # 未知值回落快速档
    assert tt._beam_size == 2 and ArgosEngine.beam_size == 2
    tt.update_fix_map({"x": "y"})
    tt.update_whole_word(False)
    assert tt.fix_map == {"x": "y"} and tt._fix_whole_word is False
    # AsrThread 侧词典热更
    at = AsrThread("tiny", "cpu", "auto")
    at.update_mishear_map({"feline": "feel in"}, True)
    assert at.mishear_map == {"feline": "feel in"} and at._mishear_whole_word is True
check("quality: 线程词典/质量档热更新（R4/R5）", t_quality_thread_hotfix)

def t_quality_cache_key_strip():
    # R3：仅空白差异的原文命中同一条缓存
    # v2.6.4（P2）：键去引擎名——备援切换后旧引擎键不再稀释缓存，
    # 同句译文跨引擎共享（引擎维度独立是旧行为，已废弃）
    from app.translate.translator import TranslateThread
    tt = TranslateThread("google", "zh-CN")
    k1 = tt._cache_key("en", "  hello world  ")
    k2 = tt._cache_key("en", "hello world")
    assert k1 == k2
    assert k1 == "zh-CN:en:hello world", "键应为 目标:源:文本 三段（无引擎前缀）"
check("quality: 缓存键首尾空白规范化（R3）", t_quality_cache_key_strip)

def t_quality_mishear_via_fixmap():
    # R2：识别侧误听词典走 fixmap——整词匹配经 _postprocess 生效
    from app.asr.engine import AsrThread
    at = AsrThread("tiny", "cpu", "auto", mishear_map={"strikes": "罢工"},
                   mishear_whole_word=True)
    assert at._postprocess("U.S. strikes back") == "U.S. 罢工 back"
    assert at._postprocess("airstrikes reported") == "airstrikes reported"
    at.update_mishear_map({"strikes": "罢工"}, False)
    assert at._postprocess("airstrikes reported") == "air罢工 reported", \
        "关闭开关后应回到子串替换"
check("quality: 误听词典经 fixmap 整词匹配（R2/R5）", t_quality_mishear_via_fixmap)

def t_quality_beam_reaches_offline_pack():
    # R4：beam 参数从模块 translate 透传到 PackTranslator.translate
    import app.translate.offline_pack as op
    captured = {}

    class FakeTr:
        def translate(self, text, beam_size=2):
            captured["beam"] = beam_size
            return "译文"

    op._translator_cache[("en", "zh")] = FakeTr()
    op._cache_order.append(("en", "zh"))
    try:
        assert op.translate("hello", "en", "zh", beam_size=5) == "译文"
        assert captured["beam"] == 5
        assert op.translate("hello", "en", "zh") == "译文", "缺省 beam 兼容旧调用"
        assert captured["beam"] == 2
    finally:
        op._translator_cache.pop(("en", "zh"), None)
        if ("en", "zh") in op._cache_order:
            op._cache_order.remove(("en", "zh"))
check("quality: beam 参数透传离线包（R4）", t_quality_beam_reaches_offline_pack)

def t_clear_captions_purges_inflight():
    # v2.6.1（P0-1）：运行中清空必须连同在途状态一起清——否则迟到译文
    # 经 _pending 配对打到已删 C++ 对象上 qFatal。清理项与 stop_pipeline 对齐
    from app.ui.main_window import _orphan_threads
    w = MainWindow()
    card = w._new_card("a")
    w._pending = [("a", card)]
    w._tgroup = ["frag"]
    w._tgroup_by_src = {"b": "grp"}
    w._submit_ts = {"c": 1.0}
    w._clear_captions()
    assert not w._pending, "占位配对应清空"
    assert not w._tgroup, "攒句缓冲应清空"
    assert not w._tgroup_by_src, "攒句簿记应清空"
    assert not w._submit_ts, "延迟簿记应清空"
    tg = getattr(w, "_tgroup_timer", None)
    if tg is not None:
        assert not tg.isActive(), "攒句定时器应停止"
    card.deleteLater()
    w._quitting = True
    w._teardown()
    _orphan_threads()   # 复核孤儿容器可访问（回归 v2.0.7 RuntimeError 守卫）
check("ui: 清空字幕同步清理在途状态（P0-1）", t_clear_captions_purges_inflight)

def t_stop_prewarm_releases_ref():
    # v2.6.1（P0-2）：退出收尾必须接管预热线程——此前游离在孤儿机制外，
    # 预热中退出 → MainWindow 析构销毁运行中 QThread → qFatal
    from app.asr.engine import PrewarmWorker
    from app.ui.main_window import _orphan_threads
    w = MainWindow()
    w._prewarm = PrewarmWorker("tiny", "cpu", w)   # 不 start（offscreen 安全）
    w._stop_prewarm()
    assert w._prewarm is None, "引用应立即释放"
    assert not any(isinstance(t, PrewarmWorker) for t in _orphan_threads()), \
        "未运行的预热线程不应入孤儿容器"
    w._quitting = True
    w._teardown()   # 幂等：再次调用 _stop_prewarm 不应报错
check("ui: 退出收尾释放预热线程引用（P0-2）", t_stop_prewarm_releases_ref)

def t_session_guard_rejects_stale_thread():
    # v2.6.2（P1-6）：新会话中旧线程迟到信号按会话身份拦截——状态/数据/错误
    # 三类槽都要挡住，最重的是旧"音频错误"误停新会话
    from app.asr.engine import AsrThread
    from app.audio.capture import CaptureThread
    from app.translate.translator import TranslateThread
    w = MainWindow()
    old_asr = AsrThread("tiny", "cpu", "auto")
    new_asr = AsrThread("tiny", "cpu", "auto")
    old_cap = CaptureThread("system", -1)
    old_tr = TranslateThread("argos", "zh-CN")
    new_tr = TranslateThread("argos", "zh-CN")
    w.running = True
    w._sid_asr = new_asr
    w._sid_cap = CaptureThread("system", -1)   # 新会话身份（≠ old_cap）
    w._sid_tr = new_tr
    old_asr.text_ready.connect(w._on_asr_text)
    new_asr.text_ready.connect(w._on_asr_text)
    old_tr.result_ready.connect(w._on_translated)
    new_tr.result_ready.connect(w._on_translated)
    old_cap.error_occurred.connect(w._on_pipeline_error)
    new_asr.error_occurred.connect(w._on_pipeline_error)
    old_asr.status_changed.connect(w._on_asr_status)
    # 旧线程迟到原文/译文/状态：全部拦截
    w._caption_seen = False
    old_asr.text_ready.emit("stale text", "en", 1.0, -1.0)
    assert w._caption_seen is False, "旧线程迟到的原文不得上屏"
    before = w.scroll_layout.count()
    old_tr.result_ready.emit("stale text", "旧译文", "argos", "en", "")
    assert w.scroll_layout.count() == before, "旧线程迟到的译文不得建卡"
    old_asr.status_changed.emit("正在加载模型")
    assert "正在加载" not in w.engine_status_label.text(), "旧线程状态不得覆盖状态栏"
    # P1-6 核心：旧采集线程"音频错误"不得误停新会话
    old_cap.error_occurred.emit("音频读取中断: 设备失效")
    assert w.running is True, "旧线程的音频错误不得误停新会话"
    new_asr.error_occurred.emit("普通错误信息")   # 新会话自身错误：放行（非音频类不停止）
    old_tr.result_ready.emit("stale 2", "x", "argos", "en", "")
    assert w.scroll_layout.count() == before, "旧线程第二次迟到译文仍不得建卡"
    # 新线程信号放行（身份匹配）
    new_asr.text_ready.emit("fresh text", "en", 1.0, -1.0)
    assert w._caption_seen is True, "当前会话线程的原文应正常上屏"
    w._quitting = True
    w._teardown()
check("pipeline: 会话身份守卫拦截旧线程迟到信号（P1-6）", t_session_guard_rejects_stale_thread)

def t_stop_pipeline_drain_contract():
    # v2.6.2（P1-4）：排水式停止契约——stop 保留最新段/句、无哨兵、身份
    # 引用存活、引用置 None、capture 尾段被同步取走
    import numpy as np
    from app.asr.engine import AsrThread
    from app.audio.capture import CaptureThread
    from app.translate.translator import TranslateThread
    w = MainWindow()
    cap = CaptureThread("system", -1)
    asr = AsrThread("tiny", "cpu", "auto")
    tr = TranslateThread("argos", "zh-CN")
    w.running = True
    w.capture_thread = cap
    w.asr_thread = asr
    w.translate_thread = tr
    w._sid_cap = cap
    w._sid_asr = asr
    w._sid_tr = tr
    cap._tail_seg = (np.zeros(16, dtype=np.float32), 1.0)
    for _ in range(3):
        asr.submit(np.zeros(16, dtype=np.float32))
    tr.submit("old sentence", "en")
    w.stop_pipeline()
    assert w.running is False
    assert w.capture_thread is None and w.asr_thread is None and w.translate_thread is None
    # 会话身份引用保留——排水链（尾句上屏/转发翻译）依赖它们放行
    assert w._sid_asr is asr and w._sid_tr is tr and w._sid_cap is cap
    assert asr._stop is True
    # 队列 = 保留的最新段 + stop_pipeline 同步直塞的 capture 尾段
    assert asr.queue_in.qsize() == 2, "asr 应保留最新段并接收直塞尾段"
    assert tr._stop is True and tr.queue_in.qsize() == 1, "translate 应保留最新一句"
    # v2.7.6（A）：队列项为 (text, lang, spec) 三元组——排水保留的必须是终版
    _kept = tr.queue_in.get_nowait()
    assert _kept[:2] == ("old sentence", "en")
    assert len(_kept) == 3 and _kept[2] is False, "排水保留的待译句应带 spec=False"
    assert cap.pop_tail_seg() is None, "capture 尾段应已被 stop_pipeline 同步取走"
    head = asr.queue_in.get_nowait()
    tail = asr.queue_in.get_nowait()
    assert head[0] is not None, "保留段应非哨兵"
    assert len(tail[0]) == 16 and tail[1] == 1.0, "第二条应是直塞的 capture 尾段"
    w._quitting = True
    w._teardown()
check("pipeline: 排水式停止契约（P1-4）", t_stop_pipeline_drain_contract)

def t_drop_translation_finalizes_cards():
    # v2.6.2（P1-7）：被队列挤掉的句子占位卡/簿记立即终态化，不再悬挂
    w = MainWindow()
    w.running = True
    card = w._new_card("dropped line")
    card.target_label.setText("⟳ …")
    w.scroll_layout.insertWidget(w.scroll_layout.count() - 1, card)
    w._pending = [("dropped line", card)]
    w._tgroup_by_src = {"combined": ["frag1", "dropped line"]}
    w._submit_ts = {"dropped line": 1.0, "combined": 2.0}
    frag = w._new_card("frag1")
    w._drop_translation("combined")   # 攒句合并句被挤掉 → 整组终态化
    assert "combined" not in w._tgroup_by_src and "combined" not in w._submit_ts
    assert "dropped line" not in w._submit_ts
    pc = w._take_pending("dropped line")
    assert pc is None, "占位配对应已被摘除"
    assert card.target_label.text() != "⟳ …", "占位文案应被终态替换"
    w._quitting = True
    w._teardown()
check("pipeline: 队列丢句占位卡终态化（P1-7）", t_drop_translation_finalizes_cards)

def t_toggle_source_snapshots_threads():
    # v2.6.2（P1-5）：切源在 stop 前快照线程引用（旧代码 stop 后再取引用
    # 恒为 None，"等旧线程退出"从未兑现）
    from app.audio.capture import CaptureThread
    w = MainWindow()
    calls = []
    cap = CaptureThread("system", -1)
    w.capture_thread = cap
    w.asr_thread = None
    w.translate_thread = None
    w.running = True
    orig_stop, orig_start = w.stop_pipeline, w.start_pipeline
    w.stop_pipeline = lambda: calls.append("stop")
    w.start_pipeline = lambda: calls.append("start")
    try:
        w._toggle_source()
        assert calls == ["stop", "start"], "应先停后启"
        new_src = w.config.get("source_type")
        assert new_src in ("microphone", "system")
    finally:
        w.stop_pipeline = orig_stop
        w.start_pipeline = orig_start
    w._quitting = True
    w._teardown()
check("pipeline: 切源前快照线程引用（P1-5）", t_toggle_source_snapshots_threads)

# ---------- 汇总 ----------
check("config: DEFAULTS 全键可读", lambda: [cfg.get(k) for k in DEFAULTS])

# ---------- v2.7.6：推测式增量翻译（延迟三件套之 A） ----------
# 遥测实锤：reco_p50=0.55s、tr_p50=0.06s，但 hold_p50=4.13s——端到端延迟的
# 87% 是"攒句等下一片冲刷"。推测式翻译让碎片一到达就上屏译文并原地生长，
# 整句终版随后接管终态化。这组锁钉住"中间版只更新、终版才收口"的契约。

class _StubTr(object):
    """离线引擎替身：只记录提交，不真翻（_active_engine 决定推测闸门放行）。"""
    _active_engine = "argos"

    def __init__(self, engine="argos"):
        self._active_engine = engine
        self.sent = []

    def isRunning(self):
        return True

    def submit(self, text, lang, spec=False):
        self.sent.append((text, lang, bool(spec)))
        return []


def t_spec_translate_growth():
    w = MainWindow()
    w.show()
    w.running = True
    stub = _StubTr()
    w._active_translate = lambda: stub        # 不动 translate_thread，stop_pipeline 不受影响
    old_spec, old_ll = w.config.get("spec_translate"), w.config.get("low_latency_mode")
    w.config.set("spec_translate", True)
    w.config.set("low_latency_mode", True)
    try:
        w._tgroup = []
        w.session_count = 0
        w.session_label.setText("本次会话：0 条")
        # 碎片一到达：原文上屏 + 推测版已提交（spec=True）
        w._on_asr_text("The quick", "en", "1.0")
        card1 = w._pending[0][1]
        assert stub.sent[-1] == ("The quick", "en", True), stub.sent
        active_before = w._active_card     # 占位卡建立时即按 v2.2.5 聚焦
        # 推测回复：译文上屏，但卡片仍是"未完成"（终版还要靠它配对）
        w._on_spec_translated("The quick", "快速的", "argos", "en", "")
        assert card1.spec is True and card1.target_label.text() == "快速的"
        assert card1.is_pending() is True, "推测态必须仍算未完成"
        assert len(w._pending) == 1, "推测回复不得摘 pending"
        assert w.session_count == 0, "中间版不计入已完成字幕条数"
        assert w._active_card is active_before, "中间版不得改写聚焦卡（那是终版的职责）"
        # 碎片二（小写开头=同句延续）：送更长版本，译文在同一批卡上生长
        w._on_asr_text("brown fox", "en", "1.0")
        card2 = w._pending[-1][1]
        assert stub.sent[-1] == ("The quick brown fox", "en", True), stub.sent
        w._on_spec_translated("The quick brown fox", "快速的棕色狐狸", "argos", "en", "")
        assert card2.spec is True and card2.target_label.text() == "快速的棕色狐狸"
        # 终版冲刷：同一文本以 spec=False 提交，回复后终态化并收编前片
        w._flush_tgroup()
        assert stub.sent[-1] == ("The quick brown fox", "en", False), stub.sent
        w._on_translated("The quick brown fox", "敏捷的棕色狐狸", "argos", "en", "")
        assert card2.spec is False, "终版必须脱离推测态"
        assert card2.target_label.text() == "敏捷的棕色狐狸"
        assert card2.is_pending() is False
        assert len(w._pending) == 0, "终版应摘走 pending"
        assert w.session_count == 1, "只有终版计入会话条数"
        # 代际守卫：条目仍在但代数不符（清理竞态窗口）→ 中间版必须被丢弃
        w._running_guard_gen = w._tgroup_gen
        w._on_asr_text("Guarded piece", "en", "1.0")
        w._spec_inflight["Guarded piece"] = (w._tgroup_gen + 7, ["Guarded piece"])
        w._on_spec_translated("Guarded piece", "不该上屏的译文", "argos", "en", "")
        guarded = w._pending[-1][1]
        assert guarded.spec is False and guarded.target_label.text() in ("...", "⟳ …"), \
            "代数不符的中间版必须丢弃，不得上屏"
        # 迟到的中间版也不得覆盖已终态化的卡
        w._on_spec_translated("The quick brown fox", "迟到污染", "argos", "en", "")
        assert card2.target_label.text() == "敏捷的棕色狐狸"
        w.stop_pipeline()
    finally:
        w.config.set("spec_translate", old_spec)
        w.config.set("low_latency_mode", old_ll)
check("card: 推测译文原地生长、终版接管", t_spec_translate_growth)


def t_spec_translate_online_engine_never_subs():
    """在线引擎（google/mymemory）必须**完全不产生推测提交**——有额度与限流。"""
    w = MainWindow()
    w.show()
    w.running = True
    online = _StubTr("google")
    w._active_translate = lambda: online
    old_spec, old_ll = w.config.get("spec_translate"), w.config.get("low_latency_mode")
    w.config.set("spec_translate", True)
    w.config.set("low_latency_mode", True)
    try:
        w._tgroup = []
        w._on_asr_text("The quick", "en", "1.0")
        w._on_asr_text("brown fox", "en", "1.0")
        assert all(s[2] is False for s in online.sent), \
            f"在线引擎不得出现 spec=True 提交：{online.sent}"
        w.stop_pipeline()
    finally:
        w.config.set("spec_translate", old_spec)
        w.config.set("low_latency_mode", old_ll)
check("pipeline: 在线引擎永不推测提交", t_spec_translate_online_engine_never_subs)


def t_overlay_spec_growth():
    """面板侧：中间版让原文与译文**一起生长**在同一行，行保持待决；
    终版才收口并计未读（中间版不算完成一句）。"""
    from app.ui.caption_overlay import CaptionOverlay
    ov = CaptionOverlay()
    ov.show()
    ov._follow = False                      # 隔离未读计数逻辑
    ov.show_pending("The quick")
    ov.update_spec_result("The quick", "快速的", True)
    r = ov._rows[-1]
    assert r["pending"] is True, "中间版不得终态化行"
    assert r.get("spec") is True
    assert r["src_text"] == "The quick" and r["tgt_text"] == "快速的"
    ov.show_pending("brown fox")            # 小写开头=延续片，同行生长
    assert len([x for x in ov._rows if x["pending"]]) == 1, "延续片应在同一行生长"
    n_before = len(ov._rows)
    # combined 键以末片结尾 → 后缀匹配命中同一行，不新增行
    ov.update_spec_result("The quick brown fox", "快速的棕色狐狸", True)
    assert len(ov._rows) == n_before, "推测更新不得新增行"
    r = ov._rows[-1]
    assert r["src_text"] == "The quick brown fox", \
        "原文行必须同步生长——否则重现 v2.7.4（B-8）'半句原文配整句译文'分叉"
    assert r["tgt_text"] == "快速的棕色狐狸" and r["pending"] is True
    unread0 = ov._unread
    assert ov._last_result == ("", ""), "中间版不得改写 _last_result"
    ov.show_pending_result("The quick brown fox", "敏捷的棕色狐狸", True)
    r = ov._rows[-1]
    assert r["pending"] is False and r.get("spec") is False, "终版必须收口并清除推测标记"
    assert r["tgt_text"] == "敏捷的棕色狐狸"
    assert ov._unread == unread0 + 1, "只有终版计未读"
    # 找不到待决行时静默丢弃（已收编/已终态），绝不新建行
    n2 = len(ov._rows)
    ov.update_spec_result("不存在的句子", "幽灵译文", True)
    assert len(ov._rows) == n2, "无匹配行时不得新建行"
    ov.deleteLater()
check("panel: 推测中间版同行生长、终版收口", t_overlay_spec_growth)


def t_spec_finalize_on_stop():
    """停止时：推测态卡片**保留已上屏的译文**（半句也胜过失败文案），
    只解除推测态并标注可能不完整；无推测译文的卡片仍走原失败文案路径。"""
    w = MainWindow()
    w.show()
    w.running = True
    stub = _StubTr()
    w._active_translate = lambda: stub
    old_spec, old_ll = w.config.get("spec_translate"), w.config.get("low_latency_mode")
    w.config.set("spec_translate", True)
    w.config.set("low_latency_mode", True)
    try:
        w.session_count = 0
        w._on_asr_text("Half done", "en", "1.0")
        w._on_spec_translated("Half done", "半句译文", "argos", "en", "")
        card = w._pending[0][1]
        assert card.spec is True
        w._on_asr_text("No result yet", "en", "1.0")
        plain = w._pending[-1][1]
        w.stop_pipeline()
        assert card.spec is False, "停止应解除推测态"
        assert card.target_label.text() == "半句译文", \
            "已有推测译文必须保留，不得被'未完成翻译'覆盖"
        assert "不完整" in card.meta_label.text(), card.meta_label.text()
        assert plain.target_label.text() == "[翻译失败]", "无译文的卡仍走失败终态"
        assert "未完成翻译" in plain.meta_label.text()
        assert len(w._pending) == 0
    finally:
        w.config.set("spec_translate", old_spec)
        w.config.set("low_latency_mode", old_ll)
check("card: 停止时推测译文保留并标注", t_spec_finalize_on_stop)


report = "\n".join(RESULTS)
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_report.txt"),
          "w", encoding="utf-8") as f:
    f.write(report + "\n\n=== FAIL DETAIL ===\n" + "\n\n".join(t for _, t in FAILS))
print(report)
print(f"TOTAL: {len(RESULTS)} PASS: {len(RESULTS) - len(FAILS)} FAIL: {len(FAILS)}")
# v2.3.7 诊断：列出退出时仍存活的线程——定位 ExitProcess 竞态 AV 的肇事者
try:
    import threading as _th
    print("ALIVE-THREADS:", [f"{t.name}(daemon={t.daemon})" for t in _th.enumerate()])
except Exception:
    pass
sys.stdout.flush()
sys.stderr.flush()
# v2.3.7：结果已落盘+打印后强制退出。os._exit 在 Windows 上仍走 CRT（触发各
# DLL 的 PROCESS_DETACH，Qt 原生线程可在其中 AV——实测 34/34 全过但退出码
# 0xC0000005 的元凶）。kernel32.TerminateProcess 跳过 CRT，零收尾窗口。
# v2.3.8 定性结论（faulthandler 抓栈实证）：AV 发生在主线程 TerminateProcess
# 的进程收尾路径（Qt DLL 卸载竞态），无肇事后台线程、测试与报告均已全部完成。
# 与 build.yml 对 smoke test 的既有备注同源——**退出码不可信，判定以
# test_report.txt / stdout 的 TOTAL 行为准**。TerminateProcess 仍保留：
# 它杜绝了早先的"挂死 5 分钟"（不退出）问题，代价只是偶发退出码被改写。
try:
    import ctypes
    _k = ctypes.windll.kernel32
    _k.TerminateProcess(_k.GetCurrentProcess(), 1 if FAILS else 0)
except Exception:
    pass
os._exit(1 if FAILS else 0)
