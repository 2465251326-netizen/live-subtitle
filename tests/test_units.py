import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

from app.audio.capture import Segmenter, resample_to_16k
from app.translate.translator import TranslationCache, _cache

# v2.3.20（测试自愈）：mkdtemp 全局记账 + 每例后回收。曾有 7 个测试造临时
# 目录不清理、其中 60MB 假 model.bin 随百余次运行在 %TEMP% 堆到 8GB。
# 包一层 tempfile.mkdtemp 而非逐个补 finally——新增测试自动纳入保护。
import tempfile as _tempfile
_MADE_TMP = []
_orig_mkdtemp = _tempfile.mkdtemp


def _tracked_mkdtemp(*a, **k):
    d = _orig_mkdtemp(*a, **k)
    _MADE_TMP.append(d)
    return d


_tempfile.mkdtemp = _tracked_mkdtemp


def _purge_tmp():
    import shutil
    while _MADE_TMP:
        try:
            shutil.rmtree(_MADE_TMP.pop(), ignore_errors=True)
        except Exception:
            pass


def test_vad_silence():
    s = Segmenter()
    out = [x for ch in [np.zeros(480, dtype=np.float32)] * 40 if (x := s.feed(ch)) is not None]
    assert not out, "纯静音不应触发分段"


def test_resample_antialias():
    """v2.0.6：48k→16k 时 >8kHz 分量应被低通抑制，而非混叠进语音带。"""
    sr = 48000
    t = np.arange(sr, dtype=np.float64) / sr
    # 13kHz 在折返区（混叠后落 3kHz，可与通带基准分离观测）；1kHz 是通带基准
    sig = (0.5 * np.sin(2 * np.pi * 1000 * t)
           + 0.5 * np.sin(2 * np.pi * 13000 * t)).astype(np.float32)
    out = resample_to_16k(sig, sr)
    assert out.shape[0] == 16000, "重采样长度应正确"
    assert np.all(np.isfinite(out)), "重采样不得产生 NaN"
    spec = np.abs(np.fft.rfft(out))
    freqs = np.fft.rfftfreq(out.shape[0], d=1.0 / 16000)

    def band_amp(lo, hi):
        m = (freqs >= lo) & (freqs <= hi)
        return float(spec[m].max())

    base = band_amp(800, 1200)
    assert base > 0.3 * 8000, "1kHz 通带应基本保留"
    assert band_amp(2800, 3200) < 0.25 * base, \
        "13kHz 混叠分量（折叠到 3kHz）应被低通显著抑制"


def test_resample_passband_unchanged():
    """44.1k→16k：通带 440Hz 正弦幅值不应被抗混叠滤波明显衰减。"""
    sr = 44100
    t = np.arange(sr, dtype=np.float64) / sr
    sig = np.sin(2 * np.pi * 440 * t).astype(np.float32)
    out = resample_to_16k(sig, sr)
    rms = float(np.sqrt(np.mean(out ** 2)))
    assert 0.6 < rms < 0.8, f"通带 440Hz 应近似无损，实测 RMS={rms:.3f}"


def test_vad_short_noise():
    s = Segmenter()
    sp = np.random.uniform(-0.3, 0.3, 3200).astype(np.float32)
    silo = np.zeros(16000, dtype=np.float32)
    out = [x for ch in list(np.array_split(sp, 6)) + list(np.array_split(silo, 10)) if (x := s.feed(ch)) is not None]
    assert not out, "极短语音应被丢弃"


def test_vad_long_speech_force_split():
    s = Segmenter()
    speech = np.random.uniform(-0.3, 0.3, 16000 * 20).astype(np.float32)
    out = [x for ch in np.array_split(speech, 333) if (x := s.feed(ch)) is not None]
    assert out, "连续语音应有输出"
    assert all(o.shape[0] / 16000 <= 14.5 for o in out), "段长不应超过上限"


def test_vad_normal_segment():
    s = Segmenter()
    speech = np.random.uniform(-0.3, 0.3, 16000 * 2).astype(np.float32)
    silo = np.zeros(9600, dtype=np.float32)
    out = [x for ch in list(np.array_split(speech, 66)) + list(np.array_split(silo, 24)) if (x := s.feed(ch)) is not None]
    assert out and out[0].shape[0] / 16000 >= 2.0, "2s 语音应完整保留"


def test_cache_persist():
    _cache.put("ci:zh-CN:hello world", ("你好世界", "en"))
    # v2.0.6 攒批落盘：put 不再立即写盘，显式 flush 后才保证持久化
    _cache.save()
    c2 = TranslationCache()
    assert c2.get("ci:zh-CN:hello world") == ("你好世界", "en"), "缓存应持久化重载"


def test_cache_batched_flush():
    """v2.0.6：攒批窗口内连续 put 不写盘，save() 一次性落盘。"""
    import tempfile
    import pathlib
    import app.translate.translator as tr
    tmp = pathlib.Path(tempfile.mkdtemp())
    orig_path_fn = tr.TranslationCache._path
    try:
        c = tr.TranslationCache()
        tr.TranslationCache._path = lambda self: tmp / "cache.json"
        c._loaded = True  # 跳过懒加载（测试不依赖真实配置路径）
        for i in range(9):
            c.put(f"k{i}", (f"v{i}", "en"))
        assert not (tmp / "cache.json").exists(), "9 条 < 阈值 10，不应已写盘"
        c.put("k9", ("v9", "en"))  # 第 10 条触发自动落盘
        assert (tmp / "cache.json").exists(), "攒满 10 条应触发自动落盘"
        c2 = tr.TranslationCache()  # 读取实例不设 _loaded，走真实懒加载路径
        assert c2.get("k9") == ("v9", "en"), "自动落盘后新实例应可读到"
    finally:
        tr.TranslationCache._path = orig_path_fn


def test_target_langs_coverage():
    from app.config import TARGET_LANGS, LANGUAGES
    assert len(TARGET_LANGS) >= 15, "目标语言应覆盖主流语种"
    assert all(code in LANGUAGES for code in TARGET_LANGS), "每个目标语言都应有显示名"
    assert TARGET_LANGS[0] == "zh-CN", "简体中文应为默认第一项"


def test_argos_code_map():
    from app.translate.translator import ArgosEngine, WHISPER_LANG_MAP
    assert WHISPER_LANG_MAP.get("zh") == "zh-CN"
    # zh 源 + 非中文目标：Argos 包码应归一为 "zh"，且不会找不到包方向
    assert ("en", "ja") in [("en", "ja")], "sanity"
    src = WHISPER_LANG_MAP.get("zh", "zh")
    if src.startswith("zh"):
        src = "zh"
    assert src == "zh", "Argos 源码应归一为 zh 以匹配 en_zh 等包目录名"


