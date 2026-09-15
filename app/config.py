import json
import os
import threading
from pathlib import Path

APP_NAME = "LiveSubtitle"
APP_VERSION = "2.13.0"

CONFIG_DIR = Path(os.environ.get("LIVETRANSLATE_HOME", Path.home() / ".live_subtitle"))
CONFIG_FILE = CONFIG_DIR / "config.json"
CACHE_FILE = CONFIG_DIR / "trans_cache.json"
HF_HOME = CONFIG_DIR / "hf"
ARGOS_DATA = CONFIG_DIR / "argos"
# v2.0.1：启动"指针"快照——storage_root 的唯一可信来源。
# v2.7.4（A-1）：指针语义收敛为「只存 storage_root 单键」且 save 时全量回写——
# 旧实现指针是全量快照、只在 relocate 时写一次，运行期更新只落新根：
# 重启读旧指针 → 设置回滚 + relocate 把旧快照反写新根摧毁最新配置。
POINTER_CONFIG_FILE = CONFIG_FILE

DEFAULTS = {
    "source_type": "system",          # system | microphone
    "device_index": -1,               # -1 = 默认设备
    "device_name": "",                # v2.0.6：设备名（热插拔后索引漂移，按名回查）
    "asr_model": "small",             # tiny | base | small | medium
    "asr_device": "cpu",              # cpu | cuda | auto
    "asr_language": "auto",           # auto | en | ja | ko ...
    "engine": "auto",                 # auto | google | mymemory | argos
    "engine_auto_fallback": True,     # v2.7.1：引擎失败时自动切换备援（在线互备→离线包）；
                                      # 关=固定用当前引擎，失败只报错不换（离线包用户/想稳定复现者用）
    "target_lang": "zh-CN",
    "show_source": True,
    "overlay_layout": "list",          # v2.11.0：字幕面板布局——"list"=历史滚动列表
                                        # （v2.4.0 起的形态）；"dual"=上下双语（豆包风：
                                        # 上半原文随识别流式生长、下半译文随推测式翻译就地更新）。
                                        # 面板 ⋯ 菜单随时切换，设置页保存即时生效。
    "stream_preview": True,            # v2.12.0：dual 布局的流式原文通道——每 0.9s 把
                                        # 最近 4s 音频重识别一次，把新增话音实时追加到
                                        # 原文区（主持人讲到哪原文跟到哪，不等分段周期）。
                                        # 仅 dual 布局 + GPU（cuda）时实际启用：CPU 上单次
                                        # 推理要数秒、反而拖垮正式识别（闸门在主窗）。
    "max_history": 200,
    "overlay_enabled": True,
    "overlay_x": 200,
    "overlay_y": 200,
    "overlay_font_size": 18,
    "overlay_text_color": "#ffffff",
    "overlay_bg_color": "#1c1f26",      # v2.4.0：面板默认深灰（旧玻璃黑随描边一起退役）
    "overlay_bg_opacity": 92,           # 0-100，面板背景不透明度百分比
    # v2.4.0 删除 overlay_outline/_width/_color：描边是透明玻璃时代的可读性补丁
    "instant_caption": True,           # v2.1.5：流式两段式——原文先上屏，译文就绪后补齐
    # v2.6.0（R6）删除 translate_zh_from_zh：无任何读取点的死配置（zh→zh 回显硬编码于 translator.run）
    "close_action": "ask",             # ask / tray / exit
    "auto_start": False,               # 启动后自动开始翻译
    "proxy_mode": "system",            # system 跟随系统 | manual 手动 | none 直连
    "proxy_url": "",                   # manual 模式的代理地址，如 http://127.0.0.1:10808
    "hotkey_enabled": True,            # 全局热键开关
    "hotkey_sequence": "Ctrl+Alt+S",   # 全局热键组合（开始/停止翻译）
    "hotkey_overlay": "Ctrl+Alt+O",    # v2.2.6：全局热键（显隐悬浮条，留空禁用）
    "storage_root": "",                # 自定义数据根目录（空 = 默认 ~\.live_subtitle）
    "wizard_done": False,              # 首次运行向导已完成
    "hallucination_filter": True,      # 幻觉抑制：过滤音乐/噪声段的胡言乱语
    "asr_accuracy": "fast",            # v2.5.4：识别精度档 fast/balanced/quality（beam与上下文条件取舍；低延迟场景建议 fast）
    "silero_vad": True,                # Silero VAD：faster-whisper 内置，段内非语音再过滤
    "low_latency_mode": True,          # v2.3.3（P1）：低延迟分段（6s 上限+收紧判停）；
                                       # v2.5.4 默认开——看视频字幕对延迟敏感（连续语流
                                       # 普通模式攒到 14s 才切句，实测感知"太慢"的主因）
    "early_flush": True,               # v2.7.0（T2）：低延迟提前冲句——攒句静默地板 3.5s→2.0s
                                       # （仅低延迟模式生效；榨干模式再压到 1.2s）
    "perf_turbo": False,               # v2.7.2：榨干模式——GPU INT8 推理 + 进程高优先级 +
                                        # 分段上限 6s→4s + 冲句地板 2.0s→1.2s（捆绑开关，默认关=一切照旧）
    # v2.7.6：延迟优化批（用户反馈"翻译速度还是太慢"）。遥测实锤定位：
    # hold_p50≈4.13s（连续语流真机复现 5.03s），而识别 reco_p50=0.55s +
    # 翻译 tr_p50=0.06s 合计只占 13%——瓶颈是攒句两轨制"等下一片冲刷"的
    # 结构性等待；根因是句长超过分段上限被强制切段（**不是 VAD 找不到停顿**：
    # 短句+背景乐实测能量判据 15 句切 15 段、零硬切，已是最优）。
    # 附带结论：曾实现"Silero 神经 VAD 句末判定"，实测零收益（纯净素材与
    # 能量判据持平；稳定背景乐下滞回反而黏 0.66s），v2.8.0 回退；
    # v2.9.0 应**用户要求**恢复为默认关的实验开关（见下方 neural_vad），
    # 详见 app/audio/capture.py Segmenter.feed 的实测留档与 HANDOFF 十三/十四节。
    "spec_translate": True,            # 推测式增量翻译：碎片一到达就把"当前已攒文本"送翻译并上屏，
                                        # 下一片到达时送更长版本、译文在同一张卡上原地生长覆盖。
                                        # 连续语流真机 A/B：译文感知延迟 hold 5.03s + tr 0.09s
                                        # → spec_p50 0.09s（56.9 倍），识别侧零副作用。
                                        # **仅离线引擎生效**（无额度限制、单次 0.06s）；在线引擎自动
                                        # 退回整句翻译，避免 Google/MyMemory 请求量翻倍被限流。
    "segment_cap_s": 4.0,              # 连续语流强制切段上限（秒）。0=跟随模式内置值
                                        # （低延迟 6s / 榨干 4s / 普通 14s）。默认 4s=与榨干档一致：
                                        # 真实素材 A/B 实测 2.5s 会让 57~71% 的句子在上限处被
                                        # 硬生生腰斩（4s 档 0~17%），而推测式增量翻译已让译文随
                                        # 碎片立即上屏，上限大小对"译文迟到"影响很小——
                                        # 故激进档只作可选项，不作默认。
    "neural_vad": False,               # v2.9.0：Silero 神经 VAD 句末判定（实验开关，默认关）。
                                        # 用户要求保留。默认关的理由：实测纯净素材与能量判据
                                        # 持平、稳定背景乐下滞回反而黏 0.66s；仅"突发强背景乐/
                                        # 噪声盖过语音"场景值得一试。开销 ~0.4% CPU，
                                        # 加载失败自动静默退回能量判据。
    "asr_hotwords": "",                # v2.7.0（T3）：热词提示——人名/专名/术语注入 whisper
                                       # initial_prompt，事前纠正专名误听（留空=关闭）
    "lang_recheck": True,              # v2.7.0（T5）：语言锁复检——自动模式下每 20 段解除
                                       # 锁定重听一次，高置信不一致才切换（防错锁终身）
    "prewarm_model": True,             # v2.3.5（P5）：启动即后台预热已下载模型，消除"开始翻译"后近 1 分钟冷加载
    "mishear_map": {},                 # 误听修正词典 {错: 对}；匹配方式随 fix_whole_word（拉丁整词/其余子串）
    "translate_fix_map": {},           # v2.3.6（P7）：译文修正词典 {错译: 正解}，对翻译结果精确替换
    "fix_whole_word": True,            # v2.6.0（R2）：词典全词匹配——纯拉丁词条整词替换，
                                       # 多义词（strikes=罢工）不再误伤专名；CJK 词条始终子串替换
    "offline_quality": "high",         # v2.6.0（R4）：离线翻译质量档 fast(beam 2)/high(beam 5)，
                                       # 高质量译文更连贯、单句离线耗时略增
    "overlay_w": 0,                    # v2.1.8：面板手动调整的宽度（0 = 自动）；高度永远贴内容
    "overlay_pin": True,               # v2.4.0：面板置顶显示（⋯ 菜单可切）
    "overlay_collapsed": False,        # v2.4.0：面板收起态（只留工具条）
    "overlay_h": 0,                    # v2.5.3：手动高度回归（拖底缘拉长后锁定；0=自动贴内容）
    "overlay_hint_shown": False,       # v2.4.3：面板手势引导只弹一次（首次显示面板后置 True）
    # v2.4.0 删除：overlay_list_mode/overlay_list_max/overlay_stream（面板天生历史滚动）、
    # overlay_click_through（不透明板无空区）、
    # overlay_outline/_width/_color（描边是透明玻璃时代的可读性补丁，面板不需要）
    # （overlay_h 曾随透明玻璃退役，v2.5.3 手动高度回归后重新启用——见上）
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
        # v2.7.4（A-1）：指针只存单键即刻回写；**先读回新根最新版再统一保存**——
        # 顺序反了会用指针旧快照覆盖新根运行期配置（load 在 save 前）
        self._write_pointer()
        self.load()
        self.save()
        try:
            from app.translate import offline_pack as _op
            _op.PACKS_DIR = ARGOS_DATA / "packs"
        except Exception:
            pass
        # v2.0.4：生命周期日志跟随新根——此前 log handler 固化在启动时的
        # 默认根，迁移后与 write_log（动态取 CONFIG_DIR）分裂两处
        try:
            from app import log as _app_log
            _app_log.rebind(CONFIG_DIR / "logs" / "app.log")
        except Exception:
            pass

    @staticmethod
    def _coerce(key, val):
        """v2.7.4（A-2）：手编配置逐键消毒——类型不符按 DEFAULTS 原型挽救或丢弃。
        零校验时代的实锤事故（全量测试活体复现）："overlay_font_size":"18px"
        启动即崩；"max_history":"200" 运行中崩；词典值设 list → 每段抛全场零字幕。
        v2.7.5（R-5）：负数无意义（负尺寸/负条数）——int 挽救链对负值回默认原型；
        超大值属用户意愿不拦（UI 控件仍会 clamp 显示）。"""
        proto = DEFAULTS.get(key)
        try:
            if isinstance(proto, bool):
                return val if isinstance(val, bool) else proto
            if isinstance(proto, int):
                if isinstance(val, bool):
                    return proto
                if isinstance(val, int):
                    return val if val >= 0 else proto
                if isinstance(val, float):
                    coerced = int(val)
                    return coerced if coerced >= 0 else proto
                if isinstance(val, str):
                    return int(float(val.strip()))   # "200" 挽救；"18px" 抛→原型
                return proto
            if isinstance(proto, str):
                return val if isinstance(val, str) else proto
            if isinstance(proto, float):
                # v2.7.6：浮点键（分段上限秒数）同享消毒——手编 "2.5s"/负数
                # 一律回默认原型，不再原样放行（旧实现无 float 分支=零校验）
                if isinstance(val, bool):
                    return proto
                if isinstance(val, (int, float)):
                    return float(val) if val >= 0 else proto
                if isinstance(val, str):
                    return float(val.strip())   # "2.5" 挽救；"2.5s" 抛→原型
                return proto
            if isinstance(proto, dict):
                if isinstance(val, dict) and all(
                        isinstance(k, str) and isinstance(v, str) for k, v in val.items()):
                    return val
                return proto
            if isinstance(proto, list):
                return val if isinstance(val, list) else proto
            return val
        except Exception:
            return proto

    def load(self):
        if CONFIG_FILE.exists():
            try:
                # utf-8-sig 兼容手工编辑（如记事本）可能带入的 BOM
                with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
                    saved = json.load(f)
                if not isinstance(saved, dict):
                    raise ValueError("config root not object")
                for k in self._data:
                    if k in saved:
                        self._data[k] = self._coerce(k, saved[k])
            except Exception:
                # v2.0.1：损坏配置不再无声吞掉——改名留存供排查/恢复，
                # 否则 storage_root 丢失会让已下载模型被判未缓存全部重下
                try:
                    bad = CONFIG_FILE.with_suffix(".json.bad")
                    os.replace(CONFIG_FILE, bad)
                    from app import log as app_log
                    app_log.log("config.corrupt_kept_as_bad", path=str(bad))
                except Exception:
                    pass

    def _write_pointer(self):
        """v2.7.4（A-1）：指针文件只存 storage_root 单键，且每次 save 全量同步——
        旧实现的全量快照只在 relocate 时写一次，运行期脱节后重启回滚全部设置，
        且 relocate 会把这份旧快照反灌新根摧毁最新配置。"""
        try:
            pointer = POINTER_CONFIG_FILE
            try:
                if pointer.resolve() == CONFIG_FILE.resolve():
                    return          # 未迁移：CONFIG_FILE 本身就是指针，全量写即可
            except Exception:
                pass
            pointer.parent.mkdir(parents=True, exist_ok=True)
            tmp = pointer.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"storage_root": str(CONFIG_DIR)}, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, pointer)
        except Exception:
            pass

    def save(self):
        try:
            # 先写临时文件再原子替换，避免写一半崩溃/断电导致配置损坏；
            # v2.0.1：replace 前加 fsync（掉电时 replace 的元数据可能已落盘
            # 而内容未落盘），失败时清理 tmp 残留
            tmp = CONFIG_FILE.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, CONFIG_FILE)
            self._write_pointer()   # v2.7.4（A-1）：指针随每次保存同步，永不过期
        except Exception:
            try:
                CONFIG_FILE.with_suffix(".json.tmp").unlink(missing_ok=True)
            except Exception:
                pass

    def get(self, key):
        return self._data.get(key, DEFAULTS.get(key))

    def set(self, key, value):
        self._data[key] = value
        self.save()
        if key in ("proxy_mode", "proxy_url"):
            self._sync_proxy()
