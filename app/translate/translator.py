import json
import queue
import re
import threading
import time
import urllib.parse

import requests
from PySide6.QtCore import QThread, Signal

from app import config as _cfgmod
from app.config import WHISPER_LANG_MAP
from app import net
from app.errors import friendly_error
from app import log as app_log

# v2.0.0：HTTP 头收敛到 net.py（此前与本包各写一份且 UA 不一致）
HEADERS = net.BROWSER_HEADERS


class TranslationCache:
    def __init__(self, max_items=800):
        self._data = {}
        self._max = max_items
        self._lock = threading.Lock()
        self._load()

    def _path(self):
        # 动态读取：支持存储根目录迁移后自动跟随新位置
        return _cfgmod.CACHE_FILE

    def _load(self):
        try:
            f = self._path()
            if f.exists():
                with open(f, "r", encoding="utf-8") as fp:
                    self._data = json.load(fp)
        except Exception:
            self._data = {}

    def save(self):
        try:
            f = self._path()
            f.parent.mkdir(parents=True, exist_ok=True)
            with open(f, "w", encoding="utf-8") as fp:
                json.dump(self._data, fp, ensure_ascii=False)
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
            if self._path().exists():
                self._path().unlink()
        except Exception:
            pass


_cache = TranslationCache()


class GoogleFree:
    name = "google"

    # 多通道链（v2.0.0）：Google 单方面限流某个 client 时自动换下一个。
    # 2026-09 实测：gtx 全面 429；dict-chrome-ex（googleapis）与
    # clients5 /translate_a/t 均可用。
    _CHAIN = [
        ("https://translate.googleapis.com/translate_a/single", "dict-chrome-ex"),
        ("https://translate.googleapis.com/translate_a/single", "gtx"),
        ("https://clients5.google.com/translate_a/t", "dict-chrome-ex"),
    ]

    @staticmethod
    def _parse(data):
        """兼容两种响应结构，返回 (译文, 检测语言)；都解析不了抛 ValueError。

        - /translate_a/single：[[[译文, 原文, ...], ...], None, "en", ...]
          （data[0] 的元素是列表）
        - clients5 /translate_a/t：[[译文, 检测语言], ...]
          （data[0] 的元素是字符串；可能外面多包一层）
        结构判错直接抛 ValueError 换下一通道——绝不能把字符串误当段
        列表逐字符拼接出乱译（本地单测实测暴露）。
        """
        if not isinstance(data, list) or not data:
            raise ValueError("Google 响应为空")
        first = data[0]
        if isinstance(first, list) and first and isinstance(first[0], list):
            # single 结构：段列表
            out = "".join(seg[0] for seg in first if seg and seg[0])
            if not out:
                raise ValueError("译文为空")
            return out, (data[2] if len(data) > 2 and isinstance(data[2], str) else None)
        rows = first
        if isinstance(rows, list) and rows and isinstance(rows[0], list):
            rows = rows[0]  # clients5 可能多包一层
        if isinstance(rows, list) and rows and isinstance(rows[0], str):
            return rows[0], (rows[1] if len(rows) > 1 and isinstance(rows[1], str) else None)
        raise ValueError("Google 响应结构无法解析")

    @classmethod
    def translate(cls, text, source, target):
        last_err = None
        for url, client in cls._CHAIN:
            params = {"client": client, "sl": source or "auto", "tl": target, "q": text}
            if url.endswith("/single"):
                params["dt"] = "t"
            try:
                r = requests.get(url, params=params, headers=HEADERS, timeout=8,
                                 proxies=net.proxies())
                if r.status_code == 429:
                    last_err = RuntimeError("Google 接口限流(429)")
                    continue
                r.raise_for_status()
                out, detected = cls._parse(r.json())
                return out, detected or (source or "auto")
            except RuntimeError:
                last_err = RuntimeError("Google 接口限流(429)")
            except Exception as e:
                last_err = e
        raise last_err or RuntimeError("Google 全部通道不可用")

    @staticmethod
    def detect_lang(text):
        try:
            url = "https://translate.googleapis.com/translate_a/single"
            params = {"client": "dict-chrome-ex", "sl": "auto", "tl": "en", "dt": "t", "q": text[:80]}
            r = requests.get(url, params=params, headers=HEADERS, timeout=6, proxies=net.proxies())
            data = r.json()
            return data[2] if len(data) > 2 else "en"
        except Exception:
            return "en"


