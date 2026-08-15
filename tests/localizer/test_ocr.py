from __future__ import annotations

import hashlib
import json

import pytest
import numpy as np
from PIL import Image

from golani_texture_localizer.ocr import (
    OcrSession,
    _deduplicate,
    _inverse_points,
    _oriented_region_variants,
    _otsu_threshold,
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
        "region_plan_sha256": None,
        "status": "completed",
        "errors": [],
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")

    assert reusable_ocr_report(Session(), image, report_path) == report
    image.write_bytes(b"changed image")
    assert reusable_ocr_report(Session(), image, report_path) is None


def test_region_ocr_crop_is_rotated_back_and_small_text_is_upscaled() -> None:
    rgba = Image.new("RGBA", (20, 40), (40, 80, 120, 255))
    rgba.putpixel((4, 10), (220, 220, 220, 255))

    variants = _oriented_region_variants(
        rgba,
        {"bbox": [2, 4, 8, 24], "rotation_deg": 90},
        ["white", "black"],
        alpha_semantics="material",
    )

    assert [name.split(":", 1)[0] for name, _ in variants] == ["rgb", "rgb"]
    assert variants[0][1].shape == (24, 80, 3)
    assert variants[1][1].shape == (24, 80, 3)


def test_otsu_threshold_separates_dark_text_from_light_background() -> None:
    grayscale = np.full((20, 20), 210, dtype=np.uint8)
    grayscale[5:15, 8:12] = 45

    threshold = _otsu_threshold(grayscale)

    assert 45 <= threshold < 210


def test_region_ocr_crop_rejects_non_right_angle_rotation() -> None:
    with pytest.raises(ValueError, match="90도"):
        _oriented_region_variants(
            Image.new("RGBA", (20, 40), "white"),
            {"bbox": [2, 4, 8, 24], "rotation_deg": 45},
            ["white"],
        )
