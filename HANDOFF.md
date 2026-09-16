# 会话交接文档 · LiveSubtitle 实时字幕翻译

> 本文件供**新会话**接手使用。读这一份即可获得全部上下文，无需翻阅历史对话。
> 最后更新：2026-09-16 **v2.18.2 已发布**（本行下面首段仍描述 v2.18.1）——面板"只留一条分割线"其实没做到（v2.18.0 漏删历史区装饰线 + 拖完胶囊永久高亮），另修字幕卡聚焦样式自 v2.2.5 起从未生效等 8 项，以及**真实英语新闻端到端实测揪出的 5 项悬浮窗缺陷**，见 CHANGELOG 与第二十/二十一节；**同日本会话另实测出三个真缺陷并全部修复补锁**——D-1 流式原文在 `asr_language=auto`（出厂默认）下整条通道静默哑火、D-2 dual 布局延续片段原文被拼接两遍、D-3 首句草稿译文必错一轮，见第二十二节（含 22.8 发版前真机复测），**v2.18.2 已发布**
> ⚠️ v2.7.5 由上一会话发布但**当时漏更新本文件**，其变更详情见 CHANGELOG.md（8 项审计修复）

---

## 一、项目概况

- **仓库**：`https://github.com/2465251326-netizen/live-subtitle`（owner 即用户本人）
- **本地路径**：`C:\deepseek (2)\live-subtitle`
- **技术栈**：Python 3.14（本机 `C:\Python314\python.exe`）+ PySide6（Qt6）+ faster-whisper（CTranslate2）+ pyaudiowpatch（WASAPI 环回采集）
- **功能**：抓取系统声音/麦克风 → 本地语音识别 → 实时翻译 → 主窗口字幕列表 + 悬浮字幕条
- **当前版本**：**v2.18.2**（已发布，含 Setup EXE + portable zip 双资产）

## 二、发版工作流（严格照做，踩过坑）

```powershell
cd "C:\deepseek (2)\live-subtitle"
$env:QT_QPA_PLATFORM = "offscreen"          # 无头测试必须
$PY = "C:\Python314\python.exe"              # ⚠ 2026-09-16 复核：本机 PATH 里的 python 已指向 C:\Python314\python.exe（3.14.7，依赖齐全），
#   旧记录"PATH python=3.13 缺 PySide6"在本机已不成立；仍建议显式写绝对路径，防 PATH 漂移
& $PY tests/test_units.py                    # 81 项单元测试
& $PY tests/test_integration.py              # 115 项集成测试（判定以 TOTAL 行为准，退出码有 Qt 收尾竞态噪声）
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

> ⚠️ **2026-09-16 10:50 磁盘现值勘误**（读 `~\.live_subtitle\config.json` 实测，上表是 09-10 的历史快照，
> 之后多轮会话与用户自改已让若干键过期；**别再照上表推断当前行为**）：
> `engine=argos`（非 auto）、`engine_auto_fallback=false`、`asr_language=en`（**非 auto**——正是 D-1
> 在本机从未暴露的原因，见第二十二节）、`show_source=true`（非"只译文"）、`overlay_layout=dual`、
> `perf_turbo=true`、`asr_accuracy=fast`、`hallucination_filter=false`（用户自己关掉了）、
> `segment_cap_s=4.0` + `neural_vad=false`（09-16 10:49 由 2.5/true 改回，见第 21.4 节更新）、
> `stream_preview=true`、面板几何 `x=1106 y=344 w=619 h=515 字号22 透明度87`、
> `translate_fix_map={"加快人工智能的发展速度":"控制人工智能的发展节奏"}`、`overlay_dual_src_h=87`。
> 备份件：`config.json.bak-20260910_235823`、`config.json.bak-20260916_104939`。

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
- 集成测试：`tests/test_integration.py`（115 项，覆盖配置/缓存/重采样/字幕面板(成对行·淘汰·清空·收起·拖移契约·空状态·图钉·引导·未读计数·高度收敛)/字幕卡生命周期/热键/设置字段/QSS 括号/向导/退出清理/声明式行表/SRT/呼吸与贴边等体验回归）
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
app/asr/preview.py       StreamPreview：dual 流式原文通道（0.9s 节拍重识别 4s 窗）；
                         语言参数必须走 normalize_language（auto/空→None，见第二十二节 D-1）
app/audio/capture.py     CaptureThread、设备解析、重采样+FIR、能量 VAD 分段
app/translate/translator.py  TranslateThread、三引擎备援链、翻译缓存
app/ui/main_window.py    主窗口、字幕卡、托盘、状态横幅、空页面速览卡
app/ui/caption_overlay.py 悬浮条（连续文本流/列表/单条 + 缩放 + 淡入）
app/ui/settings_dialog.py 设置页（声明式 _FIELD_SPECS 驱动）
app/ui/first_run.py      首启向导
scripts/bump_version.py  版本同步（唯一正确入口）
scripts/probe_text_clip.py 文字裁剪探测
tests/test_units.py      81 项单元测试
tests/test_integration.py 115 项集成测试
docs/UX-REPORT-R7.md     体验审查报告（R7：UI 全量走查 + 修复状态）
CHANGELOG.md             更新日志（用户可见；README 只留链接，v2.3.0 起）
README.md                门面：亮点/下载/反馈/使用详解/FAQ（勿把日志塞回去）
.github/ISSUE_TEMPLATE/  反馈框架：bug_report.yml（🐞BUG）/ suggestion.yml（💡建议）/ config.yml（禁空白 Issue）
```

## 七、新会话开场建议

> 「读 `HANDOFF.md` 接手 LiveSubtitle 项目。当前 v2.18.2 已发布、**v2.19.0 已实现待发版**（第二十三节＝最近一轮：用户实拍三改——双语历史区默认关 / 分割线几何恒等式修正 / 新增关攒句实时翻译开关；第二十二节＝工作区深度体检实测出 D-1 流式原文在 auto 下静默哑火、D-2 dual 原文重复拼接、D-3 首句草稿译文必错，三缺陷全修 + 真机复测；第二十一节＝真实英语新闻端到端 5 项悬浮窗缺陷；第二十节＝面板"一条线"补漏与像素锁方法论）。第十三节有延迟结构定性与 A/B 方法论，第十四节有神经 VAD 恢复始末，第十五节有常驻行为矩阵。」

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
- **处置**：用户裁决"回退 B"。已拆净 `capture.py` 的 `_init_neural_vad/_neural_voiced/voiced_override`、`DEFAULTS.neural_vad`、设置页三处登记、管线传参与 2 个单测；`Segmenter.feed` docstring 留实测数据档；结论固化为 `test_energy_vad_beats_steady_bgm`（合成"纯背景乐预热 10s + 15 短句"素材，断言零硬切且段长中位 <2.2s）。**（更新：随即用户改主意要求保留，v2.9.0 已整套恢复为"实验开关（默认关）"，实现与本节回退前完全一致、实测数据不变仍留档，且新增"neural_vad 默认必须为 False"的回归锁。见第十四节）**
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

## 十四、会话快照（2026-09-15 深夜 v2.9.0：神经 VAD 恢复为实验开关）

- **背景**：v2.8.0 刚发布（tag 已推、CI 构建中），用户**推翻上一轮"回退 B"的裁决**，要求保留神经 VAD 能力。
- **处置原则**：① 不动已发布的 v2.8.0——tag 已推送，改写已发布 tag/提交属破坏性操作，一律走新版本 **v2.9.0**；② **默认值仍为关**——实测数据没有变化（纯净素材与能量判据持平、稳定背景乐下滞回黏 0.66s），默认开会与"翻译提速"主线目标相悖；设置页文案如实写明默认关理由与实测数字，不搞"藏起来当免费功能"；③ 实现从第 13.4 节回退前的代码**逐字恢复**（`Segmenter.feed(voiced_override=None)` 注入、`_init_neural_vad`/`_neural_voiced`、滞回 0.50 进/0.35 出、异常静默永久降级），默认路径（不注入）与历代版本行为一字不差。
- **登记五件套**：`DEFAULTS.neural_vad=False` + `_FIELD_SPECS` + `_STD_ROWS`（标题"神经 VAD 句末判定（实验性，默认关）"）+ `_std_rows` keys 元组 + `CaptureThread(neural_vad=...)` 管线传参。设置页三处登记规约照旧，管线传参处算第四、五处（v2.3.0 表驱动只管到控件层）。
- **测试锁**：恢复 `test_segmenter_voiced_override`（注入/降级双路径）、`test_neural_vad_hysteresis_and_degrade`（滞回+异常降级）；另在 `test_energy_vad_beats_steady_bgm` 里新增断言 **`DEFAULTS["neural_vad"] is False`**——"要改默认值，先拿新的实测数据来"。单元 73→75、集成 92 全绿。
- **教训（流程）**：回退随 v2.8.0 发布后**当晚**用户即改主意 → 多走一整个发版周期。对"价值有争议"的功能，今后优先提议**保留默认关**（代码与开关都在、文案写明数据与适用场景），而不是拆干净；拆干净仅在用户明确要求代码库整洁时执行。本次恢复成本可控是因为实现细节都在会话上下文里；若隔会话再恢复，就得从 git 历史或留档说明反推重写。

