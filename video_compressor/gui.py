"""Графический интерфейс Video Compressor (tkinter/ttk, без внешних зависимостей)."""

from __future__ import annotations

import contextlib
import logging
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .core import (
    DEFAULT_TARGET_SIZE_MB,
    CompressionResult,
    MediaInfo,
    VideoCompressorError,
    build_plan,
    compress,
    default_output_path,
    format_duration,
    probe,
)
from .ffmpeg_tools import download_ffmpeg, find_ffmpeg

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Стили
# --------------------------------------------------------------------------- #

ACCENT = "#2563eb"
ACCENT_HOVER = "#1d4ed8"
BG = "#f4f5f7"
PANEL = "#ffffff"
TEXT = "#1f2328"
MUTED = "#6b7280"
FONT = ("Segoe UI", 10)
FONT_BOLD = ("Segoe UI", 10, "bold")
FONT_SMALL = ("Segoe UI", 9)

VIDEO_FILETYPES = (
    ("Видео", "*.mp4;*.mkv;*.avi;*.mov;*.wmv;*.flv;*.webm;*.ts;*.m4v"),
    ("Все файлы", "*.*"),
)

#: Скорость ↔ пресет libx264.
SPEED_CHOICES = {
    "Быстро (рекомендуется)": "veryfast",
    "Сбалансировано": "medium",
    "Качественно (медленно)": "slow",
}

SIZE_PRESETS = ("8", "10", "16", "25", "50", "100", "250")


# --------------------------------------------------------------------------- #
# Вспомогательные функции
# --------------------------------------------------------------------------- #


def _open_in_file_manager(path: Path) -> None:
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


def _human_size(mb: float) -> str:
    if mb >= 1024:
        return f"{mb / 1024:.2f} ГБ"
    return f"{mb:.2f} МБ"


