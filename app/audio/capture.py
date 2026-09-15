import queue
import time

import numpy as np
from PySide6.QtCore import QThread, Signal

TARGET_SR = 16000
CHUNK_MS = 30
SILENCE_END_S = 0.45
MIN_SPEECH_S = 0.7
MAX_SEGMENT_S = 14.0
HANGOVER_S = 0.12


def list_input_devices():
    devices = []
    try:
        import pyaudiowpatch as pyaudio
    except ImportError:
        try:
            import pyaudio
        except ImportError:
            return devices
    p = None
    try:
        p = pyaudio.PyAudio()
        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            if info.get("maxInputChannels", 0) > 0:
                devices.append({
                    "index": i,
                    "name": info["name"],
                    "channels": info["maxInputChannels"],
                    "rate": int(info.get("defaultSampleRate", 44100)),
                    "loopback": bool(info.get("isLoopbackDevice", False)),
                })
    except Exception:
        pass
    finally:
        if p:
            try:
                p.terminate()
            except Exception:
                pass
    return devices


def list_output_devices():
    return [d for d in list_input_devices() if d.get("loopback")]


def friendly_audio_error(e: Exception) -> str:
    """把 pyaudio 的英文错误码翻译成用户能执行的下一步动作。"""
    s = str(e)
    if "-9996" in s or "Invalid device" in s:
        return ("无法打开所选音频设备（该设备可能不支持当前采集模式）。"
                "请到「设置 - 音频输入」重新选择设备，或点「刷新」后重试。")
    if "-9985" in s or "Device unavailable" in s:
        return "音频设备被其他程序占用或暂时不可用，请关闭占用它的程序后重试。"
    if "-9984" in s or "unanticipated host error" in s.lower():
        return "音频驱动异常，请尝试更换音频设备或重启程序。"
    return s


_FIR_TAPS = 63


def _lowpass_coeffs(fc_cycles, taps=_FIR_TAPS):
    """加窗（汉宁）sinc 低通系数。fc_cycles 为截止频率/原采样率
    （cycles/sample，即 H(f)=rect 的半宽 W；如 48k→16k 传 7600/48000）。

    v2.0.6：此前 48k/44.1k→16k 直接 np.interp，>8kHz 的分量（音乐/高频
    噪声）会混叠进语音带拉低识别率；抽取前先做一次抗混叠低通。
    系数按 orig_sr 缓存，30ms 块上卷积开销可忽略。
    """
    n = np.arange(taps, dtype=np.float64) - (taps - 1) / 2.0
    h = np.sinc(2.0 * fc_cycles * n) * np.hanning(taps)
    s = float(np.sum(h))
    return (h / s).astype(np.float32) if s != 0 else h.astype(np.float32)


_FIR_CACHE = {}
# v2.2.1：跨块有状态滤波的尾部缓存（键=orig_sr，值=上块尾部 taps-1 采样）。
# 逐块独立 convolve(mode="same") 会在每块首尾补零造成 0.65ms 边缘衰减，
# 30ms 块拼接后形成 33Hz 周期性调制；有状态卷积保持信号连续性。
_STREAM_TAILS = {}


def _anti_alias_filter(mono: np.ndarray, orig_sr: int, carry_key=None) -> np.ndarray:
    """降采样路径的抗混叠低通（跨块有状态）。

    carry_key 不为 None 时启用跨块尾部衔接：把上一块的尾部样本拼进本块
    卷积，再把本块尾部存回缓存——块边界不再有补零造成的调制。
    """
    key = orig_sr
    h = _FIR_CACHE.get(key)
    if h is None:
        h = _lowpass_coeffs(0.475 * TARGET_SR / orig_sr)
        _FIR_CACHE[key] = h
    pad = len(h) - 1
    x = np.asarray(mono, dtype=np.float32)
    tail = None
    if carry_key is not None:
        tail = _STREAM_TAILS.get(carry_key)
        if tail is not None and len(tail):
            x = np.concatenate([tail, x])
    if len(x) <= pad:
        # 块太短不足以卷积：全部存为尾部，返回空（正常 30ms 块不会走到）
        if carry_key is not None:
            _STREAM_TAILS[carry_key] = x
        return np.zeros(0, dtype=np.float32)
    y = np.convolve(x, h, mode="valid")
    if carry_key is not None:
        _STREAM_TAILS[carry_key] = x[-pad:].copy()
    else:
        _STREAM_TAILS.pop(carry_key, None)
    return y.astype(np.float32)


