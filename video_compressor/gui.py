"""Графический интерфейс Video Compressor (tkinter/ttk).

Возможности:
    * очередь из нескольких файлов с пакетным сжатием;
    * перетаскивание файлов в окно (если установлен tkinterdnd2);
    * светлая/тёмная темы;
    * прогресс по всем файлам с оставшимся временем;
    * панель «Подробности» с расчётом битрейта и логом FFmpeg;
    * настройки запоминаются между запусками;
    * горячие клавиши: Enter — сжать, Esc — отмена, Ctrl+O — добавить.
"""

from __future__ import annotations

import contextlib
import json
import logging
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:  # необязательная зависимость: drag & drop файлов
    from tkinterdnd2 import DND_FILES, TkinterDnD

    _DND_IMPORT_ERROR: BaseException | None = None
except Exception as exc:  # noqa: BLE001 — отсутствие библиотеки не критично
    DND_FILES = None  # type: ignore[assignment]
    TkinterDnD = None  # type: ignore[assignment]
    _DND_IMPORT_ERROR = exc

from . import __version__
from .core import (
    DEFAULT_TARGET_SIZE_MB,
    MediaInfo,
    VideoCompressorError,
    build_plan,
    compress,
    format_duration,
    probe,
)
from .ffmpeg_tools import download_ffmpeg, find_ffmpeg

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Темы
# --------------------------------------------------------------------------- #

PALETTES: dict[str, dict[str, str]] = {
    "light": {
        "bg": "#eef2f7",
        "card": "#ffffff",
        "border": "#d7dfe9",
        "text": "#111827",
        "muted": "#6b7280",
        "field": "#ffffff",
        "field_text": "#111827",
        "btn": "#e7edf5",
        "btn_hover": "#dbe4ef",
        "btn_text": "#1f2937",
        "accent": "#2563eb",
        "accent_hover": "#1d4ed8",
        "accent_text": "#ffffff",
        "accent_disabled": "#93c5fd",
        "ok": "#15803d",
        "err": "#dc2626",
        "warn": "#b45309",
        "log_bg": "#0f172a",
        "log_text": "#cbd5e1",
        "select": "#dbeafe",
        "select_text": "#111827",
    },
    "dark": {
        "bg": "#0f172a",
        "card": "#1e293b",
        "border": "#334155",
        "text": "#e2e8f0",
        "muted": "#94a3b8",
        "field": "#0b1220",
        "field_text": "#e2e8f0",
        "btn": "#334155",
        "btn_hover": "#3b4a61",
        "btn_text": "#e2e8f0",
        "accent": "#3b82f6",
        "accent_hover": "#2563eb",
        "accent_text": "#ffffff",
        "accent_disabled": "#1d4ed8",
        "ok": "#4ade80",
        "err": "#f87171",
        "warn": "#fbbf24",
        "log_bg": "#020617",
        "log_text": "#94a3b8",
        "select": "#1e40af",
        "select_text": "#f8fafc",
    },
}

FONT = ("Segoe UI", 10)
FONT_BOLD = ("Segoe UI", 10, "bold")
FONT_SMALL = ("Segoe UI", 9)
FONT_TITLE = ("Segoe UI", 15, "bold")

VIDEO_FILETYPES = (
    ("Видео", "*.mp4;*.mkv;*.avi;*.mov;*.wmv;*.flv;*.webm;*.ts;*.m4v"),
    ("Все файлы", "*.*"),
)

#: Надпись на кнопке скорости → пресет libx264.
SPEED_CHOICES = {
    "Быстро (рекомендуется)": "veryfast",
    "Сбалансировано": "medium",
    "Качественно (медленно)": "slow",
}
SIZE_PRESETS = ("8", "10", "16", "25", "50", "100", "250")

SETTINGS_NAME = "VideoCompressor"
MAX_LOG_LINES = 400


# --------------------------------------------------------------------------- #
# Настройки
# --------------------------------------------------------------------------- #


def settings_path() -> Path:
    """Путь к файлу настроек (Windows: %APPDATA%, иначе ~/.config)."""
    if sys.platform == "win32":
        base = Path(__import__("os").environ.get("APPDATA", Path.home()))
        return base / SETTINGS_NAME / "settings.json"
    return Path.home() / ".config" / "video-compressor" / "settings.json"


def load_settings() -> dict:
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_settings(data: dict) -> None:
    path = settings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        log.debug("Не удалось сохранить настройки в %s", path)


# --------------------------------------------------------------------------- #
# Вспомогательные элементы
# --------------------------------------------------------------------------- #


class Tooltip:
    """Всплывающая подсказка при наведении."""

    def __init__(self, widget: tk.Widget, text: str, colors: Callable[[], dict[str, str]]):
        self.widget = widget
        self.text = text
        self.colors = colors
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, _event=None) -> None:
        if self._tip is not None:
            return
        p = self.colors()
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        x = self.widget.winfo_rootx() + 14
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        tip.wm_geometry(f"+{x}+{y}")
        tk.Label(
            tip,
            text=self.text,
            bg=p["card"],
            fg=p["muted"],
            font=FONT_SMALL,
            relief="solid",
            bd=1,
            padx=8,
            pady=4,
        ).pack()
        self._tip = tip

    def _hide(self, _event=None) -> None:
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


def open_in_file_manager(path: Path) -> None:
    """Открыть папку и подсветить файл (Windows/macOS/Linux)."""
    path = path.resolve()
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path.parent)])
    except OSError:  # pragma: no cover
        log.warning("Не удалось открыть папку %s", path.parent)


