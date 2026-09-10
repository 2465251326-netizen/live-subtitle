import queue
import re
import threading
import time

import numpy as np
from PySide6.QtCore import QThread, Signal


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
    parts = [p.strip() for p in re.split(r"(?<=[.!?。！？；;])\s*", text) if p.strip()]
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
    from app.config import HF_HOME
    repo = model_repo_id(model_size)
    # v2.1.0：经 _hf_call 包装——默认带本机凭据（有效令牌享更高速率、
    # 消除"未认证请求"警告），401/403 自动退回匿名（v2.0.9 场景）
    files = _hf_call(list_repo_files, repo)
    total = len(files)
    for i, name in enumerate(files, 1):
        if should_stop is not None and should_stop():
            return "stopped"
        _hf_call(hf_hub_download, repo, filename=name,
                 cache_dir=str(HF_HOME / "hub"))
        if progress is not None:
            try:
                progress(i, total, name)
            except Exception:
                pass
    return "done"


# v2.0.6：进程内模型实例缓存（容量 1）——切输入来源/改识别设置重启管线
# 不再全量重载模型（CPU 上数秒到数十秒）。键 = (model_size, device,
# compute_type)；换模型/设备时旧实例被替换、由 GC 释放显存/内存。
_MODEL_CACHE = {}
_MODEL_CACHE_LOCK = threading.Lock()


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


