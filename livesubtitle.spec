# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for LiveSubtitle (Windows)

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=[],
    datas=[],
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
