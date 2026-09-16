# User Instruction Memory

This file records user instructions, preferences, and teachings for reference in future interactions.

## Format

### User Instruction Entry
User instruction entries should follow this format:

[User Instruction Summary]
- Date: [YYYY-MM-DD]
- Context: [Mentioned scenario or time]
- Instructions:
  - [Content of user teaching or instruction, described line by line]

### Project Knowledge Entry
Entries discovered by the Agent during task execution should follow this format:

[Project Knowledge Summary]
- Date: [YYYY-MM-DD]
- Context: Discovered by Agent while performing [specific task description]
- Category: [Operations & Deployment|Build Methods|Testing Methods|Troubleshooting & Debugging|Workflow & Collaboration|Environment Configuration]
- Instructions:
  - [Specific knowledge points, described line by line]

## Deduplication Strategy
- Before adding a new entry, check for similar or identical instructions.
- If a duplicate is found, skip the new entry or merge it with the existing one.
- When merging, update the context or date information.
- This helps avoid redundant entries and keeps the memory file tidy.

## Entries

[长任务一律交给子代理]
- Date: 2026-09-13（2026-09-16 合并扩写）
- Context: 安装 Qt 系统库时用户中断前台 apt-get 命令后指示；2026-09-16 用户复申"以后长任务交给子代理就行了"
- Instructions:
  - 一切耗时任务都交给子代理（Task 工具）在后台执行，不要在前台长时间阻塞；范围不止构建类，也包括**大代码库的通读/审计/分析**、依赖安装、测试跑批、日志扫描等
  - 前台只做"必须我自己拿主意"的少量动作：定位问题、交叉验证关键结论、和用户确认取舍
  - 注意：子代理的结论属**待验证材料**，不得直接转述给用户（2026-09-16 两次实测：子代理报的"缩进错误""`is` 比较字符串""量纲不统一""`--check-config` 未实现"均为假，真实缺陷要自己复核）

[Release 资产精简]
- Date: 2026-09-14
- Context: v2.6.5 发版后用户裁定 checksums.txt 多余
- Instructions:
  - GitHub Release 资产只保留安装包 EXE + 便携版 zip，不附 checksums.txt（build.yml 已移除生成步骤）
  - 历史版本 Release 上的校验文件也一并删除（2026-09-14 用户确认，v2.6.4/v2.6.5 已清理）
