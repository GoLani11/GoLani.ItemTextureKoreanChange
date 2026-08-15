"""Reusable OCR selection helpers for Tarkov textures.

Heavy optional dependencies are deliberately imported only by engine and image
preprocessing code.  The package can therefore be inspected, planned, and
tested before PaddleOCR/EasyOCR are installed.
"""

from .scoring import Classification, Detection, classify_detections, detect_scripts

__all__ = [
    "Classification",
    "Detection",
    "classify_detections",
    "detect_scripts",
]

__version__ = "0.1.0"
