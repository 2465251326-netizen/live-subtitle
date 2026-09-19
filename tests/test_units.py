import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# v2.7.4（C-12）：测试家目录隔离——test_cache_persist 等曾直写用户真实
# trans_cache.json（对照 integration 头部已有隔离）；统一指向临时家目录
import tempfile as _tf0
os.environ.setdefault("LIVETRANSLATE_HOME", _tf0.mkdtemp(prefix="ls_ut_home_"))

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
    r = subprocess.run([sys.executable, str(script), "--check"], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
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
    assert item[:2] == ("line3", "en"), "保留的应是最新的待译句"
    # v2.7.6（A）：队列项升级为 (text, lang, spec) 三元组——spec 位区分
    # "推测中间版"与"整句终版"，普通 submit 恒为 False
    assert len(item) == 3 and item[2] is False, "普通提交应带 spec=False 终版标记"
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
    """v2.6.3（P1-3）立的锁，v2.20.2 换代：非 dict 顶层＝**读失败**，不是"空缓存"。

    旧断言「落盘文件应恢复为 dict」把数据丢失路径钉成了契约——内存里 put/get
    正常，但一次攒批落盘就把整份历史缓存 `os.replace` 覆写成近空 dict，且不留
    原件、不记日志（v2.19.2 的"读失败禁写盘"闸门因 `_load_failed` 没置位而绕过）。
    现在保留"不抛 AttributeError、本次会话照常翻译"这半条契约，持久化侧改钉
    "脏原件原样保留"。"""
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
        raw = (tmp / "cache.json").read_text(encoding="utf-8")
        assert json.loads(raw) == ["not", "a", "dict"], \
            f"脏原件被覆写（v2.19.2 数据丢失路径复发）：{raw[:60]}"
        assert (tmp / "cache.json.bad").exists(), "原件未另存留底"
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
    """v2.7.5（R-3）：预载定向——auto（源未知）只预载 en 最常见方向；
    锁定源语言时只预载该方向；非目标/非预期方向不碰。"""
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
        assert calls == [("en", "zh")], "auto 只预载 en 方向"
        calls.clear()
        tt = tr.TranslateThread("argos", "zh-CN", expected_src="ja")
        tt._stop = False
        tt._preload_argos()
        assert calls == [("ja", "zh")], "锁定 ja 只预载 ja 方向"
        calls.clear()
        tt = tr.TranslateThread("argos", "zh-CN", expected_src="fr")
        tt._stop = False
        tt._preload_argos()
        assert calls == [], "fr 无对应方向不预载"
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

def test_translate_close_input_exits_drain():
    """v2.7.3：close_input+队列排空→立即退出（不再空等 15s 宽限）；
    已入队条目仍先翻译（尾句不丢）。"""
    import time as _t
    from app.translate import translator as tmod

    class Eng:
        def translate(self, text, source, target):
            return ("译:" + text, "en")

    class FakeCache:
        def get(self, k):
            return None

        def put(self, k, v):
            pass

        def save(self):
            pass

    orig_engines, orig_cache = tmod.ENGINES, tmod._cache
    tmod.ENGINES = {"argos": Eng(), "mymemory": Eng(), "google": Eng()}
    tmod._cache = FakeCache()
    try:
        tt = tmod.TranslateThread("argos", "zh-CN")
        got = []
        tt.result_ready.connect(lambda s, tr, eng, det, err: got.append(tr))
        tt.queue_in.put(("alpha", "en"))
        tt._stop = True
        tt._stop_at = _t.monotonic()
        tt.close_input()
        t0 = _t.monotonic()
        tt.run()   # 排空后应秒退；旧行为是等满 15s
        dt = _t.monotonic() - t0
        assert got == ["译:alpha"], got
        assert dt < 3.0, f"close_input 后不得等宽限，实耗 {dt:.1f}s"
        # 对照：不关门则走宽限路径（用短宽限验证逻辑分支存在）
        tt2 = tmod.TranslateThread("argos", "zh-CN")
        tt2.DRAIN_GRACE = 0.6
        tt2._stop = True
        tt2._stop_at = _t.monotonic()
        t0 = _t.monotonic()
        tt2.run()   # 队列空+未关门→等满 0.6s 宽限再退
        assert _t.monotonic() - t0 >= 0.5, "未关门必须仍走宽限（尾句转发窗）"
    finally:
        tmod.ENGINES, tmod._cache = orig_engines, orig_cache


def test_segmenter_turbo_cap():
    """v2.7.2：榨干模式连续语流切段上限 6s→4s；非低延迟不受影响。"""
    from app.audio.capture import Segmenter
    assert Segmenter(low_latency=True).max_seg == 6.0
    assert Segmenter(low_latency=True, turbo=True).max_seg == 4.0
    assert Segmenter(low_latency=False, turbo=True).max_seg == 14.0, "普通模式不掺和"


def test_storage_pointer_roundtrip():
    """v2.7.4（A-1）：storage_root 迁移重启往返——指针收敛单键、运行期改动
    写新根、重启读指针后回读新根，不得回滚不得反灌。"""
    import json
    import tempfile
    import shutil
    from pathlib import Path
    import app.config as cfgmod
    root = Path(tempfile.mkdtemp(prefix="ls_a1_"))
    home, newroot = root / "home", root / "new"
    home.mkdir(parents=True)
    saved = (cfgmod.CONFIG_DIR, cfgmod.CONFIG_FILE, cfgmod.POINTER_CONFIG_FILE)
    try:
        cfgmod.CONFIG_DIR = home
        cfgmod.CONFIG_FILE = home / "config.json"
        cfgmod.POINTER_CONFIG_FILE = cfgmod.CONFIG_FILE
        c = cfgmod.Config()
        c.set("max_history", 300)
        c.relocate(newroot)
        c.set("target_lang", "en")            # 运行期改动只写新根
        # 模拟重启：模块全局回到默认根，Config 初始化读指针→relocate→回读新根
        cfgmod.CONFIG_DIR = home
        cfgmod.CONFIG_FILE = home / "config.json"
        c2 = cfgmod.Config()
        assert c2.get("max_history") == 300
        assert c2.get("target_lang") == "en", "运行期改动被指针旧快照回滚（A-1 复发）"
        ptr = json.loads((home / "config.json").read_text(encoding="utf-8-sig"))
        assert set(ptr) == {"storage_root"}, f"指针应只存单键: {sorted(ptr)}"
    finally:
        cfgmod.CONFIG_DIR, cfgmod.CONFIG_FILE, cfgmod.POINTER_CONFIG_FILE = saved
        shutil.rmtree(root, ignore_errors=True)


def test_asr_submit_tail_dedup():
    """v2.7.4（B-2）：停止尾段双通道（排队信号+直塞）身份去重——
    同一 tuple 对象只收一次，不同对象不误伤。"""
    import numpy as np
    from app.asr.engine import AsrThread
    at = AsrThread("tiny", "cpu", "auto")
    seg = (np.zeros(1600, dtype=np.float32), 123.0)
    at.submit(seg)
    at.submit(seg)                              # 同对象第二路 → 吞掉
    assert at.queue_in.qsize() == 1, at.queue_in.qsize()
    seg2 = (np.zeros(1600, dtype=np.float32), 456.0)
    at.submit(seg2)
    assert at.queue_in.qsize() == 2, "正常新段不得被误去重"


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


# ---------- v2.7.6：延迟三件套回归锁 ----------
# 遥测实锤（用户真机会话 n_reco=110）：reco_p50=0.55s、tr_p50=0.06s，
# 而 hold_p50=4.13s——端到端延迟的 87% 是"攒句等下一片冲刷"的结构性等待。
# hold 数值恒等于分段周期（turbo 关 6.04s / turbo 开 4.03~4.43s），三件套
# 分别从"送更早的片"(C)、"更早找到句末"(B)、"不等整句就翻"(A) 三处下手。


def test_segmenter_cap_override():
    """v2.7.6（C）：segment_cap_s 覆盖模式内置上限；0/毒药值回退内置值。"""
    from app.audio.capture import Segmenter, MAX_SEGMENT_S
    assert abs(Segmenter(cap_s=2.5).max_seg - 2.5) < 1e-6
    assert abs(Segmenter(low_latency=True, cap_s=2.5).max_seg - 2.5) < 1e-6
    assert abs(Segmenter(low_latency=True, turbo=True, cap_s=2.5).max_seg - 2.5) < 1e-6
    # 0 = 跟随模式内置值（旧行为逐字不变）
    assert abs(Segmenter(low_latency=True).max_seg - 6.0) < 1e-6
    assert abs(Segmenter(low_latency=True, turbo=True).max_seg - 4.0) < 1e-6
    assert abs(Segmenter().max_seg - MAX_SEGMENT_S) < 1e-6
    assert abs(Segmenter(cap_s=0.0).max_seg - MAX_SEGMENT_S) < 1e-6
    # 毒药值一律回退内置——绝不能得到 0/负数（那会把每块都切成一片）
    for bad in ("2.5s", None, -3, object()):
        s = Segmenter(low_latency=True, cap_s=bad)
        assert abs(s.max_seg - 6.0) < 1e-6, f"毒药值 {bad!r} 应回退内置 6s，实得 {s.max_seg}"


def test_energy_vad_beats_steady_bgm():
    """v2.7.6 实测锁（neural_vad **默认关**的依据；v2.9.0 应**用户要求**恢复
    该开关后默认值仍为关，本锁继续钉住"能量判据基线本来就够好"这一事实，勿再误诊）。

    结论：能量判据的自适应噪声底足以压住持续背景乐——15 个短句（1.5s 语音 +
    0.95s 静音）叠加 rms 0.030 的配乐，能量判据**一句一段、零硬切、段长中位
    贴近真实句长**。机理：threshold = max(noise_floor*3, 0.004) 且噪声底上限
    0.02 → 阈值最高 0.06 > 背景乐 rms 0.03，停顿期照常判静音。
    对照：Silero 神经判定（滞回 0.50 进/0.35 出）在同一素材上段长中位 2.16s
    vs 能量判据 1.50s——滞回多抱了 0.66s 尾音与背景乐，换过去反而更慢。
    另：hold_p50≈分段上限的真因是句长超过上限被强制切段，不是找不到停顿。"""
    from app.audio.capture import Segmenter, TARGET_SR, CHUNK_MS
    from app.config import DEFAULTS
    # v2.9.0 锁：neural_vad 默认必须为关（实测依据见下；要改默认先拿新数据来）
    assert DEFAULTS["neural_vad"] is False, \
        "neural_vad 默认必须 False——实测稳定背景乐下反而黏 0.66s，改默认需重新实测"
    rng = np.random.RandomState(11)
    chunk_n = int(TARGET_SR * CHUNK_MS / 1000)
    n_sent = 15
    seg_s, gap_s, warm_s = 1.5, 0.95, 10.0

    n_total = int(TARGET_SR * (warm_s + n_sent * (seg_s + gap_s)))
    t = np.arange(n_total) / TARGET_SR
    bgm = (0.5 * np.sin(2 * np.pi * 220 * t)
           + 0.35 * np.sin(2 * np.pi * 330 * t)
           + 0.25 * np.sin(2 * np.pi * 440 * t))
    bgm *= 0.5 + 0.5 * np.sin(2 * np.pi * 0.7 * t)          # 起伏，模拟真实配乐
    bgm *= 0.030 / max(1e-9, float(np.sqrt((bgm ** 2).mean())))

    voice = np.zeros(n_total, dtype=np.float32)
    placed, pos = 0, int(TARGET_SR * warm_s)   # 前 10s 纯背景乐：先让噪声底建立（等同视频开播）
    seg_len, gap = int(TARGET_SR * seg_s), int(TARGET_SR * gap_s)
    while pos + seg_len < n_total and placed < n_sent:
        x = np.arange(seg_len) / TARGET_SR
        voice[pos:pos + seg_len] = (np.sin(2 * np.pi * 180 * x) * 0.2
                                    + rng.randn(seg_len) * 0.02).astype(np.float32)
        pos += seg_len + gap
        placed += 1
    assert placed == n_sent, placed
    mixed = (voice + bgm).astype(np.float32)

    seg = Segmenter(low_latency=True, turbo=True, cap_s=4.0)
    out = []
    for i in range(0, len(mixed) - chunk_n + 1, chunk_n):
        r = seg.feed(mixed[i:i + chunk_n])
        if r is not None:
            out.append(len(r) / TARGET_SR)
    tail = seg.flush()
    if tail is not None:
        out.append(len(tail) / TARGET_SR)

    arr = np.array(out)
    hard = int((arr >= 3.82).sum())
    assert hard == 0, f"有背景乐也不该硬切到上限，硬切 {hard} 段：{np.round(arr, 2).tolist()}"
    assert len(out) >= 14, f"15 句应切出 ≈15 段（一句一段），实得 {len(out)}：{np.round(arr, 2).tolist()}"
    med = float(np.median(arr))
    assert med < 2.2, f"段长中位应贴近真实句长 1.5~1.8s，实得 {med:.2f}s（说明被判据黏住）"


def test_segmenter_voiced_override():
    """v2.9.0（恢复）：神经判定注入通道——voiced_override 提供时接管判决、
    None 时完全退回能量判据（默认路径行为与历代版本逐字一致）。"""
    from app.audio.capture import Segmenter, TARGET_SR
    loud = (np.random.RandomState(7).randn(int(TARGET_SR * 0.3)) * 0.3).astype(np.float32)
    # ① 能量很响但神经判非人声（背景乐）→ 不进语音态
    seg = Segmenter(low_latency=True, cap_s=10.0)     # 上限放宽，隔离强制切段路径
    for _ in range(4):
        assert seg.feed(loud, voiced_override=False) is None
    assert seg.in_speech is False, "神经判非人声时不得进语音态（纯能量会误判）"
    assert seg.silence_run > 0.0, "非人声块应累积静音时长（这才是提前切句的依据）"
    # ② 神经判人声（能量低到能量判据必然漏）→ 正常攒语音
    seg2 = Segmenter(low_latency=True, cap_s=10.0)
    quiet = (np.random.RandomState(3).randn(int(TARGET_SR * 0.3)) * 0.0002).astype(np.float32)
    for _ in range(6):
        seg2.feed(quiet, voiced_override=True)
    assert seg2.in_speech is True, "神经判人声则低能量也要进语音态"
    assert seg2.speech_len > 0.5
    # ③ override=None → 完全沿用旧能量判据（默认/降级路径）
    seg3 = Segmenter(low_latency=True, cap_s=10.0)
    for _ in range(6):
        seg3.feed(loud, voiced_override=None)
    assert seg3.in_speech is True, "未给 override 时应走能量判据"


def test_neural_vad_hysteresis_and_degrade():
    """v2.9.0（恢复）：滞回双阈值（0.50 进/0.35 出）防词内短间隙把判定抖碎；
    推理抛异常则永久降级返回 None（调用方静默回退能量判据，不反复刷错）。
    注意滞回也正是"背景乐下黏 0.66s"的来源——调参必读 Segmenter.feed 留档。"""
    from app.audio.capture import CaptureThread
    ct = CaptureThread("system", -1)
    ct._neural_vad = True
    ct._vad_buf = np.zeros(0, dtype=np.float32)

    class FakeModel:
        def __init__(self, prob):
            self.prob = prob
        def __call__(self, audio):
            assert audio.shape == (512,), f"必须按 512 样本整块喂给 Silero，实得 {audio.shape}"
            return np.array([[self.prob]], dtype=np.float32)

    blk = np.zeros(512, dtype=np.float32)
    ct._vad_model, ct._vad_on, ct._vad_voiced = FakeModel(0.01), True, False
    assert ct._neural_voiced(blk) is False
    # 0.42 落在双阈值之间：非人声态不得翻成"人声"（无滞回时 0.5 门槛只差一点）
    ct._vad_model = FakeModel(0.42)
    assert ct._neural_voiced(blk) is False, "0.42 < 0.50 不应翻成人声"
    # 0.9 → 人声；随后 0.42 仍保持人声（0.35 才退出）——这就是词内间隙不被腰斩的关键
    ct._vad_model = FakeModel(0.90)
    assert ct._neural_voiced(blk) is True
    ct._vad_model = FakeModel(0.42)
    assert ct._neural_voiced(blk) is True, "0.42 > 0.35 应保持人声（滞回）"
    ct._vad_model = FakeModel(0.10)
    assert ct._neural_voiced(blk) is False
    # 跨块累积：不足 512 样本时不调模型、沿用最近结论
    ct._vad_model, ct._vad_voiced = FakeModel(0.99), False
    assert ct._neural_voiced(np.zeros(100, dtype=np.float32)) is False
    assert ct._neural_voiced(np.zeros(500, dtype=np.float32)) is True, "凑满 512 才判定"

    class Boom:
        def __call__(self, audio):
            raise RuntimeError("onnx broken")

    ct._vad_model, ct._vad_on = Boom(), True
    assert ct._neural_voiced(blk) is None, "异常应降级为 None（回退能量判据）"
    assert ct._vad_on is False, "一次异常后永久降级，不再反复调用"
    assert ct._neural_voiced(blk) is None


def test_submit_spec_never_evicts_final():
    """v2.7.6（A）：队列满时推测提交**放弃自己、绝不挤掉终版**——被挤掉的
    终版会被 _drop_translation 置终态，而它的文本不会再来第二次，卡片就此悬挂。"""
    from app.translate.translator import TranslateThread
    tt = TranslateThread("argos", "zh-CN")
    for i in range(5):
        tt.submit(f"final{i}", "en")
    assert tt.queue_in.qsize() == 5
    assert tt.submit("speculative piece", "en", spec=True) == [], "推测提交不得挤掉别人"
    assert tt.queue_in.qsize() == 5, "队列满时推测应放弃自己"
    assert [tt.queue_in.get_nowait()[:2] for _ in range(5)] == \
        [(f"final{i}", "en") for i in range(5)], "五条终版必须原封不动"
    # 队列有空位时推测照常入队，带 spec=True
    tt2 = TranslateThread("argos", "zh-CN")
    assert tt2.submit("piece A", "en", spec=True) == []
    assert tt2.queue_in.get_nowait() == ("piece A", "en", True)
    # dropped 恒为二元组：保住主窗两处 `for d_text, _d_lang in (tr.submit(...) or [])` 解包契约
    # v2.20.2 换代：被挤掉的**推测中间版不再上报**（旧实现把 spec 也当"终版丢了"
    # 报出去，主窗据此把卡片置成红字"翻译队列繁忙"，而真正的终版还在队列里）。
    tt3 = TranslateThread("argos", "zh-CN")
    for i in range(2):
        tt3.submit(f"f{i}", "en")
    for i in range(3):
        tt3.submit(f"s{i}", "en", spec=True)
    assert tt3.queue_in.qsize() == 5
    d = tt3.submit("overflow", "en")
    assert [t for t, _l in d] == ["f0"], d       # 队头是终版 → 照旧上报
    assert all(len(x) == 2 for x in d), f"dropped 应为二元组，实得 {d}"
    assert [q[0] for q in tt3.queue_in.queue] == \
        ["f1", "s0", "s1", "s2", "overflow"], "被挤掉的只能是队头"
    # 队头是中间版时：它被挤掉但**不上报**，队列腾出位置后照常入队
    tt4 = TranslateThread("argos", "zh-CN")
    for i in range(5):
        tt4.submit(f"only-spec-{i}", "en", spec=True)
    assert tt4.submit("a final", "en") == [], "全是中间版时不该上报任何丢句"
    assert [q[0] for q in tt4.queue_in.queue][-1] == "a final"


def test_coerce_float_segment_cap():
    """v2.7.6：segment_cap_s 是项目首个 float 型配置键——旧 _coerce 没有
    float 分支（=零校验），手编 "2.5s"/负数会原样送进 Segmenter。"""
    from app.config import Config, DEFAULTS
    # 默认 4.0=榨干档（用户裁决）：fixture A/B 实测 2.5s 会让 71% 句子被腰斩，
    # 而推测式翻译已消除上限对"译文迟到"的影响，激进档只保留为可选项
    assert DEFAULTS["segment_cap_s"] == 4.0, f"默认应为 4.0，实得 {DEFAULTS['segment_cap_s']}"
    c = Config._coerce
    d = DEFAULTS["segment_cap_s"]
    assert c("segment_cap_s", "2.5s") == d, "毒药字符串回默认"
    assert c("segment_cap_s", -3) == d, "负值回默认"
    assert c("segment_cap_s", "3") == 3.0, "数字字符串被挽救"
    assert isinstance(c("segment_cap_s", 4), float), "int 归一为 float（combo findData 要求类型一致）"
    assert c("segment_cap_s", True) == d, "bool 回默认"
    assert c("segment_cap_s", 2.5) == 2.5, "合法浮点原样采纳"


def test_stream_preview_restart():
    """v2.13.0：stop→restart 热重启（中途切回 dual 布局场景）——停止标志
    复位、陈旧音频缓冲清空（旧窗口不该混进新布局的第一拍草稿）。"""
    from app.asr.preview import StreamPreview
    p = StreamPreview(None, "en")
    p.feed(np.zeros(1600, dtype=np.float32), 0.0)
    assert p._buf_len > 0.0
    p.stop()
    assert p._stop is True
    p.restart()
    assert p._stop is False and p._buf_len == 0.0 and len(p._buf) == 0
    # feed 在停止期间不得积累（省内存）
    p.stop()
    p.feed(np.zeros(1600, dtype=np.float32), 0.0)
    assert p._buf_len == 0.0


def test_stream_preview_language_auto():
    """v2.18.2（D-1）：识别语言=auto（**出厂默认值**）时流式预览必须把
    language 归一为 None 交给 whisper 自动检测。直传字符串 "auto" 会在
    Tokenizer 构造处抛 `ValueError: 'auto' is not a valid language code`，
    而预览线程逐拍 try 会把它整拍静默吞掉——症状=开了"流式原文"却永远
    不出草稿、界面零报错（GPU+dual+自动检测 = README 主打场景整条哑火）。
    取证：本机离线 tiny 模型实测 'auto' 抛 ValueError、None 正常检测。"""
    from app.asr.preview import StreamPreview, normalize_language
    from app.config import DEFAULTS

    assert DEFAULTS["asr_language"] == "auto", \
        "默认识别语言已变——本锁的前提（默认用户中招）需重新评估"
    # 纯函数侧
    assert normalize_language("auto") is None
    assert normalize_language("Auto") is None, "手编配置大小写不得复现同一哑火"
    assert normalize_language("  ") is None and normalize_language(None) is None
    assert normalize_language("en") == "en" and normalize_language("zh-CN") == "zh-CN"

    # 实参侧：真正落到 transcribe 的 kwargs 里不许出现 language="auto"
    calls = []

    class _Model(object):
        def transcribe(self, audio, **kw):
            calls.append(kw)
            return iter([]), None

    p = StreamPreview(_Model(), "auto")
    assert p._lang is None, "构造期就要归一，不能留到调用点各自判断"
    assert p._transcribe(np.zeros(1600, dtype=np.float32)) == ""
    assert calls, "桩模型未被调用，本锁等于没测"
    assert "language" not in calls[-1], f"auto 被原样下发给 whisper：{calls[-1]}"
    # 锁定语言时仍必须透传（草稿不该每拍重新猜语言）
    p2 = StreamPreview(_Model(), "en")
    p2._transcribe(np.zeros(1600, dtype=np.float32))
    assert calls[-1].get("language") == "en", "指定语言必须原样下发"


def test_spec_source_lang_resolution():
    """v2.18.2（D-3）：推测式送译的源语言回退链——攒句语言 → 会话最近识别
    语言 → 配置；**"auto" 一律视同未知**（translator.py:537 会把它挡成
    source=None，argos 随即抛"缺少源语言信息"，spec 不走备援链 → 首句几拍
    的草稿译文全灭；真机实测 12 条推测回复里 4 条带该错）。
    轻量替身借用未绑定函数（单元测试里不能构造 QWidget，见 HANDOFF 5.3）。"""
    from app.ui.main_window import MainWindow

    class Cfg(object):
        def __init__(self, d):
            self._d = d

        def get(self, key):
            return self._d.get(key)

    class W(object):
        _spec_source_lang = MainWindow._spec_source_lang

    w = W()
    w.config = Cfg({"asr_language": "auto"})
    assert w._spec_source_lang() == "", "全未知时必须解不出语言，绝不能把 'auto' 当语言送出去"
    w._last_asr_lang = "en"
    assert w._spec_source_lang() == "en", "会话最近识别语言可兜底（草稿早于首个终版片段）"
    w._tgroup_lang = "ja"
    assert w._spec_source_lang() == "ja", "本攒句语言优先级最高"
    del w._tgroup_lang, w._last_asr_lang
    w.config = Cfg({"asr_language": "en"})
    assert w._spec_source_lang() == "en", "用户手动锁定语言照常生效"
    w.config = Cfg({"asr_language": " AUTO "})
    assert w._spec_source_lang() == "", "大小写/空格变体同样视同未知"
    w.config = Cfg({"asr_language": "zh-CN"})
    assert w._spec_source_lang() == "zh-CN", "带地区后缀的语言码不受影响"


def test_strip_overlapped_prefix():
    """v2.12.0：流式草稿增量剥离——partial 窗口与已确认文本尾部天然重叠
    （同一段音频两次转写），剥离后只剩新增话音；无重叠时全量返回。"""
    from app.ui.main_window import MainWindow
    f = MainWindow._strip_overlapped_prefix
    assert f("The quick brown fox", "The quick brown fox jumps") == "jumps"
    # 部分重叠：partial 开头与 base 尾部对齐的部分剥掉
    assert f("The quick brown fox", "brown fox jumps over") == "jumps over"
    assert f("hello world", "completely new text") == "completely new text"
    assert f("", "anything") == "anything"
    assert f("base", "") == ""
    # 大小写抖动：词级锚 lower 匹配直接容错——正确剥离而非全量重复
    # （v2.13.0a 算法升级：旧严格对齐在此场景会整句重复上屏）
    assert f("the quick brown fox", "The quick brown fox jumps") == "jumps"


def test_stream_preview_gate():
    """v2.12.0：流式原文三重闸——开关 × dual 布局 × cuda（CPU 自动停用：
    预览每 0.9s 重识别一次，CPU 单次要数秒、反而拖垮正式识别）。"""
    from app.ui.main_window import MainWindow

    class FakeOverlay(object):
        _dual = True

        def is_dual(self):
            return self._dual

    class Cfg(object):
        def __init__(self, d):
            self._d = dict(d)

        def get(self, k):
            return self._d.get(k)

    class W(object):
        _stream_preview_enabled = MainWindow._stream_preview_enabled

        def __init__(self, cfg, ov):
            self.config = cfg
            self.overlay = ov

    d = {"stream_preview": True, "asr_device": "cuda"}
    assert W(Cfg(d), FakeOverlay())._stream_preview_enabled() is True
    assert W(Cfg(dict(d, asr_device="cpu")), FakeOverlay())._stream_preview_enabled() is False, \
        "CPU 必须停用（预览推理反而拖垮正式识别）"
    assert W(Cfg(dict(d, stream_preview=False)), FakeOverlay())._stream_preview_enabled() is False, \
        "开关关闭不得启用"
    assert W(Cfg(d), type("ListOv", (FakeOverlay,), {"_dual": False})())._stream_preview_enabled() is False, \
        "列表布局不需要草稿"


def test_spec_translate_offline_only_gate():
    """v2.7.6（A）：推测式翻译三重闸——在线引擎绝不推测（有额度与限流，
    MyMemory 每天约 5000 字符免费额度），engine=auto 按探测后实际选用引擎判定。

    不构造 MainWindow（QWidget 需 QApplication，单元测试环境没有）——
    直接借用 _spec_enabled 的函数体挂到轻量替身类上，判定逻辑逐字同源。"""
    from app.ui.main_window import MainWindow

    class W(object):
        _spec_enabled = MainWindow._spec_enabled    # 借用未绑定函数，不建控件

        def __init__(self, cfg, tr):
            self.config = cfg
            self._tr = tr

        def _active_translate(self):
            return self._tr

    class Cfg(object):
        def __init__(self, d):
            self._d = dict(d)

        def get(self, k):
            return self._d.get(k)

    class Online(object):
        _active_engine = "google"

    class Offline(object):
        _active_engine = "argos"

    d = {"spec_translate": True, "low_latency_mode": True}
    assert W(Cfg(d), Online())._spec_enabled() is False, "在线引擎不得推测（额度/限流）"
    assert W(Cfg(d), Offline())._spec_enabled() is True, "离线 argos 允许推测"
    d2 = dict(d, spec_translate=False)
    assert W(Cfg(d2), Offline())._spec_enabled() is False, "开关关闭不得推测"
    d3 = dict(d, low_latency_mode=False)
    assert W(Cfg(d3), Offline())._spec_enabled() is False, "非攒句模式（逐句直送）无可推测"
    assert W(Cfg(d), None)._spec_enabled() is False, "翻译线程不存在不得推测"
    # 引擎尚未探测完（_active_engine 为空）时不推测——避免把中间版打给未知引擎
    class Unprobed(object):
        _active_engine = ""
    assert W(Cfg(d), Unprobed())._spec_enabled() is False, "引擎未探测完不得推测"


def test_spec_translation_not_cached():
    """v2.18.1：推测式中间版**不得写持久缓存**。

    真机英语新闻实测（BBC Global News Podcast 整集，150s 会话）：222 条缓存里
    41 组是同一句话的渐进变体，最长一句被存了 7 个版本（"GMT on Tuesday 15th
    September. …" 长度 58/86/99/160/168/192/201）。半句前缀几乎不会再被原样查到
    （终版文本更长），写进去纯属污染：LRU 只有 800 格，真整句缓存被一次性碎片
    挤掉，磁盘上还永久留着大量半截译文。读侧必须照常受益（文本相同即命中）。"""
    from app.translate import translator as tr

    class FakeCache:
        def __init__(self):
            self.puts = []
            self.data = {}

        def get(self, k):
            return self.data.get(k)

        def put(self, k, v):
            self.puts.append(k)
            self.data[k] = v

    class FakeEngine:
        def translate(self, text, source, target):
            return ("译文:" + text, "en")

    real_cache, real_engines = tr._cache, tr.ENGINES
    tr._cache = FakeCache()
    tr.ENGINES = {"argos": FakeEngine()}
    try:
        tt = tr.TranslateThread("argos", "zh-CN")
        tt._active_engine = "argos"
        tt._do_translate("a long partial sentence grow", "en", store=False)
        assert tr._cache.puts == [], f"推测版不得写缓存，实写 {tr._cache.puts}"
        tt._do_translate("a long partial sentence grow", "en")     # 终版照常写
        assert len(tr._cache.puts) == 1, f"终版必须入缓存，实得 {tr._cache.puts}"
        # 同文本再推测：读侧照常命中缓存（不因 store=False 而失去命中收益）
        hit = tt._do_translate("a long partial sentence grow", "en", store=False)
        assert hit[0] == "译文:a long partial sentence grow", hit
        assert len(tr._cache.puts) == 1, "命中路径不得重复写"
    finally:
        tr._cache, tr.ENGINES = real_cache, real_engines


def test_cache_unreadable_never_wipes():
    """v2.19.2：翻译缓存"读不开" ≠ "空缓存"。

    旧实现 `_load()` 把异常吞成 `_data={}` 且照常 `_loaded=True`，绕过 v2.2.1
    那道"未加载禁写盘"守卫——首次攒批落盘（10 条或 5 秒）就用近空 dict
    `os.replace` 覆写整份持久缓存，用户几场攒下的翻译记录无声消失、不留原件。
    触发面比想象大：记事本改过编码（BOM/ANSI）、杀软或索引器瞬时占用文件。
    """
    import json
    import tempfile
    d = tempfile.mkdtemp()
    p = Path(d) / "trans_cache.json"
    try:
        # ① BOM 头（记事本"另存为 UTF-8"）：现在必须读得出来
        p.write_bytes(b"\xef\xbb\xbf" + json.dumps(
            {"hello": ["你好", "google"]}).encode("utf-8"))
        c = TranslationCache()
        c._path = lambda: p
        assert c.get("hello") == ("你好", "google"), "带 BOM 的缓存应正常读出"

        # ② 真·读不开（GBK 落盘）：按空处理，但**绝不允许覆写磁盘原件**
        junk = json.dumps({"old": ["旧记录", "google"]},
                          ensure_ascii=False).encode("gbk")
        p.write_bytes(junk)
        c2 = TranslationCache()
        c2._path = lambda: p
        assert c2.get("old") is None, "读失败按空处理（功能继续）"
        c2.put("new", ["新记录", "google"])
        c2._dirty_puts = 99
        c2._save_locked()
        assert p.read_bytes() == junk, "读失败的缓存不得被空 dict 覆写"
        assert p.with_name(p.name + ".bad").exists(), "原件必须另存供人工抢救"
    finally:
        pass


def test_probe_engine_recognises_argos():
    """v2.19.2：`probe_engine` 必须认 argos。

    旧实现只有 google/mymemory 两个分支，argos 恒落到末尾
    `return False, "未知引擎"`。后果：`engine=argos` + 自动备援（默认开）的用户
    一次离线异常落到 MyMemory 后，`_maybe_reprobe_primary` 每 60s 重探主引擎
    **永远失败**，整场被绑在在线引擎上（额度/429），而 `main_window._spec_enabled()`
    要求 `_active_engine == 'argos'` → 推测式增量翻译随之静默关闭。
    """
    from app.translate import offline_pack as op
    from app.translate import translator as tr
    real = op.list_installed
    try:
        op.list_installed = lambda: [("en", "zh")]
        ok, detail = tr.probe_engine("argos", src="en", tgt="zh-CN")
        assert ok and "en→zh" in detail, (ok, detail)
        ok2, d2 = tr.probe_engine("argos", src="ja", tgt="zh-CN")
        assert (not ok2) and "缺少" in d2, (ok2, d2)
        ok3, d3 = tr.probe_engine("argos")          # 方向未知：有包即可恢复
        assert ok3 and "1 个方向" in d3, (ok3, d3)
        op.list_installed = lambda: []
        ok4, d4 = tr.probe_engine("argos")
        assert (not ok4) and "未安装" in d4, (ok4, d4)
    finally:
        op.list_installed = real


def test_coerce_allows_negative_screen_coords():
    """v2.19.2：坐标键允许负值——副屏在主屏左侧/上方时 Qt 虚拟桌面坐标天然为负。

    v2.7.5（R-5）"负数无意义"的一刀切把 `overlay_x=-1600` 消毒回默认 200，
    多显示器用户每次重启面板都被拽回主屏左上角（而 `_clamp_overlay_pos`
    明确支持负坐标并落盘）。尺寸/条数类键必须仍然拦负。"""
    from app.config import Config, DEFAULTS
    assert Config._coerce("overlay_x", -1600) == -1600
    assert Config._coerce("overlay_y", "-240") == -240      # 字符串挽救同享
    assert Config._coerce("overlay_y", -1e9) == -1000000000
    assert Config._coerce("overlay_w", -300) == DEFAULTS["overlay_w"]
    assert Config._coerce("max_history", -5) == DEFAULTS["max_history"]


def test_opacity_alpha_mapping_single_source():
    """v2.19.2：透明度换算收口 + 100% 必须真的是 255。

    旧写法 `int(v * 2.55)` 在浮点下 100 → 254.999… → **254**，"100% 不透明"
    常年漏 1/255 的桌面进来；且主窗 `_on_panel_opacity` 自带一份同样公式直接
    改私有属性，与 `apply_style` 两副面孔（当场 254、重启 255）。"""
    from app.ui.caption_overlay import alpha_to_opacity, opacity_to_alpha
    assert opacity_to_alpha(100) == 255, opacity_to_alpha(100)
    assert opacity_to_alpha(92) == 235, "旧 int(92*2.55)=234"
    assert opacity_to_alpha(0) == opacity_to_alpha(30), "地板 30 仍在"
    assert opacity_to_alpha(500) == 255, "上限仍夹到 100 档"
    for v in (30, 60, 75, 85, 92, 100):
        assert alpha_to_opacity(opacity_to_alpha(v)) == v, (
            "档位→alpha→档位必须回到原值（菜单勾选态靠它）")


def test_bump_check_covers_version_tuples():
    """v2.19.2：`bump_version --check` 必须把 filevers/prodvers 元组也拉进比较。

    旧实现是 `{FileVersion 字符串} or {filevers 元组}`——字符串恒存在，`or`
    短路后元组**永不参与**比较：只改元组也报"版本一致"，产出的 EXE 文件属性
    却是错版本。"""
    import importlib.util
    here = Path(__file__).resolve().parents[1] / "scripts" / "bump_version.py"
    spec = importlib.util.spec_from_file_location("bv_mod", str(here))
    bv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bv)
    files = {
        "app/config.py": 'APP_VERSION = "2.19.2"',
        "setup.iss": '#define MyAppVersion "2.19.2"',
        "version_info.txt": "filevers=(2, 19, 1, 0),\n prodvers=(2, 19, 1, 0),\n"
                            "StringStruct('FileVersion', '2.19.2.0'),\n"
                            "StringStruct('ProductVersion', '2.19.2.0'),\n",
    }
    real_read = bv.read
    try:
        bv.read = lambda p: files[p]
        vs = bv.current_versions()
        assert "2.19.1" in vs["version_info.txt"], f"元组形态必须被读到: {vs}"
        vals = set()
        for f, v in vs.items():
            vals |= set(v) if f == "version_info.txt" else {v}
        assert len(vals) == 2, f"字符串/元组不一致必须判为不一致: {vals}"
    finally:
        bv.read = real_read


