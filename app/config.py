import json
import os
import threading
from pathlib import Path

APP_NAME = "LiveSubtitle"
APP_VERSION = "1.4.2"

CONFIG_DIR = Path(os.environ.get("LIVETRANSLATE_HOME", Path.home() / ".live_subtitle"))
CONFIG_FILE = CONFIG_DIR / "config.json"
CACHE_FILE = CONFIG_DIR / "trans_cache.json"
HF_HOME = CONFIG_DIR / "hf"
ARGOS_DATA = CONFIG_DIR / "argos"

DEFAULTS = {
    "source_type": "system",          # system | microphone
    "device_index": -1,               # -1 = 默认设备
    "asr_model": "small",             # tiny | base | small | medium
    "asr_device": "cpu",              # cpu | cuda | auto
    "asr_language": "auto",           # auto | en | ja | ko ...
    "engine": "auto",                 # auto | google | mymemory | argos
    "target_lang": "zh-CN",
    "show_source": True,
    "max_history": 200,
    "overlay_enabled": True,
    "overlay_x": 200,
    "overlay_y": 200,
    "overlay_font_size": 18,
    "overlay_text_color": "#ffffff",
    "overlay_bg_color": "#0c0e14",
    "overlay_bg_opacity": 78,          # 0-100，背景不透明度百分比
    "overlay_outline": True,
    "overlay_outline_width": 2,
    "overlay_outline_color": "#000000",
    "translate_zh_from_zh": False,
    "close_action": "ask",             # ask / tray / exit
    "panel_open": True,
}

LANGUAGES = {
    "auto": "自动检测",
    "zh-CN": "简体中文",
    "zh-TW": "繁体中文",
    "en": "英语",
    "ja": "日语",
    "ko": "韩语",
    "ru": "俄语",
    "fr": "法语",
    "de": "德语",
    "es": "西班牙语",
    "pt": "葡萄牙语",
    "it": "意大利语",
    "th": "泰语",
    "vi": "越南语",
    "ar": "阿拉伯语",
    "id": "印尼语",
    "hi": "印地语",
}

WHISPER_LANG_MAP = {
    "zh": "zh-CN", "zh-CN": "zh-CN", "zh-TW": "zh-TW",
    "en": "en", "ja": "ja", "ko": "ko", "ru": "ru", "fr": "fr",
    "de": "de", "es": "es", "pt": "pt", "it": "it", "th": "th",
    "vi": "vi", "ar": "ar", "id": "id", "hi": "hi",
}

TARGET_LANGS = [
    "zh-CN", "zh-TW", "en", "ja", "ko", "fr", "de", "es",
    "ru", "pt", "it", "th", "vi", "ar", "id", "hi",
]


_hf_probe_done = threading.Event()


def _start_hf_probe():
    if _hf_probe_done.is_set():
        return
    if os.environ.get("HF_ENDPOINT"):
        _hf_probe_done.set()
        return

    def probe():
        try:
            import requests
            requests.head("https://huggingface.co", timeout=2.5)
            os.environ.setdefault("HF_ENDPOINT", "https://huggingface.co")
        except Exception:
            os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        finally:
            _hf_probe_done.set()

    threading.Thread(target=probe, daemon=True).start()


def ensure_hf_endpoint_ready(timeout=4.0):
    _start_hf_probe()
    _hf_probe_done.wait(timeout)


class Config:
    def __init__(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        HF_HOME.mkdir(parents=True, exist_ok=True)
        ARGOS_DATA.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("HF_HOME", str(HF_HOME))
        os.environ.setdefault("ARGOS_DATA_HOME", str(ARGOS_DATA))
        os.environ.setdefault("ARGOS_TRANSLATE_PACKAGES_DIR", str(ARGOS_DATA / "packages"))
        _start_hf_probe()
        self._data = dict(DEFAULTS)
        self.load()

    def load(self):
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                for k in self._data:
                    if k in saved:
                        self._data[k] = saved[k]
            except Exception:
                pass

    def save(self):
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def get(self, key):
        return self._data.get(key, DEFAULTS.get(key))

    def set(self, key, value):
        self._data[key] = value
        self.save()