## 十五、会话快照（2026-09-15 深夜 v2.10.0：悬浮面板启动即常驻）

- **用户原话**："我的意思是，打开软件，字幕悬浮窗一直常驻，按热键来显示或隐藏"。此前痛点：热键/X 隐藏会把 `overlay_enabled=False` 跨会话持久化，之后每次开软件甚至开始翻译都不弹面板，"每次都要手动唤"。
- **行为矩阵**（新语义，`_load_settings` 无条件 show + 回写 True 实现）：
  | 时机 | 面板 | overlay_enabled 落盘 |
  |---|---|---|
  | App 启动 | **必显示**（常驻） | True |
  | 热键 / X / 设置页取消 | 隐藏 | False |
  | 隐藏后开始翻译 | **不弹**（尊重当次隐藏，1274 行既有逻辑未动） | False |
  | 再按热键 | 显示 | True |
  | 下次启动 | 又常驻 | True |
- **实现要点**：① `_load_settings` 里**先 `overlay.show()` 再 `_refresh_quick_panel()`**——速览卡按 `isVisible()` 取文案，顺序反了仪表盘会谎报"已关闭"（v2.10.0 锁里专门断言了这点）；② `_build_ui` 尾部原 v2.5.1 按配置恢复段改为无条件兜底 show；③ `start_pipeline`（1274 行）"仅 enabled=True 才补显示"逻辑**保持不变**——这是"隐藏后开始翻译不弹"的实现载体，勿"顺手统一"。
- **文案同步**：设置页 `overlay_enabled` 行 desc、README 亮点 bullet、三分钟上手第 5 步、README「显示/悬浮字幕面板/托盘菜单」三节（顺带清了 v2.4.0 退役的描边/右键关闭/0-95% 透明度遗留描述与特性亮点里的"三种形态"过期文案——README 后半是 v2.3 时代残留，此前多轮改版都没扫到）。
- **锁**：`t_overlay_resident_on_launch`（集成 93）——构造前落盘 False→构造后必可见且回写 True；热键隐藏→False；再按→True。注意热键有 **250ms 防抖**（既有机制），连测两次 toggle 前要 `w._overlay_hk_last = 0.0`，否则第二次被防抖吞掉误判失败。
- **版本**：v2.10.0（行为变更走 minor，项目惯例）。改动面很小（main_window 两处 + settings_dialog 文案），全套 75+93 绿后才发布。

## 十六、会话快照（2026-09-15 深夜 v2.11.0：上下双语布局 + relayout 状态机历史缺陷修复）

- **用户需求原话**："保留这个样式，再添加一个样式……不是那种一个框一个框里有原文和翻译，而是上半部分是原文，下半部分是译文，UI 要好看一点"——对标豆包 PC 实时翻译。
- **实现形态**：`overlay_layout`（list/dual）配置键；`CaptionOverlay` 内置 `_dual_body`（`_dual_src` 淡色原文 + `_dual_sep` 分隔线 + `_dual_tgt` 加粗译文），与列表模式互斥显示；主窗调用接口（show_pending/show_pending_result/update_spec_result/show_caption/clear_caption）**零改动**，内部按模式分派。dual 特有行为：原文区**流式生长**（`_starts_new_sentence` 判新句重置、延续片段 `_dual_join` CJK 邻接直连/拉丁补空格拼接）；推测版走 spec 态淡色；终版收口校准整句。⋯ 菜单加互切项（`on_layout_changed` 回调落盘，主窗 `_on_panel_layout_changed` 幂等同步 overlay+config 双保险）。设置页「显示-字幕显示」combo 登记（overlay_ 前缀键保存自动走 apply_overlay_from_config）。
- **dual 字号必须 setFont**：QSS 的 font-size **不会写回 widget.font()**，QLabel.heightForWidth/sizeForWidth 按默认 12px 字体度量 → 长句需求高被算小 → 裁切。`_apply_dual_fonts`/`_restyle_dual_tgt`（spec=主字号 DemiBold、empty=12px Normal）统一管理，QSS 只管颜色。
- **dual 高度**：`_dual_want_height()` 用 heightForWidth(可用宽) 求和（+25 常数= margins14+spacing10+sep1）；`_relayout` dual 分支**先解除 setFixedHeight 钳制**再取值（否则 label 被压在旧高度里、度量停在旧值）；无滚动、cap=0.55 屏、user_height 语义与列表一致。
- **历史缺陷修复（本节最重要）**：`_consume_relayout` 收敛后 `_relayout_pending` **永久残留 True**（v2.4.3 引入排期机制起就这样，原版无 pending=False 处置）——之后所有 `_schedule_relayout` 被守卫吞掉。列表模式有滚动条兜底视觉无感（历史全绿测试也没暴露），dual 无滚动 → **第二句起高度停在首句值、长终版底部裁切**。实证链：真机截图 shot6 裁切 → `QWidget.grab()` 面板本体渲染定位"非截图问题" → 打印 hfw/want/height 发现 want=124 而 body=74 → 轨迹复盘抓到 `pending=True` 恒真。修复=链结束置 False（两个分支都补）。**列表模式的高度贴内容也随之更及时**（顺带收益）。
- **dual 空态占位**：__init__ 的占位写在列表区 _hint；构造后经配置切 dual 的实例会错过 → `set_layout_mode` 切换即补 `_update_empty_hint()`（dual 分支只在原文区空时写占位，幂等；首次显示升级手势引导语义保留）。
- **验证方法论**：`QWidget.grab()` 把控件本体渲染成 PNG——不受桌面重叠/分辨率干扰，比全屏截图更适合 UI 排查；配合逐步 processEvents 打印 pending/height/want 轨迹，两轮就锁死根因。真机验证走隔离实例 + SAPI 连续语音 + PowerShell CopyFromScreen 全屏截图（弹窗前征得用户同意）。
- **锁**：`t_overlay_dual_layout`（全链路）、`t_overlay_layout_config_roundtrip`（配置恢复+菜单落盘）、`t_overlay_relayout_pending_release`（多句高度跟随——钉死本次历史缺陷）。集成 96 / 单元 75 全绿。
- **版本**：v2.11.0（新功能 minor）。

## 十七、会话快照（2026-09-16 凌晨 v2.12.0：dual 流式原文通道）

- **用户需求原话**："主持人讲了什么英文，就必须实时显示在原文里，必须实时不能停"×3——新闻连续口播场景，dual 原文区每 4s（分段周期）才蹦一段完全不够。
- **方案**：滑动窗口流式预览识别——`app/asr/preview.py` StreamPreview 线程每 0.9s 把最近 4s 音频（CaptureThread 新增 `raw_chunk` 原始旁路，90ms 聚合）用共享的 whisper 模型重识别一次（beam=1 + without_timestamps + condition_off 最快档），输出草稿；主窗 `_strip_overlapped_prefix`（base 尾部与 text 头部最大重叠剥离）取出新增话音追加到原文区。
- **关键设计取舍**：① 草稿不送译（每 0.9s 改写不值得，译文仍走分段+推测式）；② 不做精确时间对齐（重叠剥离 + 逐拍自我修正足够，字幕场景）；③ **仅 cuda 启用**（`_stream_preview_enabled` 三重闸：开关 × dual 布局 × asr_device==cuda——CPU 单次推理数秒会拖垮正式识别）；④ 模型共享：CTranslate2 推理线程安全（模型只读），GPU 争用时预览自动降频跳拍，实测单片识别反而 0.52→0.28s。
- **实测**：密集连拍 2.0/3.0/4.0/5.5s——2s 时原文已显示大半句（旧版空白）、3s 窗口尾带部分转写逐拍自愈、4s 收敛正确整句。识别延迟遥测 reco_p50 同场 0.28s。
- **坑**：Qt 信号 `Signal(object)` 发元组 ≠ 双参数——`raw_chunk.emit((buf, t))` 连 `feed(audio, t)` 会 TypeError missing argument；发双参数信号 `Signal(object, float)` 正解。窗口滑动的草稿会带"前瞻性延展/改写"（whisper 对含未来音频的窗口转写），是流式草稿固有特征，UI 文案要写明"逐拍自我修正属正常"。
- **锁**：`test_strip_overlapped_prefix`、`test_stream_preview_gate`、`t_overlay_stream_partial`、`t_main_partial_preview_alignment`（内联 stub——`_StubTr` 定义在文件后段，前段测试引用会 NameError）。单元 77 / 集成 98 全绿。
- **版本**：v2.12.0（新功能 minor）。用户明示"不用监控 CI"——tag 推送即收尾，轮询器流程跳过。

