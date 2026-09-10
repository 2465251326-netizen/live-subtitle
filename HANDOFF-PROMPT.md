# 新会话接力提示词（复制以下全部内容发给新会话）

---

读 `HANDOFF.md`（项目根目录，约 10KB）接手 LiveSubtitle 实时字幕翻译项目。

**工作区**：`C:\deepseek (2)\live-subtitle`（Windows + Python 3.14 + PySide6 + faster-whisper）
**仓库**：https://github.com/2465251326-netizen/live-subtitle
**当前版本**：v2.2.10（已发布，含 Setup EXE + portable zip 双资产）

## 首要任务：真实用户视角的模型端到端测试

像正常用户一样使用：**GPU 计算 + 体量最大的模型（large-v3-turbo）+ 开启连续翻译（悬浮条连续文本流）+ 打开任意英语视频**，验证完整链路（识别 → 翻译 → 主窗字幕卡 + 悬浮条连续流）。

**测试前先确认当前设置，并解决两个已知阻塞**：
1. `engine = 'argos'` 但 `~/.live_subtitle/argos/packages` **为空** → 当前翻译必然失败，需改为 `google`/`auto`（本机代理 `http://127.0.0.1:10808`）或下载离线语言包
2. `overlay_enabled = False` → 要观察连续翻译效果需先打开（设置-显示 或按 `Ctrl+Alt+O`）

**推荐测试方法**（HANDOFF.md 第三节有细节）：用 Windows SAPI 生成 en-US 英语语音 → `SoundPlayer` 播放到默认输出设备 → 系统环回采集 → 观察字幕。日志在 `~/.live_subtitle/logs/app.log`。

## 工作约定（务必遵守）

- 动手前先读 HANDOFF.md 第五节「关键知识点」：WM_HOTKEY 双发根因、offscreen 无字体、5 个已修高危问题勿回退
- **发版流程**：`python tests/test_units.py`（28 项）+ `python tests/test_integration.py`（20 项）全绿 → `python scripts/bump_version.py X.Y.Z` → 更新 README 更新日志 → `git add`（**必须包含 app/config.py**，否则 CI 版本校验失败）→ commit → push main → tag → push tag → 后台监控 CI → `gh release view` 确认双资产
- **热键类改动必须用真实 SendInput 按键验证**，不能只调内部函数自证（此前因此漏掉真实 BUG 被用户批评）
- **界面文字类问题**用 `scripts/probe_text_clip.py` 程序化探测（真实 windows 平台，offscreen 无字体测量不准）
- 探测脚本用完即删，产物走 `.gitignore`

## 用户偏好

- 需求要**全做**，功能尽量做成设置里的开关
- ⚠️ **界面文案不许承诺未实现的功能**（已因此被批评两次：Ctrl+Alt+O、托盘"切换输入来源"）
- 测试必须**真实验证**，不接受"调用内部函数自证"
- 中文沟通，反馈直接、要求高，会实测并截图指出问题

## 禁区

- 不要读取或提交 `.session-archive.md`（含敏感令牌，已 gitignore）
- 不要删除 `build_env/`（约 680MB，本机 GPU 运行时依赖）
- 修改用户配置前先征询