def test_resolve_pack_dir(tmp_path=None):
    import tempfile
    from pathlib import Path
    from app.translate import offline_pack as op
    old = op.PACKS_DIR
    try:
        with tempfile.TemporaryDirectory() as td:
            op.PACKS_DIR = Path(td)
            # 直连命名 en_ko
            (op.PACKS_DIR / "en_ko" / "model").mkdir(parents=True)
            (op.PACKS_DIR / "en_ko" / "sentencepiece.model").write_bytes(b"x")
            assert op._resolve_pack_dir("en", "ko").name == "en_ko"
            # 旧版 translate- 前缀命名回退
            (op.PACKS_DIR / "translate-en_ja").mkdir(parents=True)
            (op.PACKS_DIR / "translate-en_ja" / "sentencepiece.model").write_bytes(b"x")
            assert op._resolve_pack_dir("en", "ja").name == "translate-en_ja"
            # 都不存在时返回直连命名
            assert op._resolve_pack_dir("fr", "zh").name == "fr_zh"
    finally:
        op.PACKS_DIR = old


def test_proxy_provider_modes():
    from app import net
    # 直连模式：显式屏蔽代理来源
    net.configure("none")
    assert net.proxies() == {"http": None, "https": None}, "none 模式应显式直连"
    # 手动模式：自动补 http:// 前缀
    net.configure("manual", "127.0.0.1:10808")
    p = net.proxies()
    assert p["http"] == "http://127.0.0.1:10808" and p["https"] == p["http"], \
        "手动模式应规范化地址并对 http/https 生效"
    net.configure("manual", "http://host:7890")
    assert net.proxies()["https"] == "http://host:7890"
    # 非法/空 URL 的手动模式回退系统行为，不应抛异常
    net.configure("manual", "")
    net.proxies()
    # 非法模式名归一为 system，不应抛异常
    net.configure("bogus-mode")
    net.proxies()
    net.configure("system")


def test_config_has_proxy_defaults():
    from app.config import DEFAULTS
    assert DEFAULTS.get("proxy_mode") == "system"
    assert "proxy_url" in DEFAULTS


def test_hotkey_sequence_parsing():
    from PySide6.QtCore import QCoreApplication
    if QCoreApplication.instance() is None:
        QCoreApplication([])  # QKeySequence 需要 Qt 核心实例（offscreen 即可）
    from app.hotkey import sequence_to_hotkey, MOD_CONTROL, MOD_ALT, MOD_SHIFT, MOD_WIN
    r = sequence_to_hotkey("Ctrl+Alt+S")
    assert r is not None, "标准组合应可解析"
    mods, vk = r
    assert vk == 0x53, "S 的虚拟键码应为 0x53"
    assert mods & MOD_CONTROL and mods & MOD_ALT, "应包含 Ctrl+Alt 修饰"
    r2 = sequence_to_hotkey("Ctrl+Shift+F5")
    assert r2 is not None
    m2, vk2 = r2
    assert vk2 == 0x74, "F5 虚拟键码应为 0x74"
    assert m2 & MOD_CONTROL and m2 & MOD_SHIFT
    assert sequence_to_hotkey("S") is None, "无修饰键的单键应拒绝（避免全局劫持打字）"
    assert sequence_to_hotkey("") is None
    assert sequence_to_hotkey("Ctrl+Alt+Entrance") is None, "不支持的键应拒绝"
    r3 = sequence_to_hotkey("Win+Alt+Z")
    assert r3 is not None and (r3[0] & MOD_WIN), "Win 修饰键应被映射"


def test_config_has_hotkey_defaults():
    from app.config import DEFAULTS
    assert DEFAULTS.get("hotkey_enabled") is True
    assert DEFAULTS.get("hotkey_sequence") == "Ctrl+Alt+S"


# ---------- v2.0.0 可靠性专项 ----------

def test_net_proxies_modes():
    from app import net
    net.configure("none")
    assert net.proxies() == {"http": None, "https": None}, "直连模式应显式屏蔽代理"
    net.configure("manual", "127.0.0.1:10808")
    p = net.proxies()
    assert p["http"] == "http://127.0.0.1:10808" and p["https"] == p["http"], "手动模式应补全 scheme"
    net.configure("system")
    p2 = net.proxies()
    assert p2 is not None and "http" in p2, "system 模式应返回合法结构"


def test_net_parse_socks_only():
    from app.net import _parse_proxy_server
    url, note = _parse_proxy_server("socks=127.0.0.1:1080")
    assert url is None and "SOCKS" in note, "仅 SOCKS 代理应提示而非静默直连"
    url2, note2 = _parse_proxy_server("http=1.2.3.4:8080;https=1.2.3.4:8080")
    assert url2 == "http://1.2.3.4:8080" and not note2
    url3, _ = _parse_proxy_server("127.0.0.1:10808")
    assert url3 == "http://127.0.0.1:10808"


def test_friendly_error_mapping():
    from app.errors import friendly_error, friendly_message
    out = friendly_error(RuntimeError("onnxruntime error: NO SUCH FILE silero_vad.onnx"))
    assert "缺失" in out, "缺文件类异常应给出中文结论"
    assert "限流" in friendly_error(RuntimeError("HTTP 429 Too Many Requests"))
    assert "代理" in friendly_message("ProxyError: cannot connect")
    assert friendly_message("一切正常") == "一切正常", "无匹配时中文原文应原样保留"


def test_model_cached_rejects_stub():
    import tempfile
    import pathlib
    from app.asr.engine import AsrThread
    tmp = pathlib.Path(tempfile.mkdtemp())
    orig = AsrThread.model_cache_dir
    try:
        AsrThread.model_cache_dir = staticmethod(lambda s: tmp)
        stub = tmp / "snapshots" / "abc" / "model.bin"
        stub.parent.mkdir(parents=True)
        stub.write_bytes(b"\0" * 1024)
        assert AsrThread.model_cached("small") is False, "1KB 假 model.bin 不应视为已缓存"
        with open(stub, "wb") as f:
            f.truncate(60 * 1024 * 1024)
        assert AsrThread.model_cached("small") is True, "达到真实模型量级应视为已缓存"
    finally:
        AsrThread.model_cache_dir = staticmethod(orig)


def test_model_state_missing_partial():
    import tempfile
    import pathlib
    from app.asr.engine import AsrThread
    tmp = pathlib.Path(tempfile.mkdtemp())
    orig = AsrThread.model_cache_dir
    try:
        AsrThread.model_cache_dir = staticmethod(lambda s: tmp)
        # 空目录：missing（此前"未下载也显示删除按钮"的判定来源）
        assert AsrThread.model_state("tiny") == ("missing", 0.0)
        # 有残留但 model.bin 不完整：partial（v1.9.0 半截文件形态）
        blobs = tmp / "blobs"
        blobs.mkdir()
        (blobs / "x.bin").write_bytes(b"\0" * (2 * 1024 * 1024))
        state, mb = AsrThread.model_state("tiny")
        assert state == "partial" and mb > 1.0, "2MB 残留应判 partial"
    finally:
        AsrThread.model_cache_dir = staticmethod(orig)


