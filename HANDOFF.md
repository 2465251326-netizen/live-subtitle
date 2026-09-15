# 会话交接文档 · LiveSubtitle 实时字幕翻译

> 本文件供**新会话**接手使用。读这一份即可获得全部上下文，无需翻阅历史对话。
> 最后更新：2026-09-15 晚 v2.8.0 发布（译文速度专项，真机 A/B 实测 5.13s→0.10s；见第十三节）
> ⚠️ v2.7.5 由上一会话发布但**当时漏更新本文件**，其变更详情见 CHANGELOG.md（8 项审计修复）

---

## 一、项目概况

- **仓库**：`https://github.com/2465251326-netizen/live-subtitle`（owner 即用户本人）
- **本地路径**：`C:\deepseek (2)\live-subtitle`
- **技术栈**：Python 3.14（本机 `C:\Python314\python.exe`）+ PySide6（Qt6）+ faster-whisper（CTranslate2）+ pyaudiowpatch（WASAPI 环回采集）
- **功能**：抓取系统声音/麦克风 → 本地语音识别 → 实时翻译 → 主窗口字幕列表 + 悬浮字幕条
- **当前版本**：**v2.8.0**（已发布，含 Setup EXE + portable zip 双资产）

## 二、发版工作流（严格照做，踩过坑）

```powershell
cd "C:\deepseek (2)\live-subtitle"
$env:QT_QPA_PLATFORM = "offscreen"          # 无头测试必须
python tests/test_units.py                   # 38 项单元测试
python tests/test_integration.py             # 69 项集成测试（判定以 test_report.txt 的 TOTAL 行为准，退出码有 Qt 收尾竞态噪声）
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
- 集成测试：`tests/test_integration.py`（69 项，覆盖配置/缓存/重采样/字幕面板(成对行·淘汰·清空·收起·拖移契约·空状态·图钉·引导·未读计数·高度收敛)/字幕卡生命周期/热键/设置字段/QSS 括号/向导/退出清理/声明式行表/SRT/呼吸与贴边等体验回归）
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
- **开发前有任何疑问随时问用户**（2026-09-15 用户给予常设授权）——需求边界、方案取舍、是否动系统/用户配置，先问后做，别自己猜

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
tests/test_units.py      73 项单元测试
tests/test_integration.py 92 项集成测试
docs/UX-REPORT-R7.md     体验审查报告（R7：UI 全量走查 + 修复状态）
CHANGELOG.md             更新日志（用户可见；README 只留链接，v2.3.0 起）
README.md                门面：亮点/下载/反馈/使用详解/FAQ（勿把日志塞回去）
.github/ISSUE_TEMPLATE/  反馈框架：bug_report.yml（🐞BUG）/ suggestion.yml（💡建议）/ config.yml（禁空白 Issue）
```

## 七、新会话开场建议

> 「读 `HANDOFF.md` 接手 LiveSubtitle 项目。当前 v2.8.0 已发布（译文速度专项：推测式增量翻译，真机实测译文感知延迟 5.13s→0.10s）。第十三节有本轮的延迟结构定性、A/B 方法论与被否决方案留档。」

**注意事项**：
- 工作区里的 `.session-archive.md` **含令牌等敏感信息，已加入 .gitignore，不要读取或提交**
- `build_env/`（约 680MB CUDA wheel）已 gitignore，勿删除（本机 GPU 依赖）
- 遇到不确定是否要改用户配置时，先问

## 八、会话快照（2026-09-10 v2.3.0 发布后 · 上下文压缩存档）

