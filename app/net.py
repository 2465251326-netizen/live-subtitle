"""网络代理提供者。

统一解析代理设置，供全部 requests 调用点使用：
- system：读取 Windows 系统代理（WinINET 注册表，v2rayN/Clash 等开启
  「自动配置系统代理」即写入此处）。requests 不会自动读取这部分设置，
  这是国内用户 Google 通道不可达的根因，这里显式补齐。
- manual：用户手动填写的 http://host:port。
- none：显式直连（同时屏蔽环境变量里的代理，行为可预期）。

proxies() 返回值语义（requests 官方约定）：
- {"http": url, "https": url} —— 走该代理
- {"http": None, "https": None} —— 请求级显式覆盖，绕过环境变量直连
"""
import threading

_lock = threading.Lock()
_state = {"mode": "system", "url": ""}


def configure(mode, url=""):
    """由配置加载/修改处调用，刷新全局代理状态。"""
    with _lock:
        _state["mode"] = mode if mode in ("system", "manual", "none") else "system"
        _state["url"] = (url or "").strip()


def system_proxy_url():
    """读取 Windows 系统代理；未启用或读取失败返回 None。"""
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
        # ProxyServer 可能是 "host:port"，也可能是分协议串 "http=...;https=..."
        if server and "=" in server:
            parts = dict(p.split("=", 1) for p in server.split(";") if "=" in p)
            server = parts.get("http") or parts.get("https") or ""
        server = (server or "").strip()
        if server and "://" not in server:
            server = "http://" + server
        return server or None
    except Exception:
        return None


def proxies():
    """返回可直接传给 requests 的 proxies 参数。"""
    with _lock:
        mode, url = _state["mode"], _state["url"]
    if mode == "manual" and url:
        if "://" not in url:
            url = "http://" + url
        return {"http": url, "https": url}
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
    return http if http else "直连"


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
