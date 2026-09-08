"""系统静音状态查询（补充5）。

部分声卡驱动在系统静音后 loopback 电平不归零，"信号过弱"检测失效，
用户会困惑"为什么没有字幕"。这里直接查询默认播放设备的系统静音状态
（IAudioEndpointVolume::GetMute），静音时在状态栏/悬浮条给出明确提示。

依赖：comtypes（pyaudiowpatch 的既有依赖，无新增安装项）。
任何失败都返回 None——特性降级，绝不影响正常采集。
"""
import ctypes


def is_system_muted():
    """返回 True/False；查询失败返回 None。"""
    try:
        import comtypes
        from comtypes import CLSCTX_ALL, COMMETHOD, GUID, IUnknown, HRESULT
        from ctypes import POINTER, c_bool, c_float, c_uint32, c_void_p

        class IAudioEndpointVolume(IUnknown):
            _iid_ = GUID("{5CDF2C82-841E-4546-9722-0CF74078229A}")
            _methods_ = [
                COMMETHOD([], HRESULT, "RegisterControlChangeNotify"),
                COMMETHOD([], HRESULT, "UnregisterControlChangeNotify"),
                COMMETHOD([], HRESULT, "GetChannelCount",
                          ["out", "retval"], POINTER(c_uint32)),
                COMMETHOD([], HRESULT, "SetMasterVolumeLevel"),
                COMMETHOD([], HRESULT, "SetMasterVolumeLevelScalar"),
                COMMETHOD([], HRESULT, "GetMasterVolumeLevel"),
                COMMETHOD([], HRESULT, "GetMasterVolumeLevelScalar",
                          ["out", "retval"], POINTER(c_float)),
                COMMETHOD([], HRESULT, "SetChannelVolumeLevel"),
                COMMETHOD([], HRESULT, "SetChannelVolumeLevelScalar"),
                COMMETHOD([], HRESULT, "GetChannelVolumeLevel"),
                COMMETHOD([], HRESULT, "GetChannelVolumeLevelScalar"),
                COMMETHOD([], HRESULT, "SetMute"),
                COMMETHOD([], HRESULT, "GetMute",
                          ["out", "retval"], POINTER(c_bool)),
            ]

        class IMMDevice(IUnknown):
            _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
            _methods_ = [
                COMMETHOD([], HRESULT, "Activate",
                          ["in"], POINTER(GUID),
                          ["in"], c_uint32,
                          ["in"], c_void_p,
                          ["out", "retval"], POINTER(POINTER(IAudioEndpointVolume))),
                COMMETHOD([], HRESULT, "OpenPropertyStore"),
                COMMETHOD([], HRESULT, "GetId"),
                COMMETHOD([], HRESULT, "GetState"),
            ]

        class IMMDeviceEnumerator(IUnknown):
            _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
            _methods_ = [
                COMMETHOD([], HRESULT, "EnumAudioEndpoints"),
                COMMETHOD([], HRESULT, "GetDefaultAudioEndpoint",
                          ["in"], c_uint32,
                          ["in"], c_uint32,
                          ["out", "retval"], POINTER(POINTER(IMMDevice))),
                COMMETHOD([], HRESULT, "GetDevice"),
                COMMETHOD([], HRESULT, "RegisterEndpointNotificationCallback"),
                COMMETHOD([], HRESULT, "UnregisterEndpointNotificationCallback"),
            ]

        comtypes.CoInitialize()
        enum = comtypes.CoCreateInstance(
            GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}"),  # MMDeviceEnumerator
            interface=IMMDeviceEnumerator,
            clsctx=CLSCTX_ALL,
        )
        # eRender = 0, eConsole = 0
        device = enum.GetDefaultAudioEndpoint(0, 0)
        vol = device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        return bool(vol.GetMute())
    except Exception:
        return None
