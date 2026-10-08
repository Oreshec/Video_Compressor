"""Поиск и (при необходимости) автоматическая установка FFmpeg.

Порядок поиска:

1. папка ``ffmpeg/bin`` рядом с программой (переносимая установка);
2. ``PATH``.

Если FFmpeg не найден, GUI может предложить скачать официальную сборку
(zen-themed ``ffmpeg-release-essentials.zip``) в папку рядом с программой —
тогда пользователю не нужна установка вручную.
"""

from __future__ import annotations

import logging
import shutil
import sys
import tempfile
import threading
import urllib.request
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

#: Официальная сборка FFmpeg для Windows (зеркала в порядке приорета).
FFMPEG_DOWNLOAD_URLS: tuple[str, ...] = (
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
    "ffmpeg-master-latest-win64-gpl.zip",
)

_DOWNLOAD_CHUNK = 256 * 1024
_TIMEOUT = 60


@dataclass(frozen=True, slots=True)
class FfmpegPaths:
    """Пути к исполняемым файлам FFmpeg."""

    ffmpeg: Path
    ffprobe: Path

    @property
    def is_portable(self) -> bool:
        """True, если лежит рядом с программой, а не в PATH."""
        return _portable_dir() in self.ffmpeg.parents


def program_dir() -> Path:
    """Базовая папка программы: рядом с exe или корень проекта."""
    if getattr(sys, "frozen", False):  # PyInstaller onefile/ononedir
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _portable_dir() -> Path:
    return program_dir() / "ffmpeg"


def _candidates() -> list[Path]:
    """Папки, в которых ищем ffmpeg(.exe)/ffprobe(.exe)."""
    dirs: list[Path] = [_portable_dir() / "bin", _portable_dir()]
    exe = shutil.which("ffmpeg")
    if exe:
        dirs.append(Path(exe).resolve().parent)
    return dirs


def _find_one(names: Sequence[str]) -> Path | None:
    for directory in _candidates():
        for name in names:
            candidate = directory / name
            if candidate.is_file():
                return candidate
    # Последний шанс — PATH
    for name in names:
        found = shutil.which(name)
        if found:
            return Path(found).resolve()
    return None


_cached: FfmpegPaths | None = None


def find_ffmpeg(*, refresh: bool = False) -> FfmpegPaths | None:
    """Найти ffmpeg и ffprobe; ``None``, если не найдены. Результат кэшируется."""
    global _cached
    if _cached is not None and not refresh:
        return _cached
    ffmpeg = _find_one(("ffmpeg.exe", "ffmpeg"))
    ffprobe = _find_one(("ffprobe.exe", "ffprobe"))
    if ffmpeg and ffprobe:
        _cached = FfmpegPaths(ffmpeg=ffmpeg, ffprobe=ffprobe)
        log.debug("FFmpeg найден: %s", ffmpeg)
        return _cached
    return None


def ffmpeg_found() -> bool:
    return find_ffmpeg() is not None


def download_ffmpeg(
    progress: Callable[[float | None, str], None] | None = None,
    cancel: threading.Event | None = None,
    urls: Sequence[str] = FFMPEG_DOWNLOAD_URLS,
) -> FfmpegPaths:
    """Скачать и распаковать FFmpeg в папку ``<программа>/ffmpeg``.

    Args:
        progress: ``(доля 0..1, описание)`` — вызывается из рабочего потока.
        cancel: событие отмены — при установке прерывает скачивание.

    Returns:
        Найденные пути к ffmpeg/ffprobe.

    Raises:
        OSError: все зеркала недоступны или архив повреждён.
    """
    dest = _portable_dir()
    last_error: Exception | None = None

    for url in urls:
        try:
            _download_and_extract(url, dest, progress, cancel)
            break
        except _Cancelled:
            raise
        except Exception as exc:  # noqa: BLE001 — пробуем следующее зеркало
            log.warning("Не удалось скачать FFmpeg с %s: %s", url, exc)
            last_error = exc
    else:
        raise OSError(
            "Не удалось скачать FFmpeg. Проверьте интернет-соединение или "
            "установите FFmpeg вручную: https://ffmpeg.org/download.html"
            + (f"\nПоследняя ошибка: {last_error}" if last_error else "")
        )

    tools = find_ffmpeg(refresh=True)
    if tools is None:
        raise OSError("FFmpeg скачан, но запустился не удалось найти ffprobe.")
    if progress:
        progress(1.0, "FFmpeg установлен")
    return tools


class _Cancelled(Exception):
    pass


def _report(progress, frac: float | None, text: str) -> None:
    if progress:
        progress(frac, text)


def _download_and_extract(
    url: str,
    dest: Path,
    progress,
    cancel: threading.Event | None,
) -> None:
    """Скачать zip с FFmpeg и распаковать бинарники в ``dest``."""
    _report(progress, None, "Скачивание FFmpeg…")
    request = urllib.request.Request(url, headers={"User-Agent": "VideoCompressor/2.0"})

    with tempfile.TemporaryDirectory(prefix="ffmpeg-dl-") as tmp:
        tmp_path = Path(tmp)
        archive = tmp_path / "ffmpeg.zip"

        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            total = int(response.headers.get("Content-Length") or 0)
            received = 0
            with open(archive, "wb") as fh:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise _Cancelled("Скачивание отменено.")
                    chunk = response.read(_DOWNLOAD_CHUNK)
                    if not chunk:
                        break
                    fh.write(chunk)
                    received += len(chunk)
                    if total:
                        _report(
                            progress,
                            min(received / total, 0.99) * 0.9,
                            f"Скачивание FFmpeg: {received // (1024 * 1024)} / "
                            f"{total // (1024 * 1024)} МБ",
                        )

        if cancel is not None and cancel.is_set():
            raise _Cancelled("Скачивание отменено.")
        _report(progress, 0.92, "Распаковка FFmpeg…")

        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()
            executables = [
                n for n in names if n.replace("\\", "/").endswith(("/ffmpeg.exe", "/ffprobe.exe"))
            ]
            if not executables:
                raise OSError("В архиве нет ffmpeg.exe — повреждённая сборка.")
            for member in executables:
                # ffmpeg-release-essentials/bin/ffmpeg.exe -> ffmpeg/bin/ffmpeg.exe
                relative = Path(*Path(member).parts[-2:])
                target = dest / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
                _report(progress, 0.92 + 0.08 * (executables.index(member) + 1) / len(executables),
                        "Распаковка FFmpeg…")

    log.info("FFmpeg установлен в %s", dest)