- **已完成**：两轮 E2E → v2.2.11（六修复+SRT 导出）→ v2.2.12（硬件预选/全屏隐藏开关/聆听呼吸/SRT 折行/CI pull_request）→ v2.2.13（用户实拍速览卡版式修复+探测器盲区补强）→ v2.2.14（用户裁决：删全屏隐藏+下载 ETA）→ v2.3.0（声明式设置框架迁移，行为不变）。提交链：`2326886` → `bc2d3a9` → `8c0a553` → `d5e77ee` → `343f583` → `3821b7c` → `aeb2fb1`(v2.3.2 G1/G2) → `fe12795`(v2.3.3 P1/P2) → `9e07c3d`(v2.3.4 碎片漏网修) → `ab5a0af`(v2.3.5 P5 预热) → `c36c9cf`(v2.3.6 P7/P8+套件根治) → `5bcf032`(v2.3.7) → `25d9e35`(v2.3.8 攒句判据修正) → `30ee526`(v2.3.9 兜底窗口>分片周期，实机验收通过) → `99efde5`(v2.3.10 P11 判定同源) → `8a0806a`(v2.3.11 指引 25s) → `ca1220b`(v2.3.12 P13 无包静音告警) → `804efae`(v2.3.13 P14 卡右键纠错) → `5e637cf`(v2.3.14 P16/P17 速度专项) → `34a4fc1`(v2.3.15 P19 量纲修复) → `bd626aa`(v2.3.16 P21 契约审计) → `1c8a3f3`(v2.3.17 P22 占位可见) → `754177f`(v2.3.18 P23 寿命封顶) → `74299fc`(v2.3.19 P25 穿透三件套) → `2d39db8`(test 自愈) → `e587720`(v2.3.20 P26 延迟遥测) → `a8bca12`(v2.3.21 P28/P29 悬浮条补完) → `b3116f6`(v2.4.0 P31 字幕面板) → `fccb279`(v2.4.1 面板把玩三修) → `e9566b6`(v2.4.2 面板灰块修) → `1898dc7`(v2.4.3 面板增强A~E+高度贴内容修复) → `af40165`(v2.4.4 摸底轮九项修复) → `3886f4a`(v2.5.0 悬浮面板深度迭代) → `56b5656`(v2.5.1 新闻实测两修) → `4ab85ec`(v2.5.2 随机测试 tooltip 拦截修复) → `a4b312e`(v2.5.3 用户实拍三修) ，各版 tag CI success、双资产核对在位
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

## 九、会话快照（2026-09-14 v2.6.6 瞬窗修复）

- **v2.6.6 干了什么**：修"翻译时每句闪一个 0.04s 的无题小窗"（用户实测反馈）。根因=`caption_overlay._add_row` 里原文标签无父构造后**先 `setVisible(True)` 再 `addWidget` 收编**——未收编的 QWidget 被 show 即成原生顶层窗口，收编瞬间又销毁。开"同时显示原文"必现（v2.5.0 行卡片化遗留）。修法=构造即传父+可见性切换后置（双保险）。真实平台整场翻译实测闪窗 **19→0**；集成回归锁+1（"行标签永不成顶层窗口"），套件 单元 56/56、集成 82/82。
- **瞬窗取证方法（新增方法论）**：① Win32 EnumWindows 30ms 轮询快照差分（CREATE/DESTROY/SHOW/HIDE+存活时长，Python+ctypes 写，避开 PS5.1 here-string/&& 坑）锁定短命窗指纹；② 给 QApplication 装事件过滤器，在所有顶层窗口 Show 瞬间记 `类名/objectName/尺寸/Python 栈`——objectName 直接自报家门（`PanelSrc`×每句一次）。两件套=瞬窗类 bug 的标准探法。
- **挂起线索（用户裁决：都不急，先放着，2026-09-14）**：
  1. `pipeline.orphan_thread | cls=TranslateThread`——**每次停止翻译必现**（3s 等待超时进孤儿容器），疑似网络等待无截止导致；对用户暂无感，是"越用越沉/退出未净"类偶发问题的头号嫌疑。
  2. 集成测试环境脆弱：`tests/itest_home/config.json` 缺 `wizard_done` 标记时，构造 MainWindow 后排的 400ms 向导会在后续 `processEvents()` 处弹**模态阻塞挂死**（瘦身误删该目录时踩过，补 `{"wizard_done": true}` 即愈）；另 `TOTAL` 行只打 stdout 不落 `test_report.txt`。均为工具箱问题，不影响产品。
- **本轮工具坑（勿重踩）**：本会话 pwsh 是 5.1——`&&` 不可用、`if (git xx --is-ancestor)` 判 stdout 不判退出码（要单独读 `$LASTEXITCODE`）；`time.strftime` 无 `%f` 秒毫秒指令（Windows 直接 ValueError）；控制台 GBK 打 `✕` 崩 print——跑测试套件带 `PYTHONIOENCODING=utf-8`；PowerShell 管道下 git 进度走 stderr 显示为红字异常，非失败。
- **代理坑（重要）**：本机代理(127.0.0.1:10808)会**随机掐断大流量 git fetch 尾部**（curl 56），且"还差 N bytes"只指当前分片——曾据此误判"快成了"连打 30 次重试，白灌 ~4GB（`tmp_pack_*` 残骸堆进 .git，删残骸即愈）。大传输失败别再硬刷重试，先想包体多大。