def test_strip_overlap_prefix_equal_small_tail():
    """v2.19.3：预览草稿＝"基线整句 + 少量新词"时必须只返回新词。

    真机 60s 合成英语新闻实测：4s 滑窗每拍把同一段音频重转写一遍再往前多听几个词，
    旧剥离算法要求"基线尾部锚落在 text 前 2/3 内"，27 词基线只多 1 个词时锚在
    28 词的 ~24 位（限 18）→ 判"无重叠"→ **整句被当成新话返回** → 幕墙态同句
    重复两行、尾巴再碎成第三行（多 9 个词才正常，所以是偶发且必然复现的）。"""
    from app.ui.main_window import MainWindow
    strip = MainWindow._strip_overlapped_prefix
    base = ("Technology shares led the gain after a major chipmaker reported strong "
            "than expected demand for its latest accelerator lower inflation readings "
            "this month helped lift sentiment of")
    assert strip(base, base) == "", "草稿与基线同句应无增量"
    assert strip(base, base + " inflation") == "inflation"
    assert strip(base, base + " inflation eased this month") == "inflation eased this month"
    assert strip(base, base[0].upper() + base[1:] + " inflation") == "inflation", \
        "首字母大小写差异不得破坏前缀相等判定"
    # 反向护栏：真正的新句必须原样保留，不许被"前缀相等"误吞
    fresh = "On the sports desk the national team qualified late"
    assert strip(base, fresh) == fresh
    assert strip(base, base + " and the bond market rallied for weeks") == \
        "and the bond market rallied for weeks"


