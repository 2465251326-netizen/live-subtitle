import queue
import re
import threading

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
        return HF_HOME / "hub" / ("models--Systran--faster-whisper-" + model_size)

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
    def remove_model(model_size: str) -> bool:
        """删除已下载的识别模型缓存目录；返回是否删除了内容。"""
        import shutil
        d = AsrThread.model_cache_dir(model_size)
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
            return True
        return False

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
        try:
            while self.queue_in.qsize() >= 3:
                try:
                    self.queue_in.get_nowait()
                except queue.Empty:
                    break
            self.queue_in.put_nowait(audio)
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
        from faster_whisper import WhisperModel
        device = self.device if self.device in ("cpu", "cuda") else "auto"
        compute_type = "int8" if device in ("cpu", "auto") else "float16"
        import time as _time
        t0 = _time.perf_counter()
        try:
            self._model = WhisperModel(
                self.model_size,
                device=device,
                compute_type=compute_type,
                download_root=str(HF_HOME / "hub"),
                # 缓存完整时离线加载：跳过联网校验，避免代理抖动时卡在「正在加载模型」
                local_files_only=cached,
            )
            app_log.log("asr.model_loaded", model=self.model_size, device=device,
                        cached=cached, seconds=round(_time.perf_counter() - t0, 2))
            self._device_used = device
            return True
        except Exception as e:
            if device == "auto":
                try:
                    self._model = WhisperModel(self.model_size, device="cpu", compute_type="int8")
                    self._device_used = "cpu"
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
        if self.model_cached(self.model_size):
            self.status_changed.emit(f"正在加载 {self.model_size} 模型（本地缓存，CPU 上通常需几秒到几十秒）...")
        else:
            self.status_changed.emit(f"正在准备 {self.model_size} 模型（首次运行会自动下载，见状态栏进度）...")
        if not self._load_model():
            return
        if self._stop:
            # 加载期间用户已按停止：直接退出，不再报“就绪”
            return
        self.model_ready.emit()
        dev = getattr(self, "_device_used", "cpu")
        if dev == "cuda":
            self.status_changed.emit("就绪，正在聆听...（GPU · CUDA 加速已生效）")
        else:
            self.status_changed.emit("就绪，正在聆听...（CPU 模式）")
        if self.silero_vad and not _silero_assets_ok():
            # 打包资产缺失（v1.9.0 安装包）：回退能量 VAD，原因并入就绪提示
            self.silero_vad = False
            self.status_changed.emit("就绪，正在聆听...（Silero VAD 组件缺失，已回退默认切句，请更新安装包）")
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
        try:
            audio = np.zeros(8000, dtype=np.float32)
            list(self._model.transcribe(audio, beam_size=1)[0])
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
            t = (seg.text or "").strip()
            if not t:
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
            if self.language == "auto":
                with self._lang_lock:
                    self._last_lang = None
            return
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
        for piece in split_long_caption(text):
            self.text_ready.emit(piece, detected, f"{duration:.1f}")
