import queue
import threading

import numpy as np
from PySide6.QtCore import QThread, Signal


class AsrThread(QThread):
    text_ready = Signal(str, str, str)  # text, whisper_lang, duration
    status_changed = Signal(str)
    model_ready = Signal()
    error_occurred = Signal(str)

    def __init__(self, model_size: str, device: str, language: str, parent=None):
        super().__init__(parent)
        self.model_size = model_size
        self.device = device
        self.language = language
        self.queue_in: "queue.Queue[object]" = queue.Queue()
        self._stop = False
        self._model = None
        self._lang_lock = threading.Lock()
        self._last_lang = language if language != "auto" else None

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
        from app.config import ensure_hf_endpoint_ready
        ensure_hf_endpoint_ready()
        from faster_whisper import WhisperModel
        device = self.device if self.device in ("cpu", "cuda") else "auto"
        compute_type = "int8" if device in ("cpu", "auto") else "float16"
        try:
            self._model = WhisperModel(
                self.model_size,
                device=device,
                compute_type=compute_type,
                download_root=None,
            )
            return True
        except Exception as e:
            if device == "auto":
                try:
                    self._model = WhisperModel(self.model_size, device="cpu", compute_type="int8")
                    return True
                except Exception:
                    pass
            self.error_occurred.emit(f"模型加载失败: {e}")
            return False

    def run(self):
        self.status_changed.emit("正在加载语音识别模型（首次运行会自动下载）...")
        if not self._load_model():
            return
        self.model_ready.emit()
        self.status_changed.emit("就绪，正在聆听...")
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
                self.status_changed.emit(f"识别异常: {e}")

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
            vad_filter=False,
            no_speech_threshold=0.6,
            log_prob_threshold=-1.0,
        )
        with self._lang_lock:
            lang = self._last_lang
        if self.language != "auto":
            kwargs["language"] = self.language
            lang = self.language
        elif lang:
            kwargs["language"] = lang

        segments, info = self._model.transcribe(audio, **kwargs)
        texts = [seg.text.strip() for seg in segments if seg.text and seg.text.strip()]
        if not texts:
            if self.language == "auto":
                with self._lang_lock:
                    self._last_lang = None
            return
        text = "".join(texts) if (info.language or "").startswith("zh") else " ".join(texts)
        detected = info.language or ""
        conf = info.language_probability or 0.0
        if self.language == "auto" and conf < 0.6:
            with self._lang_lock:
                self._last_lang = None
            self.status_changed.emit("语言检测不确定已丢弃，下段重新检测；若持续偏差请锁定语言")
            return
        if self.language == "auto":
            with self._lang_lock:
                self._last_lang = detected
        self.text_ready.emit(text, detected, f"{duration:.1f}")
