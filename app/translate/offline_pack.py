"""离线翻译语言包管理。

.argosmodel 包内即为 CTranslate2 模型 + sentencepiece 词表，
直接加载推理，无需 argostranslate / torch 依赖。
"""
import json
import os
import re
import shutil
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import requests

from app.config import CONFIG_DIR
from app.i18n import ui_text

from app import net
from app import log as app_log

PACKS_DIR = CONFIG_DIR / "argos" / "packs"

INDEX_SOURCES = [
    "https://raw.githubusercontent.com/argosopentech/argospm-index/main/index.json",
    "https://cdn.jsdelivr.net/gh/argosopentech/argospm-index@main/index.json",
]

# v2.0.0：HTTP 头收敛到 net.py（应用 UA 便于开源索引方统计）
HEADERS = net.APP_HEADERS

_lock = threading.Lock()
_translator_cache = {}
_cache_order = []
_CACHE_MAX = 2


@dataclass
class PackInfo:
    code: str
    from_code: str
    to_code: str
    from_name: str
    to_name: str
    url: str


_index_cache = {"at": 0.0, "packs": None}
_INDEX_TTL = 300.0


def fetch_index(timeout=8, use_cache=True):
    with _lock:  # v2.0.3：检查-写入原子化（ArgosWorker 与 available_packages 可并发）
        now = time.time()
        if use_cache and _index_cache["packs"] and now - _index_cache["at"] < _INDEX_TTL:
            return _index_cache["packs"]
    last_err = None
    for src in INDEX_SOURCES:
        try:
            r = requests.get(src, headers=HEADERS, timeout=timeout, proxies=net.proxies())
            r.raise_for_status()
            items = r.json()
            packs = []
            for it in items:
                links = it.get("links") or []
                if not links:
                    continue
                packs.append(
                    PackInfo(
                        code=it.get("code", ""),
                        from_code=it.get("from_code", ""),
                        to_code=it.get("to_code", ""),
                        from_name=it.get("from_name", ""),
                        to_name=it.get("to_name", ""),
                        url=links[0],
                    )
                )
            if packs:
                with _lock:
                    _index_cache["at"] = time.time()
                    _index_cache["packs"] = packs
                return packs
        except Exception as e:
            last_err = e
    raise RuntimeError(f"{ui_text('语言包索引获取失败: ')}{last_err}")


def _pack_dir(pair_code):
    return PACKS_DIR / pair_code


def _resolve_pack_dir(source, target):
    """按方向码定位包目录；兼容旧版本以 pack.code 命名的目录。"""
    direct = _pack_dir(f"{source}_{target}")
    if (direct / "sentencepiece.model").exists():
        return direct
    legacy = _pack_dir(f"translate-{source}_{target}")
    if (legacy / "sentencepiece.model").exists():
        return legacy
    return direct


def list_installed():
    out = []
    if not PACKS_DIR.exists():
        return out
    for d in sorted(PACKS_DIR.iterdir()):
        meta = d / "metadata.json"
        if d.is_dir() and meta.exists():
            try:
                m = json.loads(meta.read_text(encoding="utf-8"))
                out.append((m.get("from_code", ""), m.get("to_code", "")))
            except Exception:
                continue
    return out


def dir_size_mb(path) -> float:
    """递归统计目录体积（MB）；目录不存在返回 0。

    v2.0.1：跳过符号链接——HF 缓存快照里的 model.bin 是指向 blobs 的
    符号链接，stat() 会穿透计入，导致模型体积显示为双倍。
    """
    total = 0
    p = Path(path)
    if not p.exists():
        return 0.0
    for f in p.rglob("*"):
        try:
            if f.is_symlink():
                continue
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            continue
    return total / 1048576.0


def installed_sizes():
    """返回 [(from_code, to_code, 体积MB)]，供设置页展示。"""
    out = []
    if not PACKS_DIR.exists():
        return out
    for d in sorted(PACKS_DIR.iterdir()):
        meta = d / "metadata.json"
        if d.is_dir() and meta.exists():
            try:
                m = json.loads(meta.read_text(encoding="utf-8"))
                out.append((m.get("from_code", ""), m.get("to_code", ""),
                            dir_size_mb(d)))
            except Exception:
                continue
    return out


