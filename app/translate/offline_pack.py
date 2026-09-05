"""离线翻译语言包管理。

.argosmodel 包内即为 CTranslate2 模型 + sentencepiece 词表，
直接加载推理，无需 argostranslate / torch 依赖。
"""
import json
import shutil
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

import requests

from app.config import CONFIG_DIR

PACKS_DIR = CONFIG_DIR / "argos" / "packs"

INDEX_SOURCES = [
    "https://raw.githubusercontent.com/argosopentech/argospm-index/main/index.json",
    "https://cdn.jsdelivr.net/gh/argosopentech/argospm-index@main/index.json",
]

HEADERS = {"User-Agent": "Mozilla/5.0 LiveSubtitle/1.0"}

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
    now = time.time()
    if use_cache and _index_cache["packs"] and now - _index_cache["at"] < _INDEX_TTL:
        return _index_cache["packs"]
    last_err = None
    for src in INDEX_SOURCES:
        try:
            r = requests.get(src, headers=HEADERS, timeout=timeout)
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
                _index_cache["at"] = time.time()
                _index_cache["packs"] = packs
                return packs
        except Exception as e:
            last_err = e
    raise RuntimeError(f"语言包索引获取失败: {last_err}")


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


def _extract_pack(model_path: Path, dest: Path):
    with zipfile.ZipFile(model_path) as zf:
        names = zf.namelist()
        inner_root = names[0].split("/")[0]
        tmp = dest.with_suffix(".extracting")
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True)
        for name in names:
            if name.startswith(inner_root + "/") and name != inner_root + "/":
                rel = name[len(inner_root) + 1:]
                target = tmp / rel
                if name.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(name) as fsrc, open(target, "wb") as fdst:
                        shutil.copyfileobj(fsrc, fdst)
    if dest.exists():
        shutil.rmtree(dest)
    tmp.rename(dest)


MIRROR_RELEASE = (
    "https://github.com/2465251326-netizen/live-subtitle"
    "/releases/download/offline-packs/"
)


def _download_stream(url, tmp_path, progress_cb=None):
    with requests.get(url, headers=HEADERS, stream=True, timeout=30) as r:
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
        raise RuntimeError("下载不完整，请重试")


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
    _extract_pack(tmp_path, dest)
    meta = {
        "from_code": pack.from_code,
        "to_code": pack.to_code,
        "from_name": pack.from_name,
        "to_name": pack.to_name,
        "code": pack.code,
    }
    (dest / "metadata.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )
    tmp_path.unlink(missing_ok=True)
    with _lock:
        for key in [k for k in _translator_cache if k[0] == pack.from_code]:
            _translator_cache.pop(key, None)
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


class PackTranslator:
    def __init__(self, pack_dir: Path):
        import ctranslate2
        import sentencepiece as spm

        model_dir = pack_dir / "model"
        self.translator = ctranslate2.Translator(str(model_dir), device="cpu")
        self.sp = spm.SentencePieceProcessor()
        self.sp.load(str(pack_dir / "sentencepiece.model"))

    def translate(self, text):
        text = text.strip()
        if not text:
            return ""
        chunks = _split_long(text)
        return "".join(self._translate_chunk(c) for c in chunks)

    def _translate_chunk(self, text):
        tokens = self.sp.encode(text, out_type=str)
        if not tokens:
            return text
        res = self.translator.translate_batch(
            [tokens], max_batch_size=8, beam_size=2
        )
        out = _detokenize(res[0].hypotheses[0])
        if out.count(",") > max(3, len(out) * 0.3) and len(out) > len(text):
            raise RuntimeError("离线翻译输出异常，请重试或切换在线引擎")
        return out


def _split_long(text, limit=400):
    if len(text) <= limit:
        return [text]
    parts, buf = [], ""
    for seg in text.replace("。", "。|").replace(". ", ".|").split("|"):
        if len(buf) + len(seg) > limit and buf:
            parts.append(buf)
            buf = seg
        else:
            buf += seg
    if buf:
        parts.append(buf)
    return parts


def _get_translator(source, target):
    key = (source, target)
    with _lock:
        if key in _translator_cache:
            _cache_order.remove(key)
            _cache_order.append(key)
            return _translator_cache[key]
        pack_dir = _resolve_pack_dir(source, target)
        if not (pack_dir / "sentencepiece.model").exists():
            return None
        tr = PackTranslator(pack_dir)
        _translator_cache[key] = tr
        _cache_order.append(key)
        while len(_cache_order) > _CACHE_MAX:
            old = _cache_order.pop(0)
            _translator_cache.pop(old, None)
        return tr


def translate(text, source, target):
    tr = _get_translator(source, target)
    if tr is None:
        raise RuntimeError(f"离线语言包缺失: {source}->{target}，请先在侧栏下载语言包")
    return tr.translate(text)
