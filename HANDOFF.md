# 会话交接文档 · LiveSubtitle 实时字幕翻译

> 本文件供**新会话**接手使用。读这一份即可获得全部上下文，无需翻阅历史对话。
> 最后更新：2026-09-12 v2.5.2 发布后（随机探索测试：修复面板 tooltip 拦截点击；遗留工具条右段注入点击失灵待物理鼠标复核，见工作区 RANDOM-TEST-20260912.md）

---

## 一、项目概况

- **仓库**：`https://github.com/2465251326-netizen/live-subtitle`（owner 即用户本人）
- **本地路径**：`C:\deepseek (2)\live-subtitle`
- **技术栈**：Python 3.14（本机 `C:\Python314\python.exe`）+ PySide6（Qt6）+ faster-whisper（CTranslate2）+ pyaudiowpatch（WASAPI 环回采集）
- **功能**：抓取系统声音/麦克风 → 本地语音识别 → 实时翻译 → 主窗口字幕列表 + 悬浮字幕条
- **当前版本**：**v2.5.2**（已发布，含 Setup EXE + portable zip 双资产）

## 二、发版工作流（严格照做，踩过坑）

```powershell
cd "C:\deepseek (2)\live-subtitle"
$env:QT_QPA_PLATFORM = "offscreen"          # 无头测试必须
python tests/test_units.py                   # 38 项单元测试
python tests/test_integration.py             # 66 项集成测试（判定以 test_report.txt 的 TOTAL 行为准，退出码有 Qt 收尾竞态噪声）
python scripts/bump_version.py X.Y.Z         # 同步 app/config.py + setup.iss + version_info.txt
python scripts/bump_version.py --check       # 必须输出「版本一致」
# 更新 CHANGELOG.md 更新日志（v2.3.0 起 README 为门面文档不再内嵌日志；发版说明同时进 Release body）
git add <files>                              # ⚠ 必须包含 app/config.py，否则 CI 版本校验失败
git commit -m "fix|feat: vX.Y.Z——描述"
git push origin main
git tag -a vX.Y.Z -m "说明" && git push origin vX.Y.Z   # tag 触发 CI 构建+发布
# 后台监控（不要阻塞，用 run_in_background）
gh run list --branch vX.Y.Z --limit 1 --json databaseId
gh run view <id> --json status,conclusion     # 轮询到 completed
gh release view vX.Y.Z --json name,assets     # 确认双资产
```

**CI 流程**：push main = 只跑测试；push tag = 测试 + PyInstaller 打包 + EXE 存活检查 + Inno Setup 安装包 + 发布 Release。
**历史坑**：忘 `git add app/config.py` 导致版本校验失败（已犯两次）；`smoke_test.py` 结果靠 `smoke_result.txt` 首行 PASS 判定（退出码不可靠）。

## 三、⚠️ 待办任务（新会话的首要工作）

### ✅ 任务（已完成于 2026-09-10）：真实用户视角的模型端到端测试

**结论：全链路实测通过。** GPU（RTX 2060）+ `large-v3-turbo`（CUDA，缓存加载 1.78s）+ `engine=argos` 离线翻译 + 悬浮条连续流，用 SAPI（Zira en-US）生成英语语音 → SoundPlayer 播放到默认输出 → 环回采集：3 段语音被切分为 9 个字幕段，逐句识别并离线译出（译文见 `Documents\LiveSubtitle_20260910_164327.txt` 导出件与 `trans_cache.json`），主窗字幕卡 1→9 张（UIA 实测），悬浮条区域亮像素 +3973（新译文持续追加），单句端到端延迟约 3~5s。真实 SendInput 验证：`Ctrl+Alt+J` 启停、`Ctrl+Alt+O` 显隐悬浮条均生效。

**关键纠错（本会话踩坑）**：
- ~~"argos/packages 为空需换引擎"~~——**目录名笔误**：应用实际用 `~/.live_subtitle/argos/packs`（见 `offline_pack.py PACKS_DIR`），`en_zh` 包**早已安装**（81.7MB，`list_installed()` 返回 `[('en','zh')]`，真实翻译可用）。engine 无需改动。
- Argos en→zh 直译痕迹重（"pipeline"→输油管、"LiveSubtitle"→升降字幕），属包质量，不是链路故障。

