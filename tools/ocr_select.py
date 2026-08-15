#!/usr/bin/env python3
"""Thin entry point for the texture OCR selector.

No OCR model is loaded by importing this file.  Run ``doctor`` or ``plan``
before the explicitly guarded ``scan --execute`` command.
"""

from texture_ocr.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