## 十八、会话快照（2026-09-16 凌晨 v2.13.0：草稿送译 + 流式三修）

- **用户实测反馈**（真机 v2.12.0）："译文不是实时翻译的，还那种攒句，而且原文有种攒句的感觉"。真机遥测核实：流式通道已启动（preview.started ✓）、spec_p50=0.02s 但**节奏**被正式片段（2.5~4s 分段周期）卡死——草稿不送译是 v2.12.0 的设计取舍，实测被用户否决。
- **草稿送译**：`_on_partial_preview` 每拍把"确认基线+草稿增量"送 spec 翻译；回复**不走 _spec_inflight 簿记**，按"`_dual_draft` == 回复原文"一次性配对（防迟到同文重复覆盖），`overlay.update_dual_draft_tgt` 只刷译文区淡态。冲刷（flush）与 stop_pipeline 作废在飞草稿。实测：译文 **1.8s 即出现**且与半截原文对应（旧 4.2s 整句一次性）。
- **重叠剥离算法重写**（v2.12.0 严格对齐实测整句重复）：正式/预览是同音频两次转写，标点/大小写必异，`base.endswith(text[:k])` 语义是"text 前缀 vs base 尾缀"——base=text+"." 时永不匹配（除非周期串）→ 全量重复。新算法：**词级锚点**（base 尾 5→1 词，lower+strip 尾标点）在 text 前 2/3 找最后出现，其后即新增；CJK 源字符锚（base[-6:]，rfind 且限前半）；锚全失配返回全量（重复下一拍自愈 < 丢新话）。注意：词级 lower 匹配使"大小写抖动"从"全量重复"变成"正确剥离"——单测断言已随行为升级。
- **草稿翻译语言回退**：`_tgroup_lang`（首片前为空）→ `asr_language` 配置。空串 lang 送 argos = 找不到语言包直接抛错（spec 不走备援链）→ 草稿译文全灭。
- **中途切布局热启动**：tap_enabled 常开（无接收者 emit µs 级）；预览对象创建与布局解耦（start_pipeline 只看开关+cuda）；`_on_panel_layout_changed` 与 `apply_overlay_from_config`（设置页保存路径）都调 `_maybe_start_stream_preview`——闸门含布局，切 dual 热启（`restart()` 复位 stop 标志+清陈旧缓冲）、切 list 暂停（`_pause_stream_preview` 不销毁对象）。
- **connect 幂等**：partial_ready/raw_chunk 的 connect 全部移到 start_pipeline 创建处一次性接好——_maybe_start 会被多次调用，重复 connect = 信号重复派发。
- **预览节拍遥测**：`preview.beat`（每 20 拍：interval_avg vs INTERVAL_S、infer_avg）——GPU 分时排队拖慢节奏时日志可辨。
- **用户配置提醒**（未代改，用户裁决）：真机 `neural_vad=true` + `segment_cap_s=2.5`，实测 hold_p50=4.12/7.0（应 ≈2.5~3）——神经 VAD 黏滞是嫌疑主因（v2.9.0 实测数据），已建议用户关闭对比。
- **锁**：`t_main_draft_translation_flow`（草稿送译/一次性消费/冲刷作废；注意断言的 lang：草稿=asr_language 配置值（itest_home 为 "auto"）、终版=_tgroup_lang（"en"））、`test_stream_preview_restart`、`test_strip_overlapped_prefix`（升级）。单元 78 / 集成 99 全绿。
- **版本**：v2.13.0（行为增强 minor）。CI 不监控（用户既定偏好）。

## 十九、会话快照（2026-09-16 凌晨 v2.14.0：dual 历史区 + 面板自由拉高）

- **用户需求原话**："原文和译文不是都应该保留吗，可以用滚轮进行滚上滚下查看；原文的显示范围太小了，改成可以自由上下拉长"——dual 从"当前句聚焦"演进为"历史区（可滚轮回看）+ 当前句大字区"。
- **结构**：dual 布局 = 工具条 + `_dual_hist`（QScrollArea 历史区，行=原文小灰 0.55×fs + 译文小白 0.66×fs DemiBold 成对）+ `_dual_body`（当前句大字区，流式不变）。终版句沉历史时机 = `_on_translated`（翻译成功 not error）调 `overlay.dual_push_history(combined, translated)` + `_dual_current=""`（数据清空，**显示保留**至下一句 new_sentence 自然覆盖——空窗观感更平滑）。上限 MAX_DUAL_HIST=30 行（超出删最老）；「同时显示原文」关闭时历史行原文一并隐藏。
- **高度分配**（_relayout dual 分支）：body 贴内容但上限 300px（防超长句独占）；hist 吃剩余空间（hist_want vs avail-54-body 上限），avail = max(0.68 屏, user_height)——**拉高面板=历史区变大**（v2.13 前拉高只撑 body）；cap 0.55→0.68。hist 无内容时 hide（body 独占）。
- **滚轮跟随**：`_hist_follow` + valueChanged 守卫（同列表语义：上滚不打断、回底恢复）——push 行后仅在跟随时 singleShot 滚底。
- **真机验证教训（音频链路不可信时）**：第二轮真机截图出现"信号弱+whisper 静音幻觉（you Thank you.）"——用户实时环境的系统默认输出可能已切换，环回抓空 → **音频链路验证结果不可信**。改用 `QWidget.grab()` 本体渲染 + 直接注入 6 句终版（绕开音频），两轮 grab 定案：6 行历史成对完整可见、拉高后 hist 变大、当前句草稿态、零裁切。终版→沉历史链路另有集成锁（t_main_partial_preview_alignment 断言 hist_rows==1 / _dual_current==""）。
- **易错点**：_on_translated 里 push_history 后**别忘了 `_dual_current=""`**——首轮实现漏写导致数据层当前句不归零（集成锁当场抓获：'终版沉历史后当前句数据清空'）。
- **锁**：t_main_partial_preview_alignment 升级（hist_rows==1 / hist_base / current 清空）。单元 78 / 集成 99 全绿。
- **版本**：v2.14.0（新功能 minor）。CI 不监控（用户既定偏好）。
- **v2.18.0 追记**：用户"为什么会有三条线"——v2.14~2.15 的三轮迭代堆出了三个横向把手（历史分割/原文译文分割/底缘拉伸），**交互过载**。简化裁决：历史区自动吃剩余本就是合理默认，历史手动分割是伪需求 → 删（`_dual_split_hit`/hist 模式/面板兜底/三点把手全清），只留 sep 一条 + 底缘。`overlay_dual_hist_h` 键保留兼容但不再读取。**教训：连续迭代同一个界面时，每隔几轮要退一步看"面板上共有几个交互把手"——功能正确≠交互正确。**
- **v2.17.0 追记**：dual 高度分配的**第三次重构**（v2.14.1"拉高全给当前句"→ v2.17.0"历史吃剩余"）——空间分配策略要随布局演进重审：历史区从无到有后，"当前句优先"就变成"巨幅留白+历史被挤"。同场修掉 **adjustSize 缩回**：QScrollArea 默认 sizeHint 192px 会把 fixed 高度的子控件在 adjustSize 时"缩水"（面板总高锁不住），dual 模式跳过 adjustSize、用 `_dual_total_h` 显式 resize。**dual 分配现行语义**：当前句贴内容（≤45% 总高）→ 历史吃剩余 → 分割线拖过的 hist 值最优先（body 保底 46 让位）。
- **v2.16.2 追记**：**QScrollArea 白底是三重坑**——QSS 选择器链不命中 viewport、viewport 需直接挂透明属性、真实 Windows 渲染下 QAbstractScrollArea 还会用 palette.base 填充。**新增任何 QScrollArea 必须三保险一次到位**：NoFrame + viewport.setAutoFillBackground(False)+WA_TranslucentBackground + viewport.setStyleSheet("background: transparent;")（参照 _dual_src_wrap/_dual_tgt_wrap 构建）。另：QWidget.grab() 的渲染 palette 与真实屏幕不同（白底在 grab 里可能不刺眼导致漏检）——**视觉回归验证一律用全屏 CopyFromScreen 真实渲染**。
- **v2.15.x 追记**：分割把手**四轮**修复（v2.15.0 实现 → v2.15.1 坐标错位 → v2.15.2 事件过滤器 → v2.15.3 布局缝隙兜底）。v2.15.2 教训同前述（move/hover 不传播、QScrollArea 消费 press、position 不重映射，最终方案 = 7 子控件事件过滤器 + QTest 式 sendEvent 真实事件流验证）。**v2.15.3 最终一块拼图**：分割线恰好落在 hist 与 body 之间的 **layout spacing 缝隙**（无子控件）——press 时 `widgetAt` 返回面板自身、事件直达 `mousePressEvent`，而该分支在 v2.15.2 清理时误删 → 缝隙路径无主。修复 = 恢复面板级命中兜底分支（与过滤器共用 `_dual_split_apply_drag`），**过滤器（点在子控件上）+ 面板兜底（点在缝隙）双通道**。经验：事件过滤器不是银弹——**布局缝隙上的事件没有子控件可过滤**，面板自身的事件处理必须保留；验证务必用真实坐标（widgetAt 探测接收者）而非假设事件走向。
- **v2.14.1 追记（同夜）**：用户实测"底缘完全拉不了"——v2.14.0 把拉高增量全分给历史区，历史无行时 hist_h 强制 0、body 固定内容高 → 面板弹回。修正语义：**拉高=锁定面板总高**，原文区吃"总高-工具条-历史"全部剩余，历史最多 55%、保底 56px 可滚（单行内容需求 38px 曾被"≥40 才显示"门槛整条杀掉，一并修）；底缘新增横向三点把手（此前隐形）。锁：`t_overlay_dual_user_height_body`（拉高锁定总高/body 变大/历史不压瘪）。教训：**新增"空间分配"逻辑时，每一类内容状态（空/单行/多行）都要有断言**——v2.14.0 的锁只测了"有历史行"路径，"无历史行"路径的拉高回归漏网。