**测试环境经验（本会话新增，勿重复踩）**：
1. 本会话模型**不能读图**（read_image 只回元数据）——验证 UI 内容用：UIA 导出控件树（结构/数量/几何）、`trans_cache.json` 键值差、**像素统计**（LockBits 数亮像素/差分）。
2. Qt 自绘控件（字幕卡/悬浮条/按钮）**UIA Name 全空**，只能拿 ControlType+Rect——数"Custom 卡片的个数变化"是有效证据。
3. `System.Windows.Automation` 在 pwsh7 加载不了；用 **PS5.1**（GAC）跑 UIA dump 脚本可行（见 `tests/deep_windows.py`）。PS5.1 不认 LF 行尾的 `@'...'@` here-string，含 here-string 的 .ps1 必须 CRLF。
4. 独立进程 `RegisterHotKey(0x4003, vk)` 探测热键占用（err=1409 即被占）；Ctrl+Alt+O 首轮失效为启动瞬间被第三方占用（重试即恢复）。~~注册失败时速览卡文案仍显示组合键~~ **已于 v2.2.11 修复**：速览卡改按实际注册结果展示，未生效标红并追加"（未生效）"。
5. SendInput 键入 `INPUT` 必须含 MOUSEINPUT 联合（cbSize=40）；鼠标点击 `type=0`+`MOUSEEVENTF_LEFTDOWN/UP(0x2/0x4)`，先 `SetCursorPos`。
6. 步骤方法备查：SAPI wav 句间自然停顿 0.8~1s → 正好触发自适应静音分段（每句 1 卡，偶尔句中切分属正常）；`SoundPlayer.PlaySync` 阻塞精确，可直接用作时序。

**追加：真实浏览器轮（同日晚，Chrome + YouTube "Me at the zoo" 循环播放）**
- 链路复测通过：视频原句全部识别并离线译出（`Documents\LiveSubtitle_20260910_171309.txt`），日志零 error/warn；端到端 3~5s 体感与合成语音轮一致。
- Argos 质量实锤短板：`trunks(象鼻)`→"战线/前面"；多义词全靠上下文，离线包给不了。用户在意译文观感时，这是换引擎（或换更大离线包）的正当理由。
- 转场/音乐段出"且道甚么处来/你个/……"式乱码字幕 = Whisper 对非语音音频的幻觉——`hallucination_filter` 正是为此存在（当前用户配置为关，属用户选择；文案上可提示"看到没人说话却出字幕=请开幻觉过滤"）。
- 环境教训：本机 Chrome 是**便携版单实例**（用户 GUI 和我的测试窗共用主进程 4828）——清理测试浏览器窗口必须**按标题 `WM_CLOSE` 单窗关闭**，严禁按进程名杀；`ShowWindow(SW_MINIMIZE)` 扫"所有 Chrome 大类窗口"会误伤用户自己的窗口（本次已发生一次，幸无后果）。
- 待查观察：测试中途悬浮条自行从 (429,760) 平移至 (470,701)（尺寸不变）。源码无自动 move 逻辑（仅拖拽/边缩放），疑为自动化注入的杂散鼠标事件所致，人工使用未见复现路径，暂不立案。

<details><summary>原始任务描述（存档）</summary>

用户明确要求：**像正常用户一样使用——GPU 计算 + 体量最大的模型 + 开启连续翻译 + 打开任意英语视频**，测试前先确认当前设置。

