from __future__ import annotations

import numpy as np

from golani_texture_localizer.candidate import _bbox_overlap, _mip_seam_metrics, _normalize_text


def test_text_normalization_keeps_hangul_and_removes_spacing() -> None:
    assert _normalize_text("  미군 전투식량™ ") == "미군전투식량"


def test_bbox_overlap_measures_editable_pixels() -> None:
    mask = np.zeros((10, 10), dtype=bool)
    mask[2:6, 2:6] = True

    assert _bbox_overlap(mask, [2, 2, 6, 6]) == 1.0
    assert _bbox_overlap(mask, [0, 0, 4, 4]) == 0.25
    assert _bbox_overlap(mask, [-5, -5, 1, 1]) == 0.0


def test_bbox_overlap_rejects_malformed_box() -> None:
    mask = np.ones((4, 4), dtype=bool)

    assert _bbox_overlap(mask, [0, 0, 4]) == 0.0
    assert _bbox_overlap(mask, [0, 0, "4", 4]) == 0.0


def test_mip_seam_metrics_detect_change_that_bleeds_into_guard() -> None:
    source = np.zeros((4, 4, 4), dtype=np.uint8)
    source[..., 3] = 255
    candidate = source.copy()
    candidate[0, 1, 0] = 255
    seam = np.zeros((4, 4), dtype=bool)
    seam[0, 0] = True

    report = _mip_seam_metrics(source, candidate, seam, 3)

    assert report["mips"][0]["changed_inside_seam_guard"] == 0
    assert report["mip_seam_changed_pixels"] > 0


def test_mip_seam_metrics_accepts_distant_change() -> None:
    source = np.zeros((8, 8, 4), dtype=np.uint8)
    source[..., 3] = 255
    candidate = source.copy()
    candidate[7, 7, 0] = 255
    seam = np.zeros((8, 8), dtype=bool)
    seam[0, 0] = True

    report = _mip_seam_metrics(source, candidate, seam, 2)

    assert report["mip_seam_changed_pixels"] == 0
