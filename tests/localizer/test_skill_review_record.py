from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (
    PROJECT_ROOT
    / ".agents"
    / "skills"
    / "localize-spt-food-textures"
    / "scripts"
    / "review_record.py"
)
SPEC = importlib.util.spec_from_file_location("skill_review_record", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
review_record = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review_record)


def _write(path: Path, value: bytes = b"evidence") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    return path


def _evidence(root: Path, name: str) -> dict[str, str]:
    path = _write(root / f"{name}.json", name.encode("utf-8"))
    return {"path": path.relative_to(root).as_posix(), "sha256": review_record._sha256(path)}


def _region() -> dict[str, object]:
    return {
        "region_id": "front-brand-01",
        "text": "MAYO",
        "script": "latin",
        "bbox": [10, 20, 50, 42],
        "rotation_deg": 0,
        "direction": "left-to-right",
        "face": "front",
        "artwork_direction": "뚜껑 위쪽을 향함",
    }


def _analysis_record(root: Path) -> dict[str, object]:
    source = _write(root / "source.png", b"source-pixels")
    record = {
        "schema_version": 1,
        "target_id": "mayo",
        "action": "localize",
        "expected_text": ["마요네즈"],
        "source": {
            "bundle_key": "assets/mayo.bundle",
            "texture": "item_food_mayo_D",
            "image": "source.png",
            "sha256": review_record._sha256(source),
            "width": 64,
            "height": 64,
            "color_mode": "RGBA",
            "texture_orientation": "원본 UV 방향",
            "artwork_direction": "뚜껑 위쪽을 향함",
        },
        "stages": {name: review_record._stage() for name in review_record.STAGES},
        "unresolved": [],
        "approvals": [],
    }
    ocr = {**_region(), "engine": "paddle", "model_signature": "model-v1", "confidence": 0.98}
    visual = _region()
    translation = {
        "region_id": "front-brand-01",
        "source_text": "MAYO",
        "meaning_ko": "마요네즈 제품명",
        "final_text_ko": "마요네즈",
        "occurrences": 1,
        "bbox": [10, 20, 50, 42],
        "rotation_deg": 0,
        "direction": "left-to-right",
        "face": "front",
        "visual_role": "제품명",
        "artwork_direction": "뚜껑 위쪽을 향함",
    }
    stage_data = {
        "source_ocr": {"detections": [ocr]},
        "source_visual": {"regions": [visual]},
        "cross_validation": {
            "regions": [
                {
                    "region_id": "front-brand-01",
                    "ocr_region_id": "front-brand-01",
                    "visual_region_id": "front-brand-01",
                    "agreed_text": "MAYO",
                    "matched": True,
                    "bbox": [10, 20, 50, 42],
                    "rotation_deg": 0,
                    "direction": "left-to-right",
                    "face": "front",
                    "artwork_direction": "뚜껑 위쪽을 향함",
                }
            ],
            "conflicts": [],
        },
        "translation": {"regions": [translation]},
    }
    for name, data in stage_data.items():
        record["stages"][name] = {
            "status": "pass",
            "evidence": [_evidence(root, name)],
            "data": data,
        }
    return record