## 十、会话快照（2026-09-15 v2.7.0/v2.7.1 识别+翻译优化批）

- **v2.7.0 七项**（用户只关心识别与翻译；子代理全链路审计→逐条实现→真机验证）：①面板译文配对修复（多待决行并存、按原文精确匹配、merged_from 收编攒句前片——旧"单待决槽"会被下一句覆盖导致迟到译文配错行）②提前冲句 early_flush（静默地板 3.5s→2.0s，实测 hold_p50 6.0→1.5s；定位=末句抢救，连续语流中本句译文仍由下一句到达冲刷=两轨制固有）③热词 asr_hotwords（whisper initial_prompt，真机 A/B：无提示自造专名错听成 Thragedem/Veselon，有提示 3/3 全对）④语言锁复检 lang_recheck（auto 锁定后每 20 段解除约束重听，高置信≥0.8 不一致才切换；旧锁定=终身，conf<0.6 自愈支路在传 language 后永不触发——whisper 锁定返回概率恒 1）⑤argos 包后台预载（首句翻译不再现场加载数秒冻结队列）⑥预热补完 _warmup 最后一公里（首句不再慢 1~3s）⑦google 跟随 whisper 语言（不再逐句 sl=auto 重猜）。套件 61/84→加 v2.7.1 后 62/84。
- **v2.7.1**：engine_auto_fallback 开关（用户点名要"是否启动自动切换"）——关=固定引擎失败只报错不降级，双向单元锁。
- **挂起案根因已明（仍未修，用户裁决先放着）**：`orphan_thread|TranslateThread` 每次停止必现的机制=`DRAIN_GRACE=15s`（v2.6.2 排水宽限）与 stop 等待 3s 结构性错配——队列空时线程也要等满 15s 宽限才退，3s 处必判孤儿。修法方向：宽限只对"等尾句到达"生效（asr 已退且队列空→立即退），或对齐两处时限。
- **审计证伪记录（防再犯）**：审计提出"whisper seg.end 段内尾静音"做提前冲证据——真机证伪：**whisper 把末片结束时间拉伸补齐到音频尾，tail_q 恒≈0**，信号不存在；text_ready 现仍携带 tail_q/last_lp 两参（无害，留作后用），判据只用时间地板。
- **本轮新坑**：`Config.get()` 只收一个键参（无 default 位）——写 `c.get("k", True)` 在 start_pipeline 里抛 TypeError，PySide6 吞槽异常只打 stderr（分离进程不可见），症状=asr/capture 线程根本没建、会话静默零字幕、日志只有 translate 活着；**构造参数改动必须活体跑一轮**，纯单测抓不到（既有测试全用单参 get）。`Start-Process` PS5.1 无 `-Environment` 参数（用 `$env:` 继承）。隔离实例做 A/B 的标准姿势再证：`LIVETRANSLATE_HOME`+`HF_HOME` 注入 + 隔离 config 预写 `wizard_done/auto_start`，翻译失败时**fallback 请求 URL 的 q= 参数就是识别原文**——白嫖识别结果取证通道。

## 十一、会话快照（2026-09-15 下午 v2.7.1/v2.7.2）