def test_merge_stream_drops_mid_sentence_repeat():
    """v2.19.3：流式合并不得把已显示的短语在同一行里复读。

    真实网页新闻实测（DW News 直播 150s，离线 Argos）：末行出现
    "…now in a One of those people is Lauren, now in her mid-30s. … people is
    Lauren, now in her mid-30s…" ——同一短语被拼进**三遍**。根因：预览转写在句中
    就与已显示文本分叉（"now in a" vs "now in her"），旧实现的 (c) 类重叠只做
    "current 后缀 == diff 前缀"，对不齐就落到 (d) 整段追加。"""
    from app.ui.main_window import MainWindow
    ms = MainWindow._merge_stream
    out = ms("One of those people is Lauren, now in a",
             "One of those people is Lauren, now in her mid-30s. She lives near Toronto")
    assert out.lower().count("one of those people") == 1, f"短语被复读：{out!r}"
    assert "She lives near Toronto" in out, f"新词必须保留：{out!r}"
    # 忽略标点与大小写的同一短语也要认出来
    out2 = ms("Lower inflation readings, this month",
              "lower inflation readings this month helped lift sentiment")
    assert out2.lower().count("inflation") == 1, out2
    assert out2.endswith("helped lift sentiment"), out2
    # 护栏：正常生长、后缀重叠、**合法叠词**都不许被误吞
    assert ms("Hello everyone", "welcome to the show") == "Hello everyone welcome to the show"
    assert ms("the market closed higher", "higher for the fourth session") == \
        "the market closed higher for the fourth session"
    assert "really really" in ms("it was really", "really really good news"), \
        "合法叠词不得被当成复读吞掉（(c) 类重叠只并一个词，'really really' 必须在）"
    assert ms("it was really", "really good news") == "it was really good news", \
        "单词级重叠仍按旧行为并掉"


