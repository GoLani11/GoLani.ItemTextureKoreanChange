from __future__ import annotations

from golani_texture_localizer.ocr import _literal, _store_exact_region_readings


def _reading(text: str, bbox: tuple[int, int, int, int]) -> dict:
    x0, y0, x1, y1 = bbox
    return {
        "text": text,
        "script": "korean",
        "confidence": 0.98,
        "engine": "paddleocr",
        "model_signature": "korean-v5",
        "variant": "rgb:rotation0:scale2",
        "polygon": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
    }


def test_literal_normalization_preserves_spacing_punctuation_and_units() -> None:
    assert _literal("열량 80kcal\r\n나트륨 90mg") == "열량 80kcal\n나트륨 90mg"
    assert _literal("열량 80kcal") != _literal("열량80kcal")
    assert _literal("500g(±5g)") != _literal("500g5g")


def test_exact_region_composite_preserves_word_spacing() -> None:
    readings: dict[tuple[str, str, str], dict] = {}

    _store_exact_region_readings(
        readings,
        [_reading("열량", (0, 0, 20, 10)), _reading("80kcal", (24, 0, 60, 10))],
    )

    assert ("korean-v5", "rgb:rotation0:scale2", "열량 80kcal") in readings
    assert ("korean-v5", "rgb:rotation0:scale2", "열량80kcal") not in readings


def test_exact_region_composite_preserves_line_breaks() -> None:
    readings: dict[tuple[str, str, str], dict] = {}

    _store_exact_region_readings(
        readings,
        [_reading("포화", (0, 0, 20, 10)), _reading("1.5g", (0, 18, 20, 28))],
    )

    assert ("korean-v5", "rgb:rotation0:scale2", "포화\n1.5g") in readings
