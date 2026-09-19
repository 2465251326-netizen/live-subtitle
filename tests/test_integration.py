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

# 测试名里有 ✕/⏸/▶ 等非 GBK 字符：Windows 默认控制台下 print 直接
# UnicodeEncodeError，且要跑到第 81 项才炸——前半截已跑完，极易被误读成代码缺陷。
# 结果文件本来就是 utf-8 写的，这里只把控制台一并转过去。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

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
    # v2.20.3：收起/展开的唯一入口是 ⋯ 菜单（工具条那件按钮自 e94cb59 起从未
    # 进过布局，功能在生产里不可达而 README 还在教它）。菜单文案随状态翻转。
    acts_c = ov._build_menu()
    assert ov._menu_acts["collapse"].text().startswith("展开为完整面板"),         ov._menu_acts["collapse"].text()
    acts_c.deleteLater()
    ov._toggle_collapse()
    assert got["collapsed"] == [True, False] and ov._scroll.isVisible()
    acts_c2 = ov._build_menu()
    assert ov._menu_acts["collapse"].text().startswith("收起为迷你条")
    # 菜单项真的能驱动收放（旧锁只测按钮文案，测不到"有没有入口"）
    ov._menu_dispatch(ov._menu_acts["collapse"])
    assert ov._collapsed is True
    ov._menu_dispatch(ov._menu_acts["collapse"])
    assert ov._collapsed is False
    acts_c2.deleteLater()
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
    assert w._active_card.property("state") == "active"
    assert w._active_card.objectName() == "CaptionCard"     # 状态不再靠改名
    w._on_asr_text("life2", "en", "1.0")
    w._on_translated("life2", "一生二", "google", "en", "")
    cards = [w.scroll_layout.itemAt(i).widget() for i in range(w.scroll_layout.count())
             if w.scroll_layout.itemAt(i).widget()]
    old = [c for c in cards if c.property("state") == "old"]
    assert len(old) == 1 and w._active_card.property("state") == "active"
    w.stop_pipeline()
    assert len(w._pending) == 0
check("card: 流式配对/聚焦切换/stop清空", t_card_lifecycle)


def t_card_focus_style_pixels():
    """v2.18.1 像素级锁：聚焦/渐隐样式必须**真的渲染出来**。
    历史教训：styles.py 写的是 `QFrame#CaptionCard#CaptionCardActive`
    （Qt 解释为"祖先名 CaptionCard + 自身名 CaptionCardActive"，卡片互为
    兄弟永不成立），而 set_active 又覆写唯一 objectName 把基础卡面规则一起
    踩掉——聚焦卡实际渲染成窗口底色 #0f1115（卡片"没有脸"），而旧锁只断言
    objectName 字符串，于是 102 项全绿照样漏过一个上线即失效的视觉特性。
    这条锁断言的是像素，不是内部状态。"""
    from app.ui.styles import DARK_QSS
    from PySide6.QtWidgets import QWidget, QVBoxLayout

    def px(img, x, y):
        p = img.pixel(x, y)
        return (p >> 16) & 255, (p >> 8) & 255, p & 255

    def near(c, hexs, tol=6):
        img = c.grab().toImage()
        r, g, b = px(img, 6, max(0, img.height() - 6))
        er, eg, eb = (int(hexs[i:i + 2], 16) for i in (1, 3, 5))
        return abs(r - er) <= tol and abs(g - eg) <= tol and abs(b - eb) <= tol

    host = QWidget()
    host.setStyleSheet(DARK_QSS)
    lay = QVBoxLayout(host)
    idle = CaptionCard("idle card text")
    act = CaptionCard("active card text")
    act.set_result("聚焦卡译文", "google", "en", True)
    old = CaptionCard("old card text")
    old.set_result("历史卡译文", "google", "en", True)
    for c in (idle, act, old):
        lay.addWidget(c)
    host.resize(420, 300)
    host.show()
    act.set_active(True)
    old.set_active(False)
    app.processEvents()
    assert near(idle, "#161a22"), f"基础卡面底色丢失: {px(idle.grab().toImage(), 6, idle.height() - 6)}"
    assert near(act, "#1a1f2b"), f"聚焦态背景未生效: {px(act.grab().toImage(), 6, act.height() - 6)}"
    assert near(old, "#161a22"), f"历史态把基础卡面踩掉了: {px(old.grab().toImage(), 6, old.height() - 6)}"
    # 渐隐：历史卡原文文字必须比基础卡更暗
    def brightest(c):
        img = c.source_label.grab().toImage()
        return max((((img.pixel(x, y) >> 16 & 255) + (img.pixel(x, y) >> 8 & 255)
                     + (img.pixel(x, y) & 255)) for y in range(0, max(1, img.height()), 2)
                    for x in range(0, max(1, img.width()), 2)), default=0)
    b_idle, b_old = brightest(idle), brightest(old)
    assert b_old < b_idle, f"历史卡文字未渐隐（idle={b_idle} old={b_old}）"
    for c in (idle, act, old):
        c.setParent(None); c.deleteLater()
    host.deleteLater()
check("card: 聚焦/渐隐样式像素级生效（v2.18.1 双 ID 选择器回归）", t_card_focus_style_pixels)

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
    # v2.20.4 换代：译文未落地的卡**不再回退成原文入轴**。旧注释写"回退原文并入轴"，
    # 与同文件 v2.7.4（B-12）"占位/失败卡不得混进导出"直接矛盾；实测一次导出
    # 3 条 cue 里两条是英文原文，而提示写着"已导出 3 条"。
    pending = CaptionCard("only source")
    failed = CaptionCard("bad line")
    failed.set_failed("翻译失败")
    srt, m = build_export_text([c1, c2, pending, failed], "srt")
    assert m == 2, (m, srt)
    # 时长来自 Whisper：cue1 结束被 cue2 起点前移 0.1s 夹紧（2.5→2.4）
    assert "1\n00:00:00,000 --> 00:00:02,400\n你好世界" in srt, srt
    # cue2 现在是最后一条（未落地的第 3 条已不入轴），不再被后一条夹紧 → 2.5+1.8
    assert "2\n00:00:02,500 --> 00:00:04,300\n第二行" in srt, srt
    assert "only source" not in srt and "bad line" not in srt, srt
    txt, n = build_export_text([c1, c2], "txt")
    assert n == 2 and txt.startswith("[") and "你好世界" in txt
    # v2.2.12：长句折两行（CJK 在标点处断）
    longc = CaptionCard("a very long sentence " * 5)
    longc.set_result("这是一段很长的中文译文，" * 6, "google", "en", False)
    longc.t_start, longc.dur_s = 10.0, 3.0
    srt2, _ = build_export_text([longc], "srt")
    assert "，\n" in srt2, srt2
    assert all(len(line) <= 44 for line in srt2.splitlines() if " --> " not in line)
    # 只有未落地卡时：SRT 一条都不出（v2.20.4 换代，旧断言是"仍占 1 条"）
    assert build_export_text([pending], "srt")[1] == 0
    assert build_export_text([pending], "txt")[1] == 0
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
    lab = w._quick_labels["overlay"]
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
        w.config.set("hotkey_overlay", "")              # 压根没配：指路托盘右键菜单
        w._refresh_quick_panel()
        # v2.20.1：设置页「启用字幕面板」勾选已删，指引不可能再指向「设置-显示」
        assert "托盘右键可重新显示" in lab.text(), lab.text()
        assert "设置-显示" not in lab.text(), lab.text()
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


def t_select_engine_ex_offline_last_resort():
    """v2.20.4：在线全挂时**先落到已安装的离线包**，而不是硬写 mymemory。

    旧行为把"无网 + 装好了 en→zh 包 + 引擎选自动"（正是离线使用的主流配置）
    的用户推进死胡同：`mymemory` 本身就是探不通的那个，于是每条字幕都失败，
    而横幅还写着「改用「自动」引擎」——自动恰恰是他们的默认值。"""
    import app.translate.translator as tr
    orig = tr.probe_engine
    try:
        tr.probe_engine = lambda name, timeout=2.5, src="", tgt="": (
            name == "argos", "离线包 en→zh 可用" if name == "argos" else "ConnectTimeout")
        eng, fails = tr.select_engine_ex(src="en", tgt="zh-CN")
        assert eng == "argos", eng
        assert len(fails) == 2, fails          # 两条在线失败原因仍要交给横幅
        # 没有离线包时照旧落 mymemory（行为不回退）
        tr.probe_engine = lambda name, timeout=2.5, src="", tgt="": (False, "不可用")
        eng2, _f2 = tr.select_engine_ex(src="en", tgt="zh-CN")
        assert eng2 == "mymemory", eng2
    finally:
        tr.probe_engine = orig


check("translate: 在线全挂时落已装离线包（v2.20.4 离线死胡同）",
      t_select_engine_ex_offline_last_resort)


def t_dead_translate_thread_finalizes_card():
    """v2.20.4：关攒句路径缺的这道守卫，此前让卡片永久停在 "⟳ …"。

    攒句路径（`_flush_tgroup`）自 v2.7.4（B-1）起就有"翻译线程已退出→整组终态化"
    的守卫，逐片直送那条路只判了 `tr is None`。线程自然退出后文本投进死队列，
    永不回来：占位卡停在 "⟳ …"、日志零线索，用户只会以为"翻译很慢"。
    文案也要分开：写「翻译队列繁忙」会把人支到错误的下一步。"""
    w = MainWindow()
    # 集成套件共用一个配置 home：本锁改的两个全局键必须存原值、finally 还原，
    # 否则后面所有依赖"攒句开"的锁全部误报（v2.20.0 实测一次带走 6 条）
    old_g = w.config.get("translate_grouping")
    old_ll = w.config.get("low_latency_mode")
    try:
        w.config.set("translate_grouping", False)
        w.config.set("low_latency_mode", False)

        class DeadT:
            def submit(self, text, detected):
                raise AssertionError("线程已死还往队列里投")

            def isRunning(self):
                return False

        real = w.translate_thread
        # 主窗按会话身份解析线程（`_active_translate`），直接换 `translate_thread`
        # 属性不会被读到——这里替换解析结果本身
        w._active_translate = lambda: DeadT()
        w._on_asr_text("A line that will never be translated.", "en", "2.0")
        # 守卫生效时这张占位卡会被就地终态化并从 _pending 摘除——所以先确认
        # 待决队列空了，再从在场卡片里核对文案（`_take_pending` 已经取不到了）
        assert not w._pending, "占位卡没被终态化，仍挂在待决队列里（永久 ⟳）"
        card = [w.scroll_layout.itemAt(i).widget() for i in range(w.scroll_layout.count())]
        card = [c for c in card if isinstance(c, CaptionCard)
                and c.source_label.text().startswith("A line that will never")]
        assert len(card) == 1, "卡片不见了"
        card = card[0]
        assert card is not None and not card.is_pending(), \
            "翻译线程已退出时占位卡仍悬挂 ⟳"
        # set_failed 的契约：译文行写 "[翻译失败]"，原因进 meta 行
        assert card.target_label.text() == "[翻译失败]", card.target_label.text()
        assert "翻译已停止" in card.meta_label.text(), card.meta_label.text()
        w._active_translate = MainWindow._active_translate.__get__(w, MainWindow)
        w.translate_thread = real
    finally:
        w.config.set("translate_grouping", old_g)
        w.config.set("low_latency_mode", old_ll)
        w._teardown()
        w.deleteLater()


check("translate: 线程已退出时逐片路径也终态化卡片（v2.20.4 缺守卫）",
      t_dead_translate_thread_finalizes_card)

def t_panel_drag_contract():
    # v2.4.0：面板是"诚实的板"——整板可拖、右缘调宽；穿透/紧凑带/三形态旧 API
    # 必须已随旧字幕条退役（防幽灵回归）。
    # v2.20.1：双击工具条贴边随贴边能力一并退役（本锁反向钉住"不许再动位置"）
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
    app.processEvents()
    p_before = (ov.x(), ov.y())
    # v2.20.1（用户点名删除贴边能力）：双击工具条**不得**再把面板贴到屏幕顶/底。
    # 旧实现在这里必红（`_snap_cycle` 会把 y 推到 g.top()+8）
    ov.mouseDoubleClickEvent(_Ev(100, 10))
    assert (ov.x(), ov.y()) == p_before, \
        f"双击工具条仍会移动面板（贴边应已整族退役）：{p_before} -> {(ov.x(), ov.y())}"
    # 双击底缘 = 恢复自动高度（与 ⋯ 菜单同源，不属于贴边，保留）
    ov._user_height = 400
    ov.mouseDoubleClickEvent(_Ev(100, max(0, ov.height() - 2)))
    assert ov._user_height is None, "双击底缘应恢复自动高度"
    # 菜单里也不得再有「贴到屏幕四向」四项
    menu = ov._build_menu()
    names = [a.text() for a in menu.actions() if a.text()]
    assert not [n for n in names if "贴到屏幕" in n], f"菜单仍带贴边项：{names}"
    menu.deleteLater()
    ov.deleteLater()
check("panel: 整板拖移/右缘调宽契约 + 贴边能力已退役（P31→v2.20.1）",
      t_panel_drag_contract)

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
    """v2.10.0 立、v2.20.1 改：面板**常驻实时显示**，`overlay_enabled` 键已删。

    新契约：① 构造 MainWindow 即可见——不存在任何"启用"开关能把它关掉；
    ② DEFAULTS 与运行期配置里都不得再有 `overlay_enabled`；
    ③ 热键与托盘右键菜单共用 `_toggle_overlay_hotkey`：隐藏只在当次运行内有效，
       再按一次恢复显示；④ 隐藏时仪表盘给"按 xxx 打开"的指引（v2.7.4 B-9 的
       "文案不许承诺做不到的事"——现在设置页已经没有勾选可指，只能指热键/托盘）。"""
    from app.config import DEFAULTS
    assert "overlay_enabled" not in DEFAULTS, "启用开关键应已随常驻实时显示删除"
    assert not hasattr(MainWindow, "set_overlay_enabled"), "set_overlay_enabled 应已删除"
    w = MainWindow()
    try:
        assert w.overlay.isVisible(), "常驻实时显示：构造后必须可见"
        ql = getattr(w, "_quick_labels", None)
        if ql:
            assert str(ql["overlay"].text()).startswith("已开启"), \
                "仪表盘必须显示'已开启'——先 show 再刷面板的顺序不许反"
        w._toggle_overlay_hotkey()          # 热键 / 托盘「显隐字幕面板」同一路径
        assert not w.overlay.isVisible(), "热键隐藏当次生效"
        if ql:
            txt = str(ql["overlay"].text())
            assert "已关闭" in txt, txt
            # 设置页已无启用勾选，指引不得再指向它
            assert "设置-显示" not in txt, f"指引仍指向已删除的设置页勾选：{txt}"
        w._overlay_hk_last = 0.0           # 解除 250ms 防抖（既有机制，非本测试对象）
        w._toggle_overlay_hotkey()
        assert w.overlay.isVisible(), "再按热键应重新显示（toggle 语义不变）"
        assert "overlay_enabled" not in w.config._data, \
            "显隐不得落盘：配置里不该冒出这个键"
        w._quitting = True
        w._teardown()
    finally:
        w.deleteLater()
check("panel: 悬浮条常驻实时显示、热键/托盘显隐当次有效", t_overlay_resident_on_launch)