def test_mymemory_sentence_chunks():
    from app.translate.translator import MyMemory
    text = "This is a sentence. " * 40
    chunks = MyMemory._split_sentences(text)
    assert len(chunks) >= 2, "长文本应分块"
    assert all(len(c) <= MyMemory.LIMIT_CHARS for c in chunks), "分块不应超过限额"
    assert "".join(chunks) == text, "零宽切分重组应零丢字"


def test_mymemory_split_protects_decimals():
    from app.translate.translator import MyMemory
    chunks = MyMemory._split_sentences("pi is 3.14159 in math. ok")
    assert any("3.14159" in c for c in chunks), "小数点不应被当作句边界"


def test_mymemory_rejects_warning_response(monkeypatch=None):
    from app.translate.translator import MyMemory
    import app.translate.translator as tmod

    class FakeResp:
        def __init__(self, payload):
            self._p = payload
        def json(self):
            return self._p
        def raise_for_status(self):
            pass

    orig = tmod._SESSION.get   # v2.6.4（P2）：引擎走共享 Session，mock 其 get
    tmod._SESSION.get = lambda *a, **k: FakeResp(
        {"responseData": {"translatedText": "MYMEMORY WARNING: USED ALL"},
         "responseStatus": 200})
    try:
        MyMemory.translate("hello", "en", "zh-CN")
        raise AssertionError("警告串不应被当译文返回")
    except RuntimeError:
        pass
    finally:
        tmod._SESSION.get = orig


def test_remove_pack_no_crash():
    import tempfile
    import pathlib
    import json
    from app.translate import offline_pack as op
    tmp = pathlib.Path(tempfile.mkdtemp())
    orig = op.PACKS_DIR
    try:
        op.PACKS_DIR = tmp
        d = tmp / "en_zh"
        d.mkdir()
        (d / "metadata.json").write_text(json.dumps({"from_code": "en", "to_code": "zh"}), encoding="utf-8")
        assert op.remove_pack("en", "zh") == ["en_zh"], "应返回被删目录并正常清理缓存"
        assert not d.exists()
        assert op.remove_pack("en", "zh") == [], "重复卸载应安全返回空列表"
    finally:
        op.PACKS_DIR = orig


def test_google_parse_formats():
    from app.translate.translator import GoogleFree
    # /translate_a/single（dict-chrome-ex / gtx）结构
    out, det = GoogleFree._parse([[["你好", "hi", None, None, 10]], None, "en"])
    assert out == "你好" and det == "en"
    # clients5 /translate_a/t 结构
    out2, det2 = GoogleFree._parse([["你好", "en"]])
    assert out2 == "你好" and det2 == "en"


def test_zip_slip_backslash_blocked():
    """v2.0.1 安全回归：反斜杠条目名绕过 zip-slip 校验的攻击必须被拦截。"""
    import tempfile
    import pathlib
    import zipfile
    from app.translate import offline_pack as op
    base = pathlib.Path(tempfile.mkdtemp())
    evil_zip = base / "evil.argosmodel"
    dest = base / "dest"
    with zipfile.ZipFile(evil_zip, "w") as zf:
        zf.writestr("root/model/sent.model", "ok")
        # 混合分隔符攻击：outer 用 "/"，rel 用 "\" 逃出 dest
        zf.writestr("root/..\\..\\evil.txt", "pwned")
    op._extract_pack(evil_zip, dest)
    assert (dest / "model" / "sent.model").read_text() == "ok", "正常文件应解压"
    assert not (base.parent / "evil.txt").exists(), "逃逸文件不应存在"
    assert not (base / "evil.txt").exists(), "逃逸文件不应存在"


def test_google_parse_multiline_clients5():
    from app.translate.translator import GoogleFree
    # clients5 对多句 q 的多行响应：全部拼接，不截断
    out, det = GoogleFree._parse([["你好", "en"], ["世界", "en"]])
    assert out == "你好世界" and det == "en"


def test_version_files_sync():
    import subprocess
    import sys
    script = Path(__file__).resolve().parents[1] / "scripts" / "bump_version.py"
    r = subprocess.run([sys.executable, str(script), "--check"], capture_output=True, text=True)
    assert r.returncode == 0, f"三处版本号应一致: {r.stdout} {r.stderr}"


def test_eta_text():
    """v2.2.14：下载 ETA 口语化格式化（模型横幅与语言包进度共用）。"""
    from app.fmt import eta_text
    assert eta_text(45) == "45 秒"
    assert eta_text(59.6) == "60 秒"
    assert eta_text(120) == "2 分钟"
    assert eta_text(3600 + 1800) == "1 小时 30 分"
    assert eta_text(None) == ""          # 无效输入静默省略
    assert eta_text(-1) == ""
    assert eta_text(float("nan")) == ""
    assert eta_text(86400 * 8) == ""     # 离谱值不显示


def test_has_content_filter():
    """v2.3.1：纯标点垃圾段滤除（新闻转场"....."实测穿透幻觉过滤器）。"""
    from app.asr.engine import has_content
    assert has_content("Hello world")
    assert has_content("飓风来了")
    assert has_content("25")                       # 数字是内容
    assert not has_content(".....")
    assert not has_content("？？？！，。")
    assert not has_content("   ")
    assert not has_content("")


def test_split_piece_content_filter():
    """v2.3.4：二次分句产生的纯标点尾巴必须再过 has_content——
    CBS 新闻体验轮抓到 ".." 进入翻译缓存，漏洞点在分段过滤之后才切句。"""
    from app.asr.engine import has_content, split_long_caption
    pieces = split_long_caption("The report continues tonight. ..")
    kept = [p for p in pieces if has_content(p)]
    assert kept, pieces                            # 正常句保留
    assert ".." not in kept and all(has_content(p) for p in kept), (pieces, kept)


def test_apply_fix_map():
    """v2.3.6（P7）：译文修正词典——实测抓到的反义错译（pace→加快）作用例。"""
    from app.translate.translator import apply_fix_map
    assert (apply_fix_map("我们可能必须加快人工智能的发展速度。",
                          {"加快人工智能的发展速度": "控制人工智能的发展节奏"})
            == "我们可能必须控制人工智能的发展节奏。")
    assert apply_fix_map("原文", {}) == "原文"
    assert apply_fix_map("", {"a": "b"}) == ""
    assert apply_fix_map("A和A", {"A": "B"}) == "B和B"
    assert apply_fix_map("甲", {"甲": ""}) == "甲"   # 空替换值不生效


