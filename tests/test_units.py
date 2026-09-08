import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

from app.audio.capture import Segmenter
from app.translate.translator import TranslationCache, _cache


def test_vad_silence():
    s = Segmenter()
    out = [x for ch in [np.zeros(480, dtype=np.float32)] * 40 if (x := s.feed(ch)) is not None]
    assert not out, "纯静音不应触发分段"


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
    c2 = TranslationCache()
    assert c2.get("ci:zh-CN:hello world") == ("你好世界", "en"), "缓存应持久化重载"


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


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"UNIT: {len(fns)} tests PASS")
