"""Ядро Video Compressor: анализ файла, расчёт битрейта и сжатие через FFmpeg.

Модуль не печатает и не завершает процесс сам — все ошибки передаются
вызывающему коду через исключения, прогресс — через callback.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import re
import subprocess
import sys
import tempfile
import threading
import uuid
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

#: Максимальное число повторных прогонов, если результат не влез в лимит.
MAX_ATTEMPTS = 3

#: Кодеки аудио, совместимые с контейнером MP4: если исходный битрейт аудио
#: умещается в бюджет, поток выгодно скопировать, а не перекодировать.
MP4_AUDIO_COPY_CODECS = frozenset({"aac", "mp3", "alac", "ac3", "eac3"})

#: Целевые биты на пиксель кадра: ниже планки качество «плывёт», выгоднее
#: уменьшить разрешение и дать каждому пикселю больше битов.
TARGET_BITS_PER_PIXEL = 0.06

#: Лестница высот для авторесолюшна (используется только вниз от источника).
SCALE_LADDER = (2160, 1440, 1080, 720, 540, 480)

#: Пресеты скорости libx264 (скорость ↔ сжатие).
ENCODER_PRESETS = (
    "ultrafast", "superfast", "veryfast", "faster", "fast",
    "medium", "slow", "slower", "veryslow",
)

#: Поддерживаемые кодировщики: CPU x264 и аппаратные h264-ветки.
ENCODERS = ("libx264", "h264_nvenc", "h264_qsv", "h264_amf")

#: Пресет аппаратного кодера для каждого пресета libx264.
_HW_PRESETS: dict[str, dict[str, str]] = {
    "h264_nvenc": {
        "ultrafast": "p1", "superfast": "p2", "veryfast": "p4", "faster": "p4",
        "fast": "p5", "medium": "p5", "slow": "p6", "slower": "p7", "veryslow": "p7",
    },
    "h264_qsv": {},  # понимает те же имена пресетов, что libx264
    "h264_amf": {
        "ultrafast": "speed", "superfast": "speed", "veryfast": "speed", "faster": "speed",
        "fast": "speed", "medium": "balanced", "slow": "quality", "slower": "quality",
        "veryslow": "quality",
    },
}

#: Куда направлять вывод первого прохода двухпроходного кодирования.
_NULL_OUTPUT = "NUL" if sys.platform == "win32" else "/dev/null"

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
    #: True — исходный аудиопоток умещается в бюджет и копируется без потерь.
    audio_copy: bool = False

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
    #: Кодировщик, которым реально записан файл (после возможного фолбэка).
    encoder: str = "libx264"
    #: Высота результата, если включалось уменьшение разрешения.
    output_height: int | None = None

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


_avail_encoders: set[str] | None = None


def available_encoders(*, refresh: bool = False) -> set[str]:
    """Какие кодировщики собраны в этой сборке FFmpeg (кэшируется).

    Дёшево: один вызов ``ffmpeg -encoders`` и поиск по списку. Наличие
    кодировщика не гарантирует работоспособность железа — для проверки
    есть :func:`usable_hw_encoders`.
    """
    global _avail_encoders
    if _avail_encoders is not None and not refresh:
        return _avail_encoders
    try:
        ffmpeg, _ = _require_ffmpeg()
        out = _run([str(ffmpeg), "-hide_banner", "-encoders"])
        text = out.stdout or ""
        _avail_encoders = {enc for enc in ENCODERS if enc in text}
    except (VideoCompressorError, OSError):
        _avail_encoders = set()
    return _avail_encoders


def _encoder_works(ffmpeg: Path, encoder: str) -> bool:
    """Пробная проверка: может ли кодер закодировать тестовый кадр."""
    try:
        out = _run(
            [
                str(ffmpeg), "-hide_banner", "-v", "error",
                "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10:duration=0.5",
                "-c:v", encoder, "-frames:v", "2", "-f", "null", _NULL_OUTPUT,
            ],
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.returncode == 0


def usable_hw_encoders() -> set[str]:
    """Аппаратные кодеры, реально работающие на этой машине.

    Дорогой вызов (запускает тестовое кодирование) — предназначен для GUI,
    который вызывает его один раз в фоновом потоке.
    """
    try:
        ffmpeg, _ = _require_ffmpeg()
    except VideoCompressorError:
        return set()
    listed = available_encoders()
    return {enc for enc in listed if enc != "libx264" and _encoder_works(ffmpeg, enc)}


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
    if info.duration_sec <= 0:
        raise ValueError("Длительность файла должна быть положительной.")
    # Аудио не должно съедать больше 40% целевого бюджета.
    audio_budget_bps = int(target_size_mb * 8 * 1024 * 1024 * 0.4 / info.duration_sec)
    audio_orig = max(0, info.audio_bitrate_bps)
    audio_copy = (
        info.has_audio
        and 0 < audio_orig <= audio_budget_bps
        and (info.audio_codec or "") in MP4_AUDIO_COPY_CODECS
    )
    audio_bps = audio_orig if audio_copy else min(audio_orig, audio_budget_bps)
    if not info.has_audio:
        audio_bps = 0
    video_bps = compute_video_bitrate(
        target_size_mb, info.duration_sec, audio_bps, safety_factor=safety_factor
    )
    return CompressionPlan(
        target_size_mb=target_size_mb,
        video_bitrate_bps=video_bps,
        audio_bitrate_bps=audio_bps,
        estimated_size_mb=estimate_size_mb(info.duration_sec, video_bps, audio_bps),
        audio_copy=audio_copy,
    )


# --------------------------------------------------------------------------- #
# Разрешение
# --------------------------------------------------------------------------- #


def pick_target_height(
    info: MediaInfo,
    video_bitrate_bps: int,
    *,
    bits_per_pixel_target: float = TARGET_BITS_PER_PIXEL,
) -> int | None:
    """Автовыбор высоты под данный битрейт (``None`` — оставить исходную).

    Если на исходном разрешении биты/пиксель в норме, масштабировать не нужно,
    уменьшать без причины тоже не стоит. Если биты/пиксель недостаточно —
    идём вниз по лестнице высот, пока на каждого пикселя не хватит битов.
    """
    if not (info.width and info.height and info.width > 0 and info.height > 0):
        return None
    fps = info.fps if info.fps and info.fps > 1 else 30.0
    bpp = video_bitrate_bps / (info.width * info.height * fps)
    if bpp >= bits_per_pixel_target:
        return None
    if info.height <= SCALE_LADDER[-1]:
        return None  # источник сам по себе низкий — вниз некуда
    lowest = None
    for height in SCALE_LADDER:
        if height >= info.height:
            continue
        ratio = height / info.height
        width2 = int(info.width * ratio)
        bpp2 = video_bitrate_bps / (width2 * height * fps)
        lowest = height
        if bpp2 >= bits_per_pixel_target:
            return height
    return lowest  # даже самая нижняя ступень лучше «каши» на полноразмере


def _resolve_scale(
    info: MediaInfo,
    max_height: int | str | None,
    video_bitrate_bps: int,
) -> tuple[int, int] | None:
    """Пара (ширина, высота) для фильтра scale или ``None``, если не нужно."""
    if max_height is None:
        return None
    if not (info.width and info.height and info.width > 0 and info.height > 0):
        return None
    if max_height == "auto":
        height = pick_target_height(info, video_bitrate_bps)
    else:
        height = int(max_height)
    if not height or height >= info.height:
        return None  # вверх не масштабируем
    ratio = height / info.height
    width2 = max(2, int(info.width * ratio) // 2 * 2)
    height2 = max(2, height // 2 * 2)
    return width2, height2


# --------------------------------------------------------------------------- #
# Кодирование
# --------------------------------------------------------------------------- #

_OUT_TIME_RE = re.compile(r"out_time=(\d+):(\d+):(\d+(?:\.\d+)?)")
_SPEED_RE = re.compile(r"speed=\s*([\d.]+)x")


def _parse_progress_line(line: str) -> float | None:
    """Разобрать строку вывода ``-progress`` и вернуть позицию в секундах."""
    match = _OUT_TIME_RE.match(line.strip())
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _parse_speed(line: str) -> float | None:
    """Разобрать ``speed=1.37x`` из вывода ``-progress``."""
    match = _SPEED_RE.match(line.strip())
    return float(match.group(1)) if match else None


def _video_args(encoder: str, preset: str) -> list[str]:
    """Аргументы ``(кодек, пресет)`` для выбранного кодировщика."""
    if encoder == "libx264":
        return ["-c:v", "libx264", "-preset", preset]
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", _HW_PRESETS["h264_nvenc"].get(preset, "p5")]
    if encoder == "h264_qsv":
        return ["-c:v", "h264_qsv", "-preset", preset]
    if encoder == "h264_amf":
        return ["-c:v", "h264_amf", "-quality", _HW_PRESETS["h264_amf"].get(preset, "balanced")]
    raise ValueError(f"Неизвестный кодировщик: {encoder!r}")


def _encode_command(
    ffmpeg: Path,
    info: MediaInfo,
    output: Path,
    plan: CompressionPlan,
    preset: str,
    audio: bool,
    *,
    encoder: str = "libx264",
    scale: tuple[int, int] | None = None,
    first_pass: bool = False,
    pass_logfile: Path | None = None,
) -> list[str]:
    video_kbps = plan.video_bitrate_kbps
    cmd = [
        str(ffmpeg),
        "-hide_banner",
        "-y",
        "-i", str(info.path),
    ]
    if scale:
        cmd += ["-vf", f"scale={scale[0]}:{scale[1]}:flags=lanczos"]
    cmd += _video_args(encoder, preset)
    cmd += [
        "-b:v", f"{video_kbps}k",
        "-maxrate", f"{int(video_kbps * 1.2)}k",
        "-bufsize", f"{video_kbps * 2}k",
        "-pix_fmt", "yuv420p",
        "-sn",
    ]
    if first_pass:
        # Первый проход двухпроходного режима: только анализ видео.
        if pass_logfile is None:
            raise ValueError("Первому проходу нужен путь passlogfile.")
        cmd += ["-pass", "1", "-passlogfile", str(pass_logfile), "-an"]
        cmd += ["-f", "null", _NULL_OUTPUT]
        return cmd

    if audio and plan.audio_bitrate_bps > 0:
        if plan.audio_copy:
            cmd += ["-c:a", "copy"]
        else:
            cmd += ["-c:a", "aac", "-b:a", f"{max(32, plan.audio_bitrate_bps // 1000)}k"]
    else:
        cmd += ["-an"]
    if pass_logfile is not None:
        cmd += ["-pass", "2", "-passlogfile", str(pass_logfile)]
    cmd += ["-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(output)]
    return cmd


def _cleanup_pass_logs(base: Path) -> None:
    """Удалить файлы статистики двухпроходного режима."""
    with contextlib.suppress(OSError):
        for stale in base.parent.glob(base.name + "*"):
            stale.unlink(missing_ok=True)


def _encode_once(
    ffmpeg: Path,
    info: MediaInfo,
    output: Path,
    plan: CompressionPlan,
    preset: str,
    progress: ProgressCallback | None,
    cancel: threading.Event | None,
    *,
    encoder: str = "libx264",
    scale: tuple[int, int] | None = None,
    stage: str = "Кодирование",
    two_pass: bool = False,
    pass_number: int = 2,
    pass_logfile: Path | None = None,
) -> None:
    """Один прогон FFmpeg. Бросает EncodeError или CompressionCancelled."""
    is_first_pass = two_pass and pass_number == 1 and pass_logfile is not None
    cmd = _encode_command(
        ffmpeg, info, output, plan, preset, info.has_audio,
        encoder=encoder, scale=scale,
        first_pass=is_first_pass, pass_logfile=pass_logfile,
    )
    duration = info.duration_sec

    # Диапазон прогресса этого прогона: в двухпроходном режиме проходы
    # делят файл пополам — 0–0.5 и 0.5–1.
    if two_pass and pass_number == 1:
        offset, length = 0.0, 0.5
    elif two_pass:
        offset, length = 0.5, 0.5
    else:
        offset, length = 0.0, 1.0
    if progress is not None:
        def emit(video_frac: float, speed: float | None) -> None:
            text = stage if speed is None else f"{stage} · {speed:g}×"
            mapped = offset + max(0.0, min(video_frac, 1.0)) * length
            progress(mapped, text)

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
    last_speed: float | None = None
    try:
        for raw_line in proc.stdout:
            if cancel is not None and cancel.is_set():
                proc.terminate()
                raise CompressionCancelled("Сжатие отменено.")
            if progress is None:
                continue
            if raw_line.startswith("progress=end"):
                emit(1.0, last_speed)
                continue
            seconds = _parse_progress_line(raw_line)
            if seconds is None:
                speed = _parse_speed(raw_line)
                if speed is not None:
                    last_speed = speed
                continue
            if duration > 0:
                emit(min(seconds / duration, 0.999), last_speed)
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


def _run_attempts(
    ffmpeg: Path,
    info: MediaInfo,
    output_path: Path,
    target_size_mb: float,
    *,
    preset: str,
    encoder: str,
    max_height: int | str | None,
    two_pass: bool,
    max_attempts: int,
    progress: ProgressCallback | None,
    cancel: threading.Event | None,
) -> CompressionResult:
    """Цикл попыток: кодирование и пересчёт битрейта, если не влезли в лимит."""
    plan = build_plan(info, target_size_mb)
    current_plan = plan
    log.info(
        "Цель %.2f МБ | видеобитрейт %d кбит/с | аудио %s | оценка %.2f МБ",
        target_size_mb,
        plan.video_bitrate_kbps,
        "копия" if plan.audio_copy else f"{plan.audio_bitrate_bps // 1000} кбит/с",
        plan.estimated_size_mb,
    )

    pass_logfile = None
    if two_pass:
        pass_logfile = Path(tempfile.gettempdir()) / f"vc_pass_{uuid.uuid4().hex}"

    attempts = 0
    try:
        while True:
            attempts += 1
            scale = _resolve_scale(info, max_height, current_plan.video_bitrate_bps)
            scale_note = f" · масштаб {scale[0]}×{scale[1]}" if scale else ""
            stage = f"Кодирование (попытка {attempts}/{max_attempts}){scale_note}"
            if progress:
                progress(0.0, stage)
            if two_pass:
                _encode_once(
                    ffmpeg, info, output_path, current_plan, preset, progress, cancel,
                    encoder=encoder, scale=scale, two_pass=True, pass_number=1,
                    pass_logfile=pass_logfile, stage=f"Проход 1/2 · {stage}",
                )
                _encode_once(
                    ffmpeg, info, output_path, current_plan, preset, progress, cancel,
                    encoder=encoder, scale=scale, two_pass=True, pass_number=2,
                    pass_logfile=pass_logfile, stage=f"Проход 2/2 · {stage}",
                )
            else:
                _encode_once(
                    ffmpeg, info, output_path, current_plan, preset, progress, cancel,
                    encoder=encoder, scale=scale, stage=stage,
                )

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
                audio_copy=current_plan.audio_copy,
            )
            if progress:
                progress(0.0, f"Пересчёт битрейта (попытка {attempts + 1}/{max_attempts})")

        return CompressionResult(
            input_path=info.path,
            output_path=output_path,
            target_size_mb=target_size_mb,
            actual_size_mb=actual_mb,
            plan=current_plan,
            attempts=attempts,
            encoder=encoder,
            output_height=scale[1] if scale else None,
        )
    except BaseException:
        # Отмена или ошибка — не оставляем недописанный файл.
        output_path.unlink(missing_ok=True)
        raise
    finally:
        if pass_logfile is not None:
            _cleanup_pass_logs(pass_logfile)


def compress(
    input_path: str | Path,
    output_path: str | Path,
    target_size_mb: float = DEFAULT_TARGET_SIZE_MB,
    *,
    preset: str = "veryfast",
    encoder: str = "libx264",
    max_height: int | str | None = None,
    two_pass: bool = False,
    info: MediaInfo | None = None,
    progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> CompressionResult:
    """Сжать видео так, чтобы результат уложился в ``target_size_mb``.

    Алгоритм: рассчитать битрейт, закодировать, проверить фактический размер;
    если не влезли — повторить с пониженным битрейтом (до ``max_attempts`` раз).

    Args:
        encoder: ``libx264`` или аппаратный (``h264_nvenc``, ``h264_qsv``,
            ``h264_amf``); при сбое аппаратного кодера выполняется
            автоматический повтор через libx264.
        max_height: ``None`` — оставить исходное разрешение; ``"auto"`` —
            выбирать высоту по битрейту; число — ограничение сверху
            (апскейла нет). Пересматривается после каждой попытки.
        two_pass: двухпроходное кодирование (только libx264) — результат
            точнее попадает в целевой размер, но занимает вдвое больше времени.
        info: заранее прочитанные данные ffprobe; если ``None`` — читаются сами.

    Raises:
        FfmpegNotFoundError / MediaInfoError / EncodeError / CompressionCancelled
    """
    input_path = Path(input_path)
    output_path = Path(output_path)
    if preset not in ENCODER_PRESETS:
        raise ValueError(f"Неизвестный пресет: {preset!r}")
    if encoder not in ENCODERS:
        raise ValueError(f"Неизвестный кодировщик: {encoder!r}")
    if target_size_mb <= 0:
        raise ValueError("Целевой размер должен быть положительным.")
    if max_height is not None and max_height != "auto":
        if isinstance(max_height, str) or int(max_height) <= 0:
            raise ValueError("max_height: None, 'auto' или положительное число.")
        max_height = int(max_height)

    ffmpeg, _ = _require_ffmpeg()
    if info is None:
        info = probe(input_path)
    if output_path.resolve() == input_path.resolve():
        raise EncodeError("Выходной файл совпадает с входным.")

    # Аппаратные кодеры не понимают статистику 2-pass от x264.
    if two_pass and encoder != "libx264":
        log.info("Двухпроходное кодирование поддерживает только libx264 — кодирую в один проход.")
        two_pass = False

    kwargs: dict = {
        "preset": preset,
        "encoder": encoder,
        "max_height": max_height,
        "two_pass": two_pass,
        "max_attempts": max_attempts,
        "progress": progress,
        "cancel": cancel,
    }
    try:
        return _run_attempts(ffmpeg, info, output_path, target_size_mb, **kwargs)
    except EncodeError as exc:
        cancelled = cancel is not None and cancel.is_set()
        if encoder != "libx264" and not cancelled:
            log.warning(
                "Кодировщик %s не сработал (%s) — повторяю через libx264.", encoder, exc
            )
            return _run_attempts(
                ffmpeg, info, output_path, target_size_mb, **{**kwargs, "encoder": "libx264"}
            )
        raise


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
