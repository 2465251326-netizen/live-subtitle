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


def resample_to_16k(data: np.ndarray, orig_sr: int) -> np.ndarray:
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
    x_old = np.linspace(0.0, duration, num=mono.shape[0], endpoint=False)
    x_new = np.linspace(0.0, duration, num=target_len, endpoint=False)
    return np.interp(x_new, x_old, mono).astype(np.float32)


class Segmenter:
    def __init__(self):
        self.buffer = []
        self.buffer_len = 0.0
        self.speech_len = 0.0
        self.noise_floor = 0.005
        self.in_speech = False
        self.silence_run = 0.0
        self.speech_run = 0.0

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
            if self.in_speech and self.buffer_len >= MAX_SEGMENT_S:
                return self._flush()
        else:
            self.silence_run += duration
            self.speech_run = max(0.0, self.speech_run - duration * 0.5)
            if self.in_speech:
                self.buffer.append(chunk)
                self.buffer_len += duration
                if self.silence_run >= SILENCE_END_S:
                    return self._flush()
            elif self.buffer and self.silence_run > SILENCE_END_S:
                self._reset()
        return None

    def _flush(self):
        audio = np.concatenate(self.buffer) if self.buffer else np.zeros(0, dtype=np.float32)
        spoken = self.speech_len
        self._reset()
        if spoken < MIN_SPEECH_S:
            return None
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        if peak < 0.002:
            return None
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

    QUIET_WARN_S = 12.0
    QUIET_LEVEL = 0.012

    def __init__(self, source_type: str, device_index: int, parent=None):
        super().__init__(parent)
        self.source_type = source_type
        self.device_index = device_index
        self._stop = False
        self.segmenter = Segmenter()
        self._warned_quiet = False

    def stop(self):
        self._stop = True

    def _resolve_loopback(self, p, default_index):
        try:
            default_speakers = p.get_device_info_by_index(default_index)
            if default_speakers.get("isLoopbackDevice"):
                return default_speakers
            for loopback in p.get_loopback_device_info_generator():
                if default_speakers["name"] in loopback["name"]:
                    return loopback
            return next(p.get_loopback_device_info_generator(), None)
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
                device = self._resolve_loopback(p, p.get_default_output_device_info()["index"])
                if device is None:
                    self.error_occurred.emit("未找到可用的系统声音回环设备")
                    return
                device_index = device["index"]
                channels = min(2, device.get("maxInputChannels", 2))
                sample_rate = int(device.get("defaultSampleRate", 44100))
            else:
                if self.device_index >= 0:
                    device = p.get_device_info_by_index(self.device_index)
                    device_index = device["index"]
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
                    raw = stream.read(frames_per_buffer, exception_on_overflow=False)
                except OSError as e:
                    self.error_occurred.emit(f"音频读取中断: {friendly_audio_error(e)}")
                    break
                data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                data = data.reshape(-1, channels) if channels > 1 else data.reshape(-1, 1)
                mono16 = resample_to_16k(data, sample_rate)
                if mono16.size == 0:
                    continue
                level = float(np.max(np.abs(mono16)))
                self.level_changed.emit(min(1.0, level * 8))
                # 长时间近乎无声时提醒用户：低音量/抓错设备会让字幕静默失效
                if level < self.QUIET_LEVEL:
                    self._quiet_s = getattr(self, "_quiet_s", 0.0) + CHUNK_MS / 1000.0
                else:
                    self._quiet_s = 0.0
                    if self._warned_quiet:
                        self._warned_quiet = False
                        self.low_input.emit(False)
                if self._quiet_s >= self.QUIET_WARN_S and not self._warned_quiet:
                    self._warned_quiet = True
                    self.low_input.emit(True)
                seg = self.segmenter.feed(mono16)
                if seg is not None:
                    self.segment_ready.emit(seg)
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
