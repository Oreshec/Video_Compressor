"""Ядро Video Compressor: анализ файла, расчёт битрейта и сжатие через FFmpeg.

Модуль не печатает и не завершает процесс сам — все ошибки передаются
вызывающему коду через исключения, прогресс — через callback.
"""

from __future__ import annotations

import json
import logging
import math
import re
import subprocess
import sys
import threading
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .ffmpeg_tools import find_ffmpeg

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Ошибки
# --------------------------------------------------------------------------- #


class VideoCompressorError(Exception):
    """Базовая ошибка приложения."""


class FfmpegNotFoundError(VideoCompressorError):
    """FFmpeg/ffprobe не найден в системе."""


class MediaInfoError(VideoCompressorError):
    """Не удалось проанализировать медиафайл."""


class EncodeError(VideoCompressorError):
    """Ошибка во время кодирования."""


class CompressionCancelled(VideoCompressorError):
    """Пользователь отменил операцию."""


# --------------------------------------------------------------------------- #
# Константы
# --------------------------------------------------------------------------- #

DEFAULT_TARGET_SIZE_MB = 10.0
#: Запас на контейнер и неточность кодирования (доля от целевого размера).
SAFETY_FACTOR = 0.92
#: Минимальный видеобитрейт, ниже которого результат становится бессмысленным.
MIN_VIDEO_BITRATE_BPS = 50_000
#: Максимальное число повторных прогонов, если результат не влез в лимит.
MAX_ATTEMPTS = 3

#: Пресеты скорости libx264 (скорость ↔ сжатие).
ENCODER_PRESETS = (
    "ultrafast", "superfast", "veryfast", "faster", "fast",
    "medium", "slow", "slower", "veryslow",
)

ProgressCallback = Callable[[float | None, str], None]
"""Колбэк прогресса: ``(доля 0..1 или None, описание этапа)``."""


# --------------------------------------------------------------------------- #
# Модель данных
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class MediaInfo:
    """Информация о медиафайле, полученная через ffprobe."""

    path: Path
    duration_sec: float
    size_bytes: int
    video_codec: str | None
    width: int | None
    height: int | None
    fps: float | None
    video_bitrate_bps: int | None
    audio_codec: str | None
    audio_bitrate_bps: int

    @property
    def size_mb(self) -> float:
        return self.size_bytes / (1024 * 1024)

    @property
    def resolution(self) -> str:
        if self.width and self.height:
            return f"{self.width}×{self.height}"
        return "—"

    @property
    def has_audio(self) -> bool:
        return self.audio_codec is not None


@dataclass(frozen=True, slots=True)
class CompressionPlan:
    """План сжатия: битрейты и ожидаемый размер."""

    target_size_mb: float
    video_bitrate_bps: int
    audio_bitrate_bps: int
    estimated_size_mb: float

    @property
    def video_bitrate_kbps(self) -> int:
        return self.video_bitrate_bps // 1000


@dataclass(slots=True)
class CompressionResult:
    """Итог сжатия."""

    input_path: Path
    output_path: Path
    target_size_mb: float
    actual_size_mb: float
    plan: CompressionPlan
    attempts: int = 1

    @property
    def fits_target(self) -> bool:
        return self.actual_size_mb <= self.target_size_mb

    @property
    def compression_ratio(self) -> float:
        try:
            in_size = self.input_path.stat().st_size
        except OSError:
            return 0.0
        if in_size <= 0:
            return 0.0
        return (self.actual_size_mb * 1024 * 1024) / in_size


