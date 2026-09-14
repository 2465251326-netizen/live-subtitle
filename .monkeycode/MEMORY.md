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

[长后台任务交给子代理]
- Date: 2026-09-13
- Context: 安装 Qt 系统库时用户中断前台 apt-get 命令后指示
- Instructions:
  - 以后遇到耗时的后台任务（依赖安装、构建、测试跑批等），一律交给子代理（Task 工具）执行，不要在前台长时间阻塞

[Release 资产精简]
- Date: 2026-09-14
- Context: v2.6.5 发版后用户裁定 checksums.txt 多余
- Instructions:
  - GitHub Release 资产只保留安装包 EXE + 便携版 zip，不附 checksums.txt（build.yml 已移除生成步骤）
  - 历史版本 Release 上的校验文件也一并删除（2026-09-14 用户确认，v2.6.4/v2.6.5 已清理）