class AsrThread(QThread):
    text_ready = Signal(str, str, str)  # text, whisper_lang, duration
    status_changed = Signal(str)
    model_ready = Signal()
    error_occurred = Signal(str)

    def __init__(self, model_size: str, device: str, language: str, parent=None,
                 hallucination_filter=True, silero_vad=False, mishear_map=None):
        super().__init__(parent)
        self.model_size = model_size
        self.device = device
        self.language = language
        self.hallucination_filter = bool(hallucination_filter)
        self.silero_vad = bool(silero_vad)
        self.mishear_map = dict(mishear_map or {})
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

    def _postprocess(self, text):
        """识别后处理（建议5）：可选的常见误听修正词典（精确子串替换）。"""
        if not self.mishear_map:
            return text
        for wrong, right in self.mishear_map.items():
            if wrong:
                text = text.replace(wrong, right)
        return text

    @staticmethod
    def model_cache_dir(model_size: str):
        from app.config import HF_HOME
        from app.asr.engine import model_repo_id
        # v2.0.9：缓存目录名跟随真实仓库 ID（large-v3-turbo 的权重在
        # mobiuslabsgmbh 仓库，目录名不再硬编码 Systran 前缀）
        return HF_HOME / "hub" / ("models--" + model_repo_id(model_size).replace("/", "--"))

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
        self._stop = True
        try:
            while True:
                self.queue_in.get_nowait()
        except queue.Empty:
            pass
        try:
            self.queue_in.put_nowait(None)
        except Exception:
            pass

    def submit(self, audio):
        """语音段入队。

        v2.0.8 重新设计背压：medium CPU 上一段转写 ~10s，快语速内容
        30s 就能切出 5+ 段，旧"队列 ≥3 丢最旧"会把大多数段静默丢掉
        （实测 8 段提交 0 条字幕，用户观感=完全没反应）。
        现在：上限放宽到 8 段（约 2 分钟语音），只在真实超限时丢最旧；
        丢段/积压状态只发一次，避免刷屏。
        """
        try:
            dropped = False
            while self.queue_in.qsize() >= self.QUEUE_LIMIT:
                try:
                    self.queue_in.get_nowait()
                    dropped = True
                except queue.Empty:
                    break
            self.queue_in.put_nowait(audio)
            if dropped:
                self._dropped_ever = True
                if not self._backlog_reported:
                    self._backlog_reported = True
                    self.status_changed.emit(
                        "识别积压，部分较早语音来不及转写已被跳过："
                        "CPU 跟不上当前模型，建议换 small/tiny（状态栏持续提醒）")
                from app import log as app_log
                app_log.log("asr.segment_dropped", queue=self.queue_in.qsize())
            elif self.queue_in.qsize() >= self.QUEUE_LIMIT - 2 and not self._backlog_reported:
                self._backlog_reported = True
                self.status_changed.emit(
                    "识别积压：转写速度跟不上语音产出，字幕会延迟陆续出现"
                    "（CPU 较慢建议换 small/tiny）")
        except Exception:
            pass

    def _load_model(self):
        if self._model is not None:
            return True
        from app.config import HF_HOME
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
                        self.status_changed.emit("已停止模型下载（已下载部分保留，下次继续）")
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
        compute_type = "int8" if device == "cpu" else "float16"
        # v2.0.6：进程内实例复用——同一 (模型, 设备, 量化) 在池中直接取用，
        # 切输入来源/改识别设置重启管线不再全量重载（CPU 上数秒到数十秒）。
        # 池容量 1，换模型/换设备时旧实例被替换由 GC 释放
        cache_key = (self.model_size, device, compute_type)
        with _MODEL_CACHE_LOCK:
            pooled = _MODEL_CACHE.get(cache_key)
        if pooled is not None:
            self._model = pooled
            self._device_used = getattr(pooled, "_ls_device", device)
            app_log.log("asr.model_reused", model=self.model_size,
                        device=self._device_used)
            return True
        from faster_whisper import WhisperModel
        import time as _time
        # v2.0.9：large-v3-turbo 传完整 HF 仓库 ID（faster-whisper 支持任意
        # CT2 模型 ID），否则它硬编码拼 Systran 仓库必 404
        model_ref = model_repo_id(self.model_size)
        t0 = _time.perf_counter()
        try:
            self._model = WhisperModel(
                model_ref,
                device=device,
                compute_type=compute_type,
                download_root=str(HF_HOME / "hub"),
                # 缓存完整时离线加载：跳过联网校验，避免代理抖动时卡在「正在加载模型」
                local_files_only=cached,
            )
            # v2.2.1：构造完成后先查 _stop——加载期停止的孤儿线程构造虽已完成，
            # 但不再入池/复用（否则①停止后 RAM/显存被占住直到进程退出，
            # ②与重启线程并发构造时败者覆盖胜者的池缓存）
            if self._stop:
                self._model = None
                return False
            self._model._ls_device = device
            with _MODEL_CACHE_LOCK:
                _MODEL_CACHE.clear()
                _MODEL_CACHE[cache_key] = self._model
            app_log.log("asr.model_loaded", model=self.model_size, device=device,
                        cached=cached, seconds=round(_time.perf_counter() - t0, 2))
            self._device_used = device
            return True
        except Exception as e:
            if device == "cuda":
                # v2.0.11：显式 CUDA 加载失败（缺运行时等）回落 CPU，不再依赖 auto 分支
                try:
                    self._model = WhisperModel(model_ref, device="cpu", compute_type="int8")
                    # v2.2.1：与主路径同款 _stop 后置检查（构造期可能被停止）
                    if self._stop:
                        self._model = None
                        return False
                    self._model._ls_device = "cpu"
                    with _MODEL_CACHE_LOCK:
                        _MODEL_CACHE.clear()
                        _MODEL_CACHE[(self.model_size, "cpu", "int8")] = self._model
                    self._device_used = "cpu"
                    app_log.log("asr.cuda_fallback_cpu", model=self.model_size, err=str(e)[:120])
                    return True
                except Exception:
                    pass
            self._device_used = "cpu"
            from app.errors import friendly_error
            from app import log as app_log
            app_log.exception("asr.model_load_failed", e, model=self.model_size)
            self.error_occurred.emit(f"模型加载失败：{friendly_error(e)}")
            return False

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
                        f"正在加载 {self.model_size} 模型（GPU 首次初始化约 1 分钟，"
                        "仅第一次；之后秒开，可在「设置-语音识别」开启启动预热）...")
                else:
                    self.status_changed.emit(f"正在加载 {self.model_size} 模型（本地缓存，CPU 上通常需几秒到几十秒）...")
            else:
                self.status_changed.emit(f"正在准备 {self.model_size} 模型（首次运行会自动下载，见状态栏进度）...")
            if not self._load_model():
                return
        except Exception as e:
            from app.errors import friendly_error
            from app import log as app_log
            app_log.exception("asr.setup_failed", e)
            self.error_occurred.emit(f"识别引擎启动失败：{friendly_error(e)}")
            return
        if self._stop:
            # 加载期间用户已按停止：直接退出，不再报“就绪”
            return
        self.model_ready.emit()
        dev = getattr(self, "_device_used", "cpu")
        if dev == "cuda":
            self.status_changed.emit("就绪，正在聆听...（GPU · CUDA 加速已生效）")
        elif dev == "cpu" and self.device == "cuda":
            # v2.1.1：显式"强制 GPU"回落 CPU 时明确告知原因与出路
            self.status_changed.emit("就绪，正在聆听...（CPU 模式 · 强制 GPU 不可用已回落："
                                     "请先「检测 GPU 环境」并安装 CUDA 版 PyTorch）")
        elif dev == "cpu" and self.device == "auto":
            # v2.0.11：auto 明确回落为 CPU 时如实告知（此前 auto 显示与
            # 显式 CPU 无差别，用户不知道 GPU 没用上）
            self.status_changed.emit("就绪，正在聆听...（CPU 模式 · auto 未启用 GPU："
                                     "如需加速请显式选 cuda 并安装 CUDA 版 PyTorch，见 GPU 检测引导）")
        else:
            self.status_changed.emit("就绪，正在聆听...（CPU 模式）")
        if self.silero_vad and not _silero_assets_ok():
            # 打包资产缺失（v1.9.0 安装包）：回退能量 VAD，原因并入就绪提示
            self.silero_vad = False
            self.status_changed.emit("就绪，正在聆听...（Silero VAD 组件缺失，已回退默认切句，请更新安装包）")
        if self._stop:
            return
        self._warmup()
        while not self._stop:
            try:
                audio = self.queue_in.get(timeout=0.5)
            except queue.Empty:
                continue
            if audio is None:
                break
            try:
                self._transcribe(audio)
            except Exception as e:
                from app.errors import friendly_error
                from app import log as app_log
                app_log.exception("asr.transcribe_failed", e)
                self.status_changed.emit(f"识别异常：{friendly_error(e)}")

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

    def _transcribe(self, audio):
        duration = len(audio) / 16000.0
        kwargs = dict(
            beam_size=1,
            best_of=1,
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
            log_prob_threshold=-1.0,
        )
        # Silero VAD（建议5）：faster-whisper 内置，对段内非语音再过滤一道；
        # 与能量 VAD 分工——能量 VAD 管切句，Silero 管段内净化，双保险。
        # 打包环境资产缺失时自动回退能量 VAD，不让 ONNXRuntime 报错冒给用户
        if self.silero_vad and _silero_assets_ok():
            kwargs["vad_filter"] = True
            kwargs["vad_parameters"] = {"min_silence_duration_ms": 300}
        with self._lang_lock:
            lang = self._last_lang
        if self.language != "auto":
            kwargs["language"] = self.language
            lang = self.language
        elif lang:
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
            # 幻觉抑制：音乐/噪声段常见特征是"高置信度胡言"或"无语音概率高+低置信度"，
            # 两者命中其一即丢弃（阈值偏保守，宁可少出一条也不出乱码字幕）
            texts = [t for (t, lp, ns) in segs
                     if lp >= -1.2 and not (ns > 0.8 and lp < -0.5)]
        else:
            texts = [t for (t, _lp, _ns) in segs]
        if not texts:
            if segs:
                # v2.0.8：过滤器主动丢弃不再静默——"零字幕卡聆听"的另一根因，
                # 用户有权知道段被识别了但被质量过滤掉（而不是应用没反应）
                self._filtered_streak = getattr(self, "_filtered_streak", 0) + 1
                if self._filtered_streak == 2:
                    self.status_changed.emit(
                        "有语音被识别但质量过滤丢弃（可能为音乐/噪声，或识别语言与内容不符——"
                        "当前锁定为「" + str(self.language) + "」，可在设置中改为自动检测）")
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
        if self.language == "auto" and conf < 0.6:
            with self._lang_lock:
                self._last_lang = None
            self._discard_streak += 1
            if self._discard_streak >= 3:
                self.status_changed.emit(
                    "已连续丢弃多段不确定的语音：当前模型对这段内容识别吃力，"
                    "建议在「设置 - 语音识别」换更大模型（如 small）或锁定识别语言")
            else:
                self.status_changed.emit("语言检测不确定已丢弃，下段重新检测；若持续偏差请锁定语言")
            return
        if self.language == "auto":
            with self._lang_lock:
                self._last_lang = detected
        self._discard_streak = 0
        # 快语速内容一次转写可能拿到 14 秒长文，按句末标点二次切分后再上屏
        # v2.2.3：切分时把短句合并到下一句（尾句不再单独成段），字幕节奏更自然
        # v2.3.4：切分发生在内容过滤之后——切出的纯标点尾巴（实测 ".."）会漏网，
        # 逐片再过一次 has_content（CBS 新闻体验轮抓到的过滤器漏洞）
        for piece in split_long_caption(text):
            if has_content(piece):
                self.text_ready.emit(piece, detected, f"{duration:.1f}")


