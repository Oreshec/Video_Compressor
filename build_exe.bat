@echo off
rem Сборка VideoCompressor.exe одной командой (нужен Python, exe — без Python)
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Ошибка: Python не найден. Установите Python 3.9+ с https://python.org
    pause
    exit /b 1
)

echo [1/3] Установка PyInstaller и tkinterdnd2...
python -m pip install --upgrade pyinstaller tkinterdnd2 >nul || goto :fail

echo [2/3] Сборка VideoCompressor.exe...
python -m PyInstaller VideoCompressor.spec --noconfirm --clean --log-level WARN || goto :fail

echo [3/3] Готово!
echo.
echo    dist\VideoCompressor.exe  — переносимая программа,
echo    запускается без Python и без установки.
echo.
pause
exit /b 0

:fail
echo.
echo Ошибка сборки — см. вывод выше.
pause
exit /b 1