def test_merge_stream_drops_tail_reshuffle_repeat():
    """v2.20.0：流式拍把已显示整句**重转写**一遍时，行尾不得复读。

    用户实拍（英语新闻 dual 面板）："…my guest tonight. Oh, my God! and Tom
    Cruise is my guest tonight."——复读块在 diff 的**结尾**而不是开头，v2.19.3
    那条"diff 头部命中 current"的回跳判据完全不认，于是落到"无重叠→整段追加"。
    新 (f) 判据看**两端对齐**：current 与 diff 的规范化词尾有 ≥3 词公共后缀，
    即说明这一拍没往前走，取更完整的那一份。"""
    from app.ui.main_window import MainWindow
    ms = MainWindow._merge_stream
    cur = "and Tom Cruise is my guest tonight"
    diff = "Oh, my God! and Tom Cruise is my guest tonight"
    out = ms(cur, diff)
    assert out.lower().count("my guest tonight") == 1, f"行尾复读未消除：{out!r}"
    assert out == diff, f"diff 整块包住 current 时应留更完整的一版：{out!r}"
    # 两句同尾（各 9 词、只有中间两词不同）：本拍判为"没往前走"→ 绝不同时出现两遍
    a = "the first half of the show was about weather"
    b = "the second half of the show was about weather"
    out3 = ms(a, b)
    assert out3.lower().count("the show was about weather") <= 1, f"仍复读：{out3!r}"
    assert out3 in (a, b), f"同尾两版必须二选一、不得拼接：{out3!r}"
    # 护栏：真实续接（尾词不同）必须照常追加
    assert ms("Tom Cruise is my guest", "tonight and we talk about films") == \
        "Tom Cruise is my guest tonight and we talk about films"
    # 护栏（同轮顺带修）：**单字符**重叠不算重叠——旧 (c) 类 k 一路降到 1，
    # "…is my guest" + "tonight…" 会被当成重叠吃掉首字母，拼成 "guestonight…"
    assert ms("the market closed at", "the price rose") == \
        "the market closed at the price rose"
    assert ms("it really helped a", "a lot of people today") == \
        "it really helped a a lot of people today"
    # 两字符及以上的重叠仍按旧行为并掉
    assert ms("the market closed higher", "higher for the fourth session") == \
        "the market closed higher for the fourth session"
    # 护栏：公共后缀只有 2 词不足以判定重转写，不许触发（宁可照旧）
    assert ms("we say goodbye now", "and then goodbye now") == \
        "we say goodbye now and then goodbye now"