# --------------------------------------------------------------------------- #
# Поиск FFmpeg и вызовы внешних программ
# --------------------------------------------------------------------------- #

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _run(cmd: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
    """Запустить процесс без окна консоли на Windows."""
    return subprocess.run(
        list(cmd),
        capture_output=True,
        text=True,
        creationflags=_NO_WINDOW,
        **kwargs,
    )


def _require_ffmpeg() -> tuple[Path, Path]:
    """Вернуть (ffmpeg, ffprobe) или бросить FfmpegNotFoundError."""
    tools = find_ffmpeg()
    if tools is None:
        raise FfmpegNotFoundError(
            "FFmpeg не найден. Установите его (https://ffmpeg.org/download.html) "
            "или разрешите программе скачать автоматически."
        )
    return tools.ffmpeg, tools.ffprobe


# --------------------------------------------------------------------------- #
# Анализ файла
# --------------------------------------------------------------------------- #


def _to_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_fps(raw: str | None) -> float | None:
    """Разобрать частоту кадров вида ``29.97`` или ``30000/1001``."""
    if not raw:
        return None
    if "/" in raw:
        num, _, den = raw.partition("/")
        n, d = _to_float(num), _to_float(den)
        if n and d:
            return round(n / d, 3)
    return _to_float(raw)


def _estimate_audio_bitrate(media: dict, video_bitrate: int | None) -> int:
    """Оценка битрейта аудио в битах/сек (128 kbps, если определить не удалось)."""
    for stream in media.get("streams", []):
        if stream.get("codec_type") == "audio":
            br = _to_int(stream.get("bit_rate"))
            if br and br > 0:
                return br
    # Грубая оценка: общий битрейт минус видеобитрейт.
    total = _to_int((media.get("format") or {}).get("bit_rate"))
    if total and total > 0:
        if video_bitrate:
            return max(64_000, total - video_bitrate)
        return total // 5
    return 128_000


def probe(path: str | Path) -> MediaInfo:
    """Получить информацию о видеофайле через ffprobe.

    Raises:
        MediaInfoError: файл не читается или ffprobe недоступен.
        FfmpegNotFoundError: ffprobe не установлен.
    """
    path = Path(path)
    if not path.is_file():
        raise MediaInfoError(f"Файл не найден: {path}")

    _, ffprobe = _require_ffmpeg()
    try:
        out = _run(
            [
                str(ffprobe),
                "-v", "error",
                "-print_format", "json",
                "-show_format",
                "-show_streams",
                str(path),
            ]
        )
    except OSError as exc:  # pragma: no cover — крайне редкий сбой запуска
        raise MediaInfoError(f"Не удалось запустить ffprobe: {exc}") from exc

    if out.returncode != 0:
        detail = (out.stderr or "").strip().splitlines()
        raise MediaInfoError(
            "Не удалось проанализировать файл"
            + (f": {detail[-1]}" if detail else ".")
        )
    try:
        media = json.loads(out.stdout)
    except json.JSONDecodeError as exc:
        raise MediaInfoError("ffprobe вернул некорректные данные.") from exc

    fmt = media.get("format") or {}
    duration = _to_float(fmt.get("duration")) or 0.0
    size = _to_int(fmt.get("size")) or path.stat().st_size

    video_codec = audio_codec = None
    width = height = None
    fps = None
    video_bitrate = None
    audio_bitrate = 0

    for stream in media.get("streams", []):
        ctype = stream.get("codec_type")
        if ctype == "video" and video_codec is None:
            video_codec = stream.get("codec_name")
            width, height = _to_int(stream.get("width")), _to_int(stream.get("height"))
            fps = _parse_fps(stream.get("avg_frame_rate") or stream.get("r_frame_rate"))
            video_bitrate = _to_int(stream.get("bit_rate"))
        elif ctype == "audio" and audio_codec is None:
            audio_codec = stream.get("codec_name")

    if video_codec is None:
        raise MediaInfoError("В файле нет видеопотока.")
    if duration <= 0:
        raise MediaInfoError("Не удалось определить длительность видео.")

    if audio_codec is not None:
        audio_bitrate = _estimate_audio_bitrate(media, video_bitrate)

    return MediaInfo(
        path=path,
        duration_sec=duration,
        size_bytes=size,
        video_codec=video_codec,
        width=width,
        height=height,
        fps=fps,
        video_bitrate_bps=video_bitrate,
        audio_codec=audio_codec,
        audio_bitrate_bps=audio_bitrate,
    )


def probe_async(
    path: str | Path,
    on_done: Callable[[MediaInfo | None], None],
    *,
    on_error: Callable[[str], None] | None = None,
) -> threading.Thread:
    """Проанализировать файл в фоновом потоке (для GUI)."""

    def worker() -> None:
        try:
            info = probe(path)
        except VideoCompressorError as exc:
            if on_error:
                on_error(str(exc))
            else:  # pragma: no cover
                on_done(None)
        else:
            on_done(info)

    thread = threading.Thread(target=worker, daemon=True, name="ffprobe")
    thread.start()
    return thread


# --------------------------------------------------------------------------- #
# Расчёты
# --------------------------------------------------------------------------- #


def estimate_size_mb(
    duration_sec: float,
    video_bitrate_bps: int,
    audio_bitrate_bps: int,
) -> float:
    """Оценка размера результата в МБ."""
    if duration_sec <= 0:
        return 0.0
    return (video_bitrate_bps + audio_bitrate_bps) * duration_sec / (8 * 1024 * 1024)


def compute_video_bitrate(
    target_size_mb: float,
    duration_sec: float,
    audio_bitrate_bps: int,
    *,
    safety_factor: float = SAFETY_FACTOR,
) -> int:
    """Видеобитрейт в битах/сек, чтобы уложиться в ``target_size_mb``."""
    if duration_sec <= 0:
        return 1_000_000
    target_bits = target_size_mb * 8 * 1024 * 1024
    video_bits = target_bits * safety_factor - audio_bitrate_bps * duration_sec
    if video_bits <= 0:
        video_bits = target_bits // 2
    return max(MIN_VIDEO_BITRATE_BPS, int(video_bits / duration_sec))


def build_plan(
    info: MediaInfo,
    target_size_mb: float,
    *,
    safety_factor: float = SAFETY_FACTOR,
) -> CompressionPlan:
    """Собрать план сжатия: битрейты и оценку итогового размера."""
    if target_size_mb <= 0:
        raise ValueError("Целевой размер должен быть положительным.")
    audio_bps = info.audio_bitrate_bps if info.has_audio else 0
    # Аудио не должно съедать больше 40% целевого бюджета.
    audio_bps = min(audio_bps, int(target_size_mb * 8 * 1024 * 1024 * 0.4 / info.duration_sec))
    video_bps = compute_video_bitrate(
        target_size_mb, info.duration_sec, audio_bps, safety_factor=safety_factor
    )
    return CompressionPlan(
        target_size_mb=target_size_mb,
        video_bitrate_bps=video_bps,
        audio_bitrate_bps=audio_bps,
        estimated_size_mb=estimate_size_mb(info.duration_sec, video_bps, audio_bps),
    )


# --------------------------------------------------------------------------- #
# Кодирование
# --------------------------------------------------------------------------- #

_OUT_TIME_RE = re.compile(r"out_time=(\d+):(\d+):(\d+(?:\.\d+)?)")


def _parse_progress_line(line: str) -> float | None:
    """Разобрать строку вывода ``-progress`` и вернуть позицию в секундах."""
    match = _OUT_TIME_RE.match(line.strip())
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _encode_command(
    ffmpeg: Path,
    info: MediaInfo,
    output: Path,
    plan: CompressionPlan,
    preset: str,
    audio: bool,
) -> list[str]:
    video_kbps = plan.video_bitrate_kbps
    cmd = [
        str(ffmpeg),
        "-hide_banner",
        "-y",
        "-i", str(info.path),
        "-c:v", "libx264",
        "-preset", preset,
        "-b:v", f"{video_kbps}k",
        "-maxrate", f"{int(video_kbps * 1.2)}k",
        "-bufsize", f"{video_kbps * 2}k",
        "-pix_fmt", "yuv420p",
        "-sn",
    ]
    if audio and plan.audio_bitrate_bps > 0:
        cmd += ["-c:a", "aac", "-b:a", f"{max(32, plan.audio_bitrate_bps // 1000)}k"]
    else:
        cmd += ["-an"]
    cmd += ["-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(output)]
    return cmd


def _encode_once(
    ffmpeg: Path,
    info: MediaInfo,
    output: Path,
    plan: CompressionPlan,
    preset: str,
    progress: ProgressCallback | None,
    cancel: threading.Event | None,
) -> None:
    """Один прогон FFmpeg. Бросает EncodeError или CompressionCancelled."""
    cmd = _encode_command(ffmpeg, info, output, plan, preset, info.has_audio)
    duration = info.duration_sec

    log.debug("Команда: %s", " ".join(cmd))
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_NO_WINDOW,
    )
    stderr_tail: deque[str] = deque(maxlen=40)

    def drain_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_tail.append(line.rstrip())

    threading.Thread(target=drain_stderr, daemon=True, name="ffmpeg-stderr").start()

    assert proc.stdout is not None
    try:
        for raw_line in proc.stdout:
            if cancel is not None and cancel.is_set():
                proc.terminate()
                raise CompressionCancelled("Сжатие отменено.")
            if progress is None:
                continue
            if raw_line.startswith("progress=end"):
                progress(1.0, "Завершение")
                continue
            seconds = _parse_progress_line(raw_line)
            if seconds is not None and duration > 0:
                progress(min(seconds / duration, 0.999), "Кодирование")
    finally:
        proc.stdout.close()
        returncode = proc.wait()

    if cancel is not None and cancel.is_set():
        raise CompressionCancelled("Сжатие отменено.")
    if returncode != 0:
        detail = next(
            (line for line in reversed(stderr_tail) if line.strip()), "неизвестная ошибка"
        )
        raise EncodeError(f"FFmpeg завершился с ошибкой: {detail}")


def compress(
    input_path: str | Path,
    output_path: str | Path,
    target_size_mb: float = DEFAULT_TARGET_SIZE_MB,
    *,
    preset: str = "veryfast",
    progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> CompressionResult:
    """Сжать видео так, чтобы результат уложился в ``target_size_mb``.

    Алгоритм: рассчитать битрейт, закодировать, проверить фактический размер;
    если не влезли — повторить с пониженным битрейтом (до ``max_attempts`` раз).

    Raises:
        FfmpegNotFoundError / MediaInfoError / EncodeError / CompressionCancelled
    """
    input_path = Path(input_path)
    output_path = Path(output_path)
    if preset not in ENCODER_PRESETS:
        raise ValueError(f"Неизвестный пресет: {preset!r}")
    if target_size_mb <= 0:
        raise ValueError("Целевой размер должен быть положительным.")

    ffmpeg, _ = _require_ffmpeg()
    info = probe(input_path)
    if output_path.resolve() == input_path.resolve():
        raise EncodeError("Выходной файл совпадает с входным.")

    plan = build_plan(info, target_size_mb)
    log.info(
        "Цель %.2f МБ | видеобитрейт %d кбит/с | оценка %.2f МБ",
        target_size_mb, plan.video_bitrate_kbps, plan.estimated_size_mb,
    )

    attempts = 0
    current_plan = plan
    try:
        while True:
            attempts += 1
            if progress:
                progress(0.0, f"Кодирование (попытка {attempts}/{max_attempts})")
            _encode_once(ffmpeg, info, output_path, current_plan, preset, progress, cancel)

            actual_mb = output_path.stat().st_size / (1024 * 1024)
            log.info(
                "Попытка %d: получилось %.2f МБ из %.2f МБ", attempts, actual_mb, target_size_mb
            )
            if actual_mb <= target_size_mb or attempts >= max_attempts:
                if actual_mb > target_size_mb:
                    log.warning(
                        "Размер %.2f МБ превысил цель %.2f МБ после %d попыток.",
                        actual_mb, target_size_mb, attempts,
                    )
                break

            # Не влезли — пересчитываем битрейт пропорционально реальному превышению.
            shrink = (target_size_mb / actual_mb) * SAFETY_FACTOR
            audio_bps = int(current_plan.audio_bitrate_bps * max(shrink, 0.5))
            video_bps = max(
                MIN_VIDEO_BITRATE_BPS,
                int(current_plan.video_bitrate_bps * shrink),
            )
            current_plan = CompressionPlan(
                target_size_mb=target_size_mb,
                video_bitrate_bps=video_bps,
                audio_bitrate_bps=audio_bps,
                estimated_size_mb=estimate_size_mb(info.duration_sec, video_bps, audio_bps),
            )
            if progress:
                progress(0.0, f"Пересчёт битрейта (попытка {attempts + 1}/{max_attempts})")
    except BaseException:
        # Отмена или ошибка — не оставляем недописанный файл.
        output_path.unlink(missing_ok=True)
        raise

    return CompressionResult(
        input_path=input_path,
        output_path=output_path,
        target_size_mb=target_size_mb,
        actual_size_mb=actual_mb,
        plan=current_plan,
        attempts=attempts,
    )


def default_output_path(input_path: str | Path) -> Path:
    """``video.mp4`` -> ``video_compressed.mp4``."""
    path = Path(input_path)
    return path.with_name(f"{path.stem}_compressed{path.suffix}")


def format_duration(seconds: float) -> str:
    """``125.4`` -> ``02:05``."""
    total = int(math.floor(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"
