import queue
import re
import threading
import time

import numpy as np
from PySide6.QtCore import QThread, Signal

from app.fixmap import apply_dict
from app.i18n import ui_text, ui_fmt



def _silero_assets_ok() -> bool:
    """检测 faster-whisper 自带的 Silero VAD onnx 资产是否存在。

    打包环境若漏打资产（v1.9.0 安装版），vad_filter=True 会让
    ONNXRuntime 抛英文 NO SUCH FILE；此处兜底自动回退能量 VAD。
    """
    try:
        import os
        import faster_whisper
        assets = os.path.join(os.path.dirname(faster_whisper.__file__), "assets")
        if not os.path.isdir(assets):
            return False
        return any(n.endswith(".onnx") for n in os.listdir(assets))
    except Exception:
        return False


def split_long_caption(text, limit=60):
    """把一段超长识别结果按句末标点二次切分，避免快语速内容出现 14 秒长字幕。"""
    text = text.strip()
    if len(text) <= limit:
        return [text]
    # v2.3.14（P17）：旧切分 (?<=[.!?])\s* 允许"零宽空格"下刀，把 "U.S."
    # 这类缩写从词内劈成 "U." / "S. strikes..."（第九轮实况新闻实锤）。
    # 新规则：拉丁句末标点须"后有空格且点前不是单字母大写"才切——
    # "U.S. strikes" 两处句号（U. 前是词首、S. 前是大写字母）都不下刀；
    # CJK 句末标点后通常无空格，直切。（已知边界：Mr./Dr. 等头衔仍可能切，
    # 但后续小写延续合并会吸收大部分此类碎片。）
    parts = [p.strip() for p in
             re.split(r"(?<=[^A-Z][.!?;])\s+|(?<=[。！？；])", text) if p.strip()]
    if len(parts) <= 1:
        return [text]
    merged = []
    for p in parts:
        # 过短的尾巴并入前一句，避免碎片化
        if merged and len(merged[-1]) + len(p) + 1 < limit // 2:
            sep = "" if merged[-1][-1] in ".!?。！？；;" else " "
            merged[-1] = merged[-1] + sep + p
        else:
            merged.append(p)
    return merged


def model_repo_id(model_size: str) -> str:
    """模型对应的 HF 仓库 ID（v2.0.9）。

    large-v3-turbo 的 CTranslate2 权重在社区仓库
    mobiuslabsgmbh/faster-whisper-large-v3-turbo——Systran 下**不存在**
    这个仓库（401 Repository Not Found，此前该下拉项从未真正可用，
    用户实测暴露）。其余尺寸均为官方 Systran 仓库。
    """
    if model_size == "large-v3-turbo":
        return "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
    return "Systran/faster-whisper-" + model_size


def _hf_call(fn, *args, **kwargs):
    """HF Hub 调用包装（v2.1.0）：优先本机凭据（有有效令牌者享更高速率），
    认证失败（401/403/过期令牌）自动退回匿名重试。

    v2.0.9 曾无条件 token=False 修 401（本机过期令牌会拖累公开仓库下载），
    但这让持有效令牌的用户白白失去更高速率；现改为"先试凭据、认证失败
    再匿名"，两端兼顾。仓库不存在（RepositoryNotFoundError）同样含 401
    文本，匿名重试后原样抛出，不掩盖真实错误。"""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        msg = str(e)
        if ("401" in msg or "403" in msg or "Unauthorized" in msg
                or "Invalid username or password" in msg):
            anon = dict(kwargs)
            anon["token"] = False
            return fn(*args, **anon)
        raise


def download_model_files(model_size, should_stop=None, progress=None):
    """受控逐文件下载 faster-whisper 模型（v2.0.5）。

    相比 WhisperModel 构造时的内建黑盒下载：每个文件下载之间检查
    should_stop()，命中即返回 "stopped"（单文件内部无法中断，hf_hub
    断点续传保证已下载部分下次继续有效）；progress(i, total, name)
    在调用线程内逐文件回调。使用标准 HF 缓存布局（HF_HOME/hub），
    完成后 model_cached 即通过，WhisperModel 以 local_files_only 加载。
    文件列表获取/下载异常向上抛出，由调用方负责友好化与兜底。
    """
    from huggingface_hub import list_repo_files, hf_hub_download
    repo = model_repo_id(model_size)
    # v2.1.0：经 _hf_call 包装——默认带本机凭据（有效令牌享更高速率、
    # 消除"未认证请求"警告），401/403 自动退回匿名（v2.0.9 场景）
    files = _hf_call(list_repo_files, repo)
    total = len(files)
    for i, name in enumerate(files, 1):
        if should_stop is not None and should_stop():
            return "stopped"
        _hf_call(hf_hub_download, repo, filename=name,
                 cache_dir=str(_hub_root()))
        if progress is not None:
            try:
                progress(i, total, name)
            except Exception:
                pass
    return "done"


def _hub_root():
    """HF 缓存根唯一出口（v2.6.3，P1-9）。

    用户预设 HF_HOME 环境变量时，下载/加载/判定三处必须同走环境变量
    路径——此前判定（model_cache_dir）读环境变量、下载与 WhisperModel
    构造写 config.HF_HOME 常量（CONFIG_DIR/hf），两侧分裂：预设用户的
    模型可能下到一处、判定却查另一处，1.6GB 双份下载/反复重下。
    Config relocate 会同步改写环境变量（config.py），自定义根用户不变。
    """
    import os
    from pathlib import Path
    from app.config import HF_HOME
    return Path(os.environ.get("HF_HOME") or str(HF_HOME)) / "hub"