def test_segmenter_keeps_short_fragments():
    """v2.20.1：未达 MIN_SPEECH_S 的碎片必须**并入下一段**，不许静默丢弃。

    真机实测（90.5s 合成英语新闻，低延迟 + cap=4s）：旧实现 11 次整段丢弃、
    扔掉 9.4s 语音（cap=6s 也扔 7 次 / 6.6s），用户看到的段首/段尾掉词
    （"Analysts"、"In other news,"、"three continents"、"more than eight hours"）
    全部出在这一条判据上。修完产出音频 65.4s → 74.9s。
    同时钉住两道防失控闸：累计上限 PENDING_MAX_S、纯静音超时作废 PENDING_MAX_IDLE_S
    （否则会把几十秒前的音频粘进新段）。"""
    from app.audio.capture import (Segmenter, TARGET_SR, MIN_SPEECH_S,
                                   PENDING_MAX_S, PENDING_MAX_IDLE_S)

    def ch(sec, amp=0.2):
        return np.ones(int(TARGET_SR * sec), dtype=np.float32) * amp

    # ① 短碎片 + 长静音 + 正常段：两段语音都必须完整产出
    s = Segmenter(low_latency=True, turbo=True, cap_s=4.0)
    out = []
    for c in (ch(0.35), ch(1.0, 0.0), ch(1.5), ch(1.0, 0.0)):
        r = s.feed(c)
        if r is not None:
            out.append(r)
    r = s.flush()
    if r is not None:
        out.append(r)
    got = sum(len(o) / TARGET_SR for o in out)
    assert got >= 0.35 + 1.5, f"短碎片被丢弃：产出 {got:.2f}s"
    assert any(len(o) / TARGET_SR >= 1.8 for o in out), \
        "碎片应作为下一段的**开头**被并入，而不是单独成段"
    # ② 累计上限：碎片不许无限增长
    s2 = Segmenter(low_latency=True, turbo=True, cap_s=4.0)
    for _ in range(40):
        s2.feed(ch(0.3))
        s2.feed(ch(1.2, 0.0))
        assert s2.pending_len <= PENDING_MAX_S + 0.1, s2.pending_len
    # ③ 超时作废：长时间纯静音后的陈旧碎片不许再粘进下一段
    s3 = Segmenter(low_latency=True, turbo=True, cap_s=4.0)
    s3.feed(ch(0.35))
    s3.feed(ch(1.0, 0.0))
    assert s3.pending, "前置：短碎片应已转入 pending"
    s3.feed(ch(PENDING_MAX_IDLE_S + 1.0, 0.0))
    assert not s3.pending, "陈旧碎片未作废"
    # ④ 近静音（peak 过低）的碎片仍然丢掉，不许污染下一段
    s4 = Segmenter(low_latency=True, turbo=True, cap_s=4.0)
    s4.feed(ch(0.35, 0.0001))
    s4.feed(ch(1.0, 0.0))
    assert not s4.pending, f"peak<0.002 的静音碎片不该转存：{s4.pending_len}"


