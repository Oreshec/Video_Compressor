"""Консольный интерфейс Video Compressor."""

from __future__ import annotations

import argparse
import contextlib
import logging
import sys
from pathlib import Path

from . import __version__
from .core import (
    DEFAULT_TARGET_SIZE_MB,
    ENCODER_PRESETS,
    ENCODERS,
    VideoCompressorError,
    compress,
    default_output_path,
)
from .ffmpeg_tools import download_ffmpeg, ffmpeg_found

log = logging.getLogger(__name__)

_STATUS_WIDTH = 60


def _configure_logging(quiet: bool, level: str) -> None:
    # Русская консоль (cp866/cp1251) не знает символы вроде «×» — не падаем.
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, OSError):
            stream.reconfigure(errors="replace")
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger("video_compressor")
    root.addHandler(handler)
    root.setLevel(logging.WARNING if quiet else level.upper())


def _make_progress(quiet: bool):
    """Колбэк прогресса: одна живая строка в терминале."""
    if quiet:
        return None

    def progress(frac: float | None, stage: str) -> None:
        line = f"{stage}…" if frac is None else f"[{frac * 100:5.1f}%] {stage}"
        sys.stderr.write("\r" + line.ljust(_STATUS_WIDTH)[:_STATUS_WIDTH])
        sys.stderr.flush()

    return progress


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="video-compressor",
        description=(
            "Сжатие видео до заданного размера: FFmpeg сам подбирает битрейт, "
            "а при необходимости делает повторный прогон, чтобы результат "
            "точно уложился в лимит."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("input", nargs="?", help="входной видеофайл")
    parser.add_argument("-i", "--input", dest="input_opt", help="входной видеофайл (вместо позиционного)")
    parser.add_argument(
        "-o", "--output", type=Path,
        help="выходной файл (по умолчанию: <имя>_compressed.<расширение>)",
    )
    parser.add_argument(
        "-s", "--size", type=float, default=DEFAULT_TARGET_SIZE_MB, metavar="МБ",
        help="целевой размер результата в мегабайтах",
    )
    parser.add_argument(
        "-p", "--preset", choices=ENCODER_PRESETS, default="veryfast",
        help="пресет скорости (быстрее = хуже сжатие)",
    )
    parser.add_argument(
        "--encoder", choices=ENCODERS, default="libx264",
        help="кодировщик: CPU x264 или аппаратный (NVENC/QSV/AMF)",
    )
    parser.add_argument(
        "--resolution", default="source", choices=("source", "auto", "2160", "1440", "1080", "720", "480"),
        help="ограничение разрешения: source, auto (по битрейту) или высота",
    )
    parser.add_argument(
        "--two-pass", action="store_true",
        help="двухпроходное кодирование: точнее в целевой размер, но вдвое дольше",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="только ошибки")
    parser.add_argument("--log-level", default="info", help="уровень логирования")
    parser.add_argument(
        "--download-ffmpeg", action="store_true",
        help="скачать FFmpeg в папку программы, если он не установлен",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.quiet, args.log_level)

    input_raw = args.input_opt or args.input
    if not input_raw:
        parser.error("укажите входной файл: video-compressor video.mp4")

    if args.download_ffmpeg and not ffmpeg_found():
        print("FFmpeg не найден, скачиваю…", file=sys.stderr)
        try:
            download_ffmpeg(
                progress=lambda frac, stage: print(
                    f"\r{stage}{'…' if frac is None else f' {frac * 100:.0f}%'}",
                    end="" if frac is None or frac < 1 else "\n",
                    file=sys.stderr,
                )
            )
        except OSError as exc:
            print(f"Ошибка: {exc}", file=sys.stderr)
            return 2

    input_path = Path(input_raw).expanduser()
    output_path = args.output or default_output_path(input_path)
    if args.resolution == "source":
        max_height: int | str | None = None
    elif args.resolution == "auto":
        max_height = "auto"
    else:
        max_height = int(args.resolution)

    try:
        result = compress(
            input_path,
            output_path,
            args.size,
            preset=args.preset,
            encoder=args.encoder,
            max_height=max_height,
            two_pass=args.two_pass,
            progress=_make_progress(args.quiet),
        )
    except KeyboardInterrupt:
        if not args.quiet:
            print("\nОтменено.", file=sys.stderr)
        return 130
    except VideoCompressorError as exc:
        if not args.quiet:
            print("", file=sys.stderr)  # завершить строку прогресса
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1

    if not args.quiet:
        print("", file=sys.stderr)
        print(f"Готово: {result.output_path}")
        print(f"Размер: {result.actual_size_mb:.2f} МБ "
              f"(цель {result.target_size_mb:.2f} МБ, попыток: {result.attempts})")
        if not result.fits_target:
            print(
                "Внимание: точный лимит не достигнут — кодек упёрся в минимальный битрейт.",
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
