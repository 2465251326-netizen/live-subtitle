import json
import os
import threading
from pathlib import Path

APP_NAME = "LiveSubtitle"
APP_VERSION = "1.8.1"

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
    "auto_start": False,               # 启动后自动开始翻译
    "proxy_mode": "system",            # system 跟随系统 | manual 手动 | none 直连
    "proxy_url": "",                   # manual 模式的代理地址，如 http://127.0.0.1:10808
    "hotkey_enabled": True,            # 全局热键开关
    "hotkey_sequence": "Ctrl+Alt+S",   # 全局热键组合（开始/停止翻译）
    "storage_root": "",                # 自定义数据根目录（空 = 默认 ~\.live_subtitle）
    "wizard_done": False,              # 首次运行向导已完成
    "hallucination_filter": True,      # 幻觉抑制：过滤音乐/噪声段的胡言乱语
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
            from app import net
            requests.head("https://huggingface.co", timeout=2.5, proxies=net.proxies())
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
        self._data = dict(DEFAULTS)
        self.load()
        # 自定义存储根：在创建目录/启动探测之前重定位
        saved_root = str(self._data.get("storage_root") or "").strip()
        if saved_root:
            try:
                self.relocate(saved_root)
            except Exception:
                pass
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        HF_HOME.mkdir(parents=True, exist_ok=True)
        ARGOS_DATA.mkdir(parents=True, exist_ok=True)
        self._sync_proxy()
        _start_hf_probe()

    def _sync_proxy(self):
        """把代理设置同步给网络层（app.net），并刷新模型下载用的环境变量。"""
        try:
            from app import net
            net.configure(self._data.get("proxy_mode", "system"),
                          self._data.get("proxy_url", ""))
            net.apply_proxy_env()
        except Exception:
            pass

    def relocate(self, new_root):
        """应用新的数据根目录（建议1）：更新模块常量与环境变量并写回配置。

        数据文件本身的搬移由 app/storage.migrate_root 完成，这里只负责
        「指针」重定位，保证之后所有读写都落到新位置。
        """
        global CONFIG_DIR, CONFIG_FILE, CACHE_FILE, HF_HOME, ARGOS_DATA
        new_root = Path(new_root)
        new_root.mkdir(parents=True, exist_ok=True)
        CONFIG_DIR = new_root
        CONFIG_FILE = new_root / "config.json"
        CACHE_FILE = new_root / "trans_cache.json"
        HF_HOME = new_root / "hf"
        ARGOS_DATA = new_root / "argos"
        os.environ["HF_HOME"] = str(HF_HOME)
        os.environ["ARGOS_DATA_HOME"] = str(ARGOS_DATA)
        os.environ["ARGOS_TRANSLATE_PACKAGES_DIR"] = str(ARGOS_DATA / "packages")
        self._data["storage_root"] = str(new_root)
        self.save()
        try:
            from app.translate import offline_pack as _op
            _op.PACKS_DIR = ARGOS_DATA / "packs"
        except Exception:
            pass

    def load(self):
        if CONFIG_FILE.exists():
            try:
                # utf-8-sig 兼容手工编辑（如记事本）可能带入的 BOM
                with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
                    saved = json.load(f)
                for k in self._data:
                    if k in saved:
                        self._data[k] = saved[k]
            except Exception:
                pass

    def save(self):
        try:
            # 先写临时文件再原子替换，避免写一半崩溃/断电导致配置损坏
            tmp = CONFIG_FILE.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, CONFIG_FILE)
        except Exception:
            pass

    def get(self, key):
        return self._data.get(key, DEFAULTS.get(key))

    def set(self, key, value):
        self._data[key] = value
        self.save()
        if key in ("proxy_mode", "proxy_url"):
            self._sync_proxy()
