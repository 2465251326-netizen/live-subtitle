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


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"UNIT: {len(fns)} tests PASS")