def _complete_candidate(root: Path, record: dict[str, object]) -> None:
    masks = {}
    for name in ("old_text", "new_text", "editable", "protected", "seam_guard"):
        path = _write(root / "masks" / f"{name}.png", name.encode("utf-8"))
        masks[name] = {
            "path": path.relative_to(root).as_posix(),
            "sha256": review_record._sha256(path),
            "width": 64,
            "height": 64,
        }
    lettering_artifacts = {
        name: _evidence(root, f"lettering-{name}")
        for name in (
            "source-style-reference",
            "clean-background",
            "candidate-sheet",
            "selected-lettering",
            "lettering-mask",
        )
    }
    record["stages"]["edit_plan"] = {
        "status": "pass",
        "evidence": [_evidence(root, "edit-plan")],
        "data": {
            "masks": masks,
            "compositor": {
                "mode": "ai-reference-lettering",
                "fixed_font_used": False,
                "background_locked": True,
                "regions": [
                    {
                        "region_id": "front-brand-01",
                        "exact_text": "마요네즈",
                        "bbox": [10, 20, 50, 42],
                        "rotation_deg": 0,
                        "direction": "left-to-right",
                        "model_signature": "image-model-v1:settings-sha",
                        "candidate_count": 4,
                        "ocr_exact_match": True,
                        "style_match_passed": True,
                        "style_checks": {
                            "shape_matched": True,
                            "size_matched": True,
                            "direction_matched": True,
                            "spacing_matched": True,
                            "effects_matched": True,
                            "surface_integration_matched": True,
                            "old_logo_silhouette_absent": True,
                        },
                        "source_style_reference": lettering_artifacts["source-style-reference"],
                        "clean_background": lettering_artifacts["clean-background"],
                        "candidate_sheet": lettering_artifacts["candidate-sheet"],
                        "selected_lettering": lettering_artifacts["selected-lettering"],
                        "lettering_mask": lettering_artifacts["lettering-mask"],
                    }
                ],
            },
        },
    }
    record["stages"]["candidate_validation"] = {
        "status": "pass",
        "evidence": [_evidence(root, "candidate-validation")],
        "data": {
            "resized": False,
            "alpha_equal": True,
            "changed_outside_editable": 0,
            "changed_inside_protected": 0,
            "changed_inside_seam_guard": 0,
        },
    }
    candidate_sha = "c" * 64
    record["stages"]["post_ocr"] = {
        "status": "pass",
        "evidence": [_evidence(root, "post_ocr")],
        "data": {
            "candidate_sha256": candidate_sha,
            "engine_signature": "paddle+easy-v1",
            "forbidden_foreign_detected": False,
            "expected_text_matched": True,
            "duplicate_text_detected": False,
        },
    }
    record["stages"]["post_visual"] = {
        "status": "pass",
        "evidence": [_evidence(root, "post_visual")],
        "data": {
            "candidate_sha256": candidate_sha,
            "translation_matched": True,
            "text_orientation_matched": True,
            "artwork_orientation_matched": True,
            "color_preserved": True,
            "sharpness_passed": True,
            "seams_preserved": True,
            "lettering_shape_matched": True,
            "lettering_size_matched": True,
            "lettering_direction_matched": True,
            "lettering_spacing_matched": True,
            "lettering_effects_matched": True,
            "surface_integration_matched": True,
            "old_logo_silhouette_absent": True,
        },
    }


def test_init_record_uses_profile_and_never_overwrites(tmp_path: Path) -> None:
    destination = tmp_path / "review.json"
    created = review_record.init_record(PROJECT_ROOT, "mayo", destination)
    record = json.loads(created.read_text(encoding="utf-8"))

    assert record["target_id"] == "mayo"
    profile = json.loads((PROJECT_ROOT / "profiles" / "food" / "collection.json").read_text(encoding="utf-8"))
    expected = next(target["exact_text"] for target in profile["targets"] if target["id"] == "mayo")
    assert record["expected_text"] == expected
    assert set(record["stages"]) == set(review_record.STAGES)
    with pytest.raises(FileExistsError):
        review_record.init_record(PROJECT_ROOT, "mayo", destination)


def test_analysis_gate_is_fail_closed_and_binds_evidence_hash(tmp_path: Path) -> None:
    record = _analysis_record(tmp_path)

    assert review_record.validate_record(record, "analysis", project_root=tmp_path) == []

    (tmp_path / "source_ocr.json").write_bytes(b"changed-after-review")
    errors = review_record.validate_record(record, "analysis", project_root=tmp_path)
    assert any("현재 파일 SHA가 기록과 달라요" in error for error in errors)


def test_cross_validation_accepts_resolved_visual_only_region(tmp_path: Path) -> None:
    record = _analysis_record(tmp_path)
    visual_only = {
        **_region(),
        "region_id": "lid-arc",
        "text": "DEVILDOG'S FINEST MAYO",
        "bbox": [5, 5, 60, 18],
    }
    record["stages"]["source_visual"]["data"]["regions"].append(visual_only)
    record["stages"]["cross_validation"]["data"]["regions"].append(
        {
            "region_id": "lid-arc",
            "ocr_region_id": None,
            "visual_region_id": "lid-arc",
            "agreed_text": "DEVILDOG'S FINEST MAYO",
            "resolution": "visual_only",
            "matched": False,
            "resolved": True,
            "bbox": [5, 5, 60, 18],
            "rotation_deg": 0,
            "direction": "left-to-right",
            "face": "lid",
            "artwork_direction": "뚜껑 원호를 따름",
        }
    )

    assert review_record.validate_record(record, "analysis", project_root=tmp_path) == []