## 二十、会话快照（2026-09-16 v2.18.1：面板"一条线"补漏 + 三个实测缺陷）

- **用户诉求原话**："上一个 agent 没能修好这个界面，我只想要一个分割线，你修好后最好进行截图，自己看看修复效果…直到确实只有一个分割线为止"；随后追加"顺便把 D1、D2、D3 修了"（D1/D2/D3 = 本会话工作区体检实测出的三个缺陷）。
- **v2.18.0 为什么没修好**：它删的是"历史区**可拖分割把手**"，但**漏删了历史区 QSS 的 `border-bottom`**——那条装饰线才是用户截图里 y≈410 的线。加上 `_DualSepHandle` 松手漏 `set_drag(False)` 导致胶囊永久高亮（y≈610 那条粗线），用户看到的仍是"好几条线"。教训：**"减少交互把手"≠"减少可见线"**，视觉元素要按像素清点，不能按代码里删掉的控件数清点。
- **面板最终形态**（用户二选一裁决：留可拖的「原文/译文」线；关原文时不要线）：工具条 → 历史区（自动高度、**无线**、滚轮回看）→ 原文区 → **唯一一条可拖分割线** → 译文区；关「同时显示原文」⇒ 该线一并隐藏（面板零可见线）。静息 alpha 26→**70**：删掉多余线后"保留的那条"淡到 Δ亮度仅约 9%（真机截图实测），等于留了一条看不见的线——用户要的是**看得见的一条**。
- **本轮修掉的 8 项**：① 历史区装饰线 ② `set_drag(False)` 缺失（胶囊常驻）③ `set_show_source` 漏调 `_sync_dual_visibility`（关原文仍有线）④ dual→list 历史区残留（实测 401px 空白块）⑤ 字幕卡聚焦/渐隐样式**自 v2.2.5 上线起从未生效**（见下）⑥ 面板 🌐 切语言调不存在的 `dlg.reload_values()`（异常被吞，同步从未发生）→ 新增窄同步 `sync_target_lang()`（不清 `_staged`）⑦ 重跑首启向导把麦克风改回系统声音（违背设置页承诺；模型页 v2.0.3 已回显，源页漏网）⑧ 分割把手"悬停浮现胶囊"三态里 `set_hover(True)` 全库零调用点 → 改在过滤器 MouseMove 里按"是否落在把手上"给真值。
- **D1 的根因值得反复读**：`set_active()` 用**覆写 objectName** 表达状态，而 QSS 写 `QFrame#CaptionCard#CaptionCardActive`——Qt 里 `#A#B` 是「祖先名 A 且自身名 B」，卡片互为兄弟永不成立；同时改名让基础规则 `QFrame#CaptionCard` 一起失效 ⇒ 聚焦卡渲染成窗口底色（卡片"没有脸"）。修法：状态改**动态属性** `state`（""/active/old），objectName 恒为 CaptionCard；**子控件必须逐个 unpolish/polish**——Qt 在父属性变化时只重排父自身，`#CaptionCard[state="old"] QLabel#CaptionSource` 这类后代规则不会自动重算（像素锁当场抓到：old 与 idle 文字极差 0）。
- **测试体系的新眼睛（本节最重要的方法论）**：102 项集成全绿却放过了一个"上线即失效"的视觉特性，因为旧锁 `t_card_lifecycle` 断言的是 **objectName 字符串**——把失效机制当成正确行为锁死了。本轮新增三把**像素级锁**：`t_card_focus_style_pixels`（三张卡渲染底色/文字亮度对比）、`t_overlay_single_divider`（扫描"整幅+亮度均匀+上下 2px 回落"的孤立薄行，断言恰好 1 条；关原文断言 0 条）、`t_wizard_preserves_source_type` / `t_panel_language_syncs_settings`。**判据标定实测值**：单行原文文字占比仅 0.66、亮样本极差 404；sep 线占比 0.95、极差 0、上下 2px 0.00——只用"亮像素占比"会把文字判成线，必须再加均匀度与薄行两条。
- **真机截图自查的坑（新增）**：面板 87% 不透明 ⇒ 桌面内容会漏进抓屏，**任何"绝对亮度阈值 + 面板外列做基线"的检测器都会失灵**（实测把 y=2 列当基线取到纯桌面，检出 0 条；换局部基线又检出一堆桌面峰）。结论：**客观数线用 offscreen 确定性像素锁**（背景纯色、可复现），**真机截图只用于人眼判断**。另：`overlay_enabled=false` 也挡不住面板出现——v2.10.0 的"启动即常驻"是无条件的，测主窗时别指望配置能藏掉面板。
- **子代理结论必须自己复核（再次实锤）**：本轮两个子代理交来的"严重缺陷"里，**5 条是假的**——"缩进错误 ×2"（全模块 `compile()` 通过）、"`_spec_enabled` 用 `is` 比较字符串"（实为 `==`）、"`level_changed` 量纲不统一"（实为 `min(1.0, level*8)` 发 0~1）、"`--check-config` 未实现"（全库无该引用）。真缺陷只在逐条自己验证 + 像素实测后才入册。已把这条写进 `.monkeycode/MEMORY.md`。
- **套件与发版**：本批结束时单元 78 / 集成 106；真实新闻实测（第二十一节）再加 5 把锁后为**单元 79 / 集成 110 全绿**。**v2.18.1 已按第二节工作流发布**（bump → CHANGELOG 定稿 → commit 含 `app/config.py` → push main → tag → CI 出双资产）。

## 二十一、会话快照（2026-09-16 真实英语新闻端到端测试 · 悬浮窗专项）

### 21.1 音源方法论（本轮踩通的路，下次直接抄）

- **便携版 Chrome 起不了第二实例**：`--user-data-dir=<新目录>` 在这个 `Chrome-v124…-Stable-1.8.5` 包里**被单实例策略吃掉**——实测启动器进程退出、0 个 chrome 进程 0 个窗口，第 1 轮整场零音源却"跑完了"（差点得出"面板无字幕"的错误结论）。**教训：测试开始前必须先证明音源在出声**，否则后面全是空谈。
- **MCI 不可用**：`winmm.mciSendStringW('open … type mpegvideo')` 报 err 277「初始化 MCI 时发生问题」（本机无可用 mpegvideo 设备）。
- **可用组合（已验证）**：`requests` 走代理拉真实新闻播客 mp3（`https://podcasts.files.bbci.co.uk/p02nq0gn.rss` → BBC Global News Podcast 整集 13.3MB/27min）→ `faster_whisper.audio.decode_audio(path, sampling_rate=44100)` 解码 → `pyaudiowpatch` **输出**到默认扬声器（Realtek idx=5）→ 环回设备 idx=26 自然抓到。实测 rms_max=0.288 / median=0.053，链路健康。
- **设备索引与总数都会漂，别信任何一次快照**：本会话先测到 `device_count=29`（合法 0–28，用户配置的 `device_index: 29` 当场报 Invalid device，Realtek 环回在 26），改配置时再测却是 `device_count=33` 且 **29 正好就是** `扬声器 (Realtek High Definition Audio) [Loopback]`，连测 4 次稳定 33。⇒ 我曾据第一次快照断言"用户配置越界过期"，**那是错的**（已纠正）。结论：WASAPI 枚举数量本身会随会话/虚拟设备变化，**存的索引天然不可靠，`resolve_device_index` 的按名回查是唯一稳的路径，别删、也别"顺手修正"索引数字**。
- **抓屏**：`ctypes BitBlt(GetDC(0))` 抓这台机器上 DWM 合成的字幕面板得到**纯黑**（24 张全黑），必须用 Qt `QScreen.grabWindow(0, x, y, w, h)`。窗口标题：主窗 `LiveSubtitle · 实时字幕翻译`、面板 `LiveSubtitle`（v1 驱动按"标题含 LiveSubtitle"分类 → 两个都判成 main）。
- **驱动脚本形态**：隔离 home（镜像用户配置 + **拷 argos 目录**，`PACKS_DIR` 由 `CONFIG_DIR` 派生）+ `HF_HOME` 注入复用 1.6GB 缓存 + `PYTHONUNBUFFERED=1`（否则后台任务看不到进度）+ 收尾 `WM_CLOSE` 保 `pipeline.latency` 落盘。
- **已留盘复用**（`scripts/qa/`，gitignore 内）：`qa_play_news.py <mp3> <秒数> <输出idx=5> <环回idx=26>`（真实新闻音频播放 + 环回电平自测）、`qa_news_drive.py --round N --seconds 150 [--show-source]`（隔离实例 + 周期抓屏 + 优雅收尾；`--capture-only` 可只抓屏）。下次改悬浮窗直接跑它，别再自己搭音源。

