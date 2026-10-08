# -*- mode: python ; coding: utf-8 -*-
"""Спецификация PyInstaller: одно оконное приложение VideoCompressor.exe.

Сборка:  python -m PyInstaller VideoCompressor.spec --noconfirm
или:      build_exe.bat

Итог:    dist\\VideoCompressor.exe — запускается без установленного Python.
FFmpeg программе не нужен «в коробке»: при первом запуске она сама
предложит скачать его рядом с exe (папка ffmpeg\\).
"""

from PyInstaller.utils.hooks import collect_all

# Dear PyGui — скомпилированный модуль + ресурсы (шрифты, shaders): собираем всё.
dpg_datas, dpg_binaries, dpg_hidden = collect_all("dearpygui")

a = Analysis(
    ["gui_compressor.py"],
    pathex=[],
    binaries=dpg_binaries,
    datas=dpg_datas,
    hiddenimports=dpg_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Не нужны в оконном приложении — уменьшают размер exe
        "test",
        "unittest",
        "pydoc",
        "pydoc_data",
        "tkinter",
        "_tkinter",
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
