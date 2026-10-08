"""Графический интерфейс Video Compressor на Dear PyGui.

Dear PyGui рисует через GPU (DirectX/OpenGL) с настоящим сглаживанием —
в отличие от tkinter, где края виджетов рисуются «пикселями».

Возможности:
    * чёрный минимализм со скруглениями и «мыльными» кнопками;
    * очередь из нескольких файлов с пакетным сжатием;
    * прогресс по всем файлам с оставшимся временем;
    * панель «Подробности» с расчётом битрейта и логом FFmpeg;
    * настройки запоминаются между запусками;
    * горячие клавиши: Enter — сжать, Esc — отмена, Ctrl+O — добавить.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import logging
import queue
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import dearpygui.dearpygui as dpg

from . import __version__
from .core import (
    DEFAULT_TARGET_SIZE_MB,
    VideoCompressorError,
    build_plan,
    compress,
    format_duration,
    probe,
)
from .ffmpeg_tools import download_ffmpeg, find_ffmpeg

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Палитра: чёрный минимализм (RGBA)
# --------------------------------------------------------------------------- #

BG = (0, 0, 0, 255)
CARD = (8, 8, 10, 255)
LINE = (22, 22, 27, 255)
BORDER = (36, 36, 43, 255)
TEXT = (236, 236, 241, 255)
MUTED = (119, 119, 127, 255)
FIELD = (10, 10, 12, 255)
BTN = (11, 11, 14, 255)
BTN_HOVER = (22, 22, 28, 255)
BTN_ACTIVE = (32, 32, 40, 255)
ACCENT = (125, 211, 252, 255)  # #7dd3fc
BUBBLE = (165, 243, 252, 255)  # #a5f3fc — «мыльная» кромка
OK = (110, 231, 183, 255)  # #6ee7b7
ERR = (252, 165, 165, 255)  # #fca5a5
WARN = (253, 230, 138, 255)  # #fde68a

SETTINGS_NAME = "VideoCompressor"
MAX_LOG_LINES = 400
WINDOW_MIN_W, WINDOW_MIN_H = 720, 620

#: Надпись на кнопке скорости → пресет libx264.
SPEED_CHOICES = {
    "Быстро (рекомендуется)": "veryfast",
    "Сбалансировано": "medium",
    "Максимальное качество": "slow",
}


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


def format_eta(seconds: float) -> str:
    if seconds < 0 or seconds > 86400:
        return "—"
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


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
        self._check_on_start = check_ffmpeg

        # Шрифты; если системные не найдены — остаётся None.
        self._font = None
        self._font_bold = None
        self._font_mono = None

        dpg.create_context()
        self._setup_fonts()
        self._setup_theme()
        self._create_viewport()
        self._build_ui()

    # ------------------------------------------------------------------ #
    # Шрифты и тема
    # ------------------------------------------------------------------ #

    def _setup_fonts(self) -> None:
        with dpg.font_registry():
            for path, size, attr in (
                ("C:/Windows/Fonts/segoeui.ttf", 16, "_font"),
                ("C:/Windows/Fonts/segoeuib.ttf", 16, "_font_bold"),
                ("C:/Windows/Fonts/consola.ttf", 14, "_font_mono"),
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
        with dpg.theme() as theme, dpg.theme_component(dpg.mvAll):
            dpg.add_theme_color(dpg.mvThemeCol_WindowBg, BG)
            dpg.add_theme_color(dpg.mvThemeCol_ChildBg, BG)
            dpg.add_theme_color(dpg.mvThemeCol_PopupBg, CARD)
            dpg.add_theme_color(dpg.mvThemeCol_Text, TEXT)
            dpg.add_theme_color(dpg.mvThemeCol_TextDisabled, MUTED)
            dpg.add_theme_color(dpg.mvThemeCol_Border, BORDER)
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
            dpg.add_theme_color(dpg.mvThemeCol_PlotHistogram, BUBBLE)
            dpg.add_theme_color(dpg.mvThemeCol_Separator, LINE)
            dpg.add_theme_color(dpg.mvThemeCol_TableHeaderBg, CARD)
            dpg.add_theme_color(dpg.mvThemeCol_TableBorderLight, LINE)
            dpg.add_theme_color(dpg.mvThemeCol_TableBorderStrong, BORDER)
            dpg.add_theme_color(dpg.mvThemeCol_ScrollbarBg, BG)
            dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrab, BORDER)
            dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrabHovered, MUTED)
            dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 12)
            dpg.add_theme_style(dpg.mvStyleVar_WindowRounding, 0)
            dpg.add_theme_style(dpg.mvStyleVar_PopupRounding, 12)
            dpg.add_theme_style(dpg.mvStyleVar_ScrollbarRounding, 8)
            dpg.add_theme_style(dpg.mvStyleVar_GrabRounding, 12)
            dpg.add_theme_style(dpg.mvStyleVar_FrameBorderSize, 1)
            dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 12, 10)
            dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 8, 8)
        dpg.bind_theme(theme)

        # Кнопки с «мыльной» кромкой: главная — яркая, вторичные — тихие.
        with dpg.theme() as btn_secondary, dpg.theme_component(dpg.mvButton):
            dpg.add_theme_color(dpg.mvThemeCol_Border, BORDER)
            dpg.add_theme_style(dpg.mvStyleVar_FrameBorderSize, 1)
        with dpg.theme() as btn_primary, dpg.theme_component(dpg.mvButton):
            dpg.add_theme_color(dpg.mvThemeCol_Border, BUBBLE)
            dpg.add_theme_color(dpg.mvThemeCol_Button, (10, 13, 20, 255))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (20, 28, 40, 255))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (30, 40, 56, 255))
            dpg.add_theme_style(dpg.mvStyleVar_FrameBorderSize, 1)
        self._theme_secondary = btn_secondary
        self._theme_primary = btn_primary

    def _create_viewport(self) -> None:
        width = int(self.settings.get("width", 820))
        height = int(self.settings.get("height", 700))
        width = max(width, WINDOW_MIN_W)
        height = max(height, WINDOW_MIN_H)
        dpg.create_viewport(
            title=f"Video Compressor {__version__}",
            width=width,
            height=height,
            min_width=WINDOW_MIN_W,
            min_height=WINDOW_MIN_H,
        )
        dpg.set_viewport_clear_color(BG)

    def _bubble_button(
        self,
        label: str,
        callback: Callable | None,
        *,
        primary: bool = False,
        width: int = 0,
        small: bool = False,
    ) -> int | str:
        """Кнопка с тонкой кромкой и скруглением (сглаженная отрисовка)."""
        btn = dpg.add_button(
            label=label,
            callback=callback,
            width=width,
            height=28 if small else 32,
        )
        dpg.bind_item_theme(btn, self._theme_primary if primary else self._theme_secondary)
        if self._font_bold is not None:
            dpg.bind_item_font(btn, self._font_bold)
        return btn

    # ------------------------------------------------------------------ #
    # Построение интерфейса
    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        with dpg.window(tag="main", no_title_bar=True, no_move=True, no_resize=True):
            # ---- Шапка ---- #
            with dpg.group(horizontal=True):
                title = dpg.add_text(f"Video Compressor {__version__}")
                if self._font_bold is not None:
                    dpg.bind_item_font(title, self._font_bold)
                dpg.add_text("   ·   сжатие видео точно в заданный размер", color=MUTED)
            dpg.add_separator()

            # ---- Видеофайлы ---- #
            dpg.add_text("ВИДЕОФАЙЛЫ", color=MUTED)
            with dpg.group(horizontal=True):
                self.btn_add = self._bubble_button("Добавить…", self._add_files, small=True)
                self.btn_clear = self._bubble_button("Очистить", self._clear_files, small=True)
            self.lbl_summary = dpg.add_text(
                "Файлов нет — добавьте видео кнопкой «Добавить…»", color=MUTED
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
                height=170,
            ):
                dpg.add_table_column(label="Файл", init_width_or_weight=5)
                dpg.add_table_column(label="Было", init_width_or_weight=1)
                dpg.add_table_column(label="Параметры", init_width_or_weight=3)
                dpg.add_table_column(label="Статус", init_width_or_weight=2)
                dpg.add_table_column(label="", init_width_or_weight=0, width=28)
            self.lbl_estimate = dpg.add_text("", color=MUTED)

            dpg.add_separator()

            # ---- Параметры ---- #
            dpg.add_text("ПАРАМЕТРЫ", color=MUTED)
            with dpg.group(horizontal=True):
                with dpg.group(width=136):
                    dpg.add_text("Целевой размер", color=MUTED)
                self.inp_size = dpg.add_input_text(
                    tag="inp_size",
                    width=100,
                    default_value=str(self.settings.get("size", DEFAULT_TARGET_SIZE_MB)),
                    hint="МБ",
                )
                dpg.add_text("на каждый файл", color=MUTED)
            with dpg.group(horizontal=True):
                with dpg.group(width=136):
                    dpg.add_text("Скорость", color=MUTED)
                speed = self.settings.get("speed", next(iter(SPEED_CHOICES)))
                if speed not in SPEED_CHOICES:
                    speed = next(iter(SPEED_CHOICES))
                self.cmb_speed = dpg.add_combo(
                    items=list(SPEED_CHOICES),
                    default_value=speed,
                    width=280,
                )
            with dpg.group(horizontal=True):
                with dpg.group(width=136):
                    dpg.add_text("Папка сохранения", color=MUTED)
                self.inp_outdir = dpg.add_input_text(
                    tag="inp_outdir",
                    width=-1,
                    hint="Рядом с исходным файлом",
                    default_value=self.settings.get("output_dir", ""),
                )
                self._bubble_button("Обзор…", self._choose_outdir, small=True)

            dpg.add_separator()

            # ---- Выполнение ---- #
            dpg.add_text("ВЫПОЛНЕНИЕ", color=MUTED)
            self.bar = dpg.add_progress_bar(
                tag="bar", default_value=0.0, width=-1, height=18, overlay=""
            )
            self.lbl_stage = dpg.add_text("Добавьте видеофайлы", color=MUTED)
            with dpg.group(horizontal=True):
                self.btn_compress = self._bubble_button(
                    "Сжать видео", self._start, primary=True
                )
                self.btn_cancel = self._bubble_button("Отмена", self._on_cancel, small=True)
                dpg.add_checkbox(
                    label="Подробности",
                    tag="chk_details",
                    callback=self._toggle_details,
                )

            # ---- Лог ---- #
            self.inp_log = dpg.add_input_text(
                tag="inp_log",
                multiline=True,
                readonly=True,
                width=-1,
                height=140,
                default_value="",
                show=False,
            )
            if self._font_mono is not None:
                dpg.bind_item_font(self.inp_log, self._font_mono)

            # ---- Итог ---- #
            with dpg.group(horizontal=True, tag="result_bar", show=False):
                self.lbl_result = dpg.add_text("", color=OK)
                self._bubble_button("Открыть папку", self._on_open_result, small=True)

            # ---- Статусбар ---- #
            dpg.add_separator()
            with dpg.group(horizontal=True):
                self.lbl_status = dpg.add_text("Готово к работе")
                dpg.add_text(
                    "   ·   Enter — сжать · Esc — отмена · Ctrl+O — добавить",
                    color=MUTED,
                )
            self.lbl_ffmpeg = dpg.add_text("", color=MUTED)
            self._update_ffmpeg_label()

        self._set_busy(False)
        self._update_estimate()

    # ------------------------------------------------------------------ #
    # Главный цикл
    # ------------------------------------------------------------------ #

    def run(self) -> None:
        dpg.setup_dearpygui()
        dpg.set_exit_callback(self._on_close)
        dpg.show_viewport()
        dpg.set_primary_window("main", True)
        with dpg.handler_registry():
            dpg.add_key_press_handler(dpg.mvKey_Return, callback=self._on_return)
            dpg.add_key_press_handler(dpg.mvKey_Escape, callback=self._on_cancel)
            dpg.add_key_press_handler(dpg.mvKey_O, callback=self._on_ctrl_o)
        if self._check_on_start:
            self.after(400, self._check_ffmpeg)
        while dpg.is_dearpygui_running():
            self._poll_events()
            dpg.render_dearpygui_frame()
        dpg.destroy_context()

    def after(self, delay_ms: int, callback: Callable) -> None:
        """Отложить вызов до указанного кадра."""
        frames = max(dpg.get_frame_count() + max(delay_ms // 16, 1), 2)
        dpg.set_frame_callback(frames, callback)

    # ------------------------------------------------------------------ #
    # Диалоги и окна
    # ------------------------------------------------------------------ #

    def _alert(self, title: str, text: str) -> None:
        self._alert_seq += 1
        tag = f"alert_{self._alert_seq}"
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
                callback=lambda *_: dpg.delete_item(tag),
            )
        dpg.show_item(tag)

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
        elif tools.is_portable:
            dpg.configure_item(
                self.lbl_ffmpeg, default_value="FFmpeg: скачан программой", color=OK
            )
        else:
            dpg.configure_item(self.lbl_ffmpeg, default_value="FFmpeg: системный", color=OK)

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
            return
        self._append_paths(pick_video_files())

    def _append_paths(self, paths: list[str]) -> None:
        if self._busy:
            dpg.configure_item(
                self.lbl_status, default_value="Дождитесь завершения текущей операции"
            )
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
            dpg.add_text("  ")
            btn_x = dpg.add_button(
                label="×",
                width=24,
                height=22,
                callback=lambda *_args, k=key: self._remove_file(k),
            )
            dpg.bind_item_theme(btn_x, self._theme_secondary)
        self._files[key] = {"path": path, "row": row}
        self._order.append(key)
        self._probe_start(key, path)

    @staticmethod
    def _short_name(path: Path) -> str:
        name = path.name
        return name if len(name) <= 48 else f"{name[:30]}…{name[-15:]}"

    def _remove_file(self, key: str, *_args) -> None:
        if self._busy or key not in self._files:
            return
        dpg.delete_item(self._files[key]["row"])
        self._files.pop(key, None)
        self._order = [k for k in self._order if k != key]
        self._probe_token.pop(key, None)
        self._refresh_files_summary()
        self._update_estimate()

    def _clear_files(self, *_args) -> None:
        if self._busy:
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
        if not count:
            dpg.configure_item(
                self.lbl_summary,
                default_value="Файлов нет — добавьте видео кнопкой «Добавить…»",
            )
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
            )
            return
        infos = [card["info"] for card in self._files.values() if card.get("info")]
        if not infos:
            dpg.configure_item(
                self.lbl_estimate, default_value="Оценка появится после анализа файлов"
            )
            return
        plans = [build_plan(info, size) for info in infos]
        max_est = max(p.estimated_size_mb for p in plans)
        max_br = max(p.video_bitrate_kbps for p in plans)
        suffix = "на файл" if len(plans) > 1 else f"для {infos[0].path.name}"
        dpg.configure_item(
            self.lbl_estimate,
            default_value=f"Оценка: до {human_size(max_est)} {suffix}"
            f" · видеобитрейт до {max_br} кбит/с",
        )

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
        dpg.configure_item(self.inp_log, show=bool(dpg.get_value("chk_details")))

    def _show_details(self, show: bool) -> None:
        dpg.set_value("chk_details", show)
        dpg.configure_item(self.inp_log, show=show)

    # ------------------------------------------------------------------ #
    # Запуск пакетного сжатия
    # ------------------------------------------------------------------ #

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for tag in (self.btn_add, self.btn_clear):
            dpg.configure_item(tag, enabled=not busy)
        dpg.configure_item(self.btn_compress, enabled=not busy)
        dpg.configure_item(self.btn_cancel, enabled=busy)
        dpg.configure_item("inp_size", enabled=not busy)
        dpg.configure_item(self.cmb_speed, enabled=not busy)
        dpg.configure_item(self.inp_outdir, enabled=not busy)

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
        keys = list(self._order)
        missing = [k for k in keys if not self._files[k]["path"].is_file()]
        if missing:
            self._alert("Ошибка", f"Файл не найден:\n{missing[0]}")
            return
        paths = {k: self._files[k]["path"] for k in keys}
        # Выходные пути считаем здесь: из фонового потока трогать UI нельзя.
        outputs = {k: self._output_for(p) for k, p in paths.items()}

        self._log_clear()
        dpg.configure_item("result_bar", show=False)
        self._cancel_event.clear()
        self._set_busy(True)
        dpg.configure_item(self.bar, default_value=0.0, overlay="0%")
        dpg.configure_item(self.lbl_stage, default_value="Подготовка…")
        dpg.configure_item(
            self.lbl_status,
            default_value=f"Сжатие: файлов — {len(keys)}, цель {size:g} МБ",
        )
        self._log(f"Пакет: {len(keys)} файл(ов), цель {size:g} МБ, пресет {preset}")

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
                    color = OK if result.fits_target else WARN
                    self._events.put(
                        ("file_status", key, f"✓ {human_size(result.actual_size_mb)}", color)
                    )
                    attempts = f", попыток: {result.attempts}" if result.attempts > 1 else ""
                    self._events.put(
                        (
                            "log",
                            f"{src.name}: {human_size(result.actual_size_mb)} "
                            f"(цель {size:g} МБ{attempts})",
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
                self._log(f"{self._files[key]['path'].name}: {error}")
                dpg.configure_item(
                    self.lbl_status, default_value=f"Не удалось прочитать файл: {error}"
                )
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

        elif kind == "file_status":
            _, key, text, color = event
            if key in self._files:
                dpg.configure_item(f"status_{key}", default_value=text, color=color)

        elif kind == "progress":
            _, frac, stage, idx, total, t0 = event
            frac = max(0.0, min(frac, 1.0))
            elapsed = time.monotonic() - t0
            overlay = f"{frac * 100:.0f}%"
            text = overlay
            if frac > 0.02:
                text += f" · осталось ~{format_eta(elapsed / frac - elapsed)}"
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

    def _save_settings_partial(self, *_args) -> None:
        save_settings(
            {
                "size": dpg.get_value("inp_size"),
                "speed": dpg.get_value(self.cmb_speed),
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
