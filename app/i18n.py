# -*- coding: utf-8 -*-
"""界面语言（i18n）：**中文原文即 key**。

为什么用原文当 key 而不是人造 key 表：本仓库界面文案有 800 余条且一直在改，
`ui_text("攒句合并")` 这种写法在源码里就能读懂，漏译时缺的是词典一行而不是一个查不到的 key。

兼容底座：`ui_language` 默认 `"zh"`，此时 `ui_text()` 原样返回入参。所以既有那两百多项
按中文断言的测试仍然是有效回归证明——默认态一字不变，改了才会红。

英文词典在 `app/locales/en.py`。`tests/test_units.py` 里有一道完整性锁：扫描 UI 源码
收集所有 `tr("…")` 字面量，缺英文条目即失败。也就是说"选了英文却半中半英"是测试失败，
不是靠人自觉。
"""

_LANG = "zh"
_CATALOG = None
_MISSES = set()

# 只做中英两档：本工具的受众是"看不懂中文界面的人"，日/韩/泰等对本项目属小语种，
# 每加一档就多一份永远翻译不完的词典。
SUPPORTED = (("zh", "简体中文"), ("en", "English"))
_SUPPORTED_CODES = frozenset(c for c, _ in SUPPORTED)

# 全角→半角：未收录的串也先把标点转掉，避免英文界面夹着「」（）这种混搭
_PUNCT = str.maketrans({
    "（": " (", "）": ") ", "：": ": ", "，": ", ", "；": "; ",
    "。": ". ", "、": ", ", "！": "! ", "？": "? ",
    "「": '"', "」": '"', "『": '"', "』": '"',
    "“": '"', "”": '"', "‘": "'", "’": "'",
    "—": " - ", "…": "...", "·": " · ",
})


def set_lang(code):
    """设置界面语言；未知代码回落中文（宁可显示中文，也不显示空白或 key）。"""
    global _LANG
    _LANG = code if code in _SUPPORTED_CODES else "zh"
    return _LANG


def get_lang():
    return _LANG


def is_english():
    return _LANG == "en"


def install(config):
    """从配置读取 ui_language（启动早期调用一次即可）。"""
    try:
        return set_lang(str(config.get("ui_language") or "zh"))
    except Exception:
        return set_lang("zh")


def catalog():
    global _CATALOG
    if _CATALOG is None:
        try:
            from app.locales.en import CATALOG
            _CATALOG = dict(CATALOG)
        except Exception:
            _CATALOG = {}
    return _CATALOG


def reload_catalog():
    """词典热替换用（测试与开发期）。"""
    global _CATALOG
    _CATALOG = None
    return catalog()


def ui_text(text):
    if not isinstance(text, str) or not text:
        return text
    if _LANG == "zh":
        return text
    hit = catalog().get(text)
    if hit is not None:
        return hit
    _MISSES.add(text)
    return text.translate(_PUNCT).strip()


def ui_fmt(template, **kw):
    """整句模板 + 命名占位符。带动态值的界面文案一律走这个，**不要**在 f-string
    里拼中文片段：
    1. 英文语序和中文不同（`"已导出 {n} 条"` vs `"Exported {n} captions"`），
       逐片段翻译必然拼出病句；
    2. Python 3.11 的 f-string 替换域里不许出现反斜杠，带 \\n 的片段根本没法内联。
    占位符名两侧必须保持一致（tests 里有锁）。"""
    out = ui_text(template)
    try:
        return out.format(**kw)
    except (KeyError, IndexError):
        # 词典把占位符写坏了：宁可回退中文原句，也不抛到界面去
        _MISSES.add(template)
        try:
            return template.format(**kw)
        except Exception:
            return template


def misses():
    """运行期查到的未收录串——真机切英文跑一轮后打印它来找漏网。"""
    return sorted(_MISSES)


def clear_misses():
    _MISSES.clear()