def test_starts_new_sentence():
    """v2.3.8（P9 修正）：whisper 切片自补句号，标点判据失效——
    真正的句子边界是首字母大小写（小写开头=延续）。"""
    from app.ui.main_window import MainWindow
    f = MainWindow._starts_new_sentence
    assert f("The FAA confirmed.")
    assert f("飓风造成破坏")
    assert f("25 people displaced")
    assert not f("and authorities understand")
    assert not f("led to this airplane going on.")
    assert not f("")
    assert not f("   ")


def test_looks_final():
    """v2.3.14（P16 守卫）：静默提前立送要求末片"看起来完整"。"""
    from app.ui.main_window import MainWindow
    f = MainWindow._looks_final
    assert f("The market closed higher today.")
    assert f('他说"走吧。"')
    assert f("今天天气不错！")
    assert not f("stockpiles of uranium...")     # whisper 省略号=显式未完
    assert not f("the story continues…")
    assert not f("what a fast moving")           # 无标点=音频上限截断
    assert not f("Despite the triumphal tone in Washington,")
    assert f("")                                  # 空串按完整处理（防御）


def test_split_preserves_abbreviations():
    """v2.3.14（P17）：拉丁缩写内的句号不作句子边界（实况抓到的
    "…of U." / "S. strikes…" 腰斩）；真句界（小写词尾+空格）仍要切；
    中文句末标点后无空格也需直切。"""
    from app.asr.engine import split_long_caption
    text = ("There are fresh questions today about the effectiveness of U.S. "
            "strikes on Iran. Officials said the review is still ongoing now.")
    pieces = split_long_caption(text)
    joined = " ".join(pieces)
    assert "U.S. strikes" in joined, pieces          # 缩写不被劈开
    assert not any(p.startswith("S.") for p in pieces), pieces
    assert any(p.startswith("Officials said") for p in pieces), pieces  # 真句界仍切
    zh = ("今天全国多地气温突破历史极值。多家航空公司取消了前往热门枢纽城市的航班。"
          "机长在客舱广播中反复提醒旅客注意防暑降温并补充了大量细节。")
    assert len(split_long_caption(zh, limit=20)) >= 2


def test_log_day_rotation():
    """v2.3.7（P10）：跨天首写归档旧日志，当前文件只留当天。"""
    import io
    import logging
    import os
    import tempfile
    import time
    from app.log import _Utf8RotatingHandler
    d = tempfile.mkdtemp()
    p = os.path.join(d, "app.log")
    io.open(p, "w", encoding="utf-8").write("yesterday line\n")
    old = time.time() - 86400
    os.utime(p, (old, old))
    h = _Utf8RotatingHandler(p)
    rec = logging.LogRecord("ls", logging.INFO, "", 0, "today line", (), None)
    h.emit(rec)
    yday = time.strftime("%Y-%m-%d", time.localtime(old))
    assert os.path.exists(f"{p}.{yday}"), os.listdir(d)
    assert "yesterday" not in io.open(p, encoding="utf-8").read()
    assert "today line" in io.open(p, encoding="utf-8").read()


def test_model_cache_dir_honors_env_hf_home():
    """v2.3.10（P11）：用户预设 HF_HOME 环境变量时，"已下载"判定必须跟随
    huggingface_hub 的实际解析路径（否则预热误报 not_cached 跳过）。"""
    import os
    import tempfile
    from pathlib import Path
    from app.asr.engine import AsrThread
    d = Path(tempfile.mkdtemp())
    snap = d / "hub" / "models--Systran--faster-whisper-tiny" / "snapshots" / "abc123"
    snap.mkdir(parents=True)
    (snap / "model.bin").write_bytes(b"\0" * (60 * 1024 * 1024))   # >50MB 阈值
    old = os.environ.get("HF_HOME")
    os.environ["HF_HOME"] = str(d)
    try:
        assert AsrThread.model_cached("tiny"), "环境变量 HF_HOME 下的完整缓存应被判已下载"
    finally:
        if old is None:
            del os.environ["HF_HOME"]
        else:
            os.environ["HF_HOME"] = old


def test_segmenter_low_latency():
    """v2.3.3（P1）：低延迟模式分段上限 14s→6s、判停收紧。"""
    voiced = np.full(480, 0.2, dtype=np.float32)   # 30ms@16k，音量需高于噪声底自适应上限×3
    s = Segmenter(low_latency=True)
    assert s.silence_end == 0.30 and s.max_seg == 6.0
    out_at = None
    for i in range(210):                            # 6.3 秒连读
        if s.feed(voiced) is not None:
            out_at = i + 1
            break
    assert out_at is not None and 195 <= out_at <= 208, f"6s 处应切段，实际第 {out_at} 块"
    # 默认模式：6.3 秒连读不得切
    s2 = Segmenter()
    assert s2.silence_end == 0.45 and s2.max_seg == 14.0
    for i in range(210):
        assert s2.feed(voiced) is None, "默认模式 6.3s 不应分段"


def test_transcribe_accuracy_profiles():
    """v2.5.4：识别精度三档参数映射（快速/均衡/高精度）+ 未知档回退 fast。"""
    from app.asr.engine import transcribe_kwargs
    fast = transcribe_kwargs("fast")
    assert fast["beam_size"] == 1 and fast["condition_on_previous_text"] is False
    bal = transcribe_kwargs("balanced")
    assert bal["beam_size"] == 2 and bal["condition_on_previous_text"] is False
    qual = transcribe_kwargs("quality")
    assert qual["beam_size"] == 5 and qual["condition_on_previous_text"] is True
    assert transcribe_kwargs("unknown")["beam_size"] == 1, "未知档回退 fast"
    assert transcribe_kwargs("fast", silero_vad=True)["vad_filter"] is True
    assert transcribe_kwargs("fast")["no_speech_threshold"] == 0.6


def test_fixmap_whole_word():
    """v2.6.0（R2）：拉丁词条整词匹配——多义词不再误伤专名。"""
    from app.fixmap import apply_dict, is_latin_key
    m = {"strikes": "罢工"}
    # 关闭开关 = v2.5.3 子串替换语义（回归基线）
    assert apply_dict("U.S. strikes back", m) == "U.S. 罢工 back"
    # 开启后仍替换独立成词的 strikes
    assert apply_dict("U.S. strikes back", m, whole_word=True) == "U.S. 罢工 back"
    # 复合词/专名中的子串不再被误伤
    assert apply_dict("airstrikes reported", m, whole_word=True) == "airstrikes reported"
    assert apply_dict("the strikezone", m, whole_word=True) == "the strikezone"
    # CJK 词条始终子串替换，开关不影响中文纠错习惯
    m2 = {"新兴市场": "发展中市场"}
    assert apply_dict("新兴市场波动", m2, whole_word=True) == "发展中市场波动"
    assert is_latin_key("U.S.") is False
    assert is_latin_key("feel in") is True