- **v2.7.1**：`engine_auto_fallback` 开关（用户点名"引擎是否自动切换"）——关=固定引擎失败只报错不降级备援；双向单元锁。
- **v2.7.2**：`perf_turbo` 榨干模式捆绑开关（用户："还想更快，榨干硬件"）=GPU INT8（int8_float16，显存约半、识别中位 0.44→0.41s 实测有限提速，文案如实）+ 运行期进程 HIGH 优先级（start/stop 挂 `_apply_process_priority`）+ 连续语流切段 6s→4s（25s 语流交付 3→4 段）+ 冲句地板 2.0→1.2s；**PrewarmWorker 必须透传 turbo**（池键含 compute_type，不透传=预热白建 fp16 实例）。套件 65/84。
- **硬件基线**：i5-10600KF 6C12T / RTX 2060 6GB（空闲 1365/2100MHz）/ 16G / NVMe；**电源计划=平衡**（在压频，切高性能是用户侧待办，命令已给）。用户当前 asr_accuracy=quality（三档最慢）——已建议看直播切 fast，用户未表态。
- **A/B 基准方法（留档）**：隔离实例+无停顿长句单条 wav（25.6s）跑两轮，`CloseMainWindow` 优雅退出保 `pipeline.latency` 落盘（Stop-Process 会跳过遥测汇总——踩过）；对比 n_reco/reco_p50/hold_p50。
- **常设授权（新）**：开发前有任何问题随时问用户，先问后做不猜。
- **v2.7.3 孤儿线程案销账**（用户批准修复）：根因确认=排水宽限 15s 与停止等待 2.5s 错配 + 主线程 wait 期间无人消费排队信号（尾句转发源被掐死）。修法=三件套：`TranslateThread.close_input()` 输入门 + 主窗 `_on_asr_finished`（身份守卫：冲刷残组→关门，按序排在全部尾句转发之后）+ stop_pipeline **不等翻译线程**（日志改 `pipeline.translate_draining` 诚实标签）。真机验证：停止后 <1s 退出、尾句翻译不丢、orphan_thread 不再出现。
- **同日用户侧变更**：电源计划切「高性能」（用户授权我执行）；用户配置 asr_accuracy quality→fast（用途=看英语视频，改前经问答确认）；YouTube 视频内容未抓到（页面截断），热词建议留给用户自填。

## 十二、会话快照（2026-09-15 晚 v2.7.4 地毯式大整顿·子代理接手收尾）

- **背景**：父代理完成 v2.7.4 产品手术（15 项）后会话被中转站 400 掐断，遗留=4 个集成测试被新守卫打破（stub 缺 `isRunning`、G1 断言旧硬编码文案）。子代理接手：修 stub 三处 + G1 升级为 B-9 三分支断言（注册成功/配置未注册/未配置）→ 全套 68+88 绿 → 活体冒烟（真会话：draining 零孤儿、tr_p50=0.04 真翻译、hold_p50=0.49）→ bump 2.7.4 → CHANGELOG 15 条 → 提交推送打 tag。
- **v2.7.4 内容速查**（详见 CHANGELOG）：A-1 指针单键随 save 同步（迁移回滚/覆盖双修）；A-2 `Config._coerce` 逐键消毒（"18px"型毒药实测）；A-3 退出链 `close_input`+`wait(2000)`（不再强杀翻译线程）；B-1 `_flush_tgroup` 死队列守卫；B-2 `AsrThread.submit` 尾段身份去重（排队+直塞双通道）；B-3 GPU/CUDA worker 重入闸；B-4 设置重入有暂存只前置不 reload；B-6 默认输出项恒在首位；B-7 透明度 0-100；B-8 面板整句生长（后缀匹配+combined 上屏）；B-9 速览卡热键文案实况化；B-12 导出过滤统一+停止时占位终态化；C-2/C-5/C-9/C-10/C-11 死代码/重复连接/auto primary 恒 google/下载扫描降频/词典热更统一；向导文案纠旧；5 面板键登记+双向断言。
- **接手教训（新）**：产品加了 `tr.isRunning()` 这类接口调用，**所有测试 stub 要同步扫**（grep 调用点在测试里的替身类）；文案从硬编码改实况分支后，断言旧文案的测试要升级为断言全部分支。父代理断点=「回归锁补充」做到一半：新锁已进（毒药/登记/导出/指针 roundtrip/尾段去重），旧锁适配未做——**接手时先跑全套看红点，红点即断点**。
- **QA 工具留盘**：`scripts/qa/`（qa_toolkit.ps1 活体测试台 + winflash_probe.py 窗口事件差分器 + winflash.txt 440 行零闪窗证据）已 gitignore，供后续会话复用。

## 十三、会话快照（2026-09-15 深夜 v2.8.0 译文速度专项）

### 13.1 起点与延迟定性（先测量再动手，别猜因果）

- **用户诉求**："翻译速度还是太慢"。开局先读 `~/.live_subtitle/logs/app.log` 的 `pipeline.latency` 遥测（用户 9-15 真机会话，最大样本 n_reco=110）：
  `reco_p50=0.55s`、`tr_p50=0.06s`、**`hold_p50=4.13s`（p95 7.25s）**。识别+翻译只占 13%，**攒句等待占 87%**。
