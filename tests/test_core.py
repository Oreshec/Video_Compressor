"""Тесты чистых функций ядра (без запуска FFmpeg)."""

from __future__ import annotations

from pathlib import Path

import pytest
from video_compressor.core import (
    MIN_VIDEO_BITRATE_BPS,
    CompressionPlan,
    MediaInfo,
    _parse_fps,
    _parse_progress_line,
    build_plan,
    compute_video_bitrate,
    default_output_path,
    estimate_size_mb,
    format_duration,
)


def make_info(**overrides) -> MediaInfo:
    defaults = {
        "path": Path("video.mp4"),
        "duration_sec": 300.0,
        "size_bytes": 500 * 1024 * 1024,
        "video_codec": "h264",
        "width": 1920,
        "height": 1080,
        "fps": 30.0,
        "video_bitrate_bps": 8_000_000,
        "audio_codec": "aac",
        "audio_bitrate_bps": 192_000,
    }
    defaults.update(overrides)
    return MediaInfo(**defaults)


# --------------------------------------------------------------------------- #
# Расчёты
# --------------------------------------------------------------------------- #


class TestComputeVideoBitrate:
    def test_fits_target_with_safety_margin(self):
        duration, audio = 300.0, 128_000
        bitrate = compute_video_bitrate(10, duration, audio)
        estimated = estimate_size_mb(duration, bitrate, audio)
        assert estimated <= 10  # с запасом SAFETY_FACTOR обязано влезать

    def test_never_below_minimum(self):
        # Час видео в 0.5 МБ физически невозможен — битрейт не уйдёт ниже минимума.
        bitrate = compute_video_bitrate(0.5, 3600.0, 128_000)
        assert bitrate == MIN_VIDEO_BITRATE_BPS

    def test_zero_duration_returns_default(self):
        assert compute_video_bitrate(10, 0, 128_000) == 1_000_000

    def test_audio_heavier_than_budget_splits_in_half(self):
        # Аудио съедает весь бюджет — видеобитрейт падает до половины цели.
        bitrate = compute_video_bitrate(1, 3600.0, 320_000)
        assert bitrate > 0


class TestEstimateSize:
    def test_basic(self):
        # 1000 кбит/с на 8 секундах ≈ 0.95 МБ
        assert estimate_size_mb(8, 1_000_000, 0) == pytest.approx(0.953, abs=0.001)

    def test_zero_duration(self):
        assert estimate_size_mb(0, 5_000_000, 128_000) == 0.0


class TestBuildPlan:
    def test_audio_capped_by_budget(self):
        info = make_info(duration_sec=10.0, audio_bitrate_bps=2_000_000)
        plan = build_plan(info, 1)  # 1 МБ на 10 сек — аудио не может быть 2 Мбит/с
        assert plan.audio_bitrate_bps < 2_000_000

    def test_no_audio(self):
        info = make_info(audio_codec=None, audio_bitrate_bps=0)
        plan = build_plan(info, 10)
        assert plan.audio_bitrate_bps == 0
        assert plan.estimated_size_mb <= 10

    def test_rejects_non_positive_target(self):
        with pytest.raises(ValueError):
            build_plan(make_info(), 0)

    def test_estimate_within_target(self):
        plan = build_plan(make_info(), 10)
        assert isinstance(plan, CompressionPlan)
        assert plan.estimated_size_mb <= plan.target_size_mb


# --------------------------------------------------------------------------- #
# Разбор вывода
# --------------------------------------------------------------------------- #


class TestFpsParsing:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("30/1", 30.0),
            ("30000/1001", 29.97),
            ("25", 25.0),
            (None, None),
            ("", None),
            ("bogus", None),
        ],
    )
    def test(self, raw, expected):
        assert _parse_fps(raw) == expected


class TestProgressParsing:
    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            ("out_time=00:01:30.500000", 90.5),
            ("out_time=01:02:03.25", 3723.25),
            ("frame=100", None),
            ("progress=end", None),
            ("out_time=00:00:00.000000", 0.0),
        ],
    )
    def test(self, line, expected):
        assert _parse_progress_line(line) == expected


# --------------------------------------------------------------------------- #
# Прочее
# --------------------------------------------------------------------------- #


def test_default_output_path():
    assert default_output_path("clips/video.mp4") == Path("clips/video_compressed.mp4")
    assert default_output_path(Path("a.b.mkv")) == Path("a.b_compressed.mkv")


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "00:00"), (65, "01:05"), (3599, "59:59"), (3601, "1:00:01")],
)
def test_format_duration(seconds, expected):
    assert format_duration(seconds) == expected
