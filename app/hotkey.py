"""Windows 全局热键（基于 user32.RegisterHotKey，无需额外依赖）。

- install(app, callback)：装一次全局原生事件过滤器（应用生命周期内）。
- register(hwnd, seq_str)：注册热键组合；成功后按键触发 callback。
- unregister()：注销（窗口销毁/退出前必须调用）。
- current_text()：当前生效的组合键文本（托盘菜单展示用）。

安全约束：不带修饰键的组合（如单按 S）直接拒绝注册，避免全局劫持打字。
"""
import ctypes
from ctypes import wintypes

from PySide6.QtCore import Qt, QAbstractNativeEventFilter
from PySide6.QtGui import QKeySequence

WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
HOTKEY_ID = 0x4C53  # "LS"

_callback = None
_filter = None
_hwnd = None
_registered = False
_current_text = ""


def _mod_flags(mods):
    m = 0
    if mods & Qt.ControlModifier:
        m |= MOD_CONTROL
    if mods & Qt.AltModifier:
        m |= MOD_ALT
    if mods & Qt.ShiftModifier:
        m |= MOD_SHIFT
    if mods & Qt.MetaModifier:
        m |= MOD_WIN
    return m


def sequence_to_hotkey(seq_str):
    """把 "Ctrl+Alt+S" 解析为 (win32_mods, vk_code)。

    支持：Ctrl/Alt/Shift/Win + 字母、数字、F1-F24。
    不支持或缺少修饰键时返回 None。
    """
    if not seq_str:
        return None
    try:
        import re

        # QKeySequence 不认识 "Win+" 前缀，Qt 的规范名是 Meta+；归一化常见别名
        s = re.sub(r"(?i)\b(win|windows|cmd|command)\+", "Meta+", str(seq_str).strip())
        seq = QKeySequence(s)
        if seq.count() != 1:
            return None
        combo = seq[0]
        key = combo.key()
        m = _mod_flags(combo.keyboardModifiers())
        if m == 0:
            return None
        ki = int(key)
        if 0x41 <= ki <= 0x5A or 0x30 <= ki <= 0x39:  # A-Z / 0-9（Qt 与 Win32 同值）
            vk = ki
        elif Qt.Key_F1 <= key <= Qt.Key_F24:
            vk = 0x70 + (ki - int(Qt.Key_F1))
        else:
            return None
        return m, vk
    except Exception:
        return None


class _HotkeyFilter(QAbstractNativeEventFilter):
    def nativeEventFilter(self, eventType, message):
        try:
            et = bytes(eventType)
            if et == b"windows_generic_MSG":
                msg = wintypes.MSG.from_address(int(message))
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    cb = _callback
                    if cb is not None:
                        cb()
        except Exception:
            pass
        return False, 0


def install(app, callback):
    """安装原生事件过滤器（整个应用生命周期一次）。"""
    global _filter, _callback
    _callback = callback
    if _filter is None:
        _filter = _HotkeyFilter()
        app.installNativeEventFilter(_filter)


def register(hwnd, seq_str):
    """注册全局热键；失败（被占用/不支持）返回 False。"""
    global _hwnd, _registered, _current_text
    parsed = sequence_to_hotkey(seq_str)
    if parsed is None:
        _registered = False
        _current_text = ""
        return False
    mods, vk = parsed
    try:
        ok = bool(ctypes.windll.user32.RegisterHotKey(
            wintypes.HWND(int(hwnd)), HOTKEY_ID, mods | MOD_NOREPEAT, vk))
    except Exception:
        ok = False
    if ok:
        _hwnd = int(hwnd)
        _current_text = seq_str
    _registered = ok
    return ok


def unregister():
    global _registered, _current_text
    if _registered and _hwnd is not None:
        try:
            ctypes.windll.user32.UnregisterHotKey(wintypes.HWND(_hwnd), HOTKEY_ID)
        except Exception:
            pass
    _registered = False
    _current_text = ""


def current_text():
    return _current_text