def test_candidate_gate_rejects_changes_outside_editable_mask(tmp_path: Path) -> None:
    record = _analysis_record(tmp_path)
    _complete_candidate(tmp_path, record)

    assert review_record.validate_record(record, "candidate", project_root=tmp_path) == []

    record["stages"]["candidate_validation"]["data"]["changed_outside_editable"] = 1
    errors = review_record.validate_record(record, "candidate", project_root=tmp_path)
    assert any("changed_outside_editable" in error for error in errors)


def test_candidate_gate_rejects_fixed_font_lettering(tmp_path: Path) -> None:
    record = _analysis_record(tmp_path)
    _complete_candidate(tmp_path, record)

    compositor = record["stages"]["edit_plan"]["data"]["compositor"]
    compositor["mode"] = "deterministic-font"
    compositor["fixed_font_used"] = True

    errors = review_record.validate_record(record, "candidate", project_root=tmp_path)
    assert any("mode: ai-reference-lettering" in error for error in errors)
    assert any("fixed_font_used: false" in error for error in errors)


def test_candidate_gate_requires_style_evidence_for_every_translation_region(tmp_path: Path) -> None:
    record = _analysis_record(tmp_path)
    _complete_candidate(tmp_path, record)

    record["stages"]["edit_plan"]["data"]["compositor"]["regions"] = []

    errors = review_record.validate_record(record, "candidate", project_root=tmp_path)
    assert any("regions: 비어 있지 않은 배열" in error for error in errors)


def test_candidate_gate_rejects_unchecked_lettering_style(tmp_path: Path) -> None:
    record = _analysis_record(tmp_path)
    _complete_candidate(tmp_path, record)

    region = record["stages"]["edit_plan"]["data"]["compositor"]["regions"][0]
    region["style_checks"]["effects_matched"] = False

    errors = review_record.validate_record(record, "candidate", project_root=tmp_path)
    assert any("style_checks.effects_matched" in error for error in errors)


def test_candidate_gate_binds_selected_ai_lettering_hash(tmp_path: Path) -> None:
    record = _analysis_record(tmp_path)
    _complete_candidate(tmp_path, record)
    region = record["stages"]["edit_plan"]["data"]["compositor"]["regions"][0]
    selected = tmp_path / region["selected_lettering"]["path"]

    selected.write_bytes(b"changed-after-selection")

    errors = review_record.validate_record(record, "candidate", project_root=tmp_path)
    assert any("selected_lettering: 현재 파일 SHA가 기록과 달라요" in error for error in errors)


def test_material_patch_is_bound_to_current_file_hash(tmp_path: Path) -> None:
    record = _analysis_record(tmp_path)
    _complete_candidate(tmp_path, record)
    mask = _write(tmp_path / "material-mask.png", b"mask")
    patch = _write(tmp_path / "material-patch.png", b"patch")
    record["stages"]["material_validation"] = {
        "status": "pass",
        "evidence": [_evidence(tmp_path, "material-validation")],
        "data": {
            "graph_scope": "resolved",
            "bindings": [{"material": "item", "property": "_SpecMap"}],
            "policies": {"item::_SpecMap": "neutralize_old_text"},
            "material_masks": {
                "item::_SpecMap": {
                    "path": mask.relative_to(tmp_path).as_posix(),
                    "sha256": review_record._sha256(mask),
                    "method": "patch",
                    "patch": patch.relative_to(tmp_path).as_posix(),
                    "patch_sha256": review_record._sha256(patch),
                }
            },
            "shared_consumers_resolved": True,
            "text_mask_sha256": "a" * 64,
            "alignment_passed": True,
            "foreign_relief_detected": False,
            "changed_outside_masks": 0,
        },
    }

    assert review_record.validate_record(record, "material", project_root=tmp_path) == []

    patch.write_bytes(b"changed")
    errors = review_record.validate_record(record, "material", project_root=tmp_path)
    assert any("patch: 현재 파일 SHA-256이 달라요" in error for error in errors)