def t_overlay_dual_layout():
    """v2.11.0 立、v2.20.1 改：上下双语全链路——**两栏逐句累积** + 当前句
    流式生长（新句另起一条/延续拼接）、推测版就地更新、终版收口校准、
    clear 全部归零、纯译文显隐、收起态取当前句。"""
    from app.ui.caption_overlay import CaptionOverlay
    ov = CaptionOverlay()
    ov.show()
    ov.set_layout_mode("dual")
    assert ov.is_dual()
    assert ov._dual_body.isVisible() and not ov._scroll.isVisible()
    # 空态：无槽 + 占位提示可见（v2.20.1：占位不再写进译文标签）
    assert ov._dual_src is None and ov._dual_tgt is None
    assert ov._dual_hint.isVisible()
    # 新句起点：开一对条目，原文上屏 + 译文占位（淡色推测态）
    # v2.22.0（§37.1 F8）：占位文案与列表行统一为"⟳ 翻译中…"——裸一个 "…"
    # 在 22px 字号下只是一颗灰点，用户分不清是在翻译还是卡死
    ov.show_pending("The quick brown")
    assert ov._dual_src.text() == "The quick brown"
    assert ov._dual_tgt.text() == "⟳ 翻译中…"
    assert ov._dual_tgt.property("spec") is True
    assert not ov._dual_hint.isVisible(), "有内容后占位必须让位"
    # 延续片段：整句打字机式生长在**同一条**上（拉丁补空格）
    ov.show_pending("fox jumps")
    assert ov._dual_src.text() == "The quick brown fox jumps"
    assert len(ov._dual_src_items) == 1, "延续片段不得另起一条"
    # 推测版：原文校准整句、译文就地更新且保持推测态
    ov.update_spec_result("The quick brown fox jumps", "敏捷的棕色狐狸跳", True)
    assert ov._dual_tgt.text() == "敏捷的棕色狐狸跳"
    assert ov._dual_tgt.property("spec") is True
    # 终版收口：正式样式 + _last_result 落账（仍只一条）
    ov.show_pending_result("The quick brown fox jumps", "敏捷的棕色狐狸跳了起来", True)
    assert ov._dual_tgt.text() == "敏捷的棕色狐狸跳了起来"
    assert ov._dual_tgt.property("spec") is False
    assert ov._last_result == ("The quick brown fox jumps", "敏捷的棕色狐狸跳了起来")
    assert len(ov._dual_src_items) == 1
    # 下一句（大写开头）另起一条，**上一句留在栏里**（v2.20.1 累积契约）
    ov.show_pending("Over the lazy dog")
    assert ov._dual_src.text() == "Over the lazy dog"
    assert ov._dual_src_items[0]["lab"].text() == "The quick brown fox jumps", \
        "上一句原文被顶掉了（累积失效）"
    ov.show_pending("今天天气")
    assert ov._dual_src.text() == "今天天气"
    ov.show_pending("is fine")
    assert ov._dual_src.text() == "今天天气is fine"
    assert len(ov._dual_src_items) == 3 == len(ov._dual_tgt_items), \
        f"两栏条目数必须成对增长：{len(ov._dual_src_items)}/{len(ov._dual_tgt_items)}"
    # 只有最新一张译文卡片带蓝条强调（DualTgtRowNewest），其余素底
    names = [it["card"].objectName() for it in ov._dual_tgt_items]
    assert names[-1] == "DualTgtRowNewest" and set(names[:-1]) == {"DualTgtRow"}, names
    # 关原文开关 = 纯译文：整个原文栏与分隔线一起收起（v2.18.1 语义）
    ov._dual_show_result("Hello world", "你好世界", False)
    assert not ov._dual_src_wrap.isVisible() and not ov._dual_sep.isVisible()
    assert ov._dual_tgt.text() == "你好世界"
    # 收起态：迷你条从双语区取**当前句**
    ov._dual_show_result("Stay focused", "保持专注", True)
    ov.set_collapsed(True)
    assert ov._mini_tgt.text() == "保持专注"
    assert ov._mini_src.text() == "Stay focused"
    ov.set_collapsed(False)
    # 清空 → 两栏全部归零 + 占位恢复（v2.20.1：旧实现只清一对标签）
    n_before = len(ov._dual_src_items)
    assert n_before >= 4, f"前置：应已累积多条，实得 {n_before}"
    ov.clear_caption()
    assert ov._dual_src_items == [] and ov._dual_tgt_items == []
    assert ov._dual_src is None and ov._dual_tgt is None
    assert ov._dual_hint.isVisible()
    assert ov._last_result == ("", "")
    # 清空后下一句仍能正常开槽（指针归 None 的路径）
    ov.show_pending("Fresh start after clear")
    assert len(ov._dual_src_items) == 1
    assert ov._dual_src.text() == "Fresh start after clear"
    # 切回列表模式：滚动区回归、双语区隐藏
    ov.set_layout_mode("list")
    assert not ov.is_dual()
    assert ov._scroll.isVisible() and not ov._dual_body.isVisible()
    ov.deleteLater()
check("panel: 上下双语布局（dual）全链路：两栏逐句累积", t_overlay_dual_layout)

def t_overlay_dual_user_height_body():
    """v2.17.0 立、v2.19.0 改、v2.20.0 再改：拉高面板的高度分配契约。
    v2.19.0 契约（历史按内容定份额 ≤45% 屏、当前句吃剩余）随历史区一并删除，
    v2.20.0 只剩一块正文：拉高 100% 落到 `_dual_body`，且必须保住
    「原文 30 + 把手 8 + 译文 30 + 边距」的可拖下限——低于它分割线的钳制区间
    宽度归零、彻底拖不动（v2.19.0 真实鼠标事件流实测 0 位移的根因）。"""
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
    # 没有任何"空历史块"吃空间：拉高全部落到正文区
    assert ov._dual_body.height() > body_before, \
        f"拉高应落到正文区：{body_before} -> {ov._dual_body.height()}"
    assert ov.height() >= 620 - 14, f"面板总高锁定：{ov.height()} vs 620"
    assert ov._dual_body.height() >= 92, \
        f"正文区必须保住可拖下限（原文+把手+译文）：{ov._dual_body.height()}"
    assert ov._dual_body.height() <= ov.height(), "正文区不得超出面板总高"
    # v2.20.0：历史区整块删除——面板里不得再挂任何历史滚动控件
    assert not hasattr(ov, "_dual_hist"), \
        "dual 历史区控件仍存在（本轮已裁决删除，回看历史归 list 布局）"
    assert not hasattr(ov, "_dual_hist_rows")
    ov.deleteLater()
check("panel: dual 拉高全落正文区（历史区已删 + 保住可拖下限）",
      t_overlay_dual_user_height_body)

def t_overlay_dual_split_drag():
    """v2.18.0：唯一可拖分割 = 原文/译文之间的 sep——拖出的原文区高度
    （_dual_src_h_user）被 _relayout 固定生效、译文区吃剩余。"""
    from app.ui.caption_overlay import CaptionOverlay
    ov = CaptionOverlay()
    ov.show()
    ov.set_layout_mode("dual")
    ov._dual_show_pending("Current sentence growing here")
    ov._dual_spec("Current sentence growing here", "当前句在这里生长", True)
    for _ in range(6):
        app.processEvents()
    body0 = ov._dual_body.height()
    # 模拟分割把手下拖 120px（原文区变大、译文区让位——两者各自滚动）
    ov.set_dual_src_h_user(max(60, ov._dual_src_wrap.height() + 120))
    for _ in range(6):
        app.processEvents()
    s1 = ov._dual_src_wrap.height()
    assert abs(s1 - max(60, ov._dual_src_wrap.height())) <= 4 or s1 >= 60, \
        f"原文区应为用户高度：{s1}"
    assert ov._dual_src_h_user >= 60
    # 拖回小值：原文区缩小
    ov.set_dual_src_h_user(60)
    for _ in range(6):
        app.processEvents()
    assert ov._dual_src_wrap.height() <= 70, \
        f"原文区应回缩：{ov._dual_src_wrap.height()}"
    assert ov._dual_body.height() >= body0 - 8, "body 总高不应突变"
    ov.deleteLater()
check("panel: dual 原文/译文分割线拖拽（唯一分割）", t_overlay_dual_split_drag)

# v2.18.0：t_dual_split_hit_global 已随历史分割交互移除（_dual_split_hit
# 不复存在——唯一可拖分割 = sep 本体，事件直接发给它，无坐标命中判定）