# v2.0.6：进程内模型实例缓存（容量 1）——切输入来源/改识别设置重启管线
# 不再全量重载模型（CPU 上数秒到数十秒）。键 = (model_size, device,
# compute_type)；换模型/设备时旧实例被替换、由 GC 释放显存/内存。
_MODEL_CACHE = {}
_MODEL_CACHE_LOCK = threading.Lock()
# v2.6.4（P2）：构造互斥——预热与真实管线并发时此前会双份构造 WhisperModel
# （GPU 冷初始化 49s×2、显存/内存峰值翻倍，先完成者入池另一个等 GC）
_MODEL_LOAD_LOCK = threading.Lock()


def _torch_cuda_ready() -> bool:
    """强制 GPU 的运行时前置检查 + DLL 预载（v2.1.2/v2.1.3）。

    运行时来源（二选一）：
    - CUDA 版 PyTorch（一键安装/手动装，DLL 在 torch/lib）；
    - NVIDIA 独立运行时包 nvidia-cublas-cu12 / nvidia-cudnn-cu12
      （纯二进制轮子不挑 Python 版本——PyTorch 官方源最高只发布到
      Python 3.13，3.14 用户走这条路，v2.1.3 实测）。
    机制（本机实测踩坑记录）：ctranslate2 运行时按名 LoadLibrary 加载
    cublas64_12.dll，走标准搜索顺序（PATH），**add_dll_directory 注册的
    目录不在其中**——必须把 DLL 目录前置进 PATH。然后用 ctypes 直接
    加载两个 DLL 验证（比 get_cuda_device_count 枚举更贴近真实推理）。
    """
    import os
    candidates = []
    try:
        import torch as _t
        if getattr(_t.version, "cuda", None):
            candidates.append(os.path.join(os.path.dirname(_t.__file__), "lib"))
    except Exception:
        pass
    for pkg in ("nvidia.cublas", "nvidia.cudnn"):
        try:
            import importlib.util
            spec = importlib.util.find_spec(pkg)
            if spec and spec.submodule_search_locations:
                base = list(spec.submodule_search_locations)[0]
                candidates.append(os.path.join(base, "bin"))
        except Exception:
            pass
    found = [d for d in candidates if os.path.isdir(d)]
    if not found:
        return False
    for d in found:
        try:
            os.add_dll_directory(os.path.abspath(d))
        except Exception:
            pass
        # 关键：前置进 PATH——标准 LoadLibrary 搜索顺序才会命中
        path = os.environ.get("PATH", "")
        if d not in path:
            os.environ["PATH"] = os.path.abspath(d) + os.pathsep + path
    try:
        import ctypes
        ctypes.WinDLL("cublas64_12.dll")
        ctypes.WinDLL("cudnn64_9.dll")
        return True
    except Exception:
        return False


def has_content(text: str) -> bool:
    """v2.3.1：文本是否含有效内容（字母/数字/CJK 任一）。纯标点段
    （"....." "？？？"——新闻转场/呼吸段常见残留）不是字幕，无条件滤除。
    幻觉过滤器按概率判，这类段 logprob 高会漏网，故独立于开关。"""
    return any(ch.isalnum() or "\u4e00" <= ch <= "\u9fff" for ch in text)


def hallucination_ok(avg_logprob: float, no_speech_prob: float) -> bool:
    """幻觉抑制判据（v2.22.0 起为**唯一出口**，正式识别与流式预览共用一把闸）。

    音乐/噪声段的常见特征是"高置信度胡言"或"无语音概率高 + 置信度低"，命中
    其一即丢弃（阈值偏保守，宁可少出一条也不出乱码字幕）。

    为什么必须共用：流式预览通道以前自己什么都不判（`" ".join(seg.text)`），
    于是正式通道必滤的胡话会照样上到面板的原文行，还会被送去推测翻译
    （§37.1 F4，实测 "You are a video! ♪ ♪ ♪ KRAVZO…" 整行留在屏上）。"""
    return avg_logprob >= -1.2 and not (no_speech_prob > 0.8 and avg_logprob < -0.5)


# v2.5.4：识别精度三档（用户反馈"速度慢/准确度差"的取舍显式化）——
# beam/上下文条件是转写质量与速度的最大杠杆：快速档牺牲精度换实时，
# 高精度档补回上下文条件与宽束（慢 2~4 倍，适合回看/整理字幕场景）
ACCURACY_PROFILES = {
    "fast":     {"beam_size": 1, "best_of": 1, "condition_on_previous_text": False},
    "balanced": {"beam_size": 2, "best_of": 2, "condition_on_previous_text": False},
    "quality":  {"beam_size": 5, "best_of": 5, "condition_on_previous_text": True},
}


