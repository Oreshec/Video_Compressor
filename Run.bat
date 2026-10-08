@echo off
rem Video Compressor — запуск программы (для пользователей с Python)
chcp 65001 >nul
cd /d "%~dp0"

where pythonw >nul 2>nul
if errorlevel 1 (
    echo.
    echo  Python не найден.
    echo  Проще всего скачать готовую программу без Python:
    echo  https://github.com/Oreshec/Video_Compressor/releases
    echo.
    pause
    exit /b 1
)

start "" pythonw gui_compressor.py
