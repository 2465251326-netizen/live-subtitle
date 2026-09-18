import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import CONFIG_DIR

LOG_DIR = CONFIG_DIR / "logs"
LOG_FILE = LOG_DIR / "app.log"


def write_log(title, text):
    try:
        # v2.0.1：动态取 CONFIG_DIR——此前在 import 时固化，存储根迁移后
        # 日志继续写到旧位置直至重启
        from app.config import CONFIG_DIR as _cfg_dir
        log_dir = _cfg_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        # v2.20.4：崩溃/Qt 兜底日志与结构化日志写的是**同一个文件**，README 又让
        # 用户把 app.log 贴到公开 Issues —— 脱敏必须同样生效。旧实现只有
        # app/log.py 那条路脱敏，未捕获异常里带的 `q=<整句字幕>` 与
        # `http://user:pass@proxy` 会原样落盘。
        try:
            from app.log import _redact as _rd
        except Exception:
            def _rd(x):
                return x
        with open(log_dir / "app.log", "a", encoding="utf-8") as f:
            f.write("\n[%s] %s\n%s\n"
                    % (time.strftime("%Y-%m-%d %H:%M:%S"), title, _rd(str(text))))
    except Exception:
        pass


def install_crash_logger():
    # 结构化生命周期日志（v2.0.0）：UTF-8 + 轮转，全局 logger("ls") 共用
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        from app import log as app_log
        app_log.get(str(LOG_FILE))
        app_log.log("app.start", version=_app_version())
    except Exception:
        pass

    def excepthook(exc_type, exc_value, exc_tb):
        write_log("未捕获异常", "".join(traceback.format_exception(exc_type, exc_value, exc_tb)))
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = excepthook
    # v2.19.2：`sys.excepthook` 只管主线程——采集/识别/翻译/预览线程里未捕获的
    # 异常默认只打到 stderr（打包版无人看），线程整条退出后 app.log 零证据，
    # 用户观感＝"字幕突然不再出现"，排查只能靠猜。子线程异常走
    # threading.excepthook（3.8+），这里补同一份落盘。
    def _thread_excepthook(args):
        write_log(
            "工作线程未捕获异常",
            "".join(traceback.format_exception(args.exc_type, args.exc_value,
                                               args.traceback))
            + " | thread=%s" % (args.thread.name if args.thread else "?"))

    try:
        import threading
        threading.excepthook = _thread_excepthook
    except Exception:
        pass
    try:
        from PySide6.QtCore import qInstallMessageHandler, QtMsgType

        def qt_handler(mode, context, message):
            if mode in (QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg):
                write_log("Qt 错误", message)

        qInstallMessageHandler(qt_handler)
    except Exception:
        pass


def _app_version():
    try:
        from app.config import APP_VERSION
        return APP_VERSION
    except Exception:
        return "?"


if __name__ == "__main__":
    install_crash_logger()
    try:
        from app.ui.main_window import run_app
        run_app()
    except SystemExit:
        raise
    except Exception:
        write_log("启动失败", traceback.format_exc())
        raise
