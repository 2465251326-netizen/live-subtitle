"""面向用户的友好错误映射（v2.0.0）。

此前原始英文异常（ONNXRuntime NO SUCH FILE、urllib 超时、HTTPError）
直接怼到状态栏/悬浮条上。这里统一翻译成「中文结论 + 下一步动作」，
无法识别的兜底给出摘要而不是整坨堆栈。
"""
import re

_PATTERNS = [
    # (正则, 中文结论)
    (r"NO SUCH FILE|FileNotFoundError", "本地组件/模型文件缺失"),
    (r"onnxruntime", "Silero VAD 组件加载失败"),
    (r"429", "接口限流（同一出口 IP 请求过频）"),
    (r"(?i)timed?\s*out|timeout", "网络超时"),
    (r"ProxyError|Cannot connect to proxy", "代理不可达"),
    (r"SSLError|CERTIFICATE_VERIFY", "网络证书校验失败（代理/防火墙劫持？）"),
    (r"ConnectionError|Connection refused|Connection reset|NameResolutionError|getaddrinfo",
     "网络连接失败"),
    (r"PermissionError|Permission denied", "权限不足"),
    (r"MemoryError|CUDA out of memory", "内存/显存不足"),
    (r"cublas|cudnn|cuda", "CUDA 运行环境异常"),
]

_ACTIONS = {
    "本地组件/模型文件缺失": "请删除该模型后重新下载（设置→语音识别→管理），或更新安装包",
    "Silero VAD 组件加载失败": "可在设置→语音识别关闭「Silero VAD」后重试，或更新安装包",
    "接口限流（同一出口 IP 请求过频）": "稍候重试或更换网络/代理节点，期间将自动使用备援引擎",
    "网络超时": "检查网络与代理；若使用代理请确认端口正确",
    "代理不可达": "确认代理软件在运行、端口正确，或改用「不使用代理」",
    "网络连接失败": "检查网络连通性与 DNS；使用代理时确认节点可用",
    "网络证书校验失败（代理/防火墙劫持？）": "检查代理/安全软件的 HTTPS 拦截设置",
    "权限不足": "以管理员运行或检查目录权限",
    "内存/显存不足": "换更小的识别模型，或改用 CPU 模式",
    "CUDA 运行环境异常": "更新 NVIDIA 驱动，或改用 CPU 模式",
}


def friendly_message(text: str) -> str:
    """把任意错误文本（未必是异常对象）映射为友好提示。

    与 friendly_error 的区别：识别不到任何已知模式时**原样返回**——
    管线内部很多错误消息本身就是中文，不应被二次包装。
    """
    if not text:
        return text
    for pattern, conclusion in _PATTERNS:
        if re.search(pattern, str(text), re.IGNORECASE):
            action = _ACTIONS.get(conclusion, "")
            return f"{text}（{conclusion}；{action}）" if str(text) != conclusion else f"{text}（{action}）"
    return str(text)


def friendly_error(exc) -> str:
    """把任意异常转成一行中文：结论 + 下一步动作。"""
    if exc is None:
        return "未知错误"
    text = f"{type(exc).__name__}: {exc}"
    for pattern, conclusion in _PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            action = _ACTIONS.get(conclusion, "")
            return f"{conclusion}（{action}）"
    # 兜底：摘要原文，避免一坨堆栈
    return f"错误：{str(exc)[:100]}"
