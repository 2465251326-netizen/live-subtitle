import json
import queue
import threading
import time
import urllib.parse

import requests
from PySide6.QtCore import QThread, Signal

from app.config import CACHE_FILE, WHISPER_LANG_MAP

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}


class TranslationCache:
    def __init__(self, max_items=800):
        self._data = {}
        self._max = max_items
        self._lock = threading.Lock()
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self):
        try:
            if CACHE_FILE.exists():
                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
        except Exception:
            self._data = {}

    def save(self):
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False)
        except Exception:
            pass

    def get(self, key):
        with self._lock:
            v = self._data.get(key)
            if isinstance(v, list):
                v = tuple(v)
            return v

    def put(self, key, value):
        with self._lock:
            self._data[key] = value
            while len(self._data) > self._max:
                self._data.pop(next(iter(self._data)))
        self.save()

    def clear(self):
        with self._lock:
            self._data.clear()
        try:
            if CACHE_FILE.exists():
                CACHE_FILE.unlink()
        except Exception:
            pass


_cache = TranslationCache()


class GoogleFree:
    name = "google"

    @staticmethod
    def translate(text, source, target):
        url = "https://translate.googleapis.com/translate_a/single"
        params = {"client": "gtx", "sl": source or "auto", "tl": target, "dt": "t", "q": text}
        r = requests.get(url, params=params, headers=HEADERS, timeout=8)
        if r.status_code == 429:
            raise RuntimeError("Google 接口限流(429)，已自动切换备援引擎")
        r.raise_for_status()
        data = r.json()
        parts = data[0] or []
        out = "".join(p[0] for p in parts if p and p[0])
        detected = data[2] if len(data) > 2 else (source or "auto")
        return out, detected


    @staticmethod
    def detect_lang(text):
        try:
            url = "https://translate.googleapis.com/translate_a/single"
            params = {"client": "gtx", "sl": "auto", "tl": "en", "dt": "t", "q": text[:80]}
            r = requests.get(url, params=params, headers=HEADERS, timeout=6)
            data = r.json()
            return data[2] if len(data) > 2 else "en"
        except Exception:
            return "en"


class MyMemory:
    name = "mymemory"
    LIMIT_CHARS = 480

    @staticmethod
    def translate(text, source, target):
        if not source or source == "auto":
            source = GoogleFree.detect_lang(text)
        source = WHISPER_LANG_MAP.get(source, source) or "en"
        target = "zh-CN" if target.startswith("zh") else target
        chunks = [text[i:i + MyMemory.LIMIT_CHARS] for i in range(0, len(text), MyMemory.LIMIT_CHARS)]
        out_parts = []
        for c in chunks:
            url = "https://api.mymemory.translated.net/get"
            params = {"q": c, "langpair": f"{source}|{target}"}
            r = requests.get(url, params=params, headers=HEADERS, timeout=8)
            r.raise_for_status()
            data = r.json()
            out_parts.append(data.get("responseData", {}).get("translatedText", ""))
        return "".join(out_parts), source


class ArgosEngine:
    name = "argos"

    @classmethod
    def installed_pairs(cls):
        from .offline_pack import list_installed

        return list_installed()

    @classmethod
    def available_packages(cls):
        from .offline_pack import fetch_index

        return fetch_index()

    @classmethod
    def install(cls, pack, progress_cb=None):
        from .offline_pack import install_pack

        install_pack(pack, progress_cb=progress_cb)

    @classmethod
    def translate(cls, text, source, target):
        from .offline_pack import translate as pack_translate

        source = WHISPER_LANG_MAP.get(source, source)
        if not source:
            raise RuntimeError("缺少源语言信息，无法定位离线语言包，请锁定识别语言或改用在线引擎")
        if source.startswith("zh"):
            source = "zh"
        target = "zh" if target.startswith("zh") else target
        return pack_translate(text, source, target), source


ENGINES = {"google": GoogleFree, "mymemory": MyMemory, "argos": ArgosEngine}

PROBE_ORDER = ("google", "mymemory")


def probe_engine(name, timeout=2.5):
    try:
        if name == "google":
            r = requests.get(
                "https://translate.googleapis.com/translate_a/single",
                params={"client": "gtx", "sl": "auto", "tl": "zh-CN", "dt": "t", "q": "hi"},
                headers=HEADERS, timeout=timeout,
            )
            return r.ok
        if name == "mymemory":
            r = requests.get(
                "https://api.mymemory.translated.net/get",
                params={"q": "hi", "langpair": "en|zh-CN"},
                headers=HEADERS, timeout=timeout,
            )
            return r.ok and r.json().get("responseData", {}).get("translatedText")
    except Exception:
        return False
    return False


def select_engine(timeout=2.5):
    for name in PROBE_ORDER:
        if probe_engine(name, timeout):
            return name
    return "mymemory"


class TranslateThread(QThread):
    result_ready = Signal(str, str, str, str, str)  # source_text, translated, engine, detected_lang, error
    status_changed = Signal(str)

    def __init__(self, engine_name: str, target: str, parent=None):
        super().__init__(parent)
        self.engine_name = engine_name
        self.target = target
        self.queue_in: "queue.Queue[object]" = queue.Queue()
        self._stop = False

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

    def submit(self, text, detected_lang):
        try:
            while self.queue_in.qsize() >= 5:
                try:
                    self.queue_in.get_nowait()
                except queue.Empty:
                    break
            self.queue_in.put_nowait((text, detected_lang))
        except Exception:
            pass

    def _do_translate(self, text, detected):
        key = f"{self._active_engine}:{self.target}:{text}"
        cached = _cache.get(key)
        if cached:
            return cached[0], cached[1]
        engine = ENGINES[self._active_engine]
        source = None
        if self._active_engine != "google" and detected and detected != "auto":
            source = WHISPER_LANG_MAP.get(detected, detected)
        result = engine.translate(text, source, self.target)
        _cache.put(key, result)
        return result

    def run(self):
        self._active_engine = self.engine_name
        if self.engine_name == "auto":
            self.status_changed.emit("正在探测可用翻译引擎...")
            self._active_engine = select_engine()
            self.status_changed.emit(f"已选用翻译引擎: {self._active_engine}")
        while not self._stop:
            try:
                item = self.queue_in.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                break
            text, detected = item
            if not text.strip():
                continue
            norm_detected = WHISPER_LANG_MAP.get(detected, detected)
            if self.target.startswith("zh") and norm_detected and norm_detected.startswith("zh"):
                self.result_ready.emit(text, text, self._active_engine, detected, "")
                continue
            error = ""
            translated = ""
            used_engine = self._active_engine
            try:
                translated, used_lang = self._do_translate(text, detected)
            except Exception as e:
                error = str(e)
                if self._active_engine != "mymemory":
                    try:
                        self.status_changed.emit(f"{self._active_engine} 失败，切换备援引擎...")
                        translated, used_lang = MyMemory.translate(text, detected, self.target)
                        used_engine = "mymemory"
                        self._active_engine = "mymemory"
                        self.status_changed.emit("本次会话已固定使用备援引擎 MyMemory")
                        error = ""
                        _cache.put(f"mymemory:{self.target}:{text}", (translated, used_lang))
                    except Exception as e2:
                        error = str(e2)
            self.result_ready.emit(text, translated, used_engine, detected, error)