def test_argos_junk_hypothesis_retried():
    """v2.20.1：离线高 beam 吐出不可读码位时必须退回 beam 2 重译，不许上屏。

    真机英语新闻实测（离线 argos en→zh，beam 5，48 句）：2 句上屏为乱码串，
    典型 "I don't know how" → "иぃ\\ue01d笵"（西里尔 + 假名 + 私用区）。
    同模型 float32 复现出**另一串**乱码 → 与量化无关，是 beam 搜索本身的退化
    假设；beam 2 同批 48 句零乱码。守卫只认"任何语言正文都不该出现的码位"，
    不判语种，免伤 zh→en 等拉丁方向的合法译文。"""
    from app.translate.offline_pack import PackTranslator, _has_junk

    # 判据边界：乱码 / 替换符 / 控制符命中，正常中英与"师便打"这类合法生僻字不命中
    assert _has_junk("и\u3043\ue01d笵")
    assert _has_junk("好\ufffd")
    assert _has_junk("a\u0001b")
    assert not _has_junk("我不知道如何做。")
    assert not _has_junk("DW News.")
    assert not _has_junk("师便打.")

    class _Res(list):
        @property
        def hypotheses(self):
            return self[0]

    class _FakeCT2:
        def __init__(self, table):
            self.table = table
            self.calls = []

        def translate_batch(self, batch, **kw):
            beam = kw.get("beam_size")
            self.calls.append(beam)
            key = beam if beam in self.table else "junk"
            return [_Res([[self.table[key]]])]

    class _FakeSp:
        def encode(self, text, out_type=None):
            return ["x"] * max(1, len(text.split()))

    def make(table):
        pt = PackTranslator.__new__(PackTranslator)   # 不加载真模型
        pt.sp = _FakeSp()
        pt.translator = _FakeCT2(table)
        return pt

    JUNK = "и\u3043\ue01d笵"
    pt = make({"junk": JUNK, 2: "我不知道如何"})
    assert pt._translate_chunk("I don't know how", beam_size=5) == "我不知道如何"
    assert pt.translator.calls == [5, 2], pt.translator.calls
    # 正常输出不许白跑第二遍
    pt2 = make({5: "我不知道", "junk": "不对"})
    assert pt2._translate_chunk("I don't know", beam_size=5) == "我不知道"
    assert pt2.translator.calls == [5]
    # v2.20.2：安全档 beam 2 自己吐乱码时也必须有兜底——旧守卫写着
    # `beam_size != SAFE_BEAM`，于是「快速」档用户完全没保护，而实测 int8 下
    # beam 2 同样会吐一串 U+FFFD。阶梯：用户档 → 2 → 1（贪心），三级都脏才认输。
    pt3 = make({"junk": "\ufffd\ufffd", 1: "正常"})
    assert pt3._translate_chunk("a b", beam_size=2) == "正常"
    assert pt3.translator.calls == [2, 1], pt3.translator.calls
    # 阶梯走完仍脏：原样返回（不误杀、不死循环，最多两次额外解码）
    pt4 = make({"junk": "\ufffd\ufffd"})
    assert pt4._translate_chunk("a b", beam_size=5) == "\ufffd\ufffd"
    assert pt4.translator.calls == [5, 2, 1], pt4.translator.calls


