# Requirements Document · 译文质量优化（translation-quality）

Updated: 2026-09-13
Status: 需求已确认（范围 R1~R6 全做；R2 全词匹配默认开启；R4 默认高质量档）

## 已确认的决策记录

- 2026-09-13 用户裁决：本轮实施 R1~R6 全部六项；R2「全词匹配」默认开启；R4 离线质量档默认「高质量（beam 5）」
- 候选项 NLLB 大离线模型、备援缓存共享、MyMemory 顺位调整：本轮不做

## Introduction

针对 LiveSubtitle v2.5.3 翻译链路的译文观感问题做一轮质量专项。全部优化点均来自代码审计实锤（现状见文末"代码事实"），不改音频采集与 ASR 识别链路，不改引擎备援顺序的核心行为。

## Glossary

- **修正词典**：`translate_fix_map`（修译文）与 `mishear_map`（修识别原文）两本「错误=正确」用户词典的统称
- **子串替换**：当前词典实现，`str.replace` 全量替换，无词边界
- **词边界匹配**：仅当词条两侧为单词边界（非字母数字）时替换，适用于纯拉丁词条
- **HTML 实体**：`&quot;`、`&amp;`、`&#39;` 等 HTML 转义串
- **离线质量档**：Argos 推理 beam_size 的用户可选档位
- **攒句**：低延迟模式下多个识别片合并为一个翻译输入的机制

## Requirements

### R1 译文 HTML 实体还原

**User Story:** 作为观众，我希望译文中不出现 `&quot;` 这类转义串，这样字幕观感干净。

#### Acceptance Criteria

1. WHEN 任一引擎（google/mymemory/argos）返回的译文包含 HTML 实体，系统 SHALL 在译文进入缓存与上屏前将实体还原为对应字符
2. WHEN 译文中包含双重转义的实体串（如 `&amp;quot;`），系统 SHALL 仅还原一层使双重转义保留为单层实体形态
3. IF 实体还原过程发生异常，系统 SHALL 保留引擎原译文并记录日志

### R2 修正词典语境化替换

**User Story:** 作为用户，我希望「strikes=罢工」这类词条只替换独立成词的 strikes，这样专名（如 "U.S. strikes back"）中的误替换可以避免，多义词可以安全纠错。

#### Acceptance Criteria

1. 系统 SHALL 为修正词典提供「全词匹配」开关，默认关闭，保持与 v2.5.3 相同的子串替换行为
2. WHEN 全词匹配开启且词条为纯拉丁字母/空格/连字符组成，系统 SHALL 仅在词条两侧为词边界时执行替换
3. WHEN 词条包含 CJK 字符或用户未开启全词匹配，系统 SHALL 按子串替换
4. 系统 SHALL 按词条键长度降序应用替换，使长键优先于短键
5. WHEN 一条词条的替换产物命中另一条词条的键，系统 SHALL 仅按降序规则应用一轮替换，用户期望链式纠错时可在词典中显式写出

### R3 翻译缓存键规范化

**User Story:** 作为用户，我希望同样一句话不因首尾空格差异而重复翻译，这样缓存命中率更高、出译文更快。

#### Acceptance Criteria

1. WHEN 构造翻译缓存键，系统 SHALL 对原文执行首尾去空白
2. WHEN 两条原文仅空白差异不同，系统 SHALL 命中同一条缓存记录
3. 系统 SHALL 保持缓存键包含引擎名与源/目标语言维度，备援切换后同文本的译文各自独立缓存

### R4 离线翻译质量档

**User Story:** 作为对 Argos 机翻观感不满意的用户，我可以在设置里把离线翻译切到高质量档（更大 beam），接受稍慢的出词速度换取更连贯的译文。

#### Acceptance Criteria

1. 系统 SHALL 在设置页提供离线翻译质量档：快速（beam_size=2，当前默认）与高质量（beam_size=5）
2. WHEN 用户切换质量档并保存，系统 SHALL 在下一次离线翻译生效，无需重启应用
3. WHILE 高质量档运行，系统 SHALL 保持单块翻译延迟在可接受范围，超时与备援逻辑与现有行为一致
4. 文案 SHALL 如实标注两档的速度差异，快速档标「默认 · 更快」，高质量档标「译文更连贯 · 稍慢」

### R5 修正词典即时生效

**User Story:** 作为正在看直播的用户，我保存词典纠错后，下一条字幕立即应用新词典，无需重启识别管线。

#### Acceptance Criteria

1. WHEN 用户在设置页保存 mishear_map 或 translate_fix_map 改动且管线正在运行，系统 SHALL 将新词典热更新到运行中的 ASR 线程与翻译线程
2. WHEN 热更新完成，系统 SHALL 对后续新字幕立即应用新词典
3. IF 热更新失败，系统 SHALL 沿用重启管线生效的现有路径并提示用户

### R6 死配置清理

**User Story:** 作为维护者，我希望移除不生效的 `translate_zh_from_zh` 配置项，避免误以为它是有效开关。

#### Acceptance Criteria

1. 系统 SHALL 从 DEFAULTS、设置声明表与登记行中移除 `translate_zh_from_zh`
2. WHEN 老版本配置文件中存在该键，系统 SHALL 静默忽略该键，配置下次保存时自然清除
3. 系统 SHALL 保持"检测为中文且目标为中文时回显原文"的现有行为

## 候选但本轮暂缓（待用户裁决）

- **更大离线翻译模型**（NLLB 类）：体积数百 MB 级、首次下载重，Argos en_zh 包 81.7MB 对比明显；译文质量提升空间大但工程量与分发成本高
- **备援缓存共享**（缓存键去引擎化）：译文因引擎而异，去引擎键会让备援译文覆盖主引擎缓存，存在质量回退风险
- **MyMemory 备援顺位调整**：auto 链第二顺位质量一般，但已有限流防护与 60s 重探兜底

## 代码事实（调研摘录，需求依据）

- 全库无 `html.unescape`，Google 免费接口偶发实体原样上屏并写入持久缓存（translator.py 全文无实体处理）
- 两本词典均为无词边界 `str.replace`，大小写敏感，按插入序链式替换（translator.py:317-327、engine.py:238-245）
- 缓存键 `f"{engine}:{target}:{norm_src}:{text}"` 对 text 零规范化（translator.py:421-424）
- Argos 推理 beam_size=2 固定（offline_pack.py:385-387）
- 词典改动需重启管线生效：AsrThread/TranslateThread 构造时注入快照（engine.py:226、translator.py:393-394）
- `translate_zh_from_zh` 已标 hidden 且无读取点（settings_dialog.py:178、translator.py:492）