**步骤建议**：
1. 打印并核对当前配置（见第五节"用户当前配置"）
2. **先解决已知阻塞**：`engine = 'argos'` 但离线包目录 `~/.live_subtitle/argos/packages` **为空** → 当前翻译引擎无包可用，必然翻译失败。需改为 `google`（在线，需代理）或 `auto`，或在设置页下载离线语言包（英→中）
3. 悬浮条当前 `overlay_enabled = False` → 若要看连续翻译效果需打开（设置-显示 或 `Ctrl+Alt+O` 热键）
4. 播放英语语音：最可靠的方式是 **Windows SAPI 生成 en-US 语音并播放到默认输出设备**（环回采集可直接抓到），或用浏览器播任意英语视频
   ```powershell
   Add-Type -AssemblyName System.Speech
   $s = New-Object System.Speech.Synthesis.SpeechSynthesizer
   $s.SelectVoice('Microsoft Zira Desktop')   # en-US 女声
   $s.SetOutputToWaveFile("$env:TEMP\en_test.wav")
   $s.Speak("...English paragraph...")
   # 然后用 SoundPlayer 播放到默认输出设备，环回即可采集
   ```
5. 启动管线（`large-v3-turbo` + `asr_device=cuda`），观察：主窗口字幕卡是否成对出现（原文+译文）、悬浮条连续流是否累积、延迟是否可接受
6. 日志：`~/.live_subtitle/logs/app.log`（排障必看）

</details>

## 四、用户当前配置（实测于交接时）

| 键 | 值 | 说明 |
|---|---|---|
| `asr_model` | `large-v3-turbo` | 用户要求的最大模型（HF repo: `mobiuslabsgmbh/faster-whisper-large-v3-turbo`） |
| `asr_device` | `cuda` | GPU 档；CUDA 运行时已装（`_torch_cuda_ready()` 返回 True） |
| `engine` | `auto` | v2.2.11 会话经用户批准由 argos 改为 auto（在线优先，失败自动回退离线包；en→zh 离线包仍在 `argos/packs` 可用） |
| `target_lang` | `zh-CN` | |
| `source_type` | `system` | 系统声音（环回） |
| `device_name` | Realtek High Definition Audio [Loopback] | 按名匹配设备（防热插拔漂移） |
| `overlay_enabled` | `True` | 2026-09-10 E2E 测试中经真实 `Ctrl+Alt+O` 打开并持久化 |
| `overlay_stream` | `True` | 连续文本流模式已开 |
| `show_source` | `False` | **只显示译文**（用户明确要求） |
| `instant_caption` | `True` | 流式两段式（原文先上屏、译文补齐） |
| `hotkey_sequence` | `Ctrl+Alt+J` | 用户自己改的（开始/停止） |
| `hotkey_overlay` | `Ctrl+Alt+O` | 显隐悬浮条 |
| `silero_vad` | `False` | 用户关闭 |
| `hallucination_filter` | `True` | v2.2.11 会话经用户批准开启（治理音乐/转场段的幻觉乱码字幕） |
| `proxy_mode` | `system` | 本机代理 `http://127.0.0.1:10808`（环境变量 HTTP_PROXY/HTTPS_PROXY 已设） |
| `close_action` | `tray` | 关闭窗口=最小化到托盘 |
| `max_history` | `200` | |

**机器**：Windows，RTX 2060 6GB（驱动 610.62 / CUDA UMD 13.3），Python 3.14.7（**无 torch**，GPU 走 `nvidia-cublas-cu12` + `nvidia-cudnn-cu12` 独立 wheel）。

## 四点九、引擎/模型选型实测背书（第十二轮矩阵，真人直播各 ~80s）

- turbo+google（默认）：中位说完→译文 4.5s，综合最优
- turbo+argos 离线：纯翻译仅 0.15s 但**整条字幕延迟不变**（瓶颈=攒句等待+分片节奏），机翻味重——定位=断网备胎，不是提速方案
- medium+google：中位 6.4s（更慢），财经语流错听更多，预热更久——大模型在此产品形态下无收益
- 译文词典局限备忘：全局子串替换不分语境（strikes→罢工 类多义词无法安全纠），键要选长到不歧义的短语

## 五、关键知识点（血泪教训，避免重走弯路）