- **决定性对照**：13:00 场（turbo 关，分段上限 6s）`hold_p50=6.04`；18:36 后（turbo 开，上限 4s）`hold_p50=4.03~4.43`——**hold 恒等于分段周期**，因为两轨制下"本句译文由下一片到达冲刷"，而下一片间隔=分段周期。代码注释自己也承认"两轨制固有，hold≈6s 不变"；`early_flush`（v2.7.0）只救末句，连续语流一点忙都帮不上。
- **用户当时配置**（`C:\Users\Administrator\.live_subtitle\config.json`，注意 USERPROFILE 是 Administrator 不是 Loomy 运行账号）：`large-v3-turbo` + `cuda` + `engine=argos` + `engine_auto_fallback=false` + `asr_accuracy=fast` + `perf_turbo=true` + `low_latency_mode=true` + `silero_vad=false`。

### 13.2 方案 A：推测式增量翻译（唯一被证明有效的方案，51 倍）

- **思路**：碎片一到达就翻译"目前攒到的文本"并上屏，下一片到达送更长版本，译文在同一张卡／同一面板行**原地生长覆盖**；整句终版随后接管终态。
- **实现落点**（全部走"独立新通道"，既有通路零改动，这是测试全绿的关键）：
  - `translator.py`：`submit(text, lang, spec=False)` 第三参；队列项升级为 `(text, lang, spec)` 三元组（消费侧兼容裸二元组）；**新增独立信号 `spec_result_ready`**（不是给 `result_ready` 加第 6 参——现有测试全是 5 参 lambda，改签名会全线炸）；spec 请求**不组建备援链**（备援会改写 `_active_engine` 造成引擎漂移）；队列满时 spec **放弃自己绝不挤掉终版**（`dropped` 恒回二元组，保住主窗 `for d_text, _d_lang in ...` 解包契约）。
  - `main_window.py`：`CaptionCard.spec` 标记 + `set_spec_result()`（只刷译文）+ `finalize_spec()`（停止时**保留半句译文**、标注"可能不完整"，不覆盖成失败文案）；`is_pending()` 对 spec 态返回 True（终版还要靠它配对）；`translated_text()` 对 spec 态返回已有译文；`_peek_pending()`（查卡不摘走）；`_tgroup_gen` 代数 + `_spec_inflight` 簿记；`_combine_pieces()` 提取为函数（**冲刷与推测必须算出完全相同的 combined，否则终版配不上簿记**）；`_on_spec_translated` 不计数/不切聚焦/不动横幅。
  - `caption_overlay.py`：`update_spec_result()` —— **原文行必须与译文一起生长**，否则重现 v2.7.4（B-8）"半句原文配整句译文"分叉；不动 `_last_result`、不计未读。
  - `config.py`：`_coerce` **补 float 分支**（`segment_cap_s` 是项目首个 float 键，旧消毒链没有 float 分支=零校验；顺带保证 int→float 归一，combo `findData` 才匹配得上）。
- **闸门**：`_spec_enabled()` = 开关 && 低延迟模式（否则不攒句、无可推测）&& **实际引擎 == argos**。在线引擎一律退回整句（MyMemory 每天约 5000 字符免费额度，逐片加发会成倍消耗）。
- **真机 A/B 结果**（连续语流素材，同模型同 GPU，两轮独立复跑）：
  | 组 | 译文感知延迟 | n_reco | reco_p50 | hold_p50 |
  |---|---|---|---|---|
  | baseline（spec 关） | hold 5.04 + tr 0.09 = **5.13s** | 8 | 0.52 | 5.04 |
  | new（spec 开） | **spec_p50 = 0.10s** | 8 | 0.52 | 4.98 |
  → **51.3 倍**，识别侧零副作用。回归锁：单元 +5、集成 +4。

### 13.3 方案 C：分段上限可调（`segment_cap_s`，默认 4.0）

- 用户先选 4s→2.5s，实测数据出来后**裁决收回 4.0**：真实素材 2.5s 档有 **57~71%** 的句子在上限处被硬切（4s 档 0~17%），而 A 已让译文随碎片立即上屏，上限大小对"译文迟到"影响已很小——激进档只留作可选项。
- 默认 4.0 恰等于榨干模式内置值，所以**默认行为不变**，只是多给一个调节档位（2.5/3/4/6/10/0=跟随模式）。

### 13.4 方案 B：神经 VAD —— 完整实现后经实测**否决并回退**（重要留档，勿重做）