def resample_to_16k(data: np.ndarray, orig_sr: int, carry_key=None) -> np.ndarray:
    """重采样到 16k。carry_key 提供时启用跨块有状态滤波（连续流场景），
    不提供时按独立块处理（测试/基准/一次性调用场景，v2.1.3 行为）。"""
    if data.ndim == 1:
        mono = data
    else:
        mono = data.mean(axis=1)
    if orig_sr == TARGET_SR:
        return mono.astype(np.float32)
    duration = mono.shape[0] / orig_sr
    target_len = int(duration * TARGET_SR)
    if target_len < 1:
        return np.zeros(0, dtype=np.float32)
    if orig_sr > TARGET_SR:
        mono = _anti_alias_filter(mono, orig_sr, carry_key=carry_key)
        if mono.shape[0] == 0:
            return np.zeros(0, dtype=np.float32)
    x_old = np.linspace(0.0, mono.shape[0] / orig_sr, num=mono.shape[0], endpoint=False)
    x_new = np.linspace(0.0, mono.shape[0] / orig_sr, num=target_len, endpoint=False)
    return np.interp(x_new, x_old, mono).astype(np.float32)


class Segmenter:
    def __init__(self, low_latency=False, turbo=False, cap_s=0.0):
        self.buffer = []
        self.buffer_len = 0.0
        self.speech_len = 0.0
        self.noise_floor = 0.005
        self.in_speech = False
        self.silence_run = 0.0
        self.speech_run = 0.0
        # v2.3.3（P1，模拟用户痛点"字幕滞后半分钟"）：低延迟模式——
        # 分段上限 14s→6s、静音判停 0.45s→0.30s、自适应区间收紧 0.20~0.40s。
        # 代价：句子可能切短、译文上下文变少，故为设置开关、默认关。
        self.low_latency = bool(low_latency)
        # v2.7.2：榨干模式——连续说话的强制切段上限 6s→4s（没人停顿也 4s 必交付一片，
        # 代价=句子更易被腰斩，配合提前冲地板 1.2s 使用）
        # v2.7.6（C）：分段上限改为可独立调节（segment_cap_s>0 时覆盖模式内置值）。
        # 遥测实锤：hold_p50 恒等于分段周期（turbo 关=6.04s / turbo 开=4.03~4.43s），
        # 因为连续语流中"本句译文由下一片到达冲刷"，下一片到达间隔=分段周期。
        # 默认 2.5s——有推测式增量翻译（A）兜底后，切短造成的半截译文会被整句
        # 终版原地覆盖，质量损失不再由用户承担。
        builtin = (4.0 if turbo else 6.0) if self.low_latency else MAX_SEGMENT_S
        try:
            cap = float(cap_s or 0.0)
        except (TypeError, ValueError):
            cap = 0.0
        self.max_seg = cap if cap > 0.05 else builtin
        self.sil_lo = 0.20 if self.low_latency else 0.30
        self.sil_hi = 0.40 if self.low_latency else 0.60
        # 自适应切句（建议2）：按语速在 [sil_lo, sil_hi] 间动态调整静音判停
        self.silence_end = 0.30 if self.low_latency else SILENCE_END_S

    def _adapt_silence_end(self, spoken, buffered):
        """按刚完成段的语速（有声占比）调整判停等待。

        快语速（新闻/辩论，占比高）→ 缩短等待，字幕更跟手；
        慢语速/停顿多 → 放宽等待，避免把一句话切碎。
        """
        if buffered <= 0.05:
            return
        density = spoken / buffered
        if density > 0.80:
            self.silence_end = max(self.sil_lo, self.silence_end - 0.03)
        elif density < 0.35:
            self.silence_end = min(self.sil_hi, self.silence_end + 0.03)

    def _rms(self, chunk: np.ndarray) -> float:
        if chunk.size == 0:
            return 0.0
        return float(np.sqrt(np.mean(np.square(chunk))))

    def feed(self, chunk: np.ndarray, voiced_override=None):
        """喂入一块 16k 单声道音频，切出完整语音段时返回 ndarray，否则 None。

        voiced_override（v2.7.6 实现，v2.8.0 回退，v2.9.0 按用户要求恢复为
        **默认关的实验开关**）：外部神经 VAD 给出的"这块是否人声"判定，
        None=不可用、走能量判据（默认路径，行为与历代版本逐字一致）。
        噪声底仍由能量维护（电平显示与低输入告警依赖它），只有 voiced
        判决在提供时被神经结果接管。

        实测留档（为什么默认关，勿再当成免费午餐）：短句素材（15 句 × 1~2s、
        句间 0.95s 静音）叠 rms 0.03 持续背景乐，能量判据本就 15 句切 15 段、
        零硬切、段长中位 1.50s——自适应噪声底（上限 0.02 → 阈值
        max(noise_floor*3, 0.004) 最高 0.06）压得住稳定背景乐；Silero 神经
        判定（滞回 0.50 进/0.35 出）在同素材反而多抱 0.66s 尾音与背景乐
        （段长中位 2.16s）。且 hold≈分段上限的真因是句长超过上限被强制切段，
        不是找不到停顿（治它的是推测式增量翻译）。保留本开关供**突发强背景乐/
        噪声**场景试验——能量判据在那种场景确实可能被骗；改完务必真机 A/B。
        锁测试：test_energy_vad_beats_steady_bgm（能量判据基线）+
        test_segmenter_voiced_override / test_neural_vad_hysteresis_and_degrade。"""
        duration = chunk.shape[0] / TARGET_SR
        rms = self._rms(chunk)
        if rms < self.noise_floor:
            self.noise_floor = max(rms, self.noise_floor * 0.98)
        else:
            self.noise_floor = min(self.noise_floor + (rms - self.noise_floor) * 0.01, 0.02)
        threshold = max(self.noise_floor * 3.0, 0.004)
        if voiced_override is None:
            voiced = rms > threshold
        else:
            voiced = bool(voiced_override)

        if voiced:
            self.silence_run = 0.0
            self.speech_run += duration
            self.speech_len += duration
            self.buffer.append(chunk)
            self.buffer_len += duration
            if not self.in_speech and self.speech_run > HANGOVER_S:
                self.in_speech = True
            if self.in_speech and self.buffer_len >= self.max_seg:
                return self._flush()
        else:
            self.silence_run += duration
            self.speech_run = max(0.0, self.speech_run - duration * 0.5)
            if self.in_speech:
                self.buffer.append(chunk)
                self.buffer_len += duration
                if self.silence_run >= self.silence_end:
                    return self._flush()
            elif self.buffer and self.silence_run > self.silence_end:
                self._reset()
        return None

    def flush(self):
        """强制产出缓冲中的语音段（v2.0.1：供停止采集时补发最后一句）。"""
        return self._flush()

    def _flush(self):
        audio = np.concatenate(self.buffer) if self.buffer else np.zeros(0, dtype=np.float32)
        spoken = self.speech_len
        buffered = self.buffer_len
        self._reset()
        if spoken < MIN_SPEECH_S:
            return None
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        if peak < 0.002:
            return None
        self._adapt_silence_end(spoken, buffered)
        return audio

    def _reset(self):
        self.buffer = []
        self.buffer_len = 0.0
        self.speech_len = 0.0
        self.in_speech = False
        self.silence_run = 0.0
        self.speech_run = 0.0