### 5.1 悬浮条（用户最在意、迭代最多）
- **内容形态三模式**：单条 / 列表 / **连续文本流**（`overlay_stream`，用户选定形态）
- 连续流 = `QTextBrowser` + `_stream_parts` 段列表：不断追加进同一段富文本、自动换行、自动滚底、超长从头部淘汰（>1200 字符裁到 700）
- **"只显示译文"必须过滤原文段**：`_stream_visible_kinds()` 依据 `_show_source`；`apply_overlay_from_config` 负责把配置同步到 `overlay._show_source`（曾经漏同步导致开关不生效）
- 悬浮窗**可拖边缩放**（四边四角，`_user_resized` 后禁用 `adjustSize` 覆盖），尺寸持久化 `overlay_w/h`
- 右键菜单：打开设置 / 切换输入来源 / **只显示译文** / 恢复自动大小 / 隐藏

### 5.2 热键（踩过最深的坑）
- **Qt Windows 分发器会把同一条 WM_HOTKEY 两次送达原生过滤器**（最小复现实证：单次 `RegisterHotKey` + 单次 `SendInput` → 过滤器收到 2 条同 id 同 lParam 消息）
- 主热键靠 `toggle_running` 的 0.25s 防抖侥幸掩盖多年；新增热键必须自己去重 → 已在 `hotkey.py` 按 id 做 **60ms 去重**
- **热键类改动必须过真实按键测试**：`SendInput` 注入真实按键（注意 `INPUT` 结构体必须是 40 字节，含 MOUSEINPUT 字段，否则返回 0 静默失败）
- 测试用独立 `LIVETRANSLATE_HOME` + 独立组合键，避免与用户运行中的实例冲突

### 5.3 环境与测试陷阱
- `QT_QPA_PLATFORM=offscreen` 跑无头测试，但 **offscreen 无字体** → 测量文字宽高必须用 `QT_QPA_PLATFORM=windows`（真实 Windows 平台）
- 文字裁剪检测：`scripts/probe_text_clip.py`（真实 Windows 平台逐控件比对所需尺寸 vs 实际尺寸；v2.2.13 起 word-wrap/多行标签按"当前宽度换行后需要高度"比对——旧版只比单行高度，曾漏掉速览卡热键压行 bug 被用户实拍抓包；用 `git worktree` 挂旧代码可做探测器双向验证）
- 集成测试：`tests/test_integration.py`（66 项，覆盖配置/缓存/重采样/字幕面板(成对行·淘汰·清空·收起·拖移契约·空状态·图钉·引导·未读计数·高度收敛)/字幕卡生命周期/热键/设置字段/QSS 括号/向导/退出清理/声明式行表/SRT/呼吸与贴边等体验回归）
- 阻塞式 `stream.read` 在静音环回上会挂死 → 探测脚本必须轮询 `get_read_available`
- 探测脚本用完即删，产物走 `.gitignore`

### 5.4 已修复的高危问题（勿回退）
- 翻译缓存未加载即 `save()` 会清空整个持久缓存（H1）→ `_save_locked` 加 `if not self._loaded: return`
- 字幕被 `max_history` 裁剪时 `_pending` 悬挂引用 → 裁剪时同步移除配对
- 模型加载期停止的孤儿线程构造完成后不得入池（`_stop` 后置检查）
- 跨块 FIR 抗混叠滤波（`resample_to_16k(..., carry_key=...)`）避免 30ms 块边界调制
- 迁移"目标目录非空"拒绝类异常需透传（`_MigrationRefused`）
- 尾句 flush 去掉 `not self._stop` 条件

### 5.5 用户偏好（重要）
- 要求**实现全部需求**（"全部都做"），功能尽量做成**设置里的开关**
- 关注视觉细节与文案准确性：**文案不许承诺未实现的功能**（Ctrl+Alt+O 与托盘"切换输入来源"两次因此被批评）
- 反馈直接、要求高：会实测并截图指出问题，测试必须**真实验证**而非调用内部函数自证
- 中文沟通

## 六、仓库结构速查

