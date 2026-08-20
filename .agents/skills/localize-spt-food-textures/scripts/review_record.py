#!/usr/bin/env python3
"""품목별 판독·검증 기록을 만들고 단계 누락을 fail-closed로 검사해요."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Any


STATUSES = {"pending", "pass", "block", "review", "error"}
STAGES = (
    "source_ocr",
    "source_visual",
    "cross_validation",
    "translation",
    "edit_plan",
    "candidate_validation",
    "post_ocr",
    "post_visual",
    "material_validation",
    "mip_validation",
    "bundle_validation",
    "runtime_validation",
    "release_validation",
)
ANALYSIS_STAGES = ("source_visual", "translation")
CANDIDATE_STAGES = ANALYSIS_STAGES + (
    "edit_plan",
    "candidate_validation",
    "post_ocr",
    "post_visual",
)
THROUGH = {
    "analysis": ANALYSIS_STAGES,
    "candidate": CANDIDATE_STAGES,
    "material": CANDIDATE_STAGES + ("material_validation",),
    "release": CANDIDATE_STAGES
    + (
        "material_validation",
        "mip_validation",
        "bundle_validation",
        "runtime_validation",
        "release_validation",
    ),
}
DIRECTIONS = {"left-to-right", "right-to-left", "top-to-bottom", "bottom-to-top"}
CROSS_RESOLUTIONS = {"matched", "visual_only", "ocr_only_resolved"}
TYPOGRAPHY_TEXT_FIELDS = (
    "style_class",
    "stroke_character",
    "glyph_proportions",
    "alignment",
    "spacing",
    "effects",
    "surface_finish",
)
TYPOGRAPHY_BOOLEAN_CHECKS = (
    "font_character_matched",
    "style_matched",
    "size_matched",
    "alignment_matched",
    "spacing_matched",
    "direction_exact",
    "effects_matched",
    "surface_matched",
)
MATERIAL_POLICIES = {"preserve", "neutralize_old_text", "neutralize_and_derive"}
MATERIAL_ROLES = {"normal", "gloss"}
MATERIAL_DIFFUSE_PROPERTIES = {"_MainTex", "_BaseMap", "_BaseColorMap"}
MATERIAL_NORMAL_PROPERTIES = {"_BumpMap", "_NormalMap"}
MATERIAL_GLOSS_PROPERTIES = {"_SpecMap", "_GlossMap", "_MetallicGlossMap"}
MATERIAL_PROJECTION_SIGNATURE = "continuous-alpha-same-st-integer-area:v1"
MATERIAL_NORMAL_SIGNATURE = "dxt5nm-rnm-height-from-master-alpha:v1"
MATERIAL_GLOSS_SIGNATURE = "linear-gloss-delta-from-master-alpha:v1"


def _project_root(start: Path) -> Path:
    for candidate in (start.resolve(), *start.resolve().parents):
        if (candidate / "profiles" / "food" / "collection.json").is_file():
            return candidate
    raise ValueError("profiles/food/collection.json이 있는 프로젝트 루트를 찾지 못했어요")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _project_file(project_root: Path, value: Any, location: str) -> tuple[Path | None, list[str]]:
    if not _nonempty_string(value):
        return None, [f"{location}: 비어 있어요"]
    path = Path(str(value))
    if path.is_absolute() or ".." in path.parts:
        return None, [f"{location}: 프로젝트 상대 경로여야 해요"]
    resolved = (project_root / path).resolve()
    try:
        resolved.relative_to(project_root.resolve())
    except ValueError:
        return None, [f"{location}: 프로젝트 밖을 가리켜요"]
    return resolved, []


def _stage() -> dict[str, Any]:
    return {"status": "pending", "evidence": [], "data": {}}


def _stage_data(stages: Any, name: str) -> dict[str, Any]:
    if not isinstance(stages, dict):
        return {}
    stage = stages.get(name)
    if not isinstance(stage, dict):
        return {}
    data = stage.get("data")
    return data if isinstance(data, dict) else {}


def init_record(project_root: Path, target_id: str, output: Path | None = None) -> Path:
    profile_path = project_root / "profiles" / "food" / "collection.json"
    profile = _read_json(profile_path)
    targets = [target for target in profile.get("targets", []) if target.get("id") == target_id]
    if len(targets) != 1:
        raise ValueError(f"profile에서 target {target_id!r}를 정확히 하나 찾지 못했어요")
    target = targets[0]
    destination = output or project_root / "workspace" / "reviews" / target_id / "review.json"
    destination = destination.expanduser().resolve()
    if destination.exists():
        raise FileExistsError(f"기존 작업 기록을 덮어쓰지 않아요: {destination}")
    record = {
        "schema_version": 1,
        "target_id": target_id,
        "action": target.get("action", "localize"),
        "expected_text": list(target.get("exact_text", [])),
        "source": {
            "bundle_key": target.get("bundle_key", ""),
            "texture": target.get("texture", ""),
            "image": "",
            "sha256": "",
            "width": 0,
            "height": 0,
            "color_mode": "",
            "texture_orientation": "",
            "artwork_direction": "",
        },
        "stages": {name: _stage() for name in STAGES},
        "unresolved": [],
        "approvals": [],
    }
    _write_json(destination, record)
    return destination


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _valid_sha256(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _validate_region(region: Any, location: str, *, ocr: bool) -> list[str]:
    errors: list[str] = []
    if not isinstance(region, dict):
        return [f"{location}: 객체가 아니에요"]
    for field in ("region_id", "text", "script", "face", "artwork_direction"):
        if not _nonempty_string(region.get(field)):
            errors.append(f"{location}.{field}: 비어 있어요")
    bbox = region.get("bbox")
    if (
        not isinstance(bbox, list)
        or len(bbox) != 4
        or not all(isinstance(value, int) and not isinstance(value, bool) for value in bbox)
        or bbox[0] < 0
        or bbox[1] < 0
        or bbox[2] <= bbox[0]
        or bbox[3] <= bbox[1]
    ):
        errors.append(f"{location}.bbox: [x0, y0, x1, y1] 원본 픽셀 좌표가 아니에요")
    rotation = region.get("rotation_deg")
    if not isinstance(rotation, (int, float)) or isinstance(rotation, bool):
        errors.append(f"{location}.rotation_deg: 숫자가 아니에요")
    if region.get("direction") not in DIRECTIONS:
        errors.append(f"{location}.direction: 지원하는 읽기 방향이 아니에요")
    if ocr:
        for field in ("engine", "model_signature"):
            if not _nonempty_string(region.get(field)):
                errors.append(f"{location}.{field}: 비어 있어요")
        confidence = region.get("confidence")
        if (
            not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or not 0 <= float(confidence) <= 1
        ):
            errors.append(f"{location}.confidence: 0~1 숫자가 아니에요")
    return errors


def _validate_typography_signature(signature: Any, location: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(signature, dict):
        return [f"{location}: typography signature 객체가 없어요"]
    for field in TYPOGRAPHY_TEXT_FIELDS:
        if not _nonempty_string(signature.get(field)):
            errors.append(f"{location}.{field}: 비어 있어요")
    ink_bbox = signature.get("ink_bbox")
    if (
        not isinstance(ink_bbox, list)
        or len(ink_bbox) != 4
        or not all(isinstance(value, int) and not isinstance(value, bool) for value in ink_bbox)
        or ink_bbox[0] < 0
        or ink_bbox[1] < 0
        or ink_bbox[2] <= ink_bbox[0]
        or ink_bbox[3] <= ink_bbox[1]
    ):
        errors.append(f"{location}.ink_bbox: [x0, y0, x1, y1] 원본 픽셀 좌표가 아니에요")
    for field in ("ink_width_px", "ink_height_px"):
        value = signature.get(field)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            errors.append(f"{location}.{field}: 0보다 큰 숫자가 아니에요")
    slant = signature.get("slant_deg")
    if not isinstance(slant, (int, float)) or isinstance(slant, bool):
        errors.append(f"{location}.slant_deg: 숫자가 아니에요")
    return errors


def _validate_visual_region(region: Any, location: str) -> list[str]:
    errors = _validate_region(region, location, ocr=False)
    if not isinstance(region, dict):
        return errors
    if not isinstance(region.get("needs_ocr_fallback"), bool):
        errors.append(f"{location}.needs_ocr_fallback: boolean이어야 해요")
    errors.extend(
        _validate_typography_signature(region.get("typography"), f"{location}.typography")
    )
    return errors


def _validate_translation(region: Any, location: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(region, dict):
        return [f"{location}: 객체가 아니에요"]
    for field in (
        "region_id",
        "source_text",
        "meaning_ko",
        "final_text_ko",
        "face",
        "visual_role",
        "artwork_direction",
    ):
        if not _nonempty_string(region.get(field)):
            errors.append(f"{location}.{field}: 비어 있어요")
    occurrences = region.get("occurrences")
    if not isinstance(occurrences, int) or isinstance(occurrences, bool) or occurrences < 1:
        errors.append(f"{location}.occurrences: 1 이상의 정수가 아니에요")
    errors.extend(_validate_region({**region, "text": region.get("source_text", ""), "script": "source"}, location, ocr=False))
    return errors


def _validate_cross_region(region: Any, location: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(region, dict):
        return [f"{location}: 객체가 아니에요"]
    for field in ("region_id", "agreed_text"):
        if not _nonempty_string(region.get(field)):
            errors.append(f"{location}.{field}: 비어 있어요")
    resolution = region.get("resolution", "matched")
    if resolution not in CROSS_RESOLUTIONS:
        errors.append(f"{location}.resolution: 지원하는 해소 방식이 아니에요")
    if resolution == "matched":
        for field in ("ocr_region_id", "visual_region_id"):
            if not _nonempty_string(region.get(field)):
                errors.append(f"{location}.{field}: 비어 있어요")
        if region.get("matched") is not True:
            errors.append(f"{location}.matched: true여야 해요")
    elif resolution == "visual_only":
        if not _nonempty_string(region.get("visual_region_id")):
            errors.append(f"{location}.visual_region_id: 비어 있어요")
        if region.get("ocr_region_id") not in {None, ""}:
            errors.append(f"{location}.ocr_region_id: visual_only에서는 비어 있어야 해요")
        if region.get("matched") is not False or region.get("resolved") is not True:
            errors.append(f"{location}: visual_only는 matched=false, resolved=true여야 해요")
    elif resolution == "ocr_only_resolved":
        if not _nonempty_string(region.get("ocr_region_id")):
            errors.append(f"{location}.ocr_region_id: 비어 있어요")
        if region.get("visual_region_id") not in {None, ""}:
            errors.append(f"{location}.visual_region_id: ocr_only_resolved에서는 비어 있어야 해요")
        if region.get("matched") is not False or region.get("resolved") is not True:
            errors.append(f"{location}: ocr_only_resolved는 matched=false, resolved=true여야 해요")
    normalized = {
        **region,
        "text": region.get("agreed_text", ""),
        "script": region.get("script", "source"),
        "artwork_direction": region.get("artwork_direction", ""),
    }
    errors.extend(_validate_region(normalized, location, ocr=False))
    return errors


def _validate_evidence(
    values: Any,
    location: str,
    *,
    project_root: Path | None,
    required: bool,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(values, list):
        return [f"{location}: 배열이 아니에요"]
    if required and not values:
        errors.append(f"{location}: 통과 상태인데 증거 파일이 없어요")
    for index, evidence in enumerate(values):
        item = f"{location}[{index}]"
        if not isinstance(evidence, dict):
            errors.append(f"{item}: 객체가 아니에요")
            continue
        if not _nonempty_string(evidence.get("path")):
            errors.append(f"{item}.path: 비어 있어요")
        if not _valid_sha256(evidence.get("sha256")):
            errors.append(f"{item}.sha256: SHA-256 형식이 아니에요")
        if project_root is None:
            continue
        path, path_errors = _project_file(project_root, evidence.get("path"), f"{item}.path")
        errors.extend(path_errors)
        if path is None:
            continue
        if not path.is_file():
            errors.append(f"{item}.path: 파일이 없어요: {path}")
        elif _valid_sha256(evidence.get("sha256")) and _sha256(path) != evidence["sha256"]:
            errors.append(f"{item}: 현재 파일 SHA가 기록과 달라요")
    return errors


def _validate_artifact(
    descriptor: Any,
    location: str,
    project_root: Path | None,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(descriptor, dict):
        return [f"{location}: 파일 명세가 없어요"]
    if not _nonempty_string(descriptor.get("path")):
        errors.append(f"{location}.path: 비어 있어요")
    if not _valid_sha256(descriptor.get("sha256")):
        errors.append(f"{location}.sha256: SHA-256 형식이 아니에요")
    if project_root is None:
        return errors
    path, path_errors = _project_file(project_root, descriptor.get("path"), f"{location}.path")
    errors.extend(path_errors)
    if path is None:
        return errors
    if not path.is_file():
        errors.append(f"{location}.path: 파일이 없어요: {path}")
    elif _valid_sha256(descriptor.get("sha256")) and _sha256(path) != descriptor["sha256"]:
        errors.append(f"{location}: 현재 파일 SHA가 기록과 달라요")
    return errors


def _validate_lettering(
    compositor: Any,
    translations: Any,
    project_root: Path | None,
) -> list[str]:
    errors: list[str] = []
    location = "stages.edit_plan.data.compositor"
    if not isinstance(compositor, dict):
        return [f"{location}: 객체가 없어요"]
    if compositor.get("mode") != "vision-panel-localization":
        errors.append(f"{location}.mode: vision-panel-localization이어야 해요")
    if compositor.get("fixed_font_used") is not False:
        errors.append(f"{location}.fixed_font_used: false여야 해요")
    if compositor.get("single_pass_panels") is not True:
        errors.append(f"{location}.single_pass_panels: true여야 해요")

    expected: dict[str, dict[str, Any]] = {}
    if isinstance(translations, list):
        expected = {
            str(region.get("region_id")): region
            for region in translations
            if isinstance(region, dict) and _nonempty_string(region.get("region_id"))
        }
    regions = compositor.get("regions")
    if not isinstance(regions, list) or not regions:
        errors.append(f"{location}.regions: 비어 있지 않은 배열이어야 해요")
        return errors

    seen: set[str] = set()
    for index, region in enumerate(regions):
        item = f"{location}.regions[{index}]"
        if not isinstance(region, dict):
            errors.append(f"{item}: 객체가 아니에요")
            continue
        region_id = region.get("region_id")
        if not _nonempty_string(region_id):
            errors.append(f"{item}.region_id: 비어 있어요")
            continue
        region_id = str(region_id)
        if region_id in seen:
            errors.append(f"{item}.region_id: 중복됐어요")
        seen.add(region_id)
        translation = expected.get(region_id)
        if translation is None:
            errors.append(f"{item}.region_id: 번역 영역에 없는 ID예요")
        else:
            if region.get("exact_text") != translation.get("final_text_ko"):
                errors.append(f"{item}.exact_text: 확정 한국어와 달라요")
            for field in ("bbox", "rotation_deg", "direction"):
                if region.get(field) != translation.get(field):
                    errors.append(f"{item}.{field}: 번역 명세와 달라요")
        if not _nonempty_string(region.get("model_signature")):
            errors.append(f"{item}.model_signature: 비어 있어요")
        attempts = region.get("generation_attempts")
        if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 1:
            errors.append(f"{item}.generation_attempts: 1 이상의 정수여야 해요")
        if region.get("ocr_exact_match") is not True:
            errors.append(f"{item}.ocr_exact_match: true여야 해요")
        if not _nonempty_string(region.get("panel_id")):
            errors.append(f"{item}.panel_id: 비어 있어요")
        occurrences = region.get("occurrences")
        if (
            not isinstance(occurrences, int)
            or isinstance(occurrences, bool)
            or occurrences < 1
        ):
            errors.append(f"{item}.occurrences: 1 이상의 정수여야 해요")
        errors.extend(
            _validate_artifact(region.get("panel_ocr"), f"{item}.panel_ocr", project_root)
        )
        transform = region.get("panel_transform")
        if not isinstance(transform, dict):
            errors.append(f"{item}.panel_transform: 객체가 없어요")
        else:
            if transform.get("coordinate_space") != "source-mip0":
                errors.append(f"{item}.panel_transform.coordinate_space: source-mip0여야 해요")
            crop_bbox = transform.get("crop_bbox")
            if (
                not isinstance(crop_bbox, list)
                or len(crop_bbox) != 4
                or not all(isinstance(value, int) and not isinstance(value, bool) for value in crop_bbox)
            ):
                errors.append(f"{item}.panel_transform.crop_bbox: 정수 bbox여야 해요")
            elif translation is not None:
                target_bbox = translation.get("bbox")
                if (
                    isinstance(target_bbox, list)
                    and len(target_bbox) == 4
                    and not (
                        crop_bbox[0] <= target_bbox[0]
                        and crop_bbox[1] <= target_bbox[1]
                        and crop_bbox[2] >= target_bbox[2]
                        and crop_bbox[3] >= target_bbox[3]
                    )
                ):
                    errors.append(
                        f"{item}.panel_transform.crop_bbox: 번역 bbox를 포함해야 해요"
                    )
            source_rotation = transform.get("source_rotation_deg")
            deskew = transform.get("deskew_rotation_deg")
            inverse = transform.get("inverse_rotation_deg")
            expected_rotation = region.get("rotation_deg")
            if not all(
                isinstance(value, (int, float)) and not isinstance(value, bool)
                for value in (source_rotation, deskew, inverse, expected_rotation)
            ):
                errors.append(f"{item}.panel_transform: 회전 기록이 숫자여야 해요")
            else:
                if abs(float(source_rotation) - float(expected_rotation)) > 1e-6:
                    errors.append(
                        f"{item}.panel_transform.source_rotation_deg: 영역 회전과 달라요"
                    )
                if abs(float(deskew) - float(expected_rotation)) > 1e-6:
                    errors.append(
                        f"{item}.panel_transform.deskew_rotation_deg: 원문 역회전과 달라요"
                    )
                if abs(float(inverse) + float(expected_rotation)) > 1e-6:
                    errors.append(
                        f"{item}.panel_transform.inverse_rotation_deg: 정확한 역변환이 아니에요"
                    )
            if transform.get("selected_lettering_restored_to_source") is not True:
                errors.append(
                    f"{item}.panel_transform.selected_lettering_restored_to_source: true여야 해요"
                )
            for field in ("source_texture_resampled", "final_texture_resampled"):
                if transform.get(field) is not False:
                    errors.append(f"{item}.panel_transform.{field}: false여야 해요")
        errors.extend(
            _validate_typography_signature(
                region.get("source_typography"), f"{item}.source_typography"
            )
        )
        errors.extend(
            _validate_typography_signature(
                region.get("result_typography"), f"{item}.result_typography"
            )
        )
        typography_checks = region.get("typography_checks")
        if not isinstance(typography_checks, dict):
            errors.append(f"{item}.typography_checks: 객체가 없어요")
        else:
            for field in TYPOGRAPHY_BOOLEAN_CHECKS:
                if typography_checks.get(field) is not True:
                    errors.append(f"{item}.typography_checks.{field}: true여야 해요")
            for field in (
                "ink_height_delta_ratio",
                "ink_width_delta_ratio",
                "bbox_coverage_delta_ratio",
            ):
                value = typography_checks.get(field)
                if (
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or value < 0
                    or value > 0.10
                ):
                    errors.append(f"{item}.typography_checks.{field}: 0~0.10이어야 해요")
            rotation_delta = typography_checks.get("rotation_delta_deg")
            if (
                not isinstance(rotation_delta, (int, float))
                or isinstance(rotation_delta, bool)
                or abs(float(rotation_delta)) > 2.0
            ):
                errors.append(
                    f"{item}.typography_checks.rotation_delta_deg: 절댓값 2.0 이하여야 해요"
                )
        for field in (
            "source_style_reference",
            "generated_panel",
            "selected_lettering",
            "lettering_mask",
        ):
            errors.extend(_validate_artifact(region.get(field), f"{item}.{field}", project_root))

    missing = sorted(set(expected) - seen)
    if missing:
        errors.append(f"{location}.regions: 번역 영역이 누락됐어요: {missing}")
    return errors


def _validate_masks(
    data: Any,
    source: Any,
    translations: Any,
    project_root: Path | None,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["stages.edit_plan.data: 객체가 아니에요"]
    masks = data.get("masks")
    if not isinstance(masks, dict):
        return ["stages.edit_plan.data.masks: 객체가 아니에요"]
    source_width = source.get("width") if isinstance(source, dict) else None
    source_height = source.get("height") if isinstance(source, dict) else None
    for name in ("old_text", "new_text", "editable", "protected", "seam_guard"):
        mask = masks.get(name)
        location = f"stages.edit_plan.data.masks.{name}"
        if not isinstance(mask, dict):
            errors.append(f"{location}: 객체가 없어요")
            continue
        if not _valid_sha256(mask.get("sha256")):
            errors.append(f"{location}.sha256: SHA-256 형식이 아니에요")
        if mask.get("width") != source_width or mask.get("height") != source_height:
            errors.append(f"{location}: 원본 크기와 달라요")
        if project_root is not None:
            path, path_errors = _project_file(project_root, mask.get("path"), f"{location}.path")
            errors.extend(path_errors)
            if path is not None:
                if not path.is_file():
                    errors.append(f"{location}.path: 파일이 없어요: {path}")
                elif _valid_sha256(mask.get("sha256")) and _sha256(path) != mask["sha256"]:
                    errors.append(f"{location}: 현재 마스크 SHA가 기록과 달라요")
    errors.extend(_validate_lettering(data.get("compositor"), translations, project_root))
    return errors


def _validate_candidate_metrics(data: Any) -> list[str]:
    if not isinstance(data, dict):
        return ["stages.candidate_validation.data: 객체가 아니에요"]
    expected = {
        "resized": False,
        "alpha_equal": True,
        "changed_outside_editable": 0,
        "changed_inside_protected": 0,
        "changed_inside_seam_guard": 0,
    }
    return [
        f"stages.candidate_validation.data.{field}: {wanted!r}여야 해요"
        for field, wanted in expected.items()
        if data.get(field) != wanted
    ]


def _validate_post_checks(stages: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    post_ocr = _stage_data(stages, "post_ocr")
    for field, wanted in (
        ("forbidden_foreign_detected", False),
        ("expected_text_matched", True),
        ("duplicate_text_detected", False),
        ("match_mode", "nfc-literal"),
        ("oriented_region_ocr_complete", True),
    ):
        if post_ocr.get(field) != wanted:
            errors.append(f"stages.post_ocr.data.{field}: {wanted!r}여야 해요")
    for field in ("candidate_sha256", "engine_signature"):
        value = post_ocr.get(field)
        if field.endswith("sha256"):
            if not _valid_sha256(value):
                errors.append(f"stages.post_ocr.data.{field}: SHA-256 형식이 아니에요")
        elif not _nonempty_string(value):
            errors.append(f"stages.post_ocr.data.{field}: 비어 있어요")

    post_visual = _stage_data(stages, "post_visual")
    for field, wanted in (
        ("translation_matched", True),
        ("text_orientation_matched", True),
        ("artwork_orientation_matched", True),
        ("color_preserved", True),
        ("sharpness_passed", True),
        ("seams_preserved", True),
        ("font_character_matched", True),
        ("lettering_style_matched", True),
        ("lettering_shape_matched", True),
        ("lettering_size_matched", True),
        ("lettering_bbox_coverage_matched", True),
        ("lettering_alignment_matched", True),
        ("lettering_direction_matched", True),
        ("lettering_spacing_matched", True),
        ("lettering_rotation_matched", True),
        ("lettering_effects_matched", True),
        ("surface_integration_matched", True),
        ("old_logo_silhouette_absent", True),
    ):
        if post_visual.get(field) != wanted:
            errors.append(f"stages.post_visual.data.{field}: {wanted!r}여야 해요")
    if post_ocr.get("requires_visual_resolution") is True and post_visual.get(
        "ocr_ambiguities_resolved"
    ) is not True:
        errors.append(
            "stages.post_visual.data.ocr_ambiguities_resolved: OCR 보류 항목을 시각 확인해야 해요"
        )
    if not _valid_sha256(post_visual.get("candidate_sha256")):
        errors.append("stages.post_visual.data.candidate_sha256: SHA-256 형식이 아니에요")
    if (
        _valid_sha256(post_ocr.get("candidate_sha256"))
        and _valid_sha256(post_visual.get("candidate_sha256"))
        and post_ocr["candidate_sha256"] != post_visual["candidate_sha256"]
    ):
        errors.append("post_ocr와 post_visual이 서로 다른 후보를 검사했어요")
    return errors


def _numeric_pair(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and all(
            isinstance(item, (int, float)) and not isinstance(item, bool)
            and math.isfinite(float(item))
            for item in value
        )
    )


def _master_lettering(stages: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
    edit_plan = stages.get("edit_plan")
    if not isinstance(edit_plan, dict):
        return {}
    data = edit_plan.get("data")
    if not isinstance(data, dict):
        return {}
    compositor = data.get("compositor", {})
    regions = compositor.get("regions") if isinstance(compositor, dict) else None
    if not isinstance(regions, list):
        return {}
    result: dict[str, tuple[Any, Any]] = {}
    for region in regions:
        if not isinstance(region, dict) or not _nonempty_string(region.get("region_id")):
            continue
        selected = region.get("selected_lettering")
        mask = region.get("lettering_mask")
        result[str(region["region_id"])] = (
            selected.get("sha256") if isinstance(selected, dict) else None,
            mask.get("sha256") if isinstance(mask, dict) else None,
        )
    return result


def _neutralized_base_fingerprint(
    item: dict[str, Any],
    descriptor: dict[str, Any],
) -> str:
    source_map = item.get("source_map")
    payload = {
        "mode": "neutralized-base-v1",
        "identity": item.get("identity"),
        "source_map_sha256": (
            source_map.get("sha256") if isinstance(source_map, dict) else None
        ),
        "source_effect_mask_sha256": descriptor.get("sha256"),
        "method": descriptor.get("method"),
        "patch_sha256": descriptor.get("patch_sha256"),
        "channel_contract": item.get("channel_contract"),
        "neutralization_signature": item.get("neutralization_signature"),
    }
    packed = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(packed.encode("utf-8")).hexdigest()


def _validate_effect_measurement(
    descriptor: Any,
    location: str,
    project_root: Path | None,
    *,
    role: Any,
    parameters: Any,
    contract_map: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    if project_root is None or not isinstance(descriptor, dict) or not isinstance(
        parameters, dict
    ):
        return errors
    path, path_errors = _project_file(project_root, descriptor.get("path"), f"{location}.path")
    if path_errors or path is None or not path.is_file():
        return errors
    try:
        measurement = _read_json(path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return [f"{location}: UTF-8 JSON이어야 해요"]
    source_map = contract_map.get("source_map")
    expected = {
        "schema_version": 1,
        "role": role,
        "method": (
            "controlled-lighting-fit" if role == "normal" else "source-effect-sampling"
        ),
        "source_map_sha256": (
            source_map.get("sha256") if isinstance(source_map, dict) else None
        ),
        "source_effect_mask_sha256": contract_map.get("source_effect_mask_sha256"),
        "measured_parameters": parameters,
    }
    if not isinstance(measurement, dict) or any(
        measurement.get(field) != value for field, value in expected.items()
    ):
        errors.append(f"{location}: source/effect_parameters 계약과 내용이 달라요")
        return errors
    sample_count = measurement.get("sample_count")
    if not isinstance(sample_count, int) or isinstance(sample_count, bool) or sample_count < 1:
        errors.append(f"{location}.sample_count: 1 이상이어야 해요")
    return errors


def _validate_material_metrics(
    data: Any,
    stages: dict[str, Any],
    project_root: Path | None,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["stages.material_validation.data: 객체가 아니에요"]
    base_location = "stages.material_validation.data"
    graph_scope = data.get("graph_scope")
    bindings = data.get("bindings")
    if not isinstance(bindings, list) or not bindings:
        errors.append(f"{base_location}.bindings: 비어 있지 않은 배열이어야 해요")
    else:
        for index, binding in enumerate(bindings):
            location = f"{base_location}.bindings[{index}]"
            if not isinstance(binding, dict):
                errors.append(f"{location}: 객체가 아니에요")
                continue
            for field in (
                "material_bundle_key",
                "material_assets_file",
                "material",
                "property",
                "texture_bundle_key",
                "texture",
            ):
                if not _nonempty_string(binding.get(field)):
                    errors.append(f"{location}.{field}: 비어 있어요")
            path_id = binding.get("path_id")
            if not isinstance(path_id, int) or isinstance(path_id, bool) or path_id == 0:
                errors.append(f"{location}.path_id: 0이 아닌 정수여야 해요")
            material_path_id = binding.get("material_path_id")
            if (
                not isinstance(material_path_id, int)
                or isinstance(material_path_id, bool)
                or material_path_id == 0
            ):
                errors.append(
                    f"{location}.material_path_id: 0이 아닌 정수여야 해요"
                )
            for field in ("scale", "offset"):
                if not _numeric_pair(binding.get(field)):
                    errors.append(f"{location}.{field}: 숫자 두 개 배열이어야 해요")
    if graph_scope != "resolved":
        errors.append(f"{base_location}.graph_scope: resolved여야 해요")
    expected = {"shared_consumers_resolved": True}
    for field, wanted in expected.items():
        if data.get(field) != wanted:
            errors.append(f"{base_location}.{field}: {wanted!r}여야 해요")
    text_mask_sha = data.get("text_mask_sha256")
    if not _valid_sha256(text_mask_sha):
        errors.append(f"{base_location}.text_mask_sha256: SHA-256 형식이 아니에요")
    edit_masks = _stage_data(stages, "edit_plan").get("masks", {})
    expected_text_mask_sha = (
        edit_masks.get("new_text", {}).get("sha256")
        if isinstance(edit_masks, dict)
        else None
    )
    if _valid_sha256(expected_text_mask_sha) and text_mask_sha != expected_text_mask_sha:
        errors.append(f"{base_location}.text_mask_sha256: 승인 new_text 마스크와 달라요")

    policies = data.get("policies")
    material_masks = data.get("material_masks")
    shared_consumers = data.get("shared_consumers")
    if not isinstance(policies, dict) or not policies:
        errors.append(f"{base_location}.policies: 비어 있지 않은 객체여야 해요")
    else:
        if not isinstance(shared_consumers, dict) or set(shared_consumers) != set(policies):
            errors.append(f"{base_location}.shared_consumers: policies와 같은 맵 키가 필요해요")
        for key, policy in policies.items():
            if not _nonempty_string(key) or policy not in MATERIAL_POLICIES:
                errors.append(f"{base_location}.policies.{key}: 지원하지 않는 정책이에요")
                continue
            if policy == "preserve":
                continue
            descriptor = material_masks.get(key) if isinstance(material_masks, dict) else None
            if not isinstance(descriptor, dict):
                errors.append(f"{base_location}.material_masks.{key}: 마스크가 없어요")
                continue
            errors.extend(
                _validate_artifact(
                    descriptor,
                    f"{base_location}.material_masks.{key}",
                    project_root,
                )
            )
            method = descriptor.get("method")
            if method not in {"inpaint", "patch"}:
                errors.append(
                    f"{base_location}.material_masks.{key}.method: "
                    "inpaint 또는 patch여야 해요"
                )
            if method == "patch":
                errors.extend(
                    _validate_artifact(
                        {
                            "path": descriptor.get("patch"),
                            "sha256": descriptor.get("patch_sha256"),
                        },
                        f"{base_location}.material_masks.{key}.patch",
                        project_root,
                    )
                )

        if isinstance(shared_consumers, dict):
            for key, consumers in shared_consumers.items():
                location = f"{base_location}.shared_consumers.{key}"
                if not isinstance(consumers, list) or not consumers:
                    errors.append(f"{location}: 비어 있지 않은 배열이어야 해요")
                    continue
                for index, consumer in enumerate(consumers):
                    item = f"{location}[{index}]"
                    if not isinstance(consumer, dict):
                        errors.append(f"{item}: 객체가 아니에요")
                        continue
                    for field in (
                        "material",
                        "material_bundle_key",
                        "material_assets_file",
                        "property",
                    ):
                        if not _nonempty_string(consumer.get(field)):
                            errors.append(f"{item}.{field}: 비어 있어요")
                    material_path_id = consumer.get("material_path_id")
                    if (
                        not isinstance(material_path_id, int)
                        or isinstance(material_path_id, bool)
                        or material_path_id == 0
                    ):
                        errors.append(f"{item}.material_path_id: 0이 아닌 정수여야 해요")
                    for field in ("scale", "offset"):
                        if not _numeric_pair(consumer.get(field)):
                            errors.append(f"{item}.{field}: 숫자 두 개 배열이어야 해요")

    contract = data.get("auxiliary_contract")
    contract_location = f"{base_location}.auxiliary_contract"
    if not isinstance(contract, dict):
        return errors + [
            f"{contract_location}: v1 source-base 계약이 없어요. 기존 재질 기록을 다시 검토해야 해요"
        ]
    for field, wanted in (
        ("schema_version", 1),
        ("mode", "source-base+master-lettering-alpha-v1"),
        ("master_geometry", "selected-lettering-continuous-alpha"),
        ("whole_map_generation_used", False),
        ("binary_new_text_resampled", False),
        ("source_maps_immutable_outside_effect_masks", True),
    ):
        if contract.get(field) != wanted:
            errors.append(f"{contract_location}.{field}: {wanted!r}여야 해요")

    expected_master = _master_lettering(stages)
    master_records = contract.get("master_lettering")
    recorded_master: dict[str, tuple[Any, Any]] = {}
    if not isinstance(master_records, list):
        errors.append(f"{contract_location}.master_lettering: 배열이어야 해요")
    else:
        for index, item in enumerate(master_records):
            location = f"{contract_location}.master_lettering[{index}]"
            if not isinstance(item, dict):
                errors.append(f"{location}: 객체가 아니에요")
                continue
            region_id = item.get("region_id")
            if not _nonempty_string(region_id):
                errors.append(f"{location}.region_id: 비어 있어요")
                continue
            region_id = str(region_id)
            if region_id in recorded_master:
                errors.append(f"{location}.region_id: 중복됐어요")
            selected_sha = item.get("selected_lettering_sha256")
            mask_sha = item.get("lettering_mask_sha256")
            if not _valid_sha256(selected_sha):
                errors.append(f"{location}.selected_lettering_sha256: SHA-256 형식이 아니에요")
            if not _valid_sha256(mask_sha):
                errors.append(f"{location}.lettering_mask_sha256: SHA-256 형식이 아니에요")
            recorded_master[region_id] = (selected_sha, mask_sha)
        if recorded_master != expected_master:
            errors.append(
                f"{contract_location}.master_lettering: 승인된 selected_lettering/lettering_mask와 달라요"
            )

    maps = contract.get("maps")
    if not isinstance(maps, dict) or not maps:
        errors.append(f"{contract_location}.maps: 비어 있지 않은 객체여야 해요")
        return errors
    policy_keys = set(policies) if isinstance(policies, dict) else set()
    map_keys = set(maps)
    if map_keys != policy_keys:
        errors.append(f"{contract_location}.maps: policies와 같은 맵 키를 가져야 해요")

    for key, item in maps.items():
        location = f"{contract_location}.maps.{key}"
        if not isinstance(item, dict):
            errors.append(f"{location}: 객체가 아니에요")
            continue
        policy = item.get("policy")
        if policy != (policies.get(key) if isinstance(policies, dict) else None):
            errors.append(f"{location}.policy: policies의 값과 달라요")
        identity = item.get("identity")
        identity_location = f"{location}.identity"
        if not isinstance(identity, dict):
            errors.append(f"{identity_location}: 객체가 없어요")
        else:
            for field in ("texture_bundle_key", "texture"):
                if not _nonempty_string(identity.get(field)):
                    errors.append(f"{identity_location}.{field}: 비어 있어요")
            texture_format = identity.get("format")
            if (
                not isinstance(texture_format, int)
                or isinstance(texture_format, bool)
                or texture_format < 0
            ):
                errors.append(f"{identity_location}.format: 0 이상의 정수여야 해요")
            path_id = identity.get("path_id")
            if not isinstance(path_id, int) or isinstance(path_id, bool) or path_id == 0:
                errors.append(f"{identity_location}.path_id: 0이 아닌 정수여야 해요")
            if identity.get("role") not in MATERIAL_ROLES:
                errors.append(f"{identity_location}.role: normal 또는 gloss여야 해요")
            for field in ("width", "height"):
                value = identity.get(field)
                if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                    errors.append(f"{identity_location}.{field}: 1 이상의 정수여야 해요")
            for field in ("uv_scale", "uv_offset"):
                if not _numeric_pair(identity.get(field)):
                    errors.append(f"{identity_location}.{field}: 숫자 두 개 배열이어야 해요")
            material_name, separator, property_name = str(key).rpartition("::")
            matching_bindings = [
                binding
                for binding in bindings
                if isinstance(binding, dict)
                and binding.get("material") == material_name
                and binding.get("property") == property_name
            ] if isinstance(bindings, list) and separator else []
            if not matching_bindings:
                errors.append(f"{location}: 같은 material/property binding이 없어요")
            else:
                expected_role = (
                    "normal"
                    if property_name in MATERIAL_NORMAL_PROPERTIES
                    else "gloss"
                    if property_name in MATERIAL_GLOSS_PROPERTIES
                    else None
                )
                for binding in matching_bindings:
                    comparisons = {
                        "texture_bundle_key": binding.get("texture_bundle_key"),
                        "path_id": binding.get("path_id"),
                        "texture": binding.get("texture"),
                        "role": expected_role,
                        "uv_scale": binding.get("scale"),
                        "uv_offset": binding.get("offset"),
                    }
                    for field, expected_value in comparisons.items():
                        if identity.get(field) != expected_value:
                            errors.append(
                                f"{identity_location}.{field}: binding과 달라요"
                            )
        errors.extend(
            _validate_artifact(item.get("source_map"), f"{location}.source_map", project_root)
        )
        if item.get("whole_map_generated") is not False:
            errors.append(f"{location}.whole_map_generated: false여야 해요")
        if policy == "preserve" and not isinstance(
            item.get("shared_effect_compatible"), bool
        ):
            errors.append(f"{location}.shared_effect_compatible: boolean이어야 해요")
        elif policy != "preserve" and item.get("shared_effect_compatible") is not True:
            errors.append(f"{location}.shared_effect_compatible: true여야 해요")

        if policy == "preserve":
            if item.get("effect_kind") != "none":
                errors.append(f"{location}.effect_kind: preserve는 'none'이어야 해요")
            continue

        channel = item.get("channel_contract")
        channel_location = f"{location}.channel_contract"
        if not isinstance(channel, dict):
            errors.append(f"{channel_location}: 객체가 없어요")
        else:
            if channel.get("semantics_verified") is not True:
                errors.append(f"{channel_location}.semantics_verified: true여야 해요")
            if channel.get("verification_method") not in {
                "shader-reflection",
                "controlled-render",
                "source-asset-analysis",
            }:
                errors.append(f"{channel_location}.verification_method: 지원하지 않는 방식이에요")
            errors.extend(
                _validate_artifact(
                    channel.get("evidence"),
                    f"{channel_location}.evidence",
                    project_root,
                )
            )
            if not _nonempty_string(channel.get("packing")):
                errors.append(f"{channel_location}.packing: 비어 있어요")
            used_channels = channel.get("used_channels")
            if (
                not isinstance(used_channels, list)
                or not used_channels
                or len(set(used_channels)) != len(used_channels)
                or any(value not in {"R", "G", "B", "A"} for value in used_channels)
            ):
                errors.append(f"{channel_location}.used_channels: 고유한 RGBA 채널 배열이어야 해요")
            if isinstance(identity, dict) and identity.get("role") == "normal":
                if (
                    identity.get("format") != 12
                    or channel.get("packing") != "dxt5nm-x-a-y-g"
                    or used_channels != ["G", "A"]
                ):
                    errors.append(
                        f"{channel_location}: Normal은 DXT5(12) DXT5nm의 G/A만 사용해야 해요"
                    )
            if isinstance(identity, dict) and identity.get("role") == "gloss":
                texture_format = identity.get("format")
                if texture_format not in {10, 12}:
                    errors.append(
                        f"{channel_location}: Gloss v1은 DXT1(10)/DXT5(12)만 지원해요"
                    )
                elif texture_format == 10 and isinstance(used_channels, list) and "A" in used_channels:
                    errors.append(
                        f"{channel_location}: DXT1 Gloss는 RGB 채널만 수정해야 해요"
                    )
            if channel.get("linear_data") is not True:
                errors.append(f"{channel_location}.linear_data: true여야 해요")
        descriptor = material_masks.get(key) if isinstance(material_masks, dict) else None
        mask_sha = descriptor.get("sha256") if isinstance(descriptor, dict) else None
        if item.get("source_effect_mask_sha256") != mask_sha or not _valid_sha256(mask_sha):
            errors.append(f"{location}.source_effect_mask_sha256: 재질 old-effect 마스크와 달라요")
        if not _nonempty_string(item.get("neutralization_signature")):
            errors.append(f"{location}.neutralization_signature: 비어 있어요")
        elif isinstance(descriptor, dict) and isinstance(identity, dict):
            if descriptor.get("method") == "patch":
                expected_signature = "patch-copy:v1"
            else:
                width = identity.get("width")
                height = identity.get("height")
                if all(
                    isinstance(value, int) and not isinstance(value, bool) and value > 0
                    for value in (width, height)
                ):
                    radius = max(1, round(min(width, height) / 512 * 3))
                    expected_signature = f"opencv-telea:v1:radius={radius}"
                else:
                    expected_signature = None
            if (
                expected_signature is not None
                and item.get("neutralization_signature") != expected_signature
            ):
                errors.append(
                    f"{location}.neutralization_signature: 현재 producer와 달라요"
                )
        fingerprint = item.get("base_cache_fingerprint")
        if not _valid_sha256(fingerprint):
            errors.append(f"{location}.base_cache_fingerprint: SHA-256 형식이 아니에요")
        elif isinstance(descriptor, dict) and fingerprint != _neutralized_base_fingerprint(
            item, descriptor
        ):
            errors.append(f"{location}.base_cache_fingerprint: 현재 입력 계약과 달라요")

        if policy == "neutralize_old_text":
            if item.get("effect_kind") != "remove-only":
                errors.append(f"{location}.effect_kind: 'remove-only'여야 해요")
            continue
        if policy != "neutralize_and_derive":
            continue

        role = identity.get("role") if isinstance(identity, dict) else None
        expected_effect = (
            "master-alpha-relief" if role == "normal" else "master-alpha-gloss"
        )
        expected_producer = (
            MATERIAL_NORMAL_SIGNATURE if role == "normal" else MATERIAL_GLOSS_SIGNATURE
        )
        if item.get("effect_kind") != expected_effect:
            errors.append(f"{location}.effect_kind: {expected_effect!r}여야 해요")
        derivation = item.get("derivation")
        derivation_location = f"{location}.derivation"
        if not isinstance(derivation, dict):
            errors.append(f"{derivation_location}: v1 파생 계약이 없어요")
            continue
        if derivation.get("schema_version") != 1:
            errors.append(f"{derivation_location}.schema_version: 1이어야 해요")
        if derivation.get("producer") != expected_producer:
            errors.append(f"{derivation_location}.producer: 현재 역할 producer와 달라요")
        if derivation.get("physical_component") != "all-selected-lettering-alpha":
            errors.append(
                f"{derivation_location}.physical_component: 전체 lettering alpha의 물리 효과 검증이 필요해요"
            )
        expected_region_ids = sorted(expected_master)
        if derivation.get("master_region_ids") != expected_region_ids:
            errors.append(
                f"{derivation_location}.master_region_ids: 승인된 master 영역 전체와 달라요"
            )
        errors.extend(
            _validate_artifact(
                derivation.get("effect_measurement"),
                f"{derivation_location}.effect_measurement",
                project_root,
            )
        )
        if derivation.get("alignment_limits") != {
            "center_error_texels": 0.5,
            "bbox_edge_error_texels": 1.0,
            "rotation_error_deg": 0.0,
        }:
            errors.append(f"{derivation_location}.alignment_limits: 안전 허용치와 달라요")

        projection = derivation.get("projection")
        projection_location = f"{derivation_location}.projection"
        projection_keys = {
            "signature",
            "source_size",
            "target_size",
            "diffuse_uv_scale",
            "diffuse_uv_offset",
            "auxiliary_uv_scale",
            "auxiliary_uv_offset",
            "v_axis",
            "texel_center_sampling",
        }
        if not isinstance(projection, dict) or set(projection) != projection_keys:
            errors.append(f"{projection_location}: 필수 투영 계약 필드와 정확히 일치해야 해요")
        else:
            if projection.get("signature") != MATERIAL_PROJECTION_SIGNATURE:
                errors.append(f"{projection_location}.signature: 현재 producer와 달라요")
            source_size = projection.get("source_size")
            target_size = projection.get("target_size")
            for field, value in (("source_size", source_size), ("target_size", target_size)):
                if not (
                    isinstance(value, list)
                    and len(value) == 2
                    and all(
                        isinstance(part, int) and not isinstance(part, bool) and part > 0
                        for part in value
                    )
                ):
                    errors.append(f"{projection_location}.{field}: 양의 정수 [width,height]여야 해요")
            if isinstance(identity, dict) and target_size != [
                identity.get("width"),
                identity.get("height"),
            ]:
                errors.append(f"{projection_location}.target_size: 보조맵 identity와 달라요")
            if all(
                isinstance(value, list)
                and len(value) == 2
                    and all(
                        isinstance(part, int) and not isinstance(part, bool) and part > 0
                        for part in value
                    )
                for value in (source_size, target_size)
            ):
                source_width, source_height = source_size
                target_width, target_height = target_size
                if source_width % target_width or source_height % target_height:
                    errors.append(f"{projection_location}: source/target이 정수 축소 관계가 아니에요")
                else:
                    factor_x = source_width // target_width
                    factor_y = source_height // target_height
                    if factor_x != factor_y or factor_x < 1 or factor_x & (factor_x - 1):
                        errors.append(f"{projection_location}: 2^n 동일 종횡비 축소가 아니에요")
            for field in (
                "diffuse_uv_scale",
                "diffuse_uv_offset",
                "auxiliary_uv_scale",
                "auxiliary_uv_offset",
            ):
                if not _numeric_pair(projection.get(field)):
                    errors.append(f"{projection_location}.{field}: 유한수 두 개 배열이어야 해요")
            if projection.get("auxiliary_uv_scale") != (
                identity.get("uv_scale") if isinstance(identity, dict) else None
            ) or projection.get("auxiliary_uv_offset") != (
                identity.get("uv_offset") if isinstance(identity, dict) else None
            ):
                errors.append(f"{projection_location}: auxiliary UV ST가 identity와 달라요")
            diffuse_candidates = [
                binding
                for binding in bindings
                if isinstance(binding, dict)
                and binding.get("material") == str(key).rpartition("::")[0]
                and binding.get("property") in MATERIAL_DIFFUSE_PROPERTIES
            ] if isinstance(bindings, list) else []
            diffuse_st = {
                (
                    tuple(binding.get("scale", [])),
                    tuple(binding.get("offset", [])),
                )
                for binding in diffuse_candidates
            }
            if len(diffuse_candidates) < 1 or len(diffuse_st) != 1:
                errors.append(f"{projection_location}: 같은 Material의 Diffuse UV ST가 모호해요")
            else:
                diffuse_binding = diffuse_candidates[0]
                if (
                    projection.get("diffuse_uv_scale") != diffuse_binding.get("scale")
                    or projection.get("diffuse_uv_offset")
                    != diffuse_binding.get("offset")
                ):
                    errors.append(f"{projection_location}: Diffuse UV ST가 binding과 달라요")
            if (
                projection.get("diffuse_uv_scale") != projection.get("auxiliary_uv_scale")
                or projection.get("diffuse_uv_offset") != projection.get("auxiliary_uv_offset")
            ):
                errors.append(f"{projection_location}: v1은 Diffuse/보조맵 ST가 같아야 해요")
            diffuse_scale = projection.get("diffuse_uv_scale")
            if _numeric_pair(diffuse_scale) and (
                diffuse_scale[0] <= 0 or diffuse_scale[1] <= 0
            ):
                errors.append(
                    f"{projection_location}.diffuse_uv_scale: v1은 양수 scale만 지원해요"
                )
            if projection.get("v_axis") != "png-top-left+unity-v-up":
                errors.append(f"{projection_location}.v_axis: 좌표계 계약이 달라요")
            if projection.get("texel_center_sampling") is not True:
                errors.append(f"{projection_location}.texel_center_sampling: true여야 해요")

        parameters = derivation.get("effect_parameters")
        parameter_location = f"{derivation_location}.effect_parameters"
        if not isinstance(parameters, dict):
            errors.append(f"{parameter_location}: 객체가 없어요")
        elif role == "normal":
            if set(parameters) != {"height_scale_texels", "polarity", "bevel_passes"}:
                errors.append(f"{parameter_location}: Normal 파라미터 필드가 달라요")
            height = parameters.get("height_scale_texels")
            if not (
                isinstance(height, (int, float))
                and not isinstance(height, bool)
                and math.isfinite(float(height))
                and 0 < float(height) <= 8
            ):
                errors.append(f"{parameter_location}.height_scale_texels: 0 초과 8 이하여야 해요")
            if parameters.get("polarity") not in {-1, 1}:
                errors.append(f"{parameter_location}.polarity: -1 또는 1이어야 해요")
            passes = parameters.get("bevel_passes")
            if not isinstance(passes, int) or isinstance(passes, bool) or not 0 <= passes <= 8:
                errors.append(f"{parameter_location}.bevel_passes: 0~8 정수여야 해요")
        else:
            deltas = parameters.get("channel_deltas")
            used_channels = channel.get("used_channels") if isinstance(channel, dict) else None
            if set(parameters) != {"channel_deltas"} or not isinstance(deltas, dict):
                errors.append(f"{parameter_location}.channel_deltas: 객체가 없어요")
            elif list(deltas) != used_channels:
                errors.append(f"{parameter_location}.channel_deltas: used_channels와 달라요")
            else:
                numeric_deltas = [
                    value
                    for value in deltas.values()
                    if isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and math.isfinite(float(value))
                    and -255 <= float(value) <= 255
                ]
                if len(numeric_deltas) != len(deltas) or not any(
                    float(value) != 0 for value in numeric_deltas
                ):
                    errors.append(
                        f"{parameter_location}.channel_deltas: "
                        "-255~255의 non-zero 유한수가 필요해요"
                    )
        errors.extend(
            _validate_effect_measurement(
                derivation.get("effect_measurement"),
                f"{derivation_location}.effect_measurement",
                project_root,
                role=role,
                parameters=parameters,
                contract_map=item,
            )
        )
    return errors


def _validate_release_metrics(stages: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    mip = _stage_data(stages, "mip_validation")
    if not isinstance(mip.get("checked_mips"), list) or not mip["checked_mips"]:
        errors.append("stages.mip_validation.data.checked_mips: 검사한 밉 목록이 없어요")
    if mip.get("missing_mips") != 0:
        errors.append("stages.mip_validation.data.missing_mips: 0이어야 해요")
    bundle = _stage_data(stages, "bundle_validation")
    for field in ("layout_equal", "bytes_equal_outside_payloads", "roundtrip_passed"):
        if bundle.get(field) is not True:
            errors.append(f"stages.bundle_validation.data.{field}: true여야 해요")
    runtime = _stage_data(stages, "runtime_validation")
    if not isinstance(runtime.get("capture_matrix"), list) or not runtime["capture_matrix"]:
        errors.append("stages.runtime_validation.data.capture_matrix: 실제 렌더 목록이 없어요")
    for field, wanted in (
        ("foreign_text_detected", False),
        ("alignment_passed", True),
        ("seam_passed", True),
    ):
        if runtime.get(field) != wanted:
            errors.append(f"stages.runtime_validation.data.{field}: {wanted!r}여야 해요")
    release = _stage_data(stages, "release_validation")
    for field in ("input_hashes", "report_hashes", "bundle_hashes"):
        values = release.get(field)
        if not isinstance(values, dict) or not values:
            errors.append(f"stages.release_validation.data.{field}: 비어 있지 않은 객체여야 해요")
        elif not all(_nonempty_string(key) and _valid_sha256(value) for key, value in values.items()):
            errors.append(f"stages.release_validation.data.{field}: 모든 값이 SHA-256이어야 해요")
    return errors


def validate_record(
    record: Any,
    through: str,
    *,
    project_root: Path | None = None,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(record, dict):
        return ["record: JSON 객체가 아니에요"]
    if record.get("schema_version") != 1:
        errors.append("schema_version: 1이어야 해요")
    if not _nonempty_string(record.get("target_id")):
        errors.append("target_id: 비어 있어요")
    if record.get("action") not in {"localize", "preserve"}:
        errors.append("action: localize 또는 preserve여야 해요")
    expected_text = record.get("expected_text")
    if not isinstance(expected_text, list) or not all(_nonempty_string(value) for value in expected_text):
        errors.append("expected_text: 비어 있지 않은 문자열 배열이어야 해요")

    source = record.get("source")
    if not isinstance(source, dict):
        errors.append("source: 객체가 아니에요")
    else:
        for field in (
            "bundle_key",
            "texture",
            "image",
            "color_mode",
            "texture_orientation",
            "artwork_direction",
        ):
            if not _nonempty_string(source.get(field)):
                errors.append(f"source.{field}: 비어 있어요")
        if not _valid_sha256(source.get("sha256")):
            errors.append("source.sha256: SHA-256 형식이 아니에요")
        for field in ("width", "height"):
            value = source.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                errors.append(f"source.{field}: 1 이상의 정수가 아니에요")
        if project_root is not None:
            source_path, path_errors = _project_file(project_root, source.get("image"), "source.image")
            errors.extend(path_errors)
            if source_path is not None:
                if not source_path.is_file():
                    errors.append(f"source.image: 파일이 없어요: {source_path}")
                elif _valid_sha256(source.get("sha256")) and _sha256(source_path) != source["sha256"]:
                    errors.append("source.sha256: 현재 원본 파일과 달라요")

    unresolved = record.get("unresolved")
    if not isinstance(unresolved, list):
        errors.append("unresolved: 배열이 아니에요")
    elif unresolved:
        errors.append(f"unresolved: 미해결 항목 {len(unresolved)}개가 남아 있어요")

    stages = record.get("stages")
    if not isinstance(stages, dict):
        return errors + ["stages: 객체가 아니에요"]
    scoped_stages = set(THROUGH[through])
    for name in STAGES:
        stage = stages.get(name)
        if not isinstance(stage, dict):
            errors.append(f"stages.{name}: 단계 객체가 없어요")
            continue
        if stage.get("status") not in STATUSES:
            errors.append(f"stages.{name}.status: 지원하는 상태가 아니에요")
        if name in scoped_stages:
            errors.extend(
                _validate_evidence(
                    stage.get("evidence"),
                    f"stages.{name}.evidence",
                    project_root=project_root,
                    required=stage.get("status") == "pass",
                )
            )
        elif not isinstance(stage.get("evidence"), list):
            errors.append(f"stages.{name}.evidence: 배열이 아니에요")
        if not isinstance(stage.get("data"), dict):
            errors.append(f"stages.{name}.data: 객체가 아니에요")
    for name in THROUGH[through]:
        stage = stages.get(name)
        if isinstance(stage, dict) and stage.get("status") != "pass":
            errors.append(f"stages.{name}: {stage.get('status', 'missing')} 상태라 {through} 게이트를 통과할 수 없어요")

    if through in {"analysis", "candidate", "material", "release"}:
        visual_data = _stage_data(stages, "source_visual")
        visual_regions = visual_data.get("regions")
        translations = _stage_data(stages, "translation").get("regions")
        if visual_data.get("vision_first") is not True:
            errors.append("source_visual.data.vision_first: true여야 해요")
        fallback_required = visual_data.get("ocr_fallback_required")
        if not isinstance(fallback_required, bool):
            errors.append("source_visual.data.ocr_fallback_required: boolean이어야 해요")
        for label, values in (
            ("source_visual.data.regions", visual_regions),
            ("translation.data.regions", translations),
        ):
            if not isinstance(values, list):
                errors.append(f"{label}: 배열이 아니에요")
        if isinstance(visual_regions, list):
            for index, region in enumerate(visual_regions):
                errors.extend(
                    _validate_visual_region(region, f"source_visual.data.regions[{index}]")
                )
        if isinstance(translations, list):
            for index, region in enumerate(translations):
                errors.extend(_validate_translation(region, f"translation.data.regions[{index}]"))
            final_texts = {str(region.get("final_text_ko", "")) for region in translations if isinstance(region, dict)}
            if record.get("action") == "localize":
                for expected in expected_text or []:
                    if expected not in final_texts:
                        errors.append(f"translation: profile 확정 문구 {expected!r}가 없어요")
        if record.get("action") == "localize":
            if isinstance(visual_regions, list) and not visual_regions:
                errors.append("source_visual.data.regions: 현지화 대상인데 판독 영역이 없어요")
            if isinstance(translations, list) and not translations:
                errors.append("translation.data.regions: 현지화 대상인데 번역 영역이 없어요")
        fallback_ids: set[str] = set()
        visual_ids: set[str] = set()
        if isinstance(visual_regions, list):
            visual_ids = {
                str(region.get("region_id"))
                for region in visual_regions
                if isinstance(region, dict) and _nonempty_string(region.get("region_id"))
            }
            fallback_ids = {
                str(region.get("region_id"))
                for region in visual_regions
                if isinstance(region, dict)
                and _nonempty_string(region.get("region_id"))
                and region.get("needs_ocr_fallback") is True
            }
        if fallback_ids and fallback_required is not True:
            errors.append("source_visual.data.ocr_fallback_required: 모호한 영역이 있어 true여야 해요")
        if fallback_required is True and not fallback_ids:
            errors.append("source_visual.data.ocr_fallback_required: fallback 대상 영역이 없어요")
        if isinstance(translations, list):
            translation_ids = {
                str(region.get("region_id"))
                for region in translations
                if isinstance(region, dict) and _nonempty_string(region.get("region_id"))
            }
            missing_visual = sorted(translation_ids - visual_ids)
            if missing_visual:
                errors.append(f"translation: 시각 판독에 없는 영역이 있어요: {missing_visual}")

        if fallback_required is True:
            for stage_name in ("source_ocr", "cross_validation"):
                stage = stages.get(stage_name, {})
                if not isinstance(stage, dict):
                    stage = {}
                if stage.get("status") != "pass":
                    errors.append(f"stages.{stage_name}: OCR fallback에 필요하므로 pass여야 해요")
                errors.extend(
                    _validate_evidence(
                        stage.get("evidence"),
                        f"stages.{stage_name}.evidence",
                        project_root=project_root,
                        required=True,
                    )
                )
            ocr_detections = _stage_data(stages, "source_ocr").get("detections")
            cross_data = _stage_data(stages, "cross_validation")
            cross_regions = cross_data.get("regions")
            conflicts = cross_data.get("conflicts")
            if not isinstance(ocr_detections, list) or not ocr_detections:
                errors.append("source_ocr.data.detections: fallback 검출 영역이 없어요")
            else:
                for index, region in enumerate(ocr_detections):
                    errors.extend(
                        _validate_region(
                            region, f"source_ocr.data.detections[{index}]", ocr=True
                        )
                    )
            if not isinstance(cross_regions, list) or not cross_regions:
                errors.append("cross_validation.data.regions: fallback 교차검증 영역이 없어요")
            else:
                for index, region in enumerate(cross_regions):
                    errors.extend(
                        _validate_cross_region(
                            region, f"cross_validation.data.regions[{index}]"
                        )
                    )
                cross_visual_ids = {
                    str(region.get("visual_region_id"))
                    for region in cross_regions
                    if isinstance(region, dict)
                    and _nonempty_string(region.get("visual_region_id"))
                }
                missing_fallback = sorted(fallback_ids - cross_visual_ids)
                if missing_fallback:
                    errors.append(
                        f"cross_validation: fallback 시각 영역이 누락됐어요: {missing_fallback}"
                    )
            if not isinstance(conflicts, list):
                errors.append("cross_validation.data.conflicts: 배열이 아니에요")
            elif conflicts:
                errors.append(
                    f"cross_validation.data.conflicts: 충돌 {len(conflicts)}개가 남아 있어요"
                )

    if through in {"candidate", "material", "release"}:
        if record.get("action") == "localize":
            translations = _stage_data(stages, "translation").get("regions", [])
            errors.extend(
                _validate_masks(
                    _stage_data(stages, "edit_plan"),
                    source,
                    translations,
                    project_root,
                )
            )
        else:
            candidate_data = _stage_data(stages, "candidate_validation")
            if candidate_data.get("rgba_equal") is not True:
                errors.append("보존 대상은 stages.candidate_validation.data.rgba_equal이 true여야 해요")
        errors.extend(_validate_candidate_metrics(_stage_data(stages, "candidate_validation")))
        errors.extend(_validate_post_checks(stages))
    if through in {"material", "release"}:
        errors.extend(
            _validate_material_metrics(
                _stage_data(stages, "material_validation"),
                stages,
                project_root,
            )
        )
    if through == "release":
        errors.extend(_validate_release_metrics(stages))

    return errors


def set_stage(
    project_root: Path,
    record_path: Path,
    stage_name: str,
    status: str,
    data_path: Path | None,
    evidence_paths: list[Path],
    reason: str,
) -> None:
    record = _read_json(record_path)
    stages = record.get("stages")
    if not isinstance(stages, dict) or stage_name not in stages:
        raise ValueError(f"작업 기록에 단계가 없어요: {stage_name}")
    data: dict[str, Any] = {}
    if data_path is not None:
        loaded = _read_json(data_path)
        if not isinstance(loaded, dict):
            raise ValueError("--data JSON은 객체여야 해요")
        data = loaded
    if reason:
        data["reason"] = reason
    evidence: list[dict[str, str]] = []
    for path in evidence_paths:
        resolved = path.expanduser().resolve()
        if not resolved.is_file():
            raise FileNotFoundError(resolved)
        try:
            relative = resolved.relative_to(project_root).as_posix()
        except ValueError as exc:
            raise ValueError(f"증거는 프로젝트 안에 있어야 해요: {resolved}") from exc
        evidence.append({"path": relative, "sha256": _sha256(resolved)})
    stages[stage_name] = {"status": status, "evidence": evidence, "data": data}
    _write_json(record_path, record)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    init = subparsers.add_parser("init", help="profile에서 품목 작업 기록을 만들어요")
    init.add_argument("target_id")
    init.add_argument("--project-root", type=Path)
    init.add_argument("--output", type=Path)
    check = subparsers.add_parser("check", help="작업 기록이 지정 단계까지 완료됐는지 검사해요")
    check.add_argument("record", type=Path)
    check.add_argument("--through", choices=tuple(THROUGH), required=True)
    stage = subparsers.add_parser("stage", help="단계 상태와 해시 고정 증거를 기록해요")
    stage.add_argument("record", type=Path)
    stage.add_argument("stage", choices=STAGES)
    stage.add_argument("--status", choices=tuple(sorted(STATUSES)), required=True)
    stage.add_argument("--data", type=Path)
    stage.add_argument("--evidence", type=Path, action="append", default=[])
    stage.add_argument("--reason", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "init":
            root = (args.project_root or _project_root(Path.cwd())).expanduser().resolve()
            destination = init_record(root, args.target_id, args.output)
            print(json.dumps({"created": str(destination)}, ensure_ascii=False))
            return 0
        record_path = args.record.expanduser().resolve()
        root = _project_root(record_path.parent)
        if args.command == "stage":
            set_stage(
                root,
                record_path,
                args.stage,
                args.status,
                args.data.expanduser().resolve() if args.data else None,
                args.evidence,
                args.reason,
            )
            print(json.dumps({"updated": str(record_path), "stage": args.stage}, ensure_ascii=False))
            return 0
        errors = validate_record(_read_json(record_path), args.through, project_root=root)
        result = {
            "record": str(record_path),
            "through": args.through,
            "passed": not errors,
            "errors": errors,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if not errors else 2
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"passed": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
