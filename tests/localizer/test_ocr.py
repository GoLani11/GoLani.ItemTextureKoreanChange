from __future__ import annotations

import pytest
import numpy as np

from golani_texture_localizer.ocr import _deduplicate, _inverse_points, _positions


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
