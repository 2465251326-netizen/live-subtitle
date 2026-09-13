"""用户修正词典的安全替换器（v2.6.0）。

此前译文修正（translator.apply_fix_map）与误听修正（asr.engine._postprocess）
均为逐条 str.replace：无词边界、按插入序链式应用——「live=直播」会拆坏
「LiveSubtitle」，前一条的替换产物可能被后一条二次命中，多义词（strikes=
罢工）会误伤专名。这里统一为三规则：

- 单轮语义：全部词条编译为一个 alternation 正则，一次遍历替换完成，
  替换产物在当轮不再参与匹配（根除链式误替换）
- 长键优先：分支按键长度降序排列，同位置长短键同时命中时长键胜出
- 词边界：whole_word=True 且词条为纯拉丁时加 \\b 边界（英文整词替换）；
  CJK 词条始终按子串匹配，中文纠错习惯不受影响

线程安全：_COMPILED 的读写走 GIL 原子操作，竞态最坏后果是重复编译一次，
无需加锁；热更新（线程内替换 dict 引用）对读侧天然原子。
"""
import re

from app import log

_LATIN_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]*$")
_CACHE_MAX = 32
_COMPILED = {}   # (items_tuple, whole_word) -> (pattern | None, table)


def is_latin_key(key):
    """纯拉丁词条判定：字母/数字/空格/下划线/连字符组成。"""
    return bool(_LATIN_KEY.match(key))


def _compile(mapping, whole_word):
    """按长键优先构建单轮 alternation 正则；空词典返回 (None, None)。

    空 key/空 value 的词条为 no-op（保持 v2.5.3「空替换值不生效」语义，
    用户词典里常见的半行草稿不会被当删除规则执行）。"""
    items = sorted(
        ((k, v) for k, v in mapping.items() if k and v),
        key=lambda kv: len(kv[0]), reverse=True)
    parts = []
    for wrong, _right in items:
        body = re.escape(wrong)
        if whole_word and is_latin_key(wrong):
            body = r"\b" + body + r"\b"
        parts.append(body)
    pattern = re.compile("|".join(parts)) if parts else None
    return pattern, dict(items)


def _get(mapping, whole_word):
    key = (tuple(mapping.items()), whole_word)
    hit = _COMPILED.get(key)
    if hit is not None:
        return hit
    try:
        entry = _compile(mapping, whole_word)
    except Exception as exc:   # 防御：re.escape 理论不抛，兜底保证可用
        log.exception("fixmap.compile_failed", exc)
        entry = (None, dict(mapping))
    if len(_COMPILED) >= _CACHE_MAX:
        _COMPILED.clear()
    _COMPILED[key] = entry
    return entry


def apply_dict(text, mapping, whole_word=False):
    """按三规则应用修正词典；text/mapping 为空直接原样返回。

    编译异常或 pattern 为 None 时退化为逐条 str.replace（单轮语义由
    调用方词典规模小、字幕文本短兜底，正常路径走不进来）。"""
    if not text or not mapping:
        return text
    pattern, table = _get(mapping, whole_word)
    if pattern is None:
        return text

    def _sub(m):
        return table.get(m.group(0), m.group(0))

    try:
        return pattern.sub(_sub, text)
    except Exception as exc:
        log.exception("fixmap.apply_failed", exc)
        return text
