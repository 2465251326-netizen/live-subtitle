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

from app.i18n import ui_text

from collections import deque

from PySide6.QtCore import QThread, Signal

WINDOW_S = 4.0      # 预览窗口：与正式分段等长，草稿覆盖"正在说的这一段"
INTERVAL_S = 0.9    # 刷新周期：端到端原文延迟 ≈ INTERVAL + 单次推理耗时
MIN_AUDIO_S = 1.2   # 短于这个不识别（whisper 对超短音频输出噪声）


def normalize_language(value):
    """识别语言配置值 → 转写参数（纯函数，便于回归锁）。

    "auto"（**出厂默认值**）与空串一律归一为 None——faster-whisper 只在
    language is None 时才做自动检测，把字符串 "auto" 直接喂给 transcribe
    会在 Tokenizer 构造处抛 `ValueError: 'auto' is not a valid language
    code`，而本文件的每拍 try 会把它整拍静默吞掉：现象=开了"流式原文"
    却永远不出草稿、界面无任何报错（只在日志刷 preview.round_failed）。
    正式识别通道从一开始就按同一规约处理（asr/engine.py 在 auto 时
    根本不传 language 参数），此处补齐，两轨对齐。"""
    v = str(value or "").strip()
    return None if v.lower() in ("", "auto") else v


class StreamPreview(QThread):
    partial_ready = Signal(str)          # 窗口草稿文本（可能为空串=本轮无话）
    error_occurred = Signal(str)

    def __init__(self, model, language="", parent=None):
        super().__init__(parent)
        self._model = model
        self._lang = normalize_language(language)
        self._buf = deque()              # [(np.float32 16k mono, t_mono)]
        self._buf_len = 0.0
        self._stop = False
        self._fail_streak = 0            # 连续失败拍数（整条通道哑火的一次性告警）
        # v2.13.0：节拍遥测（每 20 拍汇总一条）——排查"GPU 分时排队拖慢
        # 预览节奏"时，间隔均值 vs INTERVAL_S、推理均值一眼可辨
        self._beat_n = 0
        self._beat_interval_sum = 0.0
        self._beat_infer_sum = 0.0
        self._beat_last_t = None

    def set_model(self, model):
        """模型就绪后注入（与 AsrThread 共享同一 WhisperModel 实例）。
        start 前未注入则循环空转等待（不识别不报错）。"""
        self._model = model

    def restart(self):
        """v2.13.0：stop 后热重启（中途切回 dual 布局等场景）——重置停止
        标志与音频缓冲（陈旧窗口不该混进新布局的第一拍草稿）。"""
        self._stop = False
        self._buf.clear()
        self._buf_len = 0.0
        self._beat_last_t = None
        self._fail_streak = 0

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
        import time as _t
        while not self._stop:
            t0 = _t.monotonic()
            # 节拍遥测：实际间隔（含上一拍推理+睡眠）
            if self._beat_last_t is not None:
                self._beat_interval_sum += t0 - self._beat_last_t
            self._beat_last_t = t0
            infer_s = 0.0
            try:
                audio = self._window_audio()
                dur = self._buf_len
                if audio is not None and dur >= MIN_AUDIO_S:
                    ti = _t.monotonic()
                    text = self._transcribe(audio)
                    infer_s = _t.monotonic() - ti
                    self._fail_streak = 0
                    self.partial_ready.emit(text)
            except Exception as e:   # 单拍失败静默跳过（下一周期即恢复）
                # v2.18.2：整条通道哑火不再无声——首拍记一条，连续 3 拍升级
                # 一条 degraded 并停止刷屏（此前每拍一条同内容 error，等于
                # 把"功能整体失效"混在噪音里，实测该模式下 2 分钟可刷 130+ 条）
                self._fail_streak += 1
                try:
                    from app import log as app_log
                    if self._fail_streak == 1:
                        app_log.log("preview.round_failed", err=str(e)[:120])
                    elif self._fail_streak == 3:
                        app_log.log("preview.degraded",
                                    consecutive=self._fail_streak,
                                    language=self._lang or "auto-detect",
                                    hint=ui_text("流式原文连续失败，通道可能整体不可用"),
                                    err=str(e)[:120])
                except Exception:
                    pass
            self._beat_n += 1
            self._beat_infer_sum += infer_s
            if self._beat_n >= 20:
                try:
                    from app import log as app_log
                    app_log.log("preview.beat", n=self._beat_n,
                                interval_avg=round(self._beat_interval_sum / max(1, self._beat_n - 1), 2),
                                infer_avg=round(self._beat_infer_sum / self._beat_n, 2))
                except Exception:
                    pass
                self._beat_n = 0
                self._beat_interval_sum = 0.0
                self._beat_infer_sum = 0.0
            # 周期对齐：推理耗时从 INTERVAL 里扣（模型忙时自动降频）
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
