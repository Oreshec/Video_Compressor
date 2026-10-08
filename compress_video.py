#!/usr/bin/env python3
"""Совместимая обёртка: ``python compress_video.py video.mp4 -s 10``.

Полная версия реализована в пакете ``video_compressor.cli``.
"""

from __future__ import annotations

import sys

from video_compressor.cli import main

if __name__ == "__main__":
    sys.exit(main())
