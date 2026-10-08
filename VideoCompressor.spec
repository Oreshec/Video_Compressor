# -*- mode: python ; coding: utf-8 -*-
"""Спецификация PyInstaller: одно оконное приложение VideoCompressor.exe.

Сборка:  python -m PyInstaller VideoCompressor.spec --noconfirm
или:      build_exe.bat

Итог:    dist\\VideoCompressor.exe — запускается без установленного Python.
FFmpeg программе не нужен «в коробке»: при первом запуске она сама
предложит скачать его рядом с exe (папка ffmpeg\\).
"""

a = Analysis(
    ["gui_compressor.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Не нужны в оконном приложении — уменьшают размер exe
        "test",
        "unittest",
        "pydoc",
        "pydoc_data",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="VideoCompressor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # без чёрного окна консоли
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,  # положите video-compressor.ico в корень и укажите здесь, если появится
)
