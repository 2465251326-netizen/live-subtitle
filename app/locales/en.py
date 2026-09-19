# -*- coding: utf-8 -*-
"""英文界面词典：key 是源码里的中文原文，value 是地道英文界面文案。

写这几条规矩（tests/test_units.py 会锁前两条）：
1. 占位符必须两边一致——中文有 {n}，英文也必须有 {n}，不许改成 %s 或换名。
2. 不许漏：完整性锁扫描源码里每一个 tr("…") 字面量，缺条目即测试失败。
3. 长度：界面里有固定宽度（下拉、按钮、状态行），英文普遍比中文长 30%~60%，
   能短语就别写整句；说明性长句除外。
4. 术语统一，见 GLOSSARY，别一处 Update 一处 Upgrade。
"""

# 术语表（同类控件/概念必须全仓统一）
GLOSSARY = {
    "攒句合并": "batch sentences",
    "低延迟模式": "low-latency mode",
    "推测式增量翻译": "speculative translation",
    "语言包": "language pack",
    "字幕面板": "caption panel",
    "悬浮条": "float bar",
    "回环": "loopback",
    "幻觉抑制": "hallucination filter",
    "误听词典": "mishearing map",
    "译文修正": "translation fix-up",
}

CATALOG = {}
