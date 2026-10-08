"""Точка входа: ``python -m video_compressor``.

Без аргументов запускает GUI, с аргументами — консольную версию.
"""

from __future__ import annotations

import sys


def main() -> None:
    argv = sys.argv[1:]
    if not argv or argv[:1] == ["--gui"]:
        from .gui import main as gui_main

        gui_main()
        return

    from .cli import main as cli_main

    sys.exit(cli_main(argv))


if __name__ == "__main__":
    main()
