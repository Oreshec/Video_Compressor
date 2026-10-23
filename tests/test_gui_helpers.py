"""Тесты чистых помощников GUI (без создания окна)."""

from __future__ import annotations

from pathlib import Path

from video_compressor.gui import format_eta, unique_output_path


def test_unique_output_path_free():
    target = Path("C:/tmp/video_compressed.mp4")
    assert unique_output_path(target, []) == target
    assert unique_output_path(target, [Path("C:/tmp/other.mp4")]) == target


def test_unique_output_path_taken_by_earlier_output():
    target = Path("C:/out/video_compressed.mp4")
    taken = [Path("C:/out/video_compressed.mp4")]  # имя уже выдано другому файлу пакета
    assert unique_output_path(target, taken) == Path("C:/out/video_compressed_2.mp4")


def test_unique_output_path_several_conflicts():
    target = Path("C:/out/a_compressed.mp4")
    taken = [
        Path("C:/out/a_compressed.mp4"),
        Path("C:/out/a_compressed_2.mp4"),
        Path("C:/out/a_compressed_3.mp4"),
    ]
    assert unique_output_path(target, taken) == Path("C:/out/a_compressed_4.mp4")


def test_format_eta():
    assert format_eta(0) == "0:00"
    assert format_eta(75) == "1:15"
    assert format_eta(3600) == "1:00:00"
    assert format_eta(-5) == "—"
    assert format_eta(90000) == ">24 ч"