def remove_pack(source, target):
    """卸载 source->target 语言包：删除全部匹配目录并清理翻译器缓存。

    返回实际删除的目录名列表（兼容直连命名与旧版 translate- 前缀命名）。
    """
    removed = []
    if not PACKS_DIR.exists():
        return removed
    for d in list(PACKS_DIR.iterdir()):
        if not d.is_dir():
            continue
        match = False
        # 命名匹配：en_zh / translate-en_zh
        if d.name in (f"{source}_{target}", f"translate-{source}_{target}"):
            match = True
        else:
            # 元数据匹配（覆盖历史遗留的其他命名）
            meta = d / "metadata.json"
            if meta.exists():
                try:
                    m = json.loads(meta.read_text(encoding="utf-8"))
                    match = (m.get("from_code") == source and m.get("to_code") == target)
                except Exception:
                    match = False
        if match:
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d.name)
    with _lock:
        stale = [k for k in _translator_cache if k[0] == source]
        for key in stale:
            _translator_cache.pop(key, None)
            try:
                _cache_order.remove(key)
            except ValueError:
                pass
    app_log.log("argos.pack_removed", pair=f"{source}_{target}", dirs=",".join(removed) or "none")
    return removed


def _extract_pack(model_path: Path, dest: Path):
    with zipfile.ZipFile(model_path) as zf:
        names = zf.namelist()
        # v2.0.3：校验顶层结构——此前 names[0] 直接当根目录，条目顺序异常时
        # 前缀过滤会排除所有文件，解压出空目录但仍报"安装成功"
        inner_root = names[0].split("/")[0]
        if not any(n.startswith(inner_root + "/") and n != inner_root + "/" for n in names):
            raise RuntimeError(ui_text("语言包结构异常：缺少模型目录，文件可能已损坏"))
        tmp = dest.with_suffix(".extracting")
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True)
        for name in names:
            if name.startswith(inner_root + "/") and name != inner_root + "/":
                rel = name[len(inner_root) + 1:]
                # 防 zip-slip（v2.0.1 加固）：此前用 PurePosixPath 校验，不把 "\"
                # 当分隔符，而 WindowsPath 拼接时把 "\" 当分隔符——条目名
                # "x\..\..\evil" 可绕过 ".." 检测实现任意路径写。现统一归一为
                # "/" 并拒绝反斜杠，再加 realpath 前缀断言双保险。
                if "\\" in rel:
                    continue
                rel_norm = rel.replace("\\", "/")
                pure = PurePosixPath(rel_norm)
                if pure.is_absolute() or ".." in pure.parts or (len(rel_norm) > 1 and rel_norm[1] == ":"):
                    continue
                target = tmp / rel_norm
                try:
                    if not Path(os.path.realpath(str(target))).is_relative_to(
                            Path(os.path.realpath(str(tmp)))):
                        continue
                except (OSError, ValueError):
                    continue
                if name.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(name) as fsrc, open(target, "wb") as fdst:
                        shutil.copyfileobj(fsrc, fdst)
    if dest.exists():
        shutil.rmtree(dest)
    tmp.rename(dest)


def cleanup_temp_files():
    """清理历史版本/异常退出遗留的下载与解压临时文件。"""
    if not PACKS_DIR.exists():
        return
    for p in PACKS_DIR.glob("*.tmp"):
        try:
            p.unlink()
        except Exception:
            pass
    for d in PACKS_DIR.glob("*.extracting"):
        try:
            shutil.rmtree(d)
        except Exception:
            pass


MIRROR_RELEASE = (
    "https://github.com/2465251326-netizen/live-subtitle"
    "/releases/download/offline-packs/"
)