def test_translate_cache_never_serves_junk():
    """v2.20.2：乱码守卫必须同时长在**缓存读侧与写侧**。

    v2.20.1 的守卫只在生成侧（`PackTranslator._translate_chunk`），而脏译文早在
    升级前就进了 `trans_cache.json`（键格式没变、无版本位，LRU 800 格不会自己
    洗掉）——命中即原样端出来，等于那次修复对老用户完全无效。"""
    from app.translate import translator as T

    th = T.TranslateThread.__new__(T.TranslateThread)
    th.target = "zh-CN"
    th._active_engine = "argos"
    junk = "и\u3043\ue01d笵"
    key = th._cache_key("en", "cache junk probe")
    key2 = th._cache_key("en", "write side probe")
    calls = []

    class _Eng:
        @staticmethod
        def translate(text, source, target):
            calls.append(text)
            return ("缓存乱码探针正常译文", "en")

    class _Bad:
        @staticmethod
        def translate(text, source, target):
            return (junk, "en")

    saved = T.ENGINES["argos"]
    try:
        T.ENGINES["argos"] = _Eng
        T._cache.put(key, [junk, "en"])
        out, _lang = th._do_translate("cache junk probe", "en")
        assert out == "缓存乱码探针正常译文", f"脏缓存被原样端出：{out!r}"
        assert len(calls) == 1, "命中脏缓存必须重译一次"
        assert T._cache.get(key)[0] == out, "重译结果要覆写脏条目"
        # 写侧：引擎给的仍是乱码时不许进缓存（否则一次脏译终身脏）
        T.ENGINES["argos"] = _Bad
        th._do_translate("write side probe", "en")
        assert T._cache.get(key2) is None, "乱码结果被写进了持久缓存"
    finally:
        T.ENGINES["argos"] = saved
        for k in (key, key2):
            T._cache._data.pop(k, None)


def test_fixmap_whole_word_matches_inside_cjk():
    """v2.20.2：全词模式在**中英混排**里必须仍然命中。

    `\\b` 的 `\\w` 含 CJK，`\\bUber\\b` 在 `我们使用Uber应用` 两侧都不成立——
    开着「整词匹配」（默认开）时，用户词典在最常见中文语境里整批静默失效。"""
    from app.fixmap import apply_dict

    got = apply_dict("我们使用Uber应用，AI很好，但UberEATS不算",
                     {"Uber": "优步", "AI": "人工智能"}, True)
    assert "优步应用" in got and "人工智能很好" in got, got
    assert "UberEATS" in got, f"整词判据过松，把 UberEATS 也拆了：{got}"
    assert apply_dict("the strikes were strikes", {"strikes": "罢工"}, True) == \
        "the 罢工 were 罢工", "纯英文句里的整词替换语义不得退化"
    assert apply_dict("the strikeship sank", {"strikes": "罢工"}, True) == \
        "the strikeship sank", "整词判据过松：strikeship 被拆了"


def test_translate_submit_drops_only_finals():
    """v2.20.2：队列满时**推测中间版被挤掉**不许上报成"终版丢了"。

    单片段的 spec 文本与终版文本相同，调用方一收到 dropped 就把那张卡置失败
    （"翻译队列繁忙"），而真正的终版还在队列里——红字 + 终版到达时再建一张重复卡。"""
    from app.translate.translator import TranslateThread

    th = TranslateThread("argos", "zh-CN")
    for i in range(5):
        th.queue_in.put(("spec %d" % i, "en", True))
    dropped = th.submit("the real final", "en")
    assert all(not t.startswith("spec ") for t, _l in dropped), dropped
    assert list(th.queue_in.queue)[-1][0] == "the real final", "终版必须入队"


def test_cache_non_dict_file_is_load_failure():
    """v2.20.2：合法 JSON 但顶层不是对象＝**读失败**，必须禁写并留原件。

    v2.6.3 只把 `_data` 置空、`_load_failed` 留在 False，于是 v2.19.2 那道
    "未成功加载不许写盘"的闸门对这条分支无效：一次攒批落盘就把整份缓存覆写成
    近空 dict（实测 60 条 → 1 条），不留 .json.bad、不记日志。"""
    import shutil
    import tempfile as tf
    from pathlib import Path
    from app.translate.translator import TranslationCache

    d = Path(tf.mkdtemp())
    f = d / "trans_cache.json"
    c = TranslationCache()
    c._path = lambda: f
    try:
        f.write_text("[]", encoding="utf-8")
        c._load()
        assert c._load_failed is True, "非 dict 顶层没被当读失败"
        c.put("zh-CN:en:x", ["y", "en"])
        c.save()
        assert f.read_text(encoding="utf-8").strip() == "[]", "脏文件被覆写"
        assert (d / "trans_cache.json.bad").exists(), "原件未留存"
        # 清空 = 用户主动放弃旧文件，此后必须恢复可写（否则整场新译文静默不落盘）
        c.clear()
        assert c._load_failed is False, "clear() 后仍禁写"
        c.put("zh-CN:en:z", ["w", "en"])
        c.save()
        assert "zh-CN:en:z" in f.read_text(encoding="utf-8"), "clear 后写不进去"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_argos_split_long_keeps_separators():
    """v2.20.2：超长文本切块不得吃掉了英文句号后的空格。

    旧写法 `replace(". ", ".|") + split("|")` 把分隔空格当分隔符吃掉，而
    `translate()` 用 "".join 拼回——实测 593 字符英文长文回拼少 17 个空格，
    "today. And then" 被焊成 "today.And then" 才送进模型。"""
    from app.translate.offline_pack import _split_long

    text = ("Sentence number one about the grid deal. And then a second one that is "
            "quite long. ") * 14
    assert len(text) > 400, len(text)
    assert "".join(_split_long(text)) == text, "切分丢字/改字"


def test_log_redacts_subtitle_text():
    """v2.20.2：日志承诺"不记录字幕正文"，异常路径必须真做到。

    在线翻译失败时 urllib3 的异常文本形如 `… with url: /translate_a/single?…&q=<
    整句字幕> …`，把字幕带进 app.log；README 又让用户把 app.log 贴到公开 Issues。"""
    from app.log import _redact

    s = ("HTTPSConnectionPool(host='translate.googleapis.com', port=443): "
         "Max retries exceeded with url: /translate_a/single?client=x&sl=auto"
         "&tl=zh-CN&dt=t&q=%E7%A7%98%E5%AF%86 "
         "(Caused by NewConnectionError)")
    out = _redact(s)
    assert "%E7%A7%98" not in out, out
    assert "translate.failed" == _redact("translate.failed")


def test_merge_stream_blocks_cjk_and_interior_repeat():
    """v2.20.4：`_merge_stream` 的两类"同一句在屏上两遍"必须在 (d) 追加前拦住。

    ① CJK/假名没有空格 → 词表恒 1 个 token，(c)(e)(f) 全部词级判据退化失效，
       实测 merge("他说今天天气不错", "他说今天的天气不错还有雨") 拼成两句。
    ② 词级内嵌重复：diff 开头多听到一个词就把 current 整块包住
       （"the market closed at four" vs "and the market closed at four o'clock …"），
       旧写法只在公共词尾 >=3 时才查 `_contains_block`，这类形态词尾只有 2 个。
    同时钉住反例：真的换了一句时必须照常追加，判据不许把新内容吃掉。
    """
    from app.ui.main_window import MainWindow

    m = MainWindow._merge_stream
    cjk = m("他说今天天气不错", "他说今天的天气不错还有雨")
    assert cjk == "他说今天的天气不错还有雨", cjk
    lat = m("the market closed at four",
            "and the market closed at four o clock today")
    assert lat.count("market") == 1, lat
    # 换句必须继续追加（宁可少并不可丢句）
    assert m("这个方案今天开会讨论", "这个方案明天开始实施").count("方案") == 2
    assert m("Hello there", "how are you") == "Hello there how are you"


def test_config_dict_coercion_keeps_valid_entries():
    """v2.20.4：词典类配置**逐条**过滤，一个坏值不得毁掉整本词典。

    旧写法是"任一条目不是 str→str 就整本回退默认原型"。实测 121 条误听词典里
    混进一个数值（手编 JSON / 旧版本写坏）→ load 出来 0 条，不留 .json.bad、
    不记日志，随后空词典被持久化回磁盘，用户攒的词典**不可恢复**。"""
    from app.config import Config, DEFAULTS
    import tempfile as tf
    from pathlib import Path

    good = {"%d" % i: "第%d个" % i for i in range(120)}
    hostile = dict(good)
    hostile["坏条目"] = 7                      # value 不是字符串
    hostile[3] = "键不是字符串"                 # key 不是字符串
    out = Config._coerce("mishear_map", hostile)
    assert isinstance(out, dict) and len(out) == 120, \
        f"一个坏值把整本词典带走了：{out if not isinstance(out, dict) else len(out)}"
    assert out["0"] == "第0个"
    # 全坏（或顶层不是 dict）仍回默认原型
    assert Config._coerce("mishear_map", [1, 2]) == {}
    assert Config._coerce("mishear_map", {"a": 1}) == {}