```
app/config.py            DEFAULTS 配置白名单 + 存储根迁移
app/hotkey.py            全局热键（双发去重在此）
app/asr/engine.py        AsrThread：模型下载/加载/GPU 检测/转写/背压
app/audio/capture.py     CaptureThread、设备解析、重采样+FIR、能量 VAD 分段
app/translate/translator.py  TranslateThread、三引擎备援链、翻译缓存
app/ui/main_window.py    主窗口、字幕卡、托盘、状态横幅、空页面速览卡
app/ui/caption_overlay.py 悬浮条（连续文本流/列表/单条 + 缩放 + 淡入）
app/ui/settings_dialog.py 设置页（声明式 _FIELD_SPECS 驱动）
app/ui/first_run.py      首启向导
scripts/bump_version.py  版本同步（唯一正确入口）
scripts/probe_text_clip.py 文字裁剪探测
tests/test_units.py      38 项单元测试
tests/test_integration.py 66 项集成测试
ROADMAP.md               开发历程（每版本一节，含根因分析）
docs/UX-REPORT.md        模拟用户体验报告归档（R6 CBS 新闻配置归因+处置+验收数据）
CHANGELOG.md             更新日志（用户可见；README 只留链接，v2.3.0 起）
README.md                门面：亮点/下载/反馈/使用详解/FAQ（勿把日志塞回去）
.github/ISSUE_TEMPLATE/  反馈框架：bug_report.yml（🐞BUG）/ suggestion.yml（💡建议）/ config.yml（禁空白 Issue）
```

## 七、新会话开场建议

> 「读 `HANDOFF.md` 接手 LiveSubtitle 项目。当前 v2.2.11 已发布，第三节的端到端测试任务已完成（全链路通过，argos 离线包其实一直在，前文『packages 为空』是笔误）。」

**注意事项**：
- 工作区里的 `.session-archive.md` **含令牌等敏感信息，已加入 .gitignore，不要读取或提交**
- `build_env/`（约 680MB CUDA wheel）已 gitignore，勿删除（本机 GPU 依赖）
- 遇到不确定是否要改用户配置时，先问

## 八、会话快照（2026-09-10 v2.3.0 发布后 · 上下文压缩存档）