- **当初的错误推断**：见 `hold≈分段上限` 就推"能量 VAD 在有背景乐时把停顿判成还在说话、静音判停失效"。据此实现了 Silero 逐块判定（`get_vad_model()`，512 样本=32ms 一块，滞回 0.50 进/0.35 出），实测开销 **0.136ms/块 = 占空 0.42%**，确实等于免费。
- **实测否决**（15 句 ×1.5s、句间 0.95s 静音的短句素材，让"在哪切"由停顿判定而非分段上限决定）：
  | 素材 | 能量判据 | 神经判据 |
  |---|---|---|
  | 纯净短句 | 15 句→15 段，硬切 0%，中位 1.50s | 15 段，0%，1.59s |
  | 短句 + rms 0.03 持续背景乐 | **15 段，硬切 0%，中位 1.50s** | 15 段，0%，**2.16s** |
  能量判据的**自适应噪声底**（`threshold=max(noise_floor*3, 0.004)`，噪声底上限 0.02 → 阈值最高 0.06）本就压得住稳定背景乐；神经滞回反而多抱 0.66s 尾音与配乐。
- **真因纠正**：`hold ≈ 分段上限` 是因为**句长超过上限被强制切段**（SAPI 长句 3~5s > 4s 上限），停顿根本没机会触发判定。诊断证据：停顿区 `rms` 中位 = **0.00000**（数字静音）、neural prob 中位 = **0.024**——两种判据都**正确识别**了停顿。
- **处置**：用户裁决"回退 B"。已拆净 `capture.py` 的 `_init_neural_vad/_neural_voiced/voiced_override`、`DEFAULTS.neural_vad`、设置页三处登记、管线传参与 2 个单测；`Segmenter.feed` docstring 留实测数据档；结论固化为 `test_energy_vad_beats_steady_bgm`（合成"纯背景乐预热 10s + 15 短句"素材，断言零硬切且段长中位 <2.2s）。
- **教训**：瓶颈定位不能只看"hold 等于某个参数"就推因果——要造**能让该机制成为决定因素**的素材去反证（长句素材下 VAD 根本不参与判定，测了也白测）。

### 13.5 本轮新增环境坑（勿重踩）

- **git 没配代理、shell 里也没有 HTTP_PROXY 环境变量** → git 直连 `github.com:443` 必失败（`ls-remote` 偶尔能通是运气）。正解：单命令参数 `git -c http.proxy=http://127.0.0.1:10808 -c https.proxy=... fetch/push`，**不必也不该改全局 git config**（系统级 `credential.helper=manager` 的 GCM 凭据本身有效，`gh auth setup-git` 非必需）。
- **严禁 `git fetch --tags`/`git fetch origin`（不带 refspec）**：默认 refspec 连带**所有远程分支**，那些 feature 分支里有大对象，实测 120s 超时并在 `.git` 留下 176MB 垃圾包 `tmp_pack_*`（仓库本体只 1.3MiB）。正解：`git fetch origin +refs/heads/main:refs/remotes/origin/main --no-tags`，tag 单独 `+refs/tags/vX:refs/tags/vX`；事后 `git count-objects -vH` 查 `garbage`，删掉即愈。
- **本地曾落后远程一整版**（远程已发 v2.7.5、本地停在 v2.7.4、`APP_VERSION` 也是旧的）。**接手第一件事先比对** `gh api repos/<o>/<r>/commits/main --jq .sha` vs `git rev-parse main`，否则 bump 会撞已存在版本号。
- **正牌解释器是 `C:\Python314\python.exe`（3.14.7，六项依赖齐全）**；PATH 里默认 `python` 是 3.13.13 且**缺 PySide6**，直接 `python tests\...` 必报 ModuleNotFoundError。
- **PowerShell 5.1 会把 UTF-8 无 BOM 的中文注释按 GBK 读**，全角括号的字节可破坏字符串解析 → `Unexpected token ')'`。写 .ps1 一律**用英文注释**（.py 不受影响）。
- **单元测试里不能构造 QWidget**（`MainWindow()`）→ 无 QApplication，进程直接 `0xC0000409`（STATUS_STACK_BUFFER_OVERRUN）且 stdout 缓冲全丢，看似"无输出"。要测 MainWindow 的方法逻辑就用**轻量替身类借用未绑定函数**（`class W: _spec_enabled = MainWindow._spec_enabled`，见 `test_spec_translate_offline_only_gate`）；要真构造就去集成测试（有 QApplication）。排查此类崩溃加 `PYTHONUNBUFFERED=1` 才能看到崩在哪个测试。
- `gh --jq` 表达式含空格/管道时 PS5.1 会拆参数（`accepts 1 arg(s), received 5`）→ 用**无空格表达式**（`--jq .sha`）或改走 `--json` + PowerShell 处理。