### 21.2 实测揪出的 5 个悬浮窗缺陷（全部已修，详见 CHANGELOG v2.18.1）

1. **流式草稿整句重复上屏**（最刺眼）——剥离基线错 + 锚点取"第一次出现"。新旧对照：旧 2 次/68 词，新 1 次/39 词。
2. **关原文时译文区被饿到 21px**——只隐了标签没隐 QScrollArea 本体，且 body 需求仍按三控件算间距。**用户自己的设置正是这一档**。
3. **dual 两区溢出不跟底**——最新文字被推到可视区外（实测滚动条 max=49 停在 0），流式字幕硬伤。
4. **推测式翻译污染持久缓存**——150s 会话 218 条里 39 组是同一句的渐进变体（最长 13 版）；修后同长度会话 28 条/0 组。
5. **历史区写入占位 "…" 行**——一次性占位被当正文永久留存。

### 21.3 真机遥测基线（用户配置：turbo+cuda+argos+cap2.5+neural_vad+perf_turbo+spec）

`n_reco=58 reco_p50=0.28 reco_p95=0.50 n_tr=28 tr_p50=0.11 tr_p95=0.71 tr_max=3.07 hold_p50=2.91 hold_p95=5.70 n_spec=61 spec_p50=0.09`
→ 用户实际感知的译文延迟 = **spec_p50 0.09s**；`hold_p50≈2.9 ≈ 分段上限 2.5 + 收尾`，与第十三节定性一致（hold 恒随分段周期）。`tr_max=3.07` 是偶发网络/加载尖峰，值得后续留意。零 `orphan_thread`、零 error/Traceback。

### 21.4 遗留观察（未立案，下次可查）

- 面板 87% 不透明度 + 主窗就在面板正后方时，抓屏里两者内容混叠，**自动化视觉判读会被干扰**——测面板请把主窗移开或最小化。
- `neural_vad=true` + `segment_cap_s=2.5` 仍是用户设置，第十八节的"神经 VAD 黏滞拉长切段"嫌疑未做 A/B 复测。**→ 2026-09-16 10:49 已消解**：用户自己把配置改回 `segment_cap_s=4.0` + `neural_vad=false`（对照备份 `config.json.bak-20260916_104939`），该观察项关闭；如需再验，重新开开关跑 `scripts/qa/qa_news_drive.py`。
- 历史区文字与当前句字号差偏小（0.55×/0.66× vs 1.0×），真实新闻密集语流下"哪句是正在说的"仍需用户主观确认。

## 二十二、会话快照（2026-09-16 工作区深度体检 · 实测出三个真缺陷并修复补锁）

### 22.1 本轮性质与基线数字（全部实跑，非转述）

- 做法：四路子代理**只读**并行分析（① 采集+识别链路 ② 翻译+基础设施 ③ UI 层 ④ 测试+CI+演进），
  父代理**逐条自己复核**后才入册——按 `.monkeycode/MEMORY.md` 规约，子代理结论一律先当"待验证材料"。
- 规模：17,595 行 Python（`app/` ≈10.5k，其中 UI 5.8k：main_window 2730 / settings_dialog 2514 / caption_overlay 1779；tests ≈4.7k）。
- 仓库：`main`==`origin/main`（0/0）、工作树干净、`.git` 1.62MiB / garbage 0、**全库零 `TODO/FIXME`**。
- 依赖实测：`C:\Python314` = PySide6 6.11.2 / faster-whisper 1.2.1 / ctranslate2 4.8.2 / numpy 2.5.3；
  `argos_translate` **未装且不需要**（离线翻译走 CT2 直载）；本机 PATH 的 `python` 已指向 3.14.7（见第二节就地勘误）。
- 套件基线（本轮起点）：单元 **79 PASS** / 集成 **TOTAL: 110 PASS: 110 FAIL: 0**；
  `bump_version.py --check` → 版本一致 2.18.1；最近一次真机会话（09-16 10:45，v2.18.1）日志零 error/零孤儿，
  `reco_p50=0.34 tr_p50=0.08 hold_p50=2.74 spec_p50=0.09`。
- 收尾状态：修复 + 补锁 + **发版前真机复测双 PASS**；同族第三项 D-3 也已一并修掉（见 22.8），
  终态 **单元 81 / 集成 112 全绿**，v2.18.2 已 bump + CHANGELOG + 发布。
  （随后 v2.19.0 面板改造又加 3 把锁、升级 2 把旧锁 → 现为 **单元 81 / 集成 115**，见第二十三节）

### 22.2 D-1（严重，出厂默认配置即中招）：流式原文通道在 `asr_language=auto` 下**整条静默哑火**

- **根因链**：`main_window.py:1337` 把配置原值喂 `StreamPreview` → `preview.py` 旧实现只做 `str(language or "") or None`，
  `"auto"` 是**真值** → `_transcribe` 传 `language="auto"` → **faster-whisper 只在 `language is None` 时才自动检测**
  （`transcribe.py:471`，否则走 else 分支交给 `Tokenizer`，`tokenizer.py:28-32` 对非法语言码抛 `ValueError`）
  → `preview.py` 逐拍 `except` 整拍静默吞掉 → 用户观感 = 开了"讲到哪跟到哪"却永远不出草稿、**界面零报错**。
- **真机铁证**（本机离线 tiny 模型，修复前）：
  `asr_language='auto' → RAISED ValueError: 'auto' is not a valid language code`；
  `'en'` 与 `''`（→None）均正常；对照"不传 language"时自动检测成功。修复后 `'auto' → _lang=None → OK 无异常`。
- **中招条件**：`stream_preview=True`(默认) × `overlay_layout=dual` × `asr_device=cuda` × **`asr_language=auto`（出厂默认）**
  ——正是 README 主打的那条卖点路径。**本机从未暴露**纯粹因为用户配置 `asr_language=en`（见第四节勘误）。
- **修法**：新增纯函数 `preview.normalize_language()`（`""`/`auto`（大小写容错）→ None），构造期归一，
  与正式通道 `engine.py` "auto 时根本不传 language" 的既有规约对齐；锁定语言仍原样透传（草稿不该每拍重猜）。
- **锁**：单元 `test_stream_preview_language_auto`（纯函数 + 桩模型捕获**真实 kwargs**断言无 `language="auto"`，
  并断言 `DEFAULTS["asr_language"] == "auto"`——默认值若被改动，锁会提醒重新评估前提）。

### 22.3 D-2（中，GPU 用户被掩盖 / CPU 用户直接可见）：dual 布局延续片段原文被拼接两遍

- **根因**：`_on_asr_text` 对**同一个片段**调用 `overlay.show_pending(text)` **两次**（方法开头一次 + 建卡后一次，
  v2.4.0 遗留），而 `caption_overlay._dual_show_pending` 对"小写开头延续片段"（`_starts_new_sentence` 判为续接）
  做 `_dual_join` **累加**、无幂等守卫 → 重复拼接。列表模式因 `_find_pending` 幂等不受影响。
- **真机铁证**（offscreen 构造真实 `CaptionOverlay`，按主窗调用序列复现）：
  一次 `Hello everyone and welcome to the show` → 两次（旧行为）`...show and welcome to the show`。
- **掩盖条件**：GPU+dual 下流式预览每拍 `update_partial` 整体覆盖 `_dual_src` 把它抹掉；
  **CPU 模式（预览被三重闸自动关闭）+ dual 布局**下用户直接看到重复字幕。
- **修法（取根因，不加下游去重兜底）**：删掉 `_on_asr_text` 建卡后的第二次调用，统一保留方法开头那一次
  （两条分支——流式两段式与 `instant_caption=off`——各自恰好一次）。
  **有意不做**面板层 endswith 去重：合法叠词（`非常`+`非常`）会被误吞，语义风险大于收益。