def t_overlay_dual_split_real_drag():
    """v2.18.0：**真实事件流**拖拽——QMouseEvent + sendEvent 发到 sep 本体
    （唯一可拖分割线），验证事件路由：press 启动拖拽、move 跟手、release
    收口落盘。"""
    from app.ui.caption_overlay import CaptionOverlay
    from PySide6.QtCore import Qt, QPoint, QPointF, QEvent
    from PySide6.QtGui import QMouseEvent
    ov = CaptionOverlay()
    ov.set_layout_mode("dual")
    ov.resize(620, 300)
    ov.move(80, 60)
    ov.show()
    app.processEvents()
    ov._dual_show_pending("Current growing here")
    ov._dual_spec("Current growing here", "当前句在这里生长", True)
    for _ in range(6):
        app.processEvents()
    assert ov._dual_sep.isVisible()
    s0 = ov._dual_src_wrap.height()

    def send_mouse(target, gtype, gpos, button=None):
        local = target.mapFromGlobal(gpos)
        ev = QMouseEvent(QEvent.Type(gtype), QPointF(local), QPointF(gpos),
                         button or Qt.NoButton, button or Qt.NoButton,
                         Qt.NoModifier)
        QApplication.sendEvent(target, ev)

    g = ov._dual_sep.mapToGlobal(QPoint(ov._dual_sep.width() // 2, 4))
    send_mouse(ov._dual_sep, QEvent.Type.MouseButtonPress, g, Qt.LeftButton)
    assert ov._dual_split_drag, "sep press 应经事件过滤器启动分割拖拽"
    for dy in (30, 60, 90):
        send_mouse(ov._dual_sep, QEvent.Type.MouseMove,
                   QPoint(g.x(), g.y() + dy), Qt.LeftButton)
        # v2.18.0：_relayout 走 singleShot 排期——不 processEvents 的话
        # wrap.height() 停在旧值，s_mid 断言会误报（排期消费后才有真高度）
        for _ in range(2):
            app.processEvents()
    s_mid = ov._dual_src_wrap.height()
    assert s_mid >= s0 + 60, f"拖动中原文区应跟手变大：{s0} -> {s_mid}"
    send_mouse(ov._dual_sep, QEvent.Type.MouseButtonRelease,
               QPoint(g.x(), g.y() + 90), Qt.LeftButton)
    for _ in range(4):
        app.processEvents()
    assert ov._dual_split_drag is False, "release 应收口"
    # v2.18.1：把手本体也必须退出拖拽态——旧实现只清 _dual_split_drag 标志、
    # 漏了 set_drag(False)，中央胶囊自此**永久高亮**挂在面板上（用户截图里
    # 那条"又粗又亮的分割线"就是这个）。
    assert ov._dual_sep._drag is False, "松手后把手本体拖拽态必须复位（胶囊不得常驻）"
    assert ov._dual_src_h_user == ov._dual_src_wrap.height(), \
        f"松手记录的用户高度应与实际一致：{ov._dual_src_h_user} vs {ov._dual_src_wrap.height()}"
    ov.deleteLater()
check("panel: 分割线真实事件流拖拽（事件过滤器）", t_overlay_dual_split_real_drag)


def t_overlay_split_follows_mouse():
    """v2.19.0 立、v2.20.0 收敛：分割线必须**跟手**——用户实拍"我往上拉的时候他就往下，反之亦然"。

    旧几何（hist=总高−工具条−body、body=贴内容）下，线的位置算出来等于
    `total − sep − 译文内容高`，**与用户拖的原文高度无关**，只随译文跳。
    真实事件流实测（面板 619×515 + 历史区 302px + src_h_user=87）：
      鼠标 −60px → 线 y **+28px（反向）**；关原文时 ±60/120px → **0 位移**。
    v2.20.0：历史区已删——dual 只剩一种几何（正文区吃满工具条以外的全部高度），
    本锁从"两态都测"收敛为单态。用户实拍的另一条指认也在此锁住：**上下双语
    必须是这块可拖分割线所在的大字分栏**，绝不能再长得和列表历史一样。"""
    from app.ui.caption_overlay import CaptionOverlay
    from PySide6.QtCore import Qt, QPoint, QPointF, QEvent
    from PySide6.QtGui import QMouseEvent

    def drag(ov, dy_total, step=10):
        def send(target, gtype, gpos, button=None):
            local = target.mapFromGlobal(gpos)
            ev = QMouseEvent(QEvent.Type(gtype), QPointF(local), QPointF(gpos),
                             button or Qt.NoButton, button or Qt.NoButton, Qt.NoModifier)
            QApplication.sendEvent(target, ev)
            for _ in range(3):
                app.processEvents()
        sep = ov._dual_sep
        start = sep.mapToGlobal(QPoint(sep.width() // 2, max(2, sep.height() // 2)))
        send(sep, QEvent.Type.MouseButtonPress, start, Qt.LeftButton)
        y = start.y()
        for _ in range(abs(dy_total) // step):
            y += step if dy_total > 0 else -step
            send(sep, QEvent.Type.MouseMove, QPoint(start.x(), y), Qt.LeftButton)
        send(sep, QEvent.Type.MouseButtonRelease, QPoint(start.x(), y), Qt.LeftButton)
        return start.y(), y

    ov = CaptionOverlay()
    ov.show()
    ov.set_layout_mode("dual")
    ov._dual_show_pending("The only control that AI needs is strong and smart.")
    ov._dual_spec("The only control that AI needs is strong and smart.",
                  "AI 需要的控制是坚固而聪明", True)
    ov.set_user_height(515)
    for _ in range(6):
        app.processEvents()
    # 上下双语的**定义**：正文大字区分栏可见，且没有列表行系统
    assert ov._dual_body.isVisible(), "上下双语必须显示原文/译文分栏正文区"
    assert not ov._scroll.isVisible(), "上下双语不得回落到列表行系统"
    assert ov._dual_sep.isVisible(), "上下双语必须有原文/译文分割线"
    base_y = ov._dual_sep.mapToGlobal(QPoint(0, 0)).y()

    # ① 先**往下**拖 90：线必须下移、原文区变大（证明控制件没死）
    s0 = ov._dual_src_wrap.height()
    drag(ov, 90)
    y1 = ov._dual_sep.mapToGlobal(QPoint(0, 0)).y()
    s1 = ov._dual_src_wrap.height()
    assert y1 > base_y, \
        f"往下拖线不动/反向（旧几何实测 0 位移）：{base_y} -> {y1}"
    assert s1 >= s0 + 60, f"原文区未跟手变大：{s0} -> {s1}"

    # ② 再**往上**拖 120：线必须上移——绝不允许反向（用户实拍正是这一条）
    drag(ov, -120)
    y2 = ov._dual_sep.mapToGlobal(QPoint(0, 0)).y()
    s2 = ov._dual_src_wrap.height()
    assert y2 < y1, f"往上拖线却下移（反向）：{y1} -> {y2}"
    assert s2 < s1, f"往上拖原文区未变小：{s1} -> {s2}"

    # ③ 可拖区间不得塌缩（旧几何下 body 塌到 46px 地板 → 钳制区间宽度为 0）
    body = ov._dual_body.height()
    assert body >= 92, f"正文区塌到 {body}px，分割线将拖不动"
    assert ov._dual_src_h_user == ov._dual_src_wrap.height(), \
        f"松手记录值与实际高度不一致 {ov._dual_src_h_user} vs {ov._dual_src_wrap.height()}"
    ov.deleteLater()
check("panel: 分割线跟手（上下拖方向正确、可拖区间不塌缩）",
      t_overlay_split_follows_mouse)


def t_overlay_dual_form_and_rows():
    """v2.20.0 立、v2.20.1 改判：上下双语的**长相**与**逐句累积**。

    两轮实拍裁决叠在同一段代码上：
    ① v2.20.0——历史区与滚动字幕墙两态删除（`overlay_dual_hist` 键、
       `set_hist_enabled`/`dual_push_history` 入口、DEFAULTS 键全都不复存在），
       dual 不得再回落到列表行系统；
    ② v2.20.1——两栏改**逐句累积**（用户："原文和译文都可以不断累积，都能滚动
       查看，但不要再加历史区、不要滚动条"）。所以上一版"只留最后一句"的断言
       作废，本锁改钉：三句 → 两栏各三条、文本一一对应、两栏滚动条策略一律
       AlwaysOff；并继续钉住「清空」判据与 ⋯ 菜单同态（v2.19.2 的两入口打架）。"""
    from PySide6.QtCore import Qt
    from app.config import DEFAULTS
    from app.ui.caption_overlay import CaptionOverlay
    assert "overlay_dual_hist" not in DEFAULTS, "历史区开关键应已随布局改版删除"
    assert "overlay_dual_hist_h" not in DEFAULTS, "历史区高度键应已随布局改版删除"
    assert not hasattr(CaptionOverlay, "set_hist_enabled"), "历史区开关入口应已移除"
    assert not hasattr(CaptionOverlay, "dual_push_history"), "沉历史入口应已移除"

    ov = CaptionOverlay()
    ov.show()
    ov.set_layout_mode("dual")
    for _ in range(4):
        app.processEvents()
    assert ov._dual_body.isVisible(), "dual 必须显示原文/译文大字分栏正文区"
    assert not ov._scroll.isVisible(), "dual 不得露出列表行滚动区（用户指认的病）"
    for i in range(3):
        ov.show_pending(f"Sentence number {i} arrives live")
        ov.update_partial(f"Sentence number {i} arrives live on screen")
        ov.update_dual_draft_tgt(f"第{i}句实时草稿")
        ov.show_pending_result(f"Sentence number {i} arrives live on screen.",
                               f"第{i}句完整译文。", True)
    for _ in range(6):
        app.processEvents()
    assert ov._rows == [], f"dual 不得建列表行（历史区已删）：{len(ov._rows)} 行"
    assert [it["text"] for it in ov._dual_src_items] == [
        "Sentence number %d arrives live on screen." % i for i in range(3)], \
        [it["text"] for it in ov._dual_src_items]
    assert [it["lab"].text() for it in ov._dual_tgt_items] == [
        "第%d句完整译文。" % i for i in range(3)], \
        [it["lab"].text() for it in ov._dual_tgt_items]
    assert ov._dual_src.text() == "Sentence number 2 arrives live on screen."
    # 两栏一律不显示滚动条（用户："不要搞滚动条"）
    for wrap in (ov._dual_src_wrap, ov._dual_tgt_wrap):
        assert wrap.verticalScrollBarPolicy() == Qt.ScrollBarAlwaysOff, \
            "双语两栏不得显示滚动条"
    # 「清空」判据与 ⋯ 菜单同态
    assert ov._clear_btn.isEnabled(), "dual 有内容时「清空」必须可用"
    m = ov._build_menu()
    assert ov._menu_acts["clear"].isEnabled(), "菜单「清空面板字幕」应与工具条同态"
    assert ov._menu_acts["clear"].text() == "清空面板字幕"
    m.deleteLater()
    ov.clear_caption()
    assert not ov._clear_btn.isEnabled(), "dual 清空后按钮应回灰"
    ov.deleteLater()

    # 对照组：list 布局三句全驻留——"句子不能消失"由这条路径承担
    ov2 = CaptionOverlay()
    ov2.show()
    for i in range(3):
        ov2.show_pending(f"Sentence number {i} arrives live on screen")
        ov2.show_pending_result(f"Sentence number {i} arrives live on screen.",
                                f"第{i}句完整译文。", True)
    for _ in range(6):
        app.processEvents()
    assert len(ov2._rows) == 3, f"列表布局必须驻留全部三句：{len(ov2._rows)}"
    ov2.deleteLater()
check("panel: 上下双语=大字分栏 + 两栏逐句累积（历史区/幕墙已删）",
      t_overlay_dual_form_and_rows)


def t_overlay_dual_late_final_targets_own_row():
    """v2.20.1：累积模式下**迟到的终版必须写回它自己那一行**。

    流式预览天生比正式识别快，真机时序就是"B 的草稿先把下一行开出来、A 的终版
    随后才到"。若终版无脑写最新一行，就成了"B 的原文配 A 的译文"——v2.19.1 花
    一整轮消灭的错配窗口，会在累积模式下以更糟的形式复发（错的那一行还会永久
    留在屏上）。本锁按该时序驱动，**旧写法（只认最新行）必红**。"""
    from app.ui.caption_overlay import CaptionOverlay
    ov = CaptionOverlay()
    ov.set_layout_mode("dual")
    ov.show()
    for _ in range(4):
        app.processEvents()
    A = "The president opened the session with a short statement"
    B = "Markets reacted quickly through the afternoon"
    ov.show_pending(A)
    ov.update_partial(A)
    ov.update_dual_draft_tgt("总统以简短声明开幕")
    assert len(ov._dual_src_items) == 1
    # B 的流式草稿先到 → 另起一行
    ov.update_partial(B)
    assert len(ov._dual_src_items) == 2, f"B 草稿应另起一行：{len(ov._dual_src_items)}"
    # A 的终版迟到 → 必须落在第一行，B 行原样不动
    ov.show_pending_result(A + ".", "总统以一段简短声明开幕。", True)
    assert ov._dual_src_items[0]["text"] == A + ".", \
        f"A 的原文行没被校准：{ov._dual_src_items[0]['text']!r}"
    assert ov._dual_tgt_items[0]["lab"].text() == "总统以一段简短声明开幕。"
    assert ov._dual_src_items[1]["text"] == B, \
        f"B 行被迟到的 A 终版覆盖：{ov._dual_src_items[1]['text']!r}"
    assert ov._dual_tgt_items[1]["lab"].text() == "⟳ 翻译中…", \
        f"B 行译文被 A 的终版顶掉：{ov._dual_tgt_items[1]['lab'].text()!r}"
    assert ov._dual_rows_closed == [True, False], ov._dual_rows_closed
    # B 自己收口后两行都已闭合
    ov.show_pending_result(B + ".", "整个下午市场反应迅速。", True)
    assert ov._dual_rows_closed == [True, True], ov._dual_rows_closed
    assert ov._dual_tgt_items[1]["lab"].text() == "整个下午市场反应迅速。"
    ov.deleteLater()
check("panel: dual 迟到的终版写回自己那一行（累积模式错配防线）",
      t_overlay_dual_late_final_targets_own_row)


def t_dual_pair_atomic_swap():
    """v2.19.1：关攒句实测反馈"一旦有新的翻译和识别，已翻译的译文和原文就消失了"
    （探针 scripts/qa/dual_disappear_probe.py 真实事件流取证）。当时的裁决是维持
    当前句独占，并修两条衔接缺陷——
    ① 错配窗口：上一句终版收口后，新句流式拍只覆盖原文区，译文区仍挂着**上一句
      的终版**（B 的原文配 A 的译文）1~2 拍，随后被草稿译冲掉；
    ② 闪白：同句的正式片段晚于流式草稿到达时，被"大写开头=新句"误判整句重置，
      把该句已在生长的推测译打回 "…"，译文再长一遍。
    本锁在**旧实现上必红**（可证伪），驱动面板公开入口复现真机拍序。
    v2.20.1：独占改为两栏逐句累积后，这三条衔接契约**照旧成立**，只是作用对象
    从"那唯一的一对标签"变成"最新一行"——上一句不再消失，而是留在上面那一行。"""
    from app.ui.caption_overlay import CaptionOverlay
    ov = CaptionOverlay()
    ov.show()
    ov.set_layout_mode("dual")
    A_src = "Hello everyone and welcome to the show."
    A_tgt = "大家好，欢迎收看本期节目。"

    # ── 句1：草稿生长 → 终版收口 ─────────────────────────────
    ov.show_pending("Hello everyone")
    ov.update_partial("Hello everyone and welcome")
    ov.update_dual_draft_tgt("大家好，欢迎")
    ov.show_pending_result(A_src, A_tgt, True)
    app.processEvents()
    assert ov._dual_tgt.text() == A_tgt and not bool(ov._dual_tgt.property("spec")), \
        "前置条件：句1 终版必须已定格"

    # ① 错配锁：新句首拍到来 = **原子换句**（原文译文同刻切），
    #    绝不允许 "B 原文 + A 终版译文" 的中间态存在
    B_draft = "Today we talk about AI"
    ov.update_partial(B_draft)
    app.processEvents()
    assert ov._dual_src.text() == B_draft, "新句原文必须上屏（独占语义保留）"
    assert ov._dual_tgt.text() != A_tgt, \
        f"错配窗口复现：上一句终版译文仍挂在屏上（旧行为） tgt={ov._dual_tgt.text()!r}"
    assert bool(ov._dual_tgt.property("spec")), "换句后译文区必须回到占位/推测态"

    # ② 闪白锁：同句草稿已在屏 + 正式片段（大写开头、与草稿同源）到达
    #    → 就地校准原文，**译文区不得被打回 "…"**
    ov.update_dual_draft_tgt("今天我们聊 AI")
    B_final = "Today we talk about AI and robotics."
    B_tgt_draft = "今天我们聊 AI"
    assert ov._dual_tgt.text() == B_tgt_draft
    ov.show_pending(B_final)
    app.processEvents()
    assert ov._dual_src.text() == B_final, "正式片段应校准为完整句"
    assert ov._dual_tgt.text() == B_tgt_draft, \
        f"闪白复现：同句正式片段把在生长的推测译重置了 tgt={ov._dual_tgt.text()!r}"

    # 句2 收口 → 闭合
    B_tgt = "今天我们聊聊 AI 与机器人。"
    ov.show_pending_result(B_final, B_tgt, True)
    app.processEvents()

    # ③ 迟到草稿防御：句2 已终版，其更早拍的草稿回复迟到 → 不得冲淡定稿
    ov.update_dual_draft_tgt("迟到的半句草稿", B_final)
    assert ov._dual_tgt.text() == B_tgt and not bool(ov._dual_tgt.property("spec")), \
        "迟到草稿回复不得把已收口译文刷回淡色推测态"

    # ④ 尾重复延伸防御：终版句后 whisper 把同句再转一遍（前缀同源更长版）
    #    → 只长文本，译文终版保持（不得误判新句重置）
    ov.update_partial(B_final + " and more")
    assert ov._dual_src.text() == B_final + " and more"
    assert ov._dual_tgt.text() == B_tgt, "同句尾重复不得重置译文区"

    # ⑤ CPU 节奏（无流式拍）：终版 → 下一片段直接到达 = 同刻成对切换，无错配帧
    ov.show_pending("Tomorrow the summit begins.")
    app.processEvents()
    assert ov._dual_src.text() == "Tomorrow the summit begins."
    assert ov._dual_tgt.text() != B_tgt, \
        "CPU 路径同样禁止 '新句原文 + 旧句终版' 的错配帧"
    ov.deleteLater()
check("panel: dual 原子换句（无错配窗口 + 同句片段不闪白 + 迟到草稿不冲淡终版）",
      t_dual_pair_atomic_swap)



def t_overlay_single_divider():
    """v2.18.1 立、v2.20.0 随布局改版收敛：面板上「可见横线」必须恰好一条
    = 原文/译文那条可拖分割线。

    用户截图实证的多条线来源（逐轮处理）：
    ① `QScrollArea#PanelDualHist { border-bottom }` —— 历史区底缘装饰线，
       不可拖、纯视觉噪声 → 先删线，v2.20.0 连历史区本体一并删除；
    ② `_DualSepHandle` 自绘细线 + 中央胶囊 —— 唯一保留（可拖）；
    ③ 拖完 `set_drag` 未复位 → 胶囊永久高亮（看起来像第三条粗线）→ 已修；
    ④ 「原文 关」时 sep 未被重算可见性 → 没有两栏要分却仍挂着线 → 已修；
    ⑤ dual→list 切换残留空白块 → 历史区删除后从根上不存在，改断言正文区收起。
    这里用**像素扫描**客观数线，不靠眼睛，也不断言内部状态。"""
    from app.ui.caption_overlay import CaptionOverlay
    from PySide6.QtCore import QPoint

    ov = CaptionOverlay()
    ov.set_show_source(True)
    ov.set_layout_mode("dual")
    ov.resize(700, 420)
    ov.move(60, 60)
    ov.show()
    for _ in range(6):
        app.processEvents()
    ov._dual_show_pending("The quick brown fox jumps over the lazy dog")
    ov._dual_show_result("The quick brown fox jumps over the lazy dog",
                         "敏捷的棕色狐狸跳过了懒狗", True)
    for _ in range(12):
        app.processEvents()

    def divider_rows():
        """返回整幅横贯的分割线所在 y（相邻行归并）。三条判据同时成立：
        ① 亮像素占比 > 0.9  ② 亮样本亮度均匀（线是一次 fillRect 画出来的，
        文字行是笔画与空隙混排）  ③ 上下 2px 明显变暗（1~2px 孤立薄行）。
        实测标定（面板 700x544、底色基准 b0=89）：单行原文 y=435/447 占比
        0.66、亮样本极差 404；sep 线 y=479 占比 0.95、极差 0、上下 2px 0.00。
        只用占比阈值会把文字判成线，必须再加均匀度与薄行两条。
        扫描区间从正文区顶部起，避开工具条高亮带。"""
        img = ov.grab().toImage()
        W, H = img.width(), img.height()
        col = []
        for yy in range(H):
            pt = img.pixel(2, yy)
            col.append(((pt >> 16) & 255) + ((pt >> 8) & 255) + (pt & 255))
        b0 = sorted(col)[len(col) // 2]

        def lum(x, y):
            if not (0 <= y < H):
                return 0
            pt = img.pixel(x, y)
            return ((pt >> 16) & 255) + ((pt >> 8) & 255) + (pt & 255)

        def profile(y):
            if not (0 <= y < H):
                return 0.0, 999
            vals = [lum(x, y) - b0 for x in range(6, W - 6, 4)]
            if not vals:
                return 0.0, 999
            lit = [v for v in vals if v > 24]
            spread = (max(lit) - min(lit)) if lit else 999
            return len(lit) / len(vals), spread

        def frac(y):
            return profile(y)[0]

        ys = []
        for y in range(max(0, ov._dual_body.mapTo(ov, QPoint(0, 0)).y()), H):
            f, spread = profile(y)
            if (f > 0.9 and spread <= 40
                    and frac(y - 2) < 0.5 and frac(y + 2) < 0.5):
                if not ys or y - ys[-1] > 3:
                    ys.append(y)
        return ys

    rows = divider_rows()
    assert len(rows) == 1, f"面板应恰好一条可见分割线，实测 {len(rows)} 条 @ y={rows}"
    sep_y = ov._dual_sep.mapTo(ov, QPoint(0, ov._dual_sep.height() // 2)).y()
    assert abs(rows[0] - sep_y) <= 4, f"唯一那条线必须落在可拖把手上：线 y={rows[0]}，sep y={sep_y}"

    # 关原文（用户当前状态）：没有两栏要分 → 面板上不得再有任何可见线
    ov.set_show_source(False)
    for _ in range(12):
        app.processEvents()
    assert not ov._dual_sep.isVisible(), "关原文后把手应隐藏"
    assert divider_rows() == [], f"关原文后仍检出可见线: {divider_rows()}"

    # 切回列表布局：dual 正文区必须整体收起（v2.20.0：它已是 dual 唯一的正文）
    ov.set_show_source(True)
    ov.set_layout_mode("list")
    for _ in range(12):
        app.processEvents()
    assert not ov._dual_body.isVisible(), "dual→list 后正文区残留（列表布局里多一块空白区）"
    ov.deleteLater()
check("panel: 面板恰好一条分割线（v2.18.1 多条线回归）", t_overlay_single_divider)


def t_overlay_body_background_pixels():
    """v2.19.2：面板正文区必须真的带上底色，不许整片透出桌面。

    用户实拍：工具条以下全是壁纸。根因不在面板自己——`paintEvent` 每帧都以
    完整脏矩形画了圆角底色，但 **QScrollArea 的内容控件**（`_body` /
    `_dual_src` / `_dual_tgt`）在 `setWidgetResizable(True)`
    之下被 Qt 于 `setWidget()` 内部（C++ 侧，Python 层追不到这次调用）打开了
    `autoFillBackground`；叠加 `WA_TranslucentBackground` 后它每帧把自己整块
    矩形擦成 alpha 0，连父层刚画好的底色一起抹掉。

    真机像素实测（面板压在壁纸上）：修复前 body=壁纸蓝 (15,157,250)、100%
    像素偏离底色；只关 autoFill 立刻回到 (28,31,38) 且 std=0；单独关
    translucent 或只 repaint() 均无效——所以 v2.19.1 那句
    `resizeEvent → update()` 并没有修到这一层。offscreen 的 `grab()` alpha
    通道可确定性复现同一件事（修复后 alpha 全 255；把 autoFill 改回 True
    则 100% 为 0），故本锁**在旧实现上必红**。
    顺带锁量纲：`int(100 * 2.55)` 在浮点下是 254，"100% 不透明"常年漏
    1/255 的桌面进来，现在 100 档映射到 255。
    """
    from PySide6.QtGui import QImage

    def alpha_scan(ov):
        img = ov.grab().toImage().convertToFormat(QImage.Format_ARGB32)
        w, h = img.width(), img.height()
        mn, zero, total = 255, 0, 0
        for y in range(60, max(61, h - 20), 4):
            for x in range(30, max(31, w - 30), 4):
                a = (img.pixel(x, y) >> 24) & 0xFF
                total += 1
                if a < mn:
                    mn = a
                if a == 0:
                    zero += 1
        return mn, (zero / max(1, total))

    for mode in ("list", "dual"):
        ov = CaptionOverlay()
        ov.apply_style(22, "#ffffff", "#1c1f26", 100)
        ov.set_show_source(False)
        ov.set_user_height(515)
        ov.resize(619, 515)
        ov.set_layout_mode(mode)
        ov.show()
        for _ in range(8):
            app.processEvents()
        tag = mode
        assert ov._bg_alpha == 255, "%s：100%% 不透明应映射为 alpha 255，实得 %d" % (
            tag, ov._bg_alpha)
        for wname in ("_body", "_dual_src_body", "_dual_tgt_body"):
            assert getattr(ov, wname).autoFillBackground() is False, (
                "%s：%s 的 autoFillBackground 被打开——它会把父层底色擦成 alpha 0" % (
                    tag, wname))
        mn, frac0 = alpha_scan(ov)
        assert mn >= 250 and frac0 < 0.01, (
            "%s：正文区透出桌面（最小 alpha=%d，零 alpha 占比 %.1f%%）" % (
                tag, mn, frac0 * 100))
        ov.deleteLater()
        for _ in range(4):
            app.processEvents()


check("panel: 正文区必须带上底色不透出桌面（v2.19.2 autoFill 擦除回归）",
      t_overlay_body_background_pixels)


def t_overlay_dual_builds_no_widgets():
    """v2.20.1：dual 逐句累积的**两条边界**（旧瞬窗锁的换代）。

    历史区删除后 dual 一度"零新建控件"；本轮改两栏逐句累积，每句要新建
    1 原文标签 + 1 译文卡片 + 1 卡片内标签 = 3 个控件——于是 v2.6.6 那条
    "每来一句闪一个 40ms 无题小窗"的病根（无父构造 + 先 setVisible 后收编）
    **重新变成风险**，本锁 ① 用 setVisible 探针钉死"任何控件都不得成为顶层
    窗口"（新卡片一律"构造即传父"）。② 钉住增长有界：每句 ≤3 控件、总数被
    MAX_DUAL_LINES 钳住（跑 3 倍上限的句数也不许继续长）、两栏条目数始终成对。
    ③ 钉住被删条目的控件从分割线观察名单里摘掉（否则无限增长 + 对已 delete
    的对象再发事件）。"""
    from PySide6.QtWidgets import QWidget as _QW
    from app.ui.caption_overlay import CaptionOverlay
    hits = []
    real = _QW.setVisible

    def spy(self, on):
        r = real(self, on)
        if on and self.isWindow():
            hits.append(self.objectName() or self.metaObject().className())
        return r

    ov = CaptionOverlay()
    ov.set_layout_mode("dual")
    ov.show()
    for _ in range(6):
        app.processEvents()
    n0 = len(ov.findChildren(_QW))
    _QW.setVisible = spy
    n = ov.MAX_DUAL_LINES * 3
    try:
        for i in range(n):
            ov.show_pending(f"Accumulated sentence number {i} starts here")
            ov.show_pending_result(f"Accumulated sentence number {i} starts here.",
                                   f"累积第{i}句译文。", True)
    finally:
        _QW.setVisible = real
    app.processEvents()
    assert not hits, f"面板控件成了顶层窗口（瞬窗复发）：{hits}"
    assert len(ov._dual_src_items) == ov.MAX_DUAL_LINES, \
        f"原文栏未被上限钳住：{len(ov._dual_src_items)}"
    assert len(ov._dual_tgt_items) == len(ov._dual_src_items), "两栏条目数必须成对"
    n1 = len(ov.findChildren(_QW))
    assert n1 - n0 <= ov.MAX_DUAL_LINES * 3, \
        f"控件总数失控（应 ≤ 上限×3）：{n0} -> {n1}"
    # 最老的一句已被挤掉、最新一句在屏（累积是"滚动窗口"不是无限堆积）
    texts = [it["lab"].text() for it in ov._dual_src_items]
    assert texts[-1].startswith("Accumulated sentence number %d" % (n - 1)), texts[-1]
    assert not any("number 0 " in t for t in texts), "最老一句应已被上限淘汰"
    assert len(ov._dual_split_watch) <= n1 + 16, \
        f"分割线观察名单泄漏（未随条目删除摘除）：{len(ov._dual_split_watch)} vs {n1}"
    ov.deleteLater()


check("panel: dual 累积有界且逐句控件不逃逸成顶层窗口（瞬窗锁换代）",
      t_overlay_dual_builds_no_widgets)


def t_overlay_dual_clear_state():
    """v2.19.2 立、v2.20.0 随布局改版收敛：dual 态的「清空」必须三件事一起归零。

    ① `_last_result` 不清 → 面板已空白，但「复制最近一句 / 纠正最近识别 /
       纠正译文」仍指向被清掉的句子（列表分支无此问题）；
    ② 引导小抄 `_hint_guide` 只在 `_add_row`（列表路径）复位，dual 永不复位
       → 每次清空后三行小抄重弹，违反 v2.4.4（BUG-7）"每份配置只弹一次"；
    ③ ⋯ 菜单「清空面板字幕」的可用态只看 `_rows`，dual 正文在当前句大字区
       → 恒灰，而工具条「清空」同一动作可用，两入口打架。
    旧版本在③还多查一项"历史行数"，历史区删除后判据只剩当前句原文。"""
    ov = CaptionOverlay()
    ov.set_layout_mode("dual")
    ov.show_first_hint()                       # 置引导小抄
    ov.show()
    for _ in range(6):
        app.processEvents()
    assert ov._hint_guide is True, "前置条件：引导小抄已置位"
    ov._dual_show_result("Hello dual layout", "上下双语一句", True)
    for _ in range(4):
        app.processEvents()
    assert ov._hint_guide is False, "真实字幕上屏后引导小抄必须复位（只弹一次）"
    ov._build_menu()
    acts = ov._menu_acts
    assert acts["clear"].isEnabled(), \
        "dual 有当前句时菜单「清空」必须可用（与工具条同一判据）"
    assert acts["copy"].isEnabled(), "有最近一句时「复制」应可用"
    ov.clear_caption()
    for _ in range(4):
        app.processEvents()
    assert ov._last_result == ("", ""), \
        f"清空后仍留着最近一句＝复制/纠正菜单会指向已删内容：{ov._last_result}"
    ov._build_menu()
    acts2 = ov._menu_acts
    assert not acts2["copy"].isEnabled(), "清空后「复制最近一句」必须置灰"
    assert not acts2["clear"].isEnabled(), "清空后「清空面板字幕」必须置灰"
    ov.deleteLater()


check("panel: dual 清空后状态归零（v2.19.2 最近一句/小抄/菜单态）",
      t_overlay_dual_clear_state)


def t_settings_syncs_panel_side_changes():
    """v2.19.2 立、v2.20.0 换项：面板侧改配置键 → **已打开**的设置页控件必须跟随。

    旧状只有 `overlay_enabled`（sync_overlay_check）与 `target_lang`
    （sync_target_lang，v2.18.1 补）有窄同步，字号/透明度/布局/历史区四项漏网：
    面板 ⋯ 菜单切完，设置页控件仍显旧值；用户把控件拨到"屏幕上的实际值"时被
    `_stage` 判成"改回原值"而静默吞掉——显示改了，底部仍提示"所有改动已保存"。
    同时锁住暂存优先规则：用户已暂存该键时不得被面板值覆盖。
    v2.20.0：历史区勾选随功能退役换成本轮新增的「攒句合并」（面板 ⋯ 菜单 ↔
    设置页 `grouping_check` ↔ 配置 `translate_grouping` 三处必须一致）。"""
    from app.ui.settings_dialog import SettingsDialog
    w = MainWindow()
    w._open_settings()                       # 真实入口：对话框登记到 _settings_dlg
    dlg = w._settings_dlg
    assert isinstance(dlg, SettingsDialog) and dlg is not None
    try:
        w._on_panel_opacity(60)
        assert dlg.bg_opacity_slider.value() == 60, "面板改透明度后设置页滑条未跟随"
        w._on_panel_font_size(30)
        assert dlg.overlay_font_spin.value() == 30, "面板改字号后设置页数字框未跟随"
        w._on_panel_layout_changed("dual")
        assert dlg.layout_combo.currentData() == "dual", "面板切布局后设置页下拉未跟随"
        assert w.config.get("overlay_layout") == "dual", "面板切布局必须落盘"
        w._on_panel_grouping_toggled(False)
        assert dlg.grouping_check.isChecked() is False, "面板切攒句后设置页勾选未跟随"
        assert w.config.get("translate_grouping") is False, "面板切攒句必须落盘"
        assert w.overlay.is_grouping_enabled() is False, "面板自身勾选态必须同步"
        w._on_panel_grouping_toggled(True)
        assert dlg.grouping_check.isChecked() is True, "反向切换同样要跟回设置页"
        # 暂存优先：按真实用户流——拨滑条即产生暂存值，面板侧改动不得覆盖它
        dlg.bg_opacity_slider.setValue(80)
        assert dlg._staged.get("overlay_bg_opacity") == 80, "前置：拨滑条应已暂存"
        w._on_panel_opacity(50)
        assert dlg.bg_opacity_slider.value() == 80, "覆盖了用户尚未保存的暂存值"
        assert dlg._staged == {"overlay_bg_opacity": 80}, (
            f"面板侧改动不得写进暂存区，也不得留下别的待保存项：{dlg._staged}")
    finally:
        dlg.deleteLater()
        w.deleteLater()


check("settings: 面板侧改配置键同步已打开的设置页（v2.19.2 窄同步 + v2.20.0 攒句）",
      t_settings_syncs_panel_side_changes)


def t_panel_menu_grouping_roundtrip():
    """v2.20.0：面板 ⋯ 菜单的「攒句合并」点击后必须 落盘 + 回写勾选 + 通知主窗。

    菜单是每次 `_build_menu()` 现建的，勾选初值只能来自 `self._grouping`，
    所以"设置页改了面板菜单还显旧值"这条回路必须由主窗下发（`apply_overlay_from_config`
    里的 `set_grouping_enabled`）——本锁把两向都钉住：菜单点一下 ≡ 设置页勾一下。"""
    w = MainWindow()
    old_g = w.config.get("translate_grouping")
    try:
        w.config.set("translate_grouping", True)
        w.apply_overlay_from_config()
        assert w.overlay.is_grouping_enabled() is True, "配置必须下发到面板勾选态"
        ov = w.overlay
        orig = ov._on_grouping_toggled
        calls = []
        ov._on_grouping_toggled = lambda on: (calls.append(on),
                                              orig(on) if orig else None)
        menu = ov._build_menu()
        grouping_act = ov._menu_acts["grouping"]
        assert grouping_act.isChecked() is True, "菜单勾选初值须来自配置"
        assert grouping_act.isCheckable()
        # 真实路径：菜单 exec 返回被点的 action，由 _menu_dispatch 分派
        ov._menu_dispatch(grouping_act)
        app.processEvents()
        assert calls == [False], f"点菜单应回调主窗一次、值为取反后的 False：{calls}"
        assert w.config.get("translate_grouping") is False, "必须落盘"
        assert ov.is_grouping_enabled() is False
        menu2 = ov._build_menu()
        assert ov._menu_acts["grouping"].isChecked() is False, "重开菜单须显新状态"
        ov._menu_dispatch(ov._menu_acts["grouping"])   # 再点回去
        assert w.config.get("translate_grouping") is True
        menu.deleteLater()
        menu2.deleteLater()
        ov._on_grouping_toggled = orig
        # 反方向：设置页勾选 → 保存 → 面板菜单镜像必须跟随（两入口不得漂移）。
        # 保存路径只重放 overlay_* 与 show_source，v2.20.0 起 translate_grouping
        # 也在其列——漏掉它时这里必红。
        w._open_settings()
        dlg = w._settings_dlg
        try:
            dlg.grouping_check.setChecked(False)
            dlg._stage("translate_grouping", False)
            dlg._apply_staged()
            assert w.config.get("translate_grouping") is False, "设置页保存必须落盘"
            assert ov.is_grouping_enabled() is False, \
                "设置页保存后面板 ⋯ 菜单镜像仍停在旧值（两入口漂移）"
        finally:
            dlg.deleteLater()
    finally:
        # 共享配置 home：本锁改了 translate_grouping，必须还原，
        # 否则后面所有依赖"攒句开"的锁全部误报（实测一次带走 6 条）
        w.config.set("translate_grouping", old_g)
        w.deleteLater()


check("panel: ⋯ 菜单攒句开关双向同步（点一下＝配置+菜单+主窗一致）",
      t_panel_menu_grouping_roundtrip)


def t_overlay_dual_no_repeat_after_final():
    """v2.20.0（换代 v2.19.3 的幕墙锁）：dual 收口后同句流式拍不得复读。

    真机 60s 英语新闻实测（离线 Argos + GPU turbo + 攒句开）：幕墙里同一句
    原文+译文**逐字重复驻留两行**，其后还跟一个只剩 "inflation." 的碎片行。
    链路：末行被终版收口（pending=False）→ 下一拍流式草稿仍是"基线整句 + 少量
    新词"（主窗剥离失手）→ `update_partial` 见末行非待决就整句 `_add_row`。
    主窗侧的修法（规范化前缀相等快判）保留在单元锁
    `test_strip_overlap_prefix_equal_small_tail`；面板侧那条兜底随幕墙一起删了，
    因为新形态天然免疫——原文区是**整块替换**，不存在"再开一行"这条路。
    本锁按新形态重钉契约。"""
    ov = CaptionOverlay()
    ov.apply_style(22, "#ffffff", "#1c1f26", 100)
    ov.set_show_source(True)
    ov.set_layout_mode("dual")
    ov.show()
    for _ in range(6):
        app.processEvents()
    S1 = "Technology shares led the gain after a major chipmaker reported stronger demand"
    for d in (S1[:20], S1[:38], S1):
        ov.update_partial(d)
    ov.show_pending(S1)
    ov.show_pending_result(S1, "科技股上涨，芯片大厂需求走强。")
    for _ in range(4):
        app.processEvents()
    assert ov._rows == [], "dual 不得建行"
    final_tgt = ov._dual_tgt.text()
    assert not bool(ov._dual_tgt.property("spec")), "前置：终版译文已定格"
    # 下一拍草稿仍带着上一句（主窗剥离失手的形态）
    ov.update_partial(S1 + " and inflation eased")
    ov.update_partial(S1 + " and inflation eased this month")
    for _ in range(4):
        app.processEvents()
    txt = ov._dual_src.text()
    assert txt.count("Technology shares") == 1, f"同一句在原文区被复读：{txt!r}"
    assert txt == S1 + " and inflation eased this month", f"整块刷新丢失新词：{txt!r}"
    assert ov._dual_tgt.text() == final_tgt, "同句延伸不得把终版译文打回推测态"
    # 真正的新句才换句（原文+译文同刻切）
    ov.update_partial("Wall Street closed higher on Friday")
    app.processEvents()
    assert ov._dual_src.text() == "Wall Street closed higher on Friday"
    assert ov._dual_tgt.text() != final_tgt, "换句后仍挂着上一句终版译文＝错配帧"
    ov.deleteLater()


check("panel: dual 收口后同句草稿只整体刷新不复读（幕墙兜底换代）",
      t_overlay_dual_no_repeat_after_final)


def t_overlay_dual_preview_echo_no_dup_row():
    """v2.20.1：预览缓冲重启把已收口那句**重播**一遍时，不得并排出第二行。

    真机 91s 英语新闻实测（离线 Argos + GPU turbo + 攒句开）：原文栏里
    "Economists say … across the board. on the sports desk" 之后紧跟一行
    "inflation readings … on the sports desk"——同一句的两份文本并排，后一行
    还是半句。链路：末句收口 → 主窗预览缓冲重启 → 下一拍草稿从句子中段重新播，
    而 `_dual_same_sentence` 只认前缀与前 3 词，于是判成新句另起一行。
    契约两条：回声拍不开新行；也不许把收口行打回更短的半句。"""
    ov = CaptionOverlay()
    ov.apply_style(22, "#ffffff", "#1c1f26", 100)
    ov.set_show_source(True)
    ov.set_layout_mode("dual")
    ov.show()
    for _ in range(6):
        app.processEvents()
    S1 = ("Economists say lower inflation readings this month helped lift "
          "sentiment across the board. On the sports desk")
    ov.update_partial(S1[:30])
    ov.update_partial(S1)
    ov.show_pending_result(S1, "经济学家说本月通胀数据回落提振了市场情绪。")
    for _ in range(4):
        app.processEvents()
    rows = [i["text"] for i in ov._dual_src_items]
    assert len(rows) == 1, f"前置：应只有一行，实得 {rows}"
    # 干净回声：收口句的一段被原样重播（词序一致）
    echo = ("inflation readings this month helped lift sentiment across the "
            "board on the sports desk")
    ov.update_partial(echo)
    app.processEvents()
    rows = [i["text"] for i in ov._dual_src_items]
    assert len(rows) == 1, f"回声拍另起了一行：{rows}"
    assert rows[0] == S1, f"收口行被更短的回声打回半句：{rows[0]!r}"
    # v2.20.2 记录一条**已知不吸收**的形态：带结巴的回声（真机原文如此，
    # whisper 重播时多吐了 "on the sport"）。它在词级统计上与"只差一个数字的
    # 两句最小对"不可区分（0.82 vs 0.80），而阈值必须守住 0.85 才不吞真句子
    # ——所以这种回声仍会多出一行重复。判据偏向：**宁多一行重复，不丢一句真话**。
    ov.update_partial("inflation readings this month helped lift sentiment "
                      "across the board on the sport on the sports desk")
    app.processEvents()
    assert len(ov._dual_src_items) == 2, "结巴回声被吸收了＝阈值又松回去了"
    # 真正的新句照旧开新行（判据收紧不得把新内容也吞掉）
    ov.update_partial("The national team secured qualification with a late goal")
    app.processEvents()
    assert len(ov._dual_src_items) == 3, [i["text"] for i in ov._dual_src_items]
    ov.deleteLater()


check("panel: dual 预览重启的回声拍不再并排出重复行（v2.20.1 实测）",
      t_overlay_dual_preview_echo_no_dup_row)


def t_panel_run_toggle_button():
    """v2.20.1（用户点名）：面板工具条上要有「开始 / 停止翻译」的开关把手。

    与全局热键 Ctrl+Alt+S、托盘「开始 / 停止翻译」是同一个动作（主窗
    `toggle_running`），面板只转发不自主翻转——态一律由
    `update_overlay_status` 按 `self.running` 回灌，否则热键停了面板还显运行中。"""
    w = MainWindow()
    try:
        ov = w.overlay
        assert ov._run_btn.text() in ("⏸ 暂停", "▶ 开始"), ov._run_btn.text()
        assert ov._on_toggle_running == w.toggle_running, "把手必须接到主窗开关"
        calls = []
        orig = ov._on_toggle_running
        ov._on_toggle_running = lambda: calls.append(1)
        ov._run_btn.click()
        app.processEvents()
        assert calls == [1], f"点把手应转发主窗一次：{calls}"
        ov._on_toggle_running = orig
        # 态由主窗回灌：running=False → 文案转"开始"，True → "暂停"
        for flag, want in ((False, "▶ 开始"), (True, "⏸ 暂停")):
            w.running = flag
            w.update_overlay_status()
            assert ov._run_btn.text() == want, (flag, ov._run_btn.text())
        assert ov._run_btn.toolTip().find("全局热键") >= 0
    finally:
        w.deleteLater()


check("panel: 工具条「开始/停止翻译」把手接线与回灌（v2.20.1）",
      t_panel_run_toggle_button)


def _dual_panel():
    ov = CaptionOverlay()
    ov.apply_style(22, "#ffffff", "#1c1f26", 100)
    ov.set_show_source(True)
    ov.set_layout_mode("dual")
    ov.show()
    for _ in range(6):
        app.processEvents()
    return ov


def t_overlay_dual_paraphrase_kept_apart():
    """v2.20.2：回声判据必须**有序**，近义改写的两句不许并成一句。

    v2.20.1 的判据是"新拍的词 ≥85% 在旧行出现过"（词集合包含）。真机推演：
    "The president met with the prime minister in Warsaw" 与
    "The prime minister met with the president in Berlin" 词集几乎相同、词序完全
    不同 → 被判同一句 → 前一句从没在屏上出现过。丢句子比多一行重复难发现得多。
    现在判据换成有序 LCS 占比（分母取较短一句），回声仍命中、换序不命中。"""
    ov = _dual_panel()
    A = "The president met with the prime minister in Warsaw"
    B = "The prime minister met with the president in Berlin"
    ov.show_pending(A)
    ov.show_pending(B)
    texts = [i["text"] for i in ov._dual_src_items]
    assert len(texts) == 2, f"近义改写的两句被并成一行：{texts}"
    assert texts[0] == A, texts
    ov.show_pending_result(A, "总统在华沙会晤总理。")
    ov.show_pending_result(B, "总理在柏林会晤总统。")
    assert ov._dual_src_items[1]["text"] == B
    assert ov._dual_tgt_items[1]["lab"].text() == "总理在柏林会晤总统。"
    # 真回声（同一段被重播、词序一致）仍须被吸收，不许并排出两行
    C = ("Economists say lower inflation readings this month helped lift "
         "sentiment across the board. on the sports desk")
    ov.update_partial(C)
    ov.show_pending_result(C, "经济学家说本月通胀回落提振了情绪。")
    n = len(ov._dual_src_items)
    ov.update_partial("inflation readings this month helped lift sentiment "
                      "across the board on the sports desk")
    assert len(ov._dual_src_items) == n, "真回声没被吸收，屏上并排重复行"
    # 只差一个数字的"最小对"两句必须各自成行——套件里真实踩过：
    # "Sentence number 0 arrives live" / "…number 1 arrives live" 词级重合 0.80，
    # 阈值一旦放到 0.85 以下就会把第二句整个吞掉（丢句子比重复更难发现）。
    # 编号放在前三词之后，绕开 v2.19.1 的"前 3 词同源"快判，专打这条 LCS 阈值。
    ov.show_pending_result("The grid deal cleared item 7 of the checklist", "第七项通过。")
    ov.show_pending("The grid deal cleared item 8 of the checklist")
    texts = [i["text"] for i in ov._dual_src_items]
    assert texts[-1] == "The grid deal cleared item 8 of the checklist", \
        f"只差编号的两句被并成一行：{texts[-2:]}"
    ov.deleteLater()


check("panel: dual 回声判据有序化——近义两句不并、真回声仍吸收（v2.20.2）",
      t_overlay_dual_paraphrase_kept_apart)


def t_overlay_dual_closed_row_not_appended():
    """v2.20.2：收口行不许再被"延续片段"拼接，否则同文双行 + 半句配错译。

    真机复现（停/启压力测 + 离线复放）：`The meeting started` 已收口并配好译文，
    随后到的小写开头片段被 join 进这一行 → 该行文本变长而译文停在半句；整句
    终版再到达时 `_dual_row_for` 跳过收口行、另起一行 → 屏上两行原文完全相同，
    其中一行配错译且**永远不会被修复**。"""
    ov = _dual_panel()
    ov.show_pending_result("The meeting started", "会议开始了。")
    ov.show_pending("and then the chair spoke about the budget")
    full = "The meeting started and then the chair spoke about the budget"
    ov.show_pending_result(full, "然后主席谈了预算。")
    for _ in range(4):
        app.processEvents()
    texts = [i["text"] for i in ov._dual_src_items]
    assert len(set(texts)) == len(texts), f"两行原文一模一样：{texts}"
    assert texts[0] == "The meeting started", \
        f"收口行被拼接变长（译文仍停在半句）：{texts}"
    assert ov._dual_tgt_items[0]["lab"].text() == "会议开始了。"
    assert texts[-1] == full, texts
    assert ov._dual_rows_closed[-1] is True, "终版没把自己的行收口"
    ov.deleteLater()


check("panel: dual 收口行不被延续片段拼接（v2.20.2 同文双行）",
      t_overlay_dual_closed_row_not_appended)


def t_overlay_dual_hidden_no_zombie_row():
    """v2.20.2：面板隐藏期间不喂流式草稿，否则攒出一张永不收口的僵尸卡。

    终版/收口那条路（`show_pending_result`）本来就带可见性闸门，只有
    `update_partial` 没看 `isVisible()`——真机复现：隐藏期间喂 5 拍，得到
    **1 行 99 字符、未收口、译文恒为 "…"**，重新显示后用户看到一坨糊在一起的
    原文配一个省略号。"""
    ov = _dual_panel()
    base = "Economists say lower inflation readings this month helped lift sentiment"
    for i in range(5):
        ov.update_partial(base + " part%d" % i)
    app.processEvents()
    assert len(ov._dual_src_items) == 1, "前置：显示态应正常累积"
    ov.hide()
    for _ in range(4):
        app.processEvents()
    n = len(ov._dual_src_items)
    for i in range(5):
        ov.update_partial(base + " hidden%d" % i)
    app.processEvents()
    assert len(ov._dual_src_items) == n, "隐藏期间仍在写草稿"
    assert all(not t.endswith("hidden4") for t in
               [i["text"] for i in ov._dual_src_items])
    ov.show()
    for _ in range(4):
        app.processEvents()
    ov.update_partial("The national team secured qualification late")
    assert len(ov._dual_src_items) == n + 1, "重新显示后草稿照常"
    ov.deleteLater()


check("panel: dual 隐藏期不喂草稿，不再攒出僵尸未收口行（v2.20.2）",
      t_overlay_dual_hidden_no_zombie_row)


def t_panel_clear_empties_both_layouts():
    """v2.20.2：「清空」一次清掉**两种布局**的内容。

    真机停/启压力测实测：列表布局下点清空 → 列表行清了、dual 两栏的逐句条目
    原封不动（dual_src 仍为 3），切回上下双语就看见"清空之后还在"。
    旧实现按当前布局分支，两边各走各的。"""
    ov = _dual_panel()
    ov.show_pending_result("First sentence about the grid deal", "第一句。")
    ov.show_pending_result("Second sentence about the market", "第二句。")
    assert len(ov._dual_src_items) == 2
    ov.set_layout_mode("list")
    app.processEvents()
    ov.clear_caption()
    for _ in range(4):
        app.processEvents()
    assert ov._rows == [], "列表行没清"
    assert ov._dual_src_items == [] and ov._dual_tgt_items == [], \
        "切到列表布局点清空，dual 两栏的累积条目没清"
    assert ov._dual_rows_closed == []
    ov.set_layout_mode("dual")
    app.processEvents()
    assert ov._dual_src is None
    ov.deleteLater()


check("panel: 清空一次清掉双语两栏与列表行（v2.20.2 真机实测）",
      t_panel_clear_empties_both_layouts)


def t_panel_run_button_state_at_startup():
    """v2.20.2：面板把手启动那一刻就得说真话。

    `__init__` 里初值是 True 且 `set_running(True)`，而 `_load_settings` 显示
    面板时没人调 `update_overlay_status`——实测新建主窗：`running=False`、主窗
    按钮写「开始翻译」，面板却显示绿色「⏸ 暂停」。这正是该把手要防的谎报。"""
    w = MainWindow()
    try:
        want = "⏸ 暂停" if w.running else "▶ 开始"
        assert w.overlay._run_btn.text() == want, (w.running, w.overlay._run_btn.text())
        assert w.overlay.status_lbl.text(), "启动时状态行还是空的"
    finally:
        w.deleteLater()


check("panel: 把手与状态行启动即与真态一致（v2.20.2）",
      t_panel_run_button_state_at_startup)


def t_first_run_wizard_no_deleted_control():
    """v2.20.2：首次向导不许再教已删除的「启用字幕面板」勾选。

    v2.20.1 删了那个勾选与 `overlay_enabled` 键（面板改常驻），向导两支文案
    还在指它——新用户的第一动作就是去设置页找一个不存在的勾。设置页侧有锁
    钉住"该行已删"，向导侧此前没人看。"""
    from app.ui.first_run import FirstRunWizard
    from PySide6.QtWidgets import QLabel
    w = MainWindow()
    try:
        dlg = FirstRunWizard(w)
        texts = " ".join(l.text() for l in dlg.findChildren(QLabel))
        assert "启用字幕面板" not in texts, "向导仍在教一个已删除的控件"
        assert "常驻" in texts or "打开软件就在" in texts or "显隐" in texts, \
            "向导没给出面板的真实开关方式"
        dlg.deleteLater()
    finally:
        w.deleteLater()


check("panel: 首次向导文案与常驻实况一致、不教已删控件（v2.20.2）",
      t_first_run_wizard_no_deleted_control)


def t_overlay_list_draft_longer_than_final():
    """v2.19.3 立、v2.20.0 收归列表：草稿比终版**更长**时，终版必须就地收口那一行。

    真机 DW News 直播实测（离线 Argos）：面板上同一句并存两行——一行是流式草稿
    的膨胀版（短语被复读三遍 + 前瞻到下一句开头）挂着推测译，另一行是干净的
    终版。查翻译缓存证实送译原文只出现一次 → 复读纯属显示层。根因：
    `_find_pending` 只认"草稿是终版的前缀"，缺反方向（终版是草稿的前缀），
    于是终版匹配不到草稿行、另起一行。该判据属列表行系统，v2.20.0 起幕墙不再
    复用列表行，故本锁改在 **list 布局**下钉住（dual 侧的对应契约见上一条锁）。"""
    ov = CaptionOverlay()
    ov.apply_style(22, "#ffffff", "#1c1f26", 100)
    ov.set_show_source(True)
    ov.set_layout_mode("list")
    ov.show()
    for _ in range(6):
        app.processEvents()
    draft = ("I somehow got back up. and knocked a knife out of one of the guy's "
             "hands. the knife out of one of the guy's hands. that he was holding "
             "inside. that he was holding in self-defense")
    ov.show_pending(draft)
    for _ in range(4):
        app.processEvents()
    assert len(ov._rows) == 1 and ov._rows[0]["pending"], "前置：草稿应占一行待决"
    final = "I somehow got back up. and knocked a knife out of one of the guy's hands."
    ov.show_pending(final)
    ov.show_pending_result(final, "我不知怎地站起来，从他手中敲出一把刀。")
    for _ in range(4):
        app.processEvents()
    texts = [r["src"].text() for r in ov._rows]
    assert len(ov._rows) == 1, f"终版另起一行、膨胀草稿行仍挂在屏上：{texts}"
    assert texts[0] == final, f"行文本应校准为权威终版，实得 {texts[0]!r}"
    assert ov._rows[0]["tgt"].text().startswith("我不知怎地站起来")
    assert not ov._rows[0]["pending"], "终版后该行必须已收口"
    ov.deleteLater()


check("panel: 列表草稿长于终版时终版就地收口（v2.19.3 反方向配对）",
      t_overlay_list_draft_longer_than_final)


def t_overlay_qss_no_hash_comments():
    """v2.19.4：面板样式表里不得出现 `#` 行注释（Qt 只认 C 风格块注释）。

    最小实验实测 Qt 的行为：`#` 夹在中间 → **从该行起后续全部规则被丢弃**；
    放在首行 → 整张表作废。v2.16.1 起 `_apply_qss` 里就有一条 `#` 注释，其后的
    PanelRow / PanelRowNewest（行卡片底色 + 最新句左侧蓝条）/ PanelScroll 透明 /
    QScrollBar 宽度等 12 条规则从未生效过——"样式写了但看不见"这一类缺陷里最
    隐蔽的一种（v2.18.1 的 objectName 事故同族）。修好后滚动条宽度实测 8px。
    注释正文里也不得出现块注释的闭合符（会提前终止注释，重演同一事故）。"""
    import re
    ov = CaptionOverlay()
    ov.apply_style(22, "#ffffff", "#1c1f26", 92)
    css = ov.styleSheet()
    bad = [ln.strip()[:60] for ln in css.splitlines() if ln.strip().startswith("#")]
    assert not bad, f"QSS 里出现 # 行注释，其后的规则会被 Qt 整段丢弃：{bad}"
    # 块注释必须成对，且注释体内不得藏闭合符
    assert css.count("/*") == css.count("*/"), "块注释未配对"
    for seg in re.split(r"/\*", css)[1:]:
        body = seg.split("*/")[0]
        assert "*/" not in body
    ov.deleteLater()


check("panel: 样式表禁用 # 行注释（v2.19.4 QSS 静默丢规则回归）",
      t_overlay_qss_no_hash_comments)


def t_main_list_follow_bottom_guard():
    """v2.19.4：主窗字幕列表在用户回看时不得被新句拽回底部。

    直播里上滚重读刚说过的一句，旧实现每来一张卡就无条件 `setValue(maximum)`，
    2~6 秒后新片段把人拽走，回看根本完不成；悬浮面板早有 `_follow` 守卫 +
    「↓ 最新」按钮，主窗没有（同一产品里两处行为不一致）。现在只在已贴底时
    跟底，否则挂「↓ N 条新字幕」角标，点它回到底部并清零。"""
    w = MainWindow()
    try:
        w.resize(900, 420)
        w.show()                            # 必须真 show：不布局则 maximum() 恒 0，断言会空过
        for _ in range(8):
            app.processEvents()
        for i in range(14):
            w._on_asr_text("Sentence number %d about something long enough" % i,
                           "en", 1.0, -1.0)
            for _ in range(3):
                app.processEvents()
        sb = w.scroll.verticalScrollBar()
        assert sb.maximum() > 40, f"前置：内容应可滚动（maximum={sb.maximum()}）"
        assert sb.value() >= sb.maximum() - 4, "前置：默认应贴底跟随"
        sb.setValue(0)                       # 用户上轮回看
        for _ in range(4):
            app.processEvents()
        w._on_asr_text("A brand new sentence arrives while I am reading up",
                       "en", 1.0, -1.0)
        for _ in range(4):
            app.processEvents()
        assert sb.value() <= 4, f"回看时被新句拽到底部（value={sb.value()}）"
        assert not w.jump_new_button.isHidden(), "未跟底时必须给出「↓ N 条新字幕」提示"
        assert "1" in w.jump_new_button.text(), w.jump_new_button.text()
        w._jump_to_latest()
        for _ in range(4):
            app.processEvents()
        assert sb.value() >= sb.maximum() - 4, "点角标后应回到底部"
        assert w.jump_new_button.isHidden(), "回底后角标必须收起"
        w._on_asr_text("Next sentence after I returned to bottom", "en", 1.0, -1.0)
        for _ in range(4):
            app.processEvents()
        assert sb.value() >= sb.maximum() - 4, "回底后应恢复自动跟底"
    finally:
        w.deleteLater()


check("main: 列表回看不被拽底 + 新字幕角标（v2.19.4 跟底守卫）",
      t_main_list_follow_bottom_guard)


def t_overlay_dual_follow_bottom():
    """v2.18.1：dual 原文/译文区内容超出可视高度必须**自动跟底**。

    真机英语新闻实测（BBC Global News Podcast 整集）：长句把最新文字推到可视区之外
    ——原文区滚动条 max=49 却停在 value=0，用户看不到刚说出的那几个字，必须自己
    滚轮。流式字幕面板存在的意义就是"看到正在说的话"，这是硬伤。"""
    ov = CaptionOverlay()
    ov.apply_style(22, "#ffffff", "#1c1f26", 87)
    ov.set_show_source(True)
    ov.set_layout_mode("dual")
    ov.resize(560, 400)
    ov.show()
    for _ in range(8):
        app.processEvents()
    long_src = ("The minister said that the ceasefire would hold only if both sides agreed "
                "to withdraw heavy weapons and allow inspectors into the region and that the "
                "international community should support this humanitarian effort today")
    long_tgt = ("部长表示停火只有在双方同意撤出重型武器并允许核查人员进入该地区的情况下才会维持，"
                "而且国际社会应当支持这一人道主义努力，同时他还强调人道主义走廊必须立即开放"
                "以便救援物资能够送达受影响地区的平民手中，这一点至关重要")
    ov._dual_show_pending(long_src)
    ov._dual_show_result(long_src, long_tgt, True)
    for _ in range(30):
        app.processEvents()
    seen_overflow = False
    for key, wrap in (("src", ov._dual_src_wrap), ("tgt", ov._dual_tgt_wrap)):
        sb = wrap.verticalScrollBar()
        if sb.maximum() > 0:
            seen_overflow = True
            assert sb.value() >= sb.maximum() - 1, \
                f"{key} 区内容溢出却未跟底：value={sb.value()} max={sb.maximum()}"
    assert seen_overflow, "本例必须真的制造出溢出（否则锁是空的）"
    # 用户上滚回看 → 不得被后续内容强行拉回底部
    sb = ov._dual_src_wrap.verticalScrollBar()
    sb.setValue(0)
    for _ in range(4):
        app.processEvents()
    ov._dual_spec(long_src, long_tgt + "，此外还需要更多援助。", True)
    for _ in range(25):
        app.processEvents()
    assert sb.value() <= 4, f"用户回看时被强行拉回底部：value={sb.value()}"
    # 新句开始 → v2.22.0（§37.1 F6）：**不再自动夺回跟底**。v2.18.1 那版"新句
    # 一到就把两栏 follow 拨回 True"实测把用户正在读的那行拽走 178px，而 dual
    # 既没有 ↓ 按钮也没有计数——回看事实上不可能。现在暂停一直保持到用户自己
    # 滚回底部或按 ↓（与列表模式同一套契约）。
    # 先把面板高度钉住：不钉的话新句让面板长高、内容全装得下，滚动条 max 归 0
    # = "本来就在底部"，回看语义无从谈起（第一版这条锁就是被这个假象判红的）。
    ov.set_user_height(200)
    for _ in range(8):
        app.processEvents()
    sb.setValue(0)
    for _ in range(6):
        app.processEvents()
    assert sb.maximum() > 0, "钉高后原文栏必须真的溢出（否则锁是空的）"
    assert ov._dual_follow["src"] is False, "回看前提没建立"
    ov._dual_show_pending("A brand new sentence begins here now")
    for _ in range(25):
        app.processEvents()
    assert ov._dual_follow["src"] is False, "用户回看中的栏不得被新句夺回跟底"
    assert ov._jump_btn.isVisible(), "暂停跟底时面板必须给出 ↓ 回程"
    ov._dual_show_result("A brand new sentence begins here now", "全新的一句已经开始了。", True)
    assert ov._unread >= 1, f"暂停期间完成的句子要计入未读：{ov._unread}"
    ov._on_jump_clicked()
    for _ in range(8):
        app.processEvents()
    assert ov._dual_follow == {"src": True, "tgt": True}, "按 ↓ 应恢复两栏跟底"
    assert ov._unread == 0 and not ov._jump_btn.isVisible(), \
        "回到底部后未读计数与回程按钮一并收起"
    ov.deleteLater()
check("panel: dual 原文/译文区溢出自动跟底（真机新闻回归）", t_overlay_dual_follow_bottom)


def t_overlay_dual_srcoff_tgt_room():
    """v2.18.1：关「同时显示原文」时，当前句**译文区必须拿到自己的完整高度**。

    真机英语新闻实测（用户配置正是「原文 关」）：旧实现只隐藏原文标签，QScrollArea
    本体仍占 21px，加上仍按可见计算的分隔把手 8px 与三控件间距，body 60px 里
    译文区被饿到只剩 21px——一句正常译文（22px 字号约 32px 高）显示不全。"""
    ov = CaptionOverlay()
    ov.apply_style(22, "#ffffff", "#1c1f26", 87)
    ov.set_show_source(False)
    ov.set_layout_mode("dual")
    ov.resize(843, 707)
    ov.show()
    for _ in range(8):
        app.processEvents()
    src = "The quick brown fox jumps over the lazy dog and keeps running forward"
    tgt = "敏捷的棕色狐狸跳过了懒狗并继续向前奔跑"
    ov._dual_show_pending(src)
    ov._dual_show_result(src, tgt, False)
    for _ in range(30):
        app.processEvents()
    assert not ov._dual_src_wrap.isVisible(), "关原文后原文滚动区本体应收起（不再占高）"
    need = ov._dual_tgt.heightForWidth(max(40, ov._dual_tgt_wrap.viewport().width() - 2))
    have = ov._dual_tgt_wrap.viewport().height()
    assert have >= need, f"译文区被饿：可视 {have}px < 内容需求 {need}px"
    # 开原文 → 原文区回来，且分割线（唯一那条）重新可见
    ov.set_show_source(True)
    for _ in range(25):
        app.processEvents()
    assert ov._dual_src_wrap.isVisible(), "开原文后原文区应恢复"
    assert ov._dual_sep.isVisible(), "开原文后唯一分割线应出现"
    ov.deleteLater()
check("panel: 关原文时译文区不被饿（真机新闻回归）", t_overlay_dual_srcoff_tgt_room)


def t_stream_draft_no_duplicate():
    """v2.18.1：流式草稿不得把同一句上屏两遍。

    真机 BBC 新闻实测截图：原文区出现 "Our correspondent James Landale is in the
    Ukrainian capital and told... Our correspondent James Landale is in the Ukrainian
    capital and told me more about the over..."，译文区同样重复。根因：每只按
    "上一终版句"剥离重叠，没按屏幕上已显示的当前句剥离，而 whisper 两次转写措辞
    必有差异（"and told..." vs "and told me more"）→ _merge_stream 严格后缀失配
    → 走"无重叠"分支整句追加。"""
    w = MainWindow()
    w.show()
    w.running = True
    w.overlay.set_layout_mode("dual")
    w.overlay.set_show_source(True)
    for _ in range(6):
        app.processEvents()
    prev_final = "But there is no opposition."
    body = ("By the way there are several other proposals from our closest partners our "
            "international partners work on this track so that through joint efforts we can "
            "achieve this energy")
    w._dual_base = prev_final
    w._dual_current = ""
    # 第 1 拍：whisper 在窗口尾部吐出省略号（真机就是这样的文本形态）
    w._on_partial_preview(prev_final + " " + body + "...")
    first = w._dual_current
    assert first, "第 1 拍应上屏增量"
    assert "But there is no opposition" not in first, f"上一终版句不该被拖进当前句: {first}"
    # 第 2 拍：同一窗口的更完整转写——边界词从 "energy..." 变成 "energy and"，
    # 前缀对齐被打断，旧实现正是在这里整句重复
    w._on_partial_preview(body + " and our correspondent James Landale is in the "
                                 "Ukrainian capital")
    cur = w._dual_current
    key = "through joint efforts we can achieve this energy"
    assert cur.lower().count(key) == 1, \
        f"整句重复上屏（出现 {cur.lower().count(key)} 次，{len(cur.split())} 词）: {cur}"
    assert "our correspondent james landale" in cur.lower(), f"新话不得丢: {cur}"
    w._quitting = True
    w._teardown()
check("pipeline: 流式草稿不整句重复（真机新闻回归）", t_stream_draft_no_duplicate)

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
        assert w._dual_base == "The market opened higher today"
        # 终版翻译到达：v2.20.0 起 dual 不再"沉历史"——这句就地定格在大字区，
        # 驻留到下一句首拍触发原子换句；当前句数据清空（下一拍草稿零起点续接）
        w._on_translated("The market opened higher today", "今天高开", "argos", "en", "")
        assert w.overlay._dual_src.text() == "The market opened higher today", \
            "终版原文必须留在大字区"
        assert w.overlay._dual_tgt.text() == "今天高开", "终版译文必须留在大字区"
        assert w.overlay._rows == [], "dual 不得再建列表行（历史区已删）"
        assert w._dual_current == "", "终版收口后当前句数据清空"
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
        # v2.18.2（D-3）升级本锁：旧断言要求草稿以 "auto" 送译（itest_home 的
        # asr_language 恰为 "auto"）——那是把缺陷行为当契约锁住了。新契约：
        # ① 语言未知时**不送**（argos 走不通、spec 不走备援链，送=必错）
        w._dual_base = ""
        w._dual_current = ""
        w._last_asr_lang = ""
        w._tgroup_lang = ""
        w._on_partial_preview("Draft before any language is known")
        assert [x for x in tr.sent if x[2]] == [], f"语言未知时不得送推测翻译：{tr.sent}"
        assert w._dual_draft == "Draft before any language is known", "原文照常生长"
        # ② 会话一旦解出语言（此处模拟上一句已识别为 en），草稿照常送译且带真语言
        w._last_asr_lang = "en"
        w._dual_base = ""
        w._dual_current = ""
        # 草稿到达：原文区刷新 + 草稿送推测翻译（spec=True）
        w._on_partial_preview("The market opened higher")
        assert w.overlay._dual_src.text() == "The market opened higher"
        assert w._dual_draft == "The market opened higher"
        assert tr.sent[-1] == ("The market opened higher", "en", True), tr.sent
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

def t_main_panel_placeholder_once_per_piece():
    """v2.18.2（D-2）：每个识别片段只允许**一次**面板占位调用。
    历史上 _on_asr_text 对同一 text 调两次 overlay.show_pending（方法开头
    一次、建卡之后又一次，v2.4.0 遗留）。dual 原文区对"延续片段"（小写
    开头 → _starts_new_sentence 判为续接）做累加，实测原文区出现
    "Hello everyone and welcome to the show and welcome to the show"。
    GPU+dual 下被流式预览每拍整体覆盖而掩盖，**CPU 用户（预览被闸门自动
    关闭）直接可见**；列表模式因 _find_pending 幂等不受影响。"""
    w = MainWindow()
    w.show()
    w.overlay.show()
    w.running = True
    old = {k: w.config.get(k) for k in
           ("overlay_layout", "low_latency_mode", "instant_caption")}
    w.config.set("overlay_layout", "dual")
    w.config.set("low_latency_mode", True)
    w.config.set("instant_caption", True)      # 双调用只存在于流式两段式分支
    w.apply_overlay_from_config()

    class _Tr(object):
        _active_engine = "argos"

        def isRunning(self):
            return True

        def submit(self, text, lang, spec=False):
            return []

    w._active_translate = lambda: _Tr()
    hits = []
    real_show_pending = w.overlay.show_pending

    def spy(text):
        hits.append(text)
        return real_show_pending(text)

    w.overlay.show_pending = spy
    try:
        w._on_asr_text("Hello everyone", "en", "1.0")
        assert hits.count("Hello everyone") == 1, \
            f"新句片段应只调一次面板占位，实调 {hits.count('Hello everyone')} 次"
        w._on_asr_text(" and welcome to the show", "en", "1.0")
        assert hits.count(" and welcome to the show") == 1, \
            "延续片段被重复送面板（同一片段两次 show_pending）"
        src = w.overlay._dual_src.text()
        assert src == "Hello everyone and welcome to the show", src
        assert src.count("welcome") == 1, f"dual 原文区重复拼接：{src!r}"
    finally:
        w.stop_pipeline()
        for k, v in old.items():
            w.config.set(k, v)
check("panel: 每片段仅一次面板占位调用（dual 原文不重复）",
      t_main_panel_placeholder_once_per_piece)

def t_main_draft_waits_for_real_language():
    """v2.18.2（D-3）：草稿送译在语言未知时**不送**，语言一解出就照常送，
    且任何情况下都不把配置里的 "auto" 当语言喂给引擎。
    真机取证：默认 asr_language=auto + dual + argos 下，首个终版片段之前到的
    草稿全部失败（12 条推测回复 4 条报"缺少源语言信息…"，用户观感=原文在长
    译文不动）；translator.py:537 早就把 auto 挡成 source=None，spec 不走备援链。"""
    w = MainWindow()
    w.show()
    w.running = True
    old = {k: w.config.get(k) for k in
           ("overlay_layout", "low_latency_mode", "asr_language", "spec_translate")}
    w.config.set("overlay_layout", "dual")
    w.config.set("low_latency_mode", True)
    w.config.set("spec_translate", True)
    w.config.set("asr_language", "auto")      # 出厂默认，正是 D-3 的触发条件
    w.apply_overlay_from_config()

    sent = []

    class _Tr(object):
        _active_engine = "argos"

        def isRunning(self):
            return True

        def submit(self, text, lang, spec=False):
            sent.append((lang, bool(spec)))
            return []

    w._active_translate = lambda: _Tr()
    try:
        w._last_asr_lang = ""
        w._tgroup_lang = ""
        w._dual_base = ""
        w._dual_current = ""
        w._on_partial_preview("Hello there my friend")
        assert [x for x in sent if x[1]] == [], f"语言未知时不得送推测翻译：{sent}"
        assert w._dual_draft == "Hello there my friend", "原文照常生长，只是不送注定失败的请求"
        # 首个终版片段带来语言 → 之后的草稿必须带真语言送译
        w._on_asr_text("Hello there my friend", "en", "1.0")
        assert w._last_asr_lang == "en", "会话级语言记忆要落下来"
        sent.clear()
        w._on_partial_preview("Hello there my friend and welcome back")
        specs = [l for l, sp in sent if sp]
        assert specs and all(l == "en" for l in specs), f"应带 en 送译，实得 {specs}"
        assert "auto" not in [l for l, _ in sent], "任何提交都不得把 auto 当语言"
    finally:
        w.stop_pipeline()
        for k, v in old.items():
            w.config.set(k, v)
check("pipeline: 草稿送译拿不到真语言就不送（auto 视同未知）",
      t_main_draft_waits_for_real_language)

def t_translate_grouping_off():
    """v2.19.0：用户"能否添加一个关闭攒句的开关，我想进行实时的翻译"。
    开（默认）＝两轨制：碎片逐片上屏、翻译等整句攒完再送（译文更连贯）；
    关＝**每个识别片段一到达就立刻送翻译**（逐片实时），且不进攒句组。
    关键约束：关攒句**不得**连带把分段退回慢档——low_latency_mode 仍独立生效。"""
    w = MainWindow()
    w.show()
    w.running = True
    old = {k: w.config.get(k) for k in
           ("translate_grouping", "low_latency_mode", "spec_translate", "instant_caption")}
    w.config.set("low_latency_mode", True)     # 分段侧保持低延迟
    w.config.set("spec_translate", False)      # 本锁只看终版通路，排除推测式干扰
    w.config.set("instant_caption", True)

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
        # ① 开攒句：两片（新句 + 小写延续）攒在一起，未冲刷前**没有终版送译**
        w.config.set("translate_grouping", True)
        w._tgroup = []
        w._tgroup_lang = ""
        tr.sent.clear()
        w._on_asr_text("The report says the deal was signed", "en", "1.0")
        w._on_asr_text(" after months of difficult negotiation", "en", "1.0")
        assert [x for x in tr.sent if not x[2]] == [], \
            f"开攒句时不应逐片送终版翻译：{tr.sent}"
        assert len(w._tgroup) == 2, f"两片应攒在同一组：{w._tgroup}"
        w._flush_tgroup()
        finals = [x for x in tr.sent if not x[2]]
        assert len(finals) == 1 and "months" in finals[0][0] and "signed" in finals[0][0], \
            f"冲刷时应送出合并后的整句：{finals}"

        # ② 关攒句：每片到达立即送终版翻译，且不进攒句组
        w.config.set("translate_grouping", False)
        w._tgroup = []
        w._tgroup_lang = ""
        tr.sent.clear()
        w._on_asr_text("The report says the deal was signed", "en", "1.0")
        w._on_asr_text(" after months of difficult negotiation", "en", "1.0")
        finals2 = [x for x in tr.sent if not x[2]]
        assert len(finals2) == 2, f"关攒句应逐片即送，实送 {len(finals2)} 次：{finals2}"
        assert finals2[0][0] == "The report says the deal was signed" \
            and finals2[1][0] == " after months of difficult negotiation", \
            f"逐片送译应各送原文、不合并：{[f[0] for f in finals2]}"
        assert not getattr(w, "_tgroup", []), "关攒句后不得再往攒句组里塞片段"
        # ③ 分段侧不受牵连：低延迟仍为真（关掉攒句≠退回 14s 慢档）
        assert bool(w.config.get("low_latency_mode")) is True
    finally:
        w.stop_pipeline()
        for k, v in old.items():
            w.config.set(k, v)
check("pipeline: 关攒句＝逐片实时送译（且不退回慢档）", t_translate_grouping_off)


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
    # v2.5.0（F3）立，v2.22.0（§37.1 F1）重写。旧布局里状态行是工具条第 5 颗
    # `Ignored` 策略的控件，而同行 8 颗按钮 sizeHint 合计中文 598px / 英文 658px：
    # 560 面板分给它 **0px**，用户实测的 697 面板分给中文 67px、英文 **7px**；
    # 省略预算又按 `面板宽−240` 算（457px），于是屏上留下的是**没有省略号的硬裁**
    # ——面板唯一的诊断出口事实上从来没被看见过。现在它独占一行，三条都可测：
    # ① 不再挂在工具条下；② 拿得到面板的绝大部分宽度；③ 省略预算 ≤ 实宽。
    ov = CaptionOverlay()
    ov.show()
    try:
        for _ in range(4):
            app.processEvents()
        assert ov.status_lbl.parent() is ov, "状态行仍被塞在工具条里"
        assert ov.status_lbl.isHidden(), "空状态不得白占一行正文高度"
        long_msg = ("翻译连续失败 4 条 · 检查网络/代理节点，或到「设置-翻译」"
                    "测试通道 / 下载离线语言包")
        for w in (420, 560, 697):
            ov.resize(w, ov.height())
            for _ in range(4):
                app.processEvents()
            ov.set_status(long_msg, is_error=True)
            for _ in range(4):
                app.processEvents()
            got = ov.status_lbl.width()
            assert got >= int(w * 0.6), f"{w}px 面板状态行只有 {got}px（旧值 0~67px）"
            assert ov.status_lbl.y() >= ov._bar.geometry().bottom(), "状态行不在工具条下方"
            fm = ov.status_lbl.fontMetrics()
            assert fm.horizontalAdvance(ov.status_lbl.text()) <= got + 1, \
                f"省略预算 {ov.status_lbl.maximumWidth()} > 实宽 {got} → 硬裁无省略号"
            if fm.horizontalAdvance(long_msg) > got:
                assert ov.status_lbl.text().endswith("…"), "放不下时必须省略号收尾"
        ov.set_status("")
        assert ov.status_lbl.isHidden(), "清空状态要把整行收回去"
    finally:
        ov.deleteLater()
check("panel: 状态行独占一行、省略预算不超实宽（F1）", t_panel_status_label_width_capped)

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

def t_panel_no_edge_snap():
    """v2.20.1（用户点名）：贴边能力整族退役——面板拖到哪就停在哪。

    旧行为会把靠近屏幕边缘的松手点自动吸到边缘（v2.5.0 磁吸），另有菜单四向
    贴边与双击工具条顶/底循环。本锁在**旧实现上必红**：近顶松手后 y 必须仍是
    被拖到的那个值，且三个贴边实现都不许再存在。位置落盘（on_moved）不受影响。
    """
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
        assert not hasattr(ov, "_magnet_snap"), "松手磁吸实现应已删除"
        assert not hasattr(ov, "_snap_to_edge"), "贴边实现应已删除"
        assert not hasattr(ov, "_snap_cycle"), "双击贴边循环应已删除"
        ov.show()
        app.processEvents()
        g = ov.screen().availableGeometry()
        ov.move(g.left() + 300, g.top() + 5)      # 顶缘距可用区顶 5px
        app.processEvents()
        ov.mousePressEvent(_Ev(100, 20, g.left() + 400, g.top() + 25))
        ov.mouseMoveEvent(_Ev(100, 20, g.left() + 400, g.top() + 26))
        p_moved = (ov.x(), ov.y())
        assert p_moved == (g.left() + 300, g.top() + 6), f"拖动不跟手：{p_moved}"
        ov.mouseReleaseEvent(_Ev(100, 20, g.left() + 400, g.top() + 26))
        assert (ov.x(), ov.y()) == p_moved, \
            f"松手被自动挪动（贴边未退役）：{p_moved} -> {(ov.x(), ov.y())}"
        app.processEvents()
        assert moved and moved[-1] == p_moved, f"位置仍应落盘：{moved}"
    finally:
        ov.deleteLater()
check("panel: 拖动松手不再贴边磁吸（v2.20.1 贴边整族退役）", t_panel_no_edge_snap)

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
    # v2.5.3（用户指出双"置顶"逻辑冲突）：设置页开关标题不得含"置顶"——置顶语义
    # 专属面板 📌 按钮（控制是否压过其他窗口）。
    # v2.20.1：「启用字幕面板」勾选行随 `overlay_enabled` 一并删除（面板常驻实时
    # 显示），本锁改钉"这行确实没了 + 显示页无残留置顶字样 + 同步入口没了"。
    from app.ui.settings_dialog import _STD_ROWS, SettingsDialog
    assert not [r for r in _STD_ROWS if r["key"] == "overlay_enabled"], \
        "「启用字幕面板」行应已删除（面板改常驻实时显示）"
    rows = [r for r in _STD_ROWS if r.get("page") == "display"]
    assert rows, "显示页不应被清空（还剩同时显示原文/面板布局/流式上屏等）"
    bad = [r["key"] for r in rows if "置顶" in str(r.get("title", ""))]
    assert not bad, f"设置页标题再现『置顶』字样（置顶专属面板 📌）：{bad}"
    assert not hasattr(SettingsDialog, "sync_overlay_check"), \
        "面板显隐的设置页同步入口应随勾选一并删除"
check("settings: 启用勾选已删 + 显示页无置顶字样（v2.5.3→v2.20.1）",
      t_settings_overlay_title_no_pin_word)

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

        def isRunning(self):
            return True   # v2.20.4：主窗新增的"翻译线程已死"守卫要读它
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
    dlg._reset_defaults(confirm=False)     # v2.20.3：恢复默认加了确认弹窗，测试走非交互路径
    assert not any(k.startswith(("overlay_x", "overlay_y", "storage_root")) for k in dlg._staged)
    # v2.20.3：回车不得触发恢复默认（Qt 会把第一个按钮当默认按钮，实测一次回车
    # 把 20 个键全暂存成出厂值）
    assert dlg.reset_button.autoDefault() is False and dlg.reset_button.isDefault() is False
    assert dlg.apply_button.isDefault() is False and dlg.cancel_button.isDefault() is False
check("settings: 字段表完整 + 恢复默认不含 internal", t_settings_fields)


def t_settings_spec_gate():
    """v2.20.5：「推测式增量翻译」的三重闸必须在界面上如实反映。

    主窗 `_spec_enabled()` 要求 ①本开关 ②低延迟模式 ③攒句合并 同时成立（外加
    仅离线引擎）。设置页此前只呈现第 ① 项：用户关掉低延迟后这一项照样亮着、
    照样打着勾，实际一条推测译文都不产生——界面与介绍一起骗人。"""
    w = MainWindow()
    old_ll = w.config.get("low_latency_mode")
    old_gp = w.config.get("translate_grouping")
    try:
        w.config.set("low_latency_mode", False)
        w.config.set("translate_grouping", True)
        w._open_settings()
        dlg = w._settings_dlg
        assert dlg.spec_translate_check.isEnabled() is False,             "低延迟关闭时「推测式增量翻译」仍可选，用户会以为它生效"
        assert "低延迟" in dlg.spec_translate_check.toolTip()
        dlg.low_latency_check.setChecked(True)
        assert dlg.spec_translate_check.isEnabled() is True, "条件满足后没恢复可用"
        dlg.grouping_check.setChecked(False)
        assert dlg.spec_translate_check.isEnabled() is False, "攒句关闭时也该置灰"
        dlg._reset_defaults(confirm=False)      # 出厂两项都开 → 必须恢复可用
        assert dlg.spec_translate_check.isEnabled() is True
        dlg.deleteLater()
    finally:
        w.config.set("low_latency_mode", old_ll)
        w.config.set("translate_grouping", old_gp)
        w.deleteLater()


check("settings: 推测式翻译三重闸在界面上如实置灰（v2.20.5）", t_settings_spec_gate)

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

# ---------- 7b) v2.18.1 承诺类回归：向导不改配置 / 面板语言同步 ----------
def t_wizard_preserves_source_type():
    """重跑首启向导必须"一路点完成无副作用"（设置页原文：不会改动你的现有配置）。
    旧实现第 1 步恒勾「系统声音」、_finish 又无条件写回 source_type →
    麦克风用户走完向导被静默改回系统声音（运行时实测坐实）。"""
    from app.ui.first_run import FirstRunWizard
    keep = ("source_type", "device_index", "asr_model", "wizard_done")
    before = {k: Config().get(k) for k in keep}      # _finish 会写这四个键，全部回滚
    try:
        Config().set("source_type", "microphone")
        w = MainWindow()
        w.show()
        dlg = FirstRunWizard(w)
        assert dlg.radio_mic.isChecked() and not dlg.radio_system.isChecked(), \
            "向导第 1 步应回显当前音频源（麦克风），不得恒勾系统声音"
        dlg._page = 2
        dlg._finish()
        assert Config().get("source_type") == "microphone", \
            "一路点『完成』不应改动音频源"
        dlg.deleteLater()
        w._quitting = True
        w._teardown()
    finally:
        for k, v in before.items():
            Config().set(k, v)
check("wizard: 重跑向导不改动现有音频源（承诺回归）", t_wizard_preserves_source_type)


def t_panel_language_syncs_settings():
    """面板 🌐 切目标语言 → 开着的设置页下拉框必须跟随。
    旧调用点写的是**不存在的** dlg.reload_values()，AttributeError 被
    `except Exception: pass` 静默吞掉 → 同步从未发生。"""
    from app.ui.settings_dialog import SettingsDialog
    before = Config().get("target_lang")
    try:
        w = MainWindow()
        w.show()
        dlg = SettingsDialog(w)
        w._settings_dlg = dlg
        assert hasattr(dlg, "sync_target_lang"), "同步入口必须真实存在"
        w._on_panel_language("ja")
        assert Config().get("target_lang") == "ja"
        assert dlg.target_combo.currentData() == "ja", \
            f"设置页目标语言未跟随面板：{dlg.target_combo.currentData()}"
        dlg._staged["target_lang"] = "de"          # 用户已暂存未保存 → 不覆盖
        w._on_panel_language("ko")
        assert dlg._staged["target_lang"] == "de", "窄同步不得吞掉用户暂存值"
        dlg.deleteLater()
        w._quitting = True
        w._teardown()
    finally:
        Config().set("target_lang", before)
check("panel: 🌐 切语言同步设置页（reload_values 死调用回归）", t_panel_language_syncs_settings)

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

        def isRunning(self):
            return True   # v2.20.4：主窗新增的"翻译线程已死"守卫要读它

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



# ============ v2.21.2 UX 巡检回归锁 ============

def _pump(n=25):
    for _ in range(n):
        app.processEvents()


def t_no_default_button_anywhere():
    """v2.20.3 只摘了底部三个按钮的默认位，Qt 便把默认位顺移到下一个建出来的
    QPushButton（实测=音频页「刷新」）：搜索框按 Enter 会重扫设备并改写用户
    已暂存的 device_index。必须扫全量，而不是逐个记得加。"""
    from app.ui.settings_dialog import SettingsDialog
    from PySide6.QtWidgets import QPushButton
    from PySide6.QtTest import QTest
    from PySide6.QtCore import Qt
    w = MainWindow()
    d = SettingsDialog(w)
    d.load_from_config(); d.show(); _pump()
    try:
        bad = [b.text() for b in d.findChildren(QPushButton)
               if b.isDefault() or b.autoDefault()]
        assert not bad, "仍有默认按钮（Enter 会误触发）: %s" % bad
        fired = []
        for b in d.findChildren(QPushButton):
            b.clicked.connect(lambda: fired.append(b.text() or b.objectName()))
        QTest.keyClick(d.search_edit, Qt.Key_Return)
        _pump()
        assert not fired, "在搜索框按 Enter 触发了按钮: %s" % fired
    finally:
        w._quitting = True; w._teardown()
check("settings: 对话框任何地方按 Enter 都不该点按钮", t_no_default_button_anywhere)


def t_reset_defaults_stages_ui_language():
    """ui_language 登记为 internal（运行态键不该被清），但界面重绘会把这行拨回默认档。
    不暂存就是演一场没发生的重置：显示 zh、磁盘仍 en、重开自己翻回 English。"""
    from app.ui.settings_dialog import SettingsDialog
    w = MainWindow()
    try:
        w.config.set("ui_language", "en")
        d = SettingsDialog(w); d.load_from_config(); d.show(); _pump()
        assert d.ui_lang_combo.currentData() == "en", d.ui_lang_combo.currentData()
        d._reset_defaults(confirm=False); _pump()
        assert d._staged.get("ui_language") == "zh",             "恢复默认没暂存 ui_language，staged=%s" % sorted(d._staged)
        d._apply_staged(); _pump()
        assert w.config.get("ui_language") == "zh",             "暂存了却没落盘: %s" % w.config.get("ui_language")
    finally:
        w.config.set("ui_language", "zh")
        w._quitting = True; w._teardown()
check("settings: 恢复默认必须真的重置界面语言", t_reset_defaults_stages_ui_language)


def t_ui_language_hint_survives_batch():
    """"换英文界面+换引擎"是最常见的批量操作，恰好必带一个管线键；
    提示语若被"管线已重启"独占，用户以为界面马上变，实际不重启永远中文。"""
    from app.ui.settings_dialog import SettingsDialog
    w = MainWindow()
    try:
        d = SettingsDialog(w); d.load_from_config(); d.show(); _pump()
        w.running = True
        d._staged.clear()
        d._staged.update({"ui_language": "en", "engine": "argos"})
        d._apply_staged(); _pump()
        hint = d.dirty_hint.text()
        assert ("界面语言" in hint and "重启" in hint), "提示语丢了界面语言那句: %r" % hint
    finally:
        w._quitting = True; w._teardown()
check("settings: 界面语言的重启提示不被批量保存吃掉", t_ui_language_hint_survives_batch)


def t_panel_side_grouping_resyncs_spec_gate():
    """sync_overlay_keys 全程 blockSignals，v2.20.5 加的推测式闸门收不到 toggled，
    于是面板侧关攒句后设置页仍显示「推测式增量翻译」开着可点——同一句谎话。"""
    from app.ui.settings_dialog import SettingsDialog
    w = MainWindow()
    try:
        d = SettingsDialog(w); d.load_from_config(); d.show(); _pump()
        assert d.spec_translate_check.isEnabled(), "前置：三闸齐开时应可用"
        d.sync_overlay_keys({"translate_grouping": False}); _pump()
        assert not d.grouping_check.isChecked(), "攒句没被同步"
        assert not d.spec_translate_check.isEnabled(),             "面板侧关攒句后推测式翻译仍亮着（闸门被绕过）"
    finally:
        w._quitting = True; w._teardown()
check("settings: 面板侧改攒句要同步重算推测式闸门", t_panel_side_grouping_resyncs_spec_gate)


def t_dual_body_keeps_minimum_height():
    """原写法 max(min(body_min, avail), avail) 恒等于 avail，那层
    "原文30+把手8+译文30"的地板从来没落地；拖矮面板时两栏各剩 7px，字幕在滚但看不见。"""
    ov = CaptionOverlay()
    ov.set_layout_mode("dual"); ov.show(); _pump()
    try:
        for h in (80, 110, 160):
            ov.resize(560, h); _pump()
            body = ov._dual_body.height()
            assert body >= 60, "面板高 %d 时 dual 正文只剩 %dpx（地板失效）" % (h, body)
    finally:
        ov.close(); ov.deleteLater()
check("panel: dual 正文保留最小高度", t_dual_body_keeps_minimum_height)


def t_hidden_panel_ignores_draft_translation():
    """update_partial 早有可见性闸，译文是另一扇门进来的：隐藏期间迟到的草稿译文
    会写进已收口的卡片，再显示就是"英文上句+中文下句"的永久错配，
    而该行簿记为 closed，之后没有任何路径会纠正。"""
    ov = CaptionOverlay()
    ov.set_layout_mode("dual"); ov.show(); _pump()
    try:
        ov._dual_show_result("The president signed the agreement.",
                             "总统今天签署了贸易协定。", True)
        _pump()
        ov.close(); _pump()
        assert not ov.isVisible(), "前置：面板应已隐藏"
        ov.update_dual_draft_tgt("一夜之间暴雨淹没了沿海公路。",
                                 "Heavy rain flooded the coastal road")
        _pump()
        ov.show(); _pump()
        tgt = ov._dual_tgt.text()
        assert "签署" in tgt, "隐藏期间的草稿译文污染了已收口卡片: %r" % tgt[:44]
    finally:
        ov.close(); ov.deleteLater()
check("panel: 隐藏时迟到草稿译文不得污染已收口卡片", t_hidden_panel_ignores_draft_translation)



def t_hotkey_failure_is_reported():
    """v2.21.2：_apply_staged 曾直接调 main.apply_hotkey_config() 并丢掉返回值，
    而唯一写 hotkey_status 的 _apply_hotkey() 全仓零调用点——热键注册失败时
    那一行永远空白，右下角还写「已保存并应用」。"""
    from app import hotkey
    from app.ui.settings_dialog import SettingsDialog
    w = MainWindow()
    d = SettingsDialog(w); d.load_from_config(); d.show(); _pump()
    orig = hotkey.register
    try:
        hotkey.register = lambda *a, **k: False
        d._staged.clear()
        d._staged["hotkey_sequence"] = "Ctrl+Shift+F12"
        d._apply_staged(); _pump()
        txt = d.hotkey_status.text()
        assert "注册失败" in txt or "未生效" in txt,             "热键注册失败却没有任何反馈，hotkey_status=%r" % txt[:60]
    finally:
        hotkey.register = orig
        w._quitting = True; w._teardown()
check("settings: 热键注册失败必须如实告知", t_hotkey_failure_is_reported)


def t_model_detail_dialog_no_enter_default():
    """模型详情弹窗第一个按钮是「下载模型」，Qt 提成默认按钮——
    在弹窗里按一次回车就直接开始下载 75MB~1.6GB。"""
    from PySide6.QtWidgets import QPushButton
    from PySide6.QtTest import QTest
    from PySide6.QtCore import Qt
    from app.ui.settings_dialog import _ModelDetailDialog
    w = MainWindow()
    md = _ModelDetailDialog(w, "small", "small", False)
    try:
        fired = []
        for b in md.findChildren(QPushButton):
            b.clicked.connect(lambda _c=None, t=b.text(): fired.append(t))
        QTest.keyClick(md, Qt.Key_Return)
        _pump()
        assert not fired, "在模型详情弹窗按 Enter 触发了按钮: %s" % fired
    finally:
        md.deleteLater()
        w._quitting = True; w._teardown()
check("settings: 模型详情弹窗按 Enter 不得开始下载", t_model_detail_dialog_no_enter_default)


# ===================== v2.22.0 第六轮巡检：悬浮窗识别与翻译体验（§37.1） =====================

def t_panel_stop_finalizes_placeholder_rows():
    """F3：停止管线必须收口面板上还没等到译文的行。

    旧实现 `stop_pipeline` 只终态化主窗自己的卡片，从不碰 `overlay._rows`：
    探针 A2 实测停止后两行仍是 `⟳ 翻译中…` + pending=True，而同一扇窗的工具条
    已经写"已停止 · 待机中"——上下自相矛盾，且这些占位一路活到下一场。"""
    w = MainWindow()
    w.show()
    w.overlay.show()
    w.overlay.set_layout_mode("list")
    w.overlay.clear_caption()
    _pump()
    try:
        w.running = True
        w.capture_thread = w.asr_thread = w.translate_thread = None
        w._on_asr_text("The first sentence never got a translation.", "en", 3.0)
        w._on_asr_text("The second one is still waiting too.", "en", 3.0)
        _pump()
        pend_before = [r for r in w.overlay._rows if r["pending"]]
        assert len(pend_before) == 2, f"前提：应有两行待决，实际 {len(pend_before)}"
        w.stop_pipeline()
        _pump()
        still = [r for r in w.overlay._rows if r["pending"]]
        assert not still, f"停止后面板仍有 {len(still)} 行挂着待决占位"
        texts = [r["tgt_text"] for r in w.overlay._rows]
        assert all("翻译中" not in t for t in texts), texts
        assert any("未完成翻译" in t for t in texts), f"占位应改写成停止终态：{texts}"
    finally:
        w._quitting = True
        w._teardown()
check("panel: 停止管线收口面板占位行（§37.1 F3）", t_panel_stop_finalizes_placeholder_rows)


def t_panel_drop_finalizes_only_that_row():
    """F3：翻译被丢弃时，面板只收口**那一句**，别的待决行不动。"""
    ov = CaptionOverlay()
    ov.show()
    _pump(4)
    try:
        ov.show_pending("The central bank held rates unchanged this morning.")
        ov.show_pending("Oil prices slid sharply on weaker demand data.")
        _pump(4)
        assert sum(1 for r in ov._rows if r["pending"]) == 2, \
            f"前提：应有两行待决，实际 {len(ov._rows)}"
        ov.finalize_pending("翻译队列繁忙，本句已跳过",
                            "The central bank held rates unchanged this morning.")
        _pump(4)
        r0, r1 = ov._rows[0], ov._rows[1]
        assert not r0["pending"] and r0["tgt_text"] == "翻译队列繁忙，本句已跳过", r0
        assert r1["pending"] and "翻译中" in r1["tgt_text"], \
            f"不该被牵连的第二行被动了：{r1['tgt_text']!r}"
    finally:
        ov.deleteLater()
check("panel: finalize_pending 只收口指定句（不误伤其它待决行）",
      t_panel_drop_finalizes_only_that_row)


def t_panel_spec_translation_survives_finalize():
    """F3 契约的另一半：已有推测译的行收口时**保住那半句**，不盖成失败文案
    （用户正在看那行字，半句也胜过"未完成翻译"——与主窗卡片 finalize_spec 同源）。"""
    ov = CaptionOverlay()
    ov.show()
    _pump(4)
    try:
        ov.show_pending("Inflation slowed for a third month in the euro area.")
        ov.update_spec_result("Inflation slowed for a third month in the euro area.",
                              "欧元区通胀连续第三个月放缓", True)
        _pump(4)
        assert ov._rows[0]["spec"] is True
        ov.finalize_pending("已停止 · 该句未完成翻译")
        _pump(4)
        r = ov._rows[0]
        assert r["tgt_text"] == "欧元区通胀连续第三个月放缓", r["tgt_text"]
        assert not r["pending"] and not r["spec"], r
    finally:
        ov.deleteLater()
check("panel: 收口时保住已生长的推测译", t_panel_spec_translation_survives_finalize)


def t_panel_hidden_final_translation_still_lands():
    """F3：面板隐藏期间到达的**终版**译文不得丢弃。

    旧实现 `if self.overlay.isVisible():` 把整条 `show_pending_result` 闸掉，
    于是隐藏前上屏的占位行在重新显示后永远停在"⟳ 翻译中…"（探针 C2），
    此后再没有路径补它。草稿那两路的可见性闸门保留（v2.20.2/v2.21.2 的
    僵尸行教训），终版是每句一次的权威结果，不在此列。"""
    w = MainWindow()
    w.show()
    w.overlay.show()
    w.overlay.set_layout_mode("list")
    w.overlay.clear_caption()
    _pump()
    try:
        w.running = True
        w._sid_tr = None
        w._on_asr_text("This one was pending when the panel got hidden.", "en", 3.0)
        _pump()
        assert "翻译中" in w.overlay._rows[-1]["tgt_text"]
        w.overlay.hide()
        _pump(4)
        w._on_translated("This one was pending when the panel got hidden.",
                         "这句在面板隐藏时译好了。", "argos", "en", None)
        _pump()
        row = w.overlay._rows[-1]
        assert not row["pending"], "隐藏期间到达的终版没收口"
        assert row["tgt_text"] == "这句在面板隐藏时译好了。", row["tgt_text"]
    finally:
        w._quitting = True
        w._teardown()
check("panel: 隐藏期间到达的终版译文照常落行（§37.1 F3）",
      t_panel_hidden_final_translation_still_lands)


def t_preview_draft_shares_quality_gate():
    """F4：流式草稿通道必须过与正式识别同一把质量闸。

    `StreamPreview._transcribe` 旧实现是 `" ".join(seg.text)`，什么都不判——
    探针 B4 实测把 "You are a video! / ♪ ♪ ♪ / KRAVZO…" 这类噪声段整串并进
    面板原文行，还会被送去推测翻译，没有终版覆盖就永久留在屏上。"""
    from app.asr.preview import StreamPreview

    class _Seg:
        def __init__(self, text, lp=0.0, ns=0.0):
            self.text = text
            self.avg_logprob = lp
            self.no_speech_prob = ns

    junk = [("", 0.0, 0.0), ("♪ ♪ ♪", 0.0, 0.0),            # 纯符号：has_content 必滤
            ("You are a video!", -2.5, 0.0),                 # 低置信度胡言：判据一支
            ("Please subscribe to my channel", -0.6, 0.9),   # 无语音概率高：判据二支
            ("The minister said the ceasefire would hold.", -0.2, 0.1)]
    segs = [_Seg(t, lp, ns) for (t, lp, ns) in junk]

    class _Model:
        def __init__(self, items):
            self._items = items

        def transcribe(self, audio, **kw):
            return iter(self._items), object()

    p = StreamPreview(_Model(segs), "en")
    out = p._transcribe("x")
    assert out == "The minister said the ceasefire would hold.", f"草稿闸门失效：{out!r}"
    # 关掉幻觉抑制仍要过 has_content（与正式通道同一取舍）
    p2 = StreamPreview(_Model(segs), "en", hallucination_filter=False)
    got2 = p2._transcribe("x")
    assert "♪" not in got2, got2
    assert "You are a video!" in got2 and "subscribe" in got2, got2
    # 判据只有一份实现：预览模块必须引用 engine 的同一个函数对象
    import app.asr.engine as eng
    import app.asr.preview as prev
    assert prev.hallucination_ok is eng.hallucination_ok, "两处各写一份阈值"
    assert prev.has_content is eng.has_content


check("asr: 流式草稿与正式识别共用质量闸（§37.1 F4）", t_preview_draft_shares_quality_gate)


def t_panel_status_line_reports_every_phase():
    """F1/F2/F5：状态行按阶段说实话，而且常规运行态不再占正文高度。

    旧实现两个毛病叠在一起：① 状态行与 8 颗按钮同行，实宽 0~67px（英文 7px），
    写什么都看不见；② 点「开始翻译」后立刻写"运行中 · 系统声音"，而模型还在
    加载/下载（十几秒到几分钟），就绪时也不回灌；③ 识别线程因致命错误退出后
    面板继续写"运行中"。"""
    w = MainWindow()
    w.show()
    _pump()
    try:
        ov = w.overlay
        w.running = False
        w._muted_warn = w._low_input_warn = False
        w._backlog_warn = False
        w._fatal_warn = None
        w._asr_ready = False
        w._model_dl_timer = None
        # ① 停止态
        w.update_overlay_status()
        assert "已停止" in ov.status_lbl.text(), ov.status_lbl.text()
        assert not ov.status_lbl.isHidden(), "有状态词时这一行必须可见"
        # ② 加载中 / 下载中
        w.running = True
        w.update_overlay_status()
        assert "正在加载识别模型" in ov.status_lbl.text(), ov.status_lbl.text()
        class _T:
            def isActive(self):
                return True
        w._model_dl_timer = _T()
        w._dl_pct = 42
        w.update_overlay_status()
        assert "42%" in ov.status_lbl.text(), ov.status_lbl.text()
        w._model_dl_timer = None
        # ③ 就绪且常规运行 → 状态行收起（不占正文高度）
        w._asr_ready = True
        w.update_overlay_status()
        assert ov.status_lbl.isHidden(), f"常规运行不该占一行：{ov.status_lbl.text()!r}"
        # ④ 致命错误置顶常驻，且把手/状态不得继续谎称运行中
        w._fatal_warn = "模型加载失败：CUDA out of memory"
        w.update_overlay_status()
        assert "out of memory" in ov.status_lbl.text(), ov.status_lbl.text()
        assert "#fbbf24" in ov.status_lbl.styleSheet()
        # ⑤ 积压丢段也要上面板（用户看的是面板，主窗常在托盘）
        w._fatal_warn = None
        w._backlog_warn = True
        w.update_overlay_status()
        assert "识别跟不上" in ov.status_lbl.text(), ov.status_lbl.text()
        w._backlog_warn = False
    finally:
        w._quitting = True
        w._teardown()
check("panel: 状态行按阶段说实话（加载/下载/停摆/积压/常规）",
      t_panel_status_line_reports_every_phase)


def t_panel_eviction_keeps_reading_position():
    """F7：40 行上限淘汰最老一行时，不得把用户正在读的那句往上抽。

    旧实现 `pop(0)` 之后不补滚动位置：探针 C1 实测回读时来新句，同一像素位置上
    换成了下一句（Line 06 → Line 07）。"""
    ov = CaptionOverlay()
    ov.show()
    ov.set_layout_mode("list")
    ov.clear_caption()
    _pump(6)
    try:
        ov.set_user_height(260)          # 钉住高度，保证真的出现滚动条
        for i in range(46):
            ov.show_caption(f"Transcript line {i:02d} is long enough to wrap onto two rows.",
                            f"这是第 {i:02d} 行字幕文本，长度足够换行。", True)
        _pump(20)
        sb = ov._scroll.verticalScrollBar()
        assert sb.maximum() > 200, f"前提：列表必须可滚（max={sb.maximum()}）"
        sb.setValue(int(sb.maximum() * 0.5))
        _pump(6)
        ov._follow = False               # 用户回读中
        mid = int(sb.maximum() * 0.5)
        sb.setValue(mid)
        _pump(6)
        assert not ov._follow, "回读前提没建立（被自动跟底拉回了）"

        def top_text():
            for it in ov._rows:
                if it["row"].geometry().bottom() > sb.value():
                    return it["src_text"]
            return None

        before = top_text()
        assert before, "视口顶部没有行，锁是空的"
        ov.show_caption("A brand new line arrives while the user reads back.",
                        "用户回读时来了一句新的。", True)
        ov.show_pending_result("A brand new line arrives while the user reads back.",
                               "用户回读时来了一句新的。", True)
        _pump(20)
        after = top_text()
        assert before == after, f"淘汰把正在读的那行抽走了：{before!r} → {after!r}"
    finally:
        ov.deleteLater()
check("panel: 40 行淘汰不抽走正在读的那行（§37.1 F7）",
      t_panel_eviction_keeps_reading_position)


# ===================== v2.23.0 第七轮巡检：重复翻译（§38 F9） =====================

_E1 = "Usually when Trump is demolishing something on Air Force One, it's a bucket of KFC."
_E1T = "通常特朗普在空军一号上拆除某事时,会是一桶KFC."
_E2 = "Great."
_E2T = "伟大的。"
_ECHO = _E1 + " that's a bucket of KFC."
_ECHOT = "通常特朗普在空军一号上拆除某事时,是一桶KFC,即一桶KFC."


def t_dual_echo_beat_opens_no_row():
    """用户实拍：滑窗预览把**已经收口那一句**连着复读尾巴再转一遍，面板上同一句
    冒出两行、各带一份措辞不同的译文。旧判据 `_dual_same_sentence(cur, t)` 只跟
    **最新一行**比（探针实测：与 'Great.' 判不同句、与第一句判同句），隔一行就漏。"""
    ov = CaptionOverlay()
    ov.set_layout_mode("dual")
    ov.show()
    _pump(6)
    try:
        ov._dual_show_pending(_E1)
        ov._dual_show_result(_E1, _E1T, True)
        ov._dual_show_pending(_E2)
        ov._dual_show_result(_E2, _E2T, True)
        _pump(6)
        assert len(ov._dual_src_items) == 2, "前提：两行都已收口"
        # 回声拍（隔了一行）——必须整拍作废
        ov.update_partial(_ECHO)
        _pump(6)
        assert len(ov._dual_src_items) == 2, \
            f"回声拍又开了一行：{[i['text'][:30] for i in ov._dual_src_items]}"
        assert ov._dual_src_items[0]["text"] == _E1, "回声把旧行文本换成了带复读尾巴的版本"
        assert ov._dual_tgt_items[0]["lab"].text() == _E1T, "回声改写了旧行已有的译文"
        # 回声拍的**译文**也不能落到当前句的卡片上
        ov.update_dual_draft_tgt(_ECHOT, _ECHO)
        _pump(4)
        assert ov._dual_tgt_items[-1]["lab"].text() == _E2T, \
            f"回声的草稿译文写进了当前句：{ov._dual_tgt_items[-1]['lab'].text()!r}"
        # 真新句照常开行（闸不能把后面所有话都拦掉）
        ov.update_partial("Oil prices slid sharply on weaker demand data today.")
        _pump(6)
        assert len(ov._dual_src_items) == 3, "回声之后的真新句被一起吞掉了"
        assert ov._dual_src_items[-1]["text"].startswith("Oil prices")
    finally:
        ov.deleteLater()
check("panel: 双语回声拍不再开出重复行（§38 F9）", t_dual_echo_beat_opens_no_row)


def t_list_echo_row_suppressed():
    """列表布局同形：回声终版/一次性上屏都不许补出第二行。"""
    ov = CaptionOverlay()
    ov.show()
    _pump(6)
    try:
        ov.show_caption(_E1, _E1T, True)
        ov.show_caption(_E2, _E2T, True)
        _pump(4)
        assert len(ov._rows) == 2
        ov.show_caption(_ECHO, _ECHOT, True)          # 一次性上屏路径
        ov.show_pending(_ECHO)                        # 占位路径
        _pump(4)
        assert len(ov._rows) == 2, f"列表回声多出一行：{len(ov._rows)}"
        ov.show_pending_result(_ECHO, _ECHOT, True)   # 终版路径
        _pump(4)
        assert len(ov._rows) == 2, "列表回声终版又补了一行"
        assert ov._rows[-1]["tgt_text"] == _E2T, "_last_result 被回声改指向了带尾巴的版本"
        assert ov._last_result == (_E2, _E2T), ov._last_result
    finally:
        ov.deleteLater()
check("panel: 列表回声行同样不补开（§38 F9）", t_list_echo_row_suppressed)


def t_main_echo_segment_not_carded_not_translated():
    """主窗侧：回声段不建卡、**也不送译**（用户说的"重复翻译"字面就是又翻了一遍）。"""
    w = MainWindow()
    w.show()
    w.overlay.set_layout_mode("list")
    _pump()
    try:
        w.running = True
        w._sid_asr = None
        w._sid_tr = None

        class _Tr:
            def isRunning(self):
                return True
        # `_on_asr_text` 末尾送译前有一道 `_active_translate() is not None` 的闸，
        # 不给它一个假线程，下面的桩根本不会被调用（锁就会空转）
        w.translate_thread = _Tr()
        submitted = []
        w._submit_for_translation = lambda t, d: submitted.append(t)
        w._on_asr_text(_E1, "en", 3.0)
        _pump()
        w._on_translated(_E1, _E1T, "argos", "en", None)
        _pump()
        w._on_asr_text(_E2, "en", 1.0)
        _pump()
        w._on_translated(_E2, _E2T, "argos", "en", None)
        _pump()
        assert len(submitted) == 2, submitted
        before = len([1 for i in range(w.scroll_layout.count())
                      if w.scroll_layout.itemAt(i).widget() is not None
                      and w.scroll_layout.itemAt(i).widget().__class__.__name__ == "CaptionCard"])
        w._on_asr_text(_ECHO, "en", 3.0)
        _pump()
        after = len([1 for i in range(w.scroll_layout.count())
                     if w.scroll_layout.itemAt(i).widget() is not None
                     and w.scroll_layout.itemAt(i).widget().__class__.__name__ == "CaptionCard"])
        assert after == before, f"回声段又建了一张卡：{before} → {after}"
        assert submitted == [_E1, _E2], f"回声段被送去翻译了：{submitted}"
    finally:
        w.translate_thread = None   # 假线程没有 stop/wait/disconnect，收尾会炸
        w._quitting = True
        w._teardown()
check("main: 回声段不建卡也不送译（§38 F9）", t_main_echo_segment_not_carded_not_translated)


def t_echo_guard_three_ways_not_to_swallow():
    """去重判据的反向护栏——每条都是"吞真话"的真实形态，必须**不**判回声。

    断言逐条收集后一次报出：第一版这条锁在三项判据同时被放松时只红了
    一处（第一条断言就把后面几条挡住了），那样根本判不出"哪条护栏没人守"。"""
    from app.ui.caption_overlay import recent_echo
    settled = [_E1, _E2]
    bad = []
    # ① 更完整的版本（whisper 带上下文重转常把句子转长）
    fuller = _E1 + " and the secretary added that the review would finish by friday."
    if recent_echo(fuller, settled):
        bad.append("把更完整的版本当回声吞掉了（ECHO_MAX_GROW 护栏失效）")
    # ② 短句真重复：说话人把 "Great." 说两遍
    if recent_echo("Great.", ["Great.", "Something else entirely here."]):
        bad.append("短句复读被吞——那是真话（ECHO_MIN_CHARS 护栏失效）")
    # ③ 近义改写不是回声：跨行判据不许用"前 3 词同源"快判
    para = "Usually when Trump is landing something on Air Force One, it's a huge win."
    if recent_echo(para, settled):
        bad.append("近义改写的下一句被当成上一句的重播（head3 快判漏进跨行判据）")
    # ④ 窗口外的旧话重播不去重（只回看 ECHO_WINDOW 条）
    long_ago = "The central bank held rates unchanged this morning as expected by traders."
    many = [long_ago] + [f"Sentence number {i} of the bulletin reads quite distinctly today."
                         for i in range(8)]
    if recent_echo(long_ago + " that is what they said", many):
        bad.append("超出回声窗口的历史句也被吞了")
    # 同一句若就在窗口内则应当判回声（证明上一条红在"窗口"而不是红在判据）
    if not recent_echo(long_ago + " that is what they said", many[-3:] + [long_ago]):
        bad.append("窗口内的真回声没被抓住")
    # ⑤ 用户实拍那条回声必须命中
    if not recent_echo(_ECHO, settled):
        bad.append("用户实拍那条回声没被抓住")
    assert not bad, "；".join(bad)


check("echo: 去重判据的反向护栏（不吞真话）", t_echo_guard_three_ways_not_to_swallow)


def t_echo_guard_never_strands_open_row():
    """回声判据只比**已定稿**的行：未收口那一句不能被当成"旧话"参与去重。

    第一版这条锁是空的——列表/双语两条路在问回声**之前**就先按原文找未收口行
    （`_find_pending` / `_dual_row_for`），所以端到端怎么放松判据都不会红。
    改成直接断言判据本身：屏上只有一行未收口的占位时，同句的更长版本必须
    判"不是回声"，否则那一行会永远停在 `⟳ 翻译中…`。"""
    ov = CaptionOverlay()
    ov.show()
    _pump(6)
    try:
        ov.show_pending(_E1)                      # 待决行（未收口）
        _pump(4)
        assert ov._rows[0]["pending"] is True
        assert ov._recent_srcs() == [], f"未收口的行漏进了回声判据输入：{ov._recent_srcs()}"
        assert not ov._recent_echo(_ECHO), "未收口行被当成旧话 → 它的终版会被当回声吞掉"
        ov.show_pending_result(_ECHO, _E1T, True)  # 同句的更长终版
        _pump(4)
        hit = [r for r in ov._rows if r["src_text"] == _ECHO]
        assert hit, "回声判据把未收口行的终版也吞了 → 那一行永远停在占位"
        assert not hit[0]["pending"], hit[0]
        # 收口之后，同一句的再重播才算"旧话"
        assert ov._recent_srcs() == [_ECHO], ov._recent_srcs()
        assert ov._recent_echo(_ECHO + " again and again"), "已定稿行的重播没被认出来"
    finally:
        ov.deleteLater()
check("echo: 未收口行绝不参与回声去重", t_echo_guard_never_strands_open_row)


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
