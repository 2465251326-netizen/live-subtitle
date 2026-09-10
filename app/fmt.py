"""通用格式化小工具（v2.2.14：下载 ETA 展示共用）。"""


def eta_text(seconds):
    """剩余时间口语化：<60 秒→"X 秒"，<1 小时→"X 分钟"，以上→"X 小时 Y 分"。

    无效输入（None/负数/NaN）返回空串——调用方据此省略 ETA 段。"""
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return ""
    if s != s or s < 0 or s > 86400 * 7:   # NaN/负值/超过一周视为不可信
        return ""
    if s < 60:
        return f"{int(round(s))} 秒"
    if s < 3600:
        return f"{int(round(s / 60))} 分钟"
    return f"{int(s // 3600)} 小时 {int(round(s % 3600 / 60))} 分"