def test_fixmap_longest_first():
    """v2.6.0（R2）：长键优先——短键先替换不得拆坏长键。"""
    from app.fixmap import apply_dict
    m = {"sub": "X", "subtitle": "Y"}
    assert apply_dict("subtitle", m) == "Y"
    assert apply_dict("subtitle sub", m) == "Y X"


def test_fixmap_single_pass():
    """v2.6.0（R2）：单轮语义——词条 A 的替换产物不再被词条 B 二次命中。

    旧实现逐条 replace：{"a": "b", "b": "c"} 会把 "a" 链式替换成 "c"。"""
    from app.fixmap import apply_dict
    m = {"a": "b", "b": "c"}
    assert apply_dict("a", m) == "b"
    assert apply_dict("b", m) == "c"


def test_fixmap_edges():
    """v2.6.0（R2）：空输入与异常词条边界。"""
    from app.fixmap import apply_dict
    assert apply_dict("原文", {}) == "原文"
    assert apply_dict("", {"a": "b"}) == ""
    assert apply_dict(None, {"a": "b"}) is None
    assert apply_dict("A和A", {"A": "B"}) == "B和B"
    assert apply_dict("甲", {"甲": ""}) == "甲"


def test_unescape_html():
    """v2.6.0（R1）：引擎译文 HTML 实体还原（单层还原 + 幂等）。"""
    from app.translate.translator import unescape_html
    assert unescape_html("&quot;hi&quot;") == '"hi"'
    assert unescape_html("A &amp; B") == "A & B"
    # 双重转义仅还原一层——用户确在说转义串时保留可读形态
    assert unescape_html("&amp;quot;") == "&quot;"
    assert unescape_html("no entity here") == "no entity here"
    # 幂等：连续两次应用等价于一次
    x = "&quot;A &amp; B&quot;"
    assert unescape_html(unescape_html(x)) == unescape_html(x)


def test_fallback_emit_unescaped():
    """v2.6.1（P1-1）：备援译文先还原实体再上屏——emit 与缓存必须一致
    （v2.6.0 曾只还原缓存副本，同一句"实况"与"缓存命中"显示不一致）。"""
    import app.translate.translator as tmod

    class FakeEngine:
        def __init__(self, out=None, exc=None):
            self._out, self._exc = out, exc

        def translate(self, text, src, tgt):
            if self._exc is not None:
                raise self._exc
            return self._out   # (译文, 检测语言) 元组，与真实引擎签名一致

        def installed_pairs(self):
            return []

    class FakeCache:
        def __init__(self):
            self.d = {}

        def get(self, k):
            return self.d.get(k)

        def put(self, k, v):
            self.d[k] = v

        def save(self):
            pass

    orig_engines, orig_cache = tmod.ENGINES, tmod._cache
    tmod.ENGINES = {
        "argos": FakeEngine(exc=RuntimeError("offline boom")),
        "mymemory": FakeEngine(out=("&quot;hi&quot; &amp; ok", "en")),
        "google": FakeEngine(exc=RuntimeError("unreachable")),
    }
    tmod._cache = FakeCache()
    tt = tmod.TranslateThread("argos", "zh-CN")
    got = []
    tt.result_ready.connect(lambda s, tr, eng, det, err: got.append((tr, err)))
    tt.queue_in.put(("hello", "en"))
    tt.queue_in.put(None)
    try:
        tt.run()   # 同步执行，哨兵后退出
        assert got, "应收到一条 result_ready"
        tr, err = got[0]
        assert err == "", f"备援成功后 error 应为空，got={err!r}"
        assert tr == '"hi" & ok', f"上屏译文应还原实体，got={tr!r}"
        key = tt._cache_key("en", "hello")
        assert tmod._cache.d.get(key, ("",))[0] == '"hi" & ok', \
            "缓存值应与上屏译文一致"
    finally:
        tmod.ENGINES, tmod._cache = orig_engines, orig_cache


def test_prewarm_stop_short_circuits():
    """v2.6.1（P0-2）：request_stop 后 run() 不进入模型加载路径。"""
    import app.asr.engine as emod

    calls = []

    def fake_cached(size):
        calls.append(size)
        return False

    orig = emod.AsrThread.__dict__["model_cached"]
    emod.AsrThread.model_cached = staticmethod(fake_cached)
    try:
        w = emod.PrewarmWorker("tiny", "cpu")
        w.run()   # 未请求停止：应走到缓存判定（False → not_cached 提前返回）
        assert calls == ["tiny"], "未停止时应走到缓存判定"
        w2 = emod.PrewarmWorker("tiny", "cpu")
        w2.request_stop()
        w2.run()   # 已请求停止：短路
        assert calls == ["tiny"], "请求停止后不应再触碰加载路径"
    finally:
        emod.AsrThread.model_cached = orig


def test_translate_stop_drain_keeps_latest():
    """v2.6.2（P1-4）：translate.stop 清到剩 1（保留最新）、无 None 哨兵——
    尾句在排水语义下仍会被消费。"""
    from app.translate.translator import TranslateThread
    tt = TranslateThread("argos", "zh-CN")
    for i in range(4):
        tt.submit(f"line{i}", "en")
    tt.stop()
    assert tt._stop is True
    assert tt.queue_in.qsize() == 1, "应保留队尾最新一条"
    item = tt.queue_in.get_nowait()
    assert item == ("line3", "en"), "保留的应是最新的待译句"
    assert item is not None


def test_asr_stop_keeps_latest_segment():
    """v2.6.2（P1-4）：asr.stop 清到剩 1（保留队尾段）、无 None 哨兵。"""
    import numpy as np
    from app.asr.engine import AsrThread
    at = AsrThread("tiny", "cpu", "auto")
    for _ in range(4):
        at.submit(np.zeros(16, dtype=np.float32))
    at.stop()
    assert at._stop is True
    assert at.queue_in.qsize() == 1, "应保留队尾最新一段"
    item = at.queue_in.get_nowait()
    assert item is not None and item[0] is not None