def test_log_redacts_proxy_credentials():
    """v2.20.4：脱敏要覆盖"代理账号口令"，且崩溃兜底日志同一条路。

    `app.log` 的 docstring 承诺不记正文，README 又让用户把它贴到公开 Issues；
    urllib3/requests 的异常文本里会带完整代理 URL（`http://user:pass@host`）。"""
    from app.log import _redact

    s = ("Cannot connect to proxy http://alice:S3cr3tPw@127.0.0.1:10808 "
         "while requesting https://translate.googleapis.com/x?q=%E4%BD%A0")
    out = _redact(s)
    assert "S3cr3tPw" not in out and "alice:" not in out, out
    assert "%E4%BD%A0" not in out, out
    # 崩溃兜底日志（main.write_log）必须走同一个脱敏口
    import main as _m
    import inspect
    src = inspect.getsource(_m.write_log)
    assert "_redact" in src, "write_log 绕过脱敏，异常原文直接落盘"


def test_segmenter_pending_cap_and_expiry():
    """v2.20.2：碎片队列的两道闸各自被实测打穿过，这里一起钉住。

    ① 上限截断：`_flush` 短路径转存的是一**整块**拼接后的 ndarray，旧截断只能整块
       跳过 → `keep_chunks=[]`，实测 `_stash_pending([2.4s])` 保留 0.000s——超上限
       时不是裁到 2s 而是全丢，稀疏语音场景 pending 在 0~1.8s 之间循环、一段都
       出不来。现在切在块内，且语音时长按占比折算而不是按缓冲时长超额扣减。
    ② 陈旧作废：旧实现任何 voiced 块都把 pending_idle 归零，周期性咔哒一直续命
       → 100 秒前的碎片仍被粘进下一段。现在只有"真的在说话"才归零。"""
    from app.audio.capture import Segmenter, TARGET_SR, PENDING_MAX_S

    def ch(sec, amp=0.2):
        return np.ones(int(TARGET_SR * sec), dtype=np.float32) * amp

    # ① 单块超限：裁到上限，且语音时长不被超额扣减
    s = Segmenter(low_latency=False, turbo=False)
    s._stash_pending([ch(2.4)], 0.6)
    kept = sum(c.shape[0] for c in s.pending) / TARGET_SR
    assert abs(kept - PENDING_MAX_S) < 0.05, f"超上限没裁到 2s，保留 {kept}s"
    assert s.pending_speech > 0.0, "语音时长被按缓冲时长超额扣光"
    # ② 瞬态续命：碎片必须在纯静音超时后作废，即使期间不断有咔哒
    s2 = Segmenter(low_latency=False, turbo=False)
    s2.feed(ch(0.4))
    s2.feed(ch(1.0, 0.0))
    assert s2.pending, "前置：短碎片应已转存"
    for _ in range(10):
        s2.feed(ch(4.97, 0.0))
        s2.feed(ch(0.03, 0.6))          # 周期性瞬态（旧实现据此无限续命）
    assert s2.pending_len <= 0.2, \
        f"陈旧碎片没作废：pending_len={s2.pending_len:.2f}s"
    # ③ 真的续上语音时计时归零（别把刚说的句子当陈货扔了）
    s3 = Segmenter(low_latency=False, turbo=False)
    s3.feed(ch(0.4))
    s3.feed(ch(1.0, 0.0))
    for _ in range(6):
        s3.feed(ch(0.5, 0.6))
    assert s3.pending_idle == 0.0, s3.pending_idle



# ===================== v2.20.6 界面语言（i18n）四道锁 =====================
# 这四条锁把"英文模式漏翻"从人眼问题变成构建失败。放 test_units 而非
# integration——CI 只跑单元+smoke，今后任何人新加一句界面文案而漏配英文，CI 当场红。

_I18N_SRC = ["app/ui/main_window.py", "app/ui/settings_dialog.py", "app/ui/caption_overlay.py",
             "app/ui/first_run.py", "app/asr/engine.py", "app/translate/translator.py",
             "app/translate/offline_pack.py", "app/audio/capture.py", "app/asr/preview.py"]
_NL = chr(10)


def _i18n_literals():
    """扫源码，收集所有 ui_text("…") / ui_fmt("…") 的字面量模板 → {模板: [位置]}。"""
    import ast
    root = Path(__file__).resolve().parents[1]
    out = {}
    for rel in _I18N_SRC:
        tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and getattr(n.func, "id", None) in ("ui_text", "ui_fmt") and n.args:
                a = n.args[0]
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    out.setdefault(a.value, []).append(rel + ":" + str(n.lineno))
    return out


def test_i18n_every_ui_string_has_english():
    """完整性锁：源码里每一句界面文案都必须有英文条目，漏一条即失败。"""
    from app.locales.en import CATALOG
    lits = _i18n_literals()
    assert lits, "没扫到任何 ui_text 字面量——codemod 或本测试的文件清单失效了"
    missing = [(k, v[0]) for k, v in lits.items() if k not in CATALOG]
    detail = _NL.join("  " + src + "  " + repr(k[:60]) for k, src in missing[:15])
    assert not missing, str(len(missing)) + " 条界面文案缺英文翻译:" + _NL + detail


def test_i18n_placeholders_match():
    """模板锁：ui_fmt 的 {占位符} 两侧必须完全一致——少一个 KeyError，多一个静默丢值。"""
    import re
    from app.locales.en import CATALOG
    pat = re.compile(r"\{(\w+)(?::[^}]*)?\}")
    bad = []
    for zh, en in CATALOG.items():
        want = set(pat.findall(zh))
        got = set(pat.findall(en))
        if want != got:
            bad.append("  " + repr(zh[:44]) + " 中文占位 " + str(sorted(want))
                       + " vs 英文 " + str(sorted(got)))
    assert not bad, "占位符不一致:" + _NL + _NL.join(bad[:12])


def test_i18n_english_has_no_cjk():
    """残留锁：英文值里不许有汉字或全角标点（漏翻/机器痕迹）。"""
    import re
    from app.locales.en import CATALOG
    pat = re.compile("[　-〿" + chr(0xFF00) + "-" + chr(0xFFEF) + "]|[一-鿿]")
    bad = ["  " + repr(v[:50]) + " (key=" + repr(k[:32]) + ")"
           for k, v in CATALOG.items() if pat.search(v)]
    assert not bad, str(len(bad)) + " 条英文值残留中文/全角标点:" + _NL + _NL.join(bad[:12])


def test_i18n_default_zh_is_identity():
    """兼容底座：默认中文时 ui_text 必须原样返回。破了它，所有按中文断言的既有
    测试就不再是回归证明。"""
    from app import i18n
    saved = i18n.get_lang()
    try:
        assert i18n.set_lang("zh") == "zh"
        for k in list(_i18n_literals())[:400]:
            assert i18n.ui_text(k) == k
        # 未知语言代码必须回落中文，而不是显示空白或裸 key
        assert i18n.set_lang("fr-XX") == "zh"
        assert i18n.ui_text("攒句合并") == "攒句合并"
    finally:
        i18n.set_lang(saved)


def test_i18n_no_bare_ampersand_in_english():
    """Qt 的 QPushButton / QCheckBox / 菜单项把 `&` 当助记符吃掉——实测英文按钮
    "Save & apply" 渲染成 "Save  apply"（& 消失、a 变下划线）。词典里不许出现裸 &，
    统一写 and；否则同类 bug 会随任意一次文案增改复发。"""
    from app.locales.en import CATALOG
    bad = [k[:40] + " -> " + CATALOG[k][:40] for k in CATALOG if "&" in CATALOG[k]]
    assert not bad, str(len(bad)) + " 条英文值含裸 &（Qt 按钮会吃掉）:" + _NL + _NL.join(bad[:8])


def test_i18n_ui_language_registered():
    """配置锁：ui_language 必须存在、默认 zh、且只放中英两档（不做小语种）。"""
    from app.config import DEFAULTS
    from app import i18n
    assert DEFAULTS.get("ui_language") == "zh"
    assert [c for c, _n in i18n.SUPPORTED] == ["zh", "en"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        finally:
            _purge_tmp()   # v2.3.20：无论成败都回收本例产生的临时目录
    print(f"UNIT: {len(fns)} tests PASS")