def human_size(mb: float) -> str:
    return f"{mb / 1024:.2f} ГБ" if mb >= 1024 else f"{mb:.2f} МБ"


def format_eta(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def parse_drop_paths(data: str) -> list[str]:
    """Разобрать данные события <<Drop>> в список путей."""
    return [p for p in re.findall(r"\{([^{}]+)\}|(\S+)", data) for p in [p[0] or p[1]] if p]


# --------------------------------------------------------------------------- #
# Приложение
# --------------------------------------------------------------------------- #


class App(tk.Tk):
    """Главное окно программы."""

    def __init__(self) -> None:
        super().__init__()
        self.settings = load_settings()
        self.theme_name = self.settings.get("theme", "light")
        if self.theme_name not in PALETTES:
            self.theme_name = "light"
        self.palette = PALETTES[self.theme_name]

        self._themed: list[tuple[tk.Widget, str, str | None]] = []
        self._combos: list[ttk.Combobox] = []
        self._events: queue.Queue[tuple] = queue.Queue()
        self._cancel_event = threading.Event()
        self._busy = False
        self._info: dict[str, MediaInfo] = {}  # iid -> MediaInfo
        self._last_output_dir: Path | None = None
        self._last_output: Path | None = None
        self._dnd = False
        self._probe_seq = 0
        self._probe_token: dict[str, int] = {}

        # Переменные
        self.var_size = tk.StringVar(value=str(self.settings.get("size", DEFAULT_TARGET_SIZE_MB)))
        self.var_speed = tk.StringVar(value=self.settings.get("speed", next(iter(SPEED_CHOICES))))
        if self.var_speed.get() not in SPEED_CHOICES:
            self.var_speed.set(next(iter(SPEED_CHOICES)))
        self.var_outdir = tk.StringVar(value=self.settings.get("output_dir", ""))
        self.var_summary = tk.StringVar(value="")       # под списком файлов
        self.var_estimate = tk.StringVar(value="")      # оценка размера
        self.var_stage = tk.StringVar(value="Добавьте видеофайлы")  # строка прогресса
        self.var_percent = tk.StringVar(value="")
        self.var_status = tk.StringVar(value="Готово к работе")
        self.var_result = tk.StringVar(value="")        # итог после пакета
        self.var_ffmpeg = tk.StringVar(value="")
        self.var_theme_btn = tk.StringVar(
            value="Тёмная тема" if self.theme_name == "light" else "Светлая тема"
        )
        self.var_details = tk.BooleanVar(value=False)

        self.title(f"Video Compressor {__version__}")
        self.minsize(760, 660)
        self.geometry(self.settings.get("geometry", "760x660"))

        self._build_ui()
        self._apply_theme()
        self._bind_shortcuts()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.after(300, self._check_ffmpeg)
        self.after(150, self._enable_dnd)
        self.after(100, self._poll_events)

    # --------------------------------------------------------------------- #
    # Построение интерфейса
    # --------------------------------------------------------------------- #

    def _reg(self, widget: tk.Widget, bg_role: str, fg_role: str | None = None) -> tk.Widget:
        """Зарегистрировать tk-виджет для перекраски при смене темы."""
        self._themed.append((widget, bg_role, fg_role))
        return widget

    def _frame(self, parent, bg_role: str = "bg", **kw) -> tk.Frame:
        return self._reg(tk.Frame(parent, bg=self.palette[bg_role], **kw), bg_role)  # type: ignore[return-value]

    def _label(self, parent, text: str = "", bg_role: str = "bg", fg_role: str = "text", **kw) -> tk.Label:
        lbl = tk.Label(
            parent, text=text, bg=self.palette[bg_role], fg=self.palette[fg_role],
            font=kw.pop("font", FONT), **kw,
        )
        return self._reg(lbl, bg_role, fg_role)  # type: ignore[return-value]

    def _button(
        self,
        parent,
        text: str,
        command: Callable | None,
        *,
        primary: bool = False,
        font=FONT_BOLD,
    ) -> tk.Button:
        """Плоская кнопка с гарантированно рабочими цветами в любой теме."""
        role = "accent" if primary else "btn"
        fg_role = "accent_text" if primary else "btn_text"
        btn = tk.Button(
            parent,
            text=text,
            command=command,
            font=font,
            relief="flat",
            bd=0,
            padx=18 if primary else 12,
            pady=7 if primary else 6,
            bg=self.palette[role],
            fg=self.palette[fg_role],
            activebackground=self.palette[role + "_hover"],
            activeforeground=self.palette[fg_role],
            disabledforeground=self.palette["accent_disabled" if primary else "muted"],
            cursor="hand2",
            highlightthickness=0,
        )
        self._themed.append((btn, role, fg_role))
        return btn  # type: ignore[return-value]

    def _card(
        self,
        parent,
        title: str = "",
        *,
        row: int,
        sticky: str = "ew",
        pady: tuple[int, int] = (0, 10),
    ) -> tk.Frame:
        """Белая карточка с однопиксельной рамкой; размещается сразу через grid."""
        p = self.palette
        shell = tk.Frame(parent, bg=p["border"])
        self._reg(shell, "border")
        shell.grid(row=row, column=0, sticky=sticky, padx=18, pady=pady)
        inner = tk.Frame(shell, bg=p["card"], padx=14, pady=12)
        self._reg(inner, "card")
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        if title:
            self._label(inner, title, "card", "text", font=FONT_BOLD).pack(
                anchor="w", pady=(0, 4)
            )
        # Отдельный контейнер под содержимое: к нему можно применять grid,
        # не конфликтуя с pack-заголовком.
        body = tk.Frame(inner, bg=p["card"])
        self._reg(body, "card")
        body.pack(fill="both", expand=True)
        return body  # type: ignore[return-value]

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)  # карточка «Выполнение» растягивается

        # ---- Шапка ---- #
        header = self._frame(self, "bg")
        header.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 10))
        header.columnconfigure(0, weight=1)
        self._label(header, f"Video Compressor {__version__}", "bg", "text", font=FONT_TITLE).grid(
            row=0, column=0, sticky="w"
        )
        self._label(
            header, "Сжатие видео точно в заданный размер", "bg", "muted", font=FONT_SMALL
        ).grid(row=1, column=0, sticky="w")
        self._button(
            header, "", self._toggle_theme, font=FONT_SMALL
        ).grid(row=0, column=1, rowspan=2, sticky="e")
        theme_btn = self._themed[-1][0]
        theme_btn.configure(textvariable=self.var_theme_btn)  # type: ignore[attr-defined]

        # ---- Карточка: файлы ---- #
        card_files = self._card(self, "Видеофайлы", row=1)

        tools = self._frame(card_files, "card")
        tools.pack(fill="x", pady=(8, 6))
        btn_add = self._button(tools, "Добавить…", self._add_files)
        btn_add.pack(side="left")
        self.btn_remove = self._button(tools, "Убрать", self._remove_selected)
        self.btn_remove.pack(side="left", padx=(6, 0))
        self.btn_clear = self._button(tools, "Очистить", self._clear_files)
        self.btn_clear.pack(side="left", padx=(6, 0))
        self._label(
            tools, "или перетащите файлы в это окно", "card", "muted", font=FONT_SMALL
        ).pack(side="right")
        self._file_buttons = [btn_add, self.btn_remove, self.btn_clear]
        Tooltip(btn_add, "Выбрать одно или несколько видео (Ctrl+O)", self.get_palette)
        Tooltip(self.btn_clear, "Очистить список", self.get_palette)

        tree_box = self._frame(card_files, "card")
        tree_box.pack(fill="both", expand=True)
        columns = ("size", "params", "status")
        self.tree = ttk.Treeview(tree_box, columns=columns, show="tree headings", height=5,
                                 selectmode="extended")
        self.tree.heading("#0", text="Файл")
        self.tree.heading("size", text="Было")
        self.tree.heading("params", text="Параметры")
        self.tree.heading("status", text="Статус")
        self.tree.column("#0", width=300, minwidth=160, stretch=True)
        self.tree.column("size", width=90, minwidth=70, anchor="e", stretch=False)
        self.tree.column("params", width=170, minwidth=110, stretch=False)
        self.tree.column("status", width=150, minwidth=100, stretch=False)
        vsb = ttk.Scrollbar(tree_box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self._reveal_row)

        self._label(card_files, "", "card", "muted", font=FONT_SMALL, textvariable=self.var_summary)\
            .pack(anchor="w", pady=(6, 0))

        # ---- Карточка: параметры ---- #
        card_opts = self._card(self, "Параметры", row=2)
        grid = self._frame(card_opts, "card")
        grid.pack(fill="x", pady=(8, 0))
        grid.columnconfigure(1, weight=1)
        grid.columnconfigure(3, weight=1)

        # Целевой размер
        self._label(grid, "Целевой размер:", "card", "text").grid(row=0, column=0, sticky="w", padx=(0, 8))
        size_box = self._frame(grid, "card")
        size_box.grid(row=0, column=1, sticky="w")
        combo_size = ttk.Combobox(size_box, textvariable=self.var_size, values=SIZE_PRESETS,
                                  width=8, font=FONT)
        combo_size.pack(side="left")
        self._combos.append(combo_size)
        self._label(size_box, "МБ", "card", "muted").pack(side="left", padx=(6, 0))
        Tooltip(combo_size, "Сколько МБ должно получиться в итоге", self.get_palette)

        # Скорость
        self._label(grid, "Скорость:", "card", "text").grid(row=0, column=2, sticky="w", padx=(24, 8))
        combo_speed = ttk.Combobox(grid, textvariable=self.var_speed, values=list(SPEED_CHOICES),
                                   state="readonly", width=24, font=FONT)
        combo_speed.grid(row=0, column=3, sticky="w")
        self._combos.append(combo_speed)
        Tooltip(combo_speed, "Быстрее — меньше сжатие; медленнее — лучшее качество при том же размере",
                self.get_palette)

        # Папка сохранения
        self._label(grid, "Сохранить в:", "card", "text").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
        entry_out = ttk.Entry(grid, textvariable=self.var_outdir)
        entry_out.grid(row=1, column=1, sticky="ew", pady=(8, 0))
        self._button(grid, "Обзор…", self._choose_outdir).grid(row=1, column=2, padx=(24, 0), pady=(8, 0))
        self._label(
            grid, "пусто = рядом с исходными файлами", "card", "muted", font=FONT_SMALL
        ).grid(row=1, column=3, sticky="w", pady=(8, 0))

        self.var_size.trace_add("write", lambda *_: self._update_estimate())
        self.var_outdir.trace_add("write", lambda *_: self._save_settings_partial())

        self._label(card_opts, "", "card", "muted", font=FONT_SMALL,
                    textvariable=self.var_estimate).pack(anchor="w", pady=(8, 0))

        # ---- Карточка: выполнение ---- #
        card_run = self._card(self, "Выполнение", row=3, sticky="nsew", pady=(0, 6))
        card_run.rowconfigure(3, weight=1)

        bar_row = self._frame(card_run, "card")
        bar_row.grid(row=0, column=0, sticky="ew", pady=(8, 0))
        bar_row.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(bar_row, maximum=1.0)
        self.progress.grid(row=0, column=0, sticky="ew")
        self._label(bar_row, "", "card", "text", font=FONT_BOLD, textvariable=self.var_percent,
                    width=6, anchor="e").grid(row=0, column=1, padx=(10, 0))

        stage_row = self._frame(card_run, "card")
        stage_row.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        stage_row.columnconfigure(0, weight=1)
        self._label(stage_row, "", "card", "muted", font=FONT_SMALL,
                    textvariable=self.var_stage).grid(row=0, column=0, sticky="w")
        self.btn_cancel = self._button(stage_row, "Отмена", self._on_cancel)
        self.btn_cancel.grid(row=0, column=1, sticky="e")
        self.btn_cancel.configure(state="disabled")

        # «Подробности»
        details_row = self._frame(card_run, "card")
        details_row.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self.chk_details = ttk.Checkbutton(
            details_row, text="Подробности ▾", variable=self.var_details,
            command=self._toggle_details,
        )
        self.chk_details.pack(side="left")
        self._label(
            details_row, "расчёт битрейта и лог FFmpeg", "card", "muted", font=FONT_SMALL
        ).pack(side="left", padx=(8, 0))

        log_box = self._frame(card_run, "card")
        self.log_text = tk.Text(
            log_box, height=7, wrap="none", state="disabled", bd=0, font=("Consolas", 9),
            bg=self.palette["log_bg"], fg=self.palette["log_text"],
            insertbackground=self.palette["log_text"], highlightthickness=0,
            padx=10, pady=8,
        )
        self._reg(log_box, "card")
        self._themed.append((self.log_text, "log_bg", "log_text"))
        vsb2 = ttk.Scrollbar(log_box, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=vsb2.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        vsb2.pack(side="right", fill="y")
        self._log_box = log_box  # хранится для show/hide

        # ---- Нижняя панель: действие + итог ---- #
        actions = self._frame(self, "bg")
        actions.grid(row=4, column=0, sticky="ew", padx=18, pady=(2, 8))
        actions.columnconfigure(1, weight=1)
        self.btn_compress = self._button(
            actions, "Сжать видео", self._start, primary=True, font=("Segoe UI", 11, "bold")
        )
        self.btn_compress.grid(row=0, column=0, sticky="w")
        Tooltip(self.btn_compress, "Запустить сжатие (Enter)", self.get_palette)

        self.result_box = self._frame(actions, "bg")
        self.result_box.grid(row=0, column=1, sticky="e")
        self.lbl_result = self._label(
            self.result_box, "", "bg", "ok", font=FONT_SMALL, textvariable=self.var_result
        )
        self.lbl_result.pack(side="left", padx=(0, 10))
        self.btn_open_folder = self._button(
            self.result_box, "Открыть папку", self._on_open_result, font=FONT_SMALL
        )
        self.btn_open_folder.pack(side="left")
        self.result_box.grid(row=0, column=1, sticky="e")
        self.result_box.grid_remove()  # появится после успешного пакета

        hint = self._frame(self, "bg")
        hint.grid(row=5, column=0, sticky="ew", padx=18)
        self._label(hint, "Enter — сжать · Esc — отмена · Ctrl+O — добавить файлы",
                    "bg", "muted", font=FONT_SMALL).pack(anchor="w")

        # ---- Статусбар ---- #
        bar = self._frame(self, "bg")
        bar.grid(row=6, column=0, sticky="ew", padx=18, pady=(6, 10))
        bar.columnconfigure(0, weight=1)
        self._label(bar, "", "bg", "text", font=FONT_SMALL, textvariable=self.var_status)\
            .grid(row=0, column=0, sticky="w")
        self.lbl_ffmpeg = self._label(bar, "", "bg", "muted", font=FONT_SMALL,
                                      textvariable=self.var_ffmpeg)
        self.lbl_ffmpeg.grid(row=0, column=1, sticky="e")

        self._refresh_files_summary()

    # --------------------------------------------------------------------- #
    # Тема и стиль
    # --------------------------------------------------------------------- #

    def get_palette(self) -> dict[str, str]:
        return self.palette

    def _apply_theme(self) -> None:
        p = self.palette
        style = ttk.Style(self)
        style.theme_use("clam")

        # tk-виджеты
        for widget, bg_role, fg_role in self._themed:
            cfg: dict[str, str] = {"bg": p[bg_role]}
            if fg_role:
                if isinstance(widget, tk.Button) and fg_role == "btn_text":
                    cfg["fg"] = p["btn_text"]
                    cfg["activebackground"] = p["btn_hover"]
                    cfg["activeforeground"] = p["btn_text"]
                elif isinstance(widget, tk.Button) and fg_role == "accent_text":
                    cfg["fg"] = p["accent_text"]
                    cfg["activebackground"] = p["accent_hover"]
                    cfg["activeforeground"] = p["accent_text"]
                else:
                    cfg["fg"] = p[fg_role]
            with contextlib.suppress(tk.TclError):
                widget.configure(**cfg)

        # ttk-виджеты
        style.configure(".", background=p["bg"], foreground=p["text"], font=FONT)
        style.configure("TFrame", background=p["bg"])
        style.configure("TLabel", background=p["bg"], foreground=p["text"])
        style.configure("TButton", background=p["btn"], foreground=p["btn_text"],
                        bordercolor=p["border"], lightcolor=p["btn"], darkcolor=p["btn"],
                        focusthickness=1, relief="flat", padding=(10, 5), font=FONT)
        style.map("TButton",
                  background=[("active", p["btn_hover"]), ("disabled", p["bg"])],
                  foreground=[("disabled", p["muted"])])
        style.configure("TEntry", fieldbackground=p["field"], foreground=p["field_text"],
                        insertcolor=p["text"], bordercolor=p["border"],
                        lightcolor=p["border"], darkcolor=p["border"], relief="flat")
        style.configure("TCombobox", fieldbackground=p["field"], foreground=p["field_text"],
                        arrowcolor=p["muted"], bordercolor=p["border"],
                        lightcolor=p["border"], darkcolor=p["border"], relief="flat")
        style.map("TCombobox",
                  fieldbackground=[("readonly", p["field"])],
                  foreground=[("readonly", p["field_text"])],
                  selectbackground=[("readonly", p["field"])],
                  selectforeground=[("readonly", p["field_text"])])
        style.configure("TCheckbutton", background=p["card"], foreground=p["text"],
                        indicatorcolor=p["field"])
        style.map("TCheckbutton", background=[("active", p["card"])],
                  indicatorcolor=[("selected", p["accent"])])
        style.configure("Horizontal.TProgressbar", troughcolor=p["border"],
                        background=p["accent"], thickness=16)
        style.configure("Vertical.TScrollbar", background=p["btn"], troughcolor=p["card"],
                        arrowcolor=p["muted"], bordercolor=p["card"])
        style.configure("Treeview", background=p["card"], fieldbackground=p["card"],
                        foreground=p["text"], font=FONT, rowheight=26)
        style.configure("Treeview.Heading", background=p["btn"], foreground=p["muted"],
                        font=FONT_SMALL, relief="flat")
        style.map("Treeview", background=[("selected", p["select"])],
                  foreground=[("selected", p["select_text"])])

        self.tree.tag_configure("ok", foreground=p["ok"])
        self.tree.tag_configure("err", foreground=p["err"])
        self.tree.tag_configure("muted", foreground=p["muted"])
        self.tree.tag_configure("busy", foreground=p["warn"])

        # Лог
        self.log_text.configure(bg=p["log_bg"], fg=p["log_text"], insertbackground=p["log_text"])

        # Выпадающие списки комбобоксов (перекраска через Tcl: tkinter не
        # регистрирует popdown-виджет на своей стороне)
        self.option_add("*TCombobox*Listbox.background", p["field"])
        self.option_add("*TCombobox*Listbox.foreground", p["field_text"])
        self.option_add("*TCombobox*Listbox.selectBackground", p["select"])
        self.option_add("*TCombobox*Listbox.selectForeground", p["select_text"])
        for combo in self._combos:
            with contextlib.suppress(tk.TclError):
                self._recolor_listbox(combo, p)

        self.configure(bg=p["bg"])

    def _recolor_listbox(self, combo: ttk.Combobox, p: dict[str, str]) -> None:
        """Перекрасить выпадающий список комбобокса, если он уже создан."""
        popdown = self.tk.call("ttk::combobox::PopdownWindow", combo)
        stack = [popdown]
        while stack:
            widget = stack.pop()
            if self.tk.call("winfo", "class", widget) == "Listbox":
                self.tk.call(
                    widget, "configure",
                    "-background", p["field"],
                    "-foreground", p["field_text"],
                    "-selectbackground", p["select"],
                    "-selectforeground", p["select_text"],
                )
            children = self.tk.call("winfo", "children", widget)
            stack.extend(self.tk.splitlist(children))

    def _toggle_theme(self) -> None:
        self.theme_name = "dark" if self.theme_name == "light" else "light"
        self.palette = PALETTES[self.theme_name]
        self.var_theme_btn.set("Тёмная тема" if self.theme_name == "light" else "Светлая тема")
        self._apply_theme()
        self._save_settings_partial()

    # --------------------------------------------------------------------- #
    # Настройки, подсказки, горячие клавиши
    # --------------------------------------------------------------------- #

    def _save_settings_partial(self) -> None:
        save_settings({
            "theme": self.theme_name,
            "size": self.var_size.get(),
            "speed": self.var_speed.get(),
            "output_dir": self.var_outdir.get(),
            "geometry": self.geometry(),
        })

    def _bind_shortcuts(self) -> None:
        self.bind_all("<Return>", self._on_return, add="+")
        self.bind_all("<KP_Enter>", self._on_return, add="+")
        self.bind_all("<Escape>", lambda _e: self._on_cancel(), add="+")
        self.bind_all("<Control-o>", lambda _e: self._add_files(), add="+")

    def _on_return(self, event) -> str | None:
        if isinstance(event.widget, ttk.Combobox):
            return None  # не мешать выбору в списке
        if not self._busy and self.tree.get_children():
            self._start()
        return "break"

    def _choose_outdir(self) -> None:
        path = filedialog.askdirectory(title="Папка для сжатых файлов")
        if path:
            self.var_outdir.set(path)

    # --------------------------------------------------------------------- #
    # FFmpeg
    # --------------------------------------------------------------------- #

    def _check_ffmpeg(self) -> None:
        self._update_ffmpeg_label()
        if find_ffmpeg() is None:
            answer = messagebox.askyesno(
                "FFmpeg не найден",
                "Для работы нужен FFmpeg (бесплатный видеокодек).\n\n"
                "Скачать его автоматически в папку программы (~110 МБ, один раз)?\n\n"
                "Нет — если FFmpeg уже установлен, просто перезапустите программу.",
            )
            if answer:
                self._download_ffmpeg()

    def _update_ffmpeg_label(self) -> None:
        tools = find_ffmpeg()
        if tools is None:
            self.var_ffmpeg.set("FFmpeg не найден")
            self.lbl_ffmpeg.configure(fg=self.palette["err"])
        elif tools.is_portable:
            self.var_ffmpeg.set("FFmpeg: скачан программой")
            self.lbl_ffmpeg.configure(fg=self.palette["ok"])
        else:
            self.var_ffmpeg.set("FFmpeg: системный")
            self.lbl_ffmpeg.configure(fg=self.palette["ok"])

    def _download_ffmpeg(self) -> None:
        self._set_busy(True)
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)

        def worker() -> None:
            try:
                download_ffmpeg(
                    progress=lambda frac, stage: self._events.put(("dl", frac, stage))
                )
            except OSError as exc:
                self._events.put(("error", str(exc)))
            except Exception as exc:  # noqa: BLE001
                self._events.put(("error", f"Ошибка скачивания FFmpeg: {exc}"))
            else:
                self._events.put(("dl_done",))

        threading.Thread(target=worker, daemon=True, name="ffmpeg-download").start()

    # --------------------------------------------------------------------- #
    # Список файлов
    # --------------------------------------------------------------------- #

    def _add_files(self) -> None:
        if self._busy:
            return
        paths = filedialog.askopenfilenames(title="Выберите видеофайлы", filetypes=VIDEO_FILETYPES)
        self._append_paths(list(paths))

    def _append_paths(self, paths: list[str]) -> None:
        if self._busy:
            self.var_status.set("Дождитесь завершения текущей операции")
            return
        existing = {self.tree.item(iid, "text") for iid in self.tree.get_children()}
        added = 0
        for raw in paths:
            path = Path(raw.strip()).expanduser()
            if not path.is_file() or str(path) in existing:
                continue
            existing.add(str(path))
            iid = self.tree.insert("", "end", text=str(path), values=("", "", "анализ…"))
            self._probe_token[iid] = 0
            self._probe_start(iid, path)
            added += 1
        if added:
            self.var_result.set("")
            self.result_box.pack_forget()
            self.log_clear()
            self._refresh_files_summary()
            self._update_estimate()

    def _remove_selected(self) -> None:
        if self._busy:
            return
        for iid in self.tree.selection():
            self.tree.delete(iid)
            self._info.pop(iid, None)
            self._probe_token.pop(iid, None)
        self._refresh_files_summary()
        self._update_estimate()

    def _clear_files(self) -> None:
        if self._busy:
            return
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self._info.clear()
        self._probe_token.clear()
        self._refresh_files_summary()
        self._update_estimate()

    def _reveal_row(self, _event=None) -> None:
        sel = self.tree.selection()
        if sel:
            path = Path(self.tree.item(sel[0], "text"))
            if path.exists():
                open_in_file_manager(path)

    def _probe_start(self, iid: str, path: Path) -> None:
        token = self._probe_seq = self._probe_seq + 1
        self._probe_token[iid] = token

        def worker() -> None:
            try:
                info = probe(path)
            except VideoCompressorError as exc:
                self._events.put(("probe", iid, token, None, str(exc)))
            except OSError as exc:
                self._events.put(("probe", iid, token, None, f"Не удалось прочитать: {exc}"))
            else:
                self._events.put(("probe", iid, token, info, None))

        threading.Thread(target=worker, daemon=True, name="ffprobe").start()

    def _refresh_files_summary(self) -> None:
        count = len(self.tree.get_children())
        if not count:
            self.var_summary.set("Файлов нет — добавьте видео кнопкой «Добавить…» или перетаскиванием")
            return
        total = 0.0
        for iid in self.tree.get_children():
            info = self._info.get(iid)
            if info:
                total += info.size_mb
        self.var_summary.set(
            f"Файлов: {count}"
            + (f" · исходный объём: {human_size(total)}" if total else "")
        )

    def _output_for(self, src: Path) -> Path:
        folder = self.var_outdir.get().strip()
        name = f"{src.stem}_compressed.mp4"
        return (Path(folder) if folder else src.parent) / name

    # --------------------------------------------------------------------- #
    # Оценка
    # --------------------------------------------------------------------- #

    def _target_size(self) -> float | None:
        try:
            value = float(self.var_size.get().strip().replace(",", "."))
        except ValueError:
            return None
        return value if value > 0 else None

    def _update_estimate(self, *_args) -> None:
        size = self._target_size()
        if size is None:
            self.var_estimate.set("Целевой размер должен быть положительным числом")
            return
        infos = list(self._info.values())
        if not infos:
            self.var_estimate.set("Оценка появится после анализа файлов")
            return
        plans = [build_plan(i, size) for i in infos]
        max_est = max(p.estimated_size_mb for p in plans)
        max_br = max(p.video_bitrate_kbps for p in plans)
        suffix = "на файл" if len(plans) > 1 else f"для {infos[0].path.name}"
        self.var_estimate.set(
            f"Оценка: до {human_size(max_est)} {suffix} · видеобитрейт до {max_br} кбит/с"
        )

    # --------------------------------------------------------------------- #
    # Лог «Подробности»
    # --------------------------------------------------------------------- #

    def log(self, line: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{stamp}] {line}\n")
        count = int(self.log_text.index("end-1c").split(".")[0])
        if count > MAX_LOG_LINES:
            self.log_text.delete("1.0", f"{count - MAX_LOG_LINES}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def log_clear(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _toggle_details(self) -> None:
        if self.var_details.get():
            self._log_box.grid(row=3, column=0, sticky="nsew", pady=(8, 0))
        else:
            self._log_box.grid_remove()

    def _open_details(self) -> None:
        if not self.var_details.get():
            self.var_details.set(True)
            self._toggle_details()

    # --------------------------------------------------------------------- #
    # Запуск пакетного сжатия
    # --------------------------------------------------------------------- #

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.btn_compress.configure(state="normal" if not busy else "disabled")
        self.btn_cancel.configure(state="normal" if busy else "disabled")
        self.tree.configure(selectmode="extended" if not busy else "none")
        for button in getattr(self, "_file_buttons", []):
            button.configure(state="disabled" if busy else "normal")

    def _start(self) -> None:
        if self._busy:
            return
        iids = list(self.tree.get_children())
        if not iids:
            messagebox.showwarning("Нет файлов", "Сначала добавьте хотя бы одно видео.")
            return
        size = self._target_size()
        if size is None:
            messagebox.showerror("Ошибка", "Целевой размер должен быть положительным числом.")
            return
        outdir = self.var_outdir.get().strip()
        if outdir and not Path(outdir).is_dir():
            messagebox.showerror("Ошибка", f"Папка не найдена:\n{outdir}")
            return
        if find_ffmpeg() is None:
            self._check_ffmpeg()
            return

        preset = SPEED_CHOICES.get(self.var_speed.get(), "veryfast")
        paths = {iid: Path(self.tree.item(iid, "text")) for iid in iids}
        missing = [p for p in paths.values() if not p.is_file()]
        if missing:
            messagebox.showerror("Ошибка", f"Файл не найден:\n{missing[0]}")
            return
        # Выходные пути и параметры считаем здесь: из фонового потока
        # трогать tkinter нельзя.
        outputs = {iid: self._output_for(path) for iid, path in paths.items()}

        self.log_clear()
        self.result_box.grid_remove()
        self.var_result.set("")
        self._cancel_event.clear()
        self._set_busy(True)
        self.progress.configure(mode="determinate", value=0)
        self.var_percent.set("")
        self.var_stage.set("Подготовка…")
        self.var_status.set(f"Сжатие: файлов — {len(iids)}, цель {size:g} МБ")
        self.log(f"Пакет: {len(iids)} файл(ов), цель {size:g} МБ, пресет {preset}")

        def worker() -> None:
            started = time.monotonic()
            total = len(iids)
            ok_count = err_count = 0
            last_out: Path | None = None
            for idx, iid in enumerate(iids, 1):
                if self._cancel_event.is_set():
                    self._events.put(("file_status", iid, "— пропущено", "muted"))
                    continue
                src = paths[iid]
                dst = outputs[iid]
                self._events.put(("file_status", iid, "сжимается…", "busy"))

                def progress(frac, stage, idx=idx, t0=started):
                    overall = ((idx - 1) + (frac or 0)) / total
                    self._events.put(("progress", overall, stage, idx, total, t0))

                try:
                    result = compress(
                        src, dst, size, preset=preset,
                        progress=progress, cancel=self._cancel_event,
                    )
                except VideoCompressorError as exc:
                    if "отмен" in str(exc).lower():
                        self._events.put(("file_status", iid, "— отменено", "muted"))
                    else:
                        err_count += 1
                        self._events.put(("file_status", iid, "✗ ошибка", "err"))
                        self._events.put(("log", f"{src.name}: ОШИБКА — {exc}"))
                        log.info("Ошибка %s: %s", src, exc)
                except Exception as exc:  # noqa: BLE001
                    err_count += 1
                    self._events.put(("file_status", iid, "✗ ошибка", "err"))
                    self._events.put(("log", f"{src.name}: НЕПРЕДВИДЕННАЯ ОШИБКА — {exc}"))
                    log.exception("Ошибка сжатия %s", src)
                else:
                    ok_count += 1
                    last_out = result.output_path
                    self._last_output_dir = result.output_path.parent
                    self._last_output = result.output_path
                    tag = "ok" if result.fits_target else "busy"
                    status = f"✓ {human_size(result.actual_size_mb)}"
                    self._events.put(("file_status", iid, status, tag))
                    attempts = f", попыток: {result.attempts}" if result.attempts > 1 else ""
                    self._events.put(
                        ("log", f"{src.name}: {human_size(result.actual_size_mb)} "
                                f"(цель {size:g} МБ{attempts})")
                    )
            self._events.put(("batch_done", ok_count, err_count, total, last_out))

        threading.Thread(target=worker, daemon=True, name="compress-batch").start()

    def _on_cancel(self) -> None:
        if self._busy:
            self._cancel_event.set()
            self.var_status.set("Останавливаю…")

    # --------------------------------------------------------------------- #
    # События из рабочих потоков
    # --------------------------------------------------------------------- #

    def _poll_events(self) -> None:
        try:
            while True:
                self._handle_event(self._events.get_nowait())
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def _handle_event(self, event: tuple) -> None:
        kind = event[0]

        if kind == "probe":
            _, iid, token, info, error = event
            if self._probe_token.get(iid) != token or not self.tree.exists(iid):
                return
            if info is None:
                self.tree.set(iid, "status", "✗ не прочитан")
                self.tree.item(iid, tags=("err",))
                self.log(f"{Path(self.tree.item(iid, 'text')).name}: {error}")
                self.var_status.set(f"Не удалось прочитать файл: {error}")
                return
            self._info[iid] = info
            params = (
                f"{info.resolution} · {format_duration(info.duration_sec)}"
                + (f" · {info.fps:g} fps" if info.fps else "")
            )
            self.tree.set(iid, "size", human_size(info.size_mb))
            self.tree.set(iid, "params", params)
            self.tree.set(iid, "status", "готов")
            self.tree.item(iid, tags=("muted",))
            self._refresh_files_summary()
            self._update_estimate()

        elif kind == "file_status":
            _, iid, text, tag = event
            if self.tree.exists(iid):
                self.tree.set(iid, "status", text)
                self.tree.item(iid, tags=(tag,))

        elif kind == "progress":
            _, frac, stage, idx, total, t0 = event
            frac = max(0.0, min(frac, 1.0))
            self.progress.configure(value=frac)
            elapsed = time.monotonic() - t0
            text = f"{frac * 100:.0f}%"
            if frac > 0.02:
                text += f" · осталось ~{format_eta(elapsed / frac - elapsed)}"
            if total > 1:
                text += f" · файл {idx}/{total}"
            self.var_percent.set(f"{frac * 100:.0f}%")
            self.var_stage.set(f"{text} · {stage}")
            self.var_status.set(f"Сжатие {idx} из {total}…")

        elif kind == "log":
            self.log(event[1])

        elif kind == "batch_done":
            _, ok_count, err_count, total, _last_out = event
            self._finish_batch(ok_count, err_count, total)

        elif kind == "dl":
            _, frac, stage = event
            self.var_stage.set(stage)
            self.var_status.set(stage)
            if frac is not None:
                self.var_percent.set(f"{frac * 100:.0f}%")

        elif kind == "dl_done":
            self.progress.stop()
            self.progress.configure(mode="determinate", value=0)
            self.var_stage.set("FFmpeg установлен — можно сжимать")
            self.var_percent.set("")
            self._set_busy(False)
            self._update_ffmpeg_label()
            self.var_status.set("FFmpeg успешно скачан")

        elif kind == "error":
            self.progress.stop()
            self._set_busy(False)
            self.var_stage.set("Ошибка")
            self.var_percent.set("")
            self.var_status.set("Ошибка")
            self._open_details()
            self.log(event[1])
            messagebox.showerror("Ошибка", event[1])

    def _finish_batch(self, ok_count: int, err_count: int, total: int) -> None:
        self._set_busy(False)
        skipped = total - ok_count - err_count
        parts = [f"Готово: {ok_count} из {total}"]
        if err_count:
            parts.append(f"ошибок: {err_count}")
        if skipped:
            parts.append(f"пропущено: {skipped}")
        summary = " · ".join(parts)
        self.var_stage.set(summary)
        self.var_status.set(summary)
        if ok_count:
            self.progress.configure(value=1.0)
            self.var_percent.set("100%")
            self.var_result.set(summary)
            self.lbl_result.configure(
                fg=self.palette["ok"] if not err_count else self.palette["warn"]
            )
            self.result_box.grid()
            self.log(f"Итог: {ok_count} ok, {err_count} ошибок, {skipped} пропущено")
        elif err_count:
            self.var_status.set("Ни один файл не удалось сжать — подробности в панели ниже")
            self._open_details()
        else:
            self.progress.configure(value=0)
            self.var_percent.set("")
            self.var_stage.set("Отменено пользователем")
            self.var_status.set("Отменено пользователем")
        self._save_settings_partial()

    # --------------------------------------------------------------------- #
    # Разное
    # --------------------------------------------------------------------- #

    def _on_open_result(self) -> None:
        if self._last_output and self._last_output.exists():
            open_in_file_manager(self._last_output)
        elif self._last_output_dir and self._last_output_dir.exists():
            open_in_file_manager(next(self._last_output_dir.iterdir(), self._last_output_dir))
        elif self._info:
            open_in_file_manager(next(iter(self._info.values())).path)

    def _enable_dnd(self) -> None:
        if TkinterDnD is None:
            log.debug("tkinterdnd2 недоступен (%s) — drag&drop выключен", _DND_IMPORT_ERROR)
            return
        try:
            TkinterDnD.require(self)
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", self._on_drop)
            self.dnd_bind("<<DropEnter>>", lambda _e: "copy")
            self.dnd_bind("<<DropPosition>>", lambda _e: "copy")
        except Exception as exc:  # noqa: BLE001 — важна только работоспособность
            log.debug("Не удалось включить drag&drop: %s", exc)

    def _on_drop(self, event) -> str:
        paths = parse_drop_paths(event.data)
        if paths:
            self._append_paths(paths)
            self.var_status.set(f"Добавлено файлов: {len(paths)}")
        return "copy"

    def _on_close(self) -> None:
        self._cancel_event.set()
        self._save_settings_partial()
        self.destroy()


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, OSError):
            stream.reconfigure(errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    app = App()
    app.mainloop()


if __name__ == "__main__":  # pragma: no cover
    main()