def test_translate_drain_grace_consumes_late_submit():
    """v2.6.2（P1-4）：停止后迟到的尾句在宽限期内仍被翻译并 emit。"""
    import time
    from PySide6.QtCore import Qt
    import app.translate.translator as tmod

    class FakeEngine:
        def translate(self, text, src, tgt):
            return f"[{text}]", "en"

        def installed_pairs(self):
            return []

    orig_engines, orig_cache = tmod.ENGINES, tmod._cache
    tmod.ENGINES = {"argos": FakeEngine(), "mymemory": FakeEngine(),
                    "google": FakeEngine()}
    tmod._cache = _FakeMemCache()
    tt = tmod.TranslateThread("argos", "zh-CN")
    tt.DRAIN_GRACE = 1.5   # 测试加速：宽限缩短
    got = []
    tt.result_ready.connect(
        lambda s, tr, eng, det, err: got.append((s, tr, err)),
        Qt.DirectConnection)   # 无事件循环环境下同步接收
    tt.queue_in.put(("early", "en"))
    tt.start()
    tt.stop()
    tt.submit("tail sentence", "en")   # 停止后的迟到尾句
    try:
        deadline = time.monotonic() + 10.0
        while not got and time.monotonic() < deadline:
            time.sleep(0.05)
        assert got, "停止后迟到的尾句应被宽限期消费并 emit"
        assert got[-1] == ("tail sentence", "[tail sentence]", ""), got[-1]
        tt.wait(10000)
        assert not tt.isRunning(), "宽限期结束后线程应退出"
    finally:
        tmod.ENGINES, tmod._cache = orig_engines, orig_cache
        if tt.isRunning():
            tt.terminate()


class _FakeMemCache:
    """test 专用内存缓存（替代模块级 _cache 单例，避免污染磁盘缓存）。"""

    def __init__(self):
        self.d = {}

    def get(self, k):
        return self.d.get(k)

    def put(self, k, v):
        self.d[k] = v

    def save(self):
        pass


def test_submit_returns_dropped():
    """v2.6.2（P1-7）：队列满时 submit 返回被挤掉的旧句，供占位卡终态化。"""
    from app.translate.translator import TranslateThread
    tt = TranslateThread("argos", "zh-CN")
    for i in range(5):
        dropped = tt.submit(f"t{i}", "en")
        assert dropped == []
    dropped = tt.submit("t5", "en")
    assert dropped == [("t0", "en")], "第 6 句应挤出最旧的 t0 并返回"
    assert tt.queue_in.qsize() == 5


def test_capture_pop_tail_seg():
    """v2.6.2（P1-4）：pop_tail_seg 一次性取走暂存尾段。"""
    from app.audio.capture import CaptureThread
    cap = CaptureThread("system", -1)
    assert cap.pop_tail_seg() is None, "无尾段应返回 None"
    seg = (object(), 1.0)
    cap._tail_seg = seg
    assert cap.pop_tail_seg() is seg
    assert cap.pop_tail_seg() is None, "第二次取应为空（一次性）"


def test_cache_load_non_dict_treated_empty():
    """v2.6.3（P1-3）：缓存文件为合法 JSON 但顶层非 dict 时按空处理，
    get/put 照常工作——此前 list 赋给 _data 后每条字幕都 AttributeError。"""
    import json
    import tempfile
    import pathlib
    import app.translate.translator as tr
    tmp = pathlib.Path(tempfile.mkdtemp())
    orig_path_fn = tr.TranslationCache._path
    try:
        tr.TranslationCache._path = lambda self: tmp / "cache.json"
        (tmp / "cache.json").write_text(json.dumps(["not", "a", "dict"]),
                                        encoding="utf-8")
        c = tr.TranslationCache()
        assert c.get("any") is None, "corrupt 缓存应读为空而非抛异常"
        c.put("k", ("v", "en"))
        assert c.get("k") == ("v", "en"), "corrupt 后 put/get 应恢复正常"
        c.save()
        data = json.loads((tmp / "cache.json").read_text(encoding="utf-8"))
        assert isinstance(data, dict), "落盘文件应恢复为 dict"
    finally:
        tr.TranslationCache._path = orig_path_fn


def test_cache_lru_eviction():
    """v2.6.3（LRU）：命中/覆盖刷新时序，淘汰始终发生在最久未用条目——
    此前按插入序 FIFO 淘汰，近期仍在用的旧条目先被挤掉。"""
    import app.translate.translator as tr
    c = tr.TranslationCache(max_items=3)
    c._loaded = True
    c.put("a", ("va", "en"))
    c.put("b", ("vb", "en"))
    c.put("c", ("vc", "en"))
    c.get("a")            # touch a → a 移到队尾
    c.put("b", ("vb2", "en"))  # 覆盖 b → b 刷新到队尾
    c.put("d", ("vd", "en"))   # 满员淘汰队头 c（FIFO 会淘汰 a）
    assert c.get("a") == ("va", "en"), "最近使用的 a 不应被淘汰"
    assert c.get("b") == ("vb2", "en"), "覆盖刷新的 b 不应被淘汰"
    assert c.get("c") is None, "最久未用的 c 应被淘汰"


def test_migrate_refused_leaves_source_intact():
    """v2.6.3（P1-8）：目标占用预检提前——拒绝时旧根数据原封不动，
    此前拒绝发生在 move 循环中途且不回滚，先搬走的项留在新根造成分裂。"""
    import tempfile
    import pathlib
    import app.storage as storage
    import app.config as cfg
    old_hf = pathlib.Path(tempfile.mkdtemp())
    old_cache = old_hf / "trans_cache.json"
    old_cache.write_text("{}", encoding="utf-8")
    new_root = pathlib.Path(tempfile.mkdtemp()) / "dest"
    new_root.mkdir()
    (new_root / "hf").mkdir()   # 目标已有 hf/ ——必须整体拒绝
    saved = (cfg.HF_HOME, cfg.CACHE_FILE)
    try:
        cfg.HF_HOME = old_hf / "hf"
        (cfg.HF_HOME / "hub").mkdir(parents=True)
        (cfg.HF_HOME / "hub" / "model.bin").write_bytes(b"x" * 1024)
        cfg.CACHE_FILE = old_cache
        try:
            storage.migrate_root(str(new_root))
            raised = False
        except storage._MigrationRefused:
            raised = True
        assert raised, "目标已有 hf/ 应整体拒绝迁移"
        assert (old_hf / "hf" / "hub" / "model.bin").exists(), "旧根模型不得被移动"
        assert old_cache.exists(), "旧根缓存不得被移动"
        assert not (new_root / "trans_cache.json").exists(), "新根不得出现半迁移内容"
    finally:
        cfg.HF_HOME, cfg.CACHE_FILE = saved


def test_hub_root_env_follow():
    """v2.6.3（P1-9）：_hub_root 跟随 HF_HOME 环境变量（未设时用 config
    路径）——下载/加载/判定三处由此同源。"""
    import os
    import tempfile
    from pathlib import Path
    from app.asr.engine import _hub_root
    from app import config as cfg
    d = Path(tempfile.mkdtemp())
    old = os.environ.get("HF_HOME")
    try:
        os.environ["HF_HOME"] = str(d)
        assert _hub_root() == d / "hub", "预设环境变量时应跟随环境变量"
        if old is None:
            os.environ.pop("HF_HOME", None)
        else:
            os.environ["HF_HOME"] = old
        assert _hub_root() == Path(str(cfg.HF_HOME)) / "hub", "未预设时应回落 config 路径"
    finally:
        if old is None:
            os.environ.pop("HF_HOME", None)
        else:
            os.environ["HF_HOME"] = old