class PrewarmWorker(QThread):
    """v2.3.5（P5-A）：启动即后台预热模型——CBS 新闻实测轮抓到
    pipeline.start→model_loaded 竟需 49 秒（CUDA 上下文冷初始化），期间界面
    只有"正在加载"无解释。预热把这段等待挪到软件启动后的空闲期，用户点
    「开始翻译」时命中 _MODEL_CACHE 池秒就绪。

    安全边界：只加载**已完整下载**的模型（绝不因预热触发联网下载）；
    复用 AsrThread._load_model 同一条设备解析/回落/入池路径，保证 cache_key
    与真实管线一致；预热失败静默——真实管线会给出带原因的报错。"""

    def __init__(self, model_size: str, device: str, parent=None):
        super().__init__(parent)
        self.model_size = model_size
        self.device = device

    def run(self):
        from app import log as app_log
        try:
            if not AsrThread.model_cached(self.model_size):
                app_log.log("asr.prewarm_skipped", model=self.model_size,
                            reason="not_cached")
                return
            app_log.log("asr.prewarm_start", model=self.model_size, device=self.device)
            t0 = time.time()
            loader = AsrThread(self.model_size, self.device, "auto", None)
            ok = loader._load_model()
            dev = getattr(loader, "_device_used", "?")
            app_log.log("asr.prewarm_done", model=self.model_size, ok=bool(ok),
                        device=dev, seconds=round(time.time() - t0, 1))
        except Exception as e:
            app_log.exception("asr.prewarm_failed", e)