def _format_eta(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


# --------------------------------------------------------------------------- #
# Приложение
# --------------------------------------------------------------------------- #


class App(tk.Tk):
    """Главное окно программы."""

    def __init__(self) -> None:
        super().__init__()
        self.title(f"Video Compressor {__version__}")
        self.minsize(640, 560)
        self.configure(bg=BG)

        # Данные
        self._info: MediaInfo | None = None
        self._result: Path | None = None
        self._events: queue.Queue[tuple] = queue.Queue()
        self._cancel_event = threading.Event()
        self._busy = False  # сжатие или скачивание ffmpeg
        self._probe_token = 0  # защита от гонок быстрых кликов по файлам

        # Переменные
        self.var_input = tk.StringVar()
        self.var_output = tk.StringVar()
        self.var_size = tk.StringVar(value=str(DEFAULT_TARGET_SIZE_MB))
        self.var_speed = tk.StringVar(value=next(iter(SPEED_CHOICES)))
        self.var_info = tk.StringVar(value="Выберите видеофайл, чтобы увидеть его параметры")
        self.var_estimate = tk.StringVar(value="")
        self.var_status = tk.StringVar(value="Готово к работе")
        self.var_progress = tk.StringVar(value="")
        self.var_ffmpeg = tk.StringVar(value="")

        self._init_styles()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.after(100, self._poll_events)
        self.after(300, self._check_ffmpeg)

    # --------------------------------------------------------------------- #
    # Стили и разметка
    # --------------------------------------------------------------------- #

    def _init_styles(self) -> None:
        try:
            theme = "vista" if "vista" in ttk.Style().theme_names() else "clam"
        except tk.TclError:  # pragma: no cover
            theme = "clam"
        style = ttk.Style(self)
        style.theme_use(theme)

        style.configure(".", font=FONT, background=BG, foreground=TEXT)
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Panel.TLabel", background=PANEL)
        style.configure("Muted.TLabel", background=PANEL, foreground=MUTED, font=FONT_SMALL)
        style.configure("Title.TLabel", background=BG, foreground=TEXT,
                        font=("Segoe UI", 16, "bold"))
        style.configure("Header.TLabel", background=PANEL, foreground=TEXT, font=FONT_BOLD)
        style.configure("Status.TLabel", background=BG, foreground=MUTED, font=FONT_SMALL)

        style.configure(
            "Accent.TButton",
            font=("Segoe UI", 11, "bold"),
            padding=(24, 10),
            foreground="white",
            background=ACCENT,
        )
        style.map(
            "Accent.TButton",
            background=[("active", ACCENT_HOVER), ("disabled", "#93c5fd")],
            foreground=[("disabled", "#eff6ff")],
        )
        style.configure("TButton", padding=(10, 5))
        style.configure("Horizontal.TProgressbar", thickness=18, troughcolor="#e5e7eb")

    def _panel(self, parent, title: str) -> ttk.Frame:
        """Белая карточка с заголовком."""
        frame = ttk.Frame(parent, style="Panel.TFrame", padding=14)
        frame.pack(fill="x", padx=16, pady=(0, 10))
        if title:
            ttk.Label(frame, text=title, style="Header.TLabel").pack(anchor="w")
        return frame

    def _build_ui(self) -> None:
        ttk.Label(self, text="Сжатие видео до нужного размера", style="Title.TLabel").pack(
            anchor="w", padx=16, pady=(16, 2)
        )
        ttk.Label(
            self,
            text="Укажите лимит в мегабайтах — программа сама подберёт битрейт",
            style="Status.TLabel",
        ).pack(anchor="w", padx=16, pady=(0, 14))

        # ---- Исходный файл ---- #
        p1 = self._panel(self, "Исходный файл")
        row = ttk.Frame(p1, style="Panel.TFrame")
        row.pack(fill="x")
        ttk.Entry(row, textvariable=self.var_input).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Обзор…", command=self._choose_input).pack(side="left", padx=(8, 0))
        ttk.Label(p1, textvariable=self.var_info, style="Muted.TLabel").pack(
            anchor="w", pady=(8, 0)
        )

        # ---- Результат и параметры ---- #
        p2 = self._panel(self, "Параметры")
        grid = ttk.Frame(p2, style="Panel.TFrame")
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)

        ttk.Label(grid, text="Сохранить в:", style="Panel.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 10), pady=4
        )
        ttk.Entry(grid, textvariable=self.var_output).grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Button(grid, text="Обзор…", command=self._choose_output).grid(
            row=0, column=2, padx=(8, 0), pady=4
        )

        ttk.Label(grid, text="Целевой размер:", style="Panel.TLabel").grid(
            row=1, column=0, sticky="w", padx=(0, 10), pady=4
        )
        size_box = ttk.Frame(grid, style="Panel.TFrame")
        size_box.grid(row=1, column=1, sticky="w", pady=4)
        ttk.Combobox(
            size_box, textvariable=self.var_size, values=SIZE_PRESETS,
            width=8, font=FONT,
        ).pack(side="left")
        ttk.Label(size_box, text="МБ", style="Panel.TLabel").pack(side="left", padx=(6, 0))
        self.var_size.trace_add("write", lambda *_: self._update_estimate())

        ttk.Label(grid, text="Скорость:", style="Panel.TLabel").grid(
            row=2, column=0, sticky="w", padx=(0, 10), pady=4
        )
        speed = ttk.Combobox(
            grid, textvariable=self.var_speed, values=list(SPEED_CHOICES),
            state="readonly", width=28, font=FONT,
        )
        speed.grid(row=2, column=1, sticky="w", pady=4)

        ttk.Label(p2, textvariable=self.var_estimate, style="Muted.TLabel").pack(
            anchor="w", pady=(10, 0)
        )

        # ---- Прогресс ---- #
        p3 = self._panel(self, "Выполнение")
        self.progress = ttk.Progressbar(p3, maximum=1.0)
        self.progress.pack(fill="x")
        prog_row = ttk.Frame(p3, style="Panel.TFrame")
        prog_row.pack(fill="x", pady=(6, 0))
        ttk.Label(prog_row, textvariable=self.var_progress, style="Muted.TLabel").pack(
            side="left"
        )
        self.btn_cancel = ttk.Button(prog_row, text="Отмена", command=self._on_cancel)
        self.btn_cancel.pack(side="right")
        self.btn_cancel.state(["disabled"])

        # ---- Кнопки ---- #
        actions = ttk.Frame(self, style="TFrame")
        actions.pack(fill="x", padx=16, pady=(4, 0))
        self.btn_compress = ttk.Button(
            actions, text="Сжать видео", style="Accent.TButton", command=self._on_compress
        )
        self.btn_compress.pack(side="left")
        self.btn_open = ttk.Button(actions, text="Открыть папку", command=self._on_open_result)
        self.btn_open.pack(side="left", padx=(10, 0))
        self.btn_open.state(["disabled"])

        # ---- Статусбар ---- #
        bar = ttk.Frame(self, style="TFrame", padding=(16, 8))
        bar.pack(fill="x", side="bottom")
        ttk.Label(bar, textvariable=self.var_status, style="Status.TLabel").pack(side="left")
        ttk.Label(bar, textvariable=self.var_ffmpeg, style="Status.TLabel").pack(side="right")

    # --------------------------------------------------------------------- #
    # FFmpeg
    # --------------------------------------------------------------------- #

    def _check_ffmpeg(self) -> None:
        if find_ffmpeg() is None:
            self.var_ffmpeg.set("FFmpeg не найден")
            answer = messagebox.askyesno(
                "FFmpeg не найден",
                "Для работы программы нужен FFmpeg (бесплатный видеокодек).\n\n"
                "Скачать его автоматически в папку программы (~110 МБ, один раз)?\n\n"
                "Нет — если вы установите FFmpeg вручную и перезапустите программу.",
            )
            if answer:
                self._download_ffmpeg()
        else:
            self.var_ffmpeg.set("FFmpeg: готов")

    def _download_ffmpeg(self) -> None:
        self._set_busy(True, "Скачивание FFmpeg…")
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
    # Выбор файлов
    # --------------------------------------------------------------------- #

    def _choose_input(self) -> None:
        path = filedialog.askopenfilename(title="Выберите видеофайл", filetypes=VIDEO_FILETYPES)
        if path:
            self._set_input(path)

    def _set_input(self, path: str) -> None:
        self.var_input.set(path)
        if not self.var_output.get().strip():
            self.var_output.set(str(default_output_path(path)))
        self.btn_open.state(["disabled"])
        self._info = None
        self.var_info.set("Анализ файла…")
        self.var_estimate.set("")
        self._probe_token += 1
        token = self._probe_token

        def done(info: MediaInfo | None) -> None:
            self._events.put(("probe", token, info, None))

        def error(message: str) -> None:
            self._events.put(("probe", token, None, message))

        def worker() -> None:
            try:
                info = probe(path)
            except VideoCompressorError as exc:
                error(str(exc))
            except OSError as exc:
                error(f"Не удалось прочитать файл: {exc}")
            else:
                done(info)

        threading.Thread(target=worker, daemon=True, name="ffprobe").start()

    def _choose_output(self) -> None:
        initial = self.var_output.get().strip() or self.var_input.get().strip()
        path = filedialog.asksaveasfilename(
            title="Куда сохранить сжатое видео",
            defaultextension=".mp4",
            initialfile=Path(initial).name if initial else "video_compressed.mp4",
            initialdir=str(Path(initial).parent) if initial else "",
            filetypes=(("Видео MP4", "*.mp4"), ("Все файлы", "*.*")),
        )
        if path:
            self.var_output.set(path)

    # --------------------------------------------------------------------- #
    # Оценка
    # --------------------------------------------------------------------- #

    def _target_size(self) -> float | None:
        """Размер из поля ввода или None, если значение некорректно."""
        try:
            value = float(self.var_size.get().strip().replace(",", "."))
        except ValueError:
            return None
        return value if value > 0 else None

    def _update_estimate(self) -> None:
        if self._info is None:
            return
        size = self._target_size()
        if size is None:
            self.var_estimate.set("Целевой размер должен быть положительным числом")
            return
        plan = build_plan(self._info, size)
        if plan.estimated_size_mb > size:
            plan_text = f"оценка {_human_size(plan.estimated_size_mb)} — потребуется повторный прогон"
        else:
            plan_text = f"оценка ≈ {_human_size(plan.estimated_size_mb)}"
        self.var_estimate.set(
            f"{plan.video_bitrate_kbps} кбит/с · {plan_text} "
            f"(было {_human_size(self._info.size_mb)})"
        )

    # --------------------------------------------------------------------- #
    # Запуск сжатия
    # --------------------------------------------------------------------- #

    def _on_compress(self) -> None:
        if self._busy:
            return
        input_path = self.var_input.get().strip()
        output_path = self.var_output.get().strip()

        if not input_path:
            messagebox.showerror("Ошибка", "Сначала выберите исходный видеофайл.")
            return
        if not Path(input_path).is_file():
            messagebox.showerror("Ошибка", f"Файл не найден:\n{input_path}")
            return
        if not output_path:
            messagebox.showerror("Ошибка", "Укажите, куда сохранить результат.")
            return
        size = self._target_size()
        if size is None:
            messagebox.showerror("Ошибка", "Целевой размер должен быть положительным числом.")
            return
        if Path(output_path).resolve() == Path(input_path).resolve():
            messagebox.showerror("Ошибка", "Выходной файл совпадает с исходным.")
            return
        if find_ffmpeg() is None:
            messagebox.showerror(
                "FFmpeg", "FFmpeg не найден. Перезапустите программу и разрешите его скачивание."
            )
            return

        preset = SPEED_CHOICES.get(self.var_speed.get(), "veryfast")
        self._set_busy(True, "Подготовка…")
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self._cancel_event.clear()
        self.btn_cancel.state(["!disabled"])
        started = time.monotonic()

        def worker() -> None:
            try:
                result = compress(
                    input_path,
                    output_path,
                    size,
                    preset=preset,
                    progress=lambda frac, stage: self._events.put(
                        ("progress", frac, stage, started)
                    ),
                    cancel=self._cancel_event,
                )
            except VideoCompressorError as exc:
                self._events.put(("error", str(exc)))
            except Exception as exc:  # noqa: BLE001
                log.exception("Неожиданная ошибка")
                self._events.put(("error", f"Непредвиденная ошибка: {exc}"))
            else:
                self._events.put(("done", result))

        threading.Thread(target=worker, daemon=True, name="compress").start()

    def _on_cancel(self) -> None:
        if self._busy:
            self._cancel_event.set()
            self.var_status.set("Останавливаю…")

    # --------------------------------------------------------------------- #
    # Состояния и события
    # --------------------------------------------------------------------- #

    def _set_busy(self, busy: bool, status: str = "") -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        self.btn_compress.state([state] if busy else ["!disabled"])
        if not busy:
            self.btn_cancel.state(["disabled"])
        if status:
            self.var_status.set(status)

    def _poll_events(self) -> None:
        """Переносим события из рабочих потоков в поток Tk."""
        try:
            while True:
                event = self._events.get_nowait()
                self._handle_event(event)
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def _handle_event(self, event: tuple) -> None:
        kind = event[0]

        if kind == "probe":
            _, token, info, error = event
            if token != self._probe_token:
                return  # устаревший результат
            if info is None:
                self.var_info.set(f"Не удалось прочитать файл: {error}")
                return
            self._info = info
            self.var_info.set(
                f"{info.resolution} · {format_duration(info.duration_sec)} · "
                f"{_human_size(info.size_mb)} · {info.video_codec.upper()}"
                + (f" · {info.fps:g} fps" if info.fps else "")
                + ("" if info.has_audio else " · без звука")
            )
            self._update_estimate()

        elif kind == "progress":
            _, frac, stage, started = event
            self.progress.configure(value=frac if frac is not None else 0)
            elapsed = time.monotonic() - started
            eta = (elapsed / frac - elapsed) if frac and frac > 0.02 else None
            text = f"{frac * 100:.0f}%" if frac is not None else stage
            if eta:
                text += f" · осталось ~{_format_eta(eta)}"
            self.var_progress.set(f"{text} · {stage}")
            self.var_status.set("Сжатие выполняется…")

        elif kind == "done":
            result: CompressionResult = event[1]
            self._finish_success(result)

        elif kind == "error":
            message: str = event[1]
            self._finish_error(message)

        elif kind == "dl":
            _, frac, stage = event
            self.var_status.set(stage)
            if frac is not None:
                self.progress.configure(mode="determinate", value=frac)
                self.var_progress.set(f"{frac * 100:.0f}%")

        elif kind == "dl_done":
            self.progress.stop()
            self.progress.configure(mode="determinate", value=0)
            self.var_progress.set("")
            self._set_busy(False, "FFmpeg установлен — можно сжимать")
            self.var_ffmpeg.set("FFmpeg: готов")

    def _finish_success(self, result: CompressionResult) -> None:
        self._set_busy(False, "Готово")
        self.progress.configure(value=1)
        self.var_progress.set("100% · Готово")
        self._result = result.output_path
        self.btn_open.state(["!disabled"])

        delta = (1 - result.compression_ratio) * 100
        lines = [
            f"Файл: {result.output_path.name}",
            f"Размер: {_human_size(result.actual_size_mb)} из {_human_size(result.target_size_mb)}",
        ]
        if result.input_path.is_file():
            src_mb = result.input_path.stat().st_size / (1024 * 1024)
            lines.append(f"Исходник: {_human_size(src_mb)} (сжат в {src_mb / max(result.actual_size_mb, 0.01):.1f}×)")
        if result.attempts > 1:
            lines.append(f"Попыток: {result.attempts}")
        if not result.fits_target:
            lines.append("\nТочный лимит не достигнут: кодек упёрся в минимальный битрейт.")

        self.var_status.set(
            f"Готово · {_human_size(result.actual_size_mb)} "
            f"(−{delta:.0f}%)"
        )
        messagebox.showinfo("Сжатие завершено", "\n".join(lines))

    def _finish_error(self, message: str) -> None:
        self.progress.stop()
        self._set_busy(False, "Ошибка")
        self.var_progress.set("")
        if "отмен" in message.lower():
            self.var_status.set("Отменено пользователем")
        else:
            self.var_status.set("Ошибка")
            messagebox.showerror("Ошибка", message)

    def _on_open_result(self) -> None:
        if self._result and self._result.exists():
            _open_in_file_manager(self._result)

    def _on_close(self) -> None:
        self._cancel_event.set()
        self.destroy()


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, OSError):
            stream.reconfigure(errors="replace")
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    app = App()
    app.mainloop()


if __name__ == "__main__":  # pragma: no cover
    main()
