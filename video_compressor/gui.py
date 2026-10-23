"""Графический интерфейс Video Compressor на Dear PyGui.

Dear PyGui рисует через GPU (DirectX/OpenGL) с настоящим сглаживанием —
в отличие от tkinter, где края виджетов рисуются «пикселями».

Возможности:
    * чёрный минимализм со скруглениями и «мыльными» кнопками;
    * очередь из нескольких файлов с пакетным сжатием и drag-and-drop;
    * выбор кодировщика: CPU x264 или аппаратный (NVENC/QSV/AMF);
    * точное попадание в размер (двухпроходное кодирование) и авторесолюшн;
    * прогресс по всем файлам с оставшимся временем и скоростью;
    * панель «Подробности» с расчётом битрейта и логом FFmpeg;
    * настройки запоминаются между запусками;
    * горячие клавиши: Enter — сжать, Esc — отмена, Ctrl+O — добавить.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.wintypes
import json
import logging
import queue
import sys
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path

import dearpygui.dearpygui as dpg

from . import __version__
from .core import (
    DEFAULT_TARGET_SIZE_MB,
    VideoCompressorError,
    build_plan,
    compress,
    format_duration,
    pick_target_height,
    probe,
    usable_hw_encoders,
)
from .ffmpeg_tools import download_ffmpeg, find_ffmpeg

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Палитра: тёмный графит с светло-голубым акцентом (RGBA)
# --------------------------------------------------------------------------- #

BG = (10, 11, 15, 255)          # фон окна
CARD = (16, 17, 23, 255)        # карточки-секции
DROP_ZONE = (12, 13, 18, 255)   # «пустая зона» для перетаскивания
LINE = (27, 29, 37, 255)
BORDER = (42, 44, 56, 255)
TEXT = (236, 239, 244, 255)
MUTED = (124, 128, 138, 255)
FIELD = (13, 14, 19, 255)
BTN = (20, 21, 28, 255)
BTN_HOVER = (32, 34, 44, 255)
BTN_ACTIVE = (43, 46, 60, 255)
ACCENT = (125, 211, 252, 255)   # #7dd3fc
BUBBLE = (165, 243, 252, 255)   # #a5f3fc — «мыльная» кромка
OK = (110, 231, 183, 255)  # #6ee7b7
ERR = (252, 165, 165, 255)  # #fca5a5
WARN = (253, 230, 138, 255)  # #fde68a

SETTINGS_NAME = "VideoCompressor"
MAX_LOG_LINES = 400
WINDOW_MIN_W, WINDOW_MIN_H = 800, 620

#: Надпись на кнопке скорости → пресет libx264.
SPEED_CHOICES = {
    "Быстро (рекомендуется)": "veryfast",
    "Сбалансировано": "medium",
    "Максимальное качество": "slow",
}

#: Надпись комбобокса кодировщика → имя для ядра.
ENCODER_CHOICES = {
    "CPU x264 — лучшее качество": "libx264",
    "Авто (аппаратный, если есть)": "auto",
    "NVIDIA NVENC": "h264_nvenc",
    "Intel QSV": "h264_qsv",
    "AMD AMF": "h264_amf",
}

#: Порядок предпочтения аппаратных кодеров в режиме «Авто».
_HW_PRIORITY = ("h264_nvenc", "h264_qsv", "h264_amf")

#: Имя кодировщика для лога/статуса.
ENCODER_LABELS = {
    "libx264": "CPU x264",
    "h264_nvenc": "NVIDIA NVENC",
    "h264_qsv": "Intel QSV",
    "h264_amf": "AMD AMF",
}

#: Надпись комбобокса разрешения → параметр ядра.
RESOLUTION_CHOICES = {
    "Исходное разрешение": None,
    "Авто (по битрейту)": "auto",
    "≤2160p": 2160,
    "≤1440p": 1440,
    "≤1080p": 1080,
    "≤720p": 720,
    "≤480p": 480,
}

#: Чем меньше ~300 кбит/с на видео, тем заметнее «мыло» на движении.
LOW_BITRATE_WARN_BPS = 300_000

#: Расширения, которые можно перетащить в окно.
VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv",
    ".webm", ".ts", ".m4v", ".mpg", ".mpeg", ".3gp",
}

NO_FILES_MSG = "Файлов нет — добавьте видео кнопкой «Добавить…» или перетащите в окно"


# --------------------------------------------------------------------------- #
# Настройки
# --------------------------------------------------------------------------- #


def settings_path() -> Path:
    """Путь к файлу настроек в профиле пользователя."""
    if sys.platform == "win32":
        base = Path.home() / "AppData" / "Roaming"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path.home() / ".config"
    return base / SETTINGS_NAME / "settings.json"


def load_settings() -> dict:
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data: dict) -> None:
    try:
        path = settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:  # pragma: no cover — настройки не критичны
        log.debug("Не удалось сохранить настройки: %s", exc)


# --------------------------------------------------------------------------- #
# Утилиты
# --------------------------------------------------------------------------- #


def open_in_file_manager(path: Path) -> None:
    """Открыть файл в проводнике и выделить его."""
    try:
        if sys.platform == "win32":
            ctypes.windll.shell32.ShellExecuteW(  # type: ignore[attr-defined]
                None, "open", "explorer.exe", f'/select,"{path}"', None, 1
            )
        elif sys.platform == "darwin":  # pragma: no cover
            import subprocess

            subprocess.run(["open", "-R", str(path)], check=False)
        else:  # pragma: no cover
            import subprocess

            subprocess.Popen(["xdg-open", str(path.parent)])
    except OSError as exc:  # pragma: no cover
        log.debug("Не удалось открыть проводник: %s", exc)


def human_size(mb: float) -> str:
    if mb >= 1024:
        return f"{mb / 1024:.2f} ГБ"
    return f"{mb:.1f} МБ"


def _system_dpi() -> float:
    """DPI основного монитора (96 = 100%)."""
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    try:
        get_dpi = getattr(user32, "GetDpiForSystem", None)
        if get_dpi is not None:
            dpi = get_dpi()
            if dpi:
                return dpi / 96.0
    except (AttributeError, OSError):
        pass
    hdc = user32.GetDC(0)
    try:
        dpi = gdi32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
        return (dpi or 96) / 96.0
    finally:
        user32.ReleaseDC(0, hdc)


def set_dpi_awareness() -> float:
    """Синхронизировать масштаб мыши/отрисовки с масштабом Windows.

    Дорогой вызов для Dear PyGui: без DPI-aware процесса Windows
    виртуализирует координаты, и курсор оказывается со сдвигом от
    виджетов (на 125–150% «попадаешь выше элемента»).

    Должен вызываться **до** создания окна. Возвращает масштаб
    (1.0 при 100%, 1.5 при 150% и т. д.).
    """
    if sys.platform != "win32":  # pragma: no cover — только Windows
        return 1.0
    user32 = ctypes.windll.user32
    set_ctx = getattr(user32, "SetProcessDpiAwarenessContext", None)
    if set_ctx is not None:
        try:
            if set_ctx(ctypes.c_void_p(-4)):  # PER_MONITOR_AWARE_V2 — точные координаты
                return _system_dpi()
        except OSError as exc:
            log.debug("SetProcessDpiAwarenessContext не удался: %s", exc)
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Win 8.1: системный DPI-aware
        return _system_dpi()
    except (AttributeError, OSError):
        user32.SetProcessDPIAware()  # Win 7+: самый простой режим
        return _system_dpi()


def format_eta(seconds: float) -> str:
    if seconds < 0:
        return "—"
    if seconds >= 86400:
        return ">24 ч"
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def unique_output_path(target: Path, taken: Iterable[Path]) -> Path:
    """Вернуть путь, не пересекающийся с ``taken``, добавляя суффикс ``_2``…"""
    taken = {Path(t) for t in taken}
    if target not in taken:
        return target
    counter = 2
    stem, suffix = target.stem, target.suffix
    while Path(target.parent) / f"{stem}_{counter}{suffix}" in taken:
        counter += 1
    return target.with_name(f"{stem}_{counter}{suffix}")


def scaled_height_for(info, plan, setting) -> int | None:
    """Высота, до которой реально уменьшится файл (для строки «Оценка»)."""
    if setting is None or not getattr(info, "width", None) or not getattr(info, "height", None):
        return None
    if setting == "auto":
        height = pick_target_height(info, plan.video_bitrate_bps)
        return height if info.height and height and height < info.height else None
    return setting if setting < info.height else None


# --------------------------------------------------------------------------- #
# Перетаскивание файлов на окно (Windows WM_DROPFILES)
# --------------------------------------------------------------------------- #

if sys.platform == "win32":  # код активен только на Windows

    _WM_DROPFILES = 0x0233
    _GWL_WNDPROC = -4
    _SUBCLASS_ID = 0x51F0  # произвольный устойчивый идентификатор перехватчика

    _SUBCLASSPROC = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t,   # LRESULT
        ctypes.c_void_p,    # HWND
        ctypes.c_uint,      # UINT message
        ctypes.c_void_p,    # WPARAM
        ctypes.c_ssize_t,   # LPARAM
        ctypes.c_size_t,    # UINT_PTR uIdSubclass
        ctypes.c_size_t,    # DWORD_PTR dwRefData
    )

    _ENUMWINDOWSPROC = ctypes.WINFUNCTYPE(
        ctypes.c_int,       # результат перебора (0 — остановка)
        ctypes.c_void_p,    # HWND
        ctypes.c_ssize_t,   # LPARAM
    )

    class WindowsFileDrop:
        """Перехватывает WM_DROPFILES у окна: вызов колбэка с путями файлов.

        Способ: SetWindowSubclass (comctl32, как рекомендует MSDN) — корректно
        сочетается с чужим WndProc (у окон Dear PyGui это GLFW). Если comctl32
        недоступен — запасной путь через SetWindowLongPtr с цепочкой вызовов.
        """

        def __init__(self, hwnd: int, on_paths: Callable[[list[str]], None]) -> None:
            user32 = ctypes.windll.user32
            self._shell32 = ctypes.windll.shell32
            self._user32 = user32
            self._on_paths = on_paths
            self._hwnd = hwnd
            self._proc = _SUBCLASSPROC(self._on_message)
            # Точные прототипы shell32: инт-аргументы иначе не принимают
            # Large-значения (номер «все файлы» — 0xFFFFFFFF).
            self._shell32.DragQueryFileW.argtypes = [
                ctypes.c_void_p, ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_uint
            ]
            self._shell32.DragQueryFileW.restype = ctypes.c_uint
            self._shell32.DragFinish.argtypes = [ctypes.c_void_p]
            self._shell32.DragFinish.restype = None
            self._shell32.DragAcceptFiles.argtypes = [ctypes.c_void_p, ctypes.c_int]
            self._shell32.DragAcceptFiles.restype = None
            try:
                comctl = ctypes.WinDLL("comctl32")
                self._set_subclass = getattr(comctl, "SetWindowSubclass", None)
                self._def_proc = getattr(comctl, "DefSubclassProc", None)
            except (AttributeError, OSError):
                self._set_subclass = None
                self._def_proc = None
            if self._set_subclass is not None and self._def_proc is not None:
                self._set_subclass.argtypes = [
                    ctypes.c_void_p, _SUBCLASSPROC, ctypes.c_size_t, ctypes.c_size_t
                ]
                self._set_subclass.restype = ctypes.c_int
                self._def_proc.argtypes = [
                    ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_ssize_t
                ]
                self._def_proc.restype = ctypes.c_ssize_t
                self._subclassed = bool(
                    self._set_subclass(ctypes.c_void_p(hwnd), self._proc, _SUBCLASS_ID, 0)
                )
            if not self._subclassed:
                # Запасной вариант: подменяем WndProc и перезапускаем старый.
                if self._def_proc is None:
                    self._def_proc = user32.CallWindowProcW
                self._old_proc = user32.GetWindowLongPtrW(hwnd, _GWL_WNDPROC)
                user32.SetWindowLongPtrW(hwnd, _GWL_WNDPROC, self._proc)
                log.debug("SetWindowSubclass не применился — работает запасной перехват")
            self._shell32.DragAcceptFiles(hwnd, True)

        def _on_message(self, hwnd, msg, wparam, lparam, *_subclass_args):
            if msg == _WM_DROPFILES:
                paths = self._collect_paths(wparam)
                self._shell32.DragFinish(wparam)
                try:
                    self._on_paths(paths)
                except Exception:  # noqa: BLE001 — колбэк не должен валить окно
                    logging.getLogger(__name__).exception("Ошибка drag-and-drop")
                return 0
            if not self._subclassed and self._old_proc:
                return self._def_proc(self._old_proc, hwnd, msg, wparam, lparam)
            return self._def_proc(hwnd, msg, wparam, lparam)

        def _collect_paths(self, hdrop) -> list[str]:
            count = self._shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
            buffer = ctypes.create_unicode_buffer(1024)
            paths = []
            for index in range(count):
                self._shell32.DragQueryFileW(hdrop, index, buffer, len(buffer))
                paths.append(buffer.value)
            return paths

else:  # pragma: no cover — перетаскивание делается руками для каждой платформы

    class WindowsFileDrop:  # type: ignore[no-redef]  # noqa: D101
        def __init__(self, *_args, **_kwargs) -> None:
            raise NotImplementedError("Drag-and-drop поддерживается только на Windows")


# --------------------------------------------------------------------------- #
# Стандартный диалог выбора файлов Windows (мультивыбор)
# --------------------------------------------------------------------------- #

_OFN_ALLOWMULTISELECT = 0x200
_OFN_EXPLORER = 0x80000
_OFN_FILEMUSTEXIST = 0x1000
_OFN_HIDEREADONLY = 0x4


class _OPENFILENAMEW(ctypes.Structure):
    _fields_ = [
        ("lStructSize", ctypes.c_uint32),
        ("hwndOwner", ctypes.c_void_p),
        ("hInstance", ctypes.c_void_p),
        ("lpstrFilter", ctypes.c_wchar_p),
        ("lpstrCustomFilter", ctypes.c_wchar_p),
        ("nMaxCustFilter", ctypes.c_uint32),
        ("nFilterIndex", ctypes.c_uint32),
        ("lpstrFile", ctypes.c_wchar_p),
        ("nMaxFile", ctypes.c_uint32),
        ("lpstrFileTitle", ctypes.c_wchar_p),
        ("nMaxFileTitle", ctypes.c_uint32),
        ("lpstrInitialDir", ctypes.c_wchar_p),
        ("lpstrTitle", ctypes.c_wchar_p),
        ("Flags", ctypes.c_uint32),
        ("nFileOffset", ctypes.c_uint16),
        ("nFileExtension", ctypes.c_uint16),
        ("lpstrDefExt", ctypes.c_wchar_p),
        ("lCustData", ctypes.c_ssize_t),
        ("lpfnHook", ctypes.c_void_p),
        ("lpTemplateName", ctypes.c_wchar_p),
        ("pvReserved", ctypes.c_void_p),
        ("dwReserved", ctypes.c_uint32),
        ("FlagsEx", ctypes.c_uint32),
    ]


def pick_video_files() -> list[str]:
    """Мультивыбор видеофайлов стандартным диалогом Windows."""
    if sys.platform != "win32":  # pragma: no cover — только Windows
        return []
    buffer = ctypes.create_unicode_buffer(1 << 17)
    filters = (
        "Видео (*.mp4 *.mkv *.avi *.mov *.wmv *.flv *.webm *.ts *.m4v)"
        "\0*.mp4;*.mkv;*.avi;*.mov;*.wmv;*.flv;*.webm;*.ts;*.m4v\0"
        "Все файлы (*.*)\0*.*\0\0"
    )
    owner = ctypes.windll.user32.FindWindowW(None, f"Video Compressor {__version__}")
    ofn = _OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(_OPENFILENAMEW)
    ofn.hwndOwner = owner or None
    ofn.lpstrFilter = filters
    ofn.lpstrFile = ctypes.cast(buffer, ctypes.c_wchar_p)
    ofn.nMaxFile = len(buffer)
    ofn.lpstrTitle = "Выберите видеофайлы"
    ofn.Flags = _OFN_EXPLORER | _OFN_ALLOWMULTISELECT | _OFN_FILEMUSTEXIST | _OFN_HIDEREADONLY
    if not ctypes.windll.comdlg32.GetOpenFileNameW(ctypes.byref(ofn)):
        return []
    raw = buffer.value
    if not raw:
        return []
    parts = [p for p in raw.split("\0") if p]
    if len(parts) == 1:
        return [parts[0]]
    directory = parts[0]
    return [str(Path(directory) / name) for name in parts[1:]]


# --------------------------------------------------------------------------- #
# Приложение
# --------------------------------------------------------------------------- #


class App:
    """Окно приложения на Dear PyGui."""

    def __init__(self, *, check_ffmpeg: bool = True) -> None:
        # Обязательно до создания окна: синхронизируем координаты мыши с DPI.
        self._scale = set_dpi_awareness()
        self.settings = load_settings()
        self._events: queue.Queue[tuple] = queue.Queue()
        self._cancel_event = threading.Event()
        self._busy = False
        self._files: dict[str, dict] = {}  # путь -> карточка файла
        self._order: list[str] = []  # порядок карточек
        self._probe_seq = 0
        self._probe_token: dict[str, int] = {}
        self._last_output_dir: Path | None = None
        self._last_output: Path | None = None
        self._log_lines: list[str] = []
        self._alert_seq = 0
        self._alert_tag: str | None = None
        self._pending_alerts: list[tuple[str, str]] = []
        self._check_on_start = check_ffmpeg
        self._hw_encoders: set[str] | None = None  # из фонового определения
        self._eta_ema: float | None = None  # сглаженная оценка остатка времени
        self._toast_seq = 0
        self._toast_tag: str | None = None
        self._size_chips: list[int | str] = []
        self._drop: object | None = None  # удерживает хук перетаскивания живым

        # Высота панели файлов (рассчитывается в _relayout).
        self._files_panel_h = self._s(250)
        self._last_files_h = 0

        # Шрифты; если системные не найдены — остаётся None.
        self._font = None
        self._font_bold = None
        self._font_title = None
        self._font_mono = None

        dpg.create_context()
        self._setup_fonts()
        self._setup_theme()
        self._create_viewport()
        self._build_ui()

    def _s(self, value: float) -> int:
        """Масштабировать размер пикселей под масштаб Windows (DPI)."""
        return int(round(value * self._scale))

    # ------------------------------------------------------------------ #
    # Шрифты и тема
    # ------------------------------------------------------------------ #

    def _setup_fonts(self) -> None:
        with dpg.font_registry():
            for path, size, attr in (
                ("C:/Windows/Fonts/segoeui.ttf", self._s(16), "_font"),
                ("C:/Windows/Fonts/segoeuib.ttf", self._s(16), "_font_bold"),
                ("C:/Windows/Fonts/segoeuib.ttf", self._s(21), "_font_title"),
                ("C:/Windows/Fonts/consola.ttf", self._s(13), "_font_mono"),
            ):
                try:
                    with dpg.font(path, size) as font:  # кириллица — в наборе по умолчанию
                        pass
                    setattr(self, attr, font)
                except Exception as exc:  # noqa: BLE001 — не-Windows или нет шрифта
                    log.debug("Шрифт %s недоступен: %s", path, exc)
        if self._font is not None:
            dpg.bind_font(self._font)

    def _setup_theme(self) -> None:
        s = self._s
        with dpg.theme() as theme, dpg.theme_component(dpg.mvAll):
            dpg.add_theme_color(dpg.mvThemeCol_WindowBg, BG)
            dpg.add_theme_color(dpg.mvThemeCol_ChildBg, CARD)
            dpg.add_theme_color(dpg.mvThemeCol_PopupBg, CARD)
            dpg.add_theme_color(dpg.mvThemeCol_Text, TEXT)
            dpg.add_theme_color(dpg.mvThemeCol_TextDisabled, MUTED)
            dpg.add_theme_color(dpg.mvThemeCol_Border, LINE)
            dpg.add_theme_color(dpg.mvThemeCol_BorderShadow, BG)
            dpg.add_theme_color(dpg.mvThemeCol_Button, BTN)
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, BTN_HOVER)
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, BTN_ACTIVE)
            dpg.add_theme_color(dpg.mvThemeCol_FrameBg, FIELD)
            dpg.add_theme_color(dpg.mvThemeCol_FrameBgHovered, BTN_HOVER)
            dpg.add_theme_color(dpg.mvThemeCol_FrameBgActive, BTN_ACTIVE)
            dpg.add_theme_color(dpg.mvThemeCol_Header, BTN_HOVER)
            dpg.add_theme_color(dpg.mvThemeCol_HeaderHovered, BTN_ACTIVE)
            dpg.add_theme_color(dpg.mvThemeCol_CheckMark, ACCENT)
            dpg.add_theme_color(dpg.mvThemeCol_SliderGrab, ACCENT)
            dpg.add_theme_color(dpg.mvThemeCol_PlotHistogram, ACCENT)
            dpg.add_theme_color(dpg.mvThemeCol_Separator, LINE)
            dpg.add_theme_color(dpg.mvThemeCol_TableHeaderBg, CARD)
            dpg.add_theme_color(dpg.mvThemeCol_TableBorderLight, LINE)
            dpg.add_theme_color(dpg.mvThemeCol_TableBorderStrong, LINE)
            dpg.add_theme_color(dpg.mvThemeCol_ScrollbarBg, BG)
            dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrab, BORDER)
            dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrabHovered, MUTED)
            dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, s(10))
            dpg.add_theme_style(dpg.mvStyleVar_WindowRounding, 0)
            dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, s(12))
            dpg.add_theme_style(dpg.mvStyleVar_PopupRounding, s(12))
            dpg.add_theme_style(dpg.mvStyleVar_ScrollbarRounding, s(8))
            dpg.add_theme_style(dpg.mvStyleVar_GrabRounding, s(12))
            dpg.add_theme_style(dpg.mvStyleVar_FrameBorderSize, 1)
            dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, s(10), s(9))
        dpg.bind_theme(theme)

        # Призрачные кнопки: тёмная заливка, тонкая кромка.
        with dpg.theme() as btn_secondary, dpg.theme_component(dpg.mvButton):
            dpg.add_theme_color(dpg.mvThemeCol_Border, LINE)
            dpg.add_theme_color(dpg.mvThemeCol_Button, BTN)
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, BTN_HOVER)
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, BTN_ACTIVE)
            dpg.add_theme_style(dpg.mvStyleVar_FrameBorderSize, 1)
            dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, s(10))

        # Главная кнопка: залитая акцентом, тёмный текст — «CTA».
        with dpg.theme() as btn_cta, dpg.theme_component(dpg.mvButton):
            dpg.add_theme_color(dpg.mvThemeCol_Text, (13, 17, 26, 255))
            dpg.add_theme_color(dpg.mvThemeCol_Button, ACCENT)
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (150, 224, 255, 255))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (98, 192, 241, 255))
            dpg.add_theme_style(dpg.mvStyleVar_FrameBorderSize, 0)
            dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, s(10))

        # Обводка «сжать» в стиле пузыря — вторичная выделенная.
        with dpg.theme() as btn_primary, dpg.theme_component(dpg.mvButton):
            dpg.add_theme_color(dpg.mvThemeCol_Border, BUBBLE)
            dpg.add_theme_color(dpg.mvThemeCol_Button, (12, 15, 22, 255))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (22, 30, 44, 255))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (32, 42, 60, 255))
            dpg.add_theme_style(dpg.mvStyleVar_FrameBorderSize, 1)
            dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, s(10))

        # Карточка-секция: чуть светлее фона, скруглённая, с внутренним отступом.
        with dpg.theme() as card_theme, dpg.theme_component(dpg.mvAll):
            dpg.add_theme_color(dpg.mvThemeCol_ChildBg, CARD)
            dpg.add_theme_color(dpg.mvThemeCol_BorderShadow, (0, 0, 0, 0))
            dpg.add_theme_color(dpg.mvThemeCol_WindowBg, CARD)
            dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, s(12))
            dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, s(14), s(12))
            dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, s(10))

        # Зона перетаскивания: темнее карточки, с тонкой пунктирной кромкой.
        with dpg.theme() as dropzone_theme, dpg.theme_component(dpg.mvAll):
            dpg.add_theme_color(dpg.mvThemeCol_ChildBg, DROP_ZONE)
            dpg.add_theme_color(dpg.mvThemeCol_Border, BORDER)
            dpg.add_theme_color(dpg.mvThemeCol_WindowBg, DROP_ZONE)
            dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, s(12))
            dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, s(10))

        # Лог процесса: тёмный фон, чуть темнее окна, скруглённый.
        with dpg.theme() as log_theme, dpg.theme_component(dpg.mvAll):
            dpg.add_theme_color(dpg.mvThemeCol_FrameBg, DROP_ZONE)
            dpg.add_theme_color(dpg.mvThemeCol_FrameBgHovered, DROP_ZONE)
            dpg.add_theme_color(dpg.mvThemeCol_FrameBgActive, DROP_ZONE)
            dpg.add_theme_color(dpg.mvThemeCol_Border, BORDER)
            dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, s(10))

        self._theme_secondary = btn_secondary
        self._theme_primary = btn_primary
        self._theme_cta = btn_cta
        self._theme_card = card_theme
        self._theme_dropzone = dropzone_theme
        self._theme_log = log_theme

    def _create_viewport(self) -> None:
        s = self._s
        width = int(self.settings.get("width", 860) * self._scale)
        height = int(self.settings.get("height", 720) * self._scale)
        width = max(width, s(WINDOW_MIN_W))
        height = max(height, s(WINDOW_MIN_H))
        # Не открывать окно больше рабочей области экрана: тогда размеры,
        # сохранённые на большом мониторе, на меньшем не обрежут низ окна.
        screen_w, screen_h = self._workarea()
        width = min(width, max(screen_w - s(40), s(WINDOW_MIN_W)))
        height = min(height, max(screen_h - s(40), s(WINDOW_MIN_H)))
        dpg.create_viewport(
            title=f"Video Compressor {__version__}",
            width=width,
            height=height,
            min_width=s(WINDOW_MIN_W),
            min_height=s(WINDOW_MIN_H),
        )
        dpg.set_viewport_clear_color(BG)

    def _workarea(self) -> tuple[int, int]:
        """Свободная область рабочего стола Windows (без панели задач)."""
        if sys.platform == "win32":
            try:
                rect = ctypes.wintypes.RECT()
                ok = ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(rect), 0)
                if ok and rect.right > rect.left:
                    return rect.right - rect.left, rect.bottom - rect.top
            except (AttributeError, OSError):
                pass
        return 0, 0

    def _bubble_button(
        self,
        label: str,
        callback: Callable | None,
        *,
        primary: bool = False,
        width: int = 0,
        small: bool = False,
        height: int | None = None,
    ) -> int | str:
        """Кнопка со скруглением: залитая акцентом (CTA) или призрачная."""
        btn = dpg.add_button(
            label=label,
            callback=callback,
            width=width,
            height=height if height is not None else (self._s(26) if small else self._s(34)),
        )
        dpg.bind_item_theme(btn, self._theme_cta if primary else self._theme_secondary)
        if self._font_bold is not None:
            dpg.bind_item_font(btn, self._font_bold)
        return btn

    def _on_viewport_resized(self, *_args) -> None:
        """Пересчитать компоновку и центрирование под новый размер окна."""
        self._relayout()
        self._update_centering()

    def _update_centering(self) -> None:
        """Отцентрировать тексты «пустой» зоны перетаскивания по ширине окна."""
        if not dpg.does_item_exist("empty_files"):
            return
        zone_w = dpg.get_item_rect_size("empty_files")[0]
        widest = max(
            dpg.get_item_rect_size("dz_line1")[0],
            dpg.get_item_rect_size("dz_line2")[0],
        )
        # Округляем: отступ = (зона − текст)/2; спейсер межстрочный не нужен.
        left = max(0, (int(zone_w) - int(widest)) // 2 - self._s(6))
        if zone_w <= 0:  # контент ещё не отрисован — оставить исходные отступы
            return
        dpg.configure_item("dz_left1", width=left)
        dpg.configure_item("dz_left2", width=left)

    def _card(self, tag: str | None = None) -> int | str:
        """Карточка-секция: скруглённый подблок светлее фона; дети — ``parent=...``."""
        child = dpg.add_child_window(border=False, auto_resize_y=True, tag=tag)
        dpg.bind_item_theme(child, self._theme_card)
        return child

    # ------------------------------------------------------------------ #
    # Построение интерфейса
    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        s = self._s
        with dpg.window(
            tag="main",
            no_title_bar=True,
            no_move=True,
            no_resize=True,
            no_scrollbar=True,
        ):
            # ---- Верхняя страница: заголовок, файлы, параметры, лог. ---- #
            # Высота рассчитывается в `_relayout`; в самом низу окна —
            # отдельный «док» (run_card) с прогрессом и кнопками.
            with dpg.child_window(tag="content", border=False):
                # Заголовок: логотип + версия.
                with dpg.group(horizontal=True, tag="hdr_group"):
                    brand = dpg.add_text("Video Compressor")
                    if self._font_bold is not None:
                        dpg.bind_item_font(brand, self._font_bold)
                    dpg.add_spacer(width=s(6))
                    dpg.add_text(f"v{__version__}", color=MUTED)
                    dpg.add_spacer(width=s(20))
                    self.lbl_ffmpeg = dpg.add_text("", color=MUTED)
                    self._update_ffmpeg_label()

                # ---- Файлы: панель растягивается по высоте при resize ---- #
                with dpg.child_window(
                    tag="files_panel", border=False
                ) as files_panel:
                    dpg.bind_item_theme(files_panel, self._theme_card)
                    with dpg.group(horizontal=True):
                        hdr = dpg.add_text("Файлы")
                        if self._font_bold is not None:
                            dpg.bind_item_font(hdr, self._font_bold)
                        dpg.add_spacer(width=s(12))
                        self.btn_add = self._bubble_button(
                            "Добавить…", self._add_files, small=True
                        )
                        dpg.add_spacer(width=s(6))
                        self.btn_clear = self._bubble_button(
                            "Очистить", self._clear_files, small=True
                        )
                    self.lbl_summary = dpg.add_text(NO_FILES_MSG, color=MUTED)

                    # Большая зона перетаскивания, когда список пуст
                    # (высоту задаёт _relayout — растёт вместе с окном).
                    with dpg.child_window(
                        tag="empty_files", border=True, height=self._zone_height()
                    ) as empty_zone:
                        dpg.bind_item_theme(empty_zone, self._theme_dropzone)
                        dpg.add_spacer(height=s(30))
                        with dpg.group(horizontal=True):
                            dpg.add_spacer(tag="dz_left1", width=s(110))
                            line = dpg.add_text(
                                "Перетащите видеофайлы в окно", tag="dz_line1"
                            )
                            if self._font_bold is not None:
                                dpg.bind_item_font(line, self._font_bold)
                        with dpg.group(horizontal=True):
                            dpg.add_spacer(tag="dz_left2", width=s(110))
                            dpg.add_text(
                                "или нажмите «Добавить…» · Ctrl+O",
                                color=MUTED,
                                tag="dz_line2",
                            )

                    with dpg.table(
                        tag="table_files",
                        header_row=True,
                        borders_innerH=False,
                        borders_outerH=False,
                        borders_innerV=False,
                        borders_outerV=False,
                        policy=dpg.mvTable_SizingStretchProp,
                        scrollY=True,
                        height=self._zone_height(),
                        show=False,
                    ):
                        dpg.add_table_column(label="Файл", init_width_or_weight=5)
                        dpg.add_table_column(label="Было", init_width_or_weight=1)
                        dpg.add_table_column(label="Параметры", init_width_or_weight=3)
                        dpg.add_table_column(label="Статус", init_width_or_weight=2)
                        # «Открыть» и «×» вместе в одной фиксированной колонке.
                        dpg.add_table_column(
                            label="Действия",
                            init_width_or_weight=0,
                            width=s(136),
                            width_fixed=True,
                        )
                    self.lbl_estimate = dpg.add_text("", color=MUTED)

                # ---- Параметры: три компактные строки, всё под рукой ---- #
                # ВАЖНО: не задавать width группам — DPG мутирует конфиг-ширину
                # детей; ширины задаются самим полям.
                with dpg.group(tag="sec_params"):
                    with dpg.group(horizontal=True):
                        dpg.add_text("Размер, МБ", color=MUTED)
                        self.inp_size = dpg.add_input_text(
                            tag="inp_size",
                            width=s(90),
                            default_value=str(
                                self.settings.get("size", DEFAULT_TARGET_SIZE_MB)
                            ),
                            hint="МБ",
                        )
                        dpg.add_spacer(width=s(6))
                        for chip_mb in (10, 25, 50, 100):
                            chip = self._bubble_button(
                                f"{chip_mb}",
                                lambda *_a, mb=chip_mb: self._on_size_chip(mb),
                                small=True,
                                width=s(38),
                            )
                            self._size_chips.append(chip)
                        dpg.add_spacer(width=s(14))
                        dpg.add_text("Скорость", color=MUTED)
                        speed = self.settings.get(
                            "speed", next(iter(SPEED_CHOICES))
                        )
                        if speed not in SPEED_CHOICES:
                            speed = next(iter(SPEED_CHOICES))
                        self.cmb_speed = dpg.add_combo(
                            items=list(SPEED_CHOICES),
                            default_value=speed,
                            width=s(200),
                            callback=self._save_settings_partial,
                        )
                    with dpg.group(horizontal=True):
                        dpg.add_text("Кодировщик", color=MUTED)
                        encoder_value = self.settings.get("encoder", "libx264")
                        if encoder_value not in ENCODER_CHOICES.values():
                            encoder_value = "libx264"
                        self.cmb_encoder = dpg.add_combo(
                            items=list(ENCODER_CHOICES),
                            default_value=next(
                                label
                                for label, value in ENCODER_CHOICES.items()
                                if value == encoder_value
                            ),
                            width=s(200),
                            callback=self._save_settings_partial,
                        )
                        dpg.add_spacer(width=s(14))
                        dpg.add_text("Разрешение", color=MUTED)
                        res_value = self.settings.get("resolution", "source")
                        res_value = {"source": None, "auto": "auto"}.get(
                            res_value, res_value
                        )
                        if res_value not in RESOLUTION_CHOICES.values():
                            res_value = None
                        self.cmb_res = dpg.add_combo(
                            items=list(RESOLUTION_CHOICES),
                            default_value=next(
                                label
                                for label, value in RESOLUTION_CHOICES.items()
                                if value == res_value
                            ),
                            width=s(170),
                            callback=self._save_settings_partial,
                        )
                        dpg.add_spacer(width=s(14))
                        dpg.add_checkbox(
                            label="2 прохода — медленно и точно",
                            tag="chk_two_pass",
                            default_value=bool(
                                self.settings.get("two_pass", False)
                            ),
                            callback=self._save_settings_partial,
                        )
                    with dpg.group(horizontal=True):
                        dpg.add_text("Папка сохранения", color=MUTED)
                        self.inp_outdir = dpg.add_input_text(
                            tag="inp_outdir",
                            width=s(300),
                            hint="рядом с исходным файлом",
                            default_value=self.settings.get("output_dir", ""),
                        )
                        dpg.add_spacer(width=s(6))
                        self._bubble_button(
                            "Обзор…", self._choose_outdir, small=True
                        )

                # ---- Лог («Подробности») ---- #
                with dpg.group(tag="log_group"):
                    dpg.add_checkbox(
                        label="Подробности",
                        tag="chk_details",
                        default_value=bool(self.settings.get("details", False)),
                        callback=self._toggle_details,
                    )
                    self.inp_log = dpg.add_input_text(
                        tag="inp_log",
                        multiline=True,
                        readonly=True,
                        width=-1,
                        # height=-1 у input_text схлопывает его в строку —
                        # только фиксированная высота.
                        height=s(150),
                        default_value="",
                        show=bool(self.settings.get("details", False)),
                    )
                    if self._font_mono is not None:
                        dpg.bind_item_font(self.inp_log, self._font_mono)
                    dpg.bind_item_theme(self.inp_log, self._theme_log)
                dpg.add_text(
                    "Enter — сжать · Esc — отмена · Ctrl+O — добавить",
                    color=(88, 92, 104, 255),
                )

            # ---- Док внизу окна: прогресс и кнопки, всё наготове ---- #
            with dpg.child_window(
                tag="run_card", border=False, auto_resize_y=True
            ) as actions:
                dpg.bind_item_theme(actions, self._theme_card)
                self.bar = dpg.add_progress_bar(
                    tag="bar", default_value=0.0, width=-1, height=s(14), overlay=""
                )
                self.lbl_stage = dpg.add_text(
                    "Добавьте видеофайлы", color=MUTED, tag="lbl_stage"
                )
                dpg.add_spacer(height=s(2))
                with dpg.group(horizontal=True):
                    self.btn_compress = self._bubble_button(
                        "Сжать видео",
                        self._start,
                        primary=True,
                        width=s(150),
                        height=s(34),
                    )
                    self.btn_cancel = self._bubble_button(
                        "Отмена", self._on_cancel, width=s(74)
                    )
                    with dpg.group(horizontal=True, tag="result_bar", show=False):
                        dpg.add_spacer(width=s(10))
                        self.lbl_result = dpg.add_text("", color=OK)
                        self._bubble_button(
                            "Открыть папку", self._on_open_result, small=True
                        )
                    dpg.add_spacer(width=s(10))
                    self.lbl_status = dpg.add_text("Готово к работе")

        self._set_busy(False)
        self._refresh_files_summary()
        self._update_estimate()

    def _zone_height(self) -> int:
        """Высота зоны/таблицы файлов: растёт вместе с панелью файлов."""
        return max(self._files_panel_h - self._s(84), self._s(72))

    def _relayout(self) -> None:
        """Разложить окно на одну страницу под текущий размер.

        Дети главного окна позиционируются абсолютно: вверху — контент
        (панель файлов растягивается по высоте), внизу прижат «док»
        run_card с прогрессом и кнопками. Вызывается после первых кадров
        и на каждое изменение размера окна.
        """
        if not dpg.does_item_exist("content"):
            return
        s = self._s

        def _h(tag: str, fallback: int) -> int:
            try:
                value = dpg.get_item_rect_size(tag)[1]
            except (KeyError, SystemError):
                return fallback
            return int(value) if value and value > 0 else fallback

        main_w, main_h = dpg.get_item_rect_size("main")
        pad = s(8)
        gap = s(8)

        # ---- Док прижат к низу ---- #
        dock_h = max(_h("run_card", s(100)), s(56))
        dpg.configure_item("run_card", width=main_w - 2 * pad)
        dpg.set_item_pos("run_card", (pad, main_h - pad - dock_h))

        # ---- Контент: всё, что сверху дока ---- #
        content_h = max(main_h - 2 * pad - dock_h - gap, s(220))
        dpg.configure_item("content", width=main_w - 2 * pad, height=content_h)
        dpg.set_item_pos("content", (pad, pad))

        # ---- Панель файлов занимает всё, что осталось от параметров/лога ---- #
        header_h = _h("hdr_group", s(24))
        params_h = _h("sec_params", s(96))
        log_h = _h("log_group", s(26))
        files_h = max(
            content_h - header_h - params_h - log_h - s(46), s(90)
        )
        self._files_panel_h = files_h
        # Высоты-источники могли быть померены в переходном состоянии
        # раскладки — если результат изменился, дать вёрстке устояться и
        # пересчитать ещё раз.
        if abs(files_h - self._last_files_h) > 2:
            dpg.set_frame_callback(dpg.get_frame_count() + 3, self._relayout)
        self._last_files_h = files_h
        dpg.configure_item(
            "files_panel", width=main_w - 2 * pad, height=files_h
        )
        zone_h = self._zone_height()
        dpg.configure_item("empty_files", height=zone_h)
        dpg.configure_item("table_files", height=zone_h)

    # ------------------------------------------------------------------ #
    # Главный цикл
    # ------------------------------------------------------------------ #

    def run(self) -> None:
        dpg.setup_dearpygui()
        dpg.set_exit_callback(self._on_close)
        dpg.show_viewport()
        dpg.set_primary_window("main", True)
        dpg.set_viewport_resize_callback(self._on_viewport_resized)
        dpg.set_frame_callback(9, self._relayout)
        dpg.set_frame_callback(14, self._update_centering)
        self._setup_windows_extras()
        with dpg.handler_registry():
            dpg.add_key_press_handler(dpg.mvKey_Return, callback=self._on_return)
            dpg.add_key_press_handler(dpg.mvKey_Escape, callback=self._on_cancel)
            dpg.add_key_press_handler(dpg.mvKey_O, callback=self._on_ctrl_o)
        if self._check_on_start:
            self.after(400, self._check_ffmpeg)
            self.after(700, self._detect_encoders)
        while dpg.is_dearpygui_running():
            self._poll_events()
            dpg.render_dearpygui_frame()
        dpg.destroy_context()

    def _viewport_hwnd(self) -> int | None:
        """HWND собственного окна вьюпорта (Windows) или ``None``.

        Ищем по PID процесса, а не просто по заголовку: иначе легко
        зацепиться за окно другого экземпляра программы с тем же
        заголовком — и тогда ни DPI-хуки, ни drag-and-drop не заработают.
        """
        if sys.platform != "win32":
            return None
        title = f"Video Compressor {__version__}"
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        current_pid = kernel32.GetCurrentProcessId()

        found = []

        @_ENUMWINDOWSPROC
        def callback(hwnd: int, _lparam: int) -> int:
            window_pid = ctypes.c_uint()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
            if window_pid.value != current_pid:
                return 1  # продолжаем перебор
            class_name = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, class_name, 64)
            window_title = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, window_title, 256)
            # Окно DPG: класс и заголовок совпадают с названием вьюпорта
            # (служебные окна GLFW и IME не совпадают — пропускаем).
            if window_title.value != title and class_name.value != title:
                return 1  # продолжаем перебор
            found.append(hwnd)
            return 0  # нашли — прекращаем перебор

        user32.EnumWindows(callback, 0)
        return found[0] if found else None

    def _setup_windows_extras(self) -> None:
        """Тёмная полоса заголовка и перетаскивание файлов на окно (Windows)."""
        hwnd = self._viewport_hwnd()
        if hwnd is None:
            return
        self._dark_titlebar(hwnd)
        try:
            self._drop = WindowsFileDrop(hwnd, self._on_drop_paths)
        except Exception as exc:  # noqa: BLE001 — не критично для работы
            log.debug("Перетаскивание недоступно: %s", exc)

    def _dark_titlebar(self, hwnd: int) -> None:
        """Попросить Windows рисовать чёрную полосу заголовка (Win10 1809+)."""
        if sys.platform != "win32":
            return
        value = ctypes.c_int(1)
        for attribute in (20, 19):  # 20 — новые сборки, 19 — старые Insider
            result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)
            )
            if result == 0:
                return
        log.debug("DwmSetWindowAttribute не применился: код %s", result)

    def _on_drop_paths(self, paths: list[str]) -> None:
        """Колбэк из перехватчика WM_DROPFILES — в очередь событий GUI."""
        self._events.put(("dropped", paths))

    def after(self, delay_ms: int, callback: Callable) -> None:
        """Отложить вызов до указанного кадра."""
        frames = max(dpg.get_frame_count() + max(delay_ms // 16, 1), 2)
        dpg.set_frame_callback(frames, callback)

    # ------------------------------------------------------------------ #
    # Диалоги и окна
    # ------------------------------------------------------------------ #

    def _alert(self, title: str, text: str) -> None:
        # Dear PyGui не показывает вторую модалку поверх открытой, поэтому
        # откладываем алерт и показываем, когда текущая модалка закроется.
        if self._modal_open():
            self._pending_alerts.append((title, text))
            return
        self._alert_seq += 1
        tag = f"alert_{self._alert_seq}"
        self._alert_tag = tag
        with dpg.window(
            modal=True,
            label=title,
            tag=tag,
            no_resize=True,
            no_collapse=True,
            width=440,
        ):
            dpg.add_text(text, wrap=410)
            dpg.add_separator()
            dpg.add_button(
                label="OK",
                width=90,
                callback=lambda *_: (dpg.delete_item(tag), self._alert_closed()),
            )
        dpg.show_item(tag)

    def _modal_open(self) -> bool:
        """Открыта ли сейчас какая-нибудь модалка (FFmpeg или алерт)."""
        if dpg.does_item_exist("ffmpeg_dialog"):
            return True
        return self._alert_tag is not None and dpg.does_item_exist(self._alert_tag)

    def _alert_closed(self, *_args) -> None:
        self._alert_tag = None
        self._flush_alerts()

    def _flush_alerts(self) -> None:
        """Показать первый отложенный алерт, когда модалок нет."""
        if not self._pending_alerts or self._modal_open():
            return
        title, text = self._pending_alerts.pop(0)
        self.after(50, lambda: self._alert(title, text))

    def _choose_outdir(self, *_args) -> None:
        tag = "outdir_dialog"
        if dpg.does_item_exist(tag):
            dpg.delete_item(tag)
        with dpg.file_dialog(
            tag=tag,
            directory_selector=True,
            width=560,
            height=420,
            callback=self._on_outdir_picked,
            cancel_callback=lambda *_: dpg.delete_item(tag),
        ):
            dpg.add_text("Выберите папку для сжатых файлов", color=MUTED)

    def _on_outdir_picked(self, _sender, app_data, *_args) -> None:
        path = ""
        if isinstance(app_data, dict):
            selection = app_data.get("selections")
            if isinstance(selection, dict) and selection:
                path = next(iter(selection.values()), "")
            elif isinstance(selection, (list, tuple)) and selection:
                path = str(selection[0])
            path = path or app_data.get("selected_path") or app_data.get("current_path") or ""
        if path:
            dpg.configure_item("inp_outdir", default_value=str(path))
        if dpg.does_item_exist("outdir_dialog"):
            dpg.delete_item("outdir_dialog")

    # ------------------------------------------------------------------ #
    # FFmpeg
    # ------------------------------------------------------------------ #

    def _update_ffmpeg_label(self) -> None:
        tools = find_ffmpeg()
        if tools is None:
            dpg.configure_item(self.lbl_ffmpeg, default_value="FFmpeg не найден", color=ERR)
            return
        base = "FFmpeg: скачан программой" if tools.is_portable else "FFmpeg: системный"
        hw = [
            ENCODER_LABELS.get(name, name)
            for name in _HW_PRIORITY
            if self._hw_encoders and name in self._hw_encoders
        ]
        suffix = f" · аппаратное ускорение: {' + '.join(hw)}" if hw else ""
        dpg.configure_item(self.lbl_ffmpeg, default_value=base + suffix, color=OK)

    def _detect_encoders(self, *_args) -> None:
        """Определить в фоне, какие аппаратные кодеры реально работают."""
        if find_ffmpeg() is None:
            return

        def worker() -> None:
            try:
                encoders = usable_hw_encoders()
            except Exception as exc:  # noqa: BLE001 — определение не критично
                log.debug("Не удалось определить кодеры: %s", exc)
                encoders = set()
            self._events.put(("encoders", encoders))

        threading.Thread(target=worker, daemon=True, name="encoders-detect").start()

    def _check_ffmpeg(self, *_args) -> None:
        if find_ffmpeg() is not None:
            return
        tag = "ffmpeg_dialog"
        with dpg.window(
            modal=True,
            label="FFmpeg не найден",
            tag=tag,
            no_resize=True,
            no_collapse=True,
            width=470,
        ):
            dpg.add_text(
                "Для работы нужен FFmpeg (бесплатный видеокодек).\n\n"
                "Скачать его автоматически в папку программы (~110 МБ, один раз)?\n\n"
                "«Отмена» — если FFmpeg уже установлен, просто перезапустите программу.",
                wrap=440,
            )
            dpg.add_separator()
            with dpg.group(horizontal=True):
                dpg.add_button(
                    label="Скачать",
                    width=110,
                    callback=lambda *_: (dpg.delete_item(tag), self._download_ffmpeg()),
                )
                dpg.add_button(
                    label="Отмена",
                    width=110,
                    callback=lambda *_: dpg.delete_item(tag),
                )
        dpg.show_item(tag)

    def _download_ffmpeg(self, *_args) -> None:
        self._set_busy(True)
        dpg.configure_item(self.bar, default_value=-1.0, overlay="")
        dpg.configure_item(self.lbl_stage, default_value="Скачивание FFmpeg…")

        def worker() -> None:
            try:
                download_ffmpeg(progress=lambda frac, stage: self._events.put(("dl", frac, stage)))
            except OSError as exc:
                self._events.put(("error", str(exc)))
            except Exception as exc:  # noqa: BLE001
                self._events.put(("error", f"Ошибка скачивания FFmpeg: {exc}"))
            else:
                self._events.put(("dl_done",))

        threading.Thread(target=worker, daemon=True, name="ffmpeg-download").start()

    # ------------------------------------------------------------------ #
    # Список файлов
    # ------------------------------------------------------------------ #

    def _add_files(self, *_args) -> None:
        if self._busy:
            self._toast("Дождитесь завершения текущей операции", color=WARN)
            return
        self._append_paths(pick_video_files())

    def _append_paths(self, paths: list[str]) -> None:
        if self._busy:
            self._toast("Дождитесь завершения текущей операции", color=WARN)
            return
        added = 0
        for raw in paths:
            path = Path(raw.strip()).expanduser()
            if not path.is_file() or str(path) in self._files:
                continue
            self._add_card(path)
            added += 1
        if added:
            dpg.configure_item("result_bar", show=False)
            self._log_clear()
            self._refresh_files_summary()
            self._update_estimate()

    def _add_card(self, path: Path) -> None:
        key = str(path)
        with dpg.table_row(parent="table_files") as row:
            name = dpg.add_text(self._short_name(path))
            with dpg.tooltip(name):
                dpg.add_text(key)
            dpg.add_text("анализ…", tag=f"size_{key}", color=MUTED)
            dpg.add_text("", tag=f"params_{key}", color=MUTED)
            dpg.add_text("анализ…", tag=f"status_{key}", color=WARN)
            with dpg.group(horizontal=True):
                btn_open = dpg.add_button(
                    label="Открыть",
                    width=self._s(90),
                    height=self._s(24),
                    show=False,
                    callback=lambda *_args, k=key: self._open_file_result(k),
                )
                dpg.bind_item_theme(btn_open, self._theme_secondary)
                dpg.add_spacer(width=self._s(4))
                btn_x = dpg.add_button(
                    label="×",
                    width=self._s(34),
                    height=self._s(24),
                    callback=lambda *_args, k=key: self._remove_file(k),
                )
                dpg.bind_item_theme(btn_x, self._theme_secondary)
        self._files[key] = {"path": path, "row": row, "btn_open": btn_open}
        self._order.append(key)
        self._probe_start(key, path)

    def _table_height(self) -> int:
        """Высота таблицы файлов: компактная для коротких списков.

        До 4 строк таблица растёт вместе с содержимым, дальше —
        фиксированная высота со скроллом.
        """
        rows = min(len(self._order), 4)
        return self._s(38 + rows * 32)

    @staticmethod
    def _short_name(path: Path) -> str:
        name = path.name
        return name if len(name) <= 48 else f"{name[:30]}…{name[-15:]}"

    def _remove_file(self, key: str, *_args) -> None:
        if key not in self._files:
            return
        if self._busy:
            self._toast("Нельзя менять очередь во время сжатия", color=WARN)
            return
        dpg.delete_item(self._files[key]["row"])
        self._files.pop(key, None)
        self._order = [k for k in self._order if k != key]
        self._probe_token.pop(key, None)
        self._refresh_files_summary()
        self._update_estimate()

    def _clear_files(self, *_args) -> None:
        if self._busy:
            self._toast("Нельзя менять очередь во время сжатия", color=WARN)
            return
        for card in list(self._files.values()):
            dpg.delete_item(card["row"])
        self._files.clear()
        self._order.clear()
        self._probe_token.clear()
        self._refresh_files_summary()
        self._update_estimate()

    def _probe_start(self, key: str, path: Path) -> None:
        self._probe_seq += 1
        token = self._probe_seq
        self._probe_token[key] = token

        def worker() -> None:
            try:
                info = probe(path)
            except VideoCompressorError as exc:
                self._events.put(("probe", key, token, None, str(exc)))
            except OSError as exc:
                self._events.put(("probe", key, token, None, f"Не удалось прочитать: {exc}"))
            else:
                self._events.put(("probe", key, token, info, None))

        threading.Thread(target=worker, daemon=True, name="ffprobe").start()

    def _refresh_files_summary(self) -> None:
        count = len(self._order)
        empty = count == 0
        dpg.configure_item("empty_files", show=empty)
        dpg.configure_item("table_files", show=not empty)
        if not empty:
            # Автовысота: по числу файлов, максимум 4 строки, дальше скролл.
            dpg.configure_item("table_files", height=self._table_height())
        if empty:
            dpg.configure_item(self.lbl_summary, default_value=NO_FILES_MSG)
            return
        total = sum(
            card["info"].size_mb for card in self._files.values() if card.get("info")
        )
        dpg.configure_item(
            self.lbl_summary,
            default_value=f"Файлов: {count}"
            + (f" · исходный объём: {human_size(total)}" if total else ""),
        )

    # ------------------------------------------------------------------ #
    # Оценка
    # ------------------------------------------------------------------ #

    def _target_size(self) -> float | None:
        raw = dpg.get_value("inp_size").strip().replace(",", ".")
        try:
            value = float(raw)
        except ValueError:
            return None
        return value if value > 0 else None

    def _update_estimate(self, *_args) -> None:
        size = self._target_size()
        if size is None:
            dpg.configure_item(
                self.lbl_estimate,
                default_value="Целевой размер должен быть положительным числом",
                color=WARN,
            )
            return
        infos = [card["info"] for card in self._files.values() if card.get("info")]
        if not infos:
            dpg.configure_item(
                self.lbl_estimate, default_value="Оценка появится после анализа файлов", color=MUTED
            )
            return
        plans = [build_plan(info, size) for info in infos]
        max_est = max(p.estimated_size_mb for p in plans)
        max_br = max(p.video_bitrate_kbps for p in plans)
        min_br = min(p.video_bitrate_bps for p in plans)
        suffix = "на файл" if len(plans) > 1 else f"для {infos[0].path.name}"
        text = (
            f"Оценка: до {human_size(max_est)} {suffix}"
            f" · видеобитрейт до {max_br} кбит/с"
        )
        setting = self._res_setting()
        if setting is not None:
            heights = {
                h
                for info, plan in zip(infos, plans)
                if (h := scaled_height_for(info, plan, setting))
            }
            if heights:
                shown = "/".join(f"{h}p" for h in sorted(heights, reverse=True))
                text += f" · масштаб ↓ {shown}"
        color: tuple = MUTED
        if min_br < LOW_BITRATE_WARN_BPS:
            text += (
                f" ⚠ {min_br // 1000} кбит/с — качество будет низким:"
                " увеличьте целевой размер или разрешите уменьшение разрешения"
            )
            color = WARN
        dpg.configure_item(self.lbl_estimate, default_value=text, color=color)

    def _res_setting(self) -> int | str | None:
        label = dpg.get_value(self.cmb_res) if hasattr(self, "cmb_res") else None
        return RESOLUTION_CHOICES.get(label)

    # ------------------------------------------------------------------ #
    # Лог «Подробности»
    # ------------------------------------------------------------------ #

    def _log(self, line: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self._log_lines.append(f"[{stamp}] {line}")
        if len(self._log_lines) > MAX_LOG_LINES:
            self._log_lines = self._log_lines[-MAX_LOG_LINES:]
        dpg.configure_item(self.inp_log, default_value="\n".join(self._log_lines) + "\n")
        with contextlib.suppress(Exception):
            dpg.set_y_scroll(self.inp_log, -1)

    def _log_clear(self) -> None:
        self._log_lines.clear()
        dpg.configure_item(self.inp_log, default_value="")

    def _toggle_details(self, *_args) -> None:
        show = bool(dpg.get_value("chk_details"))
        dpg.configure_item(self.inp_log, show=show)
        self._save_settings_partial()

    def _show_details(self, show: bool) -> None:
        dpg.set_value("chk_details", show)
        dpg.configure_item(self.inp_log, show=show)

    # ------------------------------------------------------------------ #
    # Запуск пакетного сжатия
    # ------------------------------------------------------------------ #

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for tag in (self.btn_add, self.btn_clear, *self._size_chips):
            dpg.configure_item(tag, enabled=not busy)
        dpg.configure_item(self.btn_compress, enabled=not busy)
        dpg.configure_item(self.btn_cancel, enabled=busy)
        dpg.configure_item("inp_size", enabled=not busy)
        dpg.configure_item(self.cmb_speed, enabled=not busy)
        dpg.configure_item(self.cmb_encoder, enabled=not busy)
        dpg.configure_item(self.cmb_res, enabled=not busy)
        dpg.configure_item("chk_two_pass", enabled=not busy)
        dpg.configure_item(self.inp_outdir, enabled=not busy)

    def _resolved_encoder(self) -> str:
        """Имя кодировщика с учётом выбора «Авто» и доступности железа."""
        label = dpg.get_value(self.cmb_encoder)
        choice = ENCODER_CHOICES.get(label, "libx264")
        if choice == "auto":
            for name in _HW_PRIORITY:
                if self._hw_encoders is None or name in self._hw_encoders:
                    return name  # определение не готово — берём первого в приоритете
            return "libx264"
        known = self._hw_encoders
        if choice not in ("libx264",) and known is not None and choice not in known:
            self._toast(f"{ENCODER_LABELS.get(choice, choice)} недоступен — будет x264", color=WARN)
            return "libx264"
        return choice

    def _start(self, *_args) -> None:
        if self._busy:
            return
        if not self._order:
            self._alert("Нет файлов", "Сначала добавьте хотя бы одно видео.")
            return
        size = self._target_size()
        if size is None:
            self._alert("Ошибка", "Целевой размер должен быть положительным числом.")
            return
        outdir = dpg.get_value("inp_outdir").strip()
        if outdir and not Path(outdir).is_dir():
            self._alert("Ошибка", f"Папка не найдена:\n{outdir}")
            return
        if find_ffmpeg() is None:
            self._check_ffmpeg()
            return

        preset = SPEED_CHOICES.get(dpg.get_value(self.cmb_speed), "veryfast")
        encoder = self._resolved_encoder()
        max_height = self._res_setting()
        two_pass = bool(dpg.get_value("chk_two_pass"))
        if two_pass and encoder != "libx264":
            two_pass = False
            self._toast(
                "2 прохода работает только с CPU x264 — кодирую в один", color=WARN
            )
        keys = list(self._order)
        missing = [k for k in keys if not self._files[k]["path"].is_file()]
        if missing:
            self._alert("Ошибка", f"Файл не найден:\n{missing[0]}")
            return
        paths = {k: self._files[k]["path"] for k in keys}
        infos = {k: self._files[k].get("info") for k in keys}
        # Выходные пути считаем здесь: из фонового потока трогать UI нельзя.
        taken: set[Path] = set(paths.values())  # не перезаписываем источники
        outputs = {}
        for key in keys:
            outputs[key] = unique_output_path(self._output_for(paths[key]), taken)
            taken.add(outputs[key])

        self._log_clear()
        dpg.configure_item("result_bar", show=False)
        self._cancel_event.clear()
        self._set_busy(True)
        self._eta_ema = None
        dpg.configure_item(self.bar, default_value=0.0, overlay="0%")
        dpg.configure_item(self.lbl_stage, default_value="Подготовка…")
        dpg.configure_item(
            self.lbl_status,
            default_value=f"Сжатие: файлов — {len(keys)}, цель {size:g} МБ",
        )
        details = [f"кодировщик {ENCODER_LABELS.get(encoder, encoder)}"]
        if max_height is not None:
            details.append(
                "разрешение: авто" if max_height == "auto" else f"разрешение ≤{max_height}p"
            )
        if two_pass:
            details.append("2 прохода")
        self._log(f"Пакет: {len(keys)} файл(ов) · цель {size:g} МБ · " + ", ".join(details))

        def worker() -> None:
            started = time.monotonic()
            total = len(keys)
            ok_count = 0
            err_count = 0
            for idx, key in enumerate(keys, 1):
                if self._cancel_event.is_set():
                    self._events.put(("file_status", key, "— пропущено", MUTED))
                    continue
                src = paths[key]
                dst = outputs[key]
                self._events.put(("file_status", key, "сжимается…", WARN))

                def progress(frac, stage, idx=idx, t0=started):
                    overall = ((idx - 1) + (frac or 0.0)) / total
                    self._events.put(("progress", overall, stage, idx, total, t0))

                try:
                    result = compress(
                        src,
                        dst,
                        size,
                        preset=preset,
                        encoder=encoder,
                        max_height=max_height,
                        two_pass=two_pass,
                        info=infos.get(key),
                        progress=progress,
                        cancel=self._cancel_event,
                    )
                except VideoCompressorError as exc:
                    if "отмен" in str(exc).lower():
                        self._events.put(("file_status", key, "— отменено", MUTED))
                    else:
                        err_count += 1
                        self._events.put(("file_status", key, "✗ ошибка", ERR))
                        self._events.put(("log", f"{src.name}: ОШИБКА — {exc}"))
                        log.info("Ошибка %s: %s", src, exc)
                except Exception as exc:  # noqa: BLE001
                    err_count += 1
                    self._events.put(("file_status", key, "✗ ошибка", ERR))
                    self._events.put(("log", f"{src.name}: НЕПРЕДВИДЕННАЯ ОШИБКА — {exc}"))
                    log.exception("Ошибка сжатия %s", src)
                else:
                    ok_count += 1
                    self._last_output_dir = result.output_path.parent
                    self._last_output = result.output_path
                    self._events.put(("file_done", key, result.output_path))
                    encoder_note = (
                        f", кодировщик {ENCODER_LABELS.get(result.encoder, result.encoder)}"
                        if result.encoder != encoder else ""
                    )
                    scale_note = (
                        f", масштаб ↓{result.output_height}p"
                        if result.output_height else ""
                    )
                    color = OK if result.fits_target else WARN
                    self._events.put(
                        ("file_status", key, f"✓ {human_size(result.actual_size_mb)}", color)
                    )
                    attempts = f", попыток: {result.attempts}" if result.attempts > 1 else ""
                    self._events.put(
                        (
                            "log",
                            f"{src.name}: {human_size(result.actual_size_mb)} "
                            f"(цель {size:g} МБ{attempts}{encoder_note}{scale_note})",
                        )
                    )
            self._events.put(("batch_done", ok_count, err_count, total))

        threading.Thread(target=worker, daemon=True, name="compress-batch").start()

    def _on_cancel(self, *_args) -> None:
        if self._busy:
            self._cancel_event.set()
            dpg.configure_item(self.lbl_status, default_value="Останавливаю…")

    def _on_return(self, *_args) -> None:
        if not self._busy and self._order:
            self._start()

    def _on_ctrl_o(self, *_args) -> None:
        if dpg.is_key_down(dpg.mvKey_ModCtrl):
            self._add_files()

    def _output_for(self, src: Path) -> Path:
        folder = dpg.get_value("inp_outdir").strip()
        name = f"{src.stem}_compressed.mp4"
        return (Path(folder) if folder else src.parent) / name

    # ------------------------------------------------------------------ #
    # События из рабочих потоков
    # ------------------------------------------------------------------ #

    def _poll_events(self) -> None:
        if self._pending_alerts:
            self._flush_alerts()
        try:
            while True:
                self._handle_event(self._events.get_nowait())
        except queue.Empty:
            pass

    def _handle_event(self, event: tuple) -> None:
        kind = event[0]

        if kind == "probe":
            _, key, token, info, error = event
            if self._probe_token.get(key) != token or key not in self._files:
                return
            if info is None:
                dpg.configure_item(f"status_{key}", default_value="✗ не прочитан", color=ERR)
                self._toast(f"Не удалось прочитать: {self._files[key]['path'].name}", color=ERR)
                self._log(f"{self._files[key]['path'].name}: {error}")
                return
            self._files[key]["info"] = info
            params = (
                f"{info.resolution} · {format_duration(info.duration_sec)}"
                + (f" · {info.fps:g} fps" if info.fps else "")
            )
            dpg.configure_item(f"size_{key}", default_value=human_size(info.size_mb), color=MUTED)
            dpg.configure_item(f"params_{key}", default_value=params, color=MUTED)
            dpg.configure_item(f"status_{key}", default_value="готов", color=MUTED)
            self._refresh_files_summary()
            self._update_estimate()

        elif kind == "dropped":
            paths = [p for p in event[1] if Path(p).suffix.lower() in VIDEO_EXTS]
            skipped = len(event[1]) - len(paths)
            if skipped:
                self._toast(f"Пропущено файлов с неподдерживаемым форматом: {skipped}", color=WARN)
            self._append_paths(paths)

        elif kind == "file_done":
            _, key, output = event
            card = self._files.get(key)
            if card is not None:
                card["output"] = output
                dpg.configure_item(card["btn_open"], show=True)

        elif kind == "encoders":
            self._hw_encoders = event[1]
            self._update_ffmpeg_label()

        elif kind == "file_status":
            _, key, text, color = event
            if key in self._files:
                dpg.configure_item(f"status_{key}", default_value=text, color=color)

        elif kind == "progress":
            _, frac, stage, idx, total, t0 = event
            frac = max(0.0, min(frac, 1.0))
            elapsed = time.monotonic() - t0
            raw_eta = elapsed / frac - elapsed if frac > 0.02 else None
            if raw_eta is None:
                self._eta_ema = None
            elif self._eta_ema is None:
                self._eta_ema = raw_eta
            else:
                self._eta_ema = 0.3 * raw_eta + 0.7 * self._eta_ema
            overlay = f"{frac * 100:.0f}%"
            text = overlay
            if self._eta_ema is not None:
                text += f" · осталось ~{format_eta(self._eta_ema)}"
            if total > 1:
                text += f" · файл {idx}/{total}"
            dpg.configure_item(self.bar, default_value=frac, overlay=overlay)
            dpg.configure_item(self.lbl_stage, default_value=f"{text} · {stage}")
            dpg.configure_item(self.lbl_status, default_value=f"Сжатие {idx} из {total}…")

        elif kind == "log":
            self._log(event[1])

        elif kind == "batch_done":
            self._finish_batch(event[1], event[2], event[3])

        elif kind == "dl":
            _, frac, stage = event
            dpg.configure_item(self.lbl_stage, default_value=stage)
            dpg.configure_item(self.lbl_status, default_value=stage)
            if frac is not None:
                dpg.configure_item(self.bar, default_value=frac, overlay=f"{frac * 100:.0f}%")

        elif kind == "dl_done":
            dpg.configure_item(self.bar, default_value=0.0, overlay="")
            dpg.configure_item(
                self.lbl_stage, default_value="FFmpeg установлен — можно сжимать"
            )
            self._set_busy(False)
            self._update_ffmpeg_label()
            dpg.configure_item(self.lbl_status, default_value="FFmpeg успешно скачан")

        elif kind == "error":
            self._set_busy(False)
            dpg.configure_item(self.lbl_stage, default_value="Ошибка")
            dpg.configure_item(self.lbl_status, default_value="Ошибка")
            self._log(event[1])
            self._alert("Ошибка", event[1])

    def _finish_batch(self, ok_count: int, err_count: int, total: int) -> None:
        self._set_busy(False)
        skipped = total - ok_count - err_count
        parts = [f"Готово: {ok_count} из {total}"]
        if err_count:
            parts.append(f"ошибок: {err_count}")
        if skipped:
            parts.append(f"пропущено: {skipped}")
        summary = " · ".join(parts)
        dpg.configure_item(self.lbl_stage, default_value=summary)
        dpg.configure_item(self.lbl_status, default_value=summary)
        if ok_count:
            dpg.configure_item(self.bar, default_value=1.0, overlay="100%")
            dpg.configure_item(
                self.lbl_result,
                default_value=summary,
                color=OK if not err_count else WARN,
            )
            dpg.configure_item("result_bar", show=True)
            self._log(f"Итог: {ok_count} ok, {err_count} ошибок, {skipped} пропущено")
        elif err_count:
            dpg.configure_item(
                self.lbl_status,
                default_value="Ни один файл не удалось сжать — подробности ниже",
            )
            self._show_details(True)
        else:
            dpg.configure_item(self.bar, default_value=0.0, overlay="")
            dpg.configure_item(self.lbl_stage, default_value="Отменено пользователем")
            dpg.configure_item(self.lbl_status, default_value="Отменено пользователем")
        self._save_settings_partial()

    # ------------------------------------------------------------------ #
    # Разное
    # ------------------------------------------------------------------ #

    def _on_open_result(self, *_args) -> None:
        if self._last_output and self._last_output.exists():
            open_in_file_manager(self._last_output)
        elif self._last_output_dir and self._last_output_dir.exists():
            open_in_file_manager(
                next(self._last_output_dir.iterdir(), self._last_output_dir)
            )
        elif self._files:
            open_in_file_manager(next(iter(self._files.values()))["path"])

    def _open_file_result(self, key: str) -> None:
        """Открыть в проводнике результат конкретного файла."""
        output = self._files.get(key, {}).get("output")
        if output and Path(output).exists():
            open_in_file_manager(output)
        else:
            self._toast("Файл результата не найден", color=WARN)

    def _on_size_chip(self, mb: int) -> None:
        dpg.set_value("inp_size", str(mb))
        self._update_estimate()

    # ------------------------------------------------------------------ #
    # Тосты (немодальные уведомления)
    # ------------------------------------------------------------------ #

    def _toast(self, text: str, *, color: tuple = TEXT) -> None:
        """Короткое сообщение в углу окна; модалки не блокирует."""
        if self._toast_tag and dpg.does_item_exist(self._toast_tag):
            dpg.delete_item(self._toast_tag)
        self._toast_seq += 1
        tag = f"toast_{self._toast_seq}"
        self._toast_tag = tag
        width = min(430, dpg.get_viewport_width() - 40)
        with dpg.window(
            tag=tag,
            no_title_bar=True,
            no_move=True,
            no_resize=True,
            no_scrollbar=True,
            no_bring_to_front_on_focus=True,
            autosize=True,
        ):
            dpg.add_text(text, wrap=width - 30, color=color)
        dpg.set_item_pos(tag, (dpg.get_viewport_width() - width - 14, 14))
        dpg.show_item(tag)

        def _hide(*_args) -> None:
            if self._toast_tag == tag and dpg.does_item_exist(tag):
                dpg.configure_item(tag, show=False)

        self.after(4500, _hide)

    def _save_settings_partial(self, *_args) -> None:
        save_settings(
            {
                "size": dpg.get_value("inp_size"),
                "speed": dpg.get_value(self.cmb_speed),
                "encoder": ENCODER_CHOICES.get(dpg.get_value(self.cmb_encoder), "libx264"),
                "resolution": self._res_setting(),
                "two_pass": bool(dpg.get_value("chk_two_pass")),
                "details": bool(dpg.get_value("chk_details")),
                "output_dir": dpg.get_value("inp_outdir"),
                "width": dpg.get_viewport_width(),
                "height": dpg.get_viewport_height(),
            }
        )

    def _on_close(self, *_args) -> None:
        self._cancel_event.set()
        with contextlib.suppress(Exception):
            self._save_settings_partial()
        dpg.stop_dearpygui()


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, OSError):
            stream.reconfigure(errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    App().run()


if __name__ == "__main__":  # pragma: no cover
    main()
