"""网络代理提供者 + 共享 HTTP 头。

统一解析代理设置，供全部 requests 调用点使用：
- system：读取 Windows 系统代理（WinINET 注册表，v2rayN/Clash 等开启
  「自动配置系统代理」即写入此处）。requests 不会自动读取这部分设置，
  这是国内用户 Google 通道不可达的根因，这里显式补齐。
- manual：用户手动填写的 http://host:port。
- none：显式直连（同时屏蔽环境变量里的代理，行为可预期）。

proxies() 返回值语义（requests 官方约定）：
- {"http": url, "https": url} —— 走该代理
- {"http": None, "https": None} —— 请求级显式覆盖，绕过环境变量直连

v2.0.0：注册表读取加 3 秒 TTL 缓存（翻译每条都读注册表没必要）；
识别「仅有 SOCKS 代理」的场景并给出可读提示（此前会静默直连）。
"""
import threading
import time

_lock = threading.Lock()
_state = {"mode": "system", "url": ""}

# ---------- 共享 HTTP 头（v2.0.0 收敛：此前 translator/offline_pack 各写一份且 UA 不一致） ----------

# 浏览器 UA：访问对自动化请求更敏感的公共翻译接口（Google / MyMemory）
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}

# 应用 UA：访问无所谓的开源基础设施（argospm 索引 / GitHub raw），可识别便于对方统计
APP_HEADERS = {"User-Agent": "Mozilla/5.0 LiveSubtitle/2.0"}

# ---------- 系统代理读取（带 TTL 缓存） ----------

_PROXY_TTL = 3.0
_proxy_cache = {"at": 0.0, "url": None, "note": ""}


def configure(mode, url=""):
    """由配置加载/修改处调用，刷新全局代理状态。"""
    with _lock:
        _state["mode"] = mode if mode in ("system", "manual", "none") else "system"
        _state["url"] = (url or "").strip()
    _proxy_cache["at"] = 0.0  # 配置变更立即失效缓存


def _read_registry_proxy():
    """直读 WinINET 注册表，返回 (server 原文或 None)。"""
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        )
        enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
        if not enable:
            return None
        server, _ = winreg.QueryValueEx(key, "ProxyServer")
        return server or None
    except Exception:
        return None


def _parse_proxy_server(server):
    """解析 ProxyServer 原文 → (http 代理 URL 或 None, 提示文本)。

    兼容三种形态：
    - "host:port"
    - "http=host:port;https=host:port"
    - 仅 "socks=host:port"（v2rayN 部分配置）：requests 不能走 socks，
      此前会静默直连，现在显式提示用户改「手动指定」填 http 端口。
    """
    if not server:
        return None, ""
    if "=" in server:
        parts = dict(p.split("=", 1) for p in server.split(";") if "=" in p)
        cand = parts.get("http") or parts.get("https") or ""
        if cand:
            return _with_scheme(cand.strip()), ""
        if parts.get("socks"):
            return None, "检测到 SOCKS 代理：翻译通道需要 http 代理端口（如 http://127.0.0.1:10809），请改用「手动指定」填写"
        return None, ""
    return _with_scheme(server.strip()), ""


def _with_scheme(server):
    return server if "://" in server else "http://" + server


def system_proxy_info(force=False):
    """返回 (http 代理 URL 或 None, 提示文本)；带 3 秒 TTL 缓存。"""
    with _lock:  # v2.0.3：检查-写入原子化，防止与 configure 的失效竞态
        now = time.time()
        if not force and now - _proxy_cache["at"] < _PROXY_TTL:
            return _proxy_cache["url"], _proxy_cache["note"]
        server = _read_registry_proxy()
        url, note = _parse_proxy_server(server) if server else (None, "")
        _proxy_cache.update(at=now, url=url, note=note)
        return url, note


def system_proxy_url():
    """读取 Windows 系统代理；未启用或读取失败返回 None。"""
    return system_proxy_info()[0]


def proxies():
    """返回可直接传给 requests 的 proxies 参数。"""
    with _lock:
        mode, url = _state["mode"], _state["url"]
    if mode == "manual":
        if url:
            if "://" not in url:
                url = "http://" + url
            return {"http": url, "https": url}
        # v2.0.3：手动模式忘填地址时显式直连（此前静默落入"跟随系统"分支，
        # 与用户显式选择相反且难以察觉）
        return {"http": None, "https": None}
    if mode == "none":
        return {"http": None, "https": None}
    sys_url = system_proxy_url()
    if sys_url:
        return {"http": sys_url, "https": sys_url}
    # 跟随系统且系统未开代理 → 直连并屏蔽环境变量，保证"跟随系统"语义可预期
    return {"http": None, "https": None}


def describe():
    """给状态栏/设置页展示的简短描述。"""
    p = proxies()
    http = (p or {}).get("http")
    if http:
        return http
    _, note = system_proxy_info()
    return note or "直连"


def apply_proxy_env():
    """把代理策略同步到进程环境变量（HTTP_PROXY/HTTPS_PROXY）。

    faster-whisper 的模型下载走 huggingface_hub，它只认环境变量，
    不接收 requests 的 proxies 参数；这里统一覆盖，保证模型下载
    与翻译请求遵循同一代理策略（应用设置优先级最高）。
    """
    import os

    p = proxies()
    http = (p or {}).get("http")
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        if http:
            os.environ[name] = http
        else:
            os.environ.pop(name, None)
