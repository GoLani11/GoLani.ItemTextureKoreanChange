#!/usr/bin/env python3
"""품목별 판독·검증 기록을 만들고 단계 누락을 fail-closed로 검사해요."""

from __future__ import annotations

import argparse
import hashlib
import json
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
THROUGH = {
    "analysis": STAGES[:4],
    "candidate": STAGES[:8],
    "material": STAGES[:9],
    "release": STAGES,
}
DIRECTIONS = {"left-to-right", "right-to-left", "top-to-bottom", "bottom-to-top"}
CROSS_RESOLUTIONS = {"matched", "visual_only", "ocr_only_resolved"}


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


def _validate_masks(data: Any, source: Any, project_root: Path | None) -> list[str]:
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
    compositor = data.get("compositor")
    if not isinstance(compositor, dict):
        errors.append("stages.edit_plan.data.compositor: 객체가 없어요")
    else:
        for field in ("mode", "shaping_engine", "shaping_version"):
            if not _nonempty_string(compositor.get(field)):
                errors.append(f"stages.edit_plan.data.compositor.{field}: 비어 있어요")
        for field in ("font_sha256", "glyph_run_sha256"):
            if not _valid_sha256(compositor.get(field)):
                errors.append(f"stages.edit_plan.data.compositor.{field}: SHA-256 형식이 아니에요")
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
    post_ocr = stages.get("post_ocr", {}).get("data", {})
    for field, wanted in (
        ("forbidden_foreign_detected", False),
        ("expected_text_matched", True),
        ("duplicate_text_detected", False),
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

    post_visual = stages.get("post_visual", {}).get("data", {})
    for field, wanted in (
        ("translation_matched", True),
        ("text_orientation_matched", True),
        ("artwork_orientation_matched", True),
        ("color_preserved", True),
        ("sharpness_passed", True),
        ("seams_preserved", True),
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


def _validate_material_metrics(data: Any, project_root: Path) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["stages.material_validation.data: 객체가 아니에요"]
    graph_scope = data.get("graph_scope")
    bindings = data.get("bindings")
    if not isinstance(bindings, list) or not bindings:
        errors.append("stages.material_validation.data.bindings: 배열이 아니에요")
    if graph_scope != "resolved":
        errors.append("stages.material_validation.data.graph_scope: resolved여야 해요")
    expected = {
        "shared_consumers_resolved": True,
        "alignment_passed": True,
        "foreign_relief_detected": False,
        "changed_outside_masks": 0,
    }
    for field, wanted in expected.items():
        if data.get(field) != wanted:
            errors.append(f"stages.material_validation.data.{field}: {wanted!r}여야 해요")
    if not _valid_sha256(data.get("text_mask_sha256")):
        errors.append("stages.material_validation.data.text_mask_sha256: SHA-256 형식이 아니에요")
    policies = data.get("policies")
    material_masks = data.get("material_masks")
    if not isinstance(policies, dict) or not policies:
        errors.append("stages.material_validation.data.policies: 비어 있지 않은 객체여야 해요")
    else:
        for key, policy in policies.items():
            if policy not in {"preserve", "neutralize_old_text"}:
                errors.append(f"stages.material_validation.data.policies.{key}: 지원하지 않는 정책이에요")
                continue
            if policy != "neutralize_old_text":
                continue
            descriptor = material_masks.get(key) if isinstance(material_masks, dict) else None
            if not isinstance(descriptor, dict):
                errors.append(f"stages.material_validation.data.material_masks.{key}: 마스크가 없어요")
                continue
            mask_path, path_errors = _project_file(
                project_root,
                descriptor.get("path"),
                f"stages.material_validation.data.material_masks.{key}.path",
            )
            errors.extend(path_errors)
            checksum = descriptor.get("sha256")
            if not _valid_sha256(checksum):
                errors.append(
                    f"stages.material_validation.data.material_masks.{key}.sha256: SHA-256 형식이 아니에요"
                )
            elif mask_path is not None:
                if not mask_path.is_file():
                    errors.append(
                        f"stages.material_validation.data.material_masks.{key}.path: 파일이 없어요"
                    )
                elif _sha256(mask_path) != checksum:
                    errors.append(
                        f"stages.material_validation.data.material_masks.{key}: 현재 파일 SHA-256이 달라요"
                    )
            method = descriptor.get("method")
            if method != "inpaint":
                errors.append(
                    f"stages.material_validation.data.material_masks.{key}.method: "
                    "inpaint여야 해요"
                )
    return errors


def _validate_release_metrics(stages: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    mip = stages.get("mip_validation", {}).get("data", {})
    if not isinstance(mip.get("checked_mips"), list) or not mip["checked_mips"]:
        errors.append("stages.mip_validation.data.checked_mips: 검사한 밉 목록이 없어요")
    if mip.get("missing_mips") != 0:
        errors.append("stages.mip_validation.data.missing_mips: 0이어야 해요")
    bundle = stages.get("bundle_validation", {}).get("data", {})
    for field in ("layout_equal", "bytes_equal_outside_payloads", "roundtrip_passed"):
        if bundle.get(field) is not True:
            errors.append(f"stages.bundle_validation.data.{field}: true여야 해요")
    runtime = stages.get("runtime_validation", {}).get("data", {})
    if not isinstance(runtime.get("capture_matrix"), list) or not runtime["capture_matrix"]:
        errors.append("stages.runtime_validation.data.capture_matrix: 실제 렌더 목록이 없어요")
    for field, wanted in (
        ("foreign_text_detected", False),
        ("alignment_passed", True),
        ("seam_passed", True),
    ):
        if runtime.get(field) != wanted:
            errors.append(f"stages.runtime_validation.data.{field}: {wanted!r}여야 해요")
    release = stages.get("release_validation", {}).get("data", {})
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
    for name in STAGES:
        stage = stages.get(name)
        if not isinstance(stage, dict):
            errors.append(f"stages.{name}: 단계 객체가 없어요")
            continue
        if stage.get("status") not in STATUSES:
            errors.append(f"stages.{name}.status: 지원하는 상태가 아니에요")
        errors.extend(
            _validate_evidence(
                stage.get("evidence"),
                f"stages.{name}.evidence",
                project_root=project_root,
                required=name in THROUGH[through] and stage.get("status") == "pass",
            )
        )
        if not isinstance(stage.get("data"), dict):
            errors.append(f"stages.{name}.data: 객체가 아니에요")
    for name in THROUGH[through]:
        stage = stages.get(name)
        if isinstance(stage, dict) and stage.get("status") != "pass":
            errors.append(f"stages.{name}: {stage.get('status', 'missing')} 상태라 {through} 게이트를 통과할 수 없어요")

    if through in {"analysis", "candidate", "material", "release"}:
        ocr_detections = stages.get("source_ocr", {}).get("data", {}).get("detections")
        visual_regions = stages.get("source_visual", {}).get("data", {}).get("regions")
        cross_regions = stages.get("cross_validation", {}).get("data", {}).get("regions")
        conflicts = stages.get("cross_validation", {}).get("data", {}).get("conflicts")
        translations = stages.get("translation", {}).get("data", {}).get("regions")
        for label, values in (
            ("source_ocr.data.detections", ocr_detections),
            ("source_visual.data.regions", visual_regions),
            ("cross_validation.data.regions", cross_regions),
            ("translation.data.regions", translations),
        ):
            if not isinstance(values, list):
                errors.append(f"{label}: 배열이 아니에요")
        if not isinstance(conflicts, list):
            errors.append("cross_validation.data.conflicts: 배열이 아니에요")
        elif conflicts:
            errors.append(f"cross_validation.data.conflicts: 충돌 {len(conflicts)}개가 남아 있어요")
        if isinstance(ocr_detections, list):
            for index, region in enumerate(ocr_detections):
                errors.extend(_validate_region(region, f"source_ocr.data.detections[{index}]", ocr=True))
        if isinstance(visual_regions, list):
            for index, region in enumerate(visual_regions):
                errors.extend(_validate_region(region, f"source_visual.data.regions[{index}]", ocr=False))
        if isinstance(cross_regions, list):
            for index, region in enumerate(cross_regions):
                errors.extend(
                    _validate_cross_region(region, f"cross_validation.data.regions[{index}]")
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
            for label, values in (
                ("source_ocr.data.detections", ocr_detections),
                ("source_visual.data.regions", visual_regions),
                ("cross_validation.data.regions", cross_regions),
                ("translation.data.regions", translations),
            ):
                if isinstance(values, list) and not values:
                    errors.append(f"{label}: 현지화 대상인데 판독 영역이 없어요")
        if all(isinstance(values, list) for values in (ocr_detections, visual_regions, cross_regions)):
            source_ids = {
                str(region.get("region_id"))
                for values in (ocr_detections, visual_regions)
                for region in values
                if isinstance(region, dict) and region.get("region_id")
            }
            cross_ocr_ids = {
                str(region.get("ocr_region_id"))
                for region in cross_regions
                if isinstance(region, dict) and _nonempty_string(region.get("ocr_region_id"))
            }
            cross_visual_ids = {
                str(region.get("visual_region_id"))
                for region in cross_regions
                if isinstance(region, dict) and _nonempty_string(region.get("visual_region_id"))
            }
            ocr_ids = {
                str(region.get("region_id"))
                for region in ocr_detections
                if isinstance(region, dict) and region.get("region_id")
            }
            visual_ids = {
                str(region.get("region_id"))
                for region in visual_regions
                if isinstance(region, dict) and region.get("region_id")
            }
            missing_ocr_ids = sorted(ocr_ids - cross_ocr_ids)
            missing_visual_ids = sorted(visual_ids - cross_visual_ids)
            if missing_ocr_ids:
                errors.append(f"cross_validation: OCR 영역이 누락됐어요: {missing_ocr_ids}")
            if missing_visual_ids:
                errors.append(f"cross_validation: 시각 판독 영역이 누락됐어요: {missing_visual_ids}")

    if through in {"candidate", "material", "release"}:
        if record.get("action") == "localize":
            errors.extend(_validate_masks(stages.get("edit_plan", {}).get("data"), source, project_root))
        else:
            candidate_data = stages.get("candidate_validation", {}).get("data", {})
            if candidate_data.get("rgba_equal") is not True:
                errors.append("보존 대상은 stages.candidate_validation.data.rgba_equal이 true여야 해요")
        errors.extend(_validate_candidate_metrics(stages.get("candidate_validation", {}).get("data")))
        errors.extend(_validate_post_checks(stages))
    if through in {"material", "release"}:
        errors.extend(
            _validate_material_metrics(
                stages.get("material_validation", {}).get("data"), project_root
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