- **已完成**：两轮 E2E → v2.2.11（六修复+SRT 导出）→ v2.2.12（硬件预选/全屏隐藏开关/聆听呼吸/SRT 折行/CI pull_request）→ v2.2.13（用户实拍速览卡版式修复+探测器盲区补强）→ v2.2.14（用户裁决：删全屏隐藏+下载 ETA）→ v2.3.0（声明式设置框架迁移，行为不变）。提交链：`2326886` → `bc2d3a9` → `8c0a553` → `d5e77ee` → `343f583` → `3821b7c` → `aeb2fb1`(v2.3.2 G1/G2) → `fe12795`(v2.3.3 P1/P2) → `9e07c3d`(v2.3.4 碎片漏网修) → `ab5a0af`(v2.3.5 P5 预热) → `c36c9cf`(v2.3.6 P7/P8+套件根治) → `5bcf032`(v2.3.7) → `25d9e35`(v2.3.8 攒句判据修正) → `30ee526`(v2.3.9 兜底窗口>分片周期，实机验收通过) → `99efde5`(v2.3.10 P11 判定同源) → `8a0806a`(v2.3.11 指引 25s) → `ca1220b`(v2.3.12 P13 无包静音告警) → `804efae`(v2.3.13 P14 卡右键纠错) → `5e637cf`(v2.3.14 P16/P17 速度专项) → `34a4fc1`(v2.3.15 P19 量纲修复) → `bd626aa`(v2.3.16 P21 契约审计) → `1c8a3f3`(v2.3.17 P22 占位可见) → `754177f`(v2.3.18 P23 寿命封顶) → `74299fc`(v2.3.19 P25 穿透三件套) → `2d39db8`(test 自愈) → `e587720`(v2.3.20 P26 延迟遥测) → `a8bca12`(v2.3.21 P28/P29 悬浮条补完) → `b3116f6`(v2.4.0 P31 字幕面板) → `fccb279`(v2.4.1 面板把玩三修) → `e9566b6`(v2.4.2 面板灰块修) → `1898dc7`(v2.4.3 面板增强A~E+高度贴内容修复) → `af40165`(v2.4.4 摸底轮九项修复) → `3886f4a`(v2.5.0 悬浮面板深度迭代) → `56b5656`(v2.5.1 新闻实测两修) → `4ab85ec`(v2.5.2 随机测试 tooltip 拦截修复) ，各版 tag CI success、双资产核对在位
- **用户裁决记录**：全屏自动隐藏功能经用户 F11 实测判定"不需要"→ v2.2.14 已全删（教训：**新功能上线前要有"用户要不要"这道闸**）；说话人标签、CI 弃用告警清理=明确不做
- **红线教训（v2.2.12 体验轮，用户受惊，郑重记录）**：为测"全屏自动隐藏"在用户桌面开了 7 秒覆盖全屏的蓝色无边框窗，直接遮住用户聊天界面——**占屏测试必须先预警/约定，或改纯逻辑测试**。且该法本身无效：PowerShell 进程 Show/Activate 拿不到 `GetForegroundWindow`，自动化根本测不成"活体全屏"，别再试
- **本机运行时**：应用未在运行；用户 DSH 聊天窗=便携版 Chrome 单实例（清理测试窗按标题 WM_CLOSE，严禁杀进程）。隔离实例复用大模型缓存的正规姿势：启动前注入 `HF_HOME` → `~\.live_subtitle\hf`（app 用 setdefault 不覆盖注入值）+ `LIVETRANSLATE_HOME` 隔离配置，免重下 1.6GB
- **验证产物**：`Documents\LiveSubtitle_20260910_164327/171309/191510.txt`（三轮导出）、`%TEMP%\ls_e2e_s1..s8*.png`（本会话模型不能读图，供人眼复核；用户已明示**保留**）；日志 `~/.live_subtitle/logs/app.log`
- **方法论**（复测照抄即最快路径）：SAPI en-US 分句 wav（句间 Sleep）→SoundPlayer 播放=等价英语视频；UIA 数卡片个数（Qt 自绘 Name 全空，数结构有效）；`trans_cache.json` 键值差=识别+翻译铁证（**注意 flush 批处理 10 条/5s，读早了会误判"没产出"，以导出件为准**）；LockBits 亮像素统计=悬浮条内容级证据；导出按钮真实点击+Enter=免费拿全卡文本（默认名落 Documents）；点击前先激活主窗（浏览器覆盖时点击会落错窗，本会话踩两次）；git worktree 挂旧提交=探测器/回归的双向验证利器
- **用户配置变更记录（2026-09-10 深夜，PM 裁决+用户认可）**：模拟用户 CBS 新闻二轮体验归因"medium+CPU+argos+直连"=体验差根因，PM 拍板五项改动：asr_model→large-v3-turbo、asr_device→cuda、engine→auto、proxy_mode→system、overlay_enabled→True；备份 ~/.live_subtitle/config.json.bak-20260910_235823。第三轮验收：google 引擎恢复、90s/15 条新缓存(热缓存命中多)、字幕滞后收敛到稳定管线深度（不再滚雪球）
- **发版后观察点**：`pipeline.no_segments_hint` 日志键是否出现；速览卡热键红字在真实占用下是否显示；下载大模型时 ETA 文案观感；**教训：用户眼睛>自动化探测——探测报 0 ≠ 无问题，关键 UI 需真实平台实拍复核（版式 bug 即用户截图抓包，v2.2.9 假修复至此暴露）**
- **遗留排期候选**：仅剩 SRT 说话人标签（用户已明确**不做**，除非未来改主意）；声明式框架已于 v2.3.0 完成
- **设置页开发规约（v2.3.0 起）**：新增"键→单控件"型设置=三处登记（DEFAULTS + _FIELD_SPECS + _STD_ROWS），不要再手写控件/同步行；复合控件才允许手写
- **工具坑位新增**：Add-Type 里方法名 `Main` 被当入口点报"签名错误"（改名即过）；`gh --jq` 表达式含空格须用单引号（pwsh 双引号会被拆参数）；`FsTest` 类不跨 pwsh 调用存活（每次内联重定义）
