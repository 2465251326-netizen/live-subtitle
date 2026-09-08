# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for LiveSubtitle (Windows)

import os


def _collect_datas():
    datas = [('app/icon.ico', 'app')]
    # faster_whisper 自带的 Silero VAD onnx 资产必须随包分发（v1.9.0 漏打，
    # 导致打包版开启 Silero VAD 后 ONNXRuntime 报 NO SUCH FILE，见 v1.9.1 修复）
    try:
        import faster_whisper
        assets = os.path.join(os.path.dirname(faster_whisper.__file__), 'assets')
        if os.path.isdir(assets):
            for name in os.listdir(assets):
                p = os.path.join(assets, name)
                if os.path.isfile(p) and not name.endswith('.pyc'):
                    datas.append((p, 'faster_whisper/assets'))
    except Exception:
        pass
    # 构建期断言：Silero VAD onnx 必须收集到，否则打包版识别会在 VAD 处崩溃
    if not any(dest == 'faster_whisper/assets' for _, dest in datas):
        raise SystemExit('faster_whisper/assets 未收集到任何 onnx 资产，拒绝打包')
    return datas


a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=[],
    datas=_collect_datas(),
    hiddenimports=[
        'faster_whisper',
        'faster_whisper.transcribe',
        'faster_whisper.vad',
        'faster_whisper.tokenizer',
        'faster_whisper.audio',
        'faster_whisper.utils',
        'ctranslate2',
        'tokenizers',
        'huggingface_hub',
        'onnxruntime',
        'pyaudiowpatch',
        'requests',
        'numpy',
        'sentencepiece',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'scipy', 'pandas', 'torch', 'PyQt5'],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='LiveSubtitle',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon='app/icon.ico',
    version='version_info.txt',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='LiveSubtitle',
)