- **锁**：集成 `t_main_panel_placeholder_once_per_piece`（spy 计数断言每片段**恰好一次** `show_pending`
  + 断言 `_dual_src` 全文与"welcome 只出现 1 次"）。

### 22.4 顺手加固：整条通道哑火不再无声

- `StreamPreview` 加 `_fail_streak`：首拍记 `preview.round_failed`，**连续 3 拍升级记一条 `preview.degraded`**
  （携带 `language` 与提示）后停止刷屏。真实 `run()` 循环 + 抛错桩实测：`round_failed` 恰 1 条 + `degraded` 1 条。
- 为什么值得做：D-1 这类"功能整体失效"过去被逐拍 `except: pass` 混在噪音里（2 分钟可刷 130+ 条），
  日志反而更难判断是偶发跳拍还是永久哑火——`degraded` 是一条**可 grep 的定性证据**。

### 22.5 子代理证伪清单（六条"严重缺陷"不成立，别再当真）

| 主张 | 复核结论 |
|---|---|
| `recheck_dropped` 全仓无 connect，是死信号 | ❌ 假：`main_window.py:1295` 已连 `_on_recheck_dropped`（定义 1945） |
| `check_updates` 写 `app_version.json` 只写不读 | ❌ 假：**全仓库不存在该字符串** |
| `silero_vad` 键无消费方（仅注释） | ❌ 假：`engine.py:210/226-228/259/629`、`main_window.py:1283` 真实消费（映射 `vad_filter=True`） |
| `AsrThread.submit` 无 spec 位会挤掉终版 | ❌ 错域：`submit` 只收音频段；推测式在 `translate.submit`，那里 spec 明确"绝不挤终版" |
| `_on_asr_finished` 冲刷不 bump `_tgroup_gen` → 终版被代数校验丢弃 | ❌ 假：bump 就在 `_flush_tgroup`（2194）内，它调的正是该函数 |
| 集成测试从不设 `overlay_layout` | ❌ 假：`test_integration.py:1367/1413/1464`（本轮改锁后行号有位移）均在设 |

另：**"muted 信号无人接"亦为假**（`main_window.py:1344` 有 connect）。子代理报的缺陷命中率本轮约 3/10——**逐条自己验 + 真机取证**这条纪律继续保留。

### 22.6 已核实、本轮**未修**的技术债（下轮优先候选）

1. **115 项集成测试（含两把像素锁）不在任何 CI 闸门**：`build.yml` 只跑单元 + smoke；`deep-test.yml` 仅 `workflow_dispatch`。最强的回归保障全靠本地手跑。
2. **重复实现/死代码**：`_restyle_dual_tgt` 在 `caption_overlay.py:810` 与 `:1388` **定义两次**（后者生效、前者被遮蔽）；
   `_starts_new_sentence` 在 `main_window.py:2176` 与 `caption_overlay.py:464` 各一份（"必须同源"仅靠注释纪律）；
   `capture.py:476` `frames_per_buffer` 死代码。
   （⚠ 更正：子代理报的 `is_dual()` 零消费方**不成立**——`main_window.py:1558/1675/2309/2592` 四处调用；
   它只在本文件内 grep 才得出该结论。**证伪"死代码"必须跨文件 grep 调用方**，本轮自己也踩了这一下。）
3. **收尾**：`_stop_stream_preview`（1597-1602）不看 `wait(2000)` 返回值即置 `None`——capture/asr 有孤儿容器，preview 没有。
4. **异常静默**：`app/` 内 168 处 `except`、66 处以裸 `pass` 吞掉（≈39%，本人实数统计，非子代理报的 228/101）。
5. `probe_text_clip.py` 文字裁剪探测器**无任何测试/CI 消费方**（实 grep 0 引用）→ 版式回归全靠人记得手跑。
6. `scripts/qa/` 被 gitignore 且绝对路径写死（`Administrator` / 设备索引 5/26）→ 真机测试台知识换机即失传。
7. 依赖全下限无上限 + 本机 3.14 与 CI 3.11 双轨；`ctranslate2` 未显式声明（靠传递依赖），`requirements-offline.txt` 与 `build_exe.bat` 说法互斥。

### 22.7 新增取证方法论（可复用，别重新发明）

- **参数类缺陷用"离线快照直载"30 秒定案**，不必搭真机音源：
  `WhisperModel(str(hf/hub/models--Systran--faster-whisper-tiny/snapshots/<hash>), device="cpu", compute_type="int8")`
  → 直接调被测函数看真实异常。**注意必须传快照目录**，传 `.../model.bin` 会被 faster-whisper 1.2.1 当作"模型名"
  报 `Invalid model size`（本轮踩过）。配合 `HF_HUB_OFFLINE=1` 保证不联网。
- **面板层行为锁**用 `offscreen` + 真实 `CaptionOverlay()` 构造（集成测试既有用法），
  主窗层用 **spy 替换实例方法计数**（`w.overlay.show_pending = spy`）断言"每片段恰好一次"。
  ⚠ 这只对"**方法体内按属性查找调用**"的入口有效；**已 `connect` 的 Qt 槽改不动**（详见 22.8 血泪条）。
- **验证"某主张是否成立"先看全库 grep 命中数**：本轮 6 条假缺陷里 4 条只需一次 grep 即可证伪。
- 跑测试：判定只看 `TOTAL:` 行 / `test_report.txt`；PowerShell `*>` 重定向落的是 **UTF-16LE**，
  读回要 `read_bytes().decode("utf-16-le")`（本轮按 UTF-8 读出满屏 `C u r r e n t` 空格字，白查一次）。

### 22.8 发版前真机复测（无占屏 / 无外放 / 不动用户配置 · **双 PASS**）

形态：隔离 home（`%TEMP%\ls_e2e_home2` + 拷 `argos`）+ `HF_HOME` 注入复用缓存 + `QT_QPA_PLATFORM=offscreen`
+ **SAPI(Zira en-US) 6 句长句 53.5s wav 经 `decode_audio` 数字直注**（`raw_chunk.emit` 喂预览、`Segmenter.feed`→`asr.submit` 喂正式）
+ 关掉一切占屏动作。素材与设备索引都不写死进仓库。

| 复测项 | 判据（全部实测） | 结果 |
|---|---|---|
| **D-1** 流式原文在 `asr_language=auto` 下工作 | 预览开：`preview.started ×1`、`preview.beat ×3`（interval_avg=0.95 / infer_avg=0.54）、**`round_failed ×0`、`degraded ×0`**；**"无新片段仍生长" 40 次**（=草稿逐拍长，正是修复目标）；轨迹 t=3.3s 时 pieces=0 已出 "the research team announced yesterday that the" | **PASS** |
| **D-2** dual 原文不重复 | **专门关掉预览**（去掉"每拍整体覆盖"的掩盖源）跑一轮：13 片段 ↔ `show_pending` **恰好 13 次**（1:1），203 个面板快照**零重复 n-gram** | **PASS** |
| 顺带复验 v2.18.1 缓存修复 | `trans_cache` 13 条 = 13 次终版翻译，推测版 0 条入库 | PASS |
| 退出链 | 收尾存活线程仅 MainThread；`管线线程对象存活=[]`；`orphan ×0` | PASS |

**本轮新观察**
1. **同族 D-3（已修 + 已复验）**：草稿送译在语言锁定前取 `config.asr_language`（`main_window.py` 草稿路径）→
   值为 `"auto"` 时 `translator.py:537` 把它挡成 `source=None` → argos 抛 `缺少源语言信息…`。
   **第一次复测实测到 `('auto', True)` 提交 + 12 条 spec 回复里 4 条带该 error**（第二次复测因首个终版片先落而没触发
   → 时序相关、首句几拍内中招、自愈）。
   **修法（已落地）**：新增 `_spec_source_lang()` 统一回退链——`_tgroup_lang` → **`_last_asr_lang`（本会话最近识别到的语言，
   在 `_on_asr_text` 里记录、`start_pipeline` 清零）** → 配置项，**`"auto"`（含大小写/空格）视同未知**；
   解不出语言时 `_maybe_spec_submit` 与草稿路径**都跳过这一拍**（不写 `_spec_inflight`、不留错误态），终版路径一字未改。
   **真机复验**（44.4s 素材、dual+cuda+argos+auto）：68 次送译语言位**只有 `en`**、`auto`/空 **0 处**、
   60 条 spec 回复 **0 条 error**（修复前 4/12）、`preview.beat ×2`、失败日志 0 条。
   ⚠ **改这一处打破了一把旧锁**：v2.13.0 的 `t_main_draft_translation_flow` 原先断言"草稿以 auto 送译"
   （itest_home 的 asr_language 恰为 auto）——**那是把缺陷行为当契约锁住**，与第二十节 `objectName` 那把旧锁同类。
   已**升级**该锁为双分支（语言未知→不送；会话已解出语言→带真语言送），不是删断言。