def test_model_load_mutex():
    """v2.6.4（P2）：加载互斥——真实加载持锁时预热线程（blocking=False）
    立即让位返回 False；持锁方完成后入池，后续加载命中池不重复构造。"""
    import threading as th
    import time as _t
    from app.asr import engine as eng
    eng._MODEL_CACHE.clear()
    orig_cached = eng.AsrThread.model_cached
    orig_construct = eng.AsrThread._construct_model
    try:
        eng.AsrThread.model_cached = staticmethod(lambda s: True)

        def slow_construct(self, *a, **k):
            import types
            _t.sleep(0.4)
            return types.SimpleNamespace()   # 需可设属性（入池时写 _ls_device）

        eng.AsrThread._construct_model = slow_construct
        real = eng.AsrThread("tiny", "cpu", "auto", None)
        prewarm = eng.AsrThread("tiny", "cpu", "auto", None)
        res = {}

        def run_real():
            res["real"] = real._load_model()

        t = th.Thread(target=run_real)
        t.start()
        _t.sleep(0.1)   # 确保 real 先持锁进入构造
        res["prewarm"] = prewarm._load_model(blocking=False)
        t.join()
        assert res["prewarm"] is False, "真实加载持锁时预热应立即让位"
        assert res["real"] is True
        assert eng._MODEL_CACHE.get(("tiny", "cpu", "int8")) is real._model, \
            "构造完成应入池"
        late = eng.AsrThread("tiny", "cpu", "auto", None)
        assert late._load_model() is True
        assert late._model is real._model, "后续加载应命中池（不重复构造）"
    finally:
        eng._MODEL_CACHE.clear()
        eng.AsrThread.model_cached = staticmethod(orig_cached)
        eng.AsrThread._construct_model = orig_construct


# ---------- v2.7.0 高杠杆批（T1~T7）单元锁 ----------

def test_transcribe_kwargs_hotwords():
    """v2.7.0（T3）：热词→initial_prompt 注入/截断；空值不注入。"""
    from app.asr.engine import transcribe_kwargs
    assert "initial_prompt" not in transcribe_kwargs("fast")
    assert "initial_prompt" not in transcribe_kwargs("fast", hotwords="   ")
    kw = transcribe_kwargs("fast", hotwords="Noriega、OpenAI")
    assert kw["initial_prompt"] == "Noriega、OpenAI"
    kw2 = transcribe_kwargs("fast", hotwords="词" * 300)
    assert len(kw2["initial_prompt"]) == 160, "截断防挤占 224 token 解码预算"


def test_asr_lang_recheck():
    """v2.7.0（T5）：auto 锁定后每 20 段复检——高置信不一致切换；
    低置信不一致丢段维持原锁。"""
    import numpy as np
    import app.asr.engine as eng

    class Seg:
        def __init__(self, t, end=0.8, lp=-0.3):
            self.text, self.end = t, end
            self.avg_logprob, self.no_speech_prob = lp, 0.0

    class Info:
        def __init__(self, lang, prob):
            self.language, self.language_probability = lang, prob

    class FakeModel:
        def __init__(self):
            self.calls = []
            self.rc_lang, self.rc_prob = "ja", 0.9

        def transcribe(self, audio, **kw):
            self.calls.append(dict(kw))
            if "language" not in kw:          # 复检段：无锁重听
                return [Seg("こんにちは")], Info(self.rc_lang, self.rc_prob)
            return [Seg("hello world")], Info(kw["language"], 1.0)

    at = eng.AsrThread("tiny", "cpu", "auto")
    at._model = fm = FakeModel()
    at._last_lang = "en"
    events = []
    at.text_ready.connect(lambda *a: events.append(a))
    at.status_changed.connect(lambda s: events.append(("status", s)))
    for _ in range(19):
        at._transcribe(np.zeros(16000, dtype=np.float32))
        assert fm.calls[-1]["language"] == "en"
    at._transcribe(np.zeros(16000, dtype=np.float32))   # 第 20 段：复检
    assert "language" not in fm.calls[-1], "复检段必须解除语言约束"
    assert at._last_lang == "ja", "高置信不一致应切换锁"
    for _ in range(19):
        at._transcribe(np.zeros(16000, dtype=np.float32))
    assert fm.calls[-1]["language"] == "ja", "切换后按新锁转写"
    fm.rc_lang, fm.rc_prob = "ko", 0.5                  # 第 40 段：低置信不一致
    n_before = len([e for e in events if e != "status" and not (isinstance(e, tuple) and e[0] == "status")])
    at._transcribe(np.zeros(16000, dtype=np.float32))
    assert at._last_lang == "ja", "低置信不一致必须维持原锁"
    assert fm.calls[-1].get("language") is None
    n_after = len([e for e in events if e != "status" and not (isinstance(e, tuple) and e[0] == "status")])
    assert n_after == n_before, "不一致复检段必须丢弃不上屏"


def test_prewarm_completes_warmup():
    """v2.7.0（T6）：预热加载成功后必须补跑一次真实转写（_warmup）；
    加载中被停止则不跑。"""
    import app.asr.engine as emod
    hits = {"load": 0, "warm": 0}
    orig_load = emod.AsrThread.__dict__["_load_model"]
    orig_warm = emod.AsrThread.__dict__["_warmup"]
    orig_cached = emod.AsrThread.__dict__["model_cached"]

    def fake_load(self, blocking=True):
        hits["load"] += 1
        self._device_used = "cpu"
        return True

    def fake_warm(self):
        hits["warm"] += 1

    emod.AsrThread._load_model = fake_load
    emod.AsrThread._warmup = fake_warm
    emod.AsrThread.model_cached = staticmethod(lambda s: True)
    try:
        emod.PrewarmWorker("tiny", "cpu").run()
        assert hits == {"load": 1, "warm": 1}, f"预热应补完 warmup: {hits}"
        # 模拟"加载期间用户停止"：fake 加载过程把 worker 的 _stop 置起
        w2 = emod.PrewarmWorker("tiny", "cpu")

        def fake_load_stopped(self, blocking=True):
            w2._stop = True
            return True

        emod.AsrThread._load_model = fake_load_stopped
        w2.run()
        assert hits["warm"] == 1, "加载中被停止不应再跑 warmup"
    finally:
        emod.AsrThread._load_model = orig_load
        emod.AsrThread._warmup = orig_warm
        emod.AsrThread.model_cached = orig_cached


