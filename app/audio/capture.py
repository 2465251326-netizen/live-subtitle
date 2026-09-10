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
    def __init__(self, low_latency=False):
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
        self.max_seg = 6.0 if self.low_latency else MAX_SEGMENT_S
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

    def feed(self, chunk: np.ndarray):
        duration = chunk.shape[0] / TARGET_SR
        rms = self._rms(chunk)
        if rms < self.noise_floor:
            self.noise_floor = max(rms, self.noise_floor * 0.98)
        else:
            self.noise_floor = min(self.noise_floor + (rms - self.noise_floor) * 0.01, 0.02)
        threshold = max(self.noise_floor * 3.0, 0.004)
        voiced = rms > threshold

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
    level_changed = Signal(float)
    error_occurred = Signal(str)
    low_input = Signal(bool)  # True=输入信号持续过弱（可能音量过低/抓错设备）
    muted = Signal(bool)      # True=系统处于静音状态（补充5：静音盲区提示）

    QUIET_WARN_S = 12.0
    QUIET_LEVEL = 0.012

    def __init__(self, source_type: str, device_index: int, parent=None,
                 device_name: str = "", low_latency: bool = False):
        super().__init__(parent)
        self.source_type = source_type
        self.device_index = device_index
        self.device_name = str(device_name or "")
        self._stop = False
        self.segmenter = Segmenter(low_latency=low_latency)
        self._warned_quiet = False

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
                    raw = stream.read(frames_per_buffer, exception_on_overflow=False)
                except OSError as e:
                    self.error_occurred.emit(f"音频读取中断: {friendly_audio_error(e)}")
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
                seg = self.segmenter.feed(mono16)
                if seg is not None:
                    self.segment_ready.emit(seg)
            # v2.0.1：退出前强制 flush——停止前最后一句（尾静音不足判停时长）
            # 此前被静默丢弃，表现为"说完立刻停会丢最后一句"
            # v2.2.1：去掉 not self._stop 条件——stop 是循环唯一正常出口，
            # 旧条件使 flush 在其设计的唯一场景（用户停止）永远不发射；
            # ASR 侧 stop 先清空队列再投哨兵，此段经信号仍能入队并被消费
            try:
                tail = self.segmenter.flush()
                if tail is not None:
                    self.segment_ready.emit(tail)
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
