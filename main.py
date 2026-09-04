import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import CONFIG_DIR

LOG_DIR = CONFIG_DIR / "logs"


def write_log(title, text):
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_DIR / "app.log", "a", encoding="utf-8") as f:
            f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {title}\n{text}\n")
    except Exception:
        pass


def install_crash_logger():
    def excepthook(exc_type, exc_value, exc_tb):
        write_log("未捕获异常", "".join(traceback.format_exception(exc_type, exc_value, exc_tb)))
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = excepthook
    try:
        from PySide6.QtCore import qInstallMessageHandler, QtMsgType

        def qt_handler(mode, context, message):
            if mode in (QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg):
                write_log("Qt 错误", message)

        qInstallMessageHandler(qt_handler)
    except Exception:
        pass


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
