"""v2.12.0 流式原文预览通道（dual 布局的"实时不能停"核心）。

背景：正式识别按分段节奏出文本（连续语流下=分段上限周期，默认 4s）——
dual 原文区若只靠它刷新，就是"停 4 秒蹦一段"，完全不实时（用户三连
"必须实时不能停"）。本通道用**滑动窗口重识别**补上实时性：

  每 INTERVAL 秒把最近 WINDOW 秒的原始音频喂一次 whisper（beam=1 最快档，
  与正式识别共享同一模型实例），把窗口转写文本作为"草稿"发往原文区；
  主窗用"去掉与最后一片已确认文本的最长公共前缀"剥掉重复，只追加新增。

特性：
- partial 是草稿：随窗口滑动整体刷新，允许轻微改写（正式 final 到达后
  自然覆盖），不做精确时间对齐（字幕场景无必要）；
- 模型与 AsrThread 共享：CTranslate2 推理线程安全（模型只读），GPU 争用
  时本通道推理变慢、自然跳拍，不阻塞正式识别（各走各的线程）；
- 仅 dual 布局 + GPU（cuda）时启用：CPU 上 4s 音频一次推理要数秒，
  预览通道反而拖垮正式识别（启用闸门在主窗）。
"""

from collections import deque

from PySide6.QtCore import QThread, Signal

WINDOW_S = 4.0      # 预览窗口：与正式分段等长，草稿覆盖"正在说的这一段"
INTERVAL_S = 0.9    # 刷新周期：端到端原文延迟 ≈ INTERVAL + 单次推理耗时
MIN_AUDIO_S = 1.2   # 短于这个不识别（whisper 对超短音频输出噪声）


class StreamPreview(QThread):
    partial_ready = Signal(str)          # 窗口草稿文本（可能为空串=本轮无话）
    error_occurred = Signal(str)

    def __init__(self, model, language="", parent=None):
        super().__init__(parent)
        self._model = model
        self._lang = str(language or "") or None
        self._buf = deque()              # [(np.float32 16k mono, t_mono)]
        self._buf_len = 0.0
        self._stop = False

    def set_model(self, model):
        """模型就绪后注入（与 AsrThread 共享同一 WhisperModel 实例）。
        start 前未注入则循环空转等待（不识别不报错）。"""
        self._model = model

    def feed(self, audio, t_mono):
        """采集线程的原始 16k 单声道块（约 90ms 一块）。"""
        if self._stop:
            return
        self._buf.append((audio, t_mono))
        self._buf_len += len(audio) / 16000.0
        while self._buf_len > WINDOW_S + 0.5:
            old, _t = self._buf.popleft()
            self._buf_len -= len(old) / 16000.0

    def stop(self):
        self._stop = True

    def _window_audio(self):
        import numpy as np
        if not self._buf:
            return None
        return np.concatenate([a for a, _t in self._buf]).astype(np.float32)

    def run(self):
        while not self._stop:
            t0 = __import__("time").monotonic()
            try:
                audio = self._window_audio()
                dur = self._buf_len
                if audio is not None and dur >= MIN_AUDIO_S:
                    text = self._transcribe(audio)
                    self.partial_ready.emit(text)
            except Exception as e:   # 单拍失败静默跳过（下一周期即恢复）
                try:
                    from app import log as app_log
                    app_log.log("preview.round_failed", err=str(e)[:120])
                except Exception:
                    pass
            # 周期对齐：推理耗时从 INTERVAL 里扣（模型忙时自动降频）
            import time as _t
            wait = INTERVAL_S - (_t.monotonic() - t0)
            if wait > 0:
                for _ in range(int(wait / 0.1)):
                    if self._stop:
                        return
                    _t.sleep(0.1)
                _t.sleep(max(0.0, wait % 0.1))
            if self._stop:
                return

    def _transcribe(self, audio):
        """最快档转写：beam=1、无时间戳、无条件前文——只为草稿速度。"""
        if self._model is None:
            return ""
        kwargs = dict(beam_size=1, best_of=1,
                      condition_on_previous_text=False,
                      without_timestamps=True)
        if self._lang:
            kwargs["language"] = self._lang
        segments, _info = self._model.transcribe(audio, **kwargs)
        return " ".join(s.text for s in segments).strip()
