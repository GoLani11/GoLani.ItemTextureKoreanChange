import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image
import pytest

from golani_texture_localizer.compose import _render_text_layer, compose_candidate
from golani_texture_localizer.paths import ProjectPaths


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _font_path() -> Path:
    return next(
        path
        for path in (
            Path("C:/Windows/Fonts/arial.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        )
        if path.is_file()
    )


def test_compose_candidate_preserves_pixels_outside_editable_and_alpha(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    paths = ProjectPaths.create(root)
    source = root / "source.png"
    values = np.full((48, 96, 4), (200, 180, 120, 173), dtype=np.uint8)
    values[10:18, 5:20, :3] = 20
    Image.fromarray(values, "RGBA").save(source)
    old_text = root / "old.png"
    old_values = np.zeros((48, 96), dtype=np.uint8)
    old_values[10:18, 5:20] = 255
    Image.fromarray(old_values, "L").save(old_text)
    seam = root / "seam.png"
    Image.new("L", (96, 48), 0).save(seam)
    font = _font_path()
    recipe = root / "recipe.json"
    recipe.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "target_id": "sample",
                "source": str(source),
                "source_sha256": _sha256(source),
                "font": str(font),
                "font_sha256": _sha256(font),
                "old_text_mask": str(old_text),
                "old_text_mask_sha256": _sha256(old_text),
                "seam_guard_mask": str(seam),
                "seam_guard_mask_sha256": _sha256(seam),
                "inpaint_radius": 2,
                "editable_margin": 1,
                "text_regions": [
                    {
                        "region_id": "front",
                        "text": "KO",
                        "bbox": [30, 8, 70, 34],
                        "font_size": 12,
                        "fill": [10, 20, 30, 255],
                        "stroke_width": 0,
                        "rotation_deg": 0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    report = compose_candidate(paths, "sample", recipe)

    assert report["passed"] is True
    assert report["alpha_equal"] is True
    assert report["changed_outside_editable"] == 0
    assert report["changed_inside_protected"] == 0
    assert report["changed_inside_seam_guard"] == 0
    assert Path(report["candidate"]).is_file()


def test_arc_text_is_rendered_deterministically_inside_approved_box() -> None:
    layer, runs = _render_text_layer(
        (128, 128),
        _font_path(),
        [
            {
                "region_id": "lid-arc",
                "text": "ABC",
                "bbox": [15, 10, 113, 70],
                "font_size": 12,
                "fill": [10, 20, 30, 255],
                "stroke_width": 0,
                "arc": {
                    "center": [64, 64],
                    "radius": 38,
                    "start_angle_deg": -145,
                    "end_angle_deg": -35,
                },
            }
        ],
    )

    assert layer.getbbox() is not None
    assert runs[0]["arc"]["radius"] == 38
    assert runs[0]["rendered_bbox"][0] >= 15
    assert runs[0]["rendered_bbox"][2] <= 113


def test_tracking_expands_one_line_without_changing_recorded_text() -> None:
    _, compact = _render_text_layer(
        (240, 80),
        _font_path(),
        [
            {
                "region_id": "compact",
                "text": "ABC",
                "bbox": [10, 10, 230, 70],
                "font_size": 24,
                "fill": [10, 20, 30, 255],
                "stroke_width": 0,
            }
        ],
    )
    _, tracked = _render_text_layer(
        (240, 80),
        _font_path(),
        [
            {
                "region_id": "tracked",
                "text": "ABC",
                "bbox": [10, 10, 230, 70],
                "font_size": 24,
                "fill": [10, 20, 30, 255],
                "stroke_width": 0,
                "tracking": 10,
            }
        ],
    )

    compact_width = compact[0]["rendered_bbox"][2] - compact[0]["rendered_bbox"][0]
    tracked_width = tracked[0]["rendered_bbox"][2] - tracked[0]["rendered_bbox"][0]
    assert tracked[0]["text"] == "ABC"
    assert tracked[0]["tracking"] == 10
    assert tracked_width > compact_width


def test_segments_keep_one_semantic_run_with_multiple_colors() -> None:
    layer, runs = _render_text_layer(
        (240, 80),
        _font_path(),
        [
            {
                "region_id": "brand",
                "text": "그린 아이스",
                "bbox": [10, 10, 230, 70],
                "font_size": 24,
                "fill": [220, 220, 220, 255],
                "stroke_width": 0,
                "segments": [
                    {"text": "그린 ", "fill": [220, 220, 220, 255]},
                    {"text": "아이스", "fill": [20, 180, 80, 255]},
                ],
            }
        ],
    )

    values = np.asarray(layer)
    visible = values[..., 3] > 0
    colors = {tuple(value) for value in values[..., :3][visible]}
    assert runs[0]["text"] == "그린 아이스"
    assert runs[0]["segments"][0]["text"] == "그린 "
    assert (220, 220, 220) in colors
    assert (20, 180, 80) in colors


def test_segments_must_concatenate_to_text() -> None:
    with pytest.raises(ValueError, match="문구 합"):
        _render_text_layer(
            (240, 80),
            _font_path(),
            [
                {
                    "region_id": "brand",
                    "text": "그린 아이스",
                    "bbox": [10, 10, 230, 70],
                    "font_size": 24,
                    "fill": [220, 220, 220, 255],
                    "stroke_width": 0,
                    "segments": [{"text": "그린", "fill": [220, 220, 220, 255]}],
                }
            ],
        )


def test_left_middle_anchor_places_text_at_bbox_left_edge() -> None:
    _, runs = _render_text_layer(
        (240, 80),
        _font_path(),
        [
            {
                "region_id": "label",
                "text": "Label",
                "bbox": [30, 10, 220, 70],
                "font_size": 24,
                "fill": [220, 220, 220, 255],
                "stroke_width": 0,
                "anchor": "lm",
                "align": "left",
            }
        ],
    )

    assert runs[0]["anchor"] == "lm"
    assert runs[0]["rendered_bbox"][0] >= 30
    assert runs[0]["rendered_bbox"][0] <= 32


def test_anchor_offset_is_recorded_and_moves_glyph_inside_bbox() -> None:
    _, runs = _render_text_layer(
        (240, 80),
        _font_path(),
        [
            {
                "region_id": "label",
                "text": "Italic",
                "bbox": [30, 10, 220, 70],
                "font_size": 24,
                "fill": [220, 220, 220, 255],
                "stroke_width": 0,
                "anchor": "lm",
                "offset": [3, 0],
            }
        ],
    )

    assert runs[0]["offset"] == [3.0, 0.0]
    assert runs[0]["rendered_bbox"][0] >= 32


def test_compose_uses_hash_pinned_restoration_only_inside_old_text(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    paths = ProjectPaths.create(root)
    source = root / "source.png"
    source_values = np.full((32, 64, 4), (40, 80, 120, 211), dtype=np.uint8)
    Image.fromarray(source_values, "RGBA").save(source)
    restoration = root / "restoration.png"
    patch_values = np.full((32, 64, 4), (240, 10, 20, 255), dtype=np.uint8)
    Image.fromarray(patch_values, "RGBA").save(restoration)
    old_text = root / "old.png"
    old_values = np.zeros((32, 64), dtype=np.uint8)
    old_values[4:12, 4:16] = 255
    Image.fromarray(old_values, "L").save(old_text)
    seam = root / "seam.png"
    Image.new("L", (64, 32), 0).save(seam)
    font = _font_path()
    recipe = root / "recipe.json"
    recipe.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "target_id": "sample",
                "source": str(source),
                "source_sha256": _sha256(source),
                "font": str(font),
                "font_sha256": _sha256(font),
                "old_text_mask": str(old_text),
                "old_text_mask_sha256": _sha256(old_text),
                "seam_guard_mask": str(seam),
                "seam_guard_mask_sha256": _sha256(seam),
                "restoration_patch": str(restoration),
                "restoration_patch_sha256": _sha256(restoration),
                "editable_margin": 0,
                "text_regions": [
                    {
                        "text": "K",
                        "bbox": [24, 4, 44, 24],
                        "font_size": 10,
                        "fill": [1, 2, 3, 255],
                        "stroke_width": 0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    report = compose_candidate(paths, "sample", recipe)

    assert report["compositor"]["restoration"]["mode"] == "hash-pinned-patch-inside-old-text-only"
    with Image.open(report["candidate"]) as candidate_file:
        candidate = np.asarray(candidate_file.convert("RGBA"))
    assert np.array_equal(candidate[20:, :20], source_values[20:, :20])
    assert np.array_equal(candidate[..., 3], source_values[..., 3])


def test_compose_applies_regional_patch_and_telea_fallback(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    paths = ProjectPaths.create(root)
    source = root / "source.png"
    source_values = np.full((32, 80, 4), (40, 80, 120, 255), dtype=np.uint8)
    source_values[5:12, 5:15, :3] = 0
    source_values[5:12, 25:35, :3] = 0
    Image.fromarray(source_values, "RGBA").save(source)
    old_text = root / "old.png"
    old_values = np.zeros((32, 80), dtype=np.uint8)
    old_values[5:12, 5:15] = 255
    old_values[5:12, 25:35] = 255
    Image.fromarray(old_values, "L").save(old_text)
    regional = root / "regional.png"
    regional_values = np.zeros((32, 80), dtype=np.uint8)
    regional_values[5:12, 5:15] = 255
    Image.fromarray(regional_values, "L").save(regional)
    restoration = root / "restoration.png"
    Image.new("RGBA", (80, 32), (240, 10, 20, 255)).save(restoration)
    seam = root / "seam.png"
    Image.new("L", (80, 32), 0).save(seam)
    font = _font_path()
    recipe = root / "recipe.json"
    recipe.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "target_id": "sample",
                "source": str(source),
                "source_sha256": _sha256(source),
                "font": str(font),
                "font_sha256": _sha256(font),
                "old_text_mask": str(old_text),
                "old_text_mask_sha256": _sha256(old_text),
                "seam_guard_mask": str(seam),
                "seam_guard_mask_sha256": _sha256(seam),
                "inpaint_radius": 2,
                "restoration_layers": [
                    {
                        "region_id": "brand",
                        "patch": str(restoration),
                        "patch_sha256": _sha256(restoration),
                        "mask": str(regional),
                        "mask_sha256": _sha256(regional),
                    }
                ],
                "editable_margin": 0,
                "text_regions": [
                    {
                        "text": "K",
                        "bbox": [50, 4, 70, 24],
                        "font_size": 10,
                        "fill": [1, 2, 3, 255],
                        "stroke_width": 0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    report = compose_candidate(paths, "sample", recipe)

    assert report["compositor"]["restoration"]["mode"] == (
        "telea-fallback+hash-pinned-regional-patches"
    )
    with Image.open(report["candidate"]) as candidate_file:
        candidate = np.asarray(candidate_file.convert("RGBA"))
    assert tuple(candidate[8, 8, :3]) == (240, 10, 20)
    assert tuple(candidate[8, 28, :3]) != (240, 10, 20)