def transcribe_kwargs(accuracy, silero_vad=False, hotwords=""):
    """识别参数按精度档组装（纯函数，便于回归锁）。未知档位回退 fast。

    v2.7.0（T3）：hotwords 非空时注入 initial_prompt——whisper 官方支持的
    事前提示，专名/术语命中率受益（对比事后词典：无需选"长到不歧义的键"）。
    截断到 160 字符，防挤占 224 token 解码预算；fast/balanced 档
    condition_on_previous_text=False，无 prompt 链式复读滚雪球风险。"""
    profile = ACCURACY_PROFILES.get(str(accuracy or "fast"), ACCURACY_PROFILES["fast"])
    kwargs = dict(profile)
    kwargs.update(
        no_speech_threshold=0.6,
        log_prob_threshold=-1.0,
    )
    hw = str(hotwords or "").strip()
    if hw:
        kwargs["initial_prompt"] = hw[:160]
    if silero_vad:
        kwargs["vad_filter"] = True
        kwargs["vad_parameters"] = {"min_silence_duration_ms": 300}
    return kwargs


class AsrThread(QThread):
    # v2.3.16（P21）信号契约：text_ready = (识别文本, whisper 语言码,
    # **音频秒数**字符串如 "4.3"——不是毫秒、不是百分数)；消费方
    # _on_asr_text/_asr_timing 按秒 float()。
    # v2.3.20（P26）：追加第 4 参 t_flush_mono（浮点 monotonic 秒）——该音频段
    # 在采集线程"切分完成"的时刻，用于测"话音落→原文上屏"识别段延迟；-1=未知。
    # v2.7.5（R-4）：回退 4 参——T2 曾加 tail_quiet/last_logprob 两参作"提前冲"
    # 判据，后被真机审计证伪（whisper 把末片结束时间拉伸补齐到音频尾，tail_q
    # 恒≈0，判据只保留时间地板），死数据管道整体拆除。
    text_ready = Signal(str, str, str, float)  # text, whisper_lang, duration, t_flush_mono
    # v2.7.5（R-2）：复检丢弃通知——语言复检低置信不一致的段此前静默 return，
    # 用户视角"字幕无预警跳过一大段内容"。主窗以此建弱化卡留痕，可排查。
    recheck_dropped = Signal(str, float)
    status_changed = Signal(str)
    # v2.20.6（i18n 前置改造）：积压预警单独发信号。主窗此前用
    # `"识别积压" in text` 从状态文本里猜，界面语言一切英文这个判断就哑了
    # ——而它决定的是"要不要常驻显示丢段警告"，不是显示什么字。
    backlog = Signal()
    model_ready = Signal()
    error_occurred = Signal(str)

    def __init__(self, model_size: str, device: str, language: str, parent=None,
                 hallucination_filter=True, silero_vad=False, mishear_map=None,
                 accuracy="fast", mishear_whole_word=False,
                 hotwords="", lang_recheck=True, turbo=False):
        super().__init__(parent)
        self.model_size = model_size
        self.device = device
        self.accuracy = str(accuracy or "fast")
        self.language = language
        self.hallucination_filter = bool(hallucination_filter)
        self.silero_vad = bool(silero_vad)
        self.mishear_map = dict(mishear_map or {})
        # v2.6.0（R2）：词典全词匹配开关快照
        self._mishear_whole_word = bool(mishear_whole_word)
        # v2.7.0（T3/T5）：热词提示 + 语言锁复检
        self.hotwords = str(hotwords or "")
        self.lang_recheck = bool(lang_recheck)
        self._seg_count = 0
        # v2.7.2：榨干模式——GPU 权重 INT8（Turing 起有 INT 张量核，解码 1.2~1.6×、
        # 显存约省半；识别率可能轻微下降，文案如实）
        self.turbo = bool(turbo)
        self.queue_in: "queue.Queue[object]" = queue.Queue()
        self._stop = False
        self._model = None
        self._lang_lock = threading.Lock()
        self._last_lang = language if language != "auto" else None
        self._discard_streak = 0
        # v2.0.8：背压重设计——队列上限放宽 + 积压/丢段状态只报一次
        self.QUEUE_LIMIT = 8
        self._backlog_reported = False
        self._dropped_ever = False

    def update_mishear_map(self, mapping, whole_word):
        """v2.6.0（R5）：设置保存后热更新误听词典，无需重启管线。"""
        self.mishear_map = dict(mapping or {})
        self._mishear_whole_word = bool(whole_word)

    def _postprocess(self, text):
        """识别后处理（建议5）：可选的常见误听修正词典。

        v2.6.0（R2）：改走 fixmap 单轮替换器——长键优先、替换产物不再被
        同轮二次命中；_mishear_whole_word=True 时纯拉丁词条按整词匹配。"""
        if not self.mishear_map:
            return text
        return apply_dict(text, self.mishear_map, self._mishear_whole_word)

    @staticmethod
    def model_cache_dir(model_size: str):
        from app.asr.engine import model_repo_id
        # v2.0.9：缓存目录名跟随真实仓库 ID（large-v3-turbo 的权重在
        # mobiuslabsgmbh 仓库，目录名不再硬编码 Systran 前缀）
        # v2.3.10（P11）：根目录跟随 huggingface_hub 的实际解析——用户预设
        # HF_HOME 环境变量时 hub 下载/加载走环境变量而非 CONFIG_DIR/hf，
        # 判定不同步曾让预热误报 not_cached 跳过（第六轮隔离环境实锤）。
        # v2.6.3（P1-9）：根目录逻辑收敛到 _hub_root()，与下载/加载同源
        return _hub_root() / ("models--" + model_repo_id(model_size).replace("/", "--"))

    @staticmethod
    def model_cached(model_size: str) -> bool:
        """缓存完整判定：model.bin 存在且体积达到真实模型量级（>50MB）。

        只查存在性会把 0 字节/半截文件当完整缓存（v1.9.0 用户的
        NO SUCH FILE 事故根因），这里用体积阈值兜底识别损坏缓存。
        """
        snap = AsrThread.model_cache_dir(model_size) / "snapshots"
        for p in snap.glob("**/model.bin"):
            try:
                if p.stat().st_size > 50 * 1024 * 1024:
                    return True
            except OSError:
                continue
        return False

    @staticmethod
    def model_size_mb(model_size: str) -> float:
        """已缓存模型的实际磁盘占用（MB），未下载返回 0。"""
        from app.translate.offline_pack import dir_size_mb
        return dir_size_mb(AsrThread.model_cache_dir(model_size))

    @staticmethod
    def model_state(model_size: str):
        """(状态, 磁盘MB)：full=已缓存可用；partial=有残留但不完整
        （含半截 model.bin，v1.9.0 事故形态）；missing=本地无数据。"""
        mb = AsrThread.model_size_mb(model_size)
        if AsrThread.model_cached(model_size):
            return "full", mb
        if mb > 0.5:
            return "partial", mb
        return "missing", mb

    @staticmethod
    def remove_model(model_size: str) -> bool:
        """删除识别模型缓存目录（含未完成下载残留）；返回是否删除干净。

        Windows 上正在运行的 ctranslate2/杀软握旧文件句柄会让 rmtree
        部分失败——重试 + 复核目录确实消失，保证"删除成功"名副其实
        （v2.0.5；此前 ignore_errors=True 静默半删）。
        同时逐出进程内实例缓存（v2.0.6）：否则池中实例仍握着句柄，
        且"删除后重新下载"会被内存里的旧实例短路。
        """
        import shutil
        import time as _t
        with _MODEL_CACHE_LOCK:
            for k in [k for k in _MODEL_CACHE if k[0] == model_size]:
                _MODEL_CACHE.pop(k, None)
        d = AsrThread.model_cache_dir(model_size)
        if not d.exists():
            return False
        for _ in range(3):
            shutil.rmtree(d, ignore_errors=True)
            if not d.exists():
                return True
            _t.sleep(0.5)
        return not d.exists()

    def stop(self):
        # v2.6.2（P1-4）：排水式停止——清队列到剩 1（保留队尾最新段，即
        # capture 停止 flush 的尾段）后靠 _stop 标志排空退出。旧实现清空
        # 队列再投哨兵，尾段在三层丢弃链（断信号/清队列/running 守卫）中
        # 必死，"说完立刻停丢最后一句"（v2.2.1 修复实际无效）
        self._stop = True
        try:
            while self.queue_in.qsize() > 1:
                self.queue_in.get_nowait()
        except Exception:
            pass

    def submit(self, audio):
        """语音段入队。

        v2.0.8 重新设计背压：medium CPU 上一段转写 ~10s，快语速内容
        30s 就能切出 5+ 段，旧"队列 ≥3 丢最旧"会把大多数段静默丢掉
        （实测 8 段提交 0 条字幕，用户观感=完全没反应）。
        现在：上限放宽到 8 段（约 2 分钟语音），只在真实超限时丢最旧；
        丢段/积压状态只发一次，避免刷屏。

        v2.3.20（P26）：audio 可为 (ndarray, t_flush) 二元组（capture 侧带
        切分时刻），或裸 ndarray（deep_windows/smoke 直接 submit 的旧格式，
        t_flush 记 -1）。入队元素统一为二元组。
        """
        # v2.7.4（B-2）：尾段双通道去重——停止时 capture 的尾段会经"排队信号 +
        # stop_pipeline 直塞"两条路进同一队列（同一 tuple 对象）；慢机上 asr
        # 仍在排水时信号路先被消费、直塞路再入队一次 → 同句二次转写上屏。
        # 身份比对+强引用（防 id 复用误伤），正常段每对象仅出现一次不受影响
        if isinstance(audio, tuple) and audio is getattr(self, "_tail_seen", None):
            return []
        if isinstance(audio, tuple):
            self._tail_seen = audio
        if isinstance(audio, tuple):
            audio, t_flush = audio
        else:
            t_flush = -1.0
        try:
            dropped = False
            while self.queue_in.qsize() >= self.QUEUE_LIMIT:
                try:
                    self.queue_in.get_nowait()
                    dropped = True
                except queue.Empty:
                    break
            self.queue_in.put_nowait((audio, t_flush))
            if dropped:
                self._dropped_ever = True
                if not self._backlog_reported:
                    self._backlog_reported = True
                    # 顺序要紧：两个信号都是排队投递，backlog 先发才能保证
                    # _on_asr_status 处理时 _backlog_warn 已置位（与改造前
                    # "在同一个槽里先置标志再刷状态"的时序一致）
                    self.backlog.emit()
                    self.status_changed.emit(
                        ui_text("识别积压，部分较早语音来不及转写已被跳过："
                        "CPU 跟不上当前模型，建议换 small/tiny（状态栏持续提醒）"))
                from app import log as app_log
                app_log.log("asr.segment_dropped", queue=self.queue_in.qsize())
            elif self.queue_in.qsize() >= self.QUEUE_LIMIT - 2 and not self._backlog_reported:
                self._backlog_reported = True
                self.backlog.emit()
                self.status_changed.emit(
                    ui_text("识别积压：转写速度跟不上语音产出，字幕会延迟陆续出现"
                    "（CPU 较慢建议换 small/tiny）"))
        except Exception:
            pass

    def _load_model(self, blocking=True):
        if self._model is not None:
            return True
        from app import log as app_log
        cached = self.model_cached(self.model_size)
        if not cached:
            # 模型需要联网下载：走 huggingface_hub（只认环境变量），下载前同步代理策略
            from app.config import ensure_hf_endpoint_ready
            ensure_hf_endpoint_ready()
            try:
                from app import net as _net
                _net.apply_proxy_env()
            except Exception:
                pass
            # v2.0.5：受控逐文件下载——每个文件之间可被停止打断（此前
            # WhisperModel 构造内建下载无法中断，加载期停止要等下载完）；
            # 列表获取失败时回落内建下载路径，行为与 v2.0.4 一致
            if not self._stop:
                try:
                    outcome = download_model_files(
                        self.model_size, should_stop=lambda: self._stop)
                    if outcome == "stopped":
                        self.status_changed.emit(ui_text("已停止模型下载（已下载部分保留，下次继续）"))
                        return False
                    cached = True
                except Exception:
                    app_log.exception("asr.controlled_download_failed",
                                      model=self.model_size)
        if self._stop:
            # v2.0.4：加载期间用户已停止——端点探测/代理同步完成后直接放弃，
            # 不再进入耗时的 import/构造阶段（此前要等模型加载完才检查 _stop）
            return False
        # v2.0.11：auto 不再信任 CUDA——get_cuda_device_count 只证明驱动能看见卡，
        # 不代表 cuDNN/cuBLAS 运行时齐备；缺失时 ctranslate2 推理会**静默挂死**
        # （本机实测：7s 段 86s 无返回，症状=永远"正在聆听"）。auto 一律走 CPU
        # （必定可用）；要 GPU 需显式选 cuda 并装好 CUDA 版 PyTorch（gpu.py 引导）
        # v2.1.1：三档语义——cpu=强制 CPU；cuda=强制 GPU（加载失败回落 CPU 并
        # 明确提示）；auto=安全档（CPU，等价旧"自动"降级后的行为）
        device = self.device if self.device in ("cpu", "cuda") else "cpu"
        # v2.1.2：强制 GPU 前置运行时检查 + DLL 预载——torch CUDA 版自带
        # cuDNN/cuBLAS，import+init 将其载入进程，ctranslate2 按名解析命中；
        # 未装/CPU 版/驱动异常 → 回落 CPU（就绪提示会说明原因）
        if device == "cuda" and not _torch_cuda_ready():
            device = "cpu"
        # v2.7.2：榨干模式 GPU 权重 INT8（int8_float16：权重 int8/激活 fp16）；
        # cache_key 含 compute_type，池天然隔离，切换即重载不混池
        if device == "cuda":
            compute_type = "int8_float16" if self.turbo else "float16"
        else:
            compute_type = "int8"
        # v2.0.6：进程内实例复用——同一 (模型, 设备, 量化) 在池中直接取用，
        # 切输入来源/改识别设置重启管线不再全量重载（CPU 上数秒到数十秒）。
        # 池容量 1，换模型/换设备时旧实例被替换由 GC 释放
        cache_key = (self.model_size, device, compute_type)
        # v2.6.4（P2）：加载互斥——真实加载持锁等待（预热完成后直接命中池，
        # 不再重复构造）；预热线程以 non-blocking 参与，真实加载已持锁时
        # 立即让位（其完成即达成预热目的）。下载/设备解析留在锁外，互斥
        # 只覆盖"池检查→构造→入池"
        if not _MODEL_LOAD_LOCK.acquire(blocking=blocking):
            app_log.log("asr.model_load_skipped_busy", model=self.model_size)
            return False
        try:
            return self._construct_and_pool(cache_key, device, compute_type, cached)
        finally:
            _MODEL_LOAD_LOCK.release()

    def _construct_and_pool(self, cache_key, device, compute_type, cached):
        """构造 WhisperModel 并入池（v2.6.4 提取，加载互斥锁内执行）。"""
        from app import log as app_log
        with _MODEL_CACHE_LOCK:
            pooled = _MODEL_CACHE.get(cache_key)
        if pooled is not None:
            self._model = pooled
            self._device_used = getattr(pooled, "_ls_device", device)
            app_log.log("asr.model_reused", model=self.model_size,
                        device=self._device_used)
            return True
        import time as _time
        # v2.0.9：large-v3-turbo 传完整 HF 仓库 ID（faster-whisper 支持任意
        # CT2 模型 ID），否则它硬编码拼 Systran 仓库必 404
        model_ref = model_repo_id(self.model_size)
        t0 = _time.perf_counter()
        try:
            self._model = self._construct_model(
                model_ref, device, compute_type, local_only=cached)
            if self._stop:
                self._model = None
                return False
            self._after_model_constructed(device, cache_key)
            app_log.log("asr.model_loaded", model=self.model_size, device=device,
                        cached=cached, seconds=round(_time.perf_counter() - t0, 2))
            self._device_used = device
            return True
        except Exception as e:
            # v2.4.4（BUG-2）：判定"缓存完整"却加载失败 = 快照结构损坏/校验不过
            # （实测：huggingface_hub 1.30 离线完整性校验拒载体积完好的缓存）——
            # 先同设备联网重试一次（校验/补全快照），而不是直接降到 CPU：
            # 此前 GPU 用户被静默回落 CPU，延迟 6~10 倍且界面仍显示 GPU。
            if cached and not self._stop:
                try:
                    self.status_changed.emit(ui_text("模型缓存校验异常，正在联网修复…"))
                    self._model = self._construct_model(
                        model_ref, device, compute_type, local_only=False)
                    if self._stop:
                        self._model = None
                        return False
                    self._after_model_constructed(device, cache_key)
                    app_log.log("asr.model_repaired_online", model=self.model_size,
                                device=device, first_error=str(e)[:120])
                    self.status_changed.emit(ui_text("模型缓存已修复"))
                    self._device_used = device
                    return True
                except Exception as e2:
                    e = e2
            if device == "cuda" and not self._stop:
                # v2.0.11：显式 CUDA 加载失败（缺运行时等）回落 CPU，不再依赖 auto 分支
                try:
                    self._model = self._construct_model(
                        model_ref, "cpu", "int8", local_only=False)
                    # v2.2.1：与主路径同款 _stop 后置检查（构造期可能被停止）
                    if self._stop:
                        self._model = None
                        return False
                    self._after_model_constructed("cpu", (self.model_size, "cpu", "int8"))
                    self._device_used = "cpu"
                    # v2.4.4（BUG-2）：回落必须告知用户（此前只写日志，界面仍显示
                    # GPU，用户无感损失 6~10 倍速度）。走状态通道进主窗状态行
                    self.status_changed.emit(
                        ui_fmt("⚠ GPU 加载失败已回落 CPU（模型较重时字幕明显滞后）——"
                               "原因：{err}。可在「设置-语音识别」检测 GPU 环境",
                               err=str(e)[:60]))
                    app_log.log("asr.cuda_fallback_cpu", model=self.model_size, err=str(e)[:120])
                    return True
                except Exception:
                    pass
            self._device_used = "cpu"
            from app.errors import friendly_error
            from app import log as app_log
            app_log.exception("asr.model_load_failed", e, model=self.model_size)
            self.error_occurred.emit(f"{ui_text('模型加载失败：')}{friendly_error(e)}")
            return False

    def _construct_model(self, model_ref, device, compute_type, local_only):
        # v2.4.4：WhisperModel 构造参数唯一出口（原三处手写易漂移）
        from faster_whisper import WhisperModel
        return WhisperModel(
            model_ref,
            device=device,
            compute_type=compute_type,
            download_root=str(_hub_root()),
            local_files_only=local_only,
        )

    def _after_model_constructed(self, device, cache_key):
        # v2.4.4：入池（原 _load_model 内联逻辑提取；_stop 检查留在调用点，
        # 停止路径不得被 except-重试链吞掉）
        self._model._ls_device = device
        with _MODEL_CACHE_LOCK:
            _MODEL_CACHE.clear()
            _MODEL_CACHE[cache_key] = self._model

    def run(self):
        # v2.0.1：加载阶段整体兜底——此前 import/端点探测/构造只有构造在 try
        # 内，faster_whisper 损坏等异常让线程静默死亡（error_occurred 不发，
        # UI 永远停在"正在加载"，下载进度定时器永不停）
        try:
            if self.model_cached(self.model_size):
                # v2.3.5（P5-B）：GPU 冷启动实测近 1 分钟（CUDA 上下文初始化），
                # 文案必须如实——此前只说"几秒到几十秒"，用户以为卡死
                if str(self.device) == "cuda":
                    self.status_changed.emit(
                        ui_fmt("正在加载 {size} 模型（GPU 首次初始化约 1 分钟，"
                               "仅第一次；之后秒开，可在「设置-语音识别」开启启动预热）...",
                               size=self.model_size))
                else:
                    self.status_changed.emit(ui_fmt(
                        "正在加载 {size} 模型（本地缓存，CPU 上通常需几秒到几十秒）...",
                        size=self.model_size))
            else:
                self.status_changed.emit(ui_fmt(
                    "正在准备 {size} 模型（首次运行会自动下载，见状态栏进度）...",
                    size=self.model_size))
            if not self._load_model():
                return
        except Exception as e:
            from app.errors import friendly_error
            from app import log as app_log
            app_log.exception("asr.setup_failed", e)
            self.error_occurred.emit(f"{ui_text('识别引擎启动失败：')}{friendly_error(e)}")
            return
        if self._stop:
            # 加载期间用户已按停止：直接退出，不再报“就绪”
            return
        self.model_ready.emit()
        dev = getattr(self, "_device_used", "cpu")
        if dev == "cuda":
            self.status_changed.emit(ui_text("就绪，正在聆听...（GPU · CUDA 加速已生效）"))
        elif dev == "cpu" and self.device == "cuda":
            # v2.1.1：显式"强制 GPU"回落 CPU 时明确告知原因与出路
            self.status_changed.emit(ui_text("就绪，正在聆听...（CPU 模式 · 强制 GPU 不可用已回落："
                                     "请先「检测 GPU 环境」并安装 CUDA 版 PyTorch）"))
        elif dev == "cpu" and self.device == "auto":
            # v2.0.11：auto 明确回落为 CPU 时如实告知（此前 auto 显示与
            # 显式 CPU 无差别，用户不知道 GPU 没用上）
            self.status_changed.emit(ui_text("就绪，正在聆听...（CPU 模式 · auto 未启用 GPU："
                                     "如需加速请显式选 cuda 并安装 CUDA 版 PyTorch，见 GPU 检测引导）"))
        else:
            self.status_changed.emit(ui_text("就绪，正在聆听...（CPU 模式）"))
        if self.silero_vad and not _silero_assets_ok():
            # 打包资产缺失（v1.9.0 安装包）：回退能量 VAD，原因并入就绪提示
            self.silero_vad = False
            self.status_changed.emit(ui_text("就绪，正在聆听...（Silero VAD 组件缺失，已回退默认切句，请更新安装包）"))
        if self._stop:
            return
        self._warmup()
        # v2.6.2（P1-4）：排水式退出——_stop 置位后继续消费队列余段（停止
        # 时保留的最新段/尾段）再退；旧 while not self._stop 会让已入队的
        # 尾段滞留队列无人消费
        while True:
            try:
                item = self.queue_in.get(timeout=0.5)
            except queue.Empty:
                if self._stop:
                    break
                continue
            if item is None:
                break   # 兼容历史哨兵语义
            # v2.3.20（P26）：队元素统一 (audio, t_flush)；旧格式裸 ndarray 兜底
            if isinstance(item, tuple):
                audio, t_flush = item
            else:
                audio, t_flush = item, -1.0
            try:
                self._transcribe(audio, t_flush)
            except Exception as e:
                from app.errors import friendly_error
                from app import log as app_log
                app_log.exception("asr.transcribe_failed", e)
                self.status_changed.emit(f"{ui_text('识别异常：')}{friendly_error(e)}")

    def _warmup(self):
        # v2.0.7：复用实例只预热一次（实例标记）；停止后不再空跑——
        # 此前 warmup 不可中断，1 秒的管线停止也要等它跑完（日志实证
        # 每次 stop 都有 AsrThread 被孤儿化）
        if getattr(self._model, "_ls_warmed", False):
            return
        if self._stop:
            return
        try:
            audio = np.zeros(8000, dtype=np.float32)
            list(self._model.transcribe(audio, beam_size=1)[0])
            self._model._ls_warmed = True
        except Exception:
            pass

    def _transcribe(self, audio, t_flush=-1.0):
        duration = len(audio) / 16000.0
        kwargs = transcribe_kwargs(getattr(self, "accuracy", "fast") or "fast",
                                   bool(self.silero_vad and _silero_assets_ok()),
                                   getattr(self, "hotwords", ""))
        # Silero VAD（建议5）：faster-whisper 内置，对段内非语音再过滤一道；
        # 与能量 VAD 分工——能量 VAD 管切句，Silero 管段内净化，双保险。
        # 打包环境资产缺失时自动回退能量 VAD，不让 ONNXRuntime 报错冒给用户
        with self._lang_lock:
            lang = self._last_lang
        # v2.7.0（T5）：语言锁复检——auto+已锁定每 20 段解除语言约束重听一次。
        # 此前锁定即终身（传 language 后 whisper 概率恒为 1，conf<0.6 自愈支路
        # 永不触发），锁错语言或中途换语言的视频整场坏，仅"整段被过滤器
        # 清空"才偶然复位。复检零额外成本（语言检测本就随转写进行）。
        self._seg_count += 1
        recheck = bool(getattr(self, "lang_recheck", True)
                       and self.language == "auto" and lang
                       and self._seg_count % 20 == 0)
        if self.language != "auto":
            kwargs["language"] = self.language
            lang = self.language
        elif lang and not recheck:
            kwargs["language"] = lang

        segments, info = self._model.transcribe(audio, **kwargs)
        segs = []
        for seg in segments:
            # v2.0.1：协作取消点——segments 是惰性生成器，此前一旦开始消费
            # 就无法中断（14s 音频 CPU 大模型可达数十秒），停止超时后成僵尸线程
            if self._stop:
                return
            t = (seg.text or "").strip()
            if not t or not has_content(t):
                continue
            segs.append((t, float(getattr(seg, "avg_logprob", 0.0) or 0.0),
                         float(getattr(seg, "no_speech_prob", 0.0) or 0.0)))
        if self.hallucination_filter:
            # 幻觉抑制判据见 `hallucination_ok`（与流式预览通道共用同一个出口）
            texts = [t for (t, lp, ns) in segs if hallucination_ok(lp, ns)]
        else:
            texts = [t for (t, _lp, _ns) in segs]
        if not texts:
            if segs:
                # v2.0.8：过滤器主动丢弃不再静默——"零字幕卡聆听"的另一根因，
                # 用户有权知道段被识别了但被质量过滤掉（而不是应用没反应）
                self._filtered_streak = getattr(self, "_filtered_streak", 0) + 1
                if self._filtered_streak == 2:
                    # v2.19.2：文案诚实——旧实现在 `asr_language=auto`（出厂默认）
                    # 下会念出"当前锁定为「auto」，可在设置中改为自动检测"，
                    # 而用户本来就是自动检测、界面上也没有「auto」这个标签。
                    lang = str(self.language or "").strip()
                    if not lang or lang.lower() == "auto":
                        hint = (ui_text("识别语言＝自动检测；若内容语言固定，"
                                "在设置里手动锁定该语言可减少误听"))
                    else:
                        hint = ui_fmt("当前锁定为「{lang}」，与内容不符时"
                                      "请在设置中改为自动检测或换语言", lang=lang)
                    self.status_changed.emit(
                        ui_fmt("有语音被识别但质量过滤丢弃（可能为音乐/噪声，或识别语言与内容不符——{hint}）",
                               hint=hint))
            else:
                self._filtered_streak = 0
            if self.language == "auto":
                with self._lang_lock:
                    self._last_lang = None
            return
        self._filtered_streak = 0
        text = "".join(texts) if (info.language or "").startswith("zh") else " ".join(texts)
        text = self._postprocess(text)
        detected = info.language or ""
        conf = info.language_probability or 0.0
        if recheck:
            # v2.7.0（T5）：复检裁决——高置信且不一致才切换（保守防抖动）；
            # 低置信不一致=本段丢弃、维持原锁（错误语言解码的"流利胡话"不上屏）
            if conf >= 0.8 and detected and detected != lang:
                with self._lang_lock:
                    self._last_lang = detected
                lang = detected
                self.status_changed.emit(ui_text("语言复检：检测到说话语言变化，已切换"))
            elif detected and detected != lang:
                self.status_changed.emit(ui_text("语言复检结果不一致，本段保守丢弃，下段按原语言继续"))
                self.recheck_dropped.emit(text, duration)
                return
        if self.language == "auto" and conf < 0.6:
            with self._lang_lock:
                self._last_lang = None
            self._discard_streak += 1
            if self._discard_streak >= 3:
                self.status_changed.emit(
                    ui_text("已连续丢弃多段不确定的语音：当前模型对这段内容识别吃力，"
                    "建议在「设置 - 语音识别」换更大模型（如 small）或锁定识别语言"))
            else:
                self.status_changed.emit(ui_text("语言检测不确定已丢弃，下段重新检测；若持续偏差请锁定语言"))
            return
        if self.language == "auto":
            with self._lang_lock:
                self._last_lang = detected
        self._discard_streak = 0
        # 快语速内容一次转写可能拿到 14 秒长文，按句末标点二次切分后再上屏
        # v2.2.3：切分时把短句合并到下一句（尾句不再单独成段），字幕节奏更自然
        # v2.3.4：切分发生在内容过滤之后——切出的纯标点尾巴（实测 ".."）会漏网，
        # 逐片再过一次 has_content（CBS 新闻体验轮抓到的过滤器漏洞）
        pieces = [p for p in split_long_caption(text) if has_content(p)]
        for piece in pieces:
            self.text_ready.emit(piece, detected, f"{duration:.1f}", t_flush)