def _download_stream(url, tmp_path, progress_cb=None):
    with requests.get(url, headers=HEADERS, stream=True, timeout=30,
                      proxies=net.proxies()) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length", 0))
        done = 0
        with open(tmp_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
                done += len(chunk)
                if progress_cb and total:
                    pct = int(done * 100 / total)
                    progress_cb(min(pct, 100))
    if total and tmp_path.stat().st_size != total:
        raise RuntimeError(ui_text("下载不完整，请重试"))


def install_pack(pack: PackInfo, progress_cb=None):
    PACKS_DIR.mkdir(parents=True, exist_ok=True)
    pair = f"{pack.from_code}_{pack.to_code}"
    model_path = PACKS_DIR / f"{pair}.argosmodel"
    tmp_path = model_path.with_suffix(".tmp")
    mirror_url = MIRROR_RELEASE + pack.url.rsplit("/", 1)[-1]
    last_err = None
    for url in [mirror_url, pack.url]:
        try:
            _download_stream(url, tmp_path, progress_cb)
            last_err = None
            break
        except Exception as e:
            last_err = e
            tmp_path.unlink(missing_ok=True)
            if progress_cb:
                progress_cb(0)
    if last_err:
        raise last_err

    dest = _pack_dir(pair)
    # v2.0.1：重装同方向包时，必须先清翻译器缓存再解压——
    # 运行中的 ctranslate2 握着旧包文件句柄，Windows 上 rmtree(dest) 会失败
    with _lock:
        stale = [k for k in _translator_cache if k[0] == pack.from_code]
        for key in stale:
            _translator_cache.pop(key, None)
            try:
                _cache_order.remove(key)
            except ValueError:
                pass
    try:
        _extract_pack(tmp_path, dest)
    except Exception:
        # 解压失败同样清理下载临时文件，避免 .tmp 残留
        tmp_path.unlink(missing_ok=True)
        raise
    meta = {
        "from_code": pack.from_code,
        "to_code": pack.to_code,
        "from_name": pack.from_name,
        "to_name": pack.to_name,
        "code": pack.code,
    }
    try:
        (dest / "metadata.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        # v2.0.3：metadata 写失败（磁盘满等）时清理已解压目录 + 下载临时文件，
        # 避免留下"无 metadata 的死目录"（list_installed 不显示但占磁盘）
        shutil.rmtree(dest, ignore_errors=True)
        tmp_path.unlink(missing_ok=True)
        raise
    tmp_path.unlink(missing_ok=True)
    app_log.log("argos.pack_installed", pair=pair)
    return dest


def _is_cjk(ch):
    code = ord(ch)
    return (
        0x2E80 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
        or 0xFE30 <= code <= 0xFE4F
        or 0xFF00 <= code <= 0xFFEF
        or 0x3000 <= code <= 0x303F
    )


def _detokenize(pieces):
    out = []
    for p in pieces:
        if p in ("</s>", "<s>", "<unk>", "<pad>"):
            continue
        space_before = p.startswith("▁")
        word = p[1:] if space_before else p
        if not word:
            continue
        if space_before and out:
            prev = out[-1]
            a, b = prev[-1], word[0]
            if not (_is_cjk(a) or _is_cjk(b) or b in ",.!?;:)\"'"):
                out.append(" ")
        out.append(word)
    return "".join(out)


# v2.20.1：离线乱码守卫——beam>2 时 CTranslate2 偶发选出"吐码位"的对齐假设。
# 真机英语新闻实测（beam 5、离线 argos en→zh、48 句）：2 句上屏为不可读串
# （"I don't know how" → "иぃ\ue01d笵"：西里尔+假名+私用区）。换 float32 复现出
# 另一串乱码，故与量化无关，是高 beam 搜索本身的退化假设。
# 判据只取"任何语言的正文里都不该出现的码位"（控制符 / 私用区 / 代理区 /
# 替换符 / 非字符），不判语种——避免误伤 zh→en 等拉丁方向的合法译文。
_JUNK_RANGES = (
    (0x00, 0x08), (0x0B, 0x0C), (0x0E, 0x1F), (0x7F, 0x9F),
    (0xE000, 0xF8FF), (0xFFF9, 0xFFFD), (0xFFFE, 0xFFFF),
    (0x1FFFE, 0x1FFFF), (0xD800, 0xDFFF),
)


def _has_junk(text):
    for ch in text:
        code = ord(ch)
        for lo, hi in _JUNK_RANGES:
            if lo <= code <= hi:
                return True
    return False


class PackTranslator:
    # beam 2 在实测 48 句里零乱码，且比 beam 5 快 ~1.4 倍——乱码时退回它重译
    SAFE_BEAM = 2

    def __init__(self, pack_dir: Path):
        import ctranslate2
        import sentencepiece as spm

        model_dir = pack_dir / "model"
        self.translator = ctranslate2.Translator(str(model_dir), device="cpu")
        self.sp = spm.SentencePieceProcessor()
        self.sp.load(str(pack_dir / "sentencepiece.model"))

    def translate(self, text, beam_size=2):
        text = text.strip()
        if not text:
            return ""
        chunks = _split_long(text)
        return "".join(self._translate_chunk(c, beam_size) for c in chunks)

    def _translate_chunk(self, text, beam_size=2):
        tokens = self.sp.encode(text, out_type=str)
        if not tokens:
            return text
        # v2.6.0（R4）：beam 每次调用传入（质量档热切换无需重建模型实例）；
        # 默认 2 保持 v2.5.3 速度语义
        out = self._decode_chunk(tokens, beam_size)
        # v2.20.1：见模块级乱码守卫注释——高 beam 偶发退化假设，退回安全档重译。
        # v2.20.2：退档改成**阶梯**（用户档 → 2 → 1）。旧实现 `beam_size != 2`
        # 让守卫在「快速」档下完全失效，而实测 beam 2 同样会吐乱码（int8 扫描里
        # "Good evening, welcome to the 9 o'clock World Report." 在 beam 2 就是
        # 一串 U+FFFD）——正好把一半用户挡在保护之外。beam 1（贪心）在实测里
        # 与 beam 2 一样干净，作为最后一级；三级都脏才认输。
        for try_beam in (self.SAFE_BEAM, 1):
            if not _has_junk(out):
                break
            if try_beam == beam_size:
                continue
            # 只记长度不记正文（v2.20.2：日志承诺不落字幕原文）
            app_log.log("argos.junk_retried", beam=beam_size, retry=try_beam,
                        chars=len(text))
            out = self._decode_chunk(tokens, try_beam)
        if out.count(",") > max(3, len(out) * 0.3) and len(out) > len(text):
            raise RuntimeError(ui_text("离线翻译输出异常，请重试或切换在线引擎"))
        return out

    def _decode_chunk(self, tokens, beam_size):
        res = self.translator.translate_batch(
            [tokens], max_batch_size=8, beam_size=beam_size
        )
        return _detokenize(res[0].hypotheses[0])


def _split_long(text, limit=400):
    if len(text) <= limit:
        return [text]
    # v2.20.2：零宽切分，**分隔符留在原段**（与 MyMemory._split_sentences 同一
    # 条规则）。旧写法 `replace(". ", ".|") + split("|")` 把句号后的空格吃掉了，
    # 而 `translate()` 用 "".join 拼回——实测 593 字符英文长文回拼少 17 个空格，
    # "…today. And then…" 变成 "…today.And then…"，整句被焊死后才送进模型。
    parts, buf = [], ""
    for seg in re.split(r"(?<=[。！？；])|(?<=[.!?])(?=\s)", text):
        if not seg:
            continue
        if len(buf) + len(seg) > limit and buf:
            parts.append(buf)
            buf = seg
        else:
            buf += seg
    if buf:
        parts.append(buf)
    # v2.0.3：对仍超限的段做字符级硬切（超长无标点段此前整段进模型）
    out = []
    for seg in parts:
        while len(seg) > limit:
            out.append(seg[:limit])
            seg = seg[limit:]
        if seg:
            out.append(seg)
    return out or [text[:limit]]


def _get_translator(source, target):
    key = (source, target)
    # 锁内只做缓存查询/登记；ctranslate2 模型加载（数秒）放锁外，
    # 避免与 remove_pack 的锁互抢冻结 UI 线程（v2.0.1）
    with _lock:
        if key in _translator_cache:
            _cache_order.remove(key)
            _cache_order.append(key)
            return _translator_cache[key]
        pack_dir = _resolve_pack_dir(source, target)
        if not (pack_dir / "sentencepiece.model").exists():
            return None
    tr = PackTranslator(pack_dir)
    with _lock:
        # 双检：加载期间可能已被 remove_pack 清场/重装替换
        if key not in _translator_cache:
            _translator_cache[key] = tr
            _cache_order.append(key)
            while len(_cache_order) > _CACHE_MAX:
                old = _cache_order.pop(0)
                _translator_cache.pop(old, None)
        else:
            _cache_order.remove(key)
            _cache_order.append(key)
        return _translator_cache.get(key, tr)


def translate(text, source, target, beam_size=2):
    tr = _get_translator(source, target)
    if tr is None:
        raise RuntimeError(f"{ui_text('离线语言包缺失: ')}{source}->{target}{ui_text('，请先在侧栏下载语言包')}")
    return tr.translate(text, beam_size=beam_size)