### 13.6 真机 A/B 方法论（可照抄，本轮跑通三次）

- **隔离实例**：`LIVETRANSLATE_HOME=<临时目录>`（配置+日志隔离）+ `HF_HOME=C:\Users\Administrator\.live_subtitle\hf`（复用 1.6GB 模型缓存，app 用 setdefault 不覆盖注入值）+ **拷贝 `<真实根>\argos` 到隔离 home**（`offline_pack.PACKS_DIR` 是从 `ARGOS_DATA=CONFIG_DIR/"argos"` 派生的模块常量，光注入环境变量没用，不拷则离线包为空、argos 必失败）。
- **启动**：`subprocess.Popen([PY, "main.py"], env=env)`，env 里 **pop 掉 `QT_QPA_PLATFORM`**（offscreen 会失真）；config 预写 `wizard_done=true / auto_start=true / close_action=exit / proxy_mode=none`。
- **就绪判定**：轮询隔离 home 的 `logs/app.log` 出现 `asr.model_loaded`（或 `asr.model_reused`）——本机 turbo+CUDA 缓存加载约 4s，比 sleep 死等可靠。
- **素材**：SAPI（Zira en-US）经 **SSML** 生成 wav——`PromptBuilder.AppendSilence` 在此运行时不存在，改用 `SpeakSsml` + `<break time="950ms"/>`；`SetOutputToWaveFile(path, SpeechAudioFormatInfo(44100, Sixteen, Mono))` 出 PCM 才能被 `winsound.PlaySound` 播放（阻塞精确，可当计时器）。**素材必须匹配验证目标**：长句（>cap）验 A、短句（<cap）验 VAD。
- **收尾**：`EnumWindows` 按 pid 找可见无 owner 的顶层窗 → `PostMessageW(hwnd, 0x0010 /*WM_CLOSE*/)`；`close_action=exit` 保证不弹询问框。**严禁 Stop-Process**（会跳过 `pipeline.latency` 遥测汇总，拿不到数据）。
- **占屏必须预先征得同意**（第十二节红线；本轮两次弹窗均先问、用户明确授权后才跑）。

### 13.7 用户裁决与偏好（本轮新增）

- GitHub 令牌已接入 gh keyring（账号 `2465251326-netizen`，scope 含 `repo`+`workflow`）；**令牌在对话中明文出现过，已提示用户去 GitHub 撤销重发**。凭据未写入任何仓库文件/git config。
- 裁决记录：① 分段上限默认值"收回 4s"；② 授权 GUI 真机测试；③ 神经 VAD"回退"。
- 用户偏好复证：**要数据不要解释**、**文案必须与实际行为一致**（本轮设置页文案三易其稿，把"71% 腰斩""0.42% CPU"等实测数字直接写进说明）；被否掉的方案也要留档，不许悄悄删掉当没发生。

### 13.8 本轮产物清单

- 版本：`v2.8.0`（tag 触发 CI 出双资产）。套件：**单元 73 / 集成 92 全绿**。
- 新配置键：`spec_translate`（默认 True，仅离线引擎生效）、`segment_cap_s`（默认 4.0）。设置页新增两行（语音识别-语言与计算），已按 v2.3.0 规约三处登记（DEFAULTS + `_FIELD_SPECS` + `_STD_ROWS`），`bump_version --check` 与注册完整性双向断言均过。
- 新日志字段：`pipeline.latency` 增 `n_spec / spec_p50 / spec_p95`。
- 临时验证产物（均在 %TEMP%，不入库，可删）：`ls_gen_voice*.ps1`、`ls_ab_drive.py`、`ls_vad_ab.py`、`ls_vad_bgm.py`、`ls_vad_short.py`、`ls_diag_pause.py`、`ls_ab_voice*.wav`、隔离 home `ls_ab_base` / `ls_ab_new`（各含 81MB argos 拷贝）。