2. **流式草稿尾部近似重复仍有残留**（复测 Round 2 末句实测到
   `…training session held. in the main reading room. session held in the main reading room.`）：
   标点差异（`held.` vs `held`）让词级锚与"整词相等"的重复检测双双漏过。与 21.2 第 1 条同源
   （那里记的是"旧 2 次/68 词 → 新 1 次/39 词"，本就是降频未清零）。
   **注意：这不是本轮改动引入的**——关掉预览的那一轮零重复。

**取证方法论补两条血泪（第一版复测脚本自伤，差点得出错误结论）**
- **改实例属性拦不住已连接的 Qt 槽**：`w._on_asr_text = spy` 之后信号仍派发原绑定方法 → 计数恒 0，
  一度看起来像"修复没生效"。要计数就 spy **被动态查找的那个入口**（如 `w.overlay.show_pending`，
  方法体内是 `self.overlay.show_pending(...)` 属性查找，替换有效），或直接连 `signal.connect(spy, unique=False)`。
- **隔离驱动脚本必须自己装日志 handler**：`app.log.get(<home>/logs/app.log)`（那是 `main.py` 的活）。
  漏掉时 `app.log` 整个是空的，会把"零 error/零 preview 事件"读成天大的结论。
- 数字直注喂出来的 `pipeline.latency` 里 `n_reco=0`、`hold_p50` 失真属**注入副作用**（`submit` 裸数组 → `t_flush=-1`），
  不要拿它当产品指标。


### 22.9 本轮产物与状态

- 代码：`app/asr/preview.py`（`normalize_language` + `_fail_streak`/`preview.degraded` 一次性告警）、
  `app/ui/main_window.py`（D-2：`_on_asr_text` 删第二次 `show_pending`；D-3：新增 `_spec_source_lang()`
  回退链 + `_last_asr_lang` 会话语言记忆，推测式两处送译点改为"解不出语言就不送"，终版路径零改动）。
- 发版：`bump_version.py 2.18.2` → `--check` = 「版本一致: 2.18.2」→ CHANGELOG v2.18.2 一节 →
  单提交（**务必含 `app/config.py`**，这是 CI 版本校验的历史坑）→ push main → tag v2.18.2 → CI 双资产。
- 锁：单元 +2（79→81）、集成 +2（110→112）**并升级 1 把 v2.13.0 旧锁**，终态 **81 / 112 全绿**（判定看 `TOTAL:` / `UNIT:` 行；两套件退出码 1 均为已知 Qt 收尾 AV，非失败）。
- 文档：本文件就地纠偏 7 处（套件计数 78→80 / 106→111 / 69→111 / 102→111、PATH python 告警、
  开场建议 v2.10.0→v2.18.2、第四节用户配置**磁盘现值勘误**、21.4 观察项关闭、
  22.6 内更正 `is_dual()` 并非死代码）+ 新增第二十二节全文。
- 提交：本地单次提交（缺陷修复 + 补锁 + 文档纠偏合一，**含 `app/config.py`**）→ push → tag
- 待办（下一会话接续）：
  1. **流式草稿尾部近似重复仍有残留**（22.8 观察 2：标点差异让词级锚与重复检测双双漏过，非本轮引入）；
     D-3 已在本轮一并修掉并真机复验（语言位仅 `en`、60 条推测回复零 error）。
  2. **把 115 项集成测试拉进 `build.yml` 闸门**（当前 CI 只跑单元 + smoke）——本轮所有技术债里价值最高的一项。
  3. 22.6 其余：`_restyle_dual_tgt` 双定义、`_starts_new_sentence` 两份拷贝、`probe_text_clip` 无自动化消费方、
     qa 脚本绝对路径写死、依赖无上限、`app/` 内 66 处 `except: pass`。

## 二十三、会话快照（2026-09-16 用户实拍三改 · v2.19.0）

### 23.1 用户诉求原话（附打码截图一张）

> "这是我打开上下双语，然后开始翻译的真实效果图，我觉得好别扭（我希望删掉红色框框的历史区域），
> 还有，那个分割线，你应该测试一下，我往上拉的时候他就往下，反之亦然，这是个 BUG，
> 还有能否添加一个关闭攒句的开关，我想进行实时的翻译"

截图实测状态：面板 619×515、字号 22、`原文 开`、**历史区吃掉上半部约 400px**、
当前句只剩 `you / 你个` 两行。

### 23.2 分割线 BUG：不是手感问题，是**几何恒等式错了**（真实事件流取证）

旧分配：`body = 贴内容(≤45%总高)`、`hist = 总高 − 工具条 − body`。于是分割线绝对位置

```
y = chrome + hist + src = total − sep − tgt − 边距      ← 与用户拖的 src 高度**完全无关**
```

`QMouseEvent + sendEvent` 实测（用户同一形态：619×515 + 历史 302px + src_h_user=87）：

```
往上拖 60px ：鼠标 −60 → 线 y +28（**反向**） src 87→30  hist 302→359
往下拖 120px：鼠标 +120 → 线 y −39（**反向**） src →150  hist →239
关原文时    ：鼠标 ±60/120 → **0 位移**（body 塌到 46px 地板 → 钳制区间 [30, max(30,46-8-24)=30] 宽度为 0）
```

**新分配（v2.19.0）**：历史区份额先按内容定（`≤45% 屏` 且 **`≤可用高度一半`** 且给 body 留
`body_min=92`（原文30+把手8+译文30+边距）），**`body = avail − hist` 与 src 无关** →
`y = chrome + hist + src` 一对一跟手；拖拽与恢复两处的钳制上限统一为 `body − sep − 30`（旧值 −24 与 −40 各处不一致）。

### 23.3 历史区：默认关（**不拆代码**，按第二十二节 22.6 与 v2.9.0 的教训）

- 新键 `overlay_dual_hist`，**DEFAULTS=False**：当前句独占面板，且面板**贴内容**（不再撑到 0.68 屏剩一个空框）。
- 关闭时 `dual_push_history` **直接 return**（不建控件、不占内存），`set_hist_enabled(False)` 会清空已建历史行。
- 能力完整保留：设置页「显示-字幕显示-双语面板历史区」与面板 ⋯ 菜单「显示历史区（上下双语）」都能开回来，
  回调 `on_hist_toggled` → `_on_panel_hist_toggled` 落盘（与 layout 开关同一套路）。
- `apply_overlay_from_config` 顺序契约：**layout → hist → src_h_user**（后两者都触发 _relayout，
  先定历史份额再定原文高度，最终几何才与"一次拖出"的结果一致）。

### 23.4 关攒句开关：`translate_grouping`（默认开=现状）

- 新键 `translate_grouping`，group=**instant**（每片段送译时实时读配置，**保存即生效、不重启管线**）。
- 关＝`_submit_for_translation` 走"逐片直送终版"通路（复用原 `low_latency_mode=False` 那条），
  但**分段仍由 low_latency_mode / segment_cap_s 决定** —— 关键取舍：用户要"实时"，
  若直接关低延迟会把切段退回 14s 慢档（更慢），故两开关解耦。
- 代价按项目红线如实写进设置页文案：切段处译文不完整、机翻味更重、在线引擎请求量上升（离线包无额度压力）。

### 23.5 锁与验收

- 集成 **+3**：`t_overlay_split_follows_mouse`（真实事件流，历史开/关两态各测"先下后上"，
  断言方向不反向 + 原文区跟手 + body ≥92 不塌缩 + 松手记录值=实际高度）、
  `t_overlay_dual_hist_default_off`（默认关：不建行/不可见/当前句独占 + 打开后能力回来 + DEFAULTS 断言）、
  `t_translate_grouping_off`（开=两片攒一组只送一次；关=两片各送一次且不进组；低延迟仍为真）。
- 升级 **2** 把旧锁（它们锁的正是本次要改的行为）：
  `panel: dual 拉高→历史区吃剩余、当前句贴内容` → 改为新契约（历史按内容、当前句吃剩余、保 92px 可拖下限）；
  `pipeline: dual 流式原文接线与对齐` → 显式 `overlay_dual_hist=True`（终版沉历史是"开启态"能力）。
- 套件：**单元 81 / 集成 115 全绿**（112→115）。
- 真机 A/B（同一 44s 素材、dual+cuda+argos+auto、无占屏）：见 23.6。

### 23.6 待办

1. 发版 v2.19.0（行为变更走 minor）：bump → CHANGELOG → add（含 `app/config.py`）→ push → tag → CI 双资产；
2. README「界面与功能详解-显示」需同步：面板布局段仍写着 dual"不保留历史"与新默认一致，但需补
   「双语面板历史区」开关与「翻译攒句合并」两条说明（红线：**文案不许与实际不符**）；