class CaptureThread(QThread):
    segment_ready = Signal(object)
    # v2.3.16（P21）信号契约：level_changed 发的是 0~1 **比例**
    # （min(1.0, peak*8)），不是百分数！消费方换算/阈值按此写——
    # P19 曾因 `_on_level` 按 0~100 理解（value>=3 恒假）废掉整条电平守卫。
    level_changed = Signal(float)
    error_occurred = Signal(str)
    low_input = Signal(bool)  # True=输入信号持续过弱（可能音量过低/抓错设备）
    muted = Signal(bool)      # True=系统处于静音状态（补充5：静音盲区提示）
    # v2.12.0：原始 16k 单声道旁路（90ms 聚合一块，(ndarray, t_mono)）——
    # 供流式预览通道（StreamPreview）喂滑动窗口重识别。仅 dual+流式开启时
    # 由主窗连接；tap_enabled=False 时不发射（零开销）。
    raw_chunk = Signal(object, float)

    QUIET_WARN_S = 12.0       # 单位：秒
    QUIET_LEVEL = 0.012       # 单位：原始峰值幅度 0~1（与发出比例同量纲，8 倍增益前）

    def __init__(self, source_type: str, device_index: int, parent=None,
                 device_name: str = "", low_latency: bool = False, turbo: bool = False,
                 cap_s=0.0, neural_vad: bool = False, tap_enabled: bool = False):
        super().__init__(parent)
        self.source_type = source_type
        self.device_index = device_index
        self.device_name = str(device_name or "")
        self._stop = False
        # v2.7.6（C）：cap_s>0 覆盖模式内置分段上限
        self.segmenter = Segmenter(low_latency=low_latency, turbo=turbo, cap_s=cap_s)
        self._warned_quiet = False
        self._tail_seg = None   # v2.6.2（P1-4）：停止 flush 尾段暂存
        # v2.12.0：原始音频旁路（流式预览通道的进料口）
        self._tap_enabled = bool(tap_enabled)
        self._tap_buf = None
        self._tap_blocks = 0
        # v2.9.0：神经 VAD 实验开关（默认关，理由见 Segmenter.feed 实测留档）
        self._neural_vad = bool(neural_vad)
        self._vad_model = None
        self._vad_on = False
        self._vad_buf = np.zeros(0, dtype=np.float32)
        self._vad_voiced = None
        # 滞回双阈值（对齐 faster-whisper VadOptions 的 threshold/neg_threshold
        # = threshold-0.15）：防词内 30ms 级短间隙把判定抖成碎片。
        # 注意：这正是实测"背景乐下黏 0.66s"的来源，调参前先看留档
        self._vad_hi = 0.50
        self._vad_lo = 0.35

    def _maybe_warn_quiet(self):
        """持续无声达阈值时发一次低输入/静音告警（补充5）。电平过弱与"完全
        无包"两条路径共用（v2.3.12 P13 抽成方法）。部分驱动静音后 loopback
        电平不归零，故额外查系统静音状态给确定性提示。"""
        if getattr(self, "_quiet_s", 0.0) < self.QUIET_WARN_S or self._warned_quiet:
            return
        self._warned_quiet = True
        try:
            from app.win_mute import is_system_muted
            m = is_system_muted()
        except Exception:
            m = None
        if m is True and self.source_type == "system":
            self.muted.emit(True)
        self.low_input.emit(True)

    def stop(self):
        self._stop = True

    def pop_tail_seg(self):
        """v2.6.2（P1-4）：取走停止 flush 的尾段（一次性）。线程退出后由
        stop_pipeline 在主线程同步调用，直塞 asr 队列排水；无尾段返回 None。"""
        tail = getattr(self, "_tail_seg", None)
        self._tail_seg = None
        return tail

    @staticmethod
    def resolve_device_index(p, wanted_index, wanted_name, source_type):
        """按名回查设备索引（v2.0.6）：设备热插拔后 PyAudio 索引会漂移，
        旧逻辑存索引会静默抓错设备。优先用 device_name 精确匹配当前设备
        列表（系统声音只匹配回环设备/麦克风只匹配非回环），匹配不到回落
        旧索引（仍有效说明没漂移），再回落默认设备。返回 (索引, 设备信息)。
        """
        try:
            if source_type == "system":
                candidates = [d for d in (
                    p.get_loopback_device_info_generator()
                    if hasattr(p, "get_loopback_device_info_generator")
                    else []) if d.get("maxInputChannels", 0) > 0]
                fallback_default = p.get_default_output_device_info()
            else:
                candidates = [p.get_device_info_by_index(i)
                              for i in range(p.get_device_count())
                              if p.get_device_info_by_index(i).get("maxInputChannels", 0) > 0
                              and not p.get_device_info_by_index(i).get("isLoopbackDevice", False)]
                fallback_default = p.get_default_input_device_info()
        except Exception:
            return int(wanted_index or -1), None
        if wanted_name:
            for d in candidates:
                if d.get("name") == wanted_name:
                    return int(d["index"]), d
        if wanted_index is not None and int(wanted_index) >= 0:
            for d in candidates:
                if int(d.get("index", -1)) == int(wanted_index):
                    return int(wanted_index), d
        di = fallback_default.get("index")
        return (int(di) if di is not None else -1), fallback_default

    def _resolve_loopback(self, p, default_index):
        """默认输出的回环设备解析：精确名 → 名称子串 → None（宁可不抓也不乱抓）。

        v2.0.1：旧逻辑在精确/子串都匹配不到时兜底取"第一个回环设备"，
        多输出设备机器上会静默抓错输出源。"""
        try:
            default_speakers = p.get_device_info_by_index(default_index)
            if default_speakers.get("isLoopbackDevice"):
                return default_speakers
            name = default_speakers.get("name") or ""
            loopbacks = list(p.get_loopback_device_info_generator())
            for loopback in loopbacks:
                if loopback.get("name") == name:
                    return loopback
            for loopback in loopbacks:
                if name and name in (loopback.get("name") or ""):
                    return loopback
            return None
        except Exception:
            return None

    # ---------- v2.9.0：Silero 神经 VAD 句末判定（实验开关，默认关） ----------
    # 开启理由与代价见 Segmenter.feed 实测留档：稳定背景乐下反而黏 0.66s，
    # 只有突发强噪声/音乐盖过语音的场景才值得一试。开销本身可忽略。

    def _init_neural_vad(self):
        """在采集线程内构造 Silero VAD（onnx，CPU 单线程）。

        成本实测（本机，47s 真实语音素材）：单块 512 样本（32ms）推理约
        0.14ms → 占空比 ~0.4% CPU；概率分布 0.001~0.997 区分度好。
        任何异常（缺 onnxruntime / 打包漏资产 / 模型损坏）都静默退回能量判据，
        功能整体降级但不影响出字幕。"""
        if not self._neural_vad:
            return
        try:
            from app.asr.engine import _silero_assets_ok
            if not _silero_assets_ok():
                return                      # 资产缺失：不冒险（v1.9.0 同款教训）
            from faster_whisper.vad import get_vad_model
            self._vad_model = get_vad_model()
            self._vad_on = True
            self._vad_voiced = False
            try:
                from app import log as app_log
                app_log.log("capture.neural_vad_enabled")
            except Exception:
                pass
        except Exception:
            self._vad_model = None
            self._vad_on = False
            self._vad_voiced = None
            try:
                from app import log as app_log
                app_log.log("capture.neural_vad_unavailable")
            except Exception:
                pass

    def _neural_voiced(self, mono16):
        """喂入 16k 单声道音频，返回该块的人声判定（True/False），不可用返回 None。

        Silero 要求输入长度为 512 的整数倍（32ms），而采集块是 480 样本（30ms），
        故跨块累积、每凑满一个 512 块判一次，块间沿用最近结论（32ms 粒度远细于
        判停阈值 0.20~0.40s，不需要插值）。"""
        if not self._vad_on or self._vad_model is None:
            return None
        try:
            self._vad_buf = np.concatenate(
                [self._vad_buf, np.asarray(mono16, dtype=np.float32).reshape(-1)])
            if self._vad_buf.shape[0] < 512:
                return self._vad_voiced
            n_blocks = self._vad_buf.shape[0] // 512
            for i in range(n_blocks):
                blk = self._vad_buf[i * 512:(i + 1) * 512]
                probs = np.asarray(self._vad_model(blk)).reshape(-1)
                p = float(probs[-1]) if probs.size else 0.0
                if self._vad_voiced:
                    if p < self._vad_lo:
                        self._vad_voiced = False
                elif p >= self._vad_hi:
                    self._vad_voiced = True
            self._vad_buf = self._vad_buf[n_blocks * 512:]
            return self._vad_voiced
        except Exception:
            self._vad_on = False            # 一次异常即永久降级，不反复刷错
            self._vad_model = None
            return None

    def run(self):
        try:
            import pyaudiowpatch as pyaudio
        except ImportError:
            try:
                import pyaudio
            except ImportError:
                self.error_occurred.emit("缺少音频库 pyaudiowpatch，请运行 pip install -r requirements.txt")
                return

        p = None
        stream = None
        try:
            p = pyaudio.PyAudio()
            frames_per_buffer = int(44100 * CHUNK_MS / 1000)

            if self.source_type == "system":
                # v2.0.6：按名回查（热插拔后索引漂移防静默抓错源）——
                # 名称匹配不到再回落索引；-1 走默认输出回环解析
                if self.device_index is not None and self.device_index >= 0:
                    dev_index, device = self.resolve_device_index(
                        p, self.device_index, self.device_name, "system")
                    if device is None or not device.get("isLoopbackDevice"):
                        self.error_occurred.emit(
                            "所选输出设备不可用或不是回环设备，请到「设置-音频输入」重新选择。")
                        return
                    device_index = dev_index
                else:
                    device = self._resolve_loopback(p, p.get_default_output_device_info()["index"])
                    if device is None:
                        self.error_occurred.emit("未找到可用的系统声音回环设备")
                        return
                    device_index = device["index"]
                channels = min(2, device.get("maxInputChannels", 2))
                sample_rate = int(device.get("defaultSampleRate", 44100))
            else:
                if self.device_index >= 0 or self.device_name:
                    dev_index, device = self.resolve_device_index(
                        p, self.device_index, self.device_name, "microphone")
                    if device is None:
                        self.error_occurred.emit(
                            "所选麦克风不可用（可能已被拔出），请到「设置-音频输入」重新选择。")
                        return
                    device_index = dev_index
                else:
                    device = p.get_default_input_device_info()
                    device_index = device["index"]
                channels = min(1, device.get("maxInputChannels", 1))
                sample_rate = int(device.get("defaultSampleRate", 44100))

            frames_per_buffer = int(sample_rate * CHUNK_MS / 1000)
            stream = p.open(
                format=pyaudio.paInt16,
                channels=channels,
                rate=sample_rate,
                input=True,
                input_device_index=device_index,
                frames_per_buffer=frames_per_buffer,
            )
            # v2.9.0：VAD 模型在采集线程内构造（onnx 会话不跨线程共享）；
            # 未开启或加载失败时 _neural_voiced 返回 None，feed 走能量判据
            self._init_neural_vad()

            while not self._stop:
                try:
                    # v2.0.1：WASAPI loopback 在输出完全静音时不再产出数据包，
                    # 阻塞式 read 会让 stop 标志失效（线程卡死、segmenter 悬挂、
                    # 退出时 QThread 销毁崩溃）。改为轮询可用帧数 + 短睡眠。
                    avail = stream.get_read_available()
                    if avail < frames_per_buffer:
                        if self._stop:
                            break
                        # v2.3.12（P13）：WASAPI loopback 在输出**完全**静音时不再
                        # 产出数据包——旧代码直接 continue，_quiet_s 永不累计、电平条
                        # 停在最后值，"长时间没字幕"对用户完全不可见（第七轮实测：
                        # Chrome 失焦暂停媒体 60+ 秒，无字幕、无告警、电平条冻着，
                        # 用户分不清应用挂了还是视频没声）。无包等价于无声：计入静默
                        # 时长；持续超 0.5s 主动把电平归零；到达阈值走既有低输入/静音告警。
                        self._quiet_s = getattr(self, "_quiet_s", 0.0) + 0.01
                        self._starved_s = getattr(self, "_starved_s", 0.0) + 0.01
                        if self._starved_s >= 0.5 and not getattr(self, "_starve_zeroed", False):
                            self._starve_zeroed = True
                            self.level_changed.emit(0)
                        self._maybe_warn_quiet()
                        time.sleep(0.01)
                        continue
                    self._starved_s = 0.0
                    self._starve_zeroed = False
                    self._read_fails = 0   # v2.6.4（P2）：读成功即清零重试计数
                    raw = stream.read(frames_per_buffer, exception_on_overflow=False)
                except OSError as e:
                    # v2.6.4（P2）：短暂抖动自愈——蓝牙休眠唤醒/USB 瞬断/热插拔
                    # 瞬间 stream.read 会抛 OSError，此前一次即退场（error_occurred
                    # → 主窗停整场管线），用户必须手动重开。现抖动期静默重试
                    # （0.3s×5≈1.5s），连续失败才报错退场
                    self._read_fails = getattr(self, "_read_fails", 0) + 1
                    if self._read_fails < 5:
                        time.sleep(0.3)
                        continue
                    self.error_occurred.emit(
                        f"音频设备连续读取失败: {friendly_audio_error(e)}")
                    break
                data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                data = data.reshape(-1, channels) if channels > 1 else data.reshape(-1, 1)
                # v2.2.1：carry_key 让 FIR 抗混叠跨块有状态（本线程唯一，
                # 键绑定设备索引避免热插拔后串尾）
                mono16 = resample_to_16k(data, sample_rate, carry_key=f"cap{self.device_index}")
                if mono16.size == 0:
                    continue
                level = float(np.max(np.abs(mono16)))
                self.level_changed.emit(min(1.0, level * 8))
                # 长时间近乎无声时提醒用户：低音量/抓错设备会让字幕静默失效
                if level < self.QUIET_LEVEL:
                    self._quiet_s = getattr(self, "_quiet_s", 0.0) + CHUNK_MS / 1000.0
                    self._maybe_warn_quiet()
                else:
                    self._quiet_s = 0.0
                    if self._warned_quiet:
                        self._warned_quiet = False
                        self.muted.emit(False)
                        self.low_input.emit(False)
                # v2.12.0：原始音频旁路——90ms（3 块）聚合一发，供流式预览
                if self._tap_enabled:
                    if self._tap_buf is None:
                        self._tap_buf = mono16
                    else:
                        self._tap_buf = np.concatenate([self._tap_buf, mono16])
                    self._tap_blocks += 1
                    if self._tap_blocks >= 3:
                        self.raw_chunk.emit(self._tap_buf, time.monotonic())
                        self._tap_buf = None
                        self._tap_blocks = 0
                # v2.9.0：神经判定可用时优先于能量判据（None=降级回能量）
                seg = self.segmenter.feed(mono16, voiced_override=self._neural_voiced(mono16))
                if seg is not None:
                    # v2.3.20（P26）：随段携带"切分完成时刻"（monotonic 秒），
                    # 下游据此测"话音落→原文上屏"识别段延迟；AsrThread.submit
                    # 兼容裸 ndarray 旧格式（deep_windows/smoke 直接 submit）。
                    self.segment_ready.emit((seg, time.monotonic()))
            # v2.0.1：退出前强制 flush——停止前最后一句（尾静音不足判停时长）
            # 此前被静默丢弃，表现为"说完立刻停会丢最后一句"
            # v2.2.1：去掉 not self._stop 条件——stop 是循环唯一正常出口，
            # 旧条件使 flush 在其设计的唯一场景（用户停止）永远不发射；
            # ASR 侧 stop 先清空队列再投哨兵，此段经信号仍能入队并被消费
            # v2.6.2（P1-4）：尾段同步暂存——stop_pipeline 在 capture.wait
            # 后直塞 asr 队列（跨线程信号要经主线程事件循环中转，停止流程
            # 阻塞主线程期间 emit 无人消费，尾句仍会丢）
            try:
                tail = self.segmenter.flush()
                if tail is not None:
                    self._tail_seg = (tail, time.monotonic())  # v2.3.20 P26
                    self.segment_ready.emit(self._tail_seg)
            except Exception:
                pass
        except Exception as e:
            self.error_occurred.emit(f"音频采集失败: {friendly_audio_error(e)}")
        finally:
            if stream:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
            if p:
                try:
                    p.terminate()
                except Exception:
                    pass
