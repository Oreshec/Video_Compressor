"""Тесты чистых функций ядра (без запуска FFmpeg)."""

from __future__ import annotations

from pathlib import Path

import pytest
from video_compressor.core import (
    MIN_VIDEO_BITRATE_BPS,
    CompressionPlan,
    MediaInfo,
    _encode_command,
    _parse_fps,
    _parse_progress_line,
    _parse_speed,
    _resolve_scale,
    _video_args,
    build_plan,
    compute_video_bitrate,
    default_output_path,
    estimate_size_mb,
    format_duration,
    pick_target_height,
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

    def test_rejects_zero_duration(self):
        with pytest.raises(ValueError):
            build_plan(make_info(duration_sec=0.0), 10)

    def test_estimate_within_target(self):
        plan = build_plan(make_info(), 10)
        assert isinstance(plan, CompressionPlan)
        assert plan.estimated_size_mb <= plan.target_size_mb


class TestAudioCopyDecision:
    def test_copy_when_safe_codec_fits_budget(self):
        info = make_info(duration_sec=10.0, audio_codec="aac", audio_bitrate_bps=128_000)
        plan = build_plan(info, 10)  # бюджет аудио ≈ 335 кбит/с — 128 влезают
        assert plan.audio_copy is True
        assert plan.audio_bitrate_bps == 128_000

    def test_no_copy_for_incompatible_codec(self):
        info = make_info(duration_sec=10.0, audio_codec="opus", audio_bitrate_bps=128_000)
        plan = build_plan(info, 10)
        assert plan.audio_copy is False

    def test_no_copy_when_audio_too_fat(self):
        info = make_info(duration_sec=10.0, audio_codec="aac", audio_bitrate_bps=2_000_000)
        plan = build_plan(info, 1)
        assert plan.audio_copy is False
        assert plan.audio_bitrate_bps < 2_000_000


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


class TestSpeedParsing:
    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            ("speed=1.37x", 1.37),
            ("speed= 5x", 5.0),
            ("speed=0x", 0.0),
            ("frame=100", None),
            ("out_time=00:00:01", None),
        ],
    )
    def test(self, line, expected):
        assert _parse_speed(line) == expected


# --------------------------------------------------------------------------- #
# Кодировщики и команды
# --------------------------------------------------------------------------- #


class TestVideoArgs:
    def test_x264_keeps_preset(self):
        assert _video_args("libx264", "slow") == ["-c:v", "libx264", "-preset", "slow"]

    def test_nvenc_maps_speed_preset(self):
        assert _video_args("h264_nvenc", "veryfast") == ["-c:v", "h264_nvenc", "-preset", "p4"]

    def test_amf_maps_speed_preset(self):
        assert _video_args("h264_amf", "medium") == ["-c:v", "h264_amf", "-quality", "balanced"]

    def test_unknown_encoder(self):
        with pytest.raises(ValueError):
            _video_args("h265_unknown", "veryfast")


class TestEncodeCommand:
    def plan_for(self, info: MediaInfo) -> CompressionPlan:
        return build_plan(info, 10)

    def test_single_pass_libx264(self):
        info = make_info()
        cmd = _encode_command(
            Path("ffmpeg"), info, Path("out.mp4"), self.plan_for(info), "veryfast", True,
        )
        assert cmd[cmd.index("-c:v") + 1] == "libx264"
        assert "-pass" not in cmd
        assert cmd[cmd.index("-c:a") + 1] == "aac"  # аудио не влезает в 10 МБ/300 с

    def test_audio_copy_flag_when_fits(self):
        info = make_info(duration_sec=10.0, audio_bitrate_bps=128_000)
        cmd = _encode_command(
            Path("ffmpeg"), info, Path("out.mp4"), build_plan(info, 10), "veryfast", True,
        )
        assert cmd[cmd.index("-c:a") + 1] == "copy"

    def test_two_pass_first(self):
        info = make_info()
        cmd = _encode_command(
            Path("ffmpeg"), info, Path("out.mp4"), self.plan_for(info), "veryfast", True,
            first_pass=True, pass_logfile=Path("pass.log"),
        )
        assert cmd[cmd.index("-pass") + 1] == "1"
        assert "-an" in cmd  # первый проход без аудио
        assert "-movflags" not in cmd

    def test_two_pass_second(self):
        info = make_info()
        cmd = _encode_command(
            Path("ffmpeg"), info, Path("out.mp4"), self.plan_for(info), "veryfast", True,
            pass_logfile=Path("pass.log"),
        )
        assert cmd[cmd.index("-pass") + 1] == "2"
        assert cmd[cmd.index("-passlogfile") + 1] == "pass.log"

    def test_hw_encoder(self):
        info = make_info()
        cmd = _encode_command(
            Path("ffmpeg"), info, Path("out.mp4"), self.plan_for(info), "slow", True,
            encoder="h264_nvenc",
        )
        assert cmd[cmd.index("-c:v") + 1] == "h264_nvenc"

    def test_scale_filter(self):
        info = make_info()
        cmd = _encode_command(
            Path("ffmpeg"), info, Path("out.mp4"), self.plan_for(info), "veryfast", True,
            scale=(1280, 720),
        )
        assert cmd[cmd.index("-vf") + 1] == "scale=1280:720:flags=lanczos"


class TestResolveScale:
    def test_none_means_no_scale(self):
        info = make_info()
        assert _resolve_scale(info, None, 1_000_000) is None

    def test_never_upscales(self):
        info = make_info(width=1280, height=720)  # источник ниже лимита
        assert _resolve_scale(info, 1080, 1_000_000) is None

    def test_fixed_limit_downscales_to_even(self):
        info = make_info(width=1920, height=1080)
        assert _resolve_scale(info, 720, 1_000_000) == (1280, 720)

    def test_invalid_limit(self):
        info = make_info()
        with pytest.raises(ValueError):
            _resolve_scale(info, "999px", 1_000_000)


class TestPickTargetHeight:
    def test_no_scaling_needed_with_enough_bits(self):
        info = make_info()  # 1080p30 при 8 Мбит/с — биты/пиксель 0.128
        assert pick_target_height(info, 8_000_000) is None

    def test_steps_down_until_comfortable(self):
        info = make_info()  # 1080p30 при 1 Мбит/с — никто не разглядет детали
        assert pick_target_height(info, 1_000_000) == 540

    def test_small_source_untouched(self):
        info = make_info(width=640, height=360)
        assert pick_target_height(info, 200_000) is None

    def test_unknown_dimensions(self):
        info = make_info(width=None, height=None)
        assert pick_target_height(info, 200_000) is None


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
