from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image
import pytest

from golani_texture_localizer.compose import compose_candidate
from golani_texture_localizer.paths import ProjectPaths


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _descriptor(root: Path, path: Path) -> dict[str, str]:
    return {"path": path.relative_to(root).as_posix(), "sha256": _sha256(path)}


def _save_rgba(path: Path, values: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(values, "RGBA").save(path)


def _save_mask(path: Path, values: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(values.astype(np.uint8) * 255, "L").save(path)


def _typography() -> dict[str, object]:
    return {
        "style_class": "condensed display",
        "stroke_character": "heavy even strokes",
        "glyph_proportions": "tall and narrow",
        "ink_bbox": [12, 4, 16, 9],
        "ink_width_px": 4,
        "ink_height_px": 5,
        "alignment": "centered",
        "spacing": "tight",
        "effects": "solid fill",
        "surface_finish": "flat print",
        "slant_deg": 0,
    }


def _recipe(root: Path, *, rotation: float = 0) -> Path:
    size = (24, 16)
    source = root / "workspace/source/sample.png"
    source_values = np.full((size[1], size[0], 4), (180, 140, 90, 173), dtype=np.uint8)
    source_values[4:9, 2:7, :3] = 20
    _save_rgba(source, source_values)

    old_text = np.zeros((size[1], size[0]), dtype=bool)
    old_text[4:9, 2:7] = True
    new_text = np.zeros_like(old_text)
    new_text[4:9, 12:16] = True
    editable = old_text | new_text
    protected = ~editable
    seam_guard = np.zeros_like(old_text)
    seam_guard[0, 0] = True
    mask_paths = {}
    for name, values in {
        "old_text": old_text,
        "new_text": new_text,
        "editable": editable,
        "protected": protected,
        "seam_guard": seam_guard,
    }.items():
        path = root / f"workspace/reviews/sample/input-{name}.png"
        _save_mask(path, values)
        mask_paths[name] = _descriptor(root, path)

    lettering = np.zeros_like(source_values)
    lettering[new_text] = (20, 220, 80, 255)
    lettering_path = root / "workspace/reviews/sample/selected-lettering.png"
    _save_rgba(lettering_path, lettering)
    lettering_mask_path = root / "workspace/reviews/sample/lettering-mask.png"
    _save_mask(lettering_mask_path, new_text)
    style = root / "workspace/reviews/sample/source-style.png"
    generated = root / "workspace/reviews/sample/generated-panel.png"
    _save_rgba(style, source_values)
    generated_values = source_values.copy()
    generated_values[..., 3] = 255
    _save_rgba(generated, generated_values)
    panel_ocr = root / "workspace/reviews/sample/panel-ocr.json"
    panel_ocr.write_text(
        json.dumps(
            {
                "recognition_contract": "approved-regions+nfc-literal-v1",
                "image_sha256": _sha256(generated),
                "regions": [
                    {
                        "region_id": "brand",
                        "expected_text": "한글",
                        "matched": True,
                        "match_mode": "nfc-literal",
                    }
                ],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    recipe = root / "workspace/reviews/sample/compose-recipe-v2.json"
    recipe.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "target_id": "sample",
                "mode": "vision-panel-localization",
                "source": _descriptor(root, source),
                "masks": mask_paths,
                "background": {"method": "telea", "inpaint_radius": 2},
                "panels": [
                    {
                        "panel_id": "front",
                        "model_signature": "stellar-ko:test-checkpoint",
                        "generation_attempts": 1,
                        "single_generation_panel": True,
                        "ocr_exact_match": True,
                        "panel_ocr": _descriptor(root, panel_ocr),
                        "source_style_reference": _descriptor(root, style),
                        "generated_panel": _descriptor(root, generated),
                        "regions": [
                            {
                                "region_id": "brand",
                                "exact_text": "한글",
                                "occurrences": 1,
                                "bbox": [10, 2, 18, 12],
                                "rotation_deg": rotation,
                                "direction": "left-to-right",
                                "ocr_exact_match": True,
                                "panel_transform": {
                                    "coordinate_space": "source-mip0",
                                    "crop_bbox": [8, 1, 20, 13],
                                    "padding_px": 2,
                                    "source_rotation_deg": rotation,
                                    "deskew_rotation_deg": rotation,
                                    "inverse_rotation_deg": -rotation,
                                    "selected_lettering_restored_to_source": True,
                                    "source_texture_resampled": False,
                                    "final_texture_resampled": False,
                                },
                                "source_typography": _typography(),
                                "result_typography": _typography(),
                                "typography_checks": {
                                    "font_character_matched": True,
                                    "style_matched": True,
                                    "size_matched": True,
                                    "alignment_matched": True,
                                    "spacing_matched": True,
                                    "direction_exact": True,
                                    "effects_matched": True,
                                    "surface_matched": True,
                                    "ink_height_delta_ratio": 0,
                                    "ink_width_delta_ratio": 0,
                                    "bbox_coverage_delta_ratio": 0,
                                    "rotation_delta_deg": 0,
                                },
                                "selected_lettering": _descriptor(root, lettering_path),
                                "lettering_mask": _descriptor(root, lettering_mask_path),
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return recipe


def test_vision_compose_uses_only_approved_lettering_and_reuses_background(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    paths = ProjectPaths.create(root)
    recipe = _recipe(root)

    first = compose_candidate(paths, "sample", recipe)
    second = compose_candidate(paths, "sample", recipe)

    assert first["compositor"]["mode"] == "vision-panel-localization"
    assert first["compositor"]["fixed_font_used"] is False
    assert first["compositor"]["background"]["cache_reused"] is False
    assert second["compositor"]["background"]["cache_reused"] is True
    assert first["alpha_equal"] is True
    assert first["changed_outside_editable"] == 0
    assert first["changed_inside_protected"] == 0
    assert first["changed_inside_seam_guard"] == 0
    assert (root / "workspace/drafts/sample/lettering-run.json").is_file()
    candidate_alpha = np.asarray(
        Image.open(root / "workspace/drafts/sample/candidate.png").convert("RGBA")
    )[..., 3]
    assert np.all(candidate_alpha == 173)


def test_vision_compose_records_arbitrary_rotation_inverse(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    report = compose_candidate(ProjectPaths.create(root), "sample", _recipe(root, rotation=37.5))

    transform = report["compositor"]["regions"][0]["panel_transform"]
    assert transform["deskew_rotation_deg"] == 37.5
    assert transform["inverse_rotation_deg"] == -37.5


def test_vision_compose_rejects_lettering_mask_outside_new_text(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    recipe = _recipe(root)
    data = json.loads(recipe.read_text(encoding="utf-8"))
    mask_path = root / data["panels"][0]["regions"][0]["lettering_mask"]["path"]
    values = np.asarray(Image.open(mask_path).convert("L"), dtype=np.uint8).copy()
    values[1, 1] = 255
    Image.fromarray(values, "L").save(mask_path)
    data["panels"][0]["regions"][0]["lettering_mask"]["sha256"] = _sha256(mask_path)
    recipe.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="레터링 알파"):
        compose_candidate(ProjectPaths.create(root), "sample", recipe)
