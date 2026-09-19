# -*- coding: utf-8 -*-
"""界面语言（i18n）：**中文原文即 key**。

为什么用原文当 key 而不是人造 key 表：本仓库界面文案有 800 余条且一直在改，
`tr("攒句合并")` 这种写法在源码里就能读懂，漏译时缺的是词典一行而不是一个查不到的 key。

兼容底座：`ui_language` 默认 `"zh"`，此时 `tr()` 原样返回入参。所以既有那两百多项
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


def tr(text):
    if not isinstance(text, str) or not text:
        return text
    if _LANG == "zh":
        return text
    hit = catalog().get(text)
    if hit is not None:
        return hit
    _MISSES.add(text)
    return text.translate(_PUNCT).strip()


def misses():
    """运行期查到的未收录串——真机切英文跑一轮后打印它来找漏网。"""
    return sorted(_MISSES)


def clear_misses():
    _MISSES.clear()
