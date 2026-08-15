from __future__ import annotations

import hashlib
import json

import pytest
import numpy as np

from golani_texture_localizer.ocr import (
    OcrSession,
    _deduplicate,
    _inverse_points,
    _positions,
    reusable_ocr_report,
)


@pytest.mark.parametrize(
    ("angle", "rotated", "expected"),
    [
        (0, [[2, 3]], [[12.0, 23.0]]),
        (90, [[4, 2]], [[12.0, 23.0]]),
        (180, [[4, 4]], [[12.0, 23.0]]),
        (270, [[3, 4]], [[12.0, 23.0]]),
    ],
)
def test_rotated_ocr_coordinates_return_to_original_pixels(angle, rotated, expected) -> None:
    assert _inverse_points(rotated, angle, 6, 7, 10, 20) == expected


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
def test_rotation_inverse_matches_numpy_pixel_centers(angle) -> None:
    original = np.arange(7 * 6).reshape(7, 6)
    rotated = np.rot90(original, -(angle // 90))
    original_y, original_x = 3, 2
    rotated_y, rotated_x = np.argwhere(rotated == original[original_y, original_x])[0]

    mapped = _inverse_points(
        [[float(rotated_x) + 0.5, float(rotated_y) + 0.5]], angle, 6, 7, 0, 0
    )[0]

    assert mapped == pytest.approx([original_x + 0.5, original_y + 0.5])


def test_tiles_always_cover_the_final_edge() -> None:
    assert _positions(4000, 1536, 192)[-1] == 4000 - 1536


def test_weak_rotated_noise_does_not_create_a_false_conflict() -> None:
    base = {
        "script": "latin",
        "variant": "r0",
        "polygon": [[0, 0], [10, 0], [10, 10], [0, 10]],
        "bbox": [0, 0, 10, 10],
        "rotation_deg": 0,
        "direction": "left-to-right",
        "face": "unreviewed",
        "artwork_direction": "unreviewed",
    }
    result = _deduplicate(
        [
            {**base, "text": "PPG", "confidence": 0.95, "engine": "paddleocr", "model_signature": "en"},
            {**base, "text": "9", "confidence": 0.2, "engine": "easyocr", "model_signature": "ru"},
        ]
    )

    assert result[0]["conflicting_readings"] is False


def test_ocr_session_rejects_unknown_phase(tmp_path) -> None:
    with pytest.raises(ValueError, match="phase"):
        OcrSession(tmp_path, phase="unknown")


def test_completed_ocr_report_is_reused_only_for_same_input_and_engine(tmp_path) -> None:
    image = tmp_path / "image.png"
    image.write_bytes(b"same image")
    report_path = tmp_path / "report.json"

    class Session:
        phase = "source"
        engine_signature = {"detector": "fixed"}

    report = {
        "schema_version": 1,
        "phase": "source",
        "image_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
        "engine_signature": Session.engine_signature,
        "status": "completed",
        "errors": [],
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")

    assert reusable_ocr_report(Session(), image, report_path) == report
    image.write_bytes(b"changed image")
    assert reusable_ocr_report(Session(), image, report_path) is None
