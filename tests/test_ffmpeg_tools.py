"""Тесты поиска FFmpeg (без реального скачивания)."""

from __future__ import annotations

from pathlib import Path

from video_compressor import ffmpeg_tools


def test_program_dir_returns_path():
    assert isinstance(ffmpeg_tools.program_dir(), Path)


def test_find_one_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(ffmpeg_tools, "_candidates", lambda: [tmp_path])
    monkeypatch.setattr(ffmpeg_tools.shutil, "which", lambda _name: None)
    assert ffmpeg_tools._find_one(("ffmpeg.exe",)) is None


def test_find_one_finds_portable_binary(tmp_path, monkeypatch):
    binary = tmp_path / "ffmpeg.exe"
    binary.write_bytes(b"fake")
    monkeypatch.setattr(ffmpeg_tools, "_candidates", lambda: [tmp_path])
    monkeypatch.setattr(ffmpeg_tools.shutil, "which", lambda _name: None)
    assert ffmpeg_tools._find_one(("ffmpeg.exe",)) == binary


def test_download_raises_without_working_mirror(tmp_path, monkeypatch):
    def boom(*_args, **_kwargs):
        raise OSError("нет сети")

    monkeypatch.setattr(ffmpeg_tools, "_download_and_extract", boom)
    try:
        ffmpeg_tools.download_ffmpeg(urls=("https://example.invalid/x.zip",))
    except OSError as exc:
        assert "Не удалось скачать FFmpeg" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("должно было выбросить OSError")
