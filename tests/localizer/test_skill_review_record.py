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
        "needs_ocr_fallback": False,
        "typography": {
            "style_class": "condensed serif display",
            "stroke_character": "heavy verticals and wedge terminals",
            "glyph_proportions": "tall and narrow",
            "ink_bbox": [12, 22, 48, 40],
            "ink_width_px": 36,
            "ink_height_px": 18,
            "alignment": "centered baseline",
            "spacing": "tight tracking",
            "effects": "black fill and cream outline",
            "surface_finish": "worn print",
            "slant_deg": 0,
        },
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
        "source_visual": {
            "vision_first": True,
            "ocr_fallback_required": False,
            "regions": [visual],
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
            "generated-panel",
            "selected-lettering",
            "lettering-mask",
            "panel-ocr",
        )
    }
    source_typography = dict(_region()["typography"])
    result_typography = {
        **source_typography,
        "glyph_proportions": "tall and narrow Hangul",
        "ink_bbox": [13, 22, 48, 40],
        "ink_width_px": 35,
    }
    record["stages"]["edit_plan"] = {
        "status": "pass",
        "evidence": [_evidence(root, "edit-plan")],
        "data": {
            "masks": masks,
            "compositor": {
                "mode": "vision-panel-localization",
                "fixed_font_used": False,
                "single_pass_panels": True,
                "regions": [
                    {
                        "panel_id": "front-panel",
                        "region_id": "front-brand-01",
                        "exact_text": "마요네즈",
                        "occurrences": 1,
                        "bbox": [10, 20, 50, 42],
                        "rotation_deg": 0,
                        "direction": "left-to-right",
                        "model_signature": "image-model-v1:settings-sha",
                        "generation_attempts": 1,
                        "ocr_exact_match": True,
                        "panel_ocr": lettering_artifacts["panel-ocr"],
                        "panel_transform": {
                            "coordinate_space": "source-mip0",
                            "crop_bbox": [10, 20, 50, 42],
                            "padding_px": 0,
                            "source_rotation_deg": 0,
                            "deskew_rotation_deg": 0,
                            "inverse_rotation_deg": 0,
                            "selected_lettering_restored_to_source": True,
                            "source_texture_resampled": False,
                            "final_texture_resampled": False,
                        },
                        "source_typography": source_typography,
                        "result_typography": result_typography,
                        "typography_checks": {
                            "font_character_matched": True,
                            "style_matched": True,
                            "size_matched": True,
                            "alignment_matched": True,
                            "spacing_matched": True,
                            "direction_exact": True,
                            "effects_matched": True,
                            "surface_matched": True,
                            "ink_height_delta_ratio": 0.0,
                            "ink_width_delta_ratio": 0.03,
                            "bbox_coverage_delta_ratio": 0.03,
                            "rotation_delta_deg": 0.0,
                        },
                        "source_style_reference": lettering_artifacts["source-style-reference"],
                        "generated_panel": lettering_artifacts["generated-panel"],
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
            "match_mode": "nfc-literal",
            "oriented_region_ocr_complete": True,
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
            "font_character_matched": True,
            "lettering_style_matched": True,
            "lettering_shape_matched": True,
            "lettering_size_matched": True,
            "lettering_bbox_coverage_matched": True,
            "lettering_alignment_matched": True,
            "lettering_direction_matched": True,
            "lettering_spacing_matched": True,
            "lettering_rotation_matched": True,
            "lettering_effects_matched": True,
            "surface_integration_matched": True,
            "old_logo_silhouette_absent": True,
        },
    }


def _complete_material(
    root: Path,
    record: dict[str, object],
) -> tuple[Path, Path, dict[str, object]]:
    key = "item::_SpecMap"
    mask = _write(root / "material-mask.png", b"mask")
    patch = _write(root / "material-patch.png", b"patch")
    source_map = _write(root / "source-spec.png", b"source-map")
    channel_evidence = _write(root / "channel-evidence.json", b"channel-evidence")
    region = record["stages"]["edit_plan"]["data"]["compositor"]["regions"][0]
    new_text = record["stages"]["edit_plan"]["data"]["masks"]["new_text"]
    material_mask = {
        "path": mask.relative_to(root).as_posix(),
        "sha256": review_record._sha256(mask),
        "method": "patch",
        "patch": patch.relative_to(root).as_posix(),
        "patch_sha256": review_record._sha256(patch),
    }
    contract_map: dict[str, object] = {
        "policy": "neutralize_old_text",
        "identity": {
            "texture_bundle_key": "assets/mayo.bundle",
            "path_id": 23,
            "texture": "item_food_mayo_G",
            "role": "gloss",
            "width": 64,
            "height": 64,
            "format": 10,
            "uv_scale": [1.0, 1.0],
            "uv_offset": [0.0, 0.0],
        },
        "source_map": {
            "path": source_map.relative_to(root).as_posix(),
            "sha256": review_record._sha256(source_map),
        },
        "whole_map_generated": False,
        "shared_effect_compatible": True,
        "effect_kind": "remove-only",
        "channel_contract": {
            "semantics_verified": True,
            "verification_method": "controlled-render",
            "evidence": {
                "path": channel_evidence.relative_to(root).as_posix(),
                "sha256": review_record._sha256(channel_evidence),
            },
            "packing": "custom-spec-rgb",
            "used_channels": ["R", "G", "B"],
            "linear_data": True,
        },
        "source_effect_mask_sha256": review_record._sha256(mask),
        "neutralization_signature": "patch-copy:v1",
    }
    contract_map["base_cache_fingerprint"] = review_record._neutralized_base_fingerprint(
        contract_map, material_mask
    )
    record["stages"]["material_validation"] = {
        "status": "pass",
        "evidence": [_evidence(root, "material-validation")],
        "data": {
            "graph_scope": "resolved",
            "bindings": [
                {
                    "material_bundle_key": "assets/mayo.bundle",
                    "material_assets_file": "CAB-MAYO",
                    "material_path_id": 17,
                    "material": "item",
                    "property": "_SpecMap",
                    "texture_bundle_key": "assets/mayo.bundle",
                    "path_id": 23,
                    "texture": "item_food_mayo_G",
                    "scale": [1.0, 1.0],
                    "offset": [0.0, 0.0],
                }
            ],
            "policies": {key: "neutralize_old_text"},
            "material_masks": {key: material_mask},
            "shared_consumers": {
                key: [
                    {
                        "material": "item",
                        "material_bundle_key": "assets/mayo.bundle",
                        "material_assets_file": "CAB-MAYO",
                        "material_path_id": 17,
                        "property": "_SpecMap",
                        "scale": [1.0, 1.0],
                        "offset": [0.0, 0.0],
                    }
                ]
            },
            "shared_consumers_resolved": True,
            "text_mask_sha256": new_text["sha256"],
            "auxiliary_contract": {
                "schema_version": 1,
                "mode": "source-base+master-lettering-alpha-v1",
                "master_geometry": "selected-lettering-continuous-alpha",
                "whole_map_generation_used": False,
                "binary_new_text_resampled": False,
                "source_maps_immutable_outside_effect_masks": True,
                "master_lettering": [
                    {
                        "region_id": region["region_id"],
                        "selected_lettering_sha256": region["selected_lettering"]["sha256"],
                        "lettering_mask_sha256": region["lettering_mask"]["sha256"],
                    }
                ],
                "maps": {key: contract_map},
            },
        },
    }
    return patch, source_map, contract_map


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

    (tmp_path / "source_visual.json").write_bytes(b"changed-after-review")
    errors = review_record.validate_record(record, "analysis", project_root=tmp_path)
    assert any("현재 파일 SHA가 기록과 달라요" in error for error in errors)


def test_analysis_gate_ignores_stale_downstream_evidence(tmp_path: Path) -> None:
    record = _analysis_record(tmp_path)
    downstream = _evidence(tmp_path, "material-downstream")
    record["stages"]["material_validation"] = {
        "status": "pass",
        "evidence": [downstream],
        "data": {},
    }
    (tmp_path / downstream["path"]).write_bytes(b"changed-after-material-review")

    assert review_record.validate_record(record, "analysis", project_root=tmp_path) == []

    errors = review_record.validate_record(record, "material", project_root=tmp_path)
    assert any("현재 파일 SHA가 기록과 달라요" in error for error in errors)


def test_analysis_gate_uses_source_ocr_only_for_ambiguous_regions(tmp_path: Path) -> None:
    record = _analysis_record(tmp_path)
    visual = record["stages"]["source_visual"]["data"]["regions"][0]
    visual["needs_ocr_fallback"] = True
    record["stages"]["source_visual"]["data"]["ocr_fallback_required"] = True

    errors = review_record.validate_record(record, "analysis", project_root=tmp_path)
    assert any("stages.source_ocr" in error for error in errors)
    assert any("stages.cross_validation" in error for error in errors)

    ocr = {
        **_region(),
        "engine": "paddle",
        "model_signature": "model-v1",
        "confidence": 0.98,
    }
    record["stages"]["source_ocr"] = {
        "status": "pass",
        "evidence": [_evidence(tmp_path, "source_ocr")],
        "data": {"detections": [ocr]},
    }
    record["stages"]["cross_validation"] = {
        "status": "pass",
        "evidence": [_evidence(tmp_path, "cross_validation")],
        "data": {
            "regions": [{
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
            }],
            "conflicts": [],
        },
    }

    assert review_record.validate_record(record, "analysis", project_root=tmp_path) == []


def test_analysis_gate_rejects_missing_typography_signature(tmp_path: Path) -> None:
    record = _analysis_record(tmp_path)
    del record["stages"]["source_visual"]["data"]["regions"][0]["typography"]

    errors = review_record.validate_record(record, "analysis", project_root=tmp_path)

    assert any("typography signature 객체가 없어요" in error for error in errors)


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
    compositor["fixed_font_used"] = True

    errors = review_record.validate_record(record, "candidate", project_root=tmp_path)
    assert any("fixed_font_used: false" in error for error in errors)


def test_candidate_gate_rejects_old_hybrid_mode(tmp_path: Path) -> None:
    record = _analysis_record(tmp_path)
    _complete_candidate(tmp_path, record)

    compositor = record["stages"]["edit_plan"]["data"]["compositor"]
    compositor["mode"] = "hybrid-role-lettering"

    errors = review_record.validate_record(record, "candidate", project_root=tmp_path)

    assert any("mode: vision-panel-localization" in error for error in errors)


def test_candidate_gate_rejects_typography_size_drift(tmp_path: Path) -> None:
    record = _analysis_record(tmp_path)
    _complete_candidate(tmp_path, record)

    region = record["stages"]["edit_plan"]["data"]["compositor"]["regions"][0]
    region["typography_checks"]["ink_height_delta_ratio"] = 0.11

    errors = review_record.validate_record(record, "candidate", project_root=tmp_path)

    assert any("ink_height_delta_ratio: 0~0.10" in error for error in errors)


def test_candidate_gate_rejects_typography_rotation_drift(tmp_path: Path) -> None:
    record = _analysis_record(tmp_path)
    _complete_candidate(tmp_path, record)

    region = record["stages"]["edit_plan"]["data"]["compositor"]["regions"][0]
    region["typography_checks"]["rotation_delta_deg"] = 2.1

    errors = review_record.validate_record(record, "candidate", project_root=tmp_path)

    assert any("rotation_delta_deg: 절댓값 2.0" in error for error in errors)


def test_candidate_gate_accepts_more_than_two_ocr_driven_attempts(tmp_path: Path) -> None:
    record = _analysis_record(tmp_path)
    _complete_candidate(tmp_path, record)
    region = record["stages"]["edit_plan"]["data"]["compositor"]["regions"][0]
    region["generation_attempts"] = 4

    assert review_record.validate_record(record, "candidate", project_root=tmp_path) == []


def test_candidate_gate_rejects_zero_generation_attempts(tmp_path: Path) -> None:
    record = _analysis_record(tmp_path)
    _complete_candidate(tmp_path, record)
    region = record["stages"]["edit_plan"]["data"]["compositor"]["regions"][0]
    region["generation_attempts"] = 0

    errors = review_record.validate_record(record, "candidate", project_root=tmp_path)

    assert any("generation_attempts: 1 이상의 정수" in error for error in errors)


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
    region["typography_checks"]["effects_matched"] = False

    errors = review_record.validate_record(record, "candidate", project_root=tmp_path)
    assert any("typography_checks.effects_matched" in error for error in errors)


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
    patch, _, _ = _complete_material(tmp_path, record)

    assert review_record.validate_record(record, "material", project_root=tmp_path) == []
    assert review_record.validate_record(record, "material") == []

    patch.write_bytes(b"changed")
    errors = review_record.validate_record(record, "material", project_root=tmp_path)
    assert any("patch: 현재 파일 SHA가 기록과 달라요" in error for error in errors)


def test_material_gate_rejects_whole_map_generation(tmp_path: Path) -> None:
    record = _analysis_record(tmp_path)
    _complete_candidate(tmp_path, record)
    _, _, contract_map = _complete_material(tmp_path, record)

    contract_map["whole_map_generated"] = True
    errors = review_record.validate_record(record, "material", project_root=tmp_path)

    assert any("whole_map_generated: false여야 해요" in error for error in errors)


def test_material_gate_allows_byte_preserve_without_channel_guessing(tmp_path: Path) -> None:
    record = _analysis_record(tmp_path)
    _complete_candidate(tmp_path, record)
    _, _, contract_map = _complete_material(tmp_path, record)
    data = record["stages"]["material_validation"]["data"]
    data["policies"]["item::_SpecMap"] = "preserve"
    contract_map["policy"] = "preserve"
    contract_map["effect_kind"] = "none"
    contract_map["shared_effect_compatible"] = False
    contract_map.pop("channel_contract")

    assert review_record.validate_record(record, "material", project_root=tmp_path) == []


def test_material_gate_groups_duplicate_material_property_only_with_same_aux_st(
    tmp_path: Path,
) -> None:
    record = _analysis_record(tmp_path)
    _complete_candidate(tmp_path, record)
    _complete_material(tmp_path, record)
    data = record["stages"]["material_validation"]["data"]
    duplicate = dict(data["bindings"][0])
    duplicate["material_assets_file"] = "CAB-MAYO-DUPLICATE"
    duplicate["material_path_id"] = 19
    data["bindings"].append(duplicate)

    assert review_record.validate_record(
        record, "material", project_root=tmp_path
    ) == []

    duplicate["offset"] = [0.25, 0.0]
    errors = review_record.validate_record(record, "material", project_root=tmp_path)
    assert any("identity.uv_offset: binding과 달라요" in error for error in errors)


def test_material_gate_requires_serialized_material_identity(tmp_path: Path) -> None:
    record = _analysis_record(tmp_path)
    _complete_candidate(tmp_path, record)
    _complete_material(tmp_path, record)
    data = record["stages"]["material_validation"]["data"]
    data["bindings"][0].pop("material_assets_file")
    data["shared_consumers"]["item::_SpecMap"][0].pop("material_assets_file")

    errors = review_record.validate_record(record, "material", project_root=tmp_path)

    assert any("bindings[0].material_assets_file: 비어 있어요" in error for error in errors)
    assert any("shared_consumers.item::_SpecMap[0].material_assets_file" in error for error in errors)


def test_material_gate_binds_master_continuous_alpha(tmp_path: Path) -> None:
    record = _analysis_record(tmp_path)
    _complete_candidate(tmp_path, record)
    _complete_material(tmp_path, record)
    master = record["stages"]["material_validation"]["data"]["auxiliary_contract"][
        "master_lettering"
    ][0]

    master["selected_lettering_sha256"] = "d" * 64
    errors = review_record.validate_record(record, "material", project_root=tmp_path)

    assert any("승인된 selected_lettering/lettering_mask와 달라요" in error for error in errors)


def test_material_gate_reports_malformed_master_artifact_without_crashing(
    tmp_path: Path,
) -> None:
    record = _analysis_record(tmp_path)
    _complete_candidate(tmp_path, record)
    _complete_material(tmp_path, record)
    region = record["stages"]["edit_plan"]["data"]["compositor"]["regions"][0]
    region["selected_lettering"] = "not-an-artifact"

    errors = review_record.validate_record(record, "material", project_root=tmp_path)

    assert any("selected_lettering: 파일 명세가 없어요" in error for error in errors)
    assert any("승인된 selected_lettering/lettering_mask와 달라요" in error for error in errors)


def test_master_lettering_handles_malformed_stage_containers() -> None:
    assert review_record._master_lettering({"edit_plan": None}) == {}
    assert review_record._master_lettering({"edit_plan": {"data": []}}) == {}


@pytest.mark.parametrize(
    "malformed",
    [None, {"status": "pass", "evidence": [], "data": []}],
)
def test_material_validation_handles_malformed_edit_plan_stage(
    tmp_path: Path,
    malformed: object,
) -> None:
    record = _analysis_record(tmp_path)
    _complete_candidate(tmp_path, record)
    _complete_material(tmp_path, record)
    record["stages"]["edit_plan"] = malformed

    errors = review_record.validate_record(record, "material", project_root=tmp_path)

    assert errors


def test_material_gate_binds_map_identity_to_material_slot(tmp_path: Path) -> None:
    record = _analysis_record(tmp_path)
    _complete_candidate(tmp_path, record)
    _, _, contract_map = _complete_material(tmp_path, record)
    contract_map["identity"]["uv_offset"] = [0.25, 0.0]

    errors = review_record.validate_record(record, "material", project_root=tmp_path)

    assert any("identity.uv_offset: binding과 달라요" in error for error in errors)


def test_material_gate_rejects_color_channels_for_packed_normal(tmp_path: Path) -> None:
    record = _analysis_record(tmp_path)
    _complete_candidate(tmp_path, record)
    _, _, contract_map = _complete_material(tmp_path, record)
    contract_map["identity"]["role"] = "normal"

    errors = review_record.validate_record(record, "material", project_root=tmp_path)

    assert any(
        "Normal은 DXT5(12) DXT5nm의 G/A만 사용해야 해요" in error
        for error in errors
    )


def test_material_gate_rejects_effect_derivation_without_v1_contract(tmp_path: Path) -> None:
    record = _analysis_record(tmp_path)
    _complete_candidate(tmp_path, record)
    _complete_material(tmp_path, record)
    data = record["stages"]["material_validation"]["data"]
    data["policies"]["item::_SpecMap"] = "neutralize_and_derive"
    data["auxiliary_contract"]["maps"]["item::_SpecMap"]["policy"] = (
        "neutralize_and_derive"
    )

    errors = review_record.validate_record(record, "material", project_root=tmp_path)

    assert any("v1 파생 계약이 없어요" in error for error in errors)


def test_material_gate_accepts_hash_pinned_master_alpha_derivation(tmp_path: Path) -> None:
    record = _analysis_record(tmp_path)
    _complete_candidate(tmp_path, record)
    _complete_material(tmp_path, record)
    data = record["stages"]["material_validation"]["data"]
    key = "item::_SpecMap"
    contract_map = data["auxiliary_contract"]["maps"][key]
    parameters = {"channel_deltas": {"R": 24.0, "G": 24.0, "B": 24.0}}
    measurement = _write(
        tmp_path / "effect-measurement.json",
        json.dumps(
            {
                "schema_version": 1,
                "role": "gloss",
                "method": "source-effect-sampling",
                "source_map_sha256": contract_map["source_map"]["sha256"],
                "source_effect_mask_sha256": contract_map["source_effect_mask_sha256"],
                "measured_parameters": parameters,
                "sample_count": 32,
            }
        ).encode("utf-8"),
    )
    region_id = data["auxiliary_contract"]["master_lettering"][0]["region_id"]
    data["policies"][key] = "neutralize_and_derive"
    data["bindings"].append(
        {
            "material_bundle_key": "assets/mayo.bundle",
            "material_assets_file": "CAB-MAYO",
            "material_path_id": 17,
            "material": "item",
            "property": "_MainTex",
            "texture_bundle_key": "assets/mayo.bundle",
            "path_id": 11,
            "texture": "item_food_mayo_D",
            "scale": [1.0, 1.0],
            "offset": [0.0, 0.0],
        }
    )
    contract_map.update(
        {
            "policy": "neutralize_and_derive",
            "effect_kind": "master-alpha-gloss",
            "derivation": {
                "schema_version": 1,
                "producer": "linear-gloss-delta-from-master-alpha:v1",
                "physical_component": "all-selected-lettering-alpha",
                "master_region_ids": [region_id],
                "projection": {
                    "signature": "continuous-alpha-same-st-integer-area:v1",
                    "source_size": [64, 64],
                    "target_size": [64, 64],
                    "diffuse_uv_scale": [1.0, 1.0],
                    "diffuse_uv_offset": [0.0, 0.0],
                    "auxiliary_uv_scale": [1.0, 1.0],
                    "auxiliary_uv_offset": [0.0, 0.0],
                    "v_axis": "png-top-left+unity-v-up",
                    "texel_center_sampling": True,
                },
                "effect_parameters": parameters,
                "effect_measurement": {
                    "path": measurement.relative_to(tmp_path).as_posix(),
                    "sha256": review_record._sha256(measurement),
                },
                "alignment_limits": {
                    "center_error_texels": 0.5,
                    "bbox_edge_error_texels": 1.0,
                    "rotation_error_deg": 0.0,
                },
            },
        }
    )

    errors = review_record.validate_record(record, "material", project_root=tmp_path)

    assert errors == []

    contract_map["derivation"]["effect_parameters"]["channel_deltas"]["R"] = 25.0
    errors = review_record.validate_record(record, "material", project_root=tmp_path)
    assert any("source/effect_parameters 계약과 내용이 달라요" in error for error in errors)


def test_material_gate_rejects_different_diffuse_and_auxiliary_st(tmp_path: Path) -> None:
    record = _analysis_record(tmp_path)
    _complete_candidate(tmp_path, record)
    _complete_material(tmp_path, record)
    data = record["stages"]["material_validation"]["data"]
    key = "item::_SpecMap"
    contract_map = data["auxiliary_contract"]["maps"][key]
    parameters = {"channel_deltas": {"R": 24.0, "G": 24.0, "B": 24.0}}
    measurement = _write(
        tmp_path / "effect-measurement.json",
        json.dumps(
            {
                "schema_version": 1,
                "role": "gloss",
                "method": "source-effect-sampling",
                "source_map_sha256": contract_map["source_map"]["sha256"],
                "source_effect_mask_sha256": contract_map["source_effect_mask_sha256"],
                "measured_parameters": parameters,
                "sample_count": 32,
            }
        ).encode("utf-8"),
    )
    region_id = data["auxiliary_contract"]["master_lettering"][0]["region_id"]
    data["policies"][key] = "neutralize_and_derive"
    data["bindings"].append(
        {
            "material_bundle_key": "assets/mayo.bundle",
            "material_assets_file": "CAB-MAYO",
            "material_path_id": 17,
            "material": "item",
            "property": "_MainTex",
            "texture_bundle_key": "assets/mayo.bundle",
            "path_id": 11,
            "texture": "item_food_mayo_D",
            "scale": [2.0, 2.0],
            "offset": [0.0, 0.0],
        }
    )
    contract_map.update(
        {
            "policy": "neutralize_and_derive",
            "effect_kind": "master-alpha-gloss",
            "derivation": {
                "schema_version": 1,
                "producer": "linear-gloss-delta-from-master-alpha:v1",
                "physical_component": "all-selected-lettering-alpha",
                "master_region_ids": [region_id],
                "projection": {
                    "signature": "continuous-alpha-same-st-integer-area:v1",
                    "source_size": [64, 64],
                    "target_size": [64, 64],
                    "diffuse_uv_scale": [2.0, 2.0],
                    "diffuse_uv_offset": [0.0, 0.0],
                    "auxiliary_uv_scale": [1.0, 1.0],
                    "auxiliary_uv_offset": [0.0, 0.0],
                    "v_axis": "png-top-left+unity-v-up",
                    "texel_center_sampling": True,
                },
                "effect_parameters": parameters,
                "effect_measurement": {
                    "path": measurement.relative_to(tmp_path).as_posix(),
                    "sha256": review_record._sha256(measurement),
                },
                "alignment_limits": {
                    "center_error_texels": 0.5,
                    "bbox_edge_error_texels": 1.0,
                    "rotation_error_deg": 0.0,
                },
            },
        }
    )

    errors = review_record.validate_record(record, "material", project_root=tmp_path)

    assert any("v1은 Diffuse/보조맵 ST가 같아야 해요" in error for error in errors)


def test_material_source_map_is_hash_pinned(tmp_path: Path) -> None:
    record = _analysis_record(tmp_path)
    _complete_candidate(tmp_path, record)
    _, source_map, _ = _complete_material(tmp_path, record)

    source_map.write_bytes(b"changed-source")
    errors = review_record.validate_record(record, "material", project_root=tmp_path)

    assert any("source_map: 현재 파일 SHA가 기록과 달라요" in error for error in errors)


def test_material_base_cache_fingerprint_covers_neutralization_recipe(
    tmp_path: Path,
) -> None:
    record = _analysis_record(tmp_path)
    _complete_candidate(tmp_path, record)
    _, _, contract_map = _complete_material(tmp_path, record)

    contract_map["neutralization_signature"] = "patch-copy:v2"
    errors = review_record.validate_record(record, "material", project_root=tmp_path)

    assert any("base_cache_fingerprint: 현재 입력 계약과 달라요" in error for error in errors)