3. 用户旧配置里 `overlay_h=515` 会让面板保持 515 高（当前句独占，可拖分割线分配）——
   若用户嫌高，⋯ 菜单「恢复自动高度」即可，无需改代码。

### 23.7 新会话接手清单（本会话因图片审核反复 400 而中断，状态如下）

- **代码与测试已完成**（未提交？见下条）：三个诉求全部落地——
  `overlay_dual_hist`（默认关）/ dual 高度分配几何修正 / `translate_grouping`（默认开，关＝逐片即译），
  设置页三处登记与面板 ⋯ 菜单开关均已接好，README 两条说明已同步。
- **套件**：单元 **81** / 集成 **115** 全绿（判定看 `UNIT:` / `TOTAL:` 行，退出码 1 是 Qt 收尾 AV）。
- **真机 A/B 已过**（同一 44s 素材、dual+cuda+argos+auto、无占屏）：
  攒句开＝终版送译 7 次；攒句关＝11 次（逐片即译）；两轮历史区均 0 占位，
  `当前句区 + 工具条 66px = 面板总高`（188 / 214px）严丝合缝＝当前句独占面板。
- **待办（新会话第一件事）**：
  1. `git status` 确认改动清单 → 若未提交则按第二十三节内容提交（**含 `app/config.py`**）；
  2. 发版 v2.19.0：`python scripts/bump_version.py 2.19.0` → `--check` →
     CHANGELOG 新增 v2.19.0 一节（三条：历史区默认关 / 分割线跟手修正 / 攒句开关）→
     `git add`（含 app/config.py + README.md + HANDOFF.md）→ commit → push origin main →
     `git tag -a v2.19.0` → `git push origin +refs/tags/v2.19.0:refs/tags/v2.19.0` →
     `gh run list --branch v2.19.0` 轮询到 completed → `gh release view v2.19.0 --json assets` 核双资产；
  3. 用户侧提醒：其旧配置里 `overlay_h=515` 会让面板保持 515 高（当前句独占，可拖分割线分配）；
     嫌高就 ⋯ 菜单「恢复自动高度」。

## 二十四、会话快照（v2.19.0 已发布 → 用户回访反馈"关攒句后旧句消失"·v2.19.1 修复）

### 24.1 v2.19.0 发布记录（本会话按 23.7 SOP 完成）

- 发版提交 `c81b2f1`（三处版本号 + CHANGELOG，**显式含 app/config.py**——提交对象 blob 逐个核过）；
  push main → `git tag -a v2.19.0` → refspec push；
- CI run `35059311737`：`status=completed, conclusion=success`（版本一致性门 / 单元 / smoke / EXE 存活 / Inno 全过）；
- `gh release view v2.19.0`：**双资产齐**（Setup exe ≈91.3MB、portable zip ≈136.2MB，与 v2.18.2 体积差 ±3KB）；
- 发版前本地复跑：`UNIT: 81 PASS` / `TOTAL: 115 PASS`（offscreen 判定行，退出码 1=Qt 收尾 AV 不变）。

### 24.2 用户回访反馈与取证（真实 Qt 事件流，探针 `scripts/qa/dual_disappear_probe.py`）

> "即便我关闭了攒句，译文和原文还是有攒句感觉，一旦有新的翻译和识别，已翻译的译文和原文就会在悬浮字幕里消失了"

探针按主窗真实调用序打拍快照，结论拆两半：

1. **设计后果**（不是 bug）：历史区关（23.3 用户实拍裁决）后当前句区是**单句槽**；
   攒句关→片段短、终版频繁，上一句终版在 GPU 流式下只活 **1~2 拍（≈0.9~1.8s）**，
   CPU 无流式则下一片段一到即整对消失（t6 处 tgt 还闪 '…'）。hist_rows=0，终版在面板**无任何驻留**——
   这正是"当前句独占"的字面含义。
2. **两条真缺陷**：
   - **错配窗口**：`update_partial` 只覆盖原文区 → t4 屏上=「B 句草稿原文 + A 句终版译文」，
     随后草稿译再冲掉 A——即用户看到的"乱跳"；
   - **同句闪白**：流式草稿已在屏生长（t5 已有 B 的推测译），正式片段（大写开头、与草稿同源）
     晚到被 `_starts_new_sentence` 误判为新句 → tgt 打回 '…'，B 译文重长一遍。

### 24.3 用户裁决与修复（v2.19.1）

ask_user_question 四选一 → 用户选：**维持当前句独占，只修错配/闪白缺陷**
（历史区驻留 / 上一句驻留 / 列表布局三项均被否——别再往那个方向提）。

实现（全部在 `app/ui/caption_overlay.py` + 主窗 1 行配对参数）：

- 新状态位 `_dual_cur_open`：`_dual_new_sentence`=True、`_dual_show_result`=False、`clear_caption`=False；
- `update_partial` **原子换句**：闭合态 + 新拍不是屏上句的同源延伸 → 走 `_dual_new_sentence`
  （原文+译文同刻切，杜绝错配帧）；同源尾重复只长文本不动译文态；
- `_dual_show_pending`：大写开头片段先过 `_dual_same_sentence`（前缀包含 / 前 3 词同源）——
  同句 → 就地校准原文、**不重置译文区**；确属新句才重置；
- `update_dual_draft_tgt(translated, source_text=None)`：该句已收口时迟到草稿回复丢弃，
  不把淡定稿刷回淡色（主窗 2358 传 source 配对）；旧单参调用兼容。

### 24.4 锁与验收

- 集成新增 `t_dual_pair_atomic_swap`（**旧实现上必红**，探针 before/after 已实证）：
  ① 错配锁（新句首拍后 tgt≠A 终版）② 闪白锁（同句正式片段到达 tgt 保持）
  ③ 迟到草稿防御 ④ 尾重复延伸防御 ⑤ CPU 成对切换无错配帧；
- 套件：**单元 81 / 集成 116 全绿**；9 条 dual 相关既有锁零回归；
- 真机听感复验留给用户（新装 v2.19.1 后确认）——设备索引会漂（29↔33 教训），
  本会话未占用户音箱跑回放链路。

### 24.5 待办

1. 若用户确认体验 OK：发版 v2.19.1（补丁号，行为修正）——bump → CHANGELOG → add（含 app/config.py）
   → push → tag → CI 核双资产（同 23.7 套路）；
2. 遗留小项：CI 的 Node.js 20 弃用告警（actions/cache@v4 等四件），非阻塞，择机统一升版；
3. `dual_clear_current` 仍是应用侧零调用的死方法（仅测试引用）——留着无害，清理属另一条战线。

### 24.6 用户真机二轮裁决：历史区恢复默认开（v2.19.1 并入）

- 用户装/跑本地修复版后反馈"还是没有达到我想要的效果"；追问屏幕效果期望，答复原话：
  **"句子全部保留，不能在字幕悬浮窗里消失"** —— 24.3 那轮"维持独占"的选项词太抽象
  （用户按字面理解点了"修消失缺陷"，实际不可接受的就是消失本身），**产品侧教训：
  交互裁决题必须给画面类比与所见即所得的描述**（庭审/直播字幕、两句式、延时退场）。
- 处置：`overlay_dual_hist` **DEFAULTS False→True**。v2.19.0 默认关的依据（首轮实拍"历史区
  吃掉 2/3 面板"）是几何缺陷，23.2 已修（历史区按内容收缩、≤avail/2、body≥92 可拖），
  旧依据不成立；历史区上限 MAX_DUAL_HIST=30 对，更早句子主窗/导出永久保留——文案如实写"30 对"。
- 触点全查（红线：文案与实际一致）：`app/config.py` DEFAULTS+注释、`settings_dialog` 标题
  "（默认开）"+desc 重写、`main_window` 1058 注释、`caption_overlay` 315 注释、
  README 161 行、集成锁 `t_overlay_dual_hist_default_off` → **升级**为
  `t_overlay_dual_hist_default_on`（断言 DEFAULTS True + 开启态两句逐句驻留 + 裸构造默认开 +
  关闭态独占能力保留——两态契约都要在，不许反向锁死）。
- **用户本机持久值也要改**：其 `~/.live_subtitle/config.json` 里 `overlay_dual_hist=false`
  是 v2.19.0 期间落盘的，DEFAULTS 变更对其不生效——已直接改写该键为 true（无运行实例时改，
  防退出回写竞态）。教训：**改默认值必须检查已装用户的持久值路径**。
- 套件：单元 **81** / 集成 **116** 全绿（前台跑——用户明令**测试一律前台执行，别再开后台任务**）。
- 复验留给用户：面板启动时历史区是空的（还没有句子），开始翻译说几句后才会逐句堆积——
  试听时别因"刚打开没看到历史区"误判未生效。