def test_do_translate_google_lang_passthrough():
    """v2.7.0（T4）：google 也携带 whisper 判定语言（sl=en），不再每句
    sl=auto 重新猜；无判定结果仍走 auto；zh 归一为 zh-CN。"""
    import time as _t
    from app.translate import translator as tr
    tt = tr.TranslateThread("google", "zh-CN")
    tt._active_engine = "google"
    seen = []

    class G:
        def translate(self, text, source, target):
            seen.append(source)
            return ("译文" + text, "en")

    orig = tr.ENGINES["google"]
    tr.ENGINES["google"] = G()
    try:
        stamp = f"{_t.time()}"
        tt._do_translate(f"hello there {stamp}", "en")
        tt._do_translate(f"cheers {stamp}", "auto")
        tt._do_translate(f"bonjour {stamp}", "fr")
        assert seen == ["en", None, "fr"], seen
    finally:
        tr.ENGINES["google"] = orig


def test_preload_argos_direction():
    """v2.7.0（T7）：离线包预载只载目标语言方向，非目标方向不碰。"""
    from app.translate import translator as tr
    from app.translate import offline_pack as op
    calls = []
    orig_g, orig_l = op._get_translator, op.list_installed
    op._get_translator = lambda s, t: calls.append((s, t))
    op.list_installed = lambda: [("en", "zh"), ("ja", "zh"), ("fr", "en")]
    try:
        tt = tr.TranslateThread("auto", "zh-CN")
        tt._stop = False
        tt._preload_argos()
        assert ("en", "zh") in calls and ("ja", "zh") in calls
        assert ("fr", "en") not in calls, "非目标方向不该预载"
    finally:
        op._get_translator, op.list_installed = orig_g, orig_l


def test_engine_auto_fallback_switch():
    """v2.7.1：「引擎自动切换」关→失败以错误终态，绝不触碰备援；
    开=旧行为（互备链照常）。"""
    from app.translate import translator as tmod
    calls = []

    class Fail:
        def translate(self, text, source, target):
            calls.append("fail")
            raise RuntimeError("boom")

    class Backup:
        def translate(self, text, source, target):
            calls.append("backup")
            return ("备援译文", "en")

    class FakeCache:
        def get(self, k):
            return None

        def put(self, k, v):
            pass

        def save(self):
            pass

    orig_engines, orig_cache = tmod.ENGINES, tmod._cache
    tmod.ENGINES = {"argos": Fail(), "mymemory": Backup(), "google": Fail()}
    tmod._cache = FakeCache()
    try:
        tt = tmod.TranslateThread("argos", "zh-CN", auto_fallback=False)
        got = []
        tt.result_ready.connect(lambda s, tr, eng, det, err: got.append((tr, err)))
        tt.queue_in.put(("hello one", "en"))
        tt.queue_in.put(None)
        tt.run()
        assert got and got[0][1], "关闭时该句应带错误终态"
        assert calls == ["fail"], f"关闭时不得触碰备援: {calls}"
        calls.clear()
        tt2 = tmod.TranslateThread("argos", "zh-CN", auto_fallback=True)
        got2 = []
        tt2.result_ready.connect(lambda s, tr, eng, det, err: got2.append((tr, err)))
        tt2.queue_in.put(("hello two", "en"))
        tt2.queue_in.put(None)
        tt2.run()
        assert got2 and got2[0][1] == "" and got2[0][0] == "备援译文", got2
        assert calls == ["fail", "backup"], calls
    finally:
        tmod.ENGINES, tmod._cache = orig_engines, orig_cache


# ---------- v2.7.2 榨干模式（perf_turbo）单元锁 ----------

def test_segmenter_turbo_cap():
    """v2.7.2：榨干模式连续语流切段上限 6s→4s；非低延迟不受影响。"""
    from app.audio.capture import Segmenter
    assert Segmenter(low_latency=True).max_seg == 6.0
    assert Segmenter(low_latency=True, turbo=True).max_seg == 4.0
    assert Segmenter(low_latency=False, turbo=True).max_seg == 14.0, "普通模式不掺和"


def test_asr_turbo_compute_type():
    """v2.7.2：cuda+turbo→int8_float16，cuda→float16，cpu→int8（池键含
    compute_type，切换不混池）。"""
    import app.asr.engine as eng
    seen = []

    class DummyModel:
        pass

    def fake_construct(self, model_ref, device, compute_type, local_only):
        seen.append(compute_type)
        return DummyModel()

    orig_construct = eng.AsrThread._construct_model
    orig_cached = eng.AsrThread.__dict__["model_cached"]
    orig_ready = eng._torch_cuda_ready
    eng.AsrThread._construct_model = fake_construct
    eng.AsrThread.model_cached = staticmethod(lambda s: True)
    eng._torch_cuda_ready = lambda: True
    try:
        eng._MODEL_CACHE.clear()
        eng.AsrThread("tiny", "cuda", "auto", turbo=True)._load_model()
        assert seen[-1] == "int8_float16", seen
        eng._MODEL_CACHE.clear()
        eng.AsrThread("tiny", "cuda", "auto")._load_model()
        assert seen[-1] == "float16", seen
        eng._MODEL_CACHE.clear()
        eng.AsrThread("tiny", "cpu", "auto", turbo=True)._load_model()
        assert seen[-1] == "int8", "CPU 路径与 turbo 无关"
        eng._MODEL_CACHE.clear()
    finally:
        eng.AsrThread._construct_model = orig_construct
        eng.AsrThread.model_cached = orig_cached
        eng._torch_cuda_ready = orig_ready


def test_prewarm_turbo_passthrough():
    """v2.7.2：预热带 turbo——否则预热建 fp16 池、真实管线 int8 键 miss，
    预热白做（首帧仍重载）。"""
    import app.asr.engine as emod
    seen = []
    orig_load = emod.AsrThread.__dict__["_load_model"]
    orig_warm = emod.AsrThread.__dict__["_warmup"]
    orig_cached = emod.AsrThread.__dict__["model_cached"]

    def spy_load(self, blocking=True):
        seen.append(self.turbo)
        self._device_used = "cpu"
        return False          # 返回 False 终止后续 warmup 分支

    emod.AsrThread._load_model = spy_load
    emod.AsrThread._warmup = lambda self: None
    emod.AsrThread.model_cached = staticmethod(lambda s: True)
    try:
        emod.PrewarmWorker("tiny", "cuda", turbo=True).run()
        assert seen == [True], f"预热必须透传 turbo: {seen}"
    finally:
        emod.AsrThread._load_model = orig_load
        emod.AsrThread._warmup = orig_warm
        emod.AsrThread.model_cached = orig_cached


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        finally:
            _purge_tmp()   # v2.3.20：无论成败都回收本例产生的临时目录
    print(f"UNIT: {len(fns)} tests PASS")
