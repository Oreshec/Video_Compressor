"""Video Compressor — сжатие видео до заданного размера с точным попаданием в лимит."""

from __future__ import annotations

__version__ = "2.0.0"

from .core import (
    CompressionPlan,
    CompressionResult,
    EncodeError,
    FfmpegNotFoundError,
    MediaInfo,
    MediaInfoError,
    VideoCompressorError,
    probe,
    probe_async,
)

__all__ = [
    "CompressionPlan",
    "CompressionResult",
    "EncodeError",
    "FfmpegNotFoundError",
    "MediaInfo",
    "MediaInfoError",
    "VideoCompressorError",
    "probe",
    "probe_async",
    "__version__",
]
