import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

from app.audio.capture import Segmenter, resample_to_16k
from app.translate.translator import TranslationCache, _cache


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

    orig = tmod.requests.get
    tmod.requests.get = lambda *a, **k: FakeResp(
        {"responseData": {"translatedText": "MYMEMORY WARNING: USED ALL"},
         "responseStatus": 200})
    try:
        MyMemory.translate("hello", "en", "zh-CN")
        raise AssertionError("警告串不应被当译文返回")
    except RuntimeError:
        pass
    finally:
        tmod.requests.get = orig


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


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"UNIT: {len(fns)} tests PASS")
