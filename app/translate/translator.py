import html
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
from app.fixmap import apply_dict as _apply_dict

# v2.0.0：HTTP 头收敛到 net.py（此前与本包各写一份且 UA 不一致）
HEADERS = net.BROWSER_HEADERS

# v2.6.4（P2）：进程级 Session 复用——此前每条字幕 requests.get 都新建
# TCP+TLS 连接（每次多 1~3 个 RTT），实时字幕高频请求下延迟明显。翻译
# 消费为单线程 + 探测偶发并发，urllib3 连接池线程安全
_SESSION = requests.Session()


class TranslationCache:
    # v2.0.6：攒批落盘参数（10 条或 5 秒合并写一次）
    FLUSH_MAX_ITEMS = 10
    FLUSH_INTERVAL_S = 5.0

    def __init__(self, max_items=800):
        self._data = {}
        self._max = max_items
        self._lock = threading.Lock()
        # v2.0.1：懒加载——模块导入时 Config 尚未 relocate 到自定义存储根，
        # 提前 _load 会读错位置，且首次 put 会用默认根的残缺数据覆写自定义根缓存
        self._loaded = False
        self._load_failed = False     # v2.19.2：读失败时禁止写盘（见 _load）
        self._dirty_puts = 0
        self._last_flush = time.monotonic()

    def _path(self):
        # 动态读取：支持存储根目录迁移后自动跟随新位置
        return _cfgmod.CACHE_FILE

    def _ensure_loaded(self):
        if self._loaded:
            return
        self._load()
        self._loaded = True

    def _load(self):
        f = self._path()
        try:
            if f.exists():
                # v2.19.2：utf-8-sig——记事本"另存为 UTF-8"会在文件头加 BOM，
                # 旧实现按 utf-8 读直接抛错，与下面"读失败"同一条毁灭路径
                with open(f, "r", encoding="utf-8-sig") as fp:
                    data = json.load(fp)
                # v2.6.3（P1-3）：合法 JSON 但顶层非 dict（如 [] / "x" / null）
                # 按 corrupt 处理为空——此前 list 直接赋给 _data，后续每条
                # get/put 都 AttributeError，翻译全挂且落盘持续写坏文件
                # v2.20.2：这条分支**同样要进"读失败"状态**。旧实现只把 _data
                # 置空、`_load_failed` 留在 False，于是 v2.19.2 那道"未成功加载
                # 不许写盘"的闸门对它无效：一次攒批落盘就把整份缓存覆写成近空
                # dict（实测 60 条 → 1 条），既不留 .json.bad 原件也不记日志，
                # 与用户 2.19.2 报的那条数据丢失路径同构、只是入口不同。
                if not isinstance(data, dict):
                    self._data = {}
                    self._load_failed = True
                    keep = ""
                    try:
                        import shutil
                        dst = f.with_name(f.name + ".bad")
                        shutil.copyfile(f, dst)
                        keep = str(dst)
                    except Exception:
                        pass
                    app_log.log("translate.cache_unreadable", path=str(f),
                                err="顶层不是对象（%s）" % type(data).__name__,
                                preserved=keep or "未留存（复制失败）")
                else:
                    self._data = data
        except Exception as e:
            # v2.19.2：**读失败 ≠ 空缓存**。旧实现把异常吞成 `_data={}` 且照常
            # 置 `_loaded=True`，于是 v2.2.1 那道"未加载禁写盘"的守卫被绕过——
            # 首次攒批落盘（10 条或 5 秒）就用近空 dict `os.replace` 覆写整份
            # 持久缓存，用户攒了几场的翻译记录无声消失，且不留原件、不打日志。
            # 触发面比想象大：文件被编辑器改过编码、被杀软/索引器瞬时占用、
            # 磁盘抖动都算。现在：标记失败→拒绝写盘→原件另存 .json.bad→记日志。
            self._data = {}
            self._load_failed = True
            keep = ""
            try:
                import shutil
                dst = f.with_name(f.name + ".bad")
                shutil.copyfile(f, dst)
                keep = str(dst)
            except Exception:
                pass
            app_log.log("translate.cache_unreadable", path=str(f),
                        err=f"{type(e).__name__}: {str(e)[:80]}",
                        preserved=keep or "未留存（复制失败）")
        # v2.7.4（C 级）：清理上次崩溃可能残留的 .json.tmp——缓存 tmp 没有
        # 读取侧兜底路径，不清就会永久占盘（config 侧同类残留有双路径清理）
        try:
            self._path().with_suffix(".json.tmp").unlink(missing_ok=True)
        except Exception:
            pass

    def get(self, key):
        with self._lock:
            self._ensure_loaded()
            v = self._data.get(key)
            if v is not None:
                # v2.6.3（LRU）：命中即移到队尾，淘汰始终发生在队头——
                # 此前按插入序 FIFO 淘汰，近期仍在用的旧条目先被挤掉
                self._data[key] = self._data.pop(key)
            if isinstance(v, list):
                v = tuple(v)
            return v

    def put(self, key, value):
        with self._lock:
            self._ensure_loaded()
            # v2.6.3（LRU）：覆盖已有键也刷新时序（dict 原地赋值不动位置）
            self._data.pop(key, None)
            self._data[key] = value
            while len(self._data) > self._max:
                self._data.pop(next(iter(self._data)))
            # v2.0.6：攒批落盘——此前每条译文一次 fsync 原子写，实时字幕
            # 高频场景放大磁盘 IO；改攒 10 条或 5 秒合并写（崩溃最多丢
            # 这一小批缓存条目，缓存本身可再生）
            self._dirty_puts += 1
            now = time.monotonic()
            if (self._dirty_puts >= self.FLUSH_MAX_ITEMS
                    or now - self._last_flush >= self.FLUSH_INTERVAL_S):
                self._save_locked()
                self._dirty_puts = 0
                self._last_flush = now

    def _save_locked(self):
        # v2.2.1：未加载（懒加载未触发，内存还是空 dict）时禁止写盘——
        # 此前零翻译会话退出时 run() 尾部的 save() 会把空 dict 落盘，
        # 清空整个持久翻译缓存（用户实测数据丢失）
        if not self._loaded:
            return
        if self._load_failed:
            # v2.19.2：磁盘上那份读不开的缓存还在（已另存 .json.bad），
            # 但绝不能用内存里的空 dict 去覆写它
            return
        try:
            f = self._path()
            f.parent.mkdir(parents=True, exist_ok=True)
            tmp = f.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(self._data, fp, ensure_ascii=False)
            import os
            os.replace(tmp, f)
        except Exception as e:
            # v2.20.2：写失败不再静默——一条不可序列化的值（如引擎响应里解出的
            # 孤立代理对）会让**此后每一次**攒批落盘都抛在这里，用户看到"缓存
            # 已清空/已保存"而磁盘上什么都没变，且全程零日志。记一条不影响功能。
            app_log.exception("translate.cache_save_failed", e)

    def save(self):
        with self._lock:
            if not self._loaded:
                # v2.2.1：从未加载过就无从保存（未加载即写 = 清空历史缓存）
                return
            self._save_locked()
            self._dirty_puts = 0
            self._last_flush = time.monotonic()

    def clear(self):
        with self._lock:
            self._loaded = True
            # v2.20.2：磁盘上那份读不开的原件已被删除，"读失败禁写盘"的前提不再成立。
            # 不重置的话，用户从设置页点「清空翻译缓存」（文件删了、UI 报成功）之后
            # 整场新译文都再也落不了盘——静默丢失，且没有任何日志。
            self._load_failed = False
            self._data.clear()
            self._dirty_puts = 0
            self._last_flush = time.monotonic()
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
        # clients5 /translate_a/t：[[译文, 检测语言], ...]，多句 q 会返回多行——
        # 全部拼接（此前只取 rows[0] 会截断丢失后续句子）
        rows = data
        if rows and isinstance(rows[0], list) and rows[0] and isinstance(rows[0][0], list):
            rows = rows[0]
        if rows and all(isinstance(r, list) and r and isinstance(r[0], str) for r in rows):
            out = "".join(r[0] for r in rows)
            if not out:
                raise ValueError("译文为空")
            det = next((r[1] for r in rows
                        if len(r) > 1 and isinstance(r[1], str)), None)
            return out, det
        raise ValueError("Google 响应结构无法解析")

    @classmethod
    def translate(cls, text, source, target):
        last_err = None
        for url, client in cls._CHAIN:
            params = {"client": client, "sl": source or "auto", "tl": target, "q": text}
            if url.endswith("/single"):
                params["dt"] = "t"
            try:
                r = _SESSION.get(url, params=params, headers=HEADERS, timeout=8,
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
            r = _SESSION.get(url, params=params, headers=HEADERS, timeout=6, proxies=net.proxies())
            data = r.json()
            return data[2] if len(data) > 2 else "en"
        except Exception as e:
            # v2.0.3：失败不再静默回 "en"——非英文文本会被按英文方向翻译出
            # 乱译且写入持久缓存放大；抛出让备援链接手
            raise RuntimeError(f"语言检测失败: {e}") from e


class MyMemory:
    name = "mymemory"
    LIMIT_CHARS = 480

    @staticmethod
    def _split_sentences(text, limit=LIMIT_CHARS):
        """按句末标点切块（v2.0.0 引入、v2.0.1 修正切分规则）。

        中文句末标点（。！？；）后必切；英文 .!? 仅在后面跟空白/行尾时切
        （保护 "3.14"、"e.g."、URL）。零宽切分保证 "".join(parts) == 原文，
        重组零丢字。"""
        parts = [p for p in re.split(r"(?<=[。！？；])|(?<=[.!?])(?=\s+)", text) if p]
        chunks, cur = [], ""
        for p in parts:
            if len(cur) + len(p) <= limit or not cur:
                cur += p
                # 单句本身超长：硬切
                while len(cur) > limit:
                    chunks.append(cur[:limit])
                    cur = cur[limit:]
            else:
                chunks.append(cur)
                cur = p
                while len(cur) > limit:
                    chunks.append(cur[:limit])
                    cur = cur[limit:]
        if cur:
            chunks.append(cur)
        return chunks or [text[:limit]]

    @staticmethod
    def translate(text, source, target):
        if not source or source == "auto":
            source = GoogleFree.detect_lang(text)
        source = WHISPER_LANG_MAP.get(source, source) or "en"
        # v2.0.1：繁体目标不再静默降级——zh-TW 原样传给 MyMemory（其支持
        # zh-TW langpair），失败由备援链接手，而不是偷偷给简体
        if target.startswith("zh"):
            target = "zh-TW" if target == "zh-TW" else "zh-CN"
        out_parts = []
        for c in MyMemory._split_sentences(text):
            url = "https://api.mymemory.translated.net/get"
            params = {"q": c, "langpair": f"{source}|{target}"}
            r = _SESSION.get(url, params=params, headers=HEADERS, timeout=8,
                             proxies=net.proxies())
            r.raise_for_status()
            data = r.json()
            txt = data.get("responseData", {}).get("translatedText", "")
            status = str(data.get("responseStatus", ""))
            # v2.0.1：配额超限/无效语言对时 MyMemory 返回 HTTP 200 + 英文警告串，
            # 此前警告被当译文上屏并写入持久缓存（整天命中坏缓存）
            if status not in ("", "200") or not txt.strip() or "MYMEMORY WARNING" in txt.upper():
                raise RuntimeError(
                    f"MyMemory 响应异常（status={status or '空译文'}），已切换备援")
            out_parts.append(txt)
        return "".join(out_parts), source


class ArgosEngine:
    name = "argos"

    # v2.6.0（R4）：离线质量档——类级 beam（2=快速 / 5=高质量），设置页切换
    # 经 TranslateThread.update_beam_size 同步到此处，下一次离线翻译即生效
    beam_size = 2

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
        return pack_translate(text, source, target, beam_size=cls.beam_size), source


ENGINES = {"google": GoogleFree, "mymemory": MyMemory, "argos": ArgosEngine}

PROBE_ORDER = ("google", "mymemory")


def apply_fix_map(text: str, mapping, whole_word=False) -> str:
    """v2.3.6（P7）：译文修正——按 {错译: 正解} 替换。

    v2.6.0（R2）：改走 fixmap 单轮替换器——长键优先、替换产物不再被同轮
    二次命中；whole_word=True 时纯拉丁词条按整词匹配（多义词安全纠错）。
    应用时机在**取到译文之后、上屏之前**（缓存读取/引擎返回/备援结果都走这里），
    所以缓存里的存量错译也会即时被修正；缓存本身仍存引擎原文，不改写。"""
    if not mapping or not text:
        return text
    return _apply_dict(text, mapping, whole_word)


def unescape_html(text: str) -> str:
    """v2.6.0（R1）：还原引擎译文中的 HTML 实体（&quot; → "）。

    Google 免费接口偶发实体转义串原样上屏。html.unescape 单层还原天然
    幂等（&amp;quot; → &quot; 双重转义保留一层）；失败时保留原译——
    观感损失远小于丢译文。"""
    if "&" not in text:
        return text
    try:
        return html.unescape(text)
    except Exception as e:
        app_log.exception("translate.unescape_failed", e)
        return text


def _is_junk(text):
    """译文是否含"任何语言正文都不该出现的码位"（v2.20.2 缓存读写两侧共用）。

    判据与离线引擎的乱码守卫同源（`offline_pack._has_junk`），故惰性导入、
    不另立一套规则；离线包不可用时按"不是乱码"处理，绝不为一道校验把翻译链路打断。"""
    if not text:
        return False
    try:
        from .offline_pack import _has_junk
    except Exception:
        return False
    return _has_junk(str(text))


def probe_engine(name, timeout=2.5, src="", tgt=""):
    """探测引擎连通性，返回 (ok, 详情)。

    详情文本用于设置页「测试连通性」的可读诊断：把「连不上代理」、
    「代理通了但被 Google 限流」等不同故障区分开（v1.9.4）。

    v2.19.2：补 argos 分支。旧实现只认 google/mymemory，argos 恒落到末尾
    `return False, "未知引擎"`——于是 `engine=argos` + 自动备援（默认开）的
    用户，一次离线异常落到 MyMemory 后，`_maybe_reprobe_primary` 每 60s
    重探主引擎永远失败，**整场被绑在在线引擎上**（额度/429 风险），且
    `main_window._spec_enabled()` 要求 `_active_engine == 'argos'`，
    推测式增量翻译随之静默关闭（用户观感＝"译文又变慢了"，日志零线索）。
    离线引擎没有"连通性"可言，探针语义改为「本方向有可用的本地包」。
    """
    try:
        if name == "argos":
            from .offline_pack import list_installed
            packs = list_installed()
            if not packs:
                return False, "未安装任何离线语言包（设置-翻译-语言包下载）"
            s = str(src or "").split("-")[0]
            t = str(tgt or "")
            t = "zh" if t.startswith("zh") else t.split("-")[0]
            if s and t and s != "auto":
                if any(fc == s and tc == t for fc, tc in packs):
                    return True, f"离线包 {s}→{t} 可用"
                return False, f"缺少 {s}→{t} 离线包（已装 {len(packs)} 个方向）"
            return True, f"离线包可用（{len(packs)} 个方向）"
        if name == "google":
            t0 = time.time()
            r = _SESSION.get(
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
            r = _SESSION.get(
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


def select_engine_ex(timeout=2.5, src="", tgt=""):
    """v2.3.2（G2）：探测并返回 (选用引擎, 失败原因列表)——
    失败原因供 UI 事前横幅，不再只有事后日志。

    v2.20.4：全部在线引擎探测失败时**先落到已安装的离线包**，而不是硬写
    "mymemory"。旧行为让离线/无网用户（装好了 en→zh 包、把引擎选成"自动"）
    整场每条字幕都失败——`mymemory` 本身就是探不通的那个；而横幅还写着
    「改用「自动」引擎」，自动恰恰是他们的默认值。`src`/`tgt` 用于判断该方向
    有没有本地包（`probe_engine("argos")` 的语义就是"本方向有可用的离线包"）。"""
    from app import log as app_log
    fails = []
    for name in PROBE_ORDER:
        ok, detail = probe_engine(name, timeout)
        if ok:
            return name, fails
        app_log.log("translate.probe_failed", engine=name, detail=detail)
        fails.append(f"{name}: {detail}")
    argos_ok, argos_detail = probe_engine("argos", timeout=1.0, src=src, tgt=tgt)
    if argos_ok:
        app_log.log("translate.probe_all_failed", fallback="argos")
        return "argos", fails
    app_log.log("translate.probe_all_failed", fallback="mymemory",
                argos=argos_detail)
    return "mymemory", fails


def select_engine(timeout=2.5):
    return select_engine_ex(timeout)[0]


class TranslateThread(QThread):
    result_ready = Signal(str, str, str, str, str)  # source_text, translated, engine, detected_lang, error
    # v2.7.6（A）推测式增量翻译：中间版本（整句还没攒完）的回复走**独立信号**，
    # 不挤进 result_ready——终版通路签名与语义完全不变，既有测试/连接零影响。
    # 消费方（主窗）据此只原地更新译文、不终态化卡片、不计会话条数。
    spec_result_ready = Signal(str, str, str, str, str)  # 同上，语义为"中间版"
    status_changed = Signal(str)
    # v2.3.2（G2）：在线引擎启动即不可达的事前通知（engine_desc, reason）
    engine_fallback = Signal(str, str)
    # v2.20.6（i18n 前置改造）：主引擎恢复另发一个信号。主窗此前用
    # `"已恢复" in text and "切回" in text` 从状态文本里猜——界面语言一切英文即失效。
    primary_recovered = Signal()
    # v2.6.2（P1-4）：停止后排水宽限——等 asr 尾句转写（GPU <1s，CPU 最长
    # 约 10s）经主窗口转发进来，收到即翻；超时退出
    DRAIN_GRACE = 15.0

    def __init__(self, engine_name: str, target: str, parent=None, translate_fix_map=None,
                 fix_whole_word=False, offline_quality="high", auto_fallback=True,
                 expected_src=None):
        super().__init__(parent)
        self.engine_name = engine_name
        self.target = target
        # v2.7.1：引擎自动切换开关——关=失败时不降级换引擎，只报错（默认开=旧行为）
        self._auto_fallback = bool(auto_fallback)
        # v2.7.5（R-3）：预载定向——识别侧配置语言（auto=未知）
        self.expected_src = str(expected_src or "").strip().lower()
        # v2.3.6（P7）：译文修正词典，上屏前应用（含缓存命中的存量错译）
        self.fix_map = dict(translate_fix_map or {})
        # v2.6.0（R2）：词典全词匹配开关快照
        self._fix_whole_word = bool(fix_whole_word)
        # v2.6.0（R4）：离线质量档快照 → 同步到 ArgosEngine 类级 beam
        self._beam_size = 5 if offline_quality == "high" else 2
        ArgosEngine.beam_size = self._beam_size
        self.queue_in: "queue.Queue[object]" = queue.Queue()
        self._stop = False
        self._input_closed = False   # v2.7.3：上游（asr+主窗转发）已关门，见 close_input

    def close_input(self):
        """v2.7.3：关闭输入闸门——识别线程已退出且主窗已转发/冲刷完尾组后调用；
        排水循环发现队列空且闸门已关即退出，不再空等 15s 宽限
        （"每次停止 TranslateThread 必成孤儿"的根修正）。幂等，任意线程可调。"""
        self._input_closed = True

    def update_fix_map(self, mapping):
        """v2.6.0（R5）：设置保存后热更新译文词典，无需重启管线。"""
        self.fix_map = dict(mapping or {})

    def update_whole_word(self, flag):
        """v2.6.0（R5）：热更新全词匹配开关。"""
        self._fix_whole_word = bool(flag)

    def update_beam_size(self, beam):
        """v2.6.0（R4）：热更新离线质量档，下一次离线翻译生效。"""
        self._beam_size = 5 if beam == 5 else 2
        ArgosEngine.beam_size = self._beam_size

    def stop(self):
        # v2.6.2（P1-4）：排水式停止——清到剩 1（保留最新待译句）、不投
        # 哨兵；线程消费完余段后进入宽限期（DRAIN_GRACE），等待停止瞬间
        # 仍在转写的 asr 尾句经主窗口转发进来，收到即翻，超时退出。
        # 旧实现清空队列+哨兵：尾句译文必丢，占位卡永久 "⟳ …"
        self._stop = True
        self._stop_at = time.monotonic()
        try:
            while self.queue_in.qsize() > 1:
                self.queue_in.get_nowait()
        except Exception:
            pass

    def submit(self, text, detected_lang, spec=False):
        """入队待译句。v2.6.2（P1-7）：返回因队列满（≥5）被挤掉的旧句列表
        [(text, lang), ...]——调用方据此把对应占位卡置终态，不再悬挂 "⟳ …"。

        v2.7.6（A）推测式增量翻译：spec=True 表示提交的是"整句还没攒完"的
        中间版本（每个识别碎片到达即送译一次，让译文立刻上屏原地生长）。
        **队列满时推测提交放弃自己、绝不挤掉别人**——被挤掉的若是终版，
        占位卡会被 _drop_translation 误置终态，而终版文本更长、永远不会
        再有一次相同文本的回复，卡片就此悬挂。推测是增益，丢了只是慢一拍。
        返回的 dropped 恒为二元组（剥掉 spec 位），保住调用方解包契约。"""
        dropped = []
        try:
            if spec:
                if self.queue_in.qsize() >= 5:
                    return dropped
            else:
                while self.queue_in.qsize() >= 5:
                    try:
                        old = self.queue_in.get_nowait()
                        # v2.20.2：被挤掉的若是**推测中间版**，不许上报成"终版丢了"。
                        # 单片段的 spec 文本与终版文本相同，调用方一收到 dropped 就把
                        # 那张卡置失败（"翻译队列繁忙"），而真正的终版还在队列里等着
                        # 出结果——结果是一句红字 + 终版到达时再建一张重复卡、会话条数
                        # 多算一次。中间版本就是可以丢的，丢了慢一拍而已。
                        if len(old) > 2 and old[2]:
                            continue
                        dropped.append((old[0], old[1]))
                    except queue.Empty:
                        break
                    except Exception:
                        break
            self.queue_in.put_nowait((text, detected_lang, bool(spec)))
        except Exception:
            pass
        return dropped

    def _cache_key(self, detected, text):
        """缓存 key 统一构造（读写共用；v2.0.1 起含源语言维度）。

        v2.6.0（R3）：text 维度统一首尾去空白——攒句/引擎返回仅空白差异
        的同一句话共用一条缓存，命中率不再被稀释。
        v2.6.4（P2）：键去掉引擎名——备援切换/主引擎恢复后旧引擎键永不
        命中，缓存被稀释白占；同句译文语义与引擎无关，跨引擎共享。
        旧格式条目（三段前缀）不再命中，由 LRU 自然淘汰。"""
        norm_src = WHISPER_LANG_MAP.get(detected, detected or "")
        return f"{self.target}:{norm_src}:{text.strip()}"

    def _do_translate(self, text, detected, store=True):
        """翻译一句（带持久缓存）。

        v2.20.2：缓存**读侧与写侧都加乱码闸**。v2.20.1 的守卫只长在生成侧
        （`PackTranslator._translate_chunk`），于是历史脏缓存原样端出来——用户
        机器上那句 `"I don't know how" → "иぃ\ue01d笵"` 早在 2.20.1 之前就进了
        `trans_cache.json`，升级后照样上屏，等于没修。缓存键格式没变、也没版本
        位，LRU 800 格里存量脏条目不会自己消失。现在：命中即验，脏了当没命中、
        重译并覆写；生成结果脏了不写盘（否则备援/在线引擎的乱译同样永久驻留）。

        v2.18.1：`store` 开关——**推测式中间版不写缓存**。真机实测（BBC Global
        News Podcast，150s 会话）：222 条缓存里 41 组是同一句话的渐进变体（最长
        一句被存了 7 个版本）。半句前缀几乎不会再被原样查到（终版文本更长），
        写进去纯属污染：LRU 只有 800 格，真整句的缓存被一次性碎片挤掉。
        """
        # v2.0.1：key 加入源语言维度——同文本被 whisper 判为不同源语言时，
        # 旧 key 会让 MyMemory/Argos 命中错误语言方向的缓存译文
        key = self._cache_key(detected, text)
        cached = _cache.get(key)
        if cached and not _is_junk(cached[0]):
            return cached[0], cached[1]
        engine = ENGINES[self._active_engine]
        source = None
        # v2.7.0（T4）：google 也跟随 whisper 判定语言（此前刻意 sl=auto 让
        # Google 对孤立一句重新猜语种——短句/歧句常被猜错方向，如 "Cheers."
        # 猜成德语）。whisper 用整段音频判的语言远强于单句文本，MyMemory/
        # Argos 自 v2.0.1 起就在用它，缓存键也已含语言维度，方向本应一致。
        # 错锁风险由识别侧语言复检（engine T5）兜底。
        if detected and detected != "auto":
            source = WHISPER_LANG_MAP.get(detected, detected)
        result = engine.translate(text, source, self.target)
        # v2.6.0（R1）：实体还原后再入缓存——缓存中的译文即上屏所见
        result = (unescape_html(result[0]), result[1])
        if store and not _is_junk(result[0]):
            _cache.put(key, result)
        return result

    def _maybe_reprobe_primary(self):
        """备援期间定期重探主引擎（v2.0.6）。

        此前备援成功后会话级固定（translator 备援切换处），一次 429 抖动
        就整场走 MyMemory（匿名配额更易耗尽）且永不回主引擎。现在队列空闲
        时每 60s 静默探测主引擎，恢复即切回；auto 模式的主引擎 = 首次
        select_engine 的结果（通常 google）。"""
        if self._stop or self._active_engine == self._primary_engine:
            return
        now = time.monotonic()
        if now - self._last_probe_at < 60.0:
            return
        self._last_probe_at = now
        ok, _detail = probe_engine(self._primary_engine, timeout=2.5,
                                   src=str(getattr(self, "expected_src", "") or ""),
                                   tgt=str(self.target or ""))
        if ok:
            app_log.log("translate.primary_recovered", engine=self._primary_engine)
            self.primary_recovered.emit()   # 先于状态文本：见信号声明处
            self.status_changed.emit(f"主引擎 {self._primary_engine} 已恢复，自动切回")
            self._active_engine = self._primary_engine

    def _preload_argos(self):
        """后台预载目标语言方向的离线包（v2.7.0 T7）。失败静默——
        真实翻译调用仍会走原有加载路径；_get_translator 自带缓存+双检锁，
        与首次真实调用天然幂等合流。
        v2.7.5（R-3）：预载定向——识别侧锁定了语言时只预载该方向；
        auto（源未知）预载 en 最常见方向，其余方向首次翻译仍走原加载路径。"""
        try:
            from .offline_pack import _get_translator, list_installed
            tgt = "zh" if self.target.startswith("zh") else self.target
            es = str(getattr(self, "expected_src", "") or "").split("-")[0]
            wanted = {es} if es and es != "auto" else {"en"}
            for pair in list_installed():
                if self._stop:
                    return
                src, t = pair[0], pair[1]
                if t == tgt and src in wanted:
                    _get_translator(src, tgt)
        except Exception:
            pass

    def run(self):
        self._active_engine = self.engine_name
        if self.engine_name == "auto":
            self.status_changed.emit("正在探测可用翻译引擎...")
            self._active_engine, fails = select_engine_ex(
                src=str(getattr(self, "expected_src", "") or ""), tgt=str(self.target or ""))
            self.status_changed.emit(f"已选用翻译引擎: {self._active_engine}")
            if fails:  # G2：主引擎不可达，事前横幅（携带具体失败原因）
                self.engine_fallback.emit(f"Google 未通过，已选 {self._active_engine}",
                                          "；".join(fails))
        elif self.engine_name in ("google", "mymemory"):
            # G2：用户显式指定在线引擎——启动即探测一次，不可达先告知，
            # 避免整个会话每条字幕才翻译失败时才被动发现
            ok, detail = probe_engine(self.engine_name, timeout=2.5)
            if not ok:
                self.engine_fallback.emit(self.engine_name, detail)
        # v2.0.6：记录"主引擎"——auto 的主选结果或用户显式指定的引擎；
        # 备援期间队列空闲时定期重探，恢复即切回（见 _maybe_reprobe_primary）
        # v2.7.4（C-9）：auto 的主引擎恒为 google（探测意图），不是探测结果——
        # 旧实现全失败时 primary=mymemory(死)，重探循环永远只探死的、永不回 google
        if self.engine_name == "auto":
            self._primary_engine = "google"
        else:
            self._primary_engine = self._active_engine
        self._last_probe_at = time.monotonic()
        app_log.log("translate.engine_selected", engine=self._active_engine, target=self.target)
        # v2.7.0（T7）：离线包预载——PackTranslator 的 CTranslate2 模型在首次
        # translate() 同步加载（数秒），期间翻译线程整段冻结、队列（上限 5）
        # 溢出丢早期字幕。选定引擎后把目标方向已装包后台预载，首句不再等。
        if self._active_engine == "argos" or self.engine_name == "auto":
            threading.Thread(target=self._preload_argos, daemon=True).start()
        # v2.6.2（P1-4）：排水式退出——_stop 置位后继续消费余段；队列空且
        # 在宽限期内继续等待（asr 尾句转写 0.5~10s 后才经主窗口转发进来），
        # 收到即翻，宽限超时才退出。旧 while not self._stop 会在尾句到达前
        # 就退出，译文必丢
        while True:
            try:
                item = self.queue_in.get(timeout=0.5)
            except queue.Empty:
                if self._stop:
                    # v2.7.3：输入已关门（asr 退场+尾组已冲刷）且队列排空→立即退出；
                    # 此前一律等满 15s 宽限，而 stop_pipeline 只等 2.5s→"每次必孤儿"
                    if self._input_closed:
                        break
                    if time.monotonic() - getattr(self, "_stop_at", 0.0) < self.DRAIN_GRACE:
                        continue
                    break
                self._maybe_reprobe_primary()
                continue
            if item is None:
                break   # 兼容历史哨兵语义
            # v2.7.6（A）：队列项为 (text, lang, spec) 三元组；兼容裸二元组
            # （测试/脚本直接 put 的历史格式，见 tests、deep_windows、smoke_test）
            try:
                # v2.20.2：`text` 在 try 之外做 `.strip()`，一条非字符串队列项
                # （脚本/测试直接 put）会把 AttributeError 抛出 run()——线程当场死，
                # 之后每张卡都收到误导性的「翻译队列繁忙」（来自 isRunning 守卫），
                # 而未攒句路径连那个守卫都没有，卡片永久停在 "⟳ …"。
                text = str(item[0])
                detected = item[1]
                spec = bool(item[2]) if len(item) > 2 else False
            except Exception:
                continue
            if not text.strip():
                continue
            norm_detected = WHISPER_LANG_MAP.get(detected, detected)
            if self.target.startswith("zh") and norm_detected and norm_detected.startswith("zh"):
                # v2.20.2：源=目标语言的"直通"也要过 `unescape_html` 与修正词典。
                # 旧实现直接 emit 原文，于是同一份词典在 en→zh 生效、在 zh→zh 失效
                # （实测 `&quot;` 原样上屏、`台湾海峡` 类条目不被替换）——用户视角
                # 就是"词典时灵时不灵"。
                passthrough = apply_fix_map(unescape_html(text), self.fix_map,
                                            self._fix_whole_word)
                if spec:
                    self.spec_result_ready.emit(text, passthrough, self._active_engine, detected, "")
                else:
                    self.result_ready.emit(text, passthrough, self._active_engine, detected, "")
                continue
            error = ""
            translated = ""
            used_engine = self._active_engine
            try:
                translated, used_lang = self._do_translate(text, detected, store=not spec)
            except Exception as e:
                # 多层降级链（v2.0.0）：google ↔ mymemory 互备，最后落 Argos 离线
                # （仅当对应方向的离线包已安装时才参与，避免无意义的报错切换）
                # 注意：app_log 用模块顶部导入；此处若再局部 import 会把整个
                # run() 作用域里的 app_log 变成局部变量（UnboundLocalError，v2.0.0 实测）
                app_log.exception("translate.failed", e, engine=self._active_engine)
                error = friendly_error(e)
                fallbacks = []
                # v2.7.1：自动切换开关关闭→不组建备援链，本句以错误终态
                #（卡片红字+连续失败横幅照常提示，用户可手动换引擎）
                # v2.7.6（A）：推测式中间版本也不组建备援链——它几秒内就会被
                # 整句终版覆盖，为它跑一遍降级重试（每级 2.5s 探测）纯属浪费；
                # 更要紧的是备援成功会改写 self._active_engine，让"中间版把
                # 引擎切走了、终版却用新引擎"这种用户看不懂的漂移发生。
                # 引擎真坏了，终版会照常走备援并告警。
                if self._auto_fallback and not spec:
                    if self._active_engine != "mymemory":
                        fallbacks.append("mymemory")
                    if self._active_engine != "google":
                        fallbacks.append("google")
                    if self._active_engine != "argos":
                        try:
                            src_for_argos = WHISPER_LANG_MAP.get(detected, "") if detected and detected != "auto" else ""
                            # Argos 元数据用 "zh"（ArgosEngine.translate 内部有同样归一），
                            # 判断处漏做归一曾导致中文源永远不落 Argos 备援（v2.0.1 修）
                            if src_for_argos.startswith("zh"):
                                src_for_argos = "zh"
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
                        # v2.6.1（P1-1）：先还原实体再上屏——此前只还原缓存
                        # 副本，emit 仍是原始值，同一句"实况"与"缓存命中"
                        # 显示不一致（v2.6.0 R1 的路径遗漏）
                        translated = unescape_html(translated)
                        used_engine = fb
                        self._active_engine = fb
                        self.status_changed.emit(f"本次会话已固定使用备援引擎 {fb}")
                        error = ""
                        # v2.0.4：key 与 _do_translate 统一（含源语言维度）——
                        # 此前缺 norm_src 段与读取侧永不匹配，备援译文
                        # 只写不读（死缓存白占容量）
                        # v2.6.0（R1）：缓存中的译文即上屏所见（v2.6.1 起在
                        # 赋值处统一还原，此处直接写 translated）
                        _cache.put(self._cache_key(detected, text),
                                   (translated, used_lang))
                        break
                    except Exception as e2:
                        error = friendly_error(e2)
                        app_log.exception("translate.fallback_failed", e2, engine=fb)
            fixed = apply_fix_map(translated, self.fix_map, self._fix_whole_word)
            if spec:
                self.spec_result_ready.emit(text, fixed, used_engine, detected, error)
            else:
                self.result_ready.emit(text, fixed, used_engine, detected, error)
        # v2.0.6：退出前 flush 攒批缓存（stop 哨兵/break 落到此处）
        try:
            _cache.save()
        except Exception:
            pass