class PrewarmWorker(QThread):
    """v2.3.5（P5-A）：启动即后台预热模型——CBS 新闻实测轮抓到
    pipeline.start→model_loaded 竟需 49 秒（CUDA 上下文冷初始化），期间界面
    只有"正在加载"无解释。预热把这段等待挪到软件启动后的空闲期，用户点
    「开始翻译」时命中 _MODEL_CACHE 池秒就绪。

    安全边界：只加载**已完整下载**的模型（绝不因预热触发联网下载）；
    复用 AsrThread._load_model 同一条设备解析/回落/入池路径，保证 cache_key
    与真实管线一致；预热失败静默——真实管线会给出带原因的报错。"""

    def __init__(self, model_size: str, device: str, parent=None, turbo=False):
        super().__init__(parent)
        self.model_size = model_size
        self.device = device
        # v2.7.2：预热与真实管线必须同 compute_type 才命中同一池键——
        # 榨干模式下不带 turbo 会让预热白建 fp16 实例、首帧仍重载
        self.turbo = bool(turbo)
        self._stop = False

    def request_stop(self):
        """协作式停止（v2.6.1 P0-2）：仅在开始加载前生效。已进入
        WhisperModel 构造（阻塞在 C 扩展，GPU 冷初始化最长约 49s）时无法
        中断，由主窗口移交孤儿容器收尾（finished 后 deleteLater）。"""
        self._stop = True

    def run(self):
        from app import log as app_log
        try:
            if self._stop:
                app_log.log("asr.prewarm_stopped", model=self.model_size)
                return
            if not AsrThread.model_cached(self.model_size):
                app_log.log("asr.prewarm_skipped", model=self.model_size,
                            reason="not_cached")
                return
            app_log.log("asr.prewarm_start", model=self.model_size, device=self.device)
            t0 = time.time()
            # v2.7.4（C-2）：不再 getattr(self,"accuracy")——PrewarmWorker 无此属性
            # 恒取默认值属自欺；accuracy 只影响 transcribe_kwargs，与模型构造无关
            loader = AsrThread(self.model_size, self.device, "auto", None,
                               turbo=self.turbo)
            # v2.6.4（P2）：non-blocking 让位——真实管线正在加载时预热立即
            # 放弃（真实加载完成即入池，预热目的已达成），避免双份构造
            ok = loader._load_model(blocking=False)
            # v2.7.0（T6）：预热补完最后一公里——首次 transcribe() 触发 cuBLAS
            # 算法选择/内核载入（实测比后续慢 1~3s），此前这时间仍要用户在
            # 「开始翻译」后付；现由预热线程跑一次 0.5s 静音转写，_ls_warmed
            # 标在共享池实例上，真实管线直接复用
            if ok and not self._stop:
                loader._warmup()
            dev = getattr(loader, "_device_used", "?")
            app_log.log("asr.prewarm_done", model=self.model_size, ok=bool(ok),
                        device=dev, seconds=round(time.time() - t0, 1))
        except Exception as e:
            app_log.exception("asr.prewarm_failed", e)
