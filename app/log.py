"""应用级结构化日志（v2.0.0）。

此前 app.log 只兜 Qt 致命消息与未捕获异常（且历史记录乱码），用户报障
时无任何生命周期信息可查。这里补齐：

- UTF-8 明确编码 + 轮转（1MB × 2 个备份防无限增长；v2.3.7 起跨天自动归档为
  app.log.YYYY-MM-DD，当前文件只留当天——多日混排曾困扰排障）
- 全局 logger.get()，各模块 logging.getLogger("ls.<module>") 共用同一 handler
- 生命周期日志：启动/退出、管线启停、模型加载耗时、引擎选择/切换、代理出口
- 不记录字幕正文（隐私），只记事件与耗时

QMessageBox 级 GUI 线程安全：log() 只是普通写文件，可在任意线程调用。
"""
import logging
import threading
import time

_handler_lock = threading.Lock()
_handler = None


class _Utf8RotatingHandler(logging.Handler):
    """带大小轮转的 UTF-8 文件 handler（避免 RotatingFileHandler 在 Windows
    上的编码/占用边角问题，自实现轻量轮转）。"""

    def __init__(self, path, max_bytes=1_000_000, backups=2):
        super().__init__()
        self._path = path
        self._max = max_bytes
        self._backups = backups
        # v2.3.7（P10）：None 迫使首次 emit 检查文件 mtime 的日期——
        # 跨天重启时旧 app.log 先归档为 app.log.YYYY-MM-DD 再写今天
        self._day = None

    def _roll_day(self):
        import os
        today = time.strftime("%Y-%m-%d")
        if self._day == today:
            return
        self._day = today
        try:
            if os.path.exists(self._path):
                d = time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(self._path)))
                if d != today:
                    dst = f"{self._path}.{d}"
                    if not os.path.exists(dst):
                        os.replace(self._path, dst)
        except Exception:
            pass

    def _rotate(self):
        try:
            import os
            if os.path.exists(self._path) and os.path.getsize(self._path) >= self._max:
                for i in range(self._backups - 1, 0, -1):
                    src, dst = f"{self._path}.{i}", f"{self._path}.{i + 1}"
                    if os.path.exists(src):
                        os.replace(src, dst)
                os.replace(self._path, f"{self._path}.1")
        except Exception:
            pass

    def emit(self, record):
        with _handler_lock:
            try:
                self._roll_day()
                self._rotate()
                with open(self._path, "a", encoding="utf-8", errors="replace") as f:
                    f.write(self.format(record) + "\n")
            except Exception:
                pass


def get(path=None):
    """初始化并返回全局日志器；重复调用安全（handler 只装一次）。"""
    global _handler
    logger = logging.getLogger("ls")
    logger.setLevel(logging.INFO)
    if _handler is None and path:
        with _handler_lock:
            if _handler is None:
                _handler = _Utf8RotatingHandler(str(path))
                _handler.setFormatter(
                    logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
                logger.addHandler(_handler)
    return logger


def rebind(path):
    """存储根迁移后改绑全局日志文件（v2.0.4）。

    此前 handler 在启动时固化到默认根：自定义存储根用户迁移后，
    崩溃/Qt 日志（main.write_log 动态取新 CONFIG_DIR）写新根，
    生命周期日志（本 handler）留旧根——同一份 app.log 分裂两处，
    排障漏看一半。relocate 后调用本函数统一改绑到新根。
    """
    global _handler
    target = str(path)
    with _handler_lock:
        if _handler is not None and str(getattr(_handler, "_path", "")) == target:
            return
        try:
            import os
            os.makedirs(os.path.dirname(target), exist_ok=True)
        except Exception:
            pass
        logger = logging.getLogger("ls")
        if _handler is not None:
            try:
                logger.removeHandler(_handler)
            except Exception:
                pass
        handler = _Utf8RotatingHandler(target)
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logger.addHandler(handler)
        _handler = handler


def log(event, **fields):
    """便捷入口：结构化事件一行流（值统一 str()，防 None/Path 拼接报错）。"""
    if fields:
        extra = " | " + " ".join(f"{k}={v}" for k, v in fields.items())
    else:
        extra = ""
    get().info(f"{event}{extra}")


def exception(event, exc, **fields):
    """异常事件：类型 + 消息一行流。"""
    log(event, error=f"{type(exc).__name__}: {exc}", **fields)
