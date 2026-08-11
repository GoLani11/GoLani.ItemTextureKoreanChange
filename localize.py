#!/usr/bin/env python3
"""저장소 설치 없이 새 현지화 CLI를 실행하는 진입점."""

from __future__ import annotations

from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "localizer"))

from golani_texture_localizer.cli import main  # noqa: E402


if __name__ == "__main__":
    main()