class MyMemory:
    name = "mymemory"
    LIMIT_CHARS = 480

    @staticmethod
    def _split_sentences(text, limit=LIMIT_CHARS):
        """按句末标点切块（v2.0.0）：旧逻辑硬按 480 字符切会把句子拦腰斩断，
        翻译质量明显受损；现在尽量在句边界分块，超长单句才硬切。"""
        parts = [p for p in re.split(r"(?<=[.!?。！？；;])\s*", text) if p]
        chunks, cur = [], ""
        for p in parts:
            if len(cur) + len(p) + 1 <= limit or not cur:
                cur = (cur + " " + p).strip() if cur else p
                # 单句本身超长：硬切
                while len(cur) > limit:
                    chunks.append(cur[:limit])
                    cur = cur[limit:]
            else:
                chunks.append(cur)
                cur = p[:limit]
                if len(p) > limit:
                    rest = p[limit:]
                    while len(rest) > limit:
                        chunks.append(rest[:limit])
                        rest = rest[limit:]
                    cur = rest
        if cur:
            chunks.append(cur)
        return chunks or [text[:limit]]

    @staticmethod
    def translate(text, source, target):
        if not source or source == "auto":
            source = GoogleFree.detect_lang(text)
        source = WHISPER_LANG_MAP.get(source, source) or "en"
        target = "zh-CN" if target.startswith("zh") else target
        out_parts = []
        for c in MyMemory._split_sentences(text):
            url = "https://api.mymemory.translated.net/get"
            params = {"q": c, "langpair": f"{source}|{target}"}
            r = requests.get(url, params=params, headers=HEADERS, timeout=8,
                             proxies=net.proxies())
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
    """探测引擎连通性，返回 (ok, 详情)。

    详情文本用于设置页「测试连通性」的可读诊断：把「连不上代理」、
    「代理通了但被 Google 限流」等不同故障区分开（v1.9.4）。
    """
    try:
        if name == "google":
            t0 = time.time()
            r = requests.get(
                "https://translate.googleapis.com/translate_a/single",
                params={"client": "dict-chrome-ex", "sl": "auto", "tl": "zh-CN", "dt": "t", "q": "hi"},
                headers=HEADERS, timeout=timeout, proxies=net.proxies(),
            )
            ms = int((time.time() - t0) * 1000)
            if r.status_code == 429:
                return False, "HTTP 429：出口 IP 被 Google 限流，请更换代理节点（期间自动使用备援引擎）"
            if r.ok:
                return True, f"HTTP 200（{ms}ms）"
            return False, f"HTTP {r.status_code}"
        if name == "mymemory":
            r = requests.get(
                "https://api.mymemory.translated.net/get",
                params={"q": "hi", "langpair": "en|zh-CN"},
                headers=HEADERS, timeout=timeout, proxies=net.proxies(),
            )
            if r.ok and r.json().get("responseData", {}).get("translatedText"):
                return True, "OK"
            return False, f"HTTP {r.status_code}（响应异常）"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:80]}"
    return False, "未知引擎"


def select_engine(timeout=2.5):
    for name in PROBE_ORDER:
        ok, _detail = probe_engine(name, timeout)
        if ok:
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
        app_log.log("translate.engine_selected", engine=self._active_engine, target=self.target)
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
                # 多层降级链（v2.0.0）：google ↔ mymemory 互备，最后落 Argos 离线
                # （仅当对应方向的离线包已安装时才参与，避免无意义的报错切换）
                # 注意：app_log 用模块顶部导入；此处若再局部 import 会把整个
                # run() 作用域里的 app_log 变成局部变量（UnboundLocalError，v2.0.0 实测）
                app_log.exception("translate.failed", e, engine=self._active_engine)
                error = friendly_error(e)
                fallbacks = []
                if self._active_engine != "mymemory":
                    fallbacks.append("mymemory")
                if self._active_engine != "google":
                    fallbacks.append("google")
                if self._active_engine != "argos":
                    try:
                        src_for_argos = WHISPER_LANG_MAP.get(detected, "") if detected and detected != "auto" else ""
                        tgt_for_argos = "zh" if self.target.startswith("zh") else self.target
                        if src_for_argos and (src_for_argos, tgt_for_argos) in ENGINES["argos"].installed_pairs():
                            fallbacks.append("argos")
                    except Exception:
                        pass
                for fb in fallbacks:
                    try:
                        self.status_changed.emit(f"{self._active_engine} 失败，切换备援引擎 {fb}...")
                        src = None
                        if fb != "google" and detected and detected != "auto":
                            src = WHISPER_LANG_MAP.get(detected, detected)
                        translated, used_lang = ENGINES[fb].translate(text, src, self.target)
                        used_engine = fb
                        self._active_engine = fb
                        self.status_changed.emit(f"本次会话已固定使用备援引擎 {fb}")
                        error = ""
                        _cache.put(f"{fb}:{self.target}:{text}", (translated, used_lang))
                        break
                    except Exception as e2:
                        error = friendly_error(e2)
                        app_log.exception("translate.fallback_failed", e2, engine=fb)
            self.result_ready.emit(text, translated, used_engine, detected, error)
